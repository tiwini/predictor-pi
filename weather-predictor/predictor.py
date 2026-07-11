#!/usr/bin/env python3
"""Weather prediction CLI — educational tool, Kalshi-style probabilities.

Data:
  - NWS api.weather.gov for station metadata + METAR observations (WU-compatible)
  - Open-Meteo ensemble (31 members, GFS) for probabilistic forecasts

Run:  python3 predictor.py [STATION_ID]   (default KPHX)
"""
import json
import re
import sys
import threading
import time
import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
try:
    from climatology import percentile_of as _climate_percentile
except Exception:
    _climate_percentile = None
try:
    import calibration as _calibration
except Exception:
    _calibration = None
try:
    import kalshi as _kalshi
except Exception:
    _kalshi = None
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns

UA = "weather-predictor/0.1 (educational; contact=local)"
PR_TZ = ZoneInfo("America/Puerto_Rico")
POLL_SEC = 600  # 10 min

# Typical daily-peak window (inclusive-exclusive, local hour). Single source
# of truth en stations.py; aquí solo importamos. Usado para adaptive polling
# (predictor_web) y per-hour σ en Bayesian reweight.
from stations import PEAK_HOURS  # noqa: E402


def sigma_for_hour(hour: int, station_id: str) -> float:
    """Per-hour σ (°F) for the Bayesian reweight likelihood. Hours closer to
    the station's typical peak window get tighter σ; distant hours (dawn,
    late evening) get wider σ so noisy early-morning obs don't dominate.
    Values tuned so avg(1/σ²) over a typical day ≈ old flat σ=2 baseline,
    keeping eff_n in a similar range while redistributing weight toward
    peak-hour matches."""
    lo, hi = PEAK_HOURS.get(station_id, (12, 16))
    if lo <= hour < hi:
        return 1.5
    dist = min(abs(hour - lo), abs(hour - (hi - 1)))
    if dist <= 2:
        return 2.0
    if dist <= 4:
        return 2.5
    return 3.5
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SPARKS = "▁▂▃▄▅▆▇█"
console = Console()


# ───────────────────── models ─────────────────────

@dataclass
class Station:
    id: str
    name: str
    lat: float
    lon: float
    tz: ZoneInfo


@dataclass
class Assertion:
    expr: str            # display: ">89F" or "~80.5F±0.25"
    op: str              # ">", ">=", "<", "<=", "~"
    threshold: float
    auto: bool = False   # auto-suggested slot
    locked: bool = False # if auto+locked, don't recompute threshold
    bin_half: float = 0.5  # half-width for "~" op (so bin is [thr-h, thr+h])
    history: list = field(default_factory=list)  # [(ts, prob)]


@dataclass
class Snapshot:
    fetched_at: datetime
    station_local: datetime
    current_temp_f: float
    current_desc: str
    current_obs_time: datetime
    today_min_obs: float
    today_max_obs: float
    obs_count: int
    ensemble_daily_maxes: list   # per-member simulated max today
    forecast_next_hours: list    # [(ts_local, median, p10, p90)]
    prob_rising: float           # P(max aún sube por encima del observado)
    peak_status: str             # "subiendo" | "posible alza" | "pico probable" | "pico confirmado"
    # extended METAR fields (may be None if unavailable)
    dewpoint_f: float | None = None
    humidity_pct: float | None = None
    heat_index_f: float | None = None
    wind_chill_f: float | None = None
    wind_mph: float | None = None
    wind_gust_mph: float | None = None
    wind_dir_deg: float | None = None
    wind_dir_card: str | None = None
    pressure_inhg: float | None = None
    pressure_trend_3h: float | None = None  # inHg delta over last 3h
    visibility_mi: float | None = None
    # day chart: list of (hour_0_23, observed_f|None, median_f|None, p10_f|None, p90_f|None)
    day_chart: list = field(default_factory=list)
    # historical context (None if module unavailable or first-fetch in progress)
    climatology: object | None = None
    climatology_target_f: float | None = None  # the value we compared (expected max)
    # Bayesian reweighting diagnostics (populated by build_snapshot)
    ensemble_raw_maxes: list = field(default_factory=list)  # unweighted per-member daily max
    ensemble_weights: list = field(default_factory=list)    # normalized weights per raw member
    ensemble_eff_n: float | None = None   # Kish effective sample size (1/Σw²)
    ensemble_residual_hours: int = 0      # obs hours used to compute weights
    # Regime-break detector: past hours today where observed temp fell outside
    # the ensemble's [p1, p99] range (i.e. the model didn't even bracket
    # reality). Populated by build_snapshot. >=2 hours = likely blown forecast.
    regime_break_hours: list = field(default_factory=list)   # e.g. [8, 9, 10]
    # Per-hour reweight diagnostics (populated by build_snapshot). Each entry:
    # {hour, obs, p10, p50, p90, sigma, n_members, out_of_range}. Used by
    # the /reweight visibility panel to explain weight decisions.
    reweight_diagnostics: list = field(default_factory=list)
    # Per-station rolling bias correction subtracted from the ensemble.
    # `bias_correction_f` is the actual offset applied (0.0 if not applied).
    # `bias_info` carries the full diagnostic (n samples, threshold reason).
    bias_correction_f: float = 0.0
    bias_info: dict | None = None
    # Posterior shift hacia mediana externa (post-bias, pre-display).
    # ext_shift_f es °F sumados a daily_maxes (positivo = subimos pred).
    # ext_shift_info["ext_diff_pre"] preserva el ext_diff original para que el
    # gate direccional de bets no se autoatenúe.
    ext_shift_f: float = 0.0
    ext_shift_info: dict | None = None


# ───────────────────── fetchers ─────────────────────

def c_to_f(c):
    return None if c is None else c * 9 / 5 + 32


def kmh_to_mph(k):
    return None if k is None else k * 0.621371


def pa_to_inhg(pa):
    return None if pa is None else pa * 0.00029530


def m_to_mi(m):
    return None if m is None else m * 0.000621371


CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def deg_to_cardinal(d):
    if d is None:
        return None
    return CARDINALS[int((d % 360) / 22.5 + 0.5) % 16]


# Forecast-target overrides: cuando el id METAR no coincide con el punto
# que liquida el mercado. Mantenemos id (claves DB); el lat/lon del forecast
# apunta al punto Kalshi.
# KLGA: Kalshi NY (KXHIGHNY) liquida con NYC CLI = Central Park (KNYC).
STATION_OVERRIDES: dict[str, dict] = {
    "KLGA": {"name": "New York (Central Park, settle Kalshi)",
             "lat": 40.7794, "lon": -73.9692},
}

# Observation overrides: jalar obs (current + intra-día) de la estación que
# liquida Kalshi en vez del METAR id. Antes obs venían de KLGA airport
# mientras la pred era para Central Park — mismatch silencioso al validar
# bets en vivo. Ahora ambas miran KNYC.
OBS_STATION_OVERRIDES: dict[str, str] = {
    "KLGA": "KNYC",
}


def fetch_station(sid: str) -> Station:
    r = requests.get(f"https://api.weather.gov/stations/{sid}",
                     headers={"User-Agent": UA}, timeout=10)
    r.raise_for_status()
    d = r.json()
    p = d["properties"]
    lon, lat = d["geometry"]["coordinates"]
    name = p["name"]
    ov = STATION_OVERRIDES.get(sid.upper())
    if ov:
        name = ov.get("name", name)
        lat = ov.get("lat", lat)
        lon = ov.get("lon", lon)
    return Station(id=sid.upper(), name=name, lat=lat, lon=lon,
                   tz=ZoneInfo(p["timeZone"]))


def fetch_current(station: Station) -> dict:
    """Latest observation. If the most recent reading has no temperature
    (NWS sometimes returns null on preliminary/QC-pending reports), walk
    back through recent observations until we find one with a valid temp."""
    obs_sid = OBS_STATION_OVERRIDES.get(station.id, station.id)
    r = requests.get(
        f"https://api.weather.gov/stations/{obs_sid}/observations",
        params={"limit": 10},
        headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    features = r.json().get("features", [])
    p = None
    for f in features:
        cand = f["properties"]
        t = cand.get("temperature") or {}
        if t.get("value") is not None:
            p = cand
            break
    if p is None:
        # fall back to whatever the first entry says, even if temp is None
        p = features[0]["properties"] if features else {}
    ts_raw = p.get("timestamp")
    if ts_raw:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    else:
        ts = datetime.now(timezone.utc)

    def val(k):
        v = p.get(k)
        return v.get("value") if isinstance(v, dict) else None

    wind_deg = val("windDirection")
    return {
        "temp_f": c_to_f(val("temperature")),
        "desc": p.get("textDescription") or "",
        "time": ts,
        "dewpoint_f": c_to_f(val("dewpoint")),
        "humidity_pct": val("relativeHumidity"),
        "heat_index_f": c_to_f(val("heatIndex")),
        "wind_chill_f": c_to_f(val("windChill")),
        "wind_mph": kmh_to_mph(val("windSpeed")),
        "wind_gust_mph": kmh_to_mph(val("windGust")),
        "wind_dir_deg": wind_deg,
        "wind_dir_card": deg_to_cardinal(wind_deg),
        "pressure_inhg": pa_to_inhg(val("barometricPressure")),
        "visibility_mi": m_to_mi(val("visibility")),
    }


def fetch_today_obs(station: Station) -> list:
    """Return today's official METAR observations as list of dicts.

    NWS stations publish ~230+ automated readings/día; many are transient
    glitches (seen 27°C spikes at midnight vs 25°C daytime). Filter:
      • lecturas con `rawMessage` (METAR/SPECI oficiales), O
      • lecturas en minuto :53 o :54 aunque el texto raw no haya propagado.
        ASOS samplea a :53 (:54 en algunas estaciones); esas son las mismas
        del METAR oficial, sólo que la API a veces adjunta el texto tarde.
    Fable decision 2026-07-10 tras incidente KIAH (real 91.9°F a las 14:53
    sin rawMessage → sistema mostraba 91.0°F). Rechazos se loggean para
    graduar en ~2 sem a guarda por vecinos temporales (opción C).

    Each entry: {'time': datetime, 'temp_f': float|None, 'pressure_inhg': float|None}
    """
    today = datetime.now(station.tz).date()
    start = datetime.combine(today, datetime.min.time(), station.tz)
    end = datetime.combine(today, datetime.max.time(), station.tz)
    obs_sid = OBS_STATION_OVERRIDES.get(station.id, station.id)
    r = requests.get(
        f"https://api.weather.gov/stations/{obs_sid}/observations",
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    out = []
    rejected = []  # para telemetría
    for f in r.json().get("features", []):
        p = f["properties"]
        has_raw = bool(p.get("rawMessage"))
        ts = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
        minute = ts.minute
        is_metar_slot = minute in (53, 54)
        accepted = has_raw or is_metar_slot
        tv = p["temperature"]["value"]
        if not accepted:
            if tv is not None:
                rejected.append({"time": ts, "temp_f": c_to_f(tv),
                                 "minute": minute, "has_raw": has_raw})
            continue
        pv = (p.get("barometricPressure") or {}).get("value")
        out.append({
            "time": ts,
            "temp_f": c_to_f(tv) if tv is not None else None,
            "pressure_inhg": pa_to_inhg(pv) if pv is not None else None,
        })
    try:
        _log_obs_rejects(station.id, out, rejected)
    except Exception:
        pass
    return out


def _interp_ref_at(accepted: list, rt: datetime,
                   window_min: float = 60.0) -> tuple:
    """Interpolación lineal en `rt` entre los METARs aceptados más cercanos
    dentro de ±window_min. Fable Round 2 (2026-07-10): la guarda de fase 2
    debe comparar contra la interpolada, no contra "nearest accepted" — un
    91.9 a las 14:53 entre 91.0 (13:53) y 91.0 (15:53) tiene delta ~0.9 vs
    interpolada, no vs vecino: legítimo y chico.

    Returns (interp_temp_f, kind) donde kind ∈ {linear, before, after, none}."""
    before = None  # (time, temp) — más cercano ANTES de rt
    after = None   # (time, temp) — más cercano DESPUÉS de rt
    for a in accepted:
        at = a.get("time")
        atemp = a.get("temp_f")
        if at is None or atemp is None:
            continue
        secs = (at - rt).total_seconds()
        if abs(secs) / 60.0 > window_min:
            continue
        if secs < 0:  # accepted es anterior
            if before is None or (rt - before[0]).total_seconds() > -secs:
                before = (at, atemp)
        elif secs > 0:  # accepted es posterior
            if after is None or (after[0] - rt).total_seconds() > secs:
                after = (at, atemp)
    if before and after:
        b_t, b_temp = before
        a_t, a_temp = after
        span = (a_t - b_t).total_seconds()
        if span > 0:
            frac = (rt - b_t).total_seconds() / span
            return (b_temp + frac * (a_temp - b_temp), "linear")
        return (b_temp, "linear")
    if before:
        return (before[1], "before")
    if after:
        return (after[1], "after")
    return (None, "none")


def _log_obs_rejects(station_id: str, accepted: list, rejected: list) -> None:
    """Persistir lecturas rechazadas para backtestear el umbral de guarda C.
    Cero-op si no hay rechazos. Idempotente por (station_id, ts) — no duplica
    entre polls."""
    if not rejected:
        return
    try:
        import calibration as _cal
    except Exception:
        return
    accepted_ts = sorted(a["time"] for a in accepted if a.get("temp_f") is not None)
    for r in rejected:
        rt = r["time"]
        dist_min = None
        if accepted_ts:
            best = min(abs((rt - at).total_seconds()) for at in accepted_ts)
            dist_min = round(best / 60.0, 1)
        interp_ref, kind = _interp_ref_at(accepted, rt)
        delta = (r["temp_f"] - interp_ref) if (interp_ref is not None and
                                               r["temp_f"] is not None) else None
        _cal.record_obs_reject(
            station_id=station_id,
            ts=rt,
            temp_f=r["temp_f"],
            minute=r["minute"],
            has_rawmsg=r["has_raw"],
            dist_to_accepted_min=dist_min,
            interp_ref_f=interp_ref,
            delta_vs_interp_f=delta,
            interp_kind=kind,
        )


def pressure_trend_3h(obs_list, current_pressure_inhg):
    """Compute delta pressure (inHg) over last ~3h vs current."""
    if current_pressure_inhg is None or not obs_list:
        return None
    now = datetime.now(timezone.utc)
    target = now.timestamp() - 3 * 3600
    # find the obs closest to 3h ago with a pressure reading
    best = None
    best_diff = float("inf")
    for o in obs_list:
        if o["pressure_inhg"] is None:
            continue
        diff = abs(o["time"].timestamp() - target)
        if diff < best_diff:
            best_diff = diff
            best = o
    if best is None or best_diff > 5400:  # must be within ±90min
        return None
    return current_pressure_inhg - best["pressure_inhg"]


def fetch_ensemble(station: Station):
    r = requests.get("https://ensemble-api.open-meteo.com/v1/ensemble",
                     params={"latitude": station.lat,
                             "longitude": station.lon,
                             "hourly": "temperature_2m",
                             "models": "gfs_seamless",
                             "temperature_unit": "fahrenheit",
                             "timezone": station.tz.key,
                             "forecast_days": 3},
                     timeout=20)
    try:
        import om_quota
        om_quota.count_call("ensemble")
    except Exception:
        pass
    r.raise_for_status()
    h = r.json()["hourly"]
    times = h["time"]
    members = {"control": h["temperature_2m"]}
    for k, v in h.items():
        if k.startswith("temperature_2m_member"):
            members[k] = v
    return times, members


def fetch_precip_ensemble(station: Station):
    """Ensemble GFS con precipitation (mm) y snowfall (cm) por miembro.
    Retorna (times, precip_members, snow_members) donde cada dict mapea
    nombre-de-miembro → lista de valores horarios."""
    r = requests.get("https://ensemble-api.open-meteo.com/v1/ensemble",
                     params={"latitude": station.lat,
                             "longitude": station.lon,
                             "hourly": "precipitation,snowfall",
                             "models": "gfs_seamless",
                             "timezone": station.tz.key,
                             "forecast_days": 3},
                     timeout=20)
    try:
        import om_quota
        om_quota.count_call("precip_ensemble")
    except Exception:
        pass
    r.raise_for_status()
    h = r.json()["hourly"]
    times = h["time"]
    precip = {"control": h.get("precipitation") or []}
    snow = {"control": h.get("snowfall") or []}
    for k, v in h.items():
        if k.startswith("precipitation_member"):
            precip[k] = v
        elif k.startswith("snowfall_member"):
            snow[k] = v
    return times, precip, snow


# ───────────────────── TTL cache wrappers ─────────────────────
# Cache en memoria con TTL. Los polls del servidor corren cada 10 min y
# las rutas web pueden pegarle al mismo fetcher varias veces por request
# (p.ej. /cross con 6 estaciones). Cacheamos por station_id.

import time as _time

_FETCH_CACHE: dict = {}

# TTLs por endpoint. El audit 2026-06-22 mostró que el TTL global de 30 min
# era el culprit del límite diario de Open-Meteo (10k req/día free tier).
# Cada función ahora declara su TTL según refresh real del upstream:
#   - Open-Meteo ensemble: GFS runs 4×/día (00/06/12/18 UTC) → 60 min sobra
#   - NWS METAR: hourly oficialmente → 10 min mejora frescura sin coste (NWS
#     no tiene cuota global pública para nuestros volúmenes)
TTL_ENSEMBLE = 3600   # 60 min
TTL_PRECIP_ENSEMBLE = 3600
TTL_NWS_OBS = 600     # 10 min · NWS no cuenta para cuota Open-Meteo


def _cached(ttl: int):
    """Decorator con TTL custom + stale-on-error si upstream falla."""
    def decorator(fn):
        def wrapped(station: Station, *a, **kw):
            key = (fn.__name__, station.id)
            now = _time.time()
            hit = _FETCH_CACHE.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
            try:
                val = fn(station, *a, **kw)
            except Exception as e:
                if hit is not None:
                    age = int(now - hit[0])
                    print(f"fetch {fn.__name__}({station.id}) failed ({e}); serving stale ({age}s old)")
                    return hit[1]
                raise
            _FETCH_CACHE[key] = (now, val)
            return val
        wrapped.__name__ = fn.__name__
        wrapped.__wrapped__ = fn
        return wrapped
    return decorator


def fetch_past_precip(station: Station, hours: int = 8) -> list:
    """Hourly observed+near-real precipitation for the past `hours` (inches).
    Returns list of (datetime_local, inches). Uses Open-Meteo forecast
    endpoint (single deterministic, cheaper than ensemble) with past_hours."""
    r = requests.get("https://api.open-meteo.com/v1/forecast",
                     params={"latitude": station.lat,
                             "longitude": station.lon,
                             "hourly": "precipitation",
                             "past_hours": hours,
                             "forecast_hours": 1,
                             "timezone": station.tz.key,
                             "precipitation_unit": "inch"},
                     timeout=20)
    try:
        import om_quota
        om_quota.count_call("past_precip")
    except Exception:
        pass
    r.raise_for_status()
    h = r.json().get("hourly") or {}
    times = h.get("time") or []
    precip = h.get("precipitation") or []
    out = []
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        v = precip[i] if i < len(precip) else None
        out.append((ts, float(v) if v is not None else 0.0))
    return out


def precip_windows_from_past(past: list, now_local: datetime) -> dict:
    """Sum inches over the last 1/2/4/8h ending at now_local.
    `past` is list of (datetime_local, inches) as returned by fetch_past_precip."""
    now_ts = now_local.timestamp()
    windows = {}
    for wh in (1, 2, 4, 8):
        cutoff = now_ts - wh * 3600
        s = sum(v for (ts, v) in past
                if cutoff <= ts.timestamp() < now_ts)
        windows[wh] = round(s, 2)
    return windows


fetch_current = _cached(TTL_NWS_OBS)(fetch_current)
fetch_today_obs = _cached(TTL_NWS_OBS)(fetch_today_obs)
fetch_precip_ensemble = _cached(TTL_PRECIP_ENSEMBLE)(fetch_precip_ensemble)
fetch_ensemble = _cached(TTL_ENSEMBLE)(fetch_ensemble)
fetch_past_precip = _cached(1200)(fetch_past_precip)  # 20 min TTL


def compute_min_forecast(station: Station,
                         target_date: date | None = None) -> dict | None:
    """F8 fase 0: quantiles del min diario a partir del ensemble ya cacheado.

    Sin bayesian reweight, sin blend externo, sin bias tracker. Es un snapshot
    crudo para poder backtestear más adelante (¿el ensemble raw predice min
    con qué skill?). Reusa `fetch_ensemble` que ya vive en cache TTL 60min,
    así que llamarlo cada 20 min por estación es gratis en red.

    Returns dict con p10/p50/p90/n_members o None si no hay datos suficientes.
    """
    times, members = fetch_ensemble(station)
    if target_date is None:
        target_date = datetime.now(station.tz).date()
    # índices del día target en el ensemble
    idx = []
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        if ts.date() == target_date:
            idx.append(i)
    if not idx:
        return None
    mins = []
    for k, temps in members.items():
        vals = [temps[i] for i in idx if temps[i] is not None]
        if vals:
            mins.append(min(vals))
    if not mins:
        return None
    mins.sort()
    n = len(mins)
    p10 = mins[max(0, int(0.10 * (n - 1)))]
    p50 = mins[n // 2]
    p90 = mins[min(n - 1, int(0.90 * (n - 1)))]
    return {"p10": p10, "p50": p50, "p90": p90, "n_members": n}


def invalidate_fetch_cache(station_id: str | None = None) -> None:
    """Útil si quieres forzar un fetch fresco (p.ej. botón de refresh)."""
    if station_id is None:
        _FETCH_CACHE.clear()
        return
    for key in list(_FETCH_CACHE.keys()):
        if key[1] == station_id:
            del _FETCH_CACHE[key]


def invalidate_obs_cache(station_id: str) -> None:
    """Clear only METAR observation caches. Ensemble (expensive Open-Meteo call)
    stays cached. Used by adaptive polling so peak-window polls reuse the
    ensemble but pull fresh current-temp/today-obs each cycle."""
    for fn_name in ("fetch_current", "fetch_today_obs"):
        _FETCH_CACHE.pop((fn_name, station_id), None)


# ───────────────────── precipitation summary ─────────────────────
# Thresholds (mm for precip, cm for snow):
PRECIP_ANY = 0.1     # ~trace
PRECIP_NOTABLE = 2.5  # ~0.1 inch
PRECIP_HEAVY = 10.0   # ~0.4 inch
SNOW_ANY = 0.1
SNOW_NOTABLE = 2.5    # ~1 inch


def build_precip_summary(station: Station, day_offset: int = 0) -> dict:
    """Aggregates ensemble precipitation/snowfall members for target day.

    Returns dict con: target, n_members, precip_daily (list per member mm),
    snow_daily (list per member cm), p(any), p(notable), p(heavy),
    expected_mm, p10/p50/p90, snow equivalents.
    """
    times, precip_members, snow_members = fetch_precip_ensemble(station)
    target = datetime.now(station.tz).date() + timedelta(days=day_offset)

    day_idx = []
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        if ts.date() == target:
            day_idx.append(i)

    precip_daily = []
    for k, vals in precip_members.items():
        if not vals:
            continue
        total = sum(vals[i] for i in day_idx if vals[i] is not None)
        precip_daily.append(total)

    snow_daily = []
    for k, vals in snow_members.items():
        if not vals:
            continue
        total = sum(vals[i] for i in day_idx if vals[i] is not None)
        snow_daily.append(total)

    def _pct(lst, thr):
        if not lst:
            return None
        return sum(1 for v in lst if v > thr) / len(lst)

    def _percentiles(lst):
        if not lst:
            return (None, None, None)
        s = sorted(lst)
        n = len(s)
        return (s[int(n * 0.1)], s[n // 2], s[int(n * 0.9)])

    p10, p50, p90 = _percentiles(precip_daily)
    sp10, sp50, sp90 = _percentiles(snow_daily)

    return {
        "target": target,
        "day_offset": day_offset,
        "n_members": len(precip_daily),
        "precip_daily_mm": precip_daily,
        "snow_daily_cm": snow_daily,
        "p_any_precip": _pct(precip_daily, PRECIP_ANY),
        "p_notable_precip": _pct(precip_daily, PRECIP_NOTABLE),
        "p_heavy_precip": _pct(precip_daily, PRECIP_HEAVY),
        "expected_mm": sum(precip_daily) / len(precip_daily) if precip_daily else 0.0,
        "p10_mm": p10, "p50_mm": p50, "p90_mm": p90,
        "p_any_snow": _pct(snow_daily, SNOW_ANY),
        "p_notable_snow": _pct(snow_daily, SNOW_NOTABLE),
        "expected_snow_cm": sum(snow_daily) / len(snow_daily) if snow_daily else 0.0,
        "p10_snow": sp10, "p50_snow": sp50, "p90_snow": sp90,
    }


# ───────────────────── snapshot builder ─────────────────────

def build_snapshot(station: Station) -> Snapshot:
    current = fetch_current(station)
    obs_full = fetch_today_obs(station)  # list of dicts
    obs_today = [o["temp_f"] for o in obs_full if o["temp_f"] is not None]
    times, members = fetch_ensemble(station)

    now_local = datetime.now(station.tz)
    today = now_local.date()
    current_hour = now_local.replace(minute=0, second=0, microsecond=0)

    # indices of today's remaining hours in ensemble
    future_today_idx = []
    next_6h_idx = []
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        if ts < current_hour:
            continue
        if ts.date() == today:
            future_today_idx.append(i)
        if len(next_6h_idx) < 6:
            next_6h_idx.append((i, ts))

    max_obs = max(obs_today) if obs_today else (current["temp_f"] or -999)
    min_obs = min(obs_today) if obs_today else (current["temp_f"] or 999)

    # per-member simulated daily max: max(obs_so_far, remaining forecast).
    # Collect member keys + their raw maxes in parallel lists so we can
    # pair weights to members for Bayesian reweighting below.
    member_keys = list(members.keys())
    raw_maxes = []
    rising_count = 0
    for k in member_keys:
        temps = members[k]
        vals = [temps[i] for i in future_today_idx if temps[i] is not None]
        dmax = max([max_obs] + vals) if vals else max_obs
        raw_maxes.append(dmax)
        if vals and max(vals) > max_obs:
            rising_count += 1
    total_members = len(member_keys)
    prob_rising = rising_count / total_members if total_members else 0.0

    # ─── Bayesian reweighting from morning residuals ───
    # For each past hour today where we have both an observed temp and
    # ensemble forecasts, compute per-member squared residual. Members
    # that tracked reality better get higher weight. Per-hour σ: peak-window
    # obs use σ=1°F (highly diagnostic of daily max), distant hours use up
    # to σ=3°F so noisy dawn obs don't dominate.
    hour_obs_early = {}
    for o in obs_full:
        if o["temp_f"] is None:
            continue
        tl = o["time"].astimezone(station.tz)
        if tl.date() != today:
            continue
        hour_obs_early[tl.hour] = o["temp_f"]
    matched = []  # (member_idx → list of (forecast, obs))
    residual_hours = 0
    regime_break_hours: list[int] = []
    reweight_diagnostics: list[dict] = []
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        if ts.date() != today or ts > current_hour:
            continue
        obs_v = hour_obs_early.get(ts.hour)
        if obs_v is None:
            continue
        residual_hours += 1
        hour_forecasts: list[float] = []
        for mi, k in enumerate(member_keys):
            f = members[k][i]
            if f is None:
                continue
            hour_forecasts.append(f)
            while len(matched) <= mi:
                matched.append([])
            matched[mi].append((f, obs_v, ts.hour))
        # regime break: obs outside the ensemble's [p1, p99] at this hour.
        # If even the 1st/99th percentile of 500+ members misses reality,
        # the forecast isn't just imprecise — it's systematically wrong.
        out_of_range = False
        ens_p10 = ens_p50 = ens_p90 = None
        if hour_forecasts:
            hf = sorted(hour_forecasts)
            nh = len(hf)
            ens_p10 = hf[int(0.10 * (nh - 1))]
            ens_p50 = hf[nh // 2]
            ens_p90 = hf[int(0.90 * (nh - 1))]
            # Need enough members for the percentile to be meaningful. GFS
            # ensemble has ~31 members; use min/max as p1/p99 proxy there.
            # Require a 2°F margin beyond p1/p99 so we don't trigger on
            # systematic station-vs-grid bias (common in desert stations).
            if nh >= 20:
                p1 = hf[0] if nh < 50 else hf[max(0, int(0.01 * nh))]
                p99 = hf[-1] if nh < 50 else hf[min(nh - 1, int(0.99 * nh))]
                REGIME_MARGIN_F = 4.0
                if obs_v < p1 - REGIME_MARGIN_F or obs_v > p99 + REGIME_MARGIN_F:
                    regime_break_hours.append(ts.hour)
                    out_of_range = True
        reweight_diagnostics.append({
            "hour": ts.hour,
            "obs": obs_v,
            "p10": ens_p10, "p50": ens_p50, "p90": ens_p90,
            "sigma": sigma_for_hour(ts.hour, station.id),
            "n_members": len(hour_forecasts),
            "out_of_range": out_of_range,
        })
    # compute weights — SSE is standardized per-hour so σ(hour) defines the
    # noise scale of each observation. Peak-hour obs contribute more signal.
    if residual_hours >= 2 and matched and any(matched):
        sses = []
        for mi in range(total_members):
            if mi < len(matched) and matched[mi]:
                sse = sum(((f - o) / sigma_for_hour(h, station.id)) ** 2
                          for f, o, h in matched[mi])
                sses.append(sse)
            else:
                sses.append(0.0)  # no data → neutral
        # numerically stable softmax: subtract min SSE before exponent.
        # SSE is already σ-standardized, so the Gaussian log-likelihood
        # divisor is just 2 (not 2σ²).
        min_sse = min(sses)
        raw_w = [pow(2.718281828, -(s - min_sse) / 2.0)
                 for s in sses]
        z = sum(raw_w)
        weights = [w / z for w in raw_w] if z > 0 else [1.0 / total_members] * total_members
        eff_n = 1.0 / sum(w * w for w in weights) if any(weights) else float(total_members)
    else:
        weights = [1.0 / total_members] * total_members if total_members else []
        eff_n = float(total_members)

    # Resample to N_SAMPLES by proportional replication (deterministic).
    # Each member contributes round(N*w) copies to the working ensemble.
    N_SAMPLES = 500
    daily_maxes = []
    if total_members and weights:
        for m_val, w in zip(raw_maxes, weights):
            k = int(round(N_SAMPLES * w))
            if k > 0:
                daily_maxes.extend([m_val] * k)
        # if rounding left us empty (e.g. all weights tiny), fall back to raw
        if not daily_maxes:
            daily_maxes = list(raw_maxes)
    else:
        daily_maxes = list(raw_maxes)

    # Fable/Codex retro 2026-07-06 (P1 #3): seasonal offset por estación con
    # sesgo frío GFS persistente (KLAS -1.70, KPHX -1.55, KBOS -0.99). El
    # bias_tracker EWMA rebota y no captura el nivel sostenido; este offset
    # se aplica ANTES para que el tracker vea residual limpio. La compensación
    # en compute_bias sobre samples pre-fecha evita double-correction.
    try:
        import bias_tracker as _bt_off
        _seasonal = _bt_off.SEASONAL_OFFSET_F.get(station.id, 0.0)
        if _seasonal != 0.0 and daily_maxes:
            daily_maxes = [v - _seasonal for v in daily_maxes]  # _seasonal<0 → suma
    except Exception:
        pass

    # Apply per-station rolling bias correction (subtract historical bias from
    # the prior). Conditional by climatology regime when available — KLGA
    # is bimodal (cold on warm days, warm on cold days). Falls back to global
    # bias if same-regime bucket has insufficient samples.
    #
    # Pre-fetch ext_diff (pre-bias, pre-shift) para atenuar el sign-nudge:
    # los externos son evidencia de hoy y deben pesar más que la racha de ayer
    # cuando chocan (Fable round 3).
    bias_correction_f = 0.0
    bias_info = None
    pred_pre_bias = None
    pre_ext_diff = None
    pre_ext_mm = None
    try:
        import external_models as _ext_pre
        pre_ext_mm = _ext_pre.fetch_multi_model_max(station)
        if pre_ext_mm is not None and pre_ext_mm.median is not None and daily_maxes:
            _sm0 = sorted(daily_maxes)
            pred_pre_bias = _sm0[len(_sm0) // 2]
            pre_ext_diff = pred_pre_bias - pre_ext_mm.median
    except Exception:
        pre_ext_mm = None
    try:
        import bias_tracker as _bt
        today_pct = None
        pct_lookup = None
        if _climate_percentile is not None and daily_maxes:
            sm = sorted(daily_maxes)
            pred_med = sm[len(sm) // 2]
            try:
                cs = _climate_percentile(station, today, pred_med)
                if cs is not None:
                    today_pct = cs.percentile
            except Exception:
                today_pct = None

            def pct_lookup(date_str, pred_f):
                try:
                    from datetime import date as _d
                    y, m, dd = (int(x) for x in date_str.split("-"))
                    cs2 = _climate_percentile(station, _d(y, m, dd), pred_f)
                    return cs2.percentile if cs2 is not None else None
                except Exception:
                    return None

        if today_pct is not None and pct_lookup is not None:
            bias_info = _bt.compute_bias_conditional(
                station.id, predicted_max_f=pred_med,
                today_percentile=today_pct,
                percentile_for_pred=pct_lookup,
                today=today,
                ext_diff=pre_ext_diff,
            )
        else:
            bias_info = _bt.compute_bias(station.id, today,
                                         ext_diff=pre_ext_diff)
            bias_info["mode"] = "global"
        if bias_info["applied"]:
            bias_correction_f = bias_info["bias"]
            daily_maxes = [v - bias_correction_f for v in daily_maxes]
    except Exception:
        bias_info = None

    # ─── Anclaje externo en el posterior (post-bias) ───
    # Mueve daily_maxes hacia la mediana de los 6 modelos externos cuando
    # |pred - ext_med| ≥ POSTERIOR_SHIFT_THRESHOLD_F. Corrige TODO lo derivado
    # del posterior (pred, bin modal, climatología, our_p_for_bin) sin el lag
    # de 1+ días del bias tracker. El blend por-bin de blend_with_external
    # sigue activo pero se auto-atenúa: _anchor_context recalcula ext_diff
    # sobre la distribución ya desplazada. El gate direccional de bets debe
    # usar ext_shift_info["ext_diff_pre"] (señal de peligro original).
    ext_shift_f = 0.0
    ext_shift_info = None
    try:
        import external_models as _ext
        mm = pre_ext_mm if pre_ext_mm is not None else _ext.fetch_multi_model_max(station)
        if mm is not None and mm.median is not None and daily_maxes:
            _sm = sorted(daily_maxes)
            _pred_med = _sm[len(_sm) // 2]
            _ext_diff = _pred_med - mm.median
            _clim_pct = None
            if _climate_percentile is not None:
                try:
                    _cs = _climate_percentile(station, today, _pred_med)
                    _clim_pct = _cs.percentile if _cs is not None else None
                except Exception:
                    _clim_pct = None
            # P3 fix (Codex 2026-06-18): contar el sign-nudge contra el budget
            # externo. Si el nudge ya movió pred hacia externals por N°F, eso
            # equivale a un λ_implícito = N / |pre_ext_diff| que descontamos
            # del CAP disponible para el shift posterior y el blend por-bin.
            nudge_ext_used = 0.0
            if (bias_info is not None and bias_info.get("sign_nudge")
                    and pre_ext_diff is not None and abs(pre_ext_diff) > 1e-9):
                # bias_correction_f se RESTÓ de daily_maxes. Pred bajó si era +.
                # Movimiento hacia externals = bias_correction_f * pre_ext_diff > 0
                # (mismo signo: pred alto + ext_diff alto, restar baja pred → toward).
                if bias_correction_f * pre_ext_diff > 0:
                    nudge_ext_used = min(_ext.POSTERIOR_SHIFT_CAP,
                                         abs(bias_correction_f) / abs(pre_ext_diff))
            _lam = _ext.posterior_shift_weight(_ext_diff, mm.spread, _clim_pct,
                                               ext_used=nudge_ext_used)
            if _lam > 0.0:
                ext_shift_f = _lam * (mm.median - _pred_med)
                daily_maxes = [v + ext_shift_f for v in daily_maxes]
            ext_shift_info = {
                "ext_med": mm.median, "ext_spread": mm.spread,
                "ext_diff_pre": _ext_diff, "clim_pct": _clim_pct,
                "lambda": _lam, "shift_f": ext_shift_f,
                "pred_pre_bias": pred_pre_bias,
                "nudge_ext_used": nudge_ext_used,
            }
    except Exception:
        ext_shift_info = None

    if prob_rising >= 0.50:
        peak_status = "📈 subiendo"
    elif prob_rising >= 0.10:
        peak_status = "posible alza"
    elif prob_rising >= 0.01:
        peak_status = "🔒 pico probable"
    else:
        peak_status = "🔒 pico confirmado"

    # next 6h forecast distribution
    forecast = []
    for i, ts in next_6h_idx:
        vals = sorted([t[i] for t in members.values() if t[i] is not None])
        if vals:
            n = len(vals)
            forecast.append((ts, vals[n // 2], vals[int(n * 0.1)], vals[int(n * 0.9)]))

    # day chart — 24 hourly entries with observed (if any) and forecast band
    hour_obs = {}  # local hour -> last observed temp that hour
    for o in obs_full:
        if o["temp_f"] is None:
            continue
        t_local = o["time"].astimezone(station.tz)
        if t_local.date() != today:
            continue
        hour_obs[t_local.hour] = o["temp_f"]
    hour_fcst = {}
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        if ts.date() != today:
            continue
        vals = sorted([t[i] for t in members.values() if t[i] is not None])
        if vals:
            n = len(vals)
            hour_fcst[ts.hour] = (vals[n // 2], vals[int(n * 0.1)], vals[int(n * 0.9)])
    day_chart = []
    for h in range(24):
        obs = hour_obs.get(h)
        med, p10, p90 = hour_fcst.get(h, (None, None, None))
        day_chart.append((h, obs, med, p10, p90))

    snap = Snapshot(
        fetched_at=datetime.now(timezone.utc),
        station_local=now_local,
        current_temp_f=current["temp_f"],
        current_desc=current["desc"],
        current_obs_time=current["time"],
        today_min_obs=min_obs,
        today_max_obs=max_obs,
        obs_count=len(obs_today),
        ensemble_daily_maxes=daily_maxes,
        forecast_next_hours=forecast,
        prob_rising=prob_rising,
        peak_status=peak_status,
        dewpoint_f=current.get("dewpoint_f"),
        humidity_pct=current.get("humidity_pct"),
        heat_index_f=current.get("heat_index_f"),
        wind_chill_f=current.get("wind_chill_f"),
        wind_mph=current.get("wind_mph"),
        wind_gust_mph=current.get("wind_gust_mph"),
        wind_dir_deg=current.get("wind_dir_deg"),
        wind_dir_card=current.get("wind_dir_card"),
        pressure_inhg=current.get("pressure_inhg"),
        pressure_trend_3h=pressure_trend_3h(obs_full, current.get("pressure_inhg")),
        visibility_mi=current.get("visibility_mi"),
        day_chart=day_chart,
        ensemble_raw_maxes=raw_maxes,
        ensemble_weights=weights,
        ensemble_eff_n=eff_n,
        ensemble_residual_hours=residual_hours,
        regime_break_hours=regime_break_hours,
        reweight_diagnostics=reweight_diagnostics,
        bias_correction_f=bias_correction_f,
        bias_info=bias_info,
        ext_shift_f=ext_shift_f,
        ext_shift_info=ext_shift_info,
    )
    # climatology: compare expected max vs historical same-date-of-year
    if _climate_percentile is not None:
        if prob_rising < 0.10 and max_obs > -900:
            expected = max_obs
        else:
            sorted_m = sorted(daily_maxes)
            expected = sorted_m[len(sorted_m) // 2]
        try:
            snap.climatology = _climate_percentile(station, today, expected)
            snap.climatology_target_f = expected
        except Exception:
            pass
    return snap


# ───────────────────── probability logic ─────────────────────

def most_likely_max(dist):
    """Most probable single-degree (rounded) value. Used by CSV row."""
    rounded = [round(x) for x in dist]
    c = Counter(rounded)
    val, cnt = c.most_common(1)[0]
    return float(val), cnt / len(dist)


def movement_cents(a) -> int | None:
    """Cents moved since the previous snapshot, or None if history < 2 entries.

    Useful for Kalshi-style "↑+5¢ / ↓-3¢" indicators. Note: for the auto-
    suggested slot-3, the threshold itself may shift between polls, so this
    is most meaningful for user-set slots 1 and 2.
    """
    if len(a.history) < 2:
        return None
    prev = a.history[-2][1]
    curr = a.history[-1][1]
    return round((curr - prev) * 100)


def find_informative_bin(dist, trivial_cap=0.90):
    """Find the narrowest bin whose probability is meaningful (below trivial_cap).

    Tries bin widths 1.0 → 0.5 → 0.2 → 0.1 until the mode's probability
    drops under trivial_cap. If none do (e.g. pico totalmente confirmado),
    returns the tightest bin's value.

    Returns (center_value, bin_width, probability).
    """
    for w in (1.0, 0.5, 0.2, 0.1):
        bins = Counter()
        for v in dist:
            key = round(v / w) * w
            bins[round(key, 2)] += 1
        val, cnt = bins.most_common(1)[0]
        p = cnt / len(dist)
        if p < trivial_cap:
            return val, w, p
    return val, w, p


def parse_expr(s: str):
    """Parse assertion. Returns (op, threshold, bin_half, display).

    Soporta:
      >89F, >=89F, <75F, <=75F       → comparación
      =59, =59F                       → igual a 59°F (bin [58.5, 59.5])
      59-60, 59..60, 59 a 60          → rango inclusivo (bin [58.5, 60.5])
      entre 59 y 60                   → rango inclusivo
    """
    raw = s.strip()
    s = raw.upper().replace("F", "")
    # "entre N y M"
    m = re.match(r"^ENTRE\s+(-?\d+(?:\.\d+)?)\s+Y\s+(-?\d+(?:\.\d+)?)$", s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        center = (lo + hi) / 2
        half = (hi - lo) / 2 + 0.5
        return "~", center, half, f"={lo:g}–{hi:g}F"
    # "N-M" o "N..M" o "N a M"
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(?:-|\.\.|\sA\s)\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        center = (lo + hi) / 2
        half = (hi - lo) / 2 + 0.5
        return "~", center, half, f"={lo:g}–{hi:g}F"
    # "=N"
    m = re.match(r"^=\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        thr = float(m.group(1))
        return "~", thr, 0.5, f"={thr:g}F"
    # comparaciones
    m = re.match(r"^(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        op, thr = m.group(1), float(m.group(2))
        return op, thr, 0.5, f"{op}{thr:g}F"
    raise ValueError(f"Formato inválido: {s!r}. Usa >89F, =59F, 59-60, entre 59 y 60.")


# ───────────────────── display ─────────────────────

def sparkline(values, width=24):
    if not values:
        return ""
    vals = values[-width:]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return SPARKS[3] * len(vals)
    return "".join(SPARKS[min(len(SPARKS) - 1,
                              int((v - lo) / (hi - lo) * (len(SPARKS) - 1)))]
                   for v in vals)


def render(snap: Snapshot, station: Station, assertions: dict,
           auto_mode: str):
    tz_pr_time = snap.station_local.astimezone(PR_TZ)
    head = Table.grid(expand=True)
    head.add_column(justify="left")
    head.add_column(justify="right")
    head.add_row(
        Text(f"{station.id} — {station.name}", style="bold cyan"),
        Text(f"local {snap.station_local.strftime('%H:%M %Z')}  "
             f"PR {tz_pr_time.strftime('%H:%M')}",
             style="dim"),
    )

    obs_age = (datetime.now(timezone.utc) - snap.current_obs_time).total_seconds() / 60
    cur = Table.grid(padding=(0, 2))
    cur.add_column(style="bold")
    cur.add_column()
    cur.add_row("Temp actual", f"{snap.current_temp_f:.1f}°F  [dim]({snap.current_desc})[/]")
    cur.add_row("Última obs", f"{snap.current_obs_time.astimezone(station.tz).strftime('%H:%M')}  "
                              f"[dim]({obs_age:.0f} min atrás)[/]")
    if "confirmado" in snap.peak_status or "probable" in snap.peak_status:
        peak_style = "bold green"
    elif "alza" in snap.peak_status:
        peak_style = "yellow"
    else:
        peak_style = "cyan"
    cur.add_row("Hoy (obs)", f"min {snap.today_min_obs:.1f}°F  /  "
                             f"max {snap.today_max_obs:.1f}°F  "
                             f"[dim]({snap.obs_count} obs)[/]")
    cur.add_row("Pico", f"[{peak_style}]{snap.peak_status}[/]  "
                        f"[dim]P(sube más)={snap.prob_rising*100:.0f}%[/]")

    # forecast table
    fc = Table(title="Pronóstico próximas horas (ensemble GFS 31m)",
               title_style="bold", expand=False)
    fc.add_column("hora")
    fc.add_column("mediana", justify="right")
    fc.add_column("p10-p90", justify="right", style="dim")
    for ts, med, p10, p90 in snap.forecast_next_hours:
        fc.add_row(ts.strftime("%H:%M"),
                   f"{med:.0f}°F",
                   f"{p10:.0f}–{p90:.0f}°F")

    # distribution summary
    dist = sorted(snap.ensemble_daily_maxes)
    n = len(dist)
    dist_med = dist[n // 2]
    dist_p10, dist_p90 = dist[int(n * 0.1)], dist[int(n * 0.9)]
    mlv, mlp = most_likely_max(snap.ensemble_daily_maxes)
    dist_panel = Text.assemble(
        ("Max hoy (ensemble):\n", "bold"),
        f"  mediana {dist_med:.0f}°F   ",
        f"p10-p90 {dist_p10:.0f}–{dist_p90:.0f}°F\n",
        f"  valor más probable: ",
        (f"{mlv:.0f}°F", "bold yellow"),
        f"  (P={mlp*100:.0f}%)",
    )

    # assertions
    ass_tbl = Table(title=f"Aserciones  [dim](auto-sugerida: modo {auto_mode})[/]",
                    title_style="bold", expand=True)
    ass_tbl.add_column("#", width=3)
    ass_tbl.add_column("aserción")
    ass_tbl.add_column("P", justify="right")
    ass_tbl.add_column("estado")
    ass_tbl.add_column("evolución", style="dim")
    for slot in (1, 2, 3):
        a = assertions.get(slot)
        if a is None:
            ass_tbl.add_row(str(slot), "[dim]—[/]", "", "", "")
            continue
        label = a.expr + ("  [dim](auto)[/]" if a.auto else "")
        prob, status = eval_assertion(a, snap)
        a.history.append((snap.fetched_at, prob))
        spark = sparkline([p for _, p in a.history])
        style = "green" if status == "LIVE" and prob >= 0.5 else \
                "red" if status == "FALLIDA ✗" else \
                "bold green" if status == "RESUELTA ✓" else "yellow"
        ass_tbl.add_row(str(slot), label, f"[{style}]{prob*100:5.1f}%[/]",
                        status, spark)

    top = Columns([Panel(cur, title="Observación", border_style="cyan"),
                   Panel(fc, border_style="blue"),
                   Panel(dist_panel, title="Distribución max", border_style="magenta")],
                  equal=False, expand=False)

    console.print()
    console.rule(f"[bold]{station.id}[/] — {snap.station_local.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    console.print(head)
    console.print(top)
    console.print(ass_tbl)


# ───────────────────── CSV log ─────────────────────

def log_snapshot(snap: Snapshot, station: Station, assertions: dict):
    path = LOG_DIR / f"{station.id}_{snap.station_local.date()}.csv"
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["fetched_utc", "local_time", "current_f",
                        "today_min_f", "today_max_f",
                        "ens_max_median", "ens_max_p10", "ens_max_p90",
                        "most_likely_max", "most_likely_p",
                        "prob_rising", "peak_status",
                        "a1_expr", "a1_p", "a1_status",
                        "a2_expr", "a2_p", "a2_status",
                        "a3_expr", "a3_p", "a3_status"])
        dist = sorted(snap.ensemble_daily_maxes)
        n = len(dist)
        mlv, mlp = most_likely_max(snap.ensemble_daily_maxes)
        def fmt(v, spec):
            return format(v, spec) if v is not None else ""
        row = [snap.fetched_at.isoformat(),
               snap.station_local.isoformat(),
               fmt(snap.current_temp_f, ".2f"),
               fmt(snap.today_min_obs, ".2f"),
               fmt(snap.today_max_obs, ".2f"),
               f"{dist[n//2]:.2f}",
               f"{dist[int(n*0.1)]:.2f}",
               f"{dist[int(n*0.9)]:.2f}",
               f"{mlv:.0f}", f"{mlp:.3f}",
               f"{snap.prob_rising:.3f}", snap.peak_status]
        for slot in (1, 2, 3):
            a = assertions.get(slot)
            if a is None:
                row += ["", "", ""]
            else:
                p, s = eval_assertion(a, snap)
                row += [a.expr, f"{p:.3f}", s]
                if _calibration is not None:
                    try:
                        _calibration.record(station.id,
                                            snap.station_local.date(),
                                            slot, a, p, snap.fetched_at)
                    except Exception:
                        pass
        w.writerow(row)


# ───────────────────── state & loop ─────────────────────

class State:
    def __init__(self, station: Station):
        self.station = station
        self.assertions = {}  # slot -> Assertion; slot 3 = auto
        self.auto_mode = "floating"  # or "locked"
        self.last_snapshot = None
        self.prev_dist_med = None  # ensemble median from the prior snapshot
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def set_station(self, new_station: Station):
        with self.lock:
            self.station = new_station
            self.assertions = {}  # reset per user spec
            self.last_snapshot = None
            self.prev_dist_med = None


def refresh_auto(state: State, snap: Snapshot):
    """Update slot-3 auto-suggested assertion based on mode.

    Finds the narrowest bin with P<90% so the assertion stays informative
    even when the peak is confirmed and coarse bins give 100%. If the
    ensemble is tightly clustered (spread < 0.5°F), uses a fixed-target
    assertion with bin_half=0.5°F — narrower bins (e.g. 0.1°F) chronically
    fail outcome even when the prediction is within 0.4°F of the real max,
    because GFS member spread doesn't capture subgrid uncertainty.
    """
    current = state.assertions.get(3)
    if state.auto_mode == "locked" and current is not None and current.locked:
        return  # keep frozen threshold and half-width
    dist = snap.ensemble_daily_maxes
    spread = max(dist) - min(dist)
    hist = current.history if current else []
    if spread < 0.5:
        mean_v = sum(dist) / len(dist)
        a = Assertion(expr=f"final={mean_v:.1f}F±0.5", op="~", threshold=mean_v,
                      bin_half=0.5, auto=True,
                      locked=(state.auto_mode == "locked"), history=hist)
    else:
        val, width, _ = find_informative_bin(dist)
        half = width / 2
        expr = f"~{val:.0f}F±{half:.1f}" if width >= 1.0 else f"~{val:.1f}F±{half:.2f}"
        a = Assertion(expr=expr, op="~", threshold=val, bin_half=half, auto=True,
                      locked=(state.auto_mode == "locked"), history=hist)
    state.assertions[3] = a


def eval_assertion(a: Assertion, snap: Snapshot):
    """Return (probability, status). Overrides the earlier def to handle ~ op."""
    max_obs = snap.today_max_obs
    if a.op == "~":
        # bin probability: member max falls in [thr-bin_half, thr+bin_half]
        lo, hi = a.threshold - a.bin_half, a.threshold + a.bin_half
        cnt = sum(1 for m in snap.ensemble_daily_maxes if lo <= m <= hi)
        return cnt / len(snap.ensemble_daily_maxes), "LIVE"
    if a.op == ">" and max_obs > a.threshold:
        return 1.0, "RESUELTA ✓"
    if a.op == ">=" and max_obs >= a.threshold:
        return 1.0, "RESUELTA ✓"
    dist = snap.ensemble_daily_maxes
    if a.op == ">":
        cnt = sum(1 for m in dist if m > a.threshold)
    elif a.op == ">=":
        cnt = sum(1 for m in dist if m >= a.threshold)
    elif a.op == "<":
        if max_obs >= a.threshold:
            return 0.0, "FALLIDA ✗"
        cnt = sum(1 for m in dist if m < a.threshold)
    elif a.op == "<=":
        if max_obs > a.threshold:
            return 0.0, "FALLIDA ✗"
        cnt = sum(1 for m in dist if m <= a.threshold)
    else:
        return 0.0, "OP?"
    return cnt / len(dist), "LIVE"


def record_kalshi(snap: Snapshot, station: Station) -> None:
    """Fetch current Kalshi bid/ask for this station's daily high market and
    store alongside our ensemble-derived per-bin probability. No-op if the
    station has no associated Kalshi series or module unavailable."""
    if _kalshi is None:
        return
    if _kalshi.series_for(station.id) is None:
        return
    try:
        bins = _kalshi.fetch_bins(station.id, snap.station_local.date())
        if not bins:
            return
        our_p_final_per_bin = _compute_final_our_p_per_bin(station.id, snap, bins)
        _kalshi.record(station.id, snap.station_local.date(), bins,
                       snap.ensemble_daily_maxes, snap.fetched_at,
                       our_p_final_per_bin=our_p_final_per_bin)
    except Exception as e:
        console.print(f"[yellow]kalshi fetch error:[/] {e}")


def _compute_final_our_p_per_bin(station_id: str, snap: Snapshot,
                                 bins: list) -> list:
    """Per-bin our_p después de isotonic + blend_with_external. Lo que el
    usuario ve en /edge y lo que Brier histórico debe leer."""
    out: list = [None] * len(bins)
    maxes = snap.ensemble_daily_maxes
    if not maxes:
        return out
    try:
        import isotonic as _iso
    except Exception:
        _iso = None
    try:
        import external_models as _em
    except Exception:
        _em = None
    cal = None
    if _iso is not None:
        try:
            cal = _iso.get(station_id)
            if cal is not None and (cal.n_fit < _iso.MIN_N
                                    or cal.n_days < _iso.MIN_DAYS):
                cal = None
        except Exception:
            cal = None
    ext_med = ext_spread = ext_diff = None
    lam = 0.0
    info = getattr(snap, "ext_shift_info", None)
    nudge_ext_used = 0.0
    if info:
        ext_med = info.get("ext_med")
        ext_spread = info.get("ext_spread")
        lam = float(info.get("lambda") or 0.0)
        nudge_ext_used = float(info.get("nudge_ext_used") or 0.0)
        if ext_med is not None:
            sm = sorted(maxes)
            pred_med = sm[len(sm) // 2]
            ext_diff = pred_med - ext_med
    for i, b in enumerate(bins):
        p = _kalshi.our_p_for_bin(maxes, b.bin_lo, b.bin_hi)
        if cal is not None and _iso is not None:
            p = _iso.apply(cal, p)
        if _em is not None and ext_med is not None and ext_spread is not None:
            p, _w = _em.blend_with_external(
                p, ext_med, ext_spread, b.bin_lo, b.bin_hi, ext_diff, lam,
                ext_used=nudge_ext_used)
        out[i] = p
    return out


def poll_once(state: State):
    try:
        snap = build_snapshot(state.station)
    except Exception as e:
        console.print(f"[red]Error fetching:[/] {e}")
        return
    with state.lock:
        state.last_snapshot = snap
        refresh_auto(state, snap)
        render(snap, state.station, state.assertions, state.auto_mode)
        log_snapshot(snap, state.station, state.assertions)
    record_kalshi(snap, state.station)


def poll_loop(state: State):
    last_settle_day = None
    while not state.stop.is_set():
        poll_once(state)
        # once per calendar day, try to settle yesterday's pending snapshots
        if _calibration is not None:
            today = datetime.now(state.station.tz).date()
            if last_settle_day != today:
                try:
                    _calibration.settle_pending(state.station)
                    last_settle_day = today
                except Exception:
                    pass
        state.stop.wait(POLL_SEC)


# ───────────────────── commands ─────────────────────

HELP = """
Comandos:
  show                  → forzar update ahora
  set <1|2> <expr>      → aserción (ej. set 1 >89F)
  clear <1|2|3>         → borrar aserción
  mode floating|locked  → modo auto-sugerida (slot 3)
  station <ID>          → cambiar estación (resetea aserciones)
  history [1|2|3]       → ver histórico completo de una aserción
  calibration [all]     → diagrama de confiabilidad (default: esta estación)
  help                  → esta ayuda
  quit / q              → salir
"""


def render_calibration(station_id: str | None):
    """Print a reliability diagram + Brier score to the console."""
    if _calibration is None:
        console.print("[red]calibration module no disponible[/]")
        return
    rep = _calibration.reliability(station_id)
    scope = station_id if station_id else "todas las estaciones"
    if rep.settled_n == 0:
        console.print(f"[yellow]Sin datos resueltos aún para {scope}.[/] "
                      f"(Snapshots totales: {rep.total_n}. "
                      "Se resuelven el día siguiente vía archive API.)")
        return
    t = Table(title=f"Reliability — {scope}",
              caption=f"n resueltos={rep.settled_n}  "
                      f"Brier={rep.brier:.4f} (0=perfecto, 0.25=al azar)",
              expand=False)
    t.add_column("bucket P", justify="right")
    t.add_column("n", justify="right")
    t.add_column("pred medio", justify="right")
    t.add_column("hit rate", justify="right")
    t.add_column("bar", justify="left")
    for b in rep.buckets:
        if b.n == 0:
            continue
        # bar shows hit rate vs diagonal; filled block = hit rate, | = expected
        pos_hit = int(round(b.hit_rate * 20))
        pos_exp = int(round(b.mean_pred * 20))
        bar = ["·"] * 21
        bar[pos_exp] = "|"
        mark = "█" if b.hit_rate >= b.mean_pred else "▓"
        bar[pos_hit] = mark
        t.add_row(f"{b.low:.1f}-{b.high:.1f}",
                  str(b.n),
                  f"{b.mean_pred*100:5.1f}%",
                  f"{b.hit_rate*100:5.1f}%",
                  "".join(bar))
    console.print(t)
    console.print("[dim]bar: | = prob predicha, █/▓ = hit rate observado[/]")


def cmd_loop(state: State):
    console.print("[dim]Tipea 'help' para comandos. Primer update en progreso...[/]")
    while True:
        try:
            line = input("» ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nSaliendo.")
            state.stop.set()
            return
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd in ("q", "quit", "exit"):
                state.stop.set()
                return
            elif cmd == "help":
                console.print(HELP)
            elif cmd == "show":
                poll_once(state)
            elif cmd == "set":
                slot = int(parts[1])
                if slot not in (1, 2):
                    console.print("[red]Slot debe ser 1 o 2 (slot 3 es auto).[/]")
                    continue
                op, thr, half, expr = parse_expr(" ".join(parts[2:]))
                prev = state.assertions.get(slot)
                with state.lock:
                    state.assertions[slot] = Assertion(
                        expr=expr, op=op, threshold=thr, bin_half=half,
                        history=prev.history if prev else [])
                console.print(f"[green]✓ Aserción {slot}: {expr}[/]")
                if state.last_snapshot:
                    poll_once(state)  # immediate eval
            elif cmd == "clear":
                slot = int(parts[1])
                with state.lock:
                    if slot == 3:
                        console.print("[yellow]Slot 3 es auto; usa 'mode' para cambiar.[/]")
                    elif slot in state.assertions:
                        del state.assertions[slot]
                        console.print(f"[green]✓ Borrada slot {slot}[/]")
            elif cmd == "mode":
                m = parts[1].lower()
                if m not in ("floating", "locked"):
                    console.print("[red]Modo: floating o locked[/]")
                    continue
                state.auto_mode = m
                with state.lock:
                    if state.last_snapshot:
                        refresh_auto(state, state.last_snapshot)
                console.print(f"[green]✓ Modo auto: {m}[/]")
            elif cmd == "station":
                sid = parts[1].upper()
                try:
                    new = fetch_station(sid)
                except Exception as e:
                    console.print(f"[red]Estación no encontrada: {e}[/]")
                    continue
                state.set_station(new)
                console.print(f"[green]✓ Estación: {new.id} — {new.name}[/]")
                poll_once(state)
            elif cmd == "history":
                slot = int(parts[1]) if len(parts) > 1 else None
                slots = [slot] if slot else [1, 2, 3]
                for s in slots:
                    a = state.assertions.get(s)
                    if a is None:
                        continue
                    console.print(f"\n[bold]Aserción {s}: {a.expr}[/]")
                    for ts, p in a.history[-20:]:
                        local = ts.astimezone(state.station.tz)
                        console.print(f"  {local.strftime('%H:%M')}  {p*100:5.1f}%")
            elif cmd == "calibration":
                scope = None if len(parts) > 1 and parts[1].lower() == "all" \
                    else state.station.id
                render_calibration(scope)
            else:
                console.print(f"[yellow]Comando desconocido: {cmd}[/]  (help)")
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")


# ───────────────────── main ─────────────────────

def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "KPHX"
    console.print(f"[bold]Weather Predictor[/] — cargando estación [cyan]{sid}[/]...")
    try:
        station = fetch_station(sid)
    except Exception as e:
        console.print(f"[red]No se pudo cargar {sid}:[/] {e}")
        sys.exit(1)
    console.print(f"[green]✓[/] {station.name}  "
                  f"[dim]({station.lat:.4f}, {station.lon:.4f}, {station.tz.key})[/]")

    state = State(station)
    t = threading.Thread(target=poll_loop, args=(state,), daemon=True)
    t.start()
    try:
        cmd_loop(state)
    finally:
        state.stop.set()


if __name__ == "__main__":
    main()
