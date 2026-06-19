"""Kalshi BTC hourly market lookup — endpoint público, sin auth.

Series KXBTCD: 1 evento por hora, cada uno con ~188 markets binarios
"BTC > $X al cierre de esta hora UTC". Mid price ≈ probabilidad implícita.

Uso típico:
    p = kalshi.implied_above(target_at_unix=..., threshold=80000.0)
"""
from __future__ import annotations

import bisect
import time
from datetime import datetime, timezone

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTCD"

# Cache: {target_at: (timestamp_fetched, sorted_strikes, mid_prices)}
# Markets se cotizan rápido pero la curva sólo cambia significativamente cada
# pocos segundos; cache de 5s evita martillar la API.
_cache: dict[float, tuple] = {}
_CACHE_TTL = 5.0


def _open_events() -> list[dict]:
    r = requests.get(f"{BASE}/events",
                     params={"series_ticker": SERIES,
                             "status": "open", "limit": 50},
                     timeout=8.0)
    r.raise_for_status()
    return r.json().get("events", [])


def event_for_target(target_at: float) -> str | None:
    """Devuelve event_ticker cuyo strike_date == target_at, o None si no hay."""
    target_dt = datetime.fromtimestamp(target_at, tz=timezone.utc)
    for e in _open_events():
        sd = datetime.fromisoformat(e["strike_date"].replace("Z", "+00:00"))
        if abs((sd - target_dt).total_seconds()) < 30:
            return e["event_ticker"]
    return None


def _fetch_curve(event_ticker: str) -> tuple[list[float], list[float]]:
    """Para un evento, devuelve (strikes ordenados, mid prices YES).
    mid = (yes_bid + yes_ask)/2. Si no hay bid, usa ask. Si no hay ask, usa bid.
    Skip markets sin cotización."""
    r = requests.get(f"{BASE}/markets",
                     params={"event_ticker": event_ticker, "limit": 1000},
                     timeout=10.0)
    r.raise_for_status()
    pairs: list[tuple[float, float]] = []
    for m in r.json().get("markets", []):
        strike = m.get("floor_strike") or m.get("cap_strike")
        if strike is None:
            continue
        bid = m.get("yes_bid_dollars")
        ask = m.get("yes_ask_dollars")
        try:
            bid = float(bid) if bid not in (None, "") else None
            ask = float(ask) if ask not in (None, "") else None
        except (TypeError, ValueError):
            continue
        if bid is None and ask is None:
            continue
        if bid is None: mid = ask
        elif ask is None: mid = bid
        else: mid = (bid + ask) / 2.0
        pairs.append((float(strike), mid))
    pairs.sort()
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _curve_for(target_at: float) -> tuple[list[float], list[float]] | None:
    now = time.time()
    cached = _cache.get(target_at)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1], cached[2]
    ev = event_for_target(target_at)
    if ev is None:
        return None
    try:
        strikes, mids = _fetch_curve(ev)
    except Exception:
        return None
    if not strikes:
        return None
    _cache[target_at] = (now, strikes, mids)
    return strikes, mids


def nearest_strike(target_at: float, value: float) -> tuple[float, float] | None:
    """Strike discreto más cercano a `value` y su mid YES en (0,1).
    Devuelve None si no hay evento abierto para target_at."""
    curve = _curve_for(target_at)
    if curve is None:
        return None
    strikes, mids = curve
    if not strikes:
        return None
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - value))
    return strikes[i], mids[i]


def implied_above(target_at: float, threshold: float) -> float | None:
    """P(BTC > threshold) implícita por Kalshi al cierre `target_at`.
    Interpola linealmente entre strikes adyacentes."""
    curve = _curve_for(target_at)
    if curve is None:
        return None
    strikes, mids = curve
    # strikes están en orden; mids son P(BTC > strike) decrecientes en strike
    if threshold <= strikes[0]:
        return mids[0]
    if threshold >= strikes[-1]:
        return mids[-1]
    i = bisect.bisect_left(strikes, threshold)
    a, b = strikes[i - 1], strikes[i]
    pa, pb = mids[i - 1], mids[i]
    t = (threshold - a) / (b - a)
    return pa + t * (pb - pa)
