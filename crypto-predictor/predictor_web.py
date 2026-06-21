"""Flask web para crypto-predictor. Una página por símbolo con precio
actual, distribución a 1h, y tabla P(precio > X). Selector de símbolo
arriba; polling en background para los 5 símbolos en paralelo."""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from pathlib import Path

from flask import (Flask, jsonify, render_template_string, request,
                   send_from_directory)

import calibration as _cal
import hourly_call as _hcall
import kalshi as _kalshi
import predictor as _pred

app = Flask(__name__)

# Display timezone para usuario en Puerto Rico (AST = UTC-4, sin DST).
# Sólo afecta render — DB y APIs siguen usando UTC/epoch.
PR_TZ = timezone(timedelta(hours=-4), name="AST")


def _pr(ts) -> datetime:
    """Convierte epoch seconds o datetime a datetime con tzinfo=PR_TZ."""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(PR_TZ)
    return datetime.fromtimestamp(float(ts), tz=PR_TZ)


POLL_SEC = 5  # Binance: 5 syms × 12/min × 2 weight = 120/min (límite 1200)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT"]
DEFAULT_SYMBOL = "BTCUSDT"

# state[symbol] = {pred, ladder, fetched_at, last_err}
_state: dict[str, dict] = {s: {"pred": None, "ladder": None,
                               "fetched_at": None, "last_err": None,
                               "momentum_pct_per_min": None,
                               "momentum_multi": {},
                               "vol_regime": None,
                               "history_strip": None}
                           for s in SYMBOLS}
_state_lock = threading.Lock()

# Señales externas globales (no por símbolo): Fear&Greed y BRTI-proxy BTC mid.
# F&G se actualiza 1×/día (alternative.me), refresco cada 1h.
# BRTI proxy = mediana de mids de los 4 constituyentes CME CF públicos
# (Coinbase, Kraken, Bitstamp, Gemini). Kalshi BTC liquida con CFB BRTI,
# que no expone API gratis. Refresh cada poll BTC.
_external = {"fng": None, "fng_at": 0.0,
             "brti_mid": None, "brti_at": 0.0, "brti_meta": None,
             "ob_imbalance": None, "ob_at": 0.0,
             "taker_flow": None, "taker_at": 0.0,
             "funding": None, "funding_at": 0.0}
# Ring buffers para sparkline: últimas N muestras de OB imbalance y taker
# buy_ratio. Cada poll (~15s) añade una muestra → 80 muestras ≈ 20 min de
# historia visible al lado del pill.
from collections import deque
_external_hist = {"ob_imb": deque(maxlen=80),
                  "taker_br": deque(maxlen=80)}
_external_lock = threading.Lock()
FNG_TTL_SEC = 3600.0
MICRO_TTL_SEC = 15.0   # OB imbalance + taker flow: short TTL para corto plazo
TAKER_WINDOW_MIN = 5   # ventana para agregación de aggTrades
FUNDING_TTL_SEC = 60.0  # premiumIndex se mueve con cada tick de mark


def _fetch_fng() -> dict | None:
    """Crypto Fear & Greed Index (alternative.me, gratis sin auth).

    Devuelve {value: int 0-100, classification: str, ts: epoch} o None."""
    import requests
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=4)
        r.raise_for_status()
        d = r.json().get("data") or []
        if not d:
            return None
        return {
            "value": int(d[0]["value"]),
            "classification": d[0].get("value_classification", ""),
            "ts": int(d[0].get("timestamp", "0")),
        }
    except Exception as e:
        print(f"fng fetch error: {e}", file=sys.stderr)
        return None


def _fetch_brti_proxy() -> dict | None:
    """BTC ≈ CFB BRTI proxy from public constituent venue tickers.

    Uses equal-weight median of fresh venue mids. We intentionally do not
    volume-weight by 24h ticker volume: BRTI weighting is based on eligible
    transactions in the calculation window, while 24h ticker volume is a slow,
    venue-level liquidity proxy that can overweight one venue during a local
    dislocation. 24h volume is kept in meta for audit only.

    Returns {mid, n, spread_bps, sources, stale_warning, divergence_warning,
    venues} or None if fewer than 2 fresh sources respond.
    """
    import concurrent.futures as cf
    import email.utils
    import requests

    STALE_MAX_SECONDS = 2.0
    DIVERGENCE_WARN_BPS = 5.0

    def _parse_ts(value) -> float | None:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                ts = float(value)
            else:
                raw = str(value).strip()
                if raw.replace('.', '', 1).isdigit():
                    ts = float(raw)
                else:
                    try:
                        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        return email.utils.parsedate_to_datetime(raw).timestamp()
            return ts / 1000.0 if ts > 10_000_000_000 else ts
        except Exception:
            return None

    def _cb() -> dict:
        r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=3)
        r.raise_for_status()
        d = r.json()
        return {
            "mid": (float(d["bid"]) + float(d["ask"])) / 2.0,
            "ts": _parse_ts(d.get("time")),
            "volume_24h": float(d.get("volume") or 0.0),
            "timestamp_source": "payload",
        }

    def _kr() -> dict:
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=3)
        r.raise_for_status()
        fetched_at = time.time()
        t = next(iter(r.json()["result"].values()))
        return {
            "mid": (float(t["a"][0]) + float(t["b"][0])) / 2.0,
            "ts": fetched_at,
            "volume_24h": float(t.get("v", [0.0, 0.0])[1] or 0.0),
            "timestamp_source": "response",
        }

    def _bs() -> dict:
        r = requests.get("https://www.bitstamp.net/api/v2/ticker/btcusd/", timeout=3)
        r.raise_for_status()
        d = r.json()
        return {
            "mid": (float(d["bid"]) + float(d["ask"])) / 2.0,
            "ts": _parse_ts(d.get("timestamp")),
            "volume_24h": float(d.get("volume") or 0.0),
            "timestamp_source": "payload",
        }

    def _ge() -> dict:
        r = requests.get("https://api.gemini.com/v1/pubticker/btcusd", timeout=3)
        r.raise_for_status()
        d = r.json()
        vol = d.get("volume") or {}
        return {
            "mid": (float(d["bid"]) + float(d["ask"])) / 2.0,
            "ts": _parse_ts(vol.get("timestamp") or d.get("timestamp")),
            "volume_24h": float(vol.get("BTC") or vol.get("btc") or 0.0),
            "timestamp_source": "payload",
        }

    sources = {"CB": _cb, "KR": _kr, "BS": _bs, "GE": _ge}
    raw: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fn): name for name, fn in sources.items()}
        for fut in cf.as_completed(futs, timeout=4):
            name = futs[fut]
            try:
                v = fut.result()
                if v.get("mid") and v["mid"] > 0 and v.get("ts"):
                    raw[name] = v
            except Exception as e:
                print(f"brti src {name} err: {e}", file=sys.stderr)

    if len(raw) < 2:
        return None

    newest_ts = max(v["ts"] for v in raw.values())
    fresh = {
        name: v for name, v in raw.items()
        if newest_ts - v["ts"] <= STALE_MAX_SECONDS
    }
    stale = sorted(set(raw) - set(fresh))
    unavailable = sorted(set(sources) - set(raw))
    down_count = len(sources) - len(fresh)
    stale_warning_minor = down_count == 1
    stale_warning_critical = down_count >= 2
    if len(fresh) < 2:
        return None

    vals = sorted(v["mid"] for v in fresh.values())
    n = len(vals)
    mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    spread_bps = (vals[-1] - vals[0]) / mid * 10000.0
    return {
        "mid": mid,
        "n": n,
        "spread_bps": spread_bps,
        "sources": sorted(fresh.keys()),
        "stale_sources": stale,
        "unavailable_sources": unavailable,
        "stale_warning_minor": stale_warning_minor,
        "stale_warning_critical": stale_warning_critical,
        "stale_warning": stale_warning_critical,
        "divergence_warning": spread_bps > DIVERGENCE_WARN_BPS,
        "weighting": "equal_median",
        "min_sources": 2,
        "venues": {
            name: {
                "mid": v["mid"],
                "ts": v["ts"],
                "age_vs_newest_sec": newest_ts - v["ts"],
                "volume_24h": v.get("volume_24h"),
                "timestamp_source": v.get("timestamp_source"),
                "fresh": name in fresh,
            }
            for name, v in sorted(raw.items())
        },
    }


def _fetch_ob_imbalance(symbol: str = "BTCUSDT", levels: int = 20) -> dict | None:
    """Order book imbalance en Binance (top N levels).

    Devuelve {bid_vol, ask_vol, imbalance, spread_bps} o None.
    imbalance = bid_vol / (bid_vol + ask_vol), 0.5 = equilibrio.
    """
    import requests
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/depth",
            params={"symbol": symbol, "limit": levels},
            timeout=4,
        )
        r.raise_for_status()
        d = r.json()
        bids = d.get("bids") or []
        asks = d.get("asks") or []
        if not bids or not asks:
            return None
        bid_vol = sum(float(q) for _, q in bids[:levels])
        ask_vol = sum(float(q) for _, q in asks[:levels])
        total = bid_vol + ask_vol
        if total <= 0:
            return None
        imb = bid_vol / total
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 10000
        return {"bid_vol": bid_vol, "ask_vol": ask_vol,
                "imbalance": imb, "spread_bps": spread_bps,
                "levels": levels}
    except Exception as e:
        print(f"ob_imbalance fetch error: {e}", file=sys.stderr)
        return None


def _fetch_taker_flow(symbol: str = "BTCUSDT",
                      window_min: int = TAKER_WINDOW_MIN) -> dict | None:
    """Flujo agresivo (taker buy vs sell) últimos `window_min` minutos.

    Binance aggTrades: cada trade tiene `m` (isBuyerMaker). m=True → vendedor
    agresivo (hit bid); m=False → comprador agresivo (lift ask). Devuelve
    {buy_vol, sell_vol, buy_ratio, n_trades, window_min} o None.
    """
    import requests
    try:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - window_min * 60 * 1000
        r = requests.get(
            "https://api.binance.com/api/v3/aggTrades",
            params={"symbol": symbol, "startTime": start_ms, "endTime": now_ms},
            timeout=5,
        )
        r.raise_for_status()
        trades = r.json()
        if not trades:
            return None
        buy_vol = 0.0
        sell_vol = 0.0
        for t in trades:
            q = float(t["q"])
            if t.get("m"):
                sell_vol += q
            else:
                buy_vol += q
        total = buy_vol + sell_vol
        if total <= 0:
            return None
        return {"buy_vol": buy_vol, "sell_vol": sell_vol,
                "buy_ratio": buy_vol / total,
                "n_trades": len(trades),
                "window_min": window_min}
    except Exception as e:
        print(f"taker_flow fetch error: {e}", file=sys.stderr)
        return None


def _fetch_funding_rate(symbol: str = "BTCUSDT") -> dict | None:
    """Funding rate del perp Binance (fapi público, sin auth).

    `lastFundingRate` es decimal por intervalo de 8h. APR ≈ rate × 3 × 365.
    Positivo: longs pagan a shorts (mercado sesgado long). Negativo: shorts
    pagan a longs (sesgado short). Útil como contexto de sentimiento futures.

    Devuelve {rate, mark, index, next_funding_at} o None.
    """
    import requests
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=4,
        )
        r.raise_for_status()
        d = r.json()
        return {
            "rate": float(d["lastFundingRate"]),
            "mark": float(d["markPrice"]),
            "index": float(d["indexPrice"]),
            "next_funding_at": int(d["nextFundingTime"]) / 1000.0,
        }
    except Exception as e:
        print(f"funding fetch error: {e}", file=sys.stderr)
        return None


def _refresh_external_for_btc(now_unix: float) -> None:
    """Refresca señales externas: F&G TTL 1h, BRTI proxy + OB + flow cada poll."""
    with _external_lock:
        fng_age = now_unix - _external["fng_at"]
        ob_age = now_unix - _external["ob_at"]
        taker_age = now_unix - _external["taker_at"]
        funding_age = now_unix - _external["funding_at"]
    if fng_age >= FNG_TTL_SEC:
        fng = _fetch_fng()
        if fng:
            with _external_lock:
                _external["fng"] = fng
                _external["fng_at"] = now_unix
    brti = _fetch_brti_proxy()
    if brti is not None:
        with _external_lock:
            _external["brti_mid"] = brti["mid"]
            _external["brti_at"] = now_unix
            _external["brti_meta"] = brti
    if ob_age >= MICRO_TTL_SEC:
        ob = _fetch_ob_imbalance()
        if ob:
            with _external_lock:
                _external["ob_imbalance"] = ob
                _external["ob_at"] = now_unix
                _external_hist["ob_imb"].append(ob["imbalance"])
    if taker_age >= MICRO_TTL_SEC:
        flow = _fetch_taker_flow()
        if flow:
            with _external_lock:
                _external["taker_flow"] = flow
                _external["taker_at"] = now_unix
                _external_hist["taker_br"].append(flow["buy_ratio"])
    if funding_age >= FUNDING_TTL_SEC:
        funding = _fetch_funding_rate()
        if funding:
            with _external_lock:
                _external["funding"] = funding
                _external["funding_at"] = now_unix


def do_poll_symbol(symbol: str) -> None:
    try:
        p = _pred.build_prediction(symbol=symbol)
        # Momentum: regresión sobre últimos 10 closes 1m. Re-fetch barato
        # (ya hicimos uno en build_prediction; segundo trae 10 candles).
        try:
            mk = _pred.fetch_klines(symbol=symbol, interval="1m", limit=360)
            momentum = _momentum_pct_per_min(mk, lookback=10)
            momentum_multi = {
                lb: _momentum_pct_per_min(mk, lookback=lb)
                for lb in (5, 10, 30, 60)
            }
            vol_regime = _vol_regime(mk, fast_window=60)
            history_strip = _history_strip(mk)
        except Exception:
            momentum = None
            momentum_multi = {}
            vol_regime = None
            history_strip = None
        ladder = _pred.threshold_ladder_abs(p, n=10)
        kalshi_curve = None
        if symbol == "BTCUSDT":
            try:
                kalshi_curve = [
                    {"threshold": r["threshold"],
                     "kalshi_p": _kalshi.implied_above(p.target_at, r["threshold"])}
                    for r in ladder
                ]
            except Exception:
                kalshi_curve = None
        _cal.record_prediction(p, ladder, kalshi_curve=kalshi_curve)
        if symbol == "BTCUSDT":
            try:
                _hcall.make_call(p)
            except Exception as e:
                print(f"hourly_call error: {e}", file=sys.stderr)
            try:
                _refresh_external_for_btc(time.time())
            except Exception as e:
                print(f"external refresh error: {e}", file=sys.stderr)
        with _state_lock:
            _state[symbol] = {
                "pred": p,
                "ladder": ladder,
                "fetched_at": datetime.now(timezone.utc),
                "last_err": None,
                "momentum_pct_per_min": momentum,
                "momentum_multi": momentum_multi,
                "vol_regime": vol_regime,
                "history_strip": history_strip,
            }
    except Exception as e:
        with _state_lock:
            _state[symbol]["last_err"] = f"{type(e).__name__}: {e}"
        print(f"poll error {symbol}: {e}", file=sys.stderr)


def poll_loop() -> None:
    while True:
        for sym in SYMBOLS:
            do_poll_symbol(sym)
        try:
            n = _cal.settle_due()
            if n:
                print(f"settled {n} predictions", file=sys.stderr)
        except Exception as e:
            print(f"settle error: {e}", file=sys.stderr)
        try:
            nh = _hcall.settle_due()
            if nh:
                print(f"settled {nh} hourly_calls", file=sys.stderr)
        except Exception as e:
            print(f"hourly_call settle error: {e}", file=sys.stderr)
        time.sleep(POLL_SEC)


def _resolve_symbol(raw: str | None) -> str:
    if not raw:
        return DEFAULT_SYMBOL
    s = raw.upper()
    return s if s in SYMBOLS else DEFAULT_SYMBOL


def _momentum_pct_per_min(klines, lookback: int = 10) -> float | None:
    """Slope de log(close) por minuto, regresión lineal sobre últimos `lookback`
    minutos. Devuelve %/min (positivo = alcista). None si no hay datos."""
    import math
    if klines is None or len(klines) < lookback:
        return None
    recent = klines[-lookback:]
    n = len(recent)
    xs = list(range(n))
    ys = [math.log(k.close) for k in recent]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    return (num / den) * 100.0


def _vol_regime(klines, fast_window: int = 60) -> dict | None:
    """Detecta regime-shift de vol.

    Compara la std de log-returns de la ventana actual (últimos `fast_window`
    min) contra la mediana de stds de todas las ventanas deslizantes del mismo
    tamaño que caben en `klines`. Ratio > 2 → vol elevada; < 0.5 → comprimida.
    """
    import math
    import statistics
    if not klines or len(klines) < fast_window * 3:
        return None
    closes = [k.close for k in klines]
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))]
    if len(rets) < fast_window * 2:
        return None
    current = statistics.pstdev(rets[-fast_window:])
    windows = [statistics.pstdev(rets[i - fast_window:i])
               for i in range(fast_window, len(rets))]
    if not windows:
        return None
    baseline = statistics.median(windows)
    if baseline <= 0:
        return None
    return {
        "ratio": current / baseline,
        "current_pct": current * 100,
        "baseline_pct": baseline * 100,
        "lookback_min": len(rets),
    }


def _history_strip(klines, windows=(15, 30, 60, 120, 180)) -> list[dict] | None:
    """Δ% retrospectivo + high/low + percentil del |Δ| actual.

    Para cada ventana N min (≤ len(klines)-1):
      - delta_pct: 100*(close_now / close_{-N} - 1)
      - high_pct, low_pct: % desde el high/low del rango hasta close_now
      - pct_rank: percentil de |delta_pct| actual vs todos los rolling
        N-min returns que caben en `klines` (baseline ≈ 6h con 360 1m candles).

    pct_rank=95 ⇒ el movimiento actual está entre el 5% más extremo del baseline.
    """
    if not klines or len(klines) < max(windows) + 1:
        return None
    closes = [k.close for k in klines]
    highs = [k.high for k in klines]
    lows = [k.low for k in klines]
    now = closes[-1]
    out: list[dict] = []
    for n in windows:
        if len(closes) < n + 1:
            continue
        past = closes[-(n + 1)]
        if past <= 0:
            continue
        delta_pct = (now / past - 1) * 100
        win_high = max(highs[-n:])
        win_low = min(lows[-n:])
        high_pct = (now / win_high - 1) * 100  # ≤ 0; -2% = 2% bajo el high
        low_pct = (now / win_low - 1) * 100    # ≥ 0; +2% = 2% sobre el low
        # Distribución de |return| a N min sobre todo el histórico disponible
        diffs = []
        for i in range(n, len(closes)):
            base = closes[i - n]
            if base > 0:
                diffs.append(abs(closes[i] / base - 1) * 100)
        if not diffs:
            pct_rank = None
        else:
            diffs_sorted = sorted(diffs)
            target = abs(delta_pct)
            # rank: cuántos del baseline son ≤ target
            lo, hi = 0, len(diffs_sorted)
            while lo < hi:
                mid = (lo + hi) // 2
                if diffs_sorted[mid] <= target:
                    lo = mid + 1
                else:
                    hi = mid
            pct_rank = 100.0 * lo / len(diffs_sorted)
        out.append({
            "win_min": n,
            "delta_pct": delta_pct,
            "high_pct": high_pct,
            "low_pct": low_pct,
            "pct_rank": pct_rank,
            "baseline_n": len(diffs) if diffs else 0,
        })
    return out


def _build_strike_heatmap(pred, n_strikes: int = 15,
                           n_hours: int = 8) -> dict | None:
    """Tabla P(close ≥ strike) para los próximos n_hours cierres XX:00.

    Filas = strikes (n_strikes niveles centrados en now_price, espaciados a un
    incremento "redondo" elegido por el rango p05-p95 del horizonte más
    lejano). Columnas = cierres horarios futuros (AST). Sin momentum: pura
    distribución log-Student-t df=4 escalada con √(min al target).

    Devuelve {rows, hours, now_price, step, base_iso} o None si falta data.
    """
    import math
    if pred.sigma_1m <= 0 or pred.now_price <= 0:
        return None
    base_ts = pred.fetched_at
    if pred.target_at <= base_ts:
        return None
    hours = []
    for k in range(n_hours):
        t_unix = pred.target_at + k * 3600
        mins = max(1.0, (t_unix - base_ts) / 60.0)
        sigma_h = pred.sigma_1m * math.sqrt(mins)
        hours.append({
            "k": k,
            "label": _pr(t_unix).strftime("%H:%M"),
            "t_unix": t_unix,
            "mins": mins,
            "sigma_h": sigma_h,
            "sigma_h_pct": sigma_h * 100.0,
        })
    last_sigma = hours[-1]["sigma_h"]
    z_lo = _pred._dist_inv(0.05)
    z_hi = _pred._dist_inv(0.95)
    p_lo = pred.now_price * math.exp(z_lo * last_sigma)
    p_hi = pred.now_price * math.exp(z_hi * last_sigma)
    span = max(1.0, p_hi - p_lo)
    step_raw = span / (n_strikes - 1)
    nice_steps = (0.0001, 0.001, 0.01, 0.05, 0.10, 0.25, 0.50, 1, 2, 5, 10,
                  25, 50, 100, 200, 250, 500, 1000, 2000, 5000)
    step = nice_steps[-1]
    for s in nice_steps:
        if step_raw <= s * 1.5:
            step = s
            break
    center = round(pred.now_price / step) * step
    half = (n_strikes - 1) // 2
    strikes = sorted([center + (i - half) * step for i in range(n_strikes)],
                     reverse=True)
    rows = []
    for s in strikes:
        if s <= 0:
            continue
        cells = []
        for h in hours:
            z = math.log(s / pred.now_price) / h["sigma_h"]
            p_above = 1.0 - _pred._dist_cdf(z)
            cells.append({"p_above": p_above})
        is_near = abs(s - pred.now_price) < step / 2
        rows.append({"strike": s, "cells": cells, "is_near": is_near})
    return {
        "rows": rows,
        "hours": hours,
        "now_price": pred.now_price,
        "step": step,
    }


def _spark_svg(values: list[float], width: int = 60, height: int = 14) -> str:
    """SVG inline sparkline para series acotadas a [0,1]. Centro 0.5 = línea
    de equilibrio (gris). Polyline blanca sobre fondo neutro. Vacío si <3."""
    if not values or len(values) < 3:
        return ""
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        v = max(0.0, min(1.0, v))
        x = i / (n - 1) * (width - 2) + 1
        y = (1 - v) * (height - 2) + 1
        pts.append(f"{x:.1f},{y:.1f}")
    mid_y = height / 2
    return (f'<svg width="{width}" height="{height}" '
            f'style="vertical-align:middle;margin-left:6px" '
            f'viewBox="0 0 {width} {height}">'
            f'<line x1="0" y1="{mid_y}" x2="{width}" y2="{mid_y}" '
            f'stroke="#45475a" stroke-width="0.5" stroke-dasharray="2,2"/>'
            f'<polyline points="{" ".join(pts)}" fill="none" '
            f'stroke="#cdd6f4" stroke-width="1.2"/></svg>')


def _heatmap_cell_style(p: float) -> str:
    """Color de fondo + texto para una celda P(close ≥ strike).

    Divergente: rojo (0%) → gris neutro (~50%) → verde (100%). Texto blanco
    para extremos, gris para zona neutra. Diseño para el tema oscuro del app."""
    p = max(0.0, min(1.0, p))
    if p <= 0.5:
        t = p / 0.5
        r = int(180 + (60 - 180) * t)
        g = int(40 + (65 - 40) * t)
        b = int(40 + (75 - 40) * t)
    else:
        t = (p - 0.5) / 0.5
        r = int(60 + (40 - 60) * t)
        g = int(65 + (165 - 65) * t)
        b = int(75 + (75 - 75) * t)
    bg = f"rgb({r},{g},{b})"
    txt = "#fff" if (p <= 0.20 or p >= 0.80) else "#cdd6f4"
    return f"background:{bg};color:{txt}"


def _build_horizons(pred, momentum_pct_per_min: float | None) -> list[dict]:
    """3 horizontes anclados al próximo :00 en punto (= cierre).

    Cada uno se ubica en cierre+15/+30/+60 min. Los timestamps quedan estables
    durante toda la hora actual (no cambian con cada poll). σ y momentum se
    proyectan usando mins_from_now (= mins_to_cierre + h_min)."""
    import math
    base = datetime.fromtimestamp(pred.fetched_at, tz=timezone.utc)
    anchor = _pr(pred.target_at)
    out = []
    for h_min in (15, 30, 60):
        target_dt = anchor + timedelta(minutes=h_min)
        mins_from_now = (target_dt - base).total_seconds() / 60.0
        sigma_h = pred.sigma_1m * math.sqrt(mins_from_now)

        def Q(q, sh=sigma_h):
            z = _pred._dist_inv(q)
            return pred.now_price * math.exp(z * sh)

        mom_total = (momentum_pct_per_min or 0.0) * mins_from_now
        mom_price = pred.now_price * (1 + mom_total / 100.0)
        # P(close > now_price | drift = momentum extrapolado, std = sigma_h)
        # bajo log-Student-t df=4 (mismo modelo que las bandas).
        if momentum_pct_per_min is None or sigma_h <= 0:
            p_up = None
        else:
            drift_log = math.log(mom_price / pred.now_price)
            z = drift_log / sigma_h
            p_up = _pred._dist_cdf(z)
        out.append({
            "h_min": h_min,                          # offset desde cierre
            "mins_from_now": mins_from_now,          # para SVG y σ
            "target_iso": target_dt.strftime("%H:%M AST"),
            "median": pred.now_price,
            "p10": Q(0.10), "p25": Q(0.25),
            "p75": Q(0.75), "p90": Q(0.90),
            "momentum_pct_per_min": momentum_pct_per_min,
            "mom_total_pct": mom_total,
            "mom_price": mom_price,
            "sigma_h_pct": sigma_h * 100.0,
            "p_up": p_up,
        })
    return out


def _svg_horizon_fan(now_price: float, horizons: list[dict],
                     w: int = 620, h: int = 220) -> str:
    """Fan chart SVG: x = minutos desde ahora hasta el horizonte más lejano
    (cierre+60min), y = precio. Bandas p10-p90 y p25-p75 + mediana horizontal
    + línea momentum (amarilla). Marcadores verticales en cada horizonte."""
    pad_l, pad_r, pad_t, pad_b = 56, 14, 22, 26
    cw = w - pad_l - pad_r
    ch = h - pad_t - pad_b
    prices = [now_price]
    for hr in horizons:
        prices += [hr["p10"], hr["p90"], hr["mom_price"]]
    p_lo, p_hi = min(prices), max(prices)
    span = (p_hi - p_lo) or 1.0
    p_lo -= span * 0.10
    p_hi += span * 0.10

    t_max = max(hr["mins_from_now"] for hr in horizons)

    def x(t_min):
        return pad_l + cw * t_min / t_max

    def y(p):
        return pad_t + ch * (1 - (p - p_lo) / (p_hi - p_lo))

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" '
             f'style="display:block;background:#11111b;border-radius:4px">']
    # Y grid + labels
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        py = pad_t + ch * frac
        price = p_hi - (p_hi - p_lo) * frac
        parts.append(f'<line x1="{pad_l}" y1="{py:.1f}" x2="{pad_l+cw}" '
                     f'y2="{py:.1f}" stroke="#2a2e42" stroke-dasharray="2,3"/>')
        parts.append(f'<text x="{pad_l-4}" y="{py+3:.1f}" font-size="10" '
                     f'fill="#a6adc8" text-anchor="end">${price:,.0f}</text>')

    # Bands as polygons (anchored at t=0 = now_price)
    pts90_hi = [(0, now_price)] + [(hr["mins_from_now"], hr["p90"]) for hr in horizons]
    pts90_lo = [(0, now_price)] + [(hr["mins_from_now"], hr["p10"]) for hr in horizons]
    pts50_hi = [(0, now_price)] + [(hr["mins_from_now"], hr["p75"]) for hr in horizons]
    pts50_lo = [(0, now_price)] + [(hr["mins_from_now"], hr["p25"]) for hr in horizons]

    def poly(top, bot, fill):
        coords = (" ".join(f"{x(t):.1f},{y(p):.1f}" for t, p in top)
                  + " " + " ".join(f"{x(t):.1f},{y(p):.1f}"
                                   for t, p in reversed(bot)))
        return f'<polygon points="{coords}" fill="{fill}" stroke="none"/>'

    parts.append(poly(pts90_hi, pts90_lo, "rgba(137,180,250,0.15)"))
    parts.append(poly(pts50_hi, pts50_lo, "rgba(137,180,250,0.32)"))

    # Median (horizontal dashed)
    ymed = y(now_price)
    parts.append(f'<line x1="{x(0):.1f}" y1="{ymed:.1f}" x2="{x(t_max):.1f}" '
                 f'y2="{ymed:.1f}" stroke="#cdd6f4" stroke-width="1.2" '
                 f'stroke-dasharray="4,3" opacity="0.7"/>')

    # Momentum projection (yellow solid)
    if horizons[0]["momentum_pct_per_min"] is not None:
        mpts = [(0, now_price)] + [(hr["mins_from_now"], hr["mom_price"])
                                   for hr in horizons]
        coords = " ".join(f"{x(t):.1f},{y(p):.1f}" for t, p in mpts)
        parts.append(f'<polyline points="{coords}" stroke="#f9e2af" '
                     f'stroke-width="2" fill="none"/>')
        for t, p in mpts[1:]:
            parts.append(f'<circle cx="{x(t):.1f}" cy="{y(p):.1f}" '
                         f'r="2.5" fill="#f9e2af"/>')

    # Vertical markers en cada horizonte (cierre+15/+30/+60)
    for hr in horizons:
        xv = x(hr["mins_from_now"])
        parts.append(f'<line x1="{xv:.1f}" y1="{pad_t}" x2="{xv:.1f}" '
                     f'y2="{pad_t+ch}" stroke="#89b4fa" stroke-width="0.6" '
                     f'opacity="0.35" stroke-dasharray="2,2"/>')

    # X axis ticks: "ahora" + un label por horizonte con su hora AST (PR)
    ticks = [(0.0, "ahora")] + [(hr["mins_from_now"], hr["target_iso"].replace(" AST", ""))
                                for hr in horizons]
    for t_min, lbl in ticks:
        xt = x(t_min)
        parts.append(f'<line x1="{xt:.1f}" y1="{pad_t+ch}" x2="{xt:.1f}" '
                     f'y2="{pad_t+ch+3}" stroke="#a6adc8"/>')
        parts.append(f'<text x="{xt:.1f}" y="{pad_t+ch+16}" font-size="10" '
                     f'fill="#a6adc8" text-anchor="middle">{lbl}</text>')

    # Legend
    lx = pad_l
    parts.append('<g font-size="10" fill="#a6adc8">')
    parts.append(f'<rect x="{lx}" y="6" width="12" height="6" '
                 f'fill="rgba(137,180,250,0.32)"/>'
                 f'<text x="{lx+16}" y="13">p25-p75</text>')
    parts.append(f'<rect x="{lx+72}" y="6" width="12" height="6" '
                 f'fill="rgba(137,180,250,0.15)"/>'
                 f'<text x="{lx+88}" y="13">p10-p90</text>')
    parts.append(f'<line x1="{lx+150}" y1="9" x2="{lx+165}" y2="9" '
                 f'stroke="#cdd6f4" stroke-width="1.2" stroke-dasharray="4,3"/>'
                 f'<text x="{lx+169}" y="13">mediana</text>')
    parts.append(f'<line x1="{lx+222}" y1="9" x2="{lx+237}" y2="9" '
                 f'stroke="#f9e2af" stroke-width="2"/>'
                 f'<text x="{lx+241}" y="13">momentum</text>')
    parts.append('</g></svg>')
    return "".join(parts)


INDEX_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Crypto Predictor — {{pred.symbol}}</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;
       padding:1rem;max-width:920px;margin:0 auto}
  h1{color:#f9e2af;margin:0 0 .4rem}
  .dim{color:#6c7086;font-size:12px}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.7rem 0}
  .hero{font-size:2.5rem;font-weight:700;color:#a6e3a1;
        font-variant-numeric:tabular-nums}
  .sub{color:#a6adc8;font-size:.95rem;margin-top:.3rem}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:5px 8px;border-bottom:1px solid #313244;text-align:right}
  th{color:#a6adc8;font-weight:normal}
  td.lbl{text-align:left;color:#cdd6f4}
  tr.center td{background:#252535;font-weight:600}
  .err{color:#f38ba8;font-style:italic}
  .quant{display:flex;justify-content:space-between;gap:.6rem;
         font-family:monospace;font-size:14px}
  .quant span{display:block}
  .quant .k{color:#a6adc8;font-size:11px}
  .quant .v{color:#cdd6f4;font-weight:600;font-variant-numeric:tabular-nums}
  .tabs{display:flex;gap:.4rem;flex-wrap:wrap;margin:.5rem 0 1rem}
  .tab{padding:.35rem .7rem;border-radius:4px;text-decoration:none;
       background:#1e1e2e;color:#a6adc8;font-size:13px}
  .tab.active{background:#f9e2af;color:#11111b;font-weight:600}
  form.q{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
  form.q input[type=text]{background:#11111b;color:#cdd6f4;border:1px solid #313244;
       border-radius:4px;padding:.4rem .6rem;font-family:monospace;font-size:14px;width:120px}
  form.q button{background:#89b4fa;color:#11111b;border:0;border-radius:4px;
       padding:.45rem .8rem;font-weight:600;cursor:pointer}
  .qres{margin-top:.6rem;padding:.6rem .7rem;border:1px solid #313244;border-radius:6px;
        background:#181826}
  .qres + .qres{margin-top:.5rem}
  .claim{font-size:.95rem;color:#cdd6f4;margin-bottom:.4rem;font-family:monospace}
  .yesno{display:flex;gap:.5rem;margin:.3rem 0 .35rem}
  .yes,.no,.kx{flex:1;padding:.55rem .7rem;border-radius:5px;text-align:center;
           border:1px solid #313244;display:flex;align-items:center;
           justify-content:center;gap:.45rem}
  .yes{background:#1a3d2e}
  .no{background:#3d1a26}
  .kx{background:#22223a}
  .yes .lbl{color:#a6e3a1}
  .no  .lbl{color:#f38ba8}
  .kx  .lbl{color:#89b4fa}
  .edge{font-size:.7rem;color:#6c7086;font-family:monospace;display:block;margin-top:.1rem}
  .edge.pos{color:#a6e3a1}
  .edge.neg{color:#f38ba8}
  .lbl{display:block;font-size:.65rem;letter-spacing:.1em;font-weight:600;opacity:.85}
  .pct{display:block;font-size:1.55rem;font-weight:700;
       font-variant-numeric:tabular-nums;color:#cdd6f4;line-height:1}
  .arrow{font-size:1.55rem;font-weight:700;color:#6c7086;width:1.1em;
         text-align:center;font-variant-numeric:tabular-nums;
         transition:color .2s}
  .arrow.up{color:#a6e3a1}
  .arrow.dn{color:#f38ba8}
  .qmeta{font-size:11px;color:#6c7086;font-family:monospace;margin-top:.2rem}
  .qmeta .live{color:#a6e3a1}
  .qerr{color:#f38ba8;font-style:italic;margin-top:.4rem}
  .signals{display:flex;flex-wrap:wrap;gap:.4rem;margin:.5rem 0 1rem}
  .pill{display:inline-flex;align-items:center;gap:.35rem;padding:.3rem .65rem;
        border-radius:999px;font-size:.8rem;background:#1e1e2e;border:1px solid #313244}
  .pill .k{color:#6c7086;font-size:.68rem;text-transform:uppercase;letter-spacing:.05em}
  .pill .v{font-weight:600}
  .pill.warn{border-color:rgba(249,226,175,.45)}.pill.warn .v{color:#f9e2af}
  .pill.alert{border-color:rgba(243,139,168,.55);background:rgba(243,139,168,0.08)}
  .pill.alert .v{color:#f38ba8}
  .mtf{margin-top:.8rem;padding-top:.6rem;border-top:1px solid #313244}
  .mtf-label{color:#a6adc8;font-size:11px;text-transform:uppercase;
             letter-spacing:.08em;margin-bottom:.4rem}
  .mtf-row{display:grid;grid-template-columns:repeat(4,1fr);gap:.4rem}
  .mtf-cell{background:#181826;border:1px solid #313244;border-radius:6px;
            padding:.45rem .3rem;text-align:center;font-family:monospace}
  .mtf-tf{font-size:11px;color:#6c7086}
  .mtf-arrow{font-size:1.4rem;font-weight:700;line-height:1.1}
  .mtf-val{font-size:.85rem;font-variant-numeric:tabular-nums}
  .mtf-up{border-color:rgba(166,227,161,.5)}.mtf-up .mtf-arrow,.mtf-up .mtf-val{color:#a6e3a1}
  .mtf-dn{border-color:rgba(243,139,168,.5)}.mtf-dn .mtf-arrow,.mtf-dn .mtf-val{color:#f38ba8}
  .mtf-flat .mtf-arrow,.mtf-flat .mtf-val{color:#a6adc8}
  .hist{display:grid;grid-template-columns:repeat(5,1fr);gap:.4rem;margin-top:.4rem}
  .hist-cell{background:#181826;border:1px solid #313244;border-radius:6px;
             padding:.5rem .35rem;text-align:center;font-family:monospace}
  .hist-tf{font-size:11px;color:#6c7086;letter-spacing:.05em}
  .hist-delta{font-size:1.05rem;font-weight:700;font-variant-numeric:tabular-nums;
              line-height:1.2;margin:.15rem 0 .1rem}
  .hist-delta.up{color:#a6e3a1}.hist-delta.dn{color:#f38ba8}.hist-delta.flat{color:#a6adc8}
  .hist-range{font-size:10px;color:#a6adc8;line-height:1.3}
  .hist-range .h{color:#94e2d5}.hist-range .l{color:#fab387}
  .hist-rank{font-size:10px;margin-top:.25rem;color:#6c7086}
  .hist-rank.warn{color:#f9e2af}.hist-rank.alert{color:#f38ba8;font-weight:700}
  details.diag{background:#1e1e2e;border:1px solid #313244;border-radius:8px;
               padding:.55rem .9rem;margin:.7rem 0}
  details.diag>summary{cursor:pointer;font-weight:600;color:#a6adc8;list-style:none}
  details.diag[open]>summary{color:#cdd6f4;margin-bottom:.4rem}
  details.diag>summary::after{content:" ▸";color:#6c7086}
  details.diag[open]>summary::after{content:" ▾"}
  .tension{background:#1e1e2e;border-radius:8px;padding:.7rem 1rem;margin:.7rem 0;
           display:grid;grid-template-columns:auto 1fr auto;gap:.9rem;align-items:center}
  .tension-arrow{font-size:2rem;font-weight:700;line-height:1;font-family:monospace}
  .tension-meter{position:relative;height:18px;border-radius:9px;
                 background:linear-gradient(to right,#b3372e 0%,#45475a 50%,#28b463 100%);
                 box-shadow:inset 0 0 0 1px #313244}
  .tension-marker{position:absolute;top:-3px;width:4px;height:24px;
                  background:#f9e2af;border-radius:2px;box-shadow:0 0 4px rgba(0,0,0,.6)}
  .tension-meta{font-family:monospace;text-align:right;line-height:1.3}
  .tension-score{font-size:1.4rem;font-weight:700;font-variant-numeric:tabular-nums}
  .tension-dir{font-size:11px;color:#a6adc8;text-transform:uppercase;letter-spacing:.06em}
  .tension-bd{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:.4rem;
              padding-top:.4rem;border-top:1px solid #313244;font-family:monospace;font-size:11px}
  .tension-bd-cell{padding:.15rem .5rem;border-radius:4px;background:#181826;
                   color:#a6adc8;border:1px solid #313244}
  .tension-bd-cell.bull{border-color:rgba(40,180,99,.4);color:#a6e3a1}
  .tension-bd-cell.bear{border-color:rgba(179,55,46,.4);color:#f38ba8}
  .tension-bd-cell .c{margin-left:.4rem;color:#cdd6f4;font-weight:600}
</style></head><body>
<h1>{{pred.symbol}} · cierre {{target_hh}}:00 AST <span class="dim" style="font-size:.6em;font-weight:400">(hora PR)</span></h1>
<div class="tabs">
  {% for s in symbols %}
  <a class="tab {% if s == pred.symbol %}active{% endif %}"
     href="/?symbol={{s}}">{{s.replace('USDT','')}}</a>
  {% endfor %}
  <a class="tab" href="/calibration?symbol={{pred.symbol}}">calibration</a>
  {% if pred.symbol == 'BTCUSDT' %}<a class="tab" href="/hourly-call">hourly-call</a>{% endif %}
  {% if pred.symbol == 'BTCUSDT' %}<a class="tab" href="/intra15">intra-15</a>{% endif %}
  {% if pred.symbol == 'BTCUSDT' %}<a class="tab" href="/tutorial-btc.pdf" target="_blank" style="margin-left:auto;background:#313244;color:#f9e2af">📘 guía BTC</a>
  <a class="tab" href="/tutorial.pdf" target="_blank" style="background:#313244;color:#a6adc8">📄 tutorial técnico</a>{% else %}<a class="tab" href="/tutorial.pdf" target="_blank" style="margin-left:auto;background:#313244;color:#f9e2af">📄 tutorial</a>{% endif %}
</div>
<p class="dim">Modelo: log-Student-t df=4 con drift cero. σ_1m via EWMA λ={{ '%.2f'|format(lam) }}
   sobre {{pred.n_candles}} candles 1m. Refresh 30s. Última obs AST {{fetched_iso}}.<br>
   Horizonte hasta cierre: <b>{{ '%.1f'|format(pred.horizon_min) }} min</b>
   (target {{target_iso}}).</p>

{% if tension %}
<div class="tension">
  {% set arr = '↑' if tension.score >= 1.5 else ('↓' if tension.score <= -1.5 else ('↗' if tension.score >= 0.5 else ('↘' if tension.score <= -0.5 else '→'))) %}
  {% set acol = '#a6e3a1' if tension.score >= 0.5 else ('#f38ba8' if tension.score <= -0.5 else '#a6adc8') %}
  <div class="tension-arrow" style="color:{{ acol }}">{{ arr }}</div>
  <div>
    <div class="tension-meter">
      <div class="tension-marker" style="left:calc({{ '%.1f'|format(tension.pct) }}% - 2px)"></div>
    </div>
    <div class="dim" style="font-size:10px;margin-top:.25rem;display:flex;justify-content:space-between">
      <span>−5 bearish</span><span>0</span><span>+5 bullish</span>
    </div>
  </div>
  <div class="tension-meta">
    <div class="tension-score" style="color:{{ acol }}">{{ '%+.1f'|format(tension.score) }}</div>
    <div class="tension-dir">{{ tension.direction }}</div>
  </div>
  <div class="tension-bd">
    {% for c in tension.components %}
    <span class="tension-bd-cell {% if c.c > 0.05 %}bull{% elif c.c < -0.05 %}bear{% endif %}">
      {{ c.k }} {{ c.v }}<span class="c">{{ '%+.1f'|format(c.c) }}</span>
    </span>
    {% endfor %}
  </div>
</div>
{% endif %}

{% if signals %}
<div class="signals">
  {% for s in signals %}
  <span class="pill {{ s.kls or '' }}">
    <span class="k">{{s.k}}</span><span class="v">{{s.v}}</span>{% if s.spark %}{{ s.spark|safe }}{% endif %}
  </span>
  {% endfor %}
</div>
{% endif %}

{% if err %}<div class="card err">⚠ {{err}}</div>{% endif %}

<div class="card" id="qcard">
  <div class="dim" style="margin-bottom:.4rem">consulta — hasta 3 thresholds, refresh 1s</div>
  <form class="q" method="get" action="/">
    <input type="hidden" name="symbol" value="{{pred.symbol}}">
    <input type="text" name="t1" inputmode="decimal"
           placeholder="t1 (ej. {{ '%.2f'|format(pred.now_price) }})"
           value="{{ inputs.t1 }}">
    <input type="text" name="t2" inputmode="decimal" placeholder="t2 (opc.)"
           value="{{ inputs.t2 }}">
    <input type="text" name="t3" inputmode="decimal" placeholder="t3 (opc.)"
           value="{{ inputs.t3 }}">
    <button type="submit">calcular</button>
  </form>
  {% for q in queries %}
  <div class="qres" data-symbol="{{pred.symbol}}" data-slot="{{q.slot}}"
       data-threshold="{{q.threshold}}">
    <div class="claim">
      <b>{{pred.symbol.replace('USDT','')}}</b> &gt;
      <b>${{ price_fmt(q.threshold) }}</b> al cierre {{target_hh}}:00 AST
    </div>
    <div class="yesno">
      <div class="yes">
        <span class="arrow" data-side="yes">→</span>
        <div>
          <span class="lbl">YES (&gt;)</span>
          <span class="pct" data-fld="pct_yes">{{ '%.1f'|format(q.p_above*100) }}%</span>
        </div>
      </div>
      <div class="no">
        <span class="arrow" data-side="no">→</span>
        <div>
          <span class="lbl">NO (≤)</span>
          <span class="pct" data-fld="pct_no">{{ '%.1f'|format((1-q.p_above)*100) }}%</span>
        </div>
      </div>
      <div class="kx">
        <div>
          <span class="lbl">KALSHI</span>
          <span class="pct" data-fld="kalshi">
            {%- if q.kalshi_p is not none -%}{{ '%.1f'|format(q.kalshi_p*100) }}%{%- else -%}—{%- endif -%}
          </span>
          <span class="edge" data-fld="edge">
            {%- if q.kalshi_p is not none -%}edge {{ '%+.1f'|format((q.p_above-q.kalshi_p)*100) }}pp{%- endif -%}
          </span>
        </div>
      </div>
    </div>
    <div class="qmeta">
      precio: <b data-fld="qm_price">${{ price_fmt(pred.now_price) }}</b>
      · Δ <span data-fld="qm_delta">{{ '%+.2f'|format(q.delta_pct) }}%</span>
      · z <span data-fld="qm_z">{{ '%+.2f'|format(q.z) }}σ</span>
      · σ_h <span data-fld="qm_sigma">{{ '%.2f'|format(pred.sigma_horizon*100) }}%</span>
      · <span class="live" data-fld="qm_age">recién</span>
    </div>
  </div>
  {% endfor %}
</div>

<div class="card">
  {% if brti_mid %}
  <div class="dim">precio actual <span style="background:#f9e2af;color:#11111b;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;margin-left:.3rem">KALSHI-ALIGNED · BRTI proxy</span></div>
  <div class="hero">${{ '{:,.2f}'.format(brti_mid) }}</div>
  <div class="sub">
    Binance (modelo): <b>${{ '{:,.2f}'.format(pred.now_price) }}</b>
    · basis <b>{{ '%+.1f'|format((pred.now_price/brti_mid-1)*10000) }} bps</b>
    · σ horizonte <b>{{ '%.2f'|format(pred.sigma_horizon*100) }}%</b>
    (σ/min {{ '%.3f'|format(pred.sigma_1m*100) }}%)
    {% if brti_meta and brti_meta.divergence_warning %}
    · <span style="color:#fab387;font-weight:600">venue spread {{ '%.1f'|format(brti_meta.spread_bps) }}bps</span>
    {% endif %}
    {% if brti_meta and brti_meta.stale_warning_critical %}
    · <span style="color:#f38ba8;font-weight:600">BRTI proxy degradado</span>
    {% endif %}
  </div>
  {% else %}
  <div class="dim">precio actual</div>
  <div class="hero">${{ '{:,.4f}'.format(pred.now_price) if pred.now_price < 10 else '{:,.2f}'.format(pred.now_price) }}</div>
  <div class="sub">σ horizonte: <b>{{ '%.2f'|format(pred.sigma_horizon*100) }}%</b>
                 (σ por minuto {{ '%.3f'|format(pred.sigma_1m*100) }}%)</div>
  {% endif %}
</div>

<div class="card" id="whatif-card">
  <div class="dim" style="margin-bottom:.4rem">what-if — precio X en hora Y (AST · hora PR)</div>
  <form class="q" id="whatif-form">
    <input type="hidden" name="symbol" value="{{pred.symbol}}">
    <input type="text" name="price" inputmode="decimal" required
           placeholder="precio (ej. {{ '%.0f'|format(pred.now_price) }})"
           style="width:140px">
    <input type="datetime-local" name="target_iso" required style="width:200px">
    <button type="submit">calcular</button>
  </form>
  <div id="whatif-res" style="margin-top:.6rem"></div>
  <p class="dim" style="margin-top:.4rem">
    Misma distribución log-Student-t df=4 que el resto. σ se escala con √(min al
    target). Horizonte máximo: 7 días (más allá el modelo no aplica — no tiene
    drift ni mean-reversion).
  </p>
</div>

<div class="card">
  <div class="dim" style="margin-bottom:.4rem">proyección 15 / 30 / 60 min — bandas del modelo + momentum reciente</div>
  {{ fan_svg | safe }}
  <table style="margin-top:.6rem">
    <tr><th>horizonte</th><th>AST</th><th>p25–p75</th><th>p10–p90</th><th>momentum</th><th>P(sube)</th><th>señal</th></tr>
    {% for hr in horizons %}
    <tr>
      <td class="lbl">cierre +{{hr.h_min}}m</td>
      <td>{{hr.target_iso}}</td>
      <td>${{ '{:,.0f}'.format(hr.p25) }} – ${{ '{:,.0f}'.format(hr.p75) }}</td>
      <td>${{ '{:,.0f}'.format(hr.p10) }} – ${{ '{:,.0f}'.format(hr.p90) }}</td>
      <td>
        {% if hr.momentum_pct_per_min is not none %}
          {% if hr.mom_total_pct > 0 %}<span style="color:#a6e3a1">↑</span>
          {% elif hr.mom_total_pct < 0 %}<span style="color:#f38ba8">↓</span>
          {% else %}→{% endif %}
          {{ '%+.2f'|format(hr.mom_total_pct) }}% → ${{ '{:,.0f}'.format(hr.mom_price) }}
        {% else %}—{% endif %}
      </td>
      <td>
        {% if hr.p_up is not none %}
          {% set pup = (hr.p_up * 100) %}
          {% if pup >= 60 %}<span style="color:#a6e3a1;font-weight:600">↑ {{ '%.0f'|format(pup) }}%</span>
          {% elif pup <= 40 %}<span style="color:#f38ba8;font-weight:600">↓ {{ '%.0f'|format(100-pup) }}%</span>
          {% else %}<span style="color:#a6adc8">~ {{ '%.0f'|format(pup) }}%</span>{% endif %}
        {% else %}—{% endif %}
      </td>
      <td>
        {% if hr.momentum_pct_per_min is not none %}
          {% if hr.mom_price > hr.p90 %}<span style="color:#f38ba8;font-weight:600">↑ rompe p90</span>
          {% elif hr.mom_price < hr.p10 %}<span style="color:#f38ba8;font-weight:600">↓ rompe p10</span>
          {% elif hr.mom_price > hr.p75 %}<span style="color:#f9e2af">↑ sobre p75</span>
          {% elif hr.mom_price < hr.p25 %}<span style="color:#f9e2af">↓ bajo p25</span>
          {% else %}<span style="color:#a6e3a1">dentro p25-p75</span>{% endif %}
        {% else %}—{% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
  <p class="dim" style="margin-top:.5rem">
    Bandas: simétricas alrededor del precio actual (modelo log-Student-t, sin drift).
    Momentum: regresión sobre últimos 10 min, proyectado linealmente.
    <b>Señal</b>: si el momentum cruza p75/p90 (o p25/p10), la tendencia reciente
    excede lo que la vol normal explicaría → sesgo direccional fuerte
    {% if momentum is none %}<br><span class="err">⚠ momentum no disponible</span>{% endif %}
  </p>
  {% if momentum_tf %}
  <div class="mtf">
    <div class="mtf-label">tendencia por ventana</div>
    <div class="mtf-row">
      {% for r in momentum_tf %}
      <div class="mtf-cell mtf-{{r.dir}}">
        <div class="mtf-tf">{{r.lb}}m</div>
        {% if r.pct_per_min is not none %}
          <div class="mtf-arrow">
            {% if r.dir == 'up' %}↑{% elif r.dir == 'dn' %}↓{% else %}→{% endif %}
          </div>
          <div class="mtf-val">{{ '%+.2f'|format(r.total_pct) }}%</div>
        {% else %}
          <div class="mtf-arrow">—</div>
          <div class="mtf-val">—</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    <p class="dim" style="margin:.4rem 0 0">
      Cada celda: pendiente del log-precio durante los últimos N min, expresada
      como % total recorrido. Si 5m/10m apuntan opuesto a 30m/60m → reversión
      reciente (pill arriba). Si los 4 coinciden → consenso fuerte.
    </p>
  </div>
  {% endif %}
</div>

{% if history_strip %}
<div class="card">
  <div class="dim" style="margin-bottom:.2rem">histórico contextualizado — Δ desde X min atrás · high/low del rango · percentil del |Δ| vs últimas 6h</div>
  <div class="hist">
    {% for r in history_strip %}
    {% set d = r.delta_pct %}
    {% set rk = r.pct_rank %}
    <div class="hist-cell">
      <div class="hist-tf">{{ r.win_min }}m atrás</div>
      <div class="hist-delta {% if d > 0.05 %}up{% elif d < -0.05 %}dn{% else %}flat{% endif %}">
        {% if d > 0 %}↑{% elif d < 0 %}↓{% else %}→{% endif %} {{ '%+.2f'|format(d) }}%
      </div>
      <div class="hist-range">
        <span class="h">H {{ '%+.2f'|format(r.high_pct) }}%</span>
        · <span class="l">L {{ '%+.2f'|format(r.low_pct) }}%</span>
      </div>
      {% if rk is not none %}
      <div class="hist-rank {% if rk >= 90 %}alert{% elif rk >= 75 %}warn{% endif %}">
        |Δ| ≈ p{{ '%.0f'|format(rk) }} de 6h
      </div>
      {% else %}
      <div class="hist-rank">—</div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  <p class="dim" style="margin-top:.5rem">
    <b>Δ%</b>: cambio del close desde N min atrás.
    <b>H/L</b>: distancia desde el high/low del rango (H ≤ 0, L ≥ 0).
    <b>p_rank</b>: percentil del |Δ| actual vs todos los rolling N-min returns en las últimas 6h.
    p≥90 (rojo) = movimiento extremo → considera mean-reversion.
    p≤25 = mercado dormido.
  </p>
</div>
{% endif %}

<details class="diag">
  <summary>Gráficos y cuantiles al cierre</summary>

  <div class="card">
    <div class="dim" style="margin-bottom:.4rem">precio · velas 1m últimos 60 min</div>
    <div id="chart_price" style="height:280px"></div>
  </div>

  <div class="card">
    <div class="dim" style="margin-bottom:.4rem">bandas predichas al cierre {{target_hh}}:00</div>
    <div id="chart_bands" style="height:200px"></div>
    <p class="dim" style="margin-top:.4rem">
      p05 / p25 / p50 / p75 / p95: dónde el modelo espera que aterrice el precio al cierre.
      Si el precio actual (línea amarilla) sale del rango p05–p95, sería un evento ~&lt;5% por cola.
    </p>
  </div>

  <div class="card">
    <div class="dim" style="margin-bottom:.4rem">cuantiles del precio al cierre {{target_hh}}:00</div>
    <div class="quant">
      {% for q,v in quantiles %}
      <span><span class="k">p{{q}}</span><span class="v">${{ price_fmt(v) }}</span></span>
      {% endfor %}
    </div>
  </div>
</details>

<div class="card">
  <div class="dim" style="margin-bottom:.4rem">
    P(precio &gt; X) al cierre {{target_hh}}:00 — pasos de
    <b>${{ price_fmt(ladder[0].step_abs) }}</b></div>
  <table>
    <tr><th>threshold</th><th>Δ%</th><th>YES (&gt;X)</th><th>NO (≤X)</th></tr>
    {% for r in ladder %}
    <tr class="{% if r.is_center %}center{% endif %}">
      <td class="lbl">${{ price_fmt(r.threshold) }}</td>
      <td>{{ '%+.2f'|format(r.delta_pct) }}%</td>
      <td>{{ '%.1f'|format(r.p_above*100) }}%</td>
      <td>{{ '%.1f'|format((1-r.p_above)*100) }}%</td>
    </tr>
    {% endfor %}
  </table>
</div>

{% if pred.symbol == 'BTCUSDT' %}
<div class="card">
  <div class="dim" style="margin-bottom:.4rem">
    Kalshi 15-min · strike fijo · próximos 4 cierres XX:00/15/30/45 AST
  </div>
  <form method="get" action="/" style="display:flex;gap:.4rem;align-items:center;margin-bottom:.5rem;font-family:monospace">
    <input type="hidden" name="symbol" value="BTCUSDT">
    <label class="dim" style="font-size:12px">strike $</label>
    <input type="text" name="strike15" inputmode="decimal" placeholder="ej. 77589.97"
           value="{{ '%g'|format(intra15.strike) if intra15 else '' }}"
           style="background:#181826;border:1px solid #313244;color:#cdd6f4;
                  padding:.3rem .5rem;border-radius:4px;font-family:monospace;width:130px">
    <button type="submit" style="background:#f9e2af;color:#11111b;border:0;
            padding:.3rem .7rem;border-radius:4px;font-weight:600;cursor:pointer">set</button>
    {% if intra15 %}
    <span class="dim" style="font-size:11px;margin-left:.5rem" id="intra15-meta">
      spot <span id="i15-spot">${{ price_fmt(intra15.now_price) }}</span> ·
      <span id="i15-side">{{ 'sobre' if intra15.strike >= intra15.now_price else 'bajo' }}</span> spot por
      <span id="i15-diff">${{ '%.2f'|format(intra15.strike - intra15.now_price) }}</span>
    </span>
    {% endif %}
  </form>
  {% if intra15 %}
  {% if intra15.brti_mid %}
  <div class="dim" style="font-size:11px;margin-bottom:.4rem;color:#94e2d5">
    Kalshi liquida con CFB BRTI · proxy = mediana venues frescos (CB/KR/BS/GE) · mid ahora
    <span id="i15-cbmid">${{ price_fmt(intra15.brti_mid) }}</span>
    (basis Binance <span id="i15-basis">{{ '%+.1f'|format(intra15.basis_bps) }}bps</span>)
    {% if intra15.divergence_warning %}
    · <span style="color:#fab387">venue spread alto</span>
    {% endif %}
    {% if intra15.stale_warning_critical %}
    · <span style="color:#f38ba8">proxy degradado</span>
    {% endif %}
    · strike Kalshi-equivalente en Binance:
    <span style="color:#f9e2af" id="i15-strikeadj">${{ '%.2f'|format(intra15.strike_adj_binance) }}</span>
  </div>
  {% endif %}
  <table style="width:100%;border-collapse:collapse;font-family:monospace;font-size:13px" id="intra15-table">
    <thead><tr style="color:#a6adc8">
      <th style="text-align:left;padding:4px 8px">cierre AST</th>
      <th style="text-align:right;padding:4px 8px">min</th>
      <th style="text-align:right;padding:4px 8px">σ horizonte</th>
      <th style="text-align:right;padding:4px 8px">P(≥ strike)</th>
      <th style="text-align:right;padding:4px 8px">P(< strike)</th>
    </tr></thead>
    <tbody>
      {% for r in intra15.rows %}
      <tr style="border-top:1px solid #313244" data-row="{{ loop.index0 }}">
        <td class="i15-label" style="padding:4px 8px;color:#f9e2af">{{ r.label }}</td>
        <td class="i15-mins" style="text-align:right;padding:4px 8px;color:#a6adc8">{{ '%.1f'|format(r.mins) }}</td>
        <td class="i15-sig" style="text-align:right;padding:4px 8px;color:#a6adc8">{{ '%.2f'|format(r.sigma_pct) }}%</td>
        <td class="i15-pa" style="text-align:right;padding:4px 8px;color:{{ '#a6e3a1' if r.p_above >= 0.6 else ('#f38ba8' if r.p_above <= 0.4 else '#cdd6f4') }};font-weight:600">{{ '%.1f'|format(r.p_above*100) }}%</td>
        <td class="i15-pb" style="text-align:right;padding:4px 8px;color:{{ '#a6e3a1' if r.p_below >= 0.6 else ('#f38ba8' if r.p_below <= 0.4 else '#cdd6f4') }};font-weight:600">{{ '%.1f'|format(r.p_below*100) }}%</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <p class="dim" style="font-size:11px;margin-top:.4rem">
    Si Kalshi vende YES (≥strike) por menos que P(≥strike), edge buy YES = nuestra P − precio Kalshi.
    Recuerda: P aquí no incluye micro signals — usa el semáforo arriba como filtro.
    Auto-refresh cada 8s · <span id="i15-age" style="color:#6c7086">—</span>
  </p>
  <script>
  (function(){
    const strike = {{ intra15.strike }};
    const tbody = document.querySelector('#intra15-table tbody');
    if (!tbody) return;
    let lastFetched = null;
    function colorPct(p) {
      if (p >= 0.60) return '#a6e3a1';
      if (p <= 0.40) return '#f38ba8';
      return '#cdd6f4';
    }
    function poll() {
      fetch('/api/intra15?strike=' + strike)
        .then(r => r.ok ? r.json() : null)
        .then(d => {
          if (!d || !d.rows) return;
          // Solo actualizar si llegó data nueva
          if (d.fetched_at === lastFetched) return;
          lastFetched = d.fetched_at;
          // Actualizar spot/diff
          const spot = document.getElementById('i15-spot');
          const side = document.getElementById('i15-side');
          const diff = document.getElementById('i15-diff');
          if (spot) spot.textContent = '$' + d.now_price.toFixed(2);
          if (side) side.textContent = strike >= d.now_price ? 'sobre' : 'bajo';
          if (diff) diff.textContent = '$' + Math.abs(strike - d.now_price).toFixed(2);
          // BRTI proxy basis
          if (d.brti_mid) {
            const cb = document.getElementById('i15-cbmid');
            const bs = document.getElementById('i15-basis');
            const sa = document.getElementById('i15-strikeadj');
            if (cb) cb.textContent = '$' + d.brti_mid.toFixed(2);
            if (bs) bs.textContent = (d.basis_bps >= 0 ? '+' : '') + d.basis_bps.toFixed(1) + 'bps';
            if (sa) sa.textContent = '$' + d.strike_adj_binance.toFixed(2);
          }
          // Filas
          const rows = tbody.querySelectorAll('tr[data-row]');
          d.rows.forEach((r, i) => {
            const tr = rows[i];
            if (!tr) return;
            tr.querySelector('.i15-label').textContent = r.label;
            tr.querySelector('.i15-mins').textContent = r.mins.toFixed(1);
            tr.querySelector('.i15-sig').textContent = r.sigma_pct.toFixed(2) + '%';
            const pa = tr.querySelector('.i15-pa');
            const pb = tr.querySelector('.i15-pb');
            pa.textContent = (r.p_above * 100).toFixed(1) + '%';
            pa.style.color = colorPct(r.p_above);
            pb.textContent = (r.p_below * 100).toFixed(1) + '%';
            pb.style.color = colorPct(r.p_below);
          });
          const age = document.getElementById('i15-age');
          if (age) age.textContent = 'actualizado ' + new Date().toLocaleTimeString('es-PR',{hour12:false});
        })
        .catch(() => {});
    }
    poll();
    setInterval(poll, 8000);
  })();
  </script>
  {% endif %}
</div>
{% endif %}

{% if heatmap %}
<div class="card">
  <div class="dim" style="margin-bottom:.4rem">
    heatmap P(close ≥ strike) próximos {{ heatmap.hours|length }} cierres ·
    paso ${{ '%g'|format(heatmap.step) }} ·
    rojo = improbable, verde = probable
  </div>
  <table style="width:100%;border-collapse:collapse;font-family:monospace;
                font-size:12px;table-layout:fixed">
    <thead>
      <tr>
        <th style="text-align:right;padding:3px 6px;color:#a6adc8;width:90px">strike</th>
        {% for h in heatmap.hours %}
        <th style="text-align:center;padding:3px 4px;color:#a6adc8">
          {{ h.label }}<br>
          <span style="font-size:10px;color:#6c7086">σ {{ '%.2f'|format(h.sigma_h_pct) }}%</span>
        </th>
        {% endfor %}
      </tr>
    </thead>
    <tbody>
      {% for row in heatmap.rows %}
      <tr>
        <td style="text-align:right;padding:3px 6px;
                   {% if row.is_near %}color:#f9e2af;font-weight:600
                   {% else %}color:#cdd6f4{% endif %}">
          ${{ price_fmt(row.strike) }}
          {% if row.is_near %} ←{% endif %}
        </td>
        {% for c in row.cells %}
        <td style="text-align:center;padding:4px 0;{{ heatmap_cell_style(c.p_above) }}">
          {{ '%.0f'|format(c.p_above * 100) }}
        </td>
        {% endfor %}
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <p class="dim" style="margin-top:.5rem;font-size:11px">
    Cada celda = P(precio ≥ strike) al cierre de esa hora (AST). Sin momentum
    (pura distribución log-Student-t df=4). σ crece con √(min al target);
    las horas lejanas son más inciertas. Strike más cercano al spot marcado ←.
  </p>
</div>
{% endif %}

<p class="dim">Punto óptimo al cierre ≈ precio actual (martingale).
   Edge real: las bandas. Settlement con open del candle 1m en {{target_hh}}:00.
   <a href="/calibration?symbol={{pred.symbol}}" style="color:#89b4fa">→ calibration</a>
   {% if pred.symbol == 'BTCUSDT' %}
   · <a href="/hourly-call" style="color:#89b4fa">→ hourly-call</a>
   {% endif %}
</p>

<script>
// Live poll: hasta 3 .qres slots, refresh 1s, flecha sólo cambia cuando
// llega data nueva (fetched_at distinto al backend).
(function(){
  const cards = document.querySelectorAll('.qres');
  if (!cards.length) return;

  function fmtPrice(v) {
    if (v < 1) return '$' + v.toFixed(5);
    if (v < 10) return '$' + v.toFixed(4);
    if (v < 1000) return '$' + v.toFixed(2);
    return '$' + Math.round(v).toLocaleString();
  }

  // Construye URL con todos los thresholds del símbolo (un solo fetch los sirve todos)
  const sym = cards[0].dataset.symbol;
  const params = new URLSearchParams({symbol: sym});
  cards.forEach(c => params.set('t' + c.dataset.slot, c.dataset.threshold));
  const url = '/api/query?' + params.toString();

  // Estado por slot: última prob vista y último fetched_at del backend
  const state = {};
  cards.forEach(c => {
    state[c.dataset.slot] = {lastProb: null, lastFetched: null,
                             lastClient: Date.now()};
  });

  function setArrow(card, dir) {
    card.querySelectorAll('.arrow').forEach(a => {
      a.classList.remove('up', 'dn');
      if (dir === 'up') { a.classList.add('up'); a.textContent = '↑'; }
      else if (dir === 'dn') { a.classList.add('dn'); a.textContent = '↓'; }
      else { a.textContent = '→'; }
    });
  }

  function fld(card, name) {
    return card.querySelector(`[data-fld="${name}"]`);
  }

  function tickAges() {
    const now = Date.now();
    cards.forEach(c => {
      const sec = Math.floor((now - state[c.dataset.slot].lastClient) / 1000);
      const el = fld(c, 'qm_age');
      if (el) el.textContent = sec < 3 ? 'live' : `hace ${sec}s`;
    });
  }
  setInterval(tickAges, 1000);

  async function poll() {
    try {
      const r = await fetch(url);
      if (!r.ok) return;
      const d = await r.json();
      const bySlot = {};
      (d.queries || []).forEach(q => bySlot[q.slot] = q);

      cards.forEach(card => {
        const slot = card.dataset.slot;
        const q = bySlot[slot];
        if (!q) return;
        const st = state[slot];
        // Sólo movemos flecha cuando el backend trae data nueva
        if (st.lastFetched !== d.fetched_at) {
          if (st.lastProb !== null) {
            const diff = q.p_above - st.lastProb;
            if (Math.abs(diff) > 1e-5) setArrow(card, diff > 0 ? 'up' : 'dn');
            else setArrow(card, 'flat');
          }
          st.lastProb = q.p_above;
          st.lastFetched = d.fetched_at;
          st.lastClient = Date.now();
        }
        fld(card, 'pct_yes').textContent = (q.p_above * 100).toFixed(1) + '%';
        fld(card, 'pct_no' ).textContent = (q.p_below * 100).toFixed(1) + '%';
        const kxEl = fld(card, 'kalshi');
        const edgeEl = fld(card, 'edge');
        if (kxEl) {
          if (q.kalshi_p !== null && q.kalshi_p !== undefined) {
            kxEl.textContent = (q.kalshi_p * 100).toFixed(1) + '%';
            const edge = (q.p_above - q.kalshi_p) * 100;
            edgeEl.textContent = 'edge ' + (edge >= 0 ? '+' : '') + edge.toFixed(1) + 'pp';
            edgeEl.classList.remove('pos','neg');
            if (edge > 1) edgeEl.classList.add('pos');
            else if (edge < -1) edgeEl.classList.add('neg');
          } else {
            kxEl.textContent = '—';
            edgeEl.textContent = '';
          }
        }
        fld(card, 'qm_price').textContent = fmtPrice(d.now_price);
        fld(card, 'qm_delta').textContent =
          (q.delta_pct >= 0 ? '+' : '') + q.delta_pct.toFixed(2) + '%';
        fld(card, 'qm_z').textContent =
          (q.z >= 0 ? '+' : '') + q.z.toFixed(2) + 'σ';
        fld(card, 'qm_sigma').textContent = d.sigma_h_pct.toFixed(2) + '%';
      });
      tickAges();
    } catch(e) { console.error(e); }
  }
  setInterval(poll, 1000);
  poll();
})();

// what-if: precio X en hora Y → P(>X) y dirección.
(function(){
  const form = document.getElementById('whatif-form');
  const res = document.getElementById('whatif-res');
  if (!form) return;
  // Default target = próxima hora en punto, en hora PR (AST = UTC-4).
  // El input datetime-local es naive — backend interpreta naive como AST.
  const pad = n => String(n).padStart(2,'0');
  const PR_OFFSET_MIN = -4 * 60;  // AST = UTC-4, sin DST
  const nowUtc = new Date();
  // shift: UTC + offset = AST wall clock
  const prNow = new Date(nowUtc.getTime() + PR_OFFSET_MIN * 60 * 1000);
  const def = new Date(prNow.getTime() + 3600 * 1000);
  def.setUTCMinutes(0, 0, 0);
  form.target_iso.value = `${def.getUTCFullYear()}-${pad(def.getUTCMonth()+1)}-${pad(def.getUTCDate())}T${pad(def.getUTCHours())}:${pad(def.getUTCMinutes())}`;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const params = new URLSearchParams(new FormData(form));
    res.innerHTML = '<span class="dim">calculando...</span>';
    try {
      const r = await fetch('/api/whatif?' + params.toString());
      const d = await r.json();
      if (!r.ok) {
        res.innerHTML = `<span class="qerr">⚠ ${d.error || 'error'}</span>`;
        return;
      }
      const pup = (d.p_above*100);
      const pdn = (d.p_below*100);
      const pupColor = pup >= 60 ? '#a6e3a1' : (pup <= 40 ? '#f38ba8' : '#a6adc8');
      const dirArrow = d.trend.startsWith('↑') ? '↑' : (d.trend.startsWith('↓') ? '↓' : '→');
      const dirColor = d.trend.startsWith('↑') ? '#a6e3a1' : (d.trend.startsWith('↓') ? '#f38ba8' : '#a6adc8');
      const fmtPrice = v => v < 10 ? v.toFixed(4) : v.toLocaleString('en-US', {maximumFractionDigits: 2});
      const hours = (d.mins_to_target / 60).toFixed(1);
      res.innerHTML = `
        <div class="qres" style="display:block">
          <div class="claim">
            <b>${d.symbol.replace('USDT','')}</b> &gt; <b>$${fmtPrice(d.price)}</b>
            en <b>${d.target_iso}</b> (${hours}h / ${d.mins_to_target.toFixed(0)}min)
          </div>
          <div class="yesno">
            <div class="yes"><span class="lbl">P(SUBE / &gt;X)</span>
              <span class="pct" style="color:${pupColor}">${pup.toFixed(1)}%</span></div>
            <div class="no"><span class="lbl">P(BAJA / &lt;X)</span>
              <span class="pct">${pdn.toFixed(1)}%</span></div>
          </div>
          <div class="qmeta">
            tendencia: <span style="color:${dirColor};font-weight:600">${dirArrow} ${d.trend}</span>
            · σ_h ${d.sigma_h_pct.toFixed(2)}%
            · Δ ${d.delta_pct >= 0 ? '+' : ''}${d.delta_pct.toFixed(2)}% vs $${fmtPrice(d.now_price)}
          </div>
        </div>`;
    } catch (e) {
      res.innerHTML = `<span class="qerr">⚠ ${e.message}</span>`;
    }
  });
})();
</script>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const BANDS = [
  {price: {{ '%.8f'|format(quantiles[0][1]) }}, color: '#f38ba8', label: 'p05'},
  {price: {{ '%.8f'|format(quantiles[1][1]) }}, color: '#fab387', label: 'p25'},
  {price: {{pred.now_price}},                    color: '#f9e2af', label: 'p50 (now)'},
  {price: {{ '%.8f'|format(quantiles[3][1]) }}, color: '#94e2d5', label: 'p75'},
  {price: {{ '%.8f'|format(quantiles[4][1]) }}, color: '#89b4fa', label: 'p95'},
];
const COMMON = {
  layout: {background: {color: '#1e1e2e'}, textColor: '#a6adc8'},
  grid: {vertLines:{color:'#313244'}, horzLines:{color:'#313244'}},
  rightPriceScale: {borderColor: '#313244'},
  timeScale: {borderColor: '#313244', timeVisible: true, secondsVisible: false},
  crosshair: {mode: 0},
};

(async function(){
  if (!window.LightweightCharts) return;

  // Chart 1: solo velas
  const elP = document.getElementById('chart_price');
  const chartP = LightweightCharts.createChart(elP, {...COMMON, height: 280});
  const candleSeries = chartP.addCandlestickSeries({
    upColor:'#a6e3a1', downColor:'#f38ba8',
    borderUpColor:'#a6e3a1', borderDownColor:'#f38ba8',
    wickUpColor:'#a6e3a1', wickDownColor:'#f38ba8',
  });
  try {
    const r = await fetch('/candles?symbol={{pred.symbol}}&limit=60');
    const d = await r.json();
    if (d.candles) {
      candleSeries.setData(d.candles);
      chartP.timeScale().fitContent();
      // Chart 2: línea horizontal del precio actual + bandas como líneas
      const elB = document.getElementById('chart_bands');
      const chartB = LightweightCharts.createChart(elB, {...COMMON, height: 200});
      // Línea del precio actual a lo largo del tiempo (recta horizontal)
      const lineSeries = chartB.addLineSeries({
        color: '#cdd6f4', lineWidth: 2, priceLineVisible: false,
      });
      const t0 = d.candles[0].time, t1 = d.candles[d.candles.length-1].time;
      const nowPrice = {{pred.now_price}};
      lineSeries.setData([
        {time: t0, value: nowPrice},
        {time: t1, value: nowPrice},
      ]);
      for (const b of BANDS) {
        lineSeries.createPriceLine({
          price: b.price, color: b.color, lineWidth: 1,
          lineStyle: 2, axisLabelVisible: true, title: b.label,
        });
      }
      chartB.timeScale().fitContent();
      const resizeCharts = () => {
        if (elP.clientWidth) chartP.applyOptions({width: elP.clientWidth});
        if (elB.clientWidth) chartB.applyOptions({width: elB.clientWidth});
      };
      window.addEventListener('resize', resizeCharts);
      // resize when collapsed <details> is opened (width was 0 inside)
      document.querySelectorAll('details.diag').forEach(d => {
        d.addEventListener('toggle', () => { if (d.open) setTimeout(resizeCharts, 50); });
      });
    }
  } catch(e) { console.error(e); }
})();
</script>
</body></html>"""


def _price_fmt(v: float) -> str:
    if v < 1:
        return f"{v:,.5f}"
    if v < 10:
        return f"{v:,.4f}"
    if v < 1000:
        return f"{v:,.2f}"
    return f"{v:,.0f}"


def _parse_threshold(raw: str) -> float:
    return float(raw.replace(",", "").replace("$", "").strip())


@app.route("/api/query")
def api_query():
    """JSON endpoint — soporta hasta 3 thresholds (t1/t2/t3 o threshold)."""
    import math
    sym = _resolve_symbol(request.args.get("symbol"))
    with _state_lock:
        snap = dict(_state[sym])
    if snap["pred"] is None:
        return jsonify({"error": "sin datos aún"}), 503
    p = snap["pred"]
    queries = []
    for slot, key in enumerate(("t1", "t2", "t3"), start=1):
        raw = request.args.get(key) or (request.args.get("threshold")
                                        if slot == 1 else "")
        if not raw:
            continue
        try:
            thr = _parse_threshold(raw)
            if thr <= 0:
                continue
        except ValueError:
            continue
        p_above = _pred.prob_above(p, thr)
        kalshi_p = None
        if sym == "BTCUSDT":
            try:
                kalshi_p = _kalshi.implied_above(p.target_at, thr)
            except Exception:
                kalshi_p = None
        queries.append({
            "slot": slot,
            "threshold": thr,
            "p_above": p_above,
            "p_below": 1.0 - p_above,
            "delta_pct": (thr / p.now_price - 1) * 100,
            "z": (math.log(thr / p.now_price) / p.sigma_horizon
                  if p.sigma_horizon > 0 else 0.0),
            "kalshi_p": kalshi_p,
            "edge_pct": (p_above - kalshi_p) * 100 if kalshi_p is not None else None,
        })
    return jsonify({
        "symbol": sym,
        "now_price": p.now_price,
        "sigma_h_pct": p.sigma_horizon * 100,
        "fetched_at": snap["fetched_at"].isoformat(),
        "target_at": p.target_at,
        "horizon_min": p.horizon_min,
        "queries": queries,
    })


@app.route("/api/whatif")
def api_whatif():
    """¿Qué probabilidad le da el modelo a precio X en hora Y?

    Params: symbol, price (float), target_iso (AST/hora PR, e.g. 2026-05-13T14:00).
    Si el iso trae tz explícito (Z o +00:00), se respeta. Si es naive, se asume AST.
    Devuelve P(>price), P(<price), σ_horizonte y dirección del momentum.
    El horizonte = mins entre ahora y target_iso (mín 1, máx 7 días).
    """
    import math
    sym = _resolve_symbol(request.args.get("symbol"))
    with _state_lock:
        snap = dict(_state[sym])
    if snap["pred"] is None:
        return jsonify({"error": "sin datos aún"}), 503
    raw_price = request.args.get("price", "").strip()
    raw_target = request.args.get("target_iso", "").strip()
    if not raw_price or not raw_target:
        return jsonify({"error": "price y target_iso requeridos"}), 400
    try:
        price = _parse_threshold(raw_price)
        if price <= 0:
            raise ValueError
    except ValueError:
        return jsonify({"error": "price inválido"}), 400
    try:
        if raw_target.endswith("Z"):
            raw_target = raw_target[:-1]
        target_dt = datetime.fromisoformat(raw_target)
        if target_dt.tzinfo is None:
            # Naive desde el form HTML = hora PR (AST)
            target_dt = target_dt.replace(tzinfo=PR_TZ)
        target_dt = target_dt.astimezone(timezone.utc)
    except ValueError:
        return jsonify({"error": "target_iso inválido (usa YYYY-MM-DDTHH:MM)"}), 400
    p = snap["pred"]
    now_unix = snap["fetched_at"].timestamp()
    mins_to_target = (target_dt.timestamp() - now_unix) / 60.0
    if mins_to_target < 1:
        return jsonify({"error": "target debe estar al menos 1 min en el futuro"}), 400
    if mins_to_target > 7 * 24 * 60:
        return jsonify({"error": "target máximo 7 días"}), 400
    sigma_h = p.sigma_1m * math.sqrt(mins_to_target)
    z = math.log(price / p.now_price) / sigma_h
    p_above = 1.0 - _pred._dist_cdf(z)
    delta_pct = (price / p.now_price - 1) * 100
    # Trend: usar momentum multi-tf
    mtf = _build_momentum_tf(snap.get("momentum_multi") or {})
    cons = _momentum_consensus(mtf)
    if cons:
        trend = cons["v"]
        trend_kls = cons["kls"]
    else:
        m10 = next((r for r in mtf if r["lb"] == 10), None)
        if m10 and m10["dir"] in ("up", "dn"):
            trend = ("↑ corto plazo" if m10["dir"] == "up" else "↓ corto plazo")
            trend_kls = "warn"
        else:
            trend = "lateral"
            trend_kls = None
    return jsonify({
        "symbol": sym,
        "now_price": p.now_price,
        "price": price,
        "target_iso": _pr(target_dt).strftime("%Y-%m-%d %H:%M AST"),
        "mins_to_target": mins_to_target,
        "sigma_h_pct": sigma_h * 100,
        "delta_pct": delta_pct,
        "p_above": p_above,
        "p_below": 1.0 - p_above,
        "trend": trend,
        "trend_kls": trend_kls,
    })


@app.route("/api/intra15")
def api_intra15():
    """JSON con P(close ≥ strike) en próximos 4 cierres de 15-min para BTC.

    Param: `strike` (float). Devuelve también `now_price`, `brti_mid`,
    `basis_bps` y `strike_adj_binance` para el ajuste Kalshi-aligned.
    """
    with _state_lock:
        snap = dict(_state["BTCUSDT"])
    if snap["pred"] is None:
        return jsonify({"error": "sin datos aún"}), 503
    try:
        strike = float((request.args.get("strike") or "").replace(",", ""))
        if strike <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "strike inválido"}), 400
    with _external_lock:
        cb_mid = _external.get("brti_mid")
    data = _build_intra15(snap["pred"], strike, brti_mid=cb_mid,
                          brti_meta=_external.get("brti_meta"))
    if data is None:
        return jsonify({"error": "modelo sin datos suficientes"}), 503
    data["fetched_at"] = snap["fetched_at"].isoformat() if snap["fetched_at"] else None
    return jsonify(data)


@app.route("/api/quarter-signal")
def api_quarter_signal():
    """JSON minimal para el quarter-streak tracker del dashboard.

    Devuelve precio actual + score de tensión [-5, +5] + P(close ≥ now)
    en el próximo cierre de 15 min. El dashboard usa el signo de `tension_score`
    para predecir UP/DOWN cada xx:00/15/30/45.
    """
    with _state_lock:
        snap = dict(_state["BTCUSDT"])
    pred = snap.get("pred")
    if pred is None:
        return jsonify({"error": "sin datos aún"}), 503
    with _external_lock:
        external = dict(_external)
    momentum = snap.get("momentum_pct_per_min")
    momentum_multi = snap.get("momentum_multi") or {}
    momentum_tf = _build_momentum_tf(momentum_multi)
    horizons = _build_horizons(pred, momentum)
    tension = _compute_tension(pred, external, horizons, momentum_tf)
    intra15 = _build_intra15(pred, pred.now_price, n_cierres=1)
    p_above_next = None
    next_close_label = None
    if intra15 and intra15.get("rows"):
        p_above_next = intra15["rows"][0]["p_above"]
        next_close_label = intra15["rows"][0]["label"]
    return jsonify({
        "price": pred.now_price,
        "tension_score": tension["score"] if tension else None,
        "tension_direction": tension["direction"] if tension else None,
        "p_above_next": p_above_next,
        "next_close_label": next_close_label,
        "fetched_at": snap["fetched_at"].isoformat() if snap["fetched_at"] else None,
    })


@app.route("/candles")
def candles():
    sym = _resolve_symbol(request.args.get("symbol"))
    try:
        limit = max(10, min(int(request.args.get("limit", "60")), 500))
    except ValueError:
        limit = 60
    try:
        klines = _pred.fetch_klines(symbol=sym, interval="1m", limit=limit)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({
        "symbol": sym,
        "candles": [
            {"time": k.open_time // 1000,
             "open": k.open, "high": k.high,
             "low": k.low, "close": k.close}
            for k in klines
        ],
    })


@app.route("/")
def index():
    import math
    sym = _resolve_symbol(request.args.get("symbol"))
    with _state_lock:
        snap = dict(_state[sym])
    if snap["pred"] is None:
        return (f"Cargando primer fetch para {sym}... refresca en 5s", 503)
    p = snap["pred"]
    quantiles = [
        ("05", _pred.quantile(p, 0.05)),
        ("25", _pred.quantile(p, 0.25)),
        ("50", p.now_price),
        ("75", _pred.quantile(p, 0.75)),
        ("95", _pred.quantile(p, 0.95)),
    ]
    inputs = {}
    queries = []
    for slot, key in enumerate(("t1", "t2", "t3"), start=1):
        raw = request.args.get(key, "").strip()
        inputs[key] = raw
        if not raw:
            continue
        try:
            thr = _parse_threshold(raw)
            if thr <= 0:
                continue
        except ValueError:
            continue
        p_above = _pred.prob_above(p, thr)
        kalshi_p = None
        if sym == "BTCUSDT":
            try:
                kalshi_p = _kalshi.implied_above(p.target_at, thr)
            except Exception:
                pass
        queries.append({
            "slot": slot,
            "threshold": thr,
            "p_above": p_above,
            "delta_pct": (thr / p.now_price - 1) * 100,
            "z": (math.log(thr / p.now_price) / p.sigma_horizon
                  if p.sigma_horizon > 0 else 0.0),
            "kalshi_p": kalshi_p,
        })
    target_dt = _pr(p.target_at)
    momentum = snap.get("momentum_pct_per_min")
    momentum_multi = snap.get("momentum_multi") or {}
    momentum_tf = _build_momentum_tf(momentum_multi)
    vol_regime = snap.get("vol_regime")
    history_strip = snap.get("history_strip")
    horizons = _build_horizons(p, momentum)
    fan_svg = _svg_horizon_fan(p.now_price, horizons)
    heatmap = _build_strike_heatmap(p)
    with _external_lock:
        external = dict(_external)
    signals = _build_signals(p, queries, horizons, momentum, momentum_tf,
                             vol_regime, external=external)
    tension = _compute_tension(p, external, horizons, momentum_tf)
    try:
        strike15 = float(request.args.get("strike15", "").replace(",", "")) \
            if request.args.get("strike15") else None
    except (ValueError, TypeError):
        strike15 = None
    intra15 = _build_intra15(
        p, strike15,
        brti_mid=external.get("brti_mid"),
        brti_meta=external.get("brti_meta")) if p.symbol == "BTCUSDT" else None
    brti_mid = external.get("brti_mid") if p.symbol == "BTCUSDT" else None
    brti_meta = external.get("brti_meta") if p.symbol == "BTCUSDT" else None
    return render_template_string(
        INDEX_TMPL,
        pred=p,
        brti_mid=brti_mid,
        brti_meta=brti_meta,
        ladder=snap["ladder"],
        fetched_iso=_pr(snap["fetched_at"]).strftime("%Y-%m-%d %H:%M:%S"),
        err=snap["last_err"],
        lam=_pred.EWMA_LAMBDA,
        quantiles=quantiles,
        symbols=SYMBOLS,
        price_fmt=_price_fmt,
        inputs=inputs,
        queries=queries,
        target_hh=target_dt.strftime("%H"),
        target_iso=target_dt.strftime("%Y-%m-%d %H:%M AST"),
        horizons=horizons,
        fan_svg=fan_svg,
        momentum=momentum,
        momentum_tf=momentum_tf,
        vol_regime=vol_regime,
        history_strip=history_strip,
        signals=signals,
        tension=tension,
        intra15=intra15,
        heatmap=heatmap,
        heatmap_cell_style=_heatmap_cell_style,
    )


def _build_momentum_tf(momentum_multi: dict) -> list[dict]:
    """Lista de {lb, pct_per_min, total_pct, dir} para los 4 timeframes.

    `total_pct` = slope * lookback (= cuánto se ha movido el log-precio durante
    la ventana, en %). `dir` ∈ {'up','dn','flat'}."""
    rows: list[dict] = []
    for lb in (5, 10, 30, 60):
        v = momentum_multi.get(lb)
        if v is None:
            rows.append({"lb": lb, "pct_per_min": None, "total_pct": None,
                         "dir": "na"})
            continue
        total = v * lb
        if abs(total) < 0.05:
            d = "flat"
        elif total > 0:
            d = "up"
        else:
            d = "dn"
        rows.append({"lb": lb, "pct_per_min": v, "total_pct": total, "dir": d})
    return rows


def _momentum_consensus(momentum_tf: list[dict]) -> dict | None:
    """Lectura conjunta de los 4 timeframes.

    - 'consenso ↑/↓' si los 4 apuntan al mismo lado (no flat/na).
    - 'reversión ↑/↓ reciente' si 5m y 10m apuntan opuesto a 30m y 60m.
    - None en cualquier otro caso (mixto/no concluyente)."""
    dirs = {r["lb"]: r["dir"] for r in momentum_tf}
    if any(dirs.get(lb) in (None, "na") for lb in (5, 10, 30, 60)):
        return None
    fast = {dirs[5], dirs[10]}
    slow = {dirs[30], dirs[60]}
    all_dirs = [dirs[5], dirs[10], dirs[30], dirs[60]]
    if all(d == "up" for d in all_dirs):
        return {"k": "consenso", "v": "↑ todos los TF", "kls": "warn"}
    if all(d == "dn" for d in all_dirs):
        return {"k": "consenso", "v": "↓ todos los TF", "kls": "warn"}
    if fast == {"up"} and slow == {"dn"}:
        return {"k": "reversión", "v": "↑ reciente vs ↓ largo", "kls": "alert"}
    if fast == {"dn"} and slow == {"up"}:
        return {"k": "reversión", "v": "↓ reciente vs ↑ largo", "kls": "alert"}
    return None


def _build_signals(pred, queries, horizons, momentum,
                   momentum_tf: list[dict] | None = None,
                   vol_regime: dict | None = None,
                   external: dict | None = None) -> list[dict]:
    """Pills compactos para lectura rápida arriba de la home."""
    out: list[dict] = []
    for q in queries:
        if q.get("kalshi_p") is None:
            continue
        edge_pp = (q["p_above"] - q["kalshi_p"]) * 100
        if abs(edge_pp) < 5:
            continue
        side = "YES" if edge_pp > 0 else "NO"
        out.append({"k": f"edge t{q['slot']}",
                    "v": f"{edge_pp:+.1f}pp · buy {side}",
                    "kls": "alert" if abs(edge_pp) >= 10 else "warn"})
    h60 = next((h for h in horizons if h["h_min"] == 60), None)
    if h60 and momentum is not None:
        mp, p10, p25, p75, p90 = (h60["mom_price"], h60["p10"], h60["p25"],
                                  h60["p75"], h60["p90"])
        if mp > p90:
            out.append({"k": "+60min", "v": "↑ rompe p90", "kls": "alert"})
        elif mp < p10:
            out.append({"k": "+60min", "v": "↓ rompe p10", "kls": "alert"})
        elif mp > p75:
            out.append({"k": "+60min", "v": "↑ sobre p75", "kls": "warn"})
        elif mp < p25:
            out.append({"k": "+60min", "v": "↓ bajo p25", "kls": "warn"})
    sigma_h_pct = pred.sigma_horizon * 100
    if sigma_h_pct >= 0.8:
        out.append({"k": "vol", "v": f"alta σ_h {sigma_h_pct:.2f}%", "kls": "warn"})
    if momentum_tf:
        cons = _momentum_consensus(momentum_tf)
        if cons:
            out.append(cons)
    if vol_regime:
        r = vol_regime["ratio"]
        if r >= 2.0:
            out.append({"k": "regime vol",
                        "v": f"elevada {r:.1f}× baseline 6h",
                        "kls": "alert"})
        elif r <= 0.5:
            out.append({"k": "regime vol",
                        "v": f"comprimida {r:.1f}× baseline 6h",
                        "kls": "warn"})
    # Señales externas (sólo BTC). Siempre visibles; color según extremo.
    if external and pred.symbol == "BTCUSDT":
        fng = external.get("fng")
        if fng:
            v = fng["value"]
            cls = fng.get("classification", "")
            if v <= 20:
                kls, suffix = "alert", " · contrarian buy?"
            elif v >= 80:
                kls, suffix = "alert", " · contrarian sell?"
            elif v <= 35 or v >= 65:
                kls, suffix = "warn", ""
            else:
                kls, suffix = None, ""
            out.append({"k": "F&G", "v": f"{v} {cls}{suffix}", "kls": kls})
        cb_mid = external.get("brti_mid")
        if cb_mid and pred.now_price > 0:
            diff_bps = (pred.now_price / cb_mid - 1) * 10000
            arrow = "↑" if diff_bps > 0 else ("↓" if diff_bps < 0 else "→")
            ad = abs(diff_bps)
            if ad >= 10:
                kls, suffix = "alert", " · arb signal"
            elif ad >= 5:
                kls, suffix = "warn", ""
            else:
                kls, suffix = None, ""
            out.append({"k": "vs BRTI",
                        "v": f"Binance {arrow}{ad:.1f}bps{suffix}",
                        "kls": kls})
        ob = external.get("ob_imbalance")
        if ob:
            imb = ob["imbalance"]
            arrow = "↑" if imb > 0.5 else ("↓" if imb < 0.5 else "→")
            bias_pp = (imb - 0.5) * 200  # convierte a "pp por encima de 50/50"
            ab = abs(bias_pp)
            if ab >= 20:
                kls, suffix = "alert", f" · presión {'bid' if imb > 0.5 else 'ask'} fuerte"
            elif ab >= 10:
                kls, suffix = "warn", ""
            else:
                kls, suffix = None, ""
            with _external_lock:
                ob_hist = list(_external_hist["ob_imb"])
            spark = _spark_svg(ob_hist)
            out.append({"k": f"OB top{ob['levels']}",
                        "v": f"{imb*100:.0f}/{(1-imb)*100:.0f} {arrow}{suffix}",
                        "kls": kls, "spark": spark})
        flow = external.get("taker_flow")
        if flow:
            br = flow["buy_ratio"]
            arrow = "↑" if br > 0.5 else ("↓" if br < 0.5 else "→")
            bias_pp = (br - 0.5) * 200
            ab = abs(bias_pp)
            if ab >= 20:
                kls, suffix = "alert", f" · flujo {'comprador' if br > 0.5 else 'vendedor'} dominante"
            elif ab >= 10:
                kls, suffix = "warn", ""
            else:
                kls, suffix = None, ""
            with _external_lock:
                fl_hist = list(_external_hist["taker_br"])
            spark = _spark_svg(fl_hist)
            out.append({"k": f"flow {flow['window_min']}m",
                        "v": f"{br*100:.0f}/{(1-br)*100:.0f} {arrow}{suffix}",
                        "kls": kls, "spark": spark})
        funding = external.get("funding")
        if funding:
            rate = funding["rate"]
            rate_bps = rate * 10000  # bps por 8h
            apr = rate * 3 * 365 * 100  # % APR
            arrow = "↑" if rate > 0 else ("↓" if rate < 0 else "→")
            ar = abs(rate_bps)
            # Baseline neutral BTC: ~1bps/8h (~11% APR). >5bps = caro, >10 = extremo.
            if ar >= 10:
                side = "longs" if rate > 0 else "shorts"
                kls, suffix = "alert", f" · {side} pagando caro"
            elif ar >= 5:
                kls, suffix = "warn", ""
            elif rate < -2 / 10000:
                kls, suffix = "warn", " · sesgo short"
            else:
                kls, suffix = None, ""
            out.append({"k": "funding 8h",
                        "v": f"{arrow}{rate_bps:+.1f}bps (~{apr:+.0f}% APR){suffix}",
                        "kls": kls})
    return out


def _build_intra15(pred, strike: float | None,
                   n_cierres: int = 4,
                   brti_mid: float | None = None,
                   brti_meta: dict | None = None) -> dict | None:
    """Tabla P(close ≥ strike) en los próximos `n_cierres` cierres de 15 min
    (XX:00/15/30/45). Para mercados Kalshi 15-min BTC. Sin momentum; pura
    distribución log-Student-t escalada con √(min al target).
    """
    import math
    if strike is None or strike <= 0:
        return None
    if pred.sigma_1m <= 0 or pred.now_price <= 0:
        return None
    base_ts = pred.fetched_at
    base_dt = datetime.fromtimestamp(base_ts, tz=timezone.utc)
    next_min = ((base_dt.minute // 15) + 1) * 15
    add_h, m = divmod(next_min, 60)
    first_dt = base_dt.replace(minute=0, second=0, microsecond=0)
    first_dt = first_dt + timedelta(hours=add_h, minutes=m)
    first_unix = first_dt.timestamp()
    rows = []
    for k in range(n_cierres):
        t_unix = first_unix + k * 15 * 60
        mins = max(0.5, (t_unix - base_ts) / 60.0)
        sigma_h = pred.sigma_1m * math.sqrt(mins)
        z = math.log(strike / pred.now_price) / sigma_h
        p_above = 1.0 - _pred._dist_cdf(z)
        rows.append({
            "label": _pr(t_unix).strftime("%H:%M"),
            "mins": mins,
            "sigma_pct": sigma_h * 100.0,
            "p_above": p_above,
            "p_below": 1.0 - p_above,
        })
    out = {
        "strike": strike,
        "now_price": pred.now_price,
        "rows": rows,
    }
    if brti_mid and brti_mid > 0:
        # Kalshi BTC liquida con CFB BRTI (proxy aquí). Si Binance trade premium, nuestro
        # strike vale "menos" en Binance-equivalent terms.
        basis_bps = (pred.now_price / brti_mid - 1) * 10000
        strike_adj = strike * brti_mid / pred.now_price
        out["brti_mid"] = brti_mid
        out["basis_bps"] = basis_bps
        out["strike_adj_binance"] = strike_adj
        if brti_meta:
            out["brti_spread_bps"] = brti_meta.get("spread_bps")
            out["stale_warning_minor"] = bool(brti_meta.get("stale_warning_minor"))
            out["stale_warning_critical"] = bool(brti_meta.get("stale_warning_critical"))
            out["stale_warning"] = bool(brti_meta.get("stale_warning_critical"))
            out["divergence_warning"] = bool(brti_meta.get("divergence_warning"))
    return out


def _compute_tension(pred, external, horizons, momentum_tf) -> dict | None:
    """Agrega 6 señales en un score direccional [-5, +5]. Solo BTC.

    Cada componente normalizado a [-X, +X]; suma da el score global. Útil
    para leer balance bullish/bearish sin parsear cada pill por separado."""
    if pred.symbol != "BTCUSDT" or not external:
        return None
    components: list[dict] = []
    score = 0.0

    ob = external.get("ob_imbalance")
    if ob:
        c = (ob["imbalance"] - 0.5) * 2.0  # [-1, +1]
        components.append({"k": "OB top20", "c": c,
                           "v": f"{ob['imbalance']*100:.0f}/{(1-ob['imbalance'])*100:.0f}"})
        score += c

    flow = external.get("taker_flow")
    if flow:
        c = (flow["buy_ratio"] - 0.5) * 2.0  # [-1, +1]
        components.append({"k": f"flow {flow['window_min']}m", "c": c,
                           "v": f"{flow['buy_ratio']*100:.0f}/{(1-flow['buy_ratio'])*100:.0f}"})
        score += c

    funding = external.get("funding")
    if funding:
        rate_bps = funding["rate"] * 10000
        c = max(-0.5, min(0.5, rate_bps / 20.0))  # 10bps → ±0.5
        components.append({"k": "funding", "c": c,
                           "v": f"{rate_bps:+.1f}bps/8h"})
        score += c

    fng = external.get("fng")
    if fng:
        v = fng["value"]
        if v <= 25:
            c = 0.5 + (25 - v) / 50.0  # [+0.5, +1] contrarian buy
        elif v >= 75:
            c = -0.5 - (v - 75) / 50.0  # [-1, -0.5] contrarian sell
        else:
            c = 0.0
        components.append({"k": "F&G", "c": c, "v": f"{v}"})
        score += c

    cb_mid = external.get("brti_mid")
    if cb_mid and pred.now_price > 0:
        bps = (pred.now_price / cb_mid - 1) * 10000
        c = max(-0.5, min(0.5, bps / 20.0))  # 10bps → ±0.5
        components.append({"k": "vs BRTI", "c": c, "v": f"{bps:+.1f}bps"})
        score += c

    h60 = next((h for h in horizons if h["h_min"] == 60), None)
    if h60 and h60.get("mom_price"):
        mp = h60["mom_price"]
        p10, p25, p75, p90 = h60["p10"], h60["p25"], h60["p75"], h60["p90"]
        if mp > p90: c = 1.5
        elif mp > p75: c = 0.7
        elif mp < p10: c = -1.5
        elif mp < p25: c = -0.7
        else: c = 0.0
        if c != 0:
            arrow = "↑" if c > 0 else "↓"
            components.append({"k": "+60min", "c": c, "v": f"{arrow}{abs(c):.1f}"})
            score += c

    score = max(-5.0, min(5.0, score))
    if score >= 1.5: direction = "bullish"
    elif score <= -1.5: direction = "bearish"
    elif score >= 0.5: direction = "lean bull"
    elif score <= -0.5: direction = "lean bear"
    else: direction = "neutral"
    pct = (score + 5.0) / 10.0 * 100.0  # 0% = -5, 50% = 0, 100% = +5
    return {"score": score, "direction": direction, "pct": pct,
            "components": components}


CALIB_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Calibration — crypto-predictor</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;
       padding:1rem;max-width:920px;margin:0 auto}
  h1{color:#f9e2af;margin:0 0 .4rem}
  .dim{color:#6c7086;font-size:12px}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.7rem 0}
  .hero{font-size:2rem;font-weight:700;color:#a6e3a1;
        font-variant-numeric:tabular-nums}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:5px 8px;border-bottom:1px solid #313244;text-align:right}
  th{color:#a6adc8;font-weight:normal}
  td.lbl{text-align:left;color:#cdd6f4}
  .gap{color:#f38ba8}.ok{color:#a6e3a1}
  a{color:#89b4fa}
  .tabs{display:flex;gap:.4rem;flex-wrap:wrap;margin:.5rem 0 1rem}
  .tab{padding:.35rem .7rem;border-radius:4px;text-decoration:none;
       background:#1e1e2e;color:#a6adc8;font-size:13px}
  .tab.active{background:#f9e2af;color:#11111b;font-weight:600}
</style></head><body>
<h1>Calibration · {{symbol or 'todos'}}</h1>
<div class="tabs">
  <a class="tab {% if not symbol %}active{% endif %}" href="/calibration">todos</a>
  {% for s in symbols %}
  <a class="tab {% if s == symbol %}active{% endif %}"
     href="/calibration?symbol={{s}}">{{s.replace('USDT','')}}</a>
  {% endfor %}
</div>
<p class="dim">Cada predicción persistida; outcome 1h después.
   Buen modelo: P predicha ≈ frecuencia observada en cada bucket.
   Brier global: 0 perfect, 0.25 random.</p>
<p><a href="/?symbol={{symbol or 'BTCUSDT'}}">← back</a></p>

<div class="card">
  <div class="dim">Brier global</div>
  <div class="hero">{{ '%.4f'|format(brier) if brier is not none else '—' }}</div>
  <div class="dim">{{n_settled}} predicciones settleadas</div>
</div>

<div class="card">
  <div class="dim" style="margin-bottom:.4rem">últimas {{recent|length}} horas cerradas — pred TEMPRANA (lead alto) vs actual</div>
  {% if recent %}
  <table>
    <tr><th>cierre AST</th><th>sym</th><th>lead</th><th>pred</th><th>real</th>
        <th>Δ%</th><th>σ%</th><th>|z|</th><th>P(≥real)</th></tr>
    {% for h in recent %}
    <tr>
      <td class="lbl"><a href="/history?symbol={{h.symbol}}&target={{h.target_at|int}}"
                       style="color:#89b4fa">{{h.target_iso}}</a></td>
      <td class="lbl">{{h.symbol.replace('USDT','')}}</td>
      <td>{{ '%.0f'|format(h.lead_min) }}m</td>
      <td>${{ price_fmt(h.pred_price) }}</td>
      <td>${{ price_fmt(h.actual_price) }}</td>
      <td>{{ '%+.2f'|format(h.diff_pct) }}%</td>
      <td>{{ '%.2f'|format(h.sigma_h_pct) }}%</td>
      <td class="{% if h.z_actual > 2 %}gap{% elif h.z_actual < 1 %}ok{% endif %}">
        {{ '%.1f'|format(h.z_actual) }}σ</td>
      <td>{{ '%.0f'|format(h.p_above_actual*100) }}%</td>
    </tr>
    {% endfor %}
  </table>
  <p class="dim" style="margin-top:.4rem">
    pred = precio en el fetch más temprano de cada hora (lead = min antes del cierre).
    |z| = |Δ%|/σ%. Si el modelo está bien calibrado, |z| promedio ≈ 0.8 (E|Z| para normal).
    |z|&gt;2 = sorpresa (rojo). P(≥real) ideal: distribución uniforme entre 0–100% en muchas horas.</p>
  {% else %}
  <div class="dim">Sin cierres aún. El primero llega después del próximo XX:00 AST.</div>
  {% endif %}
</div>

{% if bt and bt.n_bets > 0 %}
<div class="card">
  <div class="dim" style="margin-bottom:.4rem">auto-bet backtest (BTC, |edge|≥{{ '%.0f'|format(bt.min_edge_pp) }}pp, 1 contrato/señal)</div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:.5rem;margin-bottom:.6rem">
    <div><div class="dim">apuestas</div><div class="hero" style="font-size:1.4rem">{{bt.n_bets}}</div></div>
    <div><div class="dim">win rate</div>
      <div class="hero" style="font-size:1.4rem;color:{% if bt.win_rate >= 0.55 %}#a6e3a1{% elif bt.win_rate >= 0.45 %}#f9e2af{% else %}#f38ba8{% endif %}">{{ '%.0f'|format(bt.win_rate*100) }}%</div>
    </div>
    <div><div class="dim">PnL neto</div>
      <div class="hero" style="font-size:1.4rem;color:{% if bt.gross_pnl > 0 %}#a6e3a1{% else %}#f38ba8{% endif %}">${{ '%+.2f'|format(bt.gross_pnl) }}</div>
    </div>
    <div><div class="dim">ROI</div>
      <div class="hero" style="font-size:1.4rem;color:{% if bt.roi > 0 %}#a6e3a1{% else %}#f38ba8{% endif %}">{{ '%+.1f'|format(bt.roi*100) }}%</div>
    </div>
  </div>
  <p class="dim">
    Capital total apostado: ${{ '%.2f'|format(bt.cost_sum) }} (1 contrato Kalshi paga $1 si gana).
    PnL ya está en dólares. Replica el comportamiento del pill <code>edge</code>: cada
    vez que aparecía con |edge|≥5pp en el histórico, se simula que comprabas YES/NO.
  </p>
  {% if bt.rows %}
  <details style="margin-top:.5rem">
    <summary class="dim" style="cursor:pointer">últimas {{bt.rows|length}} apuestas</summary>
    <table style="margin-top:.4rem">
      <tr><th>cierre AST</th><th>strike</th><th>side</th><th>edge</th>
          <th>cost</th><th>pnl</th><th>res</th></tr>
      {% for r in bt.rows %}
      <tr>
        <td class="lbl">{{r.iso}}</td>
        <td>${{ '{:,.0f}'.format(r.threshold) }}</td>
        <td>{{r.side}}</td>
        <td class="{% if r.edge_pp >= 0 %}ok{% else %}gap{% endif %}">{{ '%+.1f'|format(r.edge_pp) }}pp</td>
        <td>${{ '%.2f'|format(r.cost) }}</td>
        <td class="{% if r.pnl >= 0 %}ok{% else %}gap{% endif %}">${{ '%+.2f'|format(r.pnl) }}</td>
        <td>{% if r.won %}<span class="ok">✓</span>{% else %}<span class="gap">✗</span>{% endif %}</td>
      </tr>
      {% endfor %}
    </table>
  </details>
  {% endif %}
</div>
{% endif %}

{% if kx and kx.n_total > 0 %}
<div class="card">
  <div class="dim" style="margin-bottom:.4rem">vs Kalshi (sólo BTC) — {{kx.n_total}} cierres con quote</div>
  <table>
    <tr><th>métrica</th><th>modelo</th><th>Kalshi</th></tr>
    <tr><td class="lbl">Brier (per-threshold)</td>
        <td>{{ '%.4f'|format(kx.brier_model) if kx.brier_model is not none else '—' }}</td>
        <td>{{ '%.4f'|format(kx.brier_kalshi) if kx.brier_kalshi is not none else '—' }}</td></tr>
    <tr><td class="lbl">cuando difieren &gt;{{ '%.0f'|format(kx.div_threshold_pp) }}pp</td>
        <td class="{% if kx.div_model_wins > kx.div_kalshi_wins %}ok{% endif %}">gana {{kx.div_model_wins}}×</td>
        <td class="{% if kx.div_kalshi_wins > kx.div_model_wins %}ok{% endif %}">gana {{kx.div_kalshi_wins}}×</td></tr>
  </table>
  <p class="dim" style="margin-top:.4rem">
    Alineación media: <b>{{ '%.2f'|format(kx.mean_abs_align_pp) }}pp</b>
    de diferencia (|model−kalshi|) sobre {{kx.n_pairs}} pares (threshold,cierre).
    {{kx.n_diverge}} pares divergen &gt;{{ '%.0f'|format(kx.div_threshold_pp) }}pp; ahí
    quien tiene Brier menor "gana" ese par.</p>
</div>
{% endif %}

<div class="card">
  <div class="dim" style="margin-bottom:.4rem">colas — frecuencia observada de |z|>k vs esperada (T₄)</div>
  <div class="dim" style="margin-bottom:.5rem">
    n={{tail.n_total}} cierres · mean |z|={{ '%.2f'|format(tail.mean_abs_z) }}
    (esperado ≈0.80 si modelo correcto)
  </div>
  <table>
    <tr><th>nivel</th><th>observado</th><th>esperado</th><th>ratio</th></tr>
    {% for r in tail.levels %}
    <tr>
      <td class="lbl">|z| &gt; {{r.k}}</td>
      <td>{{ '%.2f'|format(r.observed*100) }}% ({{r.n_above}})</td>
      <td>{{ '%.2f'|format(r.expected*100) }}%</td>
      <td class="{% if r.observed > r.expected*1.5 %}gap{% elif r.observed < r.expected*1.2 %}ok{% endif %}">
        {{ '%.1f'|format(r.observed/r.expected) if r.expected > 0 else '—' }}×
      </td>
    </tr>
    {% endfor %}
  </table>
  <p class="dim" style="margin-top:.5rem">
    Ratio &gt;1.5× = el modelo subestima esa cola. Si todos los niveles están altos,
    σ_horizon es demasiado pequeña (subir EWMA λ, o bajar df de la t).
  </p>
</div>

<div class="card">
  <div class="dim" style="margin-bottom:.4rem">top {{shocks|length}} shocks — cierres con mayor sorpresa</div>
  {% if shocks %}
  <table>
    <tr><th>cierre AST</th><th>sym</th><th>pred</th><th>real</th>
        <th>Δ%</th><th>σ%</th><th>|z|</th></tr>
    {% for h in shocks %}
    <tr>
      <td class="lbl"><a href="/history?symbol={{h.symbol}}&target={{h.target_at|int}}"
                       style="color:#89b4fa">{{h.iso}}</a></td>
      <td class="lbl">{{h.symbol.replace('USDT','')}}</td>
      <td>${{ price_fmt(h.pred_price) }}</td>
      <td>${{ price_fmt(h.actual_price) }}</td>
      <td>{{ '%+.2f'|format(h.diff_pct) }}%</td>
      <td>{{ '%.2f'|format(h.sigma_h_pct) }}%</td>
      <td class="gap">{{ '%.1f'|format(h.z_actual) }}σ</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="dim">Sin shocks |z|&gt;2 todavía.</div>
  {% endif %}
</div>

<div class="card">
  <div class="dim" style="margin-bottom:.4rem">reliability por bucket</div>
  <table>
    <tr><th>bucket</th><th>n</th><th>P̄ predicha</th><th>frec real</th>
        <th>gap</th><th>brier</th></tr>
    {% for s in stats %}
    <tr>
      <td class="lbl">{{s.bucket}}</td>
      <td>{{s.n}}</td>
      <td>{{ '%.3f'|format(s.avg_predicted) if s.n else '—' }}</td>
      <td>{{ '%.3f'|format(s.avg_actual) if s.n else '—' }}</td>
      <td class="{% if s.n and (s.avg_predicted-s.avg_actual)|abs > 0.1 %}gap{% else %}ok{% endif %}">
        {{ '%+.3f'|format(s.avg_predicted - s.avg_actual) if s.n else '—' }}
      </td>
      <td>{{ '%.4f'|format(s.brier) if s.n else '—' }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
</body></html>"""


HOURLY_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>BTC hourly call</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;
       padding:1rem;max-width:920px;margin:0 auto}
  h1{color:#f9e2af;margin:0 0 .4rem}
  .dim{color:#6c7086;font-size:12px}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.7rem 0}
  .hero{font-size:3rem;font-weight:700;color:#f9e2af;
        font-variant-numeric:tabular-nums;line-height:1.05}
  .sub{color:#a6adc8;font-size:.95rem;margin-top:.3rem}
  .row{display:flex;gap:.6rem;flex-wrap:wrap}
  .row .card{flex:1;min-width:200px;margin:0}
  .big{font-size:2rem;font-weight:700;font-variant-numeric:tabular-nums;
       color:#a6e3a1;line-height:1}
  .big.warn{color:#f38ba8}
  .lbl{font-size:.7rem;color:#6c7086;letter-spacing:.08em;text-transform:uppercase}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:5px 8px;border-bottom:1px solid #313244;text-align:right}
  th{color:#a6adc8;font-weight:normal}
  td.tlbl{text-align:left;color:#cdd6f4}
  .win{color:#a6e3a1}.loss{color:#f38ba8}
  a{color:#89b4fa}
  .edge.pos{color:#a6e3a1}
  .edge.neg{color:#f38ba8}
  .rec{display:inline-block;padding:.4rem .8rem;border-radius:6px;
       font-weight:700;font-size:1.1rem;margin-top:.5rem}
  .rec.rec-fire{background:#a6e3a1;color:#11111b}
  .rec.rec-ok{background:#94e2d5;color:#11111b}
  .rec.rec-warn{background:#f9e2af;color:#11111b}
  .rec.rec-bad{background:#f38ba8;color:#11111b}
  .rec.dim{background:#313244;color:#6c7086}
  .rec-rat{display:block;font-size:.75rem;color:#a6adc8;font-weight:normal;
           margin-top:.3rem;font-family:monospace}
</style></head><body>
<h1>BTC · hourly point-call (p{{ '%.0f'|format(quantile*100) }})</h1>
<p class="dim">A cada hora en punto el sistema publica un valor decimal tal
   que BTC tiene ~{{ '%.0f'|format(quantile*100) }}% prob. de NO sobrepasarlo
   al cierre de la próxima hora. Win si el precio real ≤ valor.<br>
   <a href="/?symbol=BTCUSDT">← back</a> · <a href="/intra15">→ intra-15</a></p>

{% if current %}
<div class="card">
  <div class="lbl">próximo cierre · {{target_iso}} ({{ '%.0f'|format(mins_left) }} min)</div>
  <div class="hero">${{ '{:,.2f}'.format(current.call_value) }}</div>
  <div class="sub">precio al hacer la call: <b>${{ '{:,.2f}'.format(current.now_price) }}</b>
    · σ_h <b>{{ '%.2f'|format(current.sigma_h*100) }}%</b>
    · spread implícito <b>+{{ '%.2f'|format((current.call_value/current.now_price-1)*100) }}%</b></div>
</div>
{% else %}
<div class="card dim">Aún no hay call activa. La próxima se registra al cruzar XX:00 AST.</div>
{% endif %}

<div class="row">
  <div class="card">
    <div class="lbl">racha actual</div>
    <div class="big">{{streak}} <span style="font-size:1rem;color:#6c7086">hits</span></div>
    <div class="sub">consecutivos sin sobrepasar</div>
  </div>
  <div class="card">
    <div class="lbl">tasa empírica</div>
    {% if rate.rate is not none %}
    <div class="big {% if rate.rate < quantile - 0.1 %}warn{% endif %}">
      {{ '%.0f'|format(rate.rate*100) }}%</div>
    <div class="sub">{{rate.wins}}/{{rate.n}} settled · objetivo {{ '%.0f'|format(quantile*100) }}%</div>
    {% else %}
    <div class="big">—</div>
    <div class="sub">sin settled todavía</div>
    {% endif %}
  </div>
</div>

{% if current and current.kalshi_strike is not none %}
<div class="card">
  <div class="lbl">edge vs Kalshi · strike más cercano</div>
  <table>
    <tr><th>strike</th><th>Kalshi NO</th><th>nuestra NO</th><th>edge</th></tr>
    <tr>
      <td class="tlbl">${{ '{:,.0f}'.format(current.kalshi_strike) }}</td>
      <td>{{ '%.1f'|format(current.kalshi_no_at_strike*100) }}%</td>
      <td>{{ '%.1f'|format(current.model_no_at_strike*100) }}%</td>
      <td class="edge {% if current.edge_pp > 0 %}pos{% elif current.edge_pp < 0 %}neg{% endif %}">
        {{ '%+.1f'|format(current.edge_pp) }}pp</td>
    </tr>
  </table>
  {% if current.kalshi_no_at_call is not none %}
  <div class="dim" style="margin-top:.4rem">
    En nuestro número exacto (${{ '{:,.2f}'.format(current.call_value) }}) Kalshi
    interpola NO ≈ <b>{{ '%.1f'|format(current.kalshi_no_at_call*100) }}%</b>
    (nuestra NO = {{ '%.0f'|format((1-quantile)*100) }}% por construcción).</div>
  {% endif %}
  {% if recommendation %}
  <div class="rec {{recommendation.cls}}">
    {{recommendation.label}}
    <span class="rec-rat">{{recommendation.rationale}}</span>
  </div>
  {% endif %}
</div>
{% endif %}

<div class="card">
  <div class="lbl" style="margin-bottom:.4rem">últimas {{rows|length}} calls</div>
  {% if rows %}
  <table>
    <tr><th>cierre AST</th><th>made @</th><th>p{{ '%.0f'|format(quantile*100) }} call</th>
        <th>actual</th><th>Δ$</th><th>edge</th><th>res</th></tr>
    {% for r in rows %}
    <tr>
      <td class="tlbl">{{r.target_iso}}</td>
      <td>${{ '{:,.2f}'.format(r.now_price) }}</td>
      <td>${{ '{:,.2f}'.format(r.call_value) }}</td>
      {% if r.actual_price is not none %}
      <td>${{ '{:,.2f}'.format(r.actual_price) }}</td>
      <td>{{ '%+.0f'|format(r.actual_price - r.call_value) }}</td>
      {% else %}
      <td class="dim">—</td><td class="dim">—</td>
      {% endif %}
      <td class="edge {% if r.edge_pp and r.edge_pp > 0 %}pos{% elif r.edge_pp and r.edge_pp < 0 %}neg{% endif %}">
        {% if r.edge_pp is not none %}{{ '%+.1f'|format(r.edge_pp) }}pp{% else %}—{% endif %}
      </td>
      <td>
        {% if r.won == 1 %}<span class="win">✓ win</span>
        {% elif r.won == 0 %}<span class="loss">✗ loss</span>
        {% else %}<span class="dim">pend</span>{% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="dim">Sin calls aún — espera al próximo XX:00 AST.</div>
  {% endif %}
</div>
</body></html>"""


HISTORY_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>History {{symbol}} {{target_iso}}</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;
       padding:1rem;max-width:920px;margin:0 auto}
  h1{color:#f9e2af;margin:0 0 .4rem}
  .dim{color:#6c7086;font-size:12px}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.7rem 0}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:5px 8px;border-bottom:1px solid #313244;text-align:right}
  th{color:#a6adc8;font-weight:normal}
  td.lbl{text-align:left;color:#cdd6f4}
  .gap{color:#f38ba8}.ok{color:#a6e3a1}
  a{color:#89b4fa}
  .big{font-size:1.6rem;color:#a6e3a1;font-weight:600}
</style></head><body>
<h1>{{symbol}} · cierre {{target_iso}}</h1>
<p><a href="/calibration?symbol={{symbol}}">← back</a></p>
{% if actual %}
<div class="card">
  <div class="dim">precio real al cierre</div>
  <div class="big">${{ price_fmt(actual) }}</div>
</div>
{% else %}
<div class="card dim">aún no settleado</div>
{% endif %}
<div class="card">
  <div class="dim" style="margin-bottom:.4rem">{{rows|length}} predicciones para este cierre</div>
  <table>
    <tr><th>made AST</th><th>lead</th><th>price@made</th><th>σ%</th>
        {% if actual %}<th>Δ% vs real</th><th>P(≥real)</th>{% endif %}</tr>
    {% for r in rows %}
    <tr>
      <td class="lbl">{{ r.made_iso }}</td>
      <td>{{ '%.1f'|format(r.lead_min) }}m</td>
      <td>${{ price_fmt(r.now_price) }}</td>
      <td>{{ '%.2f'|format(r.sigma_h_pct) }}%</td>
      {% if actual %}
      <td>{{ '%+.2f'|format((actual - r.now_price)/r.now_price*100) }}%</td>
      <td>{{ '%.0f'|format(r.p_above_actual*100) }}%</td>
      {% endif %}
    </tr>
    {% endfor %}
  </table>
  <p class="dim" style="margin-top:.5rem">
    price@made = precio actual cuando hicimos esa predicción.
    Si el precio cayó/subió fuerte durante la hora, lo verás en esa columna.
    σ% debería crecer si EWMA captura el aumento de vol.</p>
</div>
</body></html>"""


@app.route("/history")
def history_view():
    sym = request.args.get("symbol", "BTCUSDT").upper()
    if sym not in SYMBOLS:
        return ("symbol inválido", 400)
    try:
        target_at = float(request.args.get("target", "0"))
    except ValueError:
        return ("target inválido", 400)
    if target_at == 0:
        return ("falta ?target=epoch", 400)
    h = _cal.history_for_target(sym, target_at)
    rows = []
    for r in h["rows"]:
        rows.append({
            **r,
            "made_iso": _pr(r["made_at"]).strftime("%H:%M:%S"),
        })
    target_dt = _pr(target_at)
    return render_template_string(
        HISTORY_TMPL, symbol=sym, rows=rows,
        actual=h["actual_price"],
        target_iso=target_dt.strftime("%Y-%m-%d %H:%M AST"),
        price_fmt=_price_fmt,
    )


@app.route("/calibration")
def calibration_view():
    raw = request.args.get("symbol")
    sym = raw.upper() if raw and raw.upper() in SYMBOLS else None
    stats = _cal.reliability(symbol=sym)
    brier = _cal.overall_brier(symbol=sym)
    n_settled = sum(s.n for s in stats)
    recent = _cal.recent_outcomes(symbol=sym, limit=24)
    enriched = []
    for h in recent:
        enriched.append({**h.__dict__,
                         "target_iso": _pr(h.target_at).strftime("%m-%d %H:00")})
    tail = _cal.tail_stats(symbol=sym)
    shocks_raw = _cal.top_shocks(symbol=sym, limit=15)
    shocks = []
    for h in shocks_raw:
        shocks.append({**h.__dict__,
                       "iso": _pr(h.target_at).strftime("%m-%d %H:00")})
    kx = _cal.kalshi_compare() if (sym is None or sym == "BTCUSDT") else None
    if sym is None or sym == "BTCUSDT":
        bt = _cal.auto_bet_backtest(min_edge_pp=5.0)
        bt_rows = []
        for r in bt["rows"]:
            bt_rows.append({**r,
                            "iso": _pr(r["target_at"]).strftime("%m-%d %H:00")})
        bt["rows"] = bt_rows
    else:
        bt = None
    return render_template_string(
        CALIB_TMPL, stats=stats, brier=brier, n_settled=n_settled,
        symbol=sym, symbols=SYMBOLS, recent=enriched, price_fmt=_price_fmt,
        tail=tail, shocks=shocks, kx=kx, bt=bt,
    )


@app.route("/tutorial.pdf")
def tutorial_pdf():
    return send_from_directory(Path(__file__).parent, "tutorial.pdf",
                               mimetype="application/pdf")


@app.route("/tutorial")
def tutorial_html():
    return send_from_directory(Path(__file__).parent, "tutorial.html")


@app.route("/tutorial-btc.pdf")
def tutorial_btc_pdf():
    return send_from_directory(Path(__file__).parent, "tutorial_btc.pdf",
                               mimetype="application/pdf")


@app.route("/tutorial-btc")
def tutorial_btc_html():
    return send_from_directory(Path(__file__).parent, "tutorial_btc.html")


# Time-of-day buckets descubiertos en backtest BTC (2026-05-27, n=414 settled).
# WR 88-100% en MONEY_HOURS (09 AST, 16-18 AST), WR ≤50% en BAD_HOURS (03, 10 AST).
HCALL_MONEY_HOURS_AST = {9, 16, 17, 18}
HCALL_BAD_HOURS_AST = {3, 10}


def _hcall_recommendation(edge_pp: float | None, target_at: float | None) -> dict:
    """Recomendación visual basada en (1) edge sign, (2) hora target AST.

    Backtest: edge negativo ROI -11 a -32%, edge ≥0 ROI +22 a +69%.
    Hora target = hora del cierre. Convertimos a AST y miramos bucket.
    """
    if edge_pp is None:
        return {"label": "—", "cls": "dim", "rationale": "sin edge data"}
    hour_ast = None
    if target_at is not None:
        hour_ast = _pr(target_at).hour
    if edge_pp < 0:
        return {"label": "🚫 NO APOSTAR",
                "cls": "rec-bad",
                "rationale": f"edge {edge_pp:+.1f}pp < 0 → entry price alto, ROI histórico negativo"}
    if hour_ast in HCALL_BAD_HOURS_AST:
        return {"label": "⚠ HORA MALA",
                "cls": "rec-warn",
                "rationale": f"edge {edge_pp:+.1f}pp ok pero cierre {hour_ast:02d}:00 AST tiene WR ≤50%"}
    if hour_ast in HCALL_MONEY_HOURS_AST:
        return {"label": "🔥 APUESTA FUERTE",
                "cls": "rec-fire",
                "rationale": f"edge {edge_pp:+.1f}pp + cierre {hour_ast:02d}:00 AST (WR histórico 88-100%)"}
    return {"label": "✓ APUESTA",
            "cls": "rec-ok",
            "rationale": f"edge {edge_pp:+.1f}pp ≥ 0 (ROI histórico positivo)"}


INTRA15_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>BTC intra-15</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;
       padding:1rem;max-width:920px;margin:0 auto}
  h1{color:#f9e2af;margin:0 0 .4rem}
  .dim{color:#6c7086;font-size:12px}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.7rem 0}
  .lbl{font-size:.7rem;color:#6c7086;letter-spacing:.08em;text-transform:uppercase}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:5px 8px;border-bottom:1px solid #313244;text-align:right}
  th{color:#a6adc8;font-weight:normal}
  td.tlbl{text-align:left;color:#cdd6f4}
  a{color:#89b4fa}
  .edge.pos{color:#a6e3a1}
  .edge.neg{color:#f38ba8}
  form.q{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  form.q label{display:flex;flex-direction:column;font-size:.7rem;color:#a6adc8;
               letter-spacing:.05em}
  form.q input[type=text]{background:#11111b;color:#cdd6f4;border:1px solid #313244;
       border-radius:4px;padding:.4rem .6rem;font-family:monospace;font-size:14px;width:130px;
       margin-top:.15rem}
  form.q button{background:#89b4fa;color:#11111b;border:0;border-radius:4px;
       padding:.5rem 1rem;font-weight:600;cursor:pointer;align-self:flex-end}
  .rec{display:inline-block;padding:.2rem .5rem;border-radius:5px;
       font-weight:700;font-size:.8rem}
  .rec.rec-fire{background:#a6e3a1;color:#11111b}
  .rec.rec-ok{background:#94e2d5;color:#11111b}
  .rec.rec-warn{background:#f9e2af;color:#11111b}
  .rec.rec-bad{background:#f38ba8;color:#11111b}
  .rec.dim{background:#313244;color:#6c7086}
  .hero{font-size:2.2rem;font-weight:700;color:#a6e3a1;
        font-variant-numeric:tabular-nums;line-height:1.05}
  .tag-kalshi{background:#f9e2af;color:#11111b;padding:1px 6px;border-radius:3px;
              font-size:10px;font-weight:600;margin-left:.3rem}
</style></head><body>
<h1>BTC · intra-15 (próximos cierres 15-min)</h1>
<p class="dim">Modelo log-Student-t escalado con √(min al target). Útil cuando Kalshi
   aún no abrió mercado o como segunda lectura.
   <a href="/?symbol=BTCUSDT">← spot</a> · <a href="/hourly-call">hourly-call</a></p>

<div class="card">
  {% if data and data.brti_mid %}
  <div class="dim">precio actual <span class="tag-kalshi">KALSHI-ALIGNED · BRTI proxy</span></div>
  <div class="hero">${{ '{:,.2f}'.format(data.brti_mid) }}</div>
  <div class="dim" style="margin-top:.3rem">
    Binance (modelo): <b>${{ '{:,.2f}'.format(now_price) }}</b>
    · basis <b>{{ '%+.1f'|format(data.basis_bps) }} bps</b>
    {% if data %}· strike Binance-eq <b>${{ '{:,.2f}'.format(data.strike_adj_binance) }}</b>{% endif %}
  </div>
  {% else %}
  <div class="dim">precio actual (Binance · modelo)</div>
  <div class="hero">${{ '{:,.2f}'.format(now_price) }}</div>
  {% endif %}
</div>

<div class="card">
  <form method="get" action="/intra15" class="q">
    <label>strike $<input type="text" name="strike" value="{{ '%.2f'|format(strike) if strike else '' }}"></label>
    <label>YES ¢<input type="text" name="yes_cents" value="{{yes_cents if yes_cents is not none else ''}}" placeholder="--"></label>
    <label>NO ¢<input type="text" name="no_cents" value="{{no_cents if no_cents is not none else ''}}" placeholder="--"></label>
    <button type="submit">calc</button>
  </form>
</div>

{% if data %}
<div class="card">
  <div class="lbl" style="margin-bottom:.4rem">próximos {{data.rows|length}} cierres · strike ${{ '{:,.2f}'.format(strike) }}</div>
  <table>
    <tr>
      <th>cierre AST</th><th>mins</th><th>P(above)</th><th>P(below)</th><th>σ%</th>
      {% if yes_cents is not none %}<th>edge YES</th>{% endif %}
      {% if no_cents is not none %}<th>edge NO</th>{% endif %}
      {% if has_market %}<th>recomendación</th>{% endif %}
    </tr>
    {% for r in data.rows %}
    <tr>
      <td class="tlbl">{{r.label}}</td>
      <td>{{ '%.0f'|format(r.mins) }}</td>
      <td>{{ '%.1f'|format(r.p_above*100) }}%</td>
      <td>{{ '%.1f'|format(r.p_below*100) }}%</td>
      <td>{{ '%.2f'|format(r.sigma_pct) }}%</td>
      {% if yes_cents is not none %}
      <td class="edge {% if r.edge_yes_pp > 0 %}pos{% elif r.edge_yes_pp < 0 %}neg{% endif %}">{{ '%+.1f'|format(r.edge_yes_pp) }}pp</td>
      {% endif %}
      {% if no_cents is not none %}
      <td class="edge {% if r.edge_no_pp > 0 %}pos{% elif r.edge_no_pp < 0 %}neg{% endif %}">{{ '%+.1f'|format(r.edge_no_pp) }}pp</td>
      {% endif %}
      {% if has_market %}
      <td>{% if r.rec %}<span class="rec {{r.rec.cls}}">{{r.rec.label}}{% if r.best_side %} ({{r.best_side}}){% endif %}</span>{% else %}—{% endif %}</td>
      {% endif %}
    </tr>
    {% endfor %}
  </table>
  {% if not has_market %}
  <p class="dim" style="margin-top:.5rem">Pon YES¢ y/o NO¢ del mercado Kalshi para ver edge y recomendación.</p>
  {% else %}
  <p class="dim" style="margin-top:.5rem">Recomendación usa edge sign + bucket horario (mismo backtest que /hourly-call): money hours 09/16-18 AST 🔥, bad hours 03/10 AST ⚠, edge &lt;0 🚫.</p>
  {% endif %}
</div>
{% else %}
<div class="card dim">Sin datos del modelo todavía.</div>
{% endif %}
</body></html>"""


@app.route("/intra15")
def intra15_view():
    with _state_lock:
        snap = dict(_state["BTCUSDT"])
    pred = snap.get("pred")
    if pred is None:
        return ("Sin datos aún", 503)

    raw_strike = (request.args.get("strike") or "").replace(",", "").strip()
    raw_yes = (request.args.get("yes_cents") or "").strip()
    raw_no = (request.args.get("no_cents") or "").strip()

    strike = None
    if raw_strike:
        try:
            strike = float(raw_strike)
            if strike <= 0:
                strike = None
        except ValueError:
            strike = None
    if strike is None:
        strike = round(pred.now_price / 250.0) * 250.0

    def _parse_cents(s):
        if not s:
            return None
        try:
            v = float(s)
            if 0 <= v <= 100:
                return v
        except ValueError:
            pass
        return None

    yes_cents = _parse_cents(raw_yes)
    no_cents = _parse_cents(raw_no)
    has_market = yes_cents is not None or no_cents is not None

    with _external_lock:
        cb_mid = _external.get("brti_mid")
    data = _build_intra15(pred, strike, brti_mid=cb_mid,
                          brti_meta=_external.get("brti_meta"))

    if data:
        enriched = []
        for row in data["rows"]:
            target_unix = pred.fetched_at + row["mins"] * 60.0
            edge_yes_pp = ((row["p_above"] - yes_cents / 100.0) * 100
                           if yes_cents is not None else None)
            edge_no_pp = ((row["p_below"] - no_cents / 100.0) * 100
                          if no_cents is not None else None)
            rec = None
            best_side = None
            if has_market:
                cands = []
                if edge_yes_pp is not None:
                    cands.append(("YES", edge_yes_pp))
                if edge_no_pp is not None:
                    cands.append(("NO", edge_no_pp))
                best_side, best_edge = max(cands, key=lambda x: x[1])
                rec = _hcall_recommendation(best_edge, target_unix)
            enriched.append({
                **row,
                "edge_yes_pp": edge_yes_pp,
                "edge_no_pp": edge_no_pp,
                "best_side": best_side,
                "rec": rec,
            })
        data["rows"] = enriched

    return render_template_string(
        INTRA15_TMPL,
        data=data, strike=strike,
        yes_cents=yes_cents, no_cents=no_cents,
        has_market=has_market,
        now_price=pred.now_price,
    )


@app.route("/hourly-call")
def hourly_call_view():
    current = _hcall.current_call()
    rows_raw = _hcall.recent(limit=30)
    streak = _hcall.streak()
    rate = _hcall.empirical_rate()
    rows = []
    for r in rows_raw:
        rows.append({**r.__dict__,
                     "target_iso": _pr(r.target_at).strftime("%m-%d %H:00")})
    target_iso = ""
    mins_left = 0.0
    recommendation = None
    if current is not None:
        target_iso = _pr(current.target_at).strftime("%Y-%m-%d %H:00 AST")
        mins_left = max(0.0, (current.target_at - time.time()) / 60.0)
        recommendation = _hcall_recommendation(current.edge_pp, current.target_at)
    return render_template_string(
        HOURLY_TMPL,
        current=current, rows=rows, streak=streak, rate=rate,
        quantile=_hcall.QUANTILE, target_iso=target_iso, mins_left=mins_left,
        recommendation=recommendation,
    )


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    _cal.init_db()
    _hcall.init_db()
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"crypto-predictor on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
