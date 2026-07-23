"""NWS Climatological Report (CLI) parser para settle de daily max.

Kalshi liquida con NWS CLI del WFO correspondiente a cada estación. Cada
ubicación emite ~2 reports/día: uno preliminar al final de la tarde y uno
final pasada la medianoche local. Tomamos el último report cuya fecha en
el cuerpo coincide con target_date — es el final.

API:
  - GET /products?type=CLI&location=<LOC>&limit=N → lista metadata
  - GET /products/<id> → productText con el cuerpo CLI

Si para target_date aún no hay final (ej. consultando muy pronto), devolvemos
None y dejamos que el caller use fallback (Open-Meteo archive).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

import requests

UA = "weather-predictor/0.1 jose.rubio.uhy@gmail.com"
API = "https://api.weather.gov"

# Station id NWS → location code que NWS usa en /products?location=...
# NY: Kalshi KXHIGHNY liquida con Central Park (KNYC), mismo id que la station id.
# Source of truth en stations.py.
from stations import STATION_TO_LOCATION  # noqa: E402

_MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
     "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], start=1)}
_MONTHS_ABBR = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# In-process cache de (station_id, date) → (max_f, min_f).
# F8 fase 0 2026-07-10: pasamos de float a tupla para persistir min sin
# duplicar la búsqueda del producto (max y min están en el mismo CLI).
_cache: dict[tuple[str, str], tuple[Optional[float], Optional[float]]] = {}


def _parse_summary_date(text: str) -> Optional[date]:
    """Extrae la fecha del cuerpo del CLI: '...CLIMATE SUMMARY FOR MAY 7 2026...'"""
    m = re.search(r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d+)\s+(\d{4})", text)
    if not m:
        return None
    mon_s, day_s, year_s = m.group(1), m.group(2), m.group(3)
    mon = _MONTHS.get(mon_s) or _MONTHS_ABBR.get(mon_s[:3])
    if mon is None:
        return None
    try:
        return date(int(year_s), mon, int(day_s))
    except ValueError:
        return None


def _parse_temp_extreme(text: str, keyword: str) -> Optional[float]:
    """Extrae MAXIMUM o MINIMUM del bloque TEMPERATURE (F)."""
    in_block = False
    for ln in text.split("\n"):
        if "TEMPERATURE (F)" in ln:
            in_block = True
            continue
        if not in_block:
            continue
        m = re.match(rf"\s+{keyword}\s+(-?\d+)\b", ln)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        # Salimos del bloque al toparnos con otra sección
        if ln.strip().endswith("(IN)") or ln.strip().endswith("(MPH)"):
            return None
    return None


def _parse_max(text: str) -> Optional[float]:
    """Extrae el max diario del bloque TEMPERATURE (F)."""
    return _parse_temp_extreme(text, "MAXIMUM")


def _parse_min(text: str) -> Optional[float]:
    """Extrae el min diario del bloque TEMPERATURE (F).
    Aparece justo después de MAXIMUM en el mismo bloque."""
    return _parse_temp_extreme(text, "MINIMUM")


def fetch_max_min_for(
        station_id: str, target_date: date,
        limit: int = 10, timeout: float = 15.0
) -> tuple[Optional[float], Optional[float]]:
    """Devuelve (max_f, min_f) del NWS CLI para target_date, o (None, None) si
    no hay report final aún. Un único fetch para ambos extremos.

    F8 fase 0: min viaja en el mismo producto que ya pedimos para max,
    así que persistirlo es gratis en términos de red."""
    sid = station_id.upper()
    loc = STATION_TO_LOCATION.get(sid)
    if loc is None:
        return (None, None)
    key = (sid, target_date.isoformat())
    if key in _cache:
        return _cache[key]

    headers = {"User-Agent": UA, "Accept": "application/ld+json"}
    try:
        r = requests.get(f"{API}/products",
                         params={"type": "CLI", "location": loc, "limit": limit},
                         headers=headers, timeout=timeout)
        if r.status_code != 200:
            return (None, None)
        items = r.json().get("@graph", [])
    except (requests.RequestException, ValueError):
        return (None, None)

    # Walk newest-first; el más reciente para target_date es el final
    for item in items:
        pid = item.get("id")
        if not pid:
            continue
        try:
            r2 = requests.get(f"{API}/products/{pid}",
                              headers=headers, timeout=timeout)
            if r2.status_code != 200:
                continue
            text = r2.json().get("productText", "")
        except (requests.RequestException, ValueError):
            continue
        d = _parse_summary_date(text)
        if d != target_date:
            continue
        mx = _parse_max(text)
        mn = _parse_min(text)
        if mx is not None:
            _cache[key] = (mx, mn)
            return (mx, mn)
    return (None, None)


def fetch_max_for(station_id: str, target_date: date,
                  limit: int = 10, timeout: float = 15.0) -> Optional[float]:
    """Backward-compat wrapper — solo max."""
    return fetch_max_min_for(station_id, target_date, limit, timeout)[0]


def fetch_min_for(station_id: str, target_date: date,
                  limit: int = 10, timeout: float = 15.0) -> Optional[float]:
    """Devuelve el min observado en target_date (mismo CLI, cache compartido)."""
    return fetch_max_min_for(station_id, target_date, limit, timeout)[1]


def clear_cache() -> None:
    _cache.clear()
