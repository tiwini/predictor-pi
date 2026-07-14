"""BTC hourly point-call.

A cada hora en punto (XX:00 UTC) el sistema publica un valor decimal —
el cuantil p70 de la distribución log-Student-t a 1h — tal que BTC tiene
~70% de probabilidad de NO sobrepasarlo en XX+1:00. Pasada la hora,
settleamos con el open del candle 1m en target_at: win si actual ≤ call.

Métricas:
  - streak: hits consecutivos desde la última settled call hacia atrás.
  - empirical_rate: wins / settled (debe converger a `quantile`).
  - Kalshi: strike discreto más cercano al call_value + edge en pp
    (modelo NO al strike vs Kalshi NO al strike).
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import calibration as _cal
import kalshi as _kalshi
import predictor as _pred

DB_PATH = _cal.DB_PATH
QUANTILE = 0.70           # p70 inicial; tunable luego
SYMBOL = "BTCUSDT"
MIN_HORIZON_MIN = 55      # solo aceptamos calls cerca de XX:00 (horizon ~60m)

# Fable R8-review 2026-07-09: throttle del retry pass.
# poll_loop corre a POLL_SEC=5, y el retry match'ea rows con n_venues<4 por
# 1h → sin throttle son 720 refetches/hora/row contra APIs free-tier +
# poll loop degradado hasta 15s durante cada hora post-settle.
_RETRY_SCAN_MIN_INTERVAL_S = 300.0   # gate: retry corre a lo sumo 1×/5min
_RETRY_UPGRADE_WINDOW_S = 900.0      # upgrade n→n+1: ventana 15min
_RETRY_RESCUE_WINDOW_S = 3600.0      # rescate de NULLs: hora completa
_last_retry_scan_ts: float = 0.0     # module-level timestamp del último scan

SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    made_at REAL NOT NULL,
    target_at REAL NOT NULL,
    now_price REAL NOT NULL,
    sigma_h REAL NOT NULL,
    quantile REAL NOT NULL,
    call_value REAL NOT NULL,
    kalshi_strike REAL,
    kalshi_no_at_strike REAL,
    kalshi_no_at_call REAL,
    model_no_at_strike REAL,
    edge_pp REAL,
    actual_price REAL,
    won INTEGER,
    settled_at REAL,
    z REAL,
    proxy_price_at_settle REAL,
    kalshi_null_reason TEXT,
    kalshi_curve_json TEXT,
    UNIQUE(symbol, target_at)
);
CREATE INDEX IF NOT EXISTS idx_hcalls_unsettled
  ON hourly_calls(symbol, target_at) WHERE actual_price IS NULL;
"""


@contextmanager
def _conn(db_path: str = DB_PATH):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db(db_path: str = DB_PATH) -> None:
    with _conn(db_path) as c:
        c.executescript(SCHEMA)
        existing = [r[1] for r in c.execute("PRAGMA table_info(hourly_calls)")]
        if "z" not in existing:
            c.execute("ALTER TABLE hourly_calls ADD COLUMN z REAL")
        if "proxy_price_at_settle" not in existing:
            c.execute("ALTER TABLE hourly_calls ADD COLUMN "
                      "proxy_price_at_settle REAL")
        if "kalshi_null_reason" not in existing:
            c.execute("ALTER TABLE hourly_calls ADD COLUMN "
                      "kalshi_null_reason TEXT")
        if "kalshi_curve_json" not in existing:
            c.execute("ALTER TABLE hourly_calls ADD COLUMN "
                      "kalshi_curve_json TEXT")
        # R6 2026-07-08: BRTI proxy multi-venue + features
        _new = [
            ("bitstamp_price_at_settle", "REAL"),
            ("brti_proxy_price", "REAL"),
            ("brti_proxy_n_venues", "INTEGER"),
            ("momentum_pct_per_min", "REAL"),
            ("ob_imbalance", "REAL"),
            ("taker_buy_ratio", "REAL"),
            ("funding_rate", "REAL"),
            ("fng", "INTEGER"),
            ("vol_regime_ratio", "REAL"),
            ("features_max_age_s", "REAL"),
        ]
        for name, typ in _new:
            if name not in existing:
                c.execute(f"ALTER TABLE hourly_calls ADD COLUMN {name} {typ}")


@dataclass
class CallRow:
    id: int
    symbol: str
    made_at: float
    target_at: float
    now_price: float
    sigma_h: float
    quantile: float
    call_value: float
    kalshi_strike: Optional[float]
    kalshi_no_at_strike: Optional[float]
    kalshi_no_at_call: Optional[float]
    model_no_at_strike: Optional[float]
    edge_pp: Optional[float]
    actual_price: Optional[float]
    won: Optional[int]
    settled_at: Optional[float]
    z: Optional[float] = None
    proxy_price_at_settle: Optional[float] = None
    kalshi_null_reason: Optional[str] = None
    kalshi_curve_json: Optional[str] = None
    # BRTI proxy multi-venue (R6 2026-07-08)
    bitstamp_price_at_settle: Optional[float] = None
    brti_proxy_price: Optional[float] = None
    brti_proxy_n_venues: Optional[int] = None
    # Features intraday (R6 2026-07-08)
    momentum_pct_per_min: Optional[float] = None
    ob_imbalance: Optional[float] = None
    taker_buy_ratio: Optional[float] = None
    funding_rate: Optional[float] = None
    fng: Optional[int] = None
    vol_regime_ratio: Optional[float] = None
    features_max_age_s: Optional[float] = None


def _row_to_call(r: sqlite3.Row) -> CallRow:
    return CallRow(**{k: r[k] for k in r.keys()})


def make_call(pred: _pred.Prediction, q: float = QUANTILE,
              min_horizon_min: float = MIN_HORIZON_MIN,
              db_path: str = DB_PATH,
              features: Optional[dict] = None) -> Optional[int]:
    """Inserta un nuevo call para (symbol, target_at). Idempotente: si ya
    existe call para ese (symbol, target_at) devuelve None.

    Solo dispara cuando horizon_min ≥ min_horizon_min — así garantizamos
    que la call se hizo "cerca de la hora en punto" (no a 5 min del cierre).

    features (opcional R7): dict con snapshot al momento del call.
      Keys esperadas — cualquiera faltante o None → NULL en DB:
        momentum_pct_per_min (float)   %/min slope log-close últimos 10m
        ob_imbalance         (float)   Binance depth L20, [−1,+1]
                                       (bid−ask)/(bid+ask); R6 spec.
                                       +1 = todo bid, −1 = todo ask
        taker_buy_ratio      (float)   aggTrades 5m, [0,1] buy/(buy+sell)
        funding_rate         (float)   Binance premiumIndex fracción nativa 8h
        fng                  (int)     Fear&Greed 0..100 (alternative.me)
        vol_regime_ratio     (float)   σ_fast(10m)/σ_slow(60m) RMS EWMA r²
        features_max_age_s   (float)   max age del snapshot _external en s
    """
    if pred.symbol != SYMBOL:
        return None
    if pred.horizon_min < min_horizon_min:
        return None

    call_value = _pred.quantile(pred, q)

    kalshi_strike = None
    kalshi_no_at_strike = None
    kalshi_no_at_call = None
    model_no_at_strike = None
    edge_pp = None
    kalshi_null_reason = None
    kalshi_curve_json = None
    try:
        ns, curve, kalshi_null_reason = _kalshi.curve_and_strike_with_reason(
            pred.target_at, call_value)
    except Exception as e:
        ns = None
        curve = None
        kalshi_null_reason = f"unhandled:{type(e).__name__}"
    if ns is not None:
        kalshi_strike, mid_yes = ns
        kalshi_no_at_strike = 1.0 - mid_yes
        p_above_strike = _pred.prob_above(pred, kalshi_strike)
        model_no_at_strike = 1.0 - p_above_strike
        edge_pp = (model_no_at_strike - kalshi_no_at_strike) * 100.0
    if curve is not None:
        strikes, mids, bids, asks = curve
        kalshi_curve_json = json.dumps(
            {"s": strikes, "m": mids, "b": bids, "a": asks},
            separators=(",", ":"))
    try:
        kp_above_at_call = _kalshi.implied_above(pred.target_at, call_value)
        if kp_above_at_call is not None:
            kalshi_no_at_call = 1.0 - kp_above_at_call
    except Exception:
        pass

    ftr = features or {}
    momentum = ftr.get("momentum_pct_per_min")
    ob_imb = ftr.get("ob_imbalance")
    taker_br = ftr.get("taker_buy_ratio")
    funding_rate = ftr.get("funding_rate")
    fng = ftr.get("fng")
    vol_ratio = ftr.get("vol_regime_ratio")
    fmax_age = ftr.get("features_max_age_s")

    with _conn(db_path) as c:
        try:
            cur = c.execute(
                "INSERT INTO hourly_calls(symbol, made_at, target_at, now_price, "
                "sigma_h, quantile, call_value, kalshi_strike, kalshi_no_at_strike, "
                "kalshi_no_at_call, model_no_at_strike, edge_pp, "
                "kalshi_null_reason, kalshi_curve_json, "
                "momentum_pct_per_min, ob_imbalance, taker_buy_ratio, "
                "funding_rate, fng, vol_regime_ratio, features_max_age_s) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pred.symbol, pred.fetched_at, pred.target_at, pred.now_price,
                 pred.sigma_horizon, q, call_value, kalshi_strike,
                 kalshi_no_at_strike, kalshi_no_at_call,
                 model_no_at_strike, edge_pp, kalshi_null_reason,
                 kalshi_curve_json,
                 momentum, ob_imb, taker_br, funding_rate, fng,
                 vol_ratio, fmax_age),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def _coinbase_price_at(symbol: str, target_at: float,
                       timeout: float = 5.0) -> float:
    """Open del candle 1m en Coinbase Pro. BTCUSDT → BTC-USD.

    Kalshi liquida con CFB BRTI (mediana de CB/KR/BS/GE). Coinbase es uno
    de los 4 constituyentes; ver `_multi_venue_prices` para la mediana.
    """
    import requests
    if symbol != "BTCUSDT":
        raise ValueError(f"proxy_price_at solo BTC — got {symbol}")
    r = requests.get(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        params={"granularity": 60,
                "start": int(target_at),
                "end": int(target_at) + 60},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"coinbase kline at {target_at} not available")
    # Coinbase devuelve [[time, low, high, open, close, volume], ...] descendente.
    for k in data:
        if int(k[0]) == int(target_at):
            return float(k[3])
    raise ValueError(f"coinbase kline open_time mismatch for {target_at}")


def _bitstamp_price_at(symbol: str, target_at: float,
                       timeout: float = 5.0) -> float:
    """Open del candle 1m en Bitstamp. Constituyente #2 del BRTI CFB."""
    import requests
    if symbol != "BTCUSDT":
        raise ValueError(f"bitstamp_price_at solo BTC — got {symbol}")
    r = requests.get(
        "https://www.bitstamp.net/api/v2/ohlc/btcusd/",
        params={"step": 60, "limit": 1, "start": int(target_at)},
        timeout=timeout,
    )
    r.raise_for_status()
    ohlc = r.json().get("data", {}).get("ohlc", [])
    if not ohlc:
        raise ValueError("bitstamp empty ohlc")
    c = ohlc[0]
    if int(c["timestamp"]) != int(target_at):
        raise ValueError(f"bitstamp ts mismatch got={c['timestamp']}")
    return float(c["open"])


def _kraken_price_at(symbol: str, target_at: float,
                     timeout: float = 5.0) -> float:
    """Open del candle 1m en Kraken. Constituyente #3.

    Kraken OHLC solo devuelve ~720 candles a 1m (12h). Válido solo forward
    (target_at dentro de la ventana reciente).
    """
    import requests
    if symbol != "BTCUSDT":
        raise ValueError(f"kraken_price_at solo BTC — got {symbol}")
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": "XBTUSD", "interval": 1, "since": int(target_at) - 60},
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise ValueError(f"kraken err: {j['error']}")
    data = j.get("result", {})
    for k, v in data.items():
        if k != "last" and isinstance(v, list):
            for c in v:
                if int(c[0]) == int(target_at):
                    return float(c[1])
    raise ValueError(f"kraken candle at {target_at} missing")


def _gemini_price_at(symbol: str, target_at: float,
                     timeout: float = 5.0) -> float:
    """Open del candle 1m en Gemini. Constituyente #4.

    Gemini /v2/candles/BTCUSD/1m devuelve ~500 candles recientes sin rango.
    Válido solo forward.
    """
    import requests
    if symbol != "BTCUSDT":
        raise ValueError(f"gemini_price_at solo BTC — got {symbol}")
    r = requests.get(
        "https://api.gemini.com/v2/candles/BTCUSD/1m",
        timeout=timeout,
    )
    r.raise_for_status()
    candles = r.json()
    ts_ms = int(target_at) * 1000
    for c in candles:
        if int(c[0]) == ts_ms:
            return float(c[1])
    raise ValueError(f"gemini candle at {target_at} missing")


def _multi_venue_prices(symbol: str, target_at: float,
                        per_timeout: float = 5.0,
                        budget_s: float = 15.0,
                        skip: Optional[set] = None) -> dict:
    """Fetch open del candle 1m en 4 venues (secuencial, por presupuesto).

    Orden: Coinbase → Bitstamp → Kraken → Gemini. Fable R6:
      - timeout per-venue = 5s (no 10s).
      - budget_s = 15s: tras >=2 venues attempted y elapsed>15s, break.
      - Fable R8-review 2026-07-09: último venue usa
        per_timeout_last = min(per_timeout, budget_s − elapsed), aprovechando
        segundos sobrantes cuando los primeros fueron rápidos. Sinérgico con
        el throttle del retry — más n=4 first-pass = menos retries.

    Salto histórico(N=2, Coinbase+Bitstamp) → forward(N=3-4 según respondan).
    Los 4 valores son constituyentes CFB BRTI. Consumidor calcula mediana.

    skip: set de nombres de venue a NO refetchear (ya poblados en el caller).
    Fable R8-review: el retry pasa el set de venues guardados en la row para
    no gastar requests deterministas en candles históricos que ya se
    conocen. Ausente en el settle inicial (skip={}).

    Devuelve {coinbase, bitstamp, kraken, gemini} → price float o None si
    el fetch falló / se saltó por budget / estaba en skip.
    """
    if symbol != "BTCUSDT":
        return {"coinbase": None, "bitstamp": None,
                "kraken": None, "gemini": None}
    skip = skip or set()
    venues = [
        ("coinbase", _coinbase_price_at),
        ("bitstamp", _bitstamp_price_at),
        ("kraken", _kraken_price_at),
        ("gemini", _gemini_price_at),
    ]
    out = {"coinbase": None, "bitstamp": None, "kraken": None, "gemini": None}
    to_fetch = [(n, f) for n, f in venues if n not in skip]
    if not to_fetch:
        return out
    t0 = time.time()
    n_total = len(to_fetch)
    for i, (name, fn) in enumerate(to_fetch):
        # Último venue: adaptive timeout aprovecha budget sobrante.
        if i == n_total - 1:
            remaining = budget_s - (time.time() - t0)
            if remaining < 0.5:
                # No hay budget útil — skip explícito (evita gastar 0.5s piso).
                break
            eff_timeout = min(per_timeout, remaining)
        else:
            eff_timeout = per_timeout
        try:
            out[name] = float(fn(symbol, target_at, timeout=eff_timeout))
        except Exception as e:
            print(f"  [{name}] fetch fail: {type(e).__name__}: {e}",
                  file=sys.stderr)
        elapsed = time.time() - t0
        if i >= 1 and elapsed > budget_s:
            print(f"  [multi_venue] budget {budget_s}s consumido "
                  f"tras {i+1} venues (elapsed {elapsed:.1f}s), skipping resto",
                  file=sys.stderr)
            break
    return out


def settle_due(db_path: str = DB_PATH, now: Optional[float] = None,
               price_fn=None, multi_venue_fn=None) -> int:
    """Settlea calls con target_at ≤ now. Win si actual ≤ call_value.

    R7: multi-venue BRTI-proxy escrito en cada settle. multi_venue_fn devuelve
    dict {coinbase, bitstamp, kraken, gemini} → prices o None. Persiste:
      - proxy_price_at_settle   ← Coinbase (backward compat)
      - bitstamp_price_at_settle ← Bitstamp
      - brti_proxy_price         ← statistics.median de los no-None (mean si N=2)
      - brti_proxy_n_venues      ← count de no-None
    Log a stderr cuando n_venues < 3 (health de fetchers).
    Errores no bloquean el settle — columnas quedan NULL y el resto procede.
    """
    if now is None:
        now = time.time()
    if price_fn is None:
        price_fn = _cal._price_at
    if multi_venue_fn is None:
        multi_venue_fn = _multi_venue_prices
    n = 0
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT id, symbol, target_at, call_value, now_price, sigma_h "
            "FROM hourly_calls "
            "WHERE actual_price IS NULL AND target_at <= ?", (now,)
        ).fetchall()
        for r in rows:
            try:
                price = price_fn(r["symbol"], r["target_at"])
            except Exception:
                continue
            won = 1 if price <= r["call_value"] else 0
            # z = log(actual/now) / σ_h — outcome continuo standardized.
            # Fable 2026-07-04: la calibración honesta se hace sobre PIT(z),
            # no sobre won (1 bit del outcome). z NULL sólo si σ_h≤0.
            try:
                z = (math.log(float(price) / r["now_price"]) / r["sigma_h"]
                     if r["sigma_h"] > 0 and r["now_price"] > 0 and price > 0
                     else None)
            except (ValueError, ZeroDivisionError):
                z = None
            venues = multi_venue_fn(r["symbol"], r["target_at"])
            cb_px = venues.get("coinbase")
            bs_px = venues.get("bitstamp")
            available = [v for v in venues.values() if v is not None]
            n_venues = len(available)
            brti_px = statistics.median(available) if available else None
            if n_venues < 3:
                got = [k for k, v in venues.items() if v is not None]
                print(f"[hcall] settle id={r['id']} n_venues={n_venues} "
                      f"got={got}", file=sys.stderr)
            c.execute(
                "UPDATE hourly_calls SET actual_price=?, won=?, settled_at=?, "
                "z=?, proxy_price_at_settle=?, bitstamp_price_at_settle=?, "
                "brti_proxy_price=?, brti_proxy_n_venues=? WHERE id=?",
                (float(price), won, now, z, cb_px, bs_px, brti_px, n_venues,
                 r["id"]),
            )
            n += 1
        # Retry pass: rescate de NULLs (rescue) + upgrade oportunista de
        # n_venues (upgrade). Fable R8-review 2026-07-09: throttle en 3
        # capas para no saturar APIs / poll loop dado que POLL_SEC=5 y el
        # Gemini timeout produce n=3 en ~50% de rows:
        #   1) Gate módulo: retry corre a lo sumo 1×/5min (no cada poll).
        #   2) Rescue vs upgrade con ventanas distintas: rescate de NULLs
        #      conserva 1h (propósito original), upgrade n→n+1 sólo 15min
        #      — tras 3 scans espaciados, n=3 es final y está bien.
        #   3) Fetch sólo venues faltantes: pasamos `skip=` con los venues
        #      ya guardados en la row; mediana se recompone desde
        #      saved∪new. Evita miles de requests deterministas y elimina
        #      un edge case (mediana recalculada con sólo lo que respondió
        #      esta vez podía excluir un venue previamente guardado si
        #      n_new > cur_n mezclaba distinto set).
        global _last_retry_scan_ts
        if now - _last_retry_scan_ts >= _RETRY_SCAN_MIN_INTERVAL_S:
            _last_retry_scan_ts = now
            retry_rows = c.execute(
                "SELECT id, symbol, target_at, "
                "       proxy_price_at_settle, bitstamp_price_at_settle, "
                "       brti_proxy_price, brti_proxy_n_venues, settled_at "
                "FROM hourly_calls "
                "WHERE actual_price IS NOT NULL "
                "  AND settled_at >= ? AND target_at <= ? "
                "  AND (proxy_price_at_settle IS NULL "
                "       OR bitstamp_price_at_settle IS NULL "
                "       OR brti_proxy_price IS NULL "
                "       OR (brti_proxy_n_venues < 4 AND settled_at >= ?))",
                (now - _RETRY_RESCUE_WINDOW_S, now - 90,
                 now - _RETRY_UPGRADE_WINDOW_S),
            ).fetchall()
            for r in retry_rows:
                # Fetch sólo lo faltante. La brti_* per-venue de Kraken/Gemini
                # no persiste en cols dedicadas — sólo Coinbase y Bitstamp.
                # Sabemos que Kraken/Gemini estuvieron presentes si su valor
                # entró en la mediana previa (brti_proxy_n_venues), pero no
                # cuáles individualmente. Conservador: si n_venues previo ya
                # incluye alguno de Kraken/Gemini, no podemos discernir cuál
                # → refetch los "no-Coinbase/Bitstamp" sólo si estamos en la
                # ventana de upgrade y no llegamos a 4.
                saved_px = {}
                skip: set = set()
                if r["proxy_price_at_settle"] is not None:
                    saved_px["coinbase"] = r["proxy_price_at_settle"]
                    skip.add("coinbase")
                if r["bitstamp_price_at_settle"] is not None:
                    saved_px["bitstamp"] = r["bitstamp_price_at_settle"]
                    skip.add("bitstamp")
                # Kraken/Gemini no tienen columna dedicada — refetch siempre
                # que estemos en ventana upgrade (ver test de regresión d).
                # Costo aceptado: ≤2 req/row/scan sobre Kraken+Gemini;
                # con gate 5min + ventana upgrade 15min = máx ~72 req/hora
                # sobre esos dos endpoints. Cabe holgado en cualquier
                # rate-limit razonable, y es el precio de no persistir
                # per-venue de esos dos venues en columnas dedicadas.
                venues = multi_venue_fn(r["symbol"], r["target_at"], skip=skip)
                # Combinar saved + new: mediana desde el conjunto real.
                combined = dict(saved_px)
                for k, v in venues.items():
                    if v is not None and k not in combined:
                        combined[k] = v
                available = list(combined.values())
                if not available:
                    continue
                cb_px = venues.get("coinbase")   # sólo si acabamos de conseguirlo
                bs_px = venues.get("bitstamp")
                n_new = len(available)
                cur_n = r["brti_proxy_n_venues"] or 0
                if n_new > cur_n:
                    new_median = statistics.median(available)
                    c.execute(
                        "UPDATE hourly_calls SET "
                        "  proxy_price_at_settle = COALESCE(proxy_price_at_settle, ?), "
                        "  bitstamp_price_at_settle = COALESCE(bitstamp_price_at_settle, ?), "
                        "  brti_proxy_price = ?, brti_proxy_n_venues = ? "
                        "WHERE id = ?",
                        (cb_px, bs_px, new_median, n_new, r["id"]),
                    )
                else:
                    # No ganamos venues — rellenar per-venue NULLs si Coinbase
                    # o Bitstamp acaban de responder (retry rescató uno de
                    # ellos). Mediana intacta.
                    c.execute(
                        "UPDATE hourly_calls SET "
                        "  proxy_price_at_settle = COALESCE(proxy_price_at_settle, ?), "
                        "  bitstamp_price_at_settle = COALESCE(bitstamp_price_at_settle, ?) "
                        "WHERE id = ?",
                        (cb_px, bs_px, r["id"]),
                    )
    return n


def streak(db_path: str = DB_PATH, symbol: str = SYMBOL) -> int:
    """Hits consecutivos desde la call settleada más reciente hacia atrás."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT won FROM hourly_calls WHERE symbol=? AND won IS NOT NULL "
            "ORDER BY target_at DESC", (symbol,)
        ).fetchall()
    n = 0
    for r in rows:
        if r["won"] == 1:
            n += 1
        else:
            break
    return n


def empirical_rate(db_path: str = DB_PATH, symbol: str = SYMBOL) -> dict:
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(won),0) AS w FROM hourly_calls "
            "WHERE symbol=? AND won IS NOT NULL", (symbol,)
        ).fetchone()
    n, w = row["n"], row["w"]
    return {"n": n, "wins": w, "rate": (w / n) if n > 0 else None}


def current_call(db_path: str = DB_PATH,
                 symbol: str = SYMBOL) -> Optional[CallRow]:
    """Call activa más reciente (sin settlear), si existe."""
    with _conn(db_path) as c:
        r = c.execute(
            "SELECT * FROM hourly_calls WHERE symbol=? AND actual_price IS NULL "
            "ORDER BY target_at DESC LIMIT 1", (symbol,)
        ).fetchone()
    return _row_to_call(r) if r else None


def recent(db_path: str = DB_PATH, symbol: str = SYMBOL,
           limit: int = 30) -> list[CallRow]:
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM hourly_calls WHERE symbol=? "
            "ORDER BY target_at DESC LIMIT ?", (symbol, limit)
        ).fetchall()
    return [_row_to_call(r) for r in rows]
