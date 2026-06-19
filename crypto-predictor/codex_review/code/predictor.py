"""Núcleo del crypto-predictor: fetch klines + EWMA vol + distribución 1h.

A 1h de horizonte el log-return esperado de BTC se modela como cero.
El drift horario queda disponible solo para análisis offline: falló OOS y no se
activa en live. La varianza usa realized vol de los últimos ~24h y un EWMA
λ=0.97 escalable a σ_1h por √60.

Distribución de log-returns: Student-t con df=4 (cripto tiene colas gordas;
df=4 es el estándar empírico en literatura de riesgo). Re-escalada para
preservar la varianza medida: scale = σ_h / √(df/(df-2)) = σ_h/√2.
P(price > X) = 1 - F_T4(√2 · log(X/p_now)/σ_h).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
DEFAULT_SYMBOL = "BTCUSDT"

# Parámetros del modelo
EWMA_LAMBDA = 0.97          # tunado vs 0.94: reduce |z|>2 de 1.70× a 1.46×
LOOKBACK_MINUTES = 1440     # 24h de 1m candles para inicializar EWMA
HORIZON_MINUTES = 60        # predicción a 1h
DIST_DF = 4                 # Student-t df. None ⇒ Gaussiana
AST_UTC_OFFSET_SECONDS = -4 * 3600
ENABLE_HOURLY_DRIFT = False  # Q1 failed OOS; keep mean-zero live.

# Q2 adaptive EWMA failed OOS too; keep disabled unless revalidated.
USE_ADAPTIVE_EWMA = False
ADAPTIVE_LAMBDA_MIN = 0.950
ADAPTIVE_LAMBDA_MAX = 0.985
ADAPTIVE_LAMBDA_A = 0.5
ADAPTIVE_LAMBDA_B = 1.0

# Posterior mean of E[log(actual/now) / sigma_h | made_at hour AST].
# Source: data/hourly_calls.csv settled rows, computed 2026-06-18.
# Shrinkage: (n_h * mean_h + DRIFT_PRIOR_N * global_mean) /
# (n_h + DRIFT_PRIOR_N). PRIOR_N=120 was best among {12,30,60,120}
# in weekly walk-forward OOS LL over the 60d BTC predictions DB. Values are
# dimensionless z-units, later multiplied by sigma_h.
DRIFT_PRIOR_N = 120
HOUR_DRIFT_Z_BY_AST: dict[int, float] = {
    0: -0.081471,
    1: -0.094548,
    2: -0.106957,
    3: -0.143455,
    4: -0.077265,
    5: -0.084968,
    6: -0.115996,
    7: -0.101758,
    8: -0.040852,
    9: -0.215202,
    10: -0.096774,
    11: -0.041349,
    12: -0.027528,
    13: -0.019783,
    14: -0.115083,
    15: -0.052383,
    16: -0.062404,
    17: -0.189593,
    18: -0.093302,
    19: -0.109410,
    20: -0.068567,
    21: -0.113959,
    22: -0.060238,
    23: -0.155896,
}


@dataclass
class Kline:
    open_time: int           # ms epoch (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Prediction:
    symbol: str
    now_price: float          # último close
    sigma_1m: float           # vol por minuto (log-return scale)
    sigma_horizon: float      # vol al horizonte (σ_1m · √H)
    horizon_min: float        # minutos hasta target_at
    fetched_at: float = field(default_factory=time.time)
    n_candles: int = 0
    target_at: float = 0.0    # unix epoch del cierre objetivo (XX:00:00 UTC)
    drift_z: float = 0.0      # mean log-return / sigma_h for made_at hour AST


def fetch_klines(symbol: str = DEFAULT_SYMBOL, interval: str = "1m",
                 limit: int = 500, timeout: float = 10.0) -> list[Kline]:
    """Pull last `limit` klines from Binance public API. No auth needed."""
    r = requests.get(f"{BINANCE_BASE}/klines",
                     params={"symbol": symbol, "interval": interval, "limit": limit},
                     timeout=timeout)
    r.raise_for_status()
    out = []
    for k in r.json():
        out.append(Kline(
            open_time=int(k[0]),
            open=float(k[1]), high=float(k[2]),
            low=float(k[3]), close=float(k[4]),
            volume=float(k[5]),
        ))
    return out


def log_returns(closes: list[float]) -> list[float]:
    """r_t = ln(p_t / p_{t-1})"""
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def ewma_sigma(returns: list[float], lam: float = EWMA_LAMBDA) -> float:
    """EWMA std deviation (RiskMetrics): σ²_t = λ·σ²_{t-1} + (1-λ)·r²_{t-1}.
    Inicializa con varianza simple de la primera mitad para arrancar estable.
    """
    if len(returns) < 10:
        return 0.0
    half = len(returns) // 2
    var = sum(r * r for r in returns[:half]) / half
    for r in returns[half:]:
        var = lam * var + (1 - lam) * r * r
    return math.sqrt(var)


def adaptive_lambda(r: float, sigma_prev: float,
                    lambda_min: float = ADAPTIVE_LAMBDA_MIN,
                    lambda_max: float = ADAPTIVE_LAMBDA_MAX,
                    a: float = ADAPTIVE_LAMBDA_A,
                    b: float = ADAPTIVE_LAMBDA_B) -> float:
    """Map variance shock |r²/σ² - 1| to λ. Larger shocks lower λ."""
    if sigma_prev <= 0:
        return lambda_max
    shock = abs((r * r) / (sigma_prev * sigma_prev) - 1.0)
    try:
        response = 1.0 / (1.0 + math.exp(-a * (shock - b)))
    except OverflowError:
        response = 0.0 if shock < b else 1.0
    return lambda_max - (lambda_max - lambda_min) * response


def ewma_sigma_adaptive(returns: list[float],
                        lambda_min: float = ADAPTIVE_LAMBDA_MIN,
                        lambda_max: float = ADAPTIVE_LAMBDA_MAX,
                        a: float = ADAPTIVE_LAMBDA_A,
                        b: float = ADAPTIVE_LAMBDA_B) -> float:
    """Adaptive EWMA variant. Implemented for offline testing; disabled live."""
    if len(returns) < 10:
        return 0.0
    half = len(returns) // 2
    var = sum(r * r for r in returns[:half]) / half
    for r in returns[half:]:
        sigma_prev = math.sqrt(var)
        lam = adaptive_lambda(r, sigma_prev, lambda_min, lambda_max, a, b)
        var = lam * var + (1 - lam) * r * r
    return math.sqrt(var)


def ast_hour(ts: Optional[float] = None) -> int:
    """Hour in Atlantic Standard Time (UTC-4, no DST) for a unix timestamp."""
    if ts is None:
        ts = time.time()
    return time.gmtime(ts + AST_UTC_OFFSET_SECONDS).tm_hour


def hourly_drift_z(ts: Optional[float] = None) -> float:
    """Shrinked hourly drift in sigma_h units for the prediction made_at time."""
    return HOUR_DRIFT_Z_BY_AST.get(ast_hour(ts), 0.0)


def next_clock_hour(now: Optional[float] = None) -> float:
    """Próximo XX:00:00 UTC (estricto, si now ya es XX:00 → siguiente hora)."""
    if now is None:
        now = time.time()
    return float((int(now) // 3600 + 1) * 3600)


def build_prediction(symbol: str = DEFAULT_SYMBOL,
                     target_at: Optional[float] = None) -> Prediction:
    """Fetch + cómputo end-to-end. Por defecto target = próxima hora UTC en punto."""
    now = time.time()
    if target_at is None:
        target_at = next_clock_hour(now)
    horizon_min = max(0.0, (target_at - now) / 60.0)
    klines = fetch_klines(symbol=symbol, interval="1m",
                          limit=min(LOOKBACK_MINUTES, 1000))
    if len(klines) < 30:
        raise RuntimeError(f"too few klines: {len(klines)}")
    closes = [k.close for k in klines]
    rets = log_returns(closes)
    sigma_1m = ewma_sigma_adaptive(rets) if USE_ADAPTIVE_EWMA else ewma_sigma(rets)
    sigma_h = sigma_1m * math.sqrt(horizon_min)
    return Prediction(
        symbol=symbol,
        now_price=closes[-1],
        sigma_1m=sigma_1m,
        sigma_horizon=sigma_h,
        horizon_min=horizon_min,
        fetched_at=now,
        n_candles=len(klines),
        target_at=target_at,
        drift_z=hourly_drift_z(now) if ENABLE_HOURLY_DRIFT else 0.0,
    )


def _norm_cdf(z: float) -> float:
    """Φ(z) — CDF Gaussiana estándar via erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _t4_cdf(t: float) -> float:
    """CDF de Student-t con df=4. Closed form vía integración de la pdf."""
    u = t / math.sqrt(t * t + 4.0)
    return 0.5 + 0.75 * u - 0.25 * u ** 3


def _t4_inv(p: float) -> float:
    """Inverso de F_T4 vía Newton sobre la closed form."""
    if p <= 0 or p >= 1:
        raise ValueError("p must be in (0,1)")
    # Initial guess: gaussian inverse escalada por √(df/(df-2)) = √2
    z = _inv_norm(p) * math.sqrt(2.0)
    for _ in range(50):
        f = _t4_cdf(z) - p
        if abs(f) < 1e-12:
            break
        pdf = 12.0 / (z * z + 4.0) ** 2.5
        z -= f / pdf
    return z


def _dist_cdf(z: float) -> float:
    """CDF de la distribución de log-returns ya re-escalada para que
    var(returns) = σ²_h. Argumento z = log(X/p_now)/σ_h (z-score gaussiano)."""
    if DIST_DF is None:
        return _norm_cdf(z)
    # Student-t: scale = σ_h * √((df-2)/df), entonces el arg en T_df es
    # log(X/p_now)/scale = z * √(df/(df-2))
    df = DIST_DF
    arg = z * math.sqrt(df / (df - 2))
    if df == 4:
        return _t4_cdf(arg)
    raise NotImplementedError(f"DIST_DF={df} no implementado")


def _dist_inv(q: float) -> float:
    """Inverso de _dist_cdf: devuelve z (gaussian-equivalent) tal que
    _dist_cdf(z) == q."""
    if DIST_DF is None:
        return _inv_norm(q)
    df = DIST_DF
    if df == 4:
        t = _t4_inv(q)
        return t / math.sqrt(df / (df - 2))
    raise NotImplementedError(f"DIST_DF={df} no implementado")


def prob_above(pred: Prediction, threshold: float) -> float:
    """P(price_horizon > threshold) bajo log-{Gaussiana, Student-t}."""
    if pred.sigma_horizon <= 0:
        return 1.0 if pred.now_price > threshold else 0.0
    z = math.log(threshold / pred.now_price) / pred.sigma_horizon - pred.drift_z
    return 1.0 - _dist_cdf(z)


def quantile(pred: Prediction, q: float) -> float:
    """Cuantil q ∈ (0,1) del precio al horizonte."""
    if pred.sigma_horizon <= 0:
        return pred.now_price
    z = _dist_inv(q)
    return pred.now_price * math.exp((pred.drift_z + z) * pred.sigma_horizon)


def _inv_norm(p: float) -> float:
    """Inverse Φ via Acklam approximation (precisión ~1e-9)."""
    if p <= 0 or p >= 1:
        raise ValueError("p must be in (0,1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
           ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def threshold_ladder(pred: Prediction, n: int = 10, step_pct: float = 0.005) -> list[dict]:
    """Tabla de P(>X) centrada en precio actual, ±n pasos de step_pct cada uno.
    Default: ±10 × 0.5% = ±5% del precio actual."""
    rows = []
    for i in range(-n, n + 1):
        thr = pred.now_price * (1 + i * step_pct)
        rows.append({
            "threshold": thr,
            "delta_pct": i * step_pct * 100,
            "p_above": prob_above(pred, thr),
        })
    return rows


def nice_step(now_price: float, target_pct: float = 0.0013) -> float:
    """Step absoluto "redondo" cercano a target_pct del precio.
    BTC ~$79k → $100 ; ETH ~$2.3k → $2 ; SOL ~$84 → $0.1 ; XRP ~$1.4 → $0.002."""
    target = now_price * target_pct
    exp = math.floor(math.log10(target))
    base = target / (10 ** exp)
    if base < 1.5:
        nice = 1
    elif base < 3.5:
        nice = 2
    elif base < 7.5:
        nice = 5
    else:
        nice = 10
    return nice * (10 ** exp)


def threshold_ladder_abs(pred: Prediction, n: int = 10,
                         step_abs: Optional[float] = None) -> list[dict]:
    """Ladder con step ABSOLUTO (ej. $100), centrada en el múltiplo de
    step_abs más cercano al precio actual. Si step_abs es None usa nice_step."""
    if step_abs is None:
        step_abs = nice_step(pred.now_price)
    center = round(pred.now_price / step_abs) * step_abs
    rows = []
    for i in range(-n, n + 1):
        thr = center + i * step_abs
        rows.append({
            "threshold": thr,
            "delta_pct": (thr - pred.now_price) / pred.now_price * 100,
            "p_above": prob_above(pred, thr),
            "is_center": i == 0,
            "step_abs": step_abs,
        })
    return rows
