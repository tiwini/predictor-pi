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


Curve = tuple[list[float], list[float], list[float | None], list[float | None]]


def _fetch_curve(event_ticker: str) -> Curve:
    """Para un evento, devuelve (strikes, mids, bids, asks) — todos alineados
    al mismo índice y ordenados por strike. mid = (yes_bid+yes_ask)/2 si ambos
    lados, else el que exista. bids/asks pueden traer None para lados vacíos
    (spread detector: fable dark data #4 — sin bid/ask no distingues edge real
    de mid inventado sobre bid 1¢/ask 15¢). Skip markets sin ninguna cotización."""
    r = requests.get(f"{BASE}/markets",
                     params={"event_ticker": event_ticker, "limit": 1000},
                     timeout=10.0)
    r.raise_for_status()
    rows: list[tuple[float, float, float | None, float | None]] = []
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
        # Kalshi convention: 0.0 en yes_bid/yes_ask = lado sin cotización
        # (no "cotizado a 0¢"). Sin este filtro, evento recién abierto con
        # book vacío en 188 strikes producía mid=0.0 → kalshi_no=100% →
        # edge fantasma (visto en row 967, 2026-07-05 02:00 UTC).
        bid_r = bid if (bid is not None and bid > 0.0) else None
        ask_r = ask if (ask is not None and ask > 0.0) else None
        if bid_r is None and ask_r is None:
            continue
        if bid_r is None: mid = ask_r
        elif ask_r is None: mid = bid_r
        else: mid = (bid_r + ask_r) / 2.0
        rows.append((float(strike), mid, bid, ask))
    rows.sort()
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


def _curve_for(target_at: float) -> Curve | None:
    now = time.time()
    cached = _cache.get(target_at)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1], cached[2], cached[3], cached[4]
    ev = event_for_target(target_at)
    if ev is None:
        return None
    try:
        curve = _fetch_curve(ev)
    except Exception:
        return None
    if not curve[0]:
        return None
    _cache[target_at] = (now,) + curve
    return curve


def _curve_for_with_reason(
    target_at: float,
) -> tuple[Curve | None, str | None]:
    """Como _curve_for pero devuelve (curve, reason). reason es None cuando
    curve no lo es. Categorías: 'events_error', 'no_event',
    'markets_error', 'empty_curve'."""
    now = time.time()
    cached = _cache.get(target_at)
    if cached and now - cached[0] < _CACHE_TTL:
        return (cached[1], cached[2], cached[3], cached[4]), None
    try:
        ev = event_for_target(target_at)
    except Exception:
        return None, "events_error"
    if ev is None:
        return None, "no_event"
    try:
        curve = _fetch_curve(ev)
    except Exception:
        return None, "markets_error"
    if not curve[0]:
        return None, "empty_curve"
    _cache[target_at] = (now,) + curve
    return curve, None


def nearest_strike(target_at: float, value: float) -> tuple[float, float] | None:
    """Strike discreto más cercano a `value` y su mid YES en (0,1).
    Devuelve None si no hay evento abierto para target_at."""
    curve = _curve_for(target_at)
    if curve is None:
        return None
    strikes, mids, _bids, _asks = curve
    if not strikes:
        return None
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - value))
    return strikes[i], mids[i]


def nearest_strike_with_reason(
    target_at: float, value: float
) -> tuple[tuple[float, float] | None, str | None]:
    """Como nearest_strike pero devuelve (result, reason). reason clasifica
    por qué se retornó None; None cuando result no lo es."""
    curve, reason = _curve_for_with_reason(target_at)
    if curve is None:
        return None, reason
    strikes, mids, _bids, _asks = curve
    if not strikes:
        return None, "empty_curve"
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - value))
    return (strikes[i], mids[i]), None


def curve_and_strike_with_reason(
    target_at: float, value: float
) -> tuple[tuple[float, float] | None,
           Curve | None,
           str | None]:
    """Un solo fetch (respeta cache) devuelve: nearest strike+mid, curva
    completa (strikes, mids, bids, asks YES), reason. Fable dark data #1:
    la curva completa (~188 markets) es información que se estaba tirando
    por call. Fable dark data #4 (2026-07-05): añadidos bids/asks al mismo
    curve — sin spread no distingues edge real de mid inventado sobre
    bid 1¢/ask 15¢ (confounder principal del edge_pp actual)."""
    curve, reason = _curve_for_with_reason(target_at)
    if curve is None:
        return None, None, reason
    strikes, mids, _bids, _asks = curve
    if not strikes:
        return None, None, "empty_curve"
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - value))
    return (strikes[i], mids[i]), curve, None


def implied_above(target_at: float, threshold: float) -> float | None:
    """P(BTC > threshold) implícita por Kalshi al cierre `target_at`.
    Interpola linealmente entre strikes adyacentes."""
    curve = _curve_for(target_at)
    if curve is None:
        return None
    strikes, mids, _bids, _asks = curve
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
