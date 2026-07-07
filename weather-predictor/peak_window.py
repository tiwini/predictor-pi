"""Distribución empírica de la hora-del-pico por estación (últimos 7 días).

Open-Meteo archive da hourly temperature por estación con años de historia.
Por cada día completo de los últimos 7, encontramos la hora del max y
construimos p10/p50/p90/modal en hora local de la estación.

El reloj del día usa esto para que las zonas (confianza/decisiva/post)
sean específicas de la estación y se actualicen solas — sin esperar a
que acumulemos nuestra propia data ni a tunear PEAK_HOURS a mano.

Cache 24h por estación.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from predictor import Station

UA = "weather-predictor/0.1 jose.rubio.uhy@gmail.com"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

_TTL_SEC = 24 * 3600
# station_id → (computed_at_utc, payload | None)
_cache: dict[str, tuple[datetime, Optional[dict]]] = {}


def _fetch_peak_hours(station: Station, days: int = 7) -> Optional[list[int]]:
    """Devuelve lista de horas del pico (0-23) por cada uno de los últimos
    `days` días completos en hora local de la estación. None si falla la API."""
    today_local = datetime.now(station.tz).date()
    end = today_local - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    try:
        r = requests.get(ARCHIVE_URL, params={
            "latitude": station.lat,
            "longitude": station.lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": "temperature_2m",
            "timezone": station.tz.key,
            "temperature_unit": "fahrenheit",
        }, timeout=30, headers={"User-Agent": UA})
        try:
            import om_quota
            om_quota.count_call("peak_window_archive")
        except Exception:
            pass
        if r.status_code != 200:
            return None
        h = r.json().get("hourly", {})
        times = h.get("time") or []
        temps = h.get("temperature_2m") or []
    except (requests.RequestException, ValueError):
        return None
    if not times or len(times) != len(temps):
        return None

    per_day: dict[date, tuple[float, int]] = {}
    for ts, t in zip(times, temps):
        if t is None:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        d = dt.date()
        prev = per_day.get(d)
        if prev is None or t > prev[0]:
            per_day[d] = (float(t), dt.hour)
    if not per_day:
        return None
    return [h for _, h in per_day.values()]


def _stats(peak_hours: list[int]) -> dict:
    n = len(peak_hours)
    s = sorted(peak_hours)

    def pct(p: float) -> float:
        if n <= 1:
            return float(s[0])
        idx = max(0, min(n - 1, int(round((n - 1) * p))))
        return float(s[idx])

    counts: dict[int, int] = {}
    for hr in peak_hours:
        counts[hr] = counts.get(hr, 0) + 1
    modal = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]

    return {
        "n": n,
        "p10": pct(0.10),
        "p50": pct(0.50),
        "p90": pct(0.90),
        "modal_hour": int(modal),
        "samples": list(s),
    }


def get(station: Station) -> Optional[dict]:
    """Devuelve stats empíricos o None si el archive no respondió. Cachea 24h."""
    sid = station.id
    now = datetime.now(timezone.utc)
    cached = _cache.get(sid)
    if cached and (now - cached[0]).total_seconds() < _TTL_SEC:
        return cached[1]
    peaks = _fetch_peak_hours(station)
    payload = _stats(peaks) if peaks else None
    _cache[sid] = (now, payload)
    return payload


def clear_cache() -> None:
    _cache.clear()
