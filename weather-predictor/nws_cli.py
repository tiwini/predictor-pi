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
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

UA = "weather-predictor/0.1 jose.rubio.uhy@gmail.com"
API = "https://api.weather.gov"

# Station id NWS → location code que NWS usa en /products?location=...
# NY: Kalshi KXHIGHNY liquida con Central Park (KNYC), mismo id que la station id.
# Source of truth en stations.py.
from stations import STATION_TO_LOCATION, STATION_TZ  # noqa: E402

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

# CF6 (F-6) trae el mes entero en un producto, así que el cache es por
# (station, year, month) y no por día.
_cf6_cache: dict[tuple[str, int, int],
                 dict[str, tuple[Optional[float], Optional[float]]]] = {}


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
        # El valor puede traer sufijo de una letra: 'R' = record, 'E' =
        # estimated ("MAXIMUM 101R"). Sin el [A-Z]? el \b no cierra entre el
        # dígito y la letra, el match falla y el día de récord se pierde —
        # exactamente lo que dejó KDEN 2026-07-25 settleado en 77 en vez de 101.
        m = re.match(rf"\s+{keyword}\s+(-?\d+)[A-Z]?(?=\s|$)", ln)
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

    # Cada ubicación emite 2-3 CLI por día: uno matinal y uno de la tarde que
    # reportan el día EN CURSO (parciales, con el max alcanzado hasta ese
    # momento) y el final pasada la medianoche local. Sólo aceptamos productos
    # emitidos después del cierre del día objetivo — un parcial es peor que no
    # settlear: el CLI matinal de KDEN del 2026-07-25 traía MAXIMUM 77 y el día
    # cerró en 101. Antes el walk caía en ese parcial cuando el final no
    # parseaba. Si no hay final todavía devolvemos None y el caller reintenta
    # (o cae al CF6, que sólo trae días cerrados).
    cutoff = None
    tzname = STATION_TZ.get(sid)
    if tzname is not None:
        cutoff = datetime.combine(target_date + timedelta(days=1),
                                  dtime(0, 0), tzinfo=ZoneInfo(tzname))

    # Walk newest-first; el más reciente para target_date es el final
    for item in items:
        pid = item.get("id")
        if not pid:
            continue
        iss = item.get("issuanceTime")
        if cutoff is not None and iss:
            try:
                if datetime.fromisoformat(iss) < cutoff:
                    continue
            except ValueError:
                pass
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


# ─────────────────── CLI intradía (piso, NO settle) ───────────────────
#
# Cache PROPIO, deliberadamente separado de `_cache`. Los valores que viven acá
# vienen de reports PARCIALES del día en curso: si compartieran cache con
# `fetch_max_min_for`, un parcial se serviría como settle y volveríamos exacto
# al bug que dejó KDEN 2026-07-25 liquidado en 77°F en vez de 101. La
# separación es la guarda — no la borres para "ahorrar un fetch".
# key → (max_f, issued_at_utc, fetched_at_monotonic)
_intraday_cache: dict[tuple[str, str],
                      tuple[Optional[float], Optional[datetime], float]] = {}

INTRADAY_TTL_S = 20 * 60


def fetch_intraday_max(
        station_id: str, target_date: date,
        limit: int = 6, timeout: float = 15.0, now: Optional[float] = None
) -> tuple[Optional[float], Optional[datetime]]:
    """(max_f, issued_at_utc) del CLI más reciente que reporta target_date,
    **aceptando parciales del día en curso**.

    Esto NO es un settle y no debe escribirse nunca en `day_outcomes`: es el
    max acumulado según el NWS hasta `issued_at`. Su valor está en que se mide
    con ASOS 1-min mientras nuestro feed de obs es de 5 min — medido sobre 486
    días-estación, el CLI de la tarde le gana a nuestro `today_max_obs` una
    mediana de **+1.0°F**, y en 49% de los días ≥1°F. Se consume como piso
    (`max(...)`), que es la única operación segura con un parcial: el valor
    sólo puede subir con el correr del día.

    Devuelve (None, None) si todavía no hay ningún CLI de hoy.
    """
    sid = station_id.upper()
    loc = STATION_TO_LOCATION.get(sid)
    if loc is None:
        return (None, None)

    key = (sid, target_date.isoformat())
    clock = time.monotonic() if now is None else now
    hit = _intraday_cache.get(key)
    if hit is not None and clock - hit[2] < INTRADAY_TTL_S:
        return (hit[0], hit[1])

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

    # Newest-first: el primero cuyo cuerpo reporte target_date es el más
    # reciente que tenemos para hoy. A diferencia del settle, acá NO filtramos
    # por issuanceTime — el parcial es justamente lo que buscamos.
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
        if _parse_summary_date(text) != target_date:
            continue
        mx = _parse_max(text)
        if mx is None:
            continue
        issued = None
        iss = item.get("issuanceTime")
        if iss:
            try:
                issued = datetime.fromisoformat(iss).astimezone(timezone.utc)
            except ValueError:
                issued = None
        _intraday_cache[key] = (mx, issued, clock)
        return (mx, issued)

    # Sin CLI de hoy todavía: cacheamos el negativo para no repegarle a la API
    # cada 3 minutos durante la ventana de pico.
    _intraday_cache[key] = (None, None, clock)
    return (None, None)


def _parse_cf6(text: str) -> tuple[Optional[tuple[int, int]],
                                   dict[int, tuple[Optional[float],
                                                   Optional[float]]]]:
    """Parsea un CF6 (F-6) → ((year, month), {día: (max_f, min_f)}).

    El bloque diario vive entre la fila de encabezados 'DY MAX MIN ...' y el
    '====' que la cierra; después vienen sumas (SM/AV) y una página 2 con otro
    header MONTH/YEAR que no debemos leer. 'M' = missing.
    """
    m_mon = re.search(r"MONTH:\s+([A-Z]+)", text)
    m_yr = re.search(r"YEAR:\s+(\d{4})", text)
    if not (m_mon and m_yr):
        return (None, {})
    mon = _MONTHS.get(m_mon.group(1)) or _MONTHS_ABBR.get(m_mon.group(1)[:3])
    if mon is None:
        return (None, {})
    year = int(m_yr.group(1))
    days: dict[int, tuple[Optional[float], Optional[float]]] = {}
    in_block = False
    seen_sep = False
    for ln in text.split("\n"):
        if not in_block:
            if ln.startswith("DY MAX MIN"):
                in_block = True
            continue
        if ln.startswith("="):
            # El primer '====' abre el bloque diario (viene pegado al header de
            # columnas); el segundo lo cierra y arrancan las sumas SM/AV.
            if not seen_sep:
                seen_sep = True
                continue
            break
        if ln.startswith("SM ") or ln.startswith("AV"):
            break  # defensa por si algún WFO no imprime el separador de cierre
        m = re.match(r"\s{0,2}(\d{1,2})\s+(-?\d+|M)\s+(-?\d+|M)\b", ln)
        if not m:
            continue
        day = int(m.group(1))
        if not 1 <= day <= 31:
            continue
        mx = None if m.group(2) == "M" else float(m.group(2))
        mn = None if m.group(3) == "M" else float(m.group(3))
        days[day] = (mx, mn)
    return ((year, mon), days)


def fetch_month_extremes(
        station_id: str, year: int, month: int,
        limit: int = 6, timeout: float = 20.0
) -> dict[str, tuple[Optional[float], Optional[float]]]:
    """CF6: max/min de TODOS los días del mes en un solo producto.

    Existe además del CLI porque /products sólo sirve el CLI ~2-3 días: un día
    que falle su ventana queda huérfano para siempre (medido con KPHX
    2026-07-24, ver memoria bug_settle_window_kphx_2026_07_24_perdido). El CF6
    se emite a diario con el mes entero acumulado, así que backfillea cualquier
    hueco del mes en curso. Misma fuente NWS — no es Open-Meteo.

    Devuelve {iso_date: (max_f, min_f)}. El día en curso puede venir parcial o
    faltar; filtrarlo es responsabilidad del caller.
    """
    sid = station_id.upper()
    loc = STATION_TO_LOCATION.get(sid)
    if loc is None:
        return {}
    key = (sid, year, month)
    if key in _cf6_cache:
        return _cf6_cache[key]

    headers = {"User-Agent": UA, "Accept": "application/ld+json"}
    try:
        r = requests.get(f"{API}/products",
                         params={"type": "CF6", "location": loc,
                                 "limit": limit},
                         headers=headers, timeout=timeout)
        if r.status_code != 200:
            return {}
        items = r.json().get("@graph", [])
    except (requests.RequestException, ValueError):
        return {}

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
        ym, days = _parse_cf6(text)
        if ym != (year, month) or not days:
            continue
        out: dict[str, tuple[Optional[float], Optional[float]]] = {}
        for day, vals in days.items():
            try:
                out[date(year, month, day).isoformat()] = vals
            except ValueError:
                continue
        _cf6_cache[key] = out
        return out
    return {}


def fetch_max_min_cf6(
        station_id: str, target_date: date, timeout: float = 20.0
) -> tuple[Optional[float], Optional[float]]:
    """(max_f, min_f) de target_date vía CF6. Fallback del CLI, no reemplazo:
    el CF6 es PRELIMINARY y Kalshi liquida con el CLI, así que sólo debe
    usarse cuando el CLI ya no está disponible."""
    got = fetch_month_extremes(station_id, target_date.year,
                               target_date.month, timeout=timeout)
    return got.get(target_date.isoformat(), (None, None))


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
    _cf6_cache.clear()
    _intraday_cache.clear()
