"""Predicciones externas para sanity-check: NWS narrative del día +
máximas diarias por modelo (GFS/ECMWF/ICON/Météo-France) via Open-Meteo.

Sirve para detectar si vamos solos o en manada con los grandes modelos.
Si todos dicen 78°F y nosotros 82°F, probablemente algo está roto.

Cache en memoria: NWS narrative cada 1h (texto cambia ~cada 6h), modelos
cada 30 min (los runs son 6-hourly).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

import requests

# Anchor parameters: cuando el modelo discrepa fuerte de la mediana externa,
# blendear nuestra p_bin con una Gaussiana centrada en la mediana.
# Justificación 2026-05-26: KLAS cold-side perdió 5 veces seguidas con
# ext_diff típico ≤-2°F; el bias rolling no captura regime shifts agudos.
ANCHOR_SIGMA_FLOOR_F = 1.5     # piso de incertidumbre externa (°F)
ANCHOR_SIGMA_FROM_RANGE = 2.5  # spread es max-min ≈ 2.5σ para n=6 modelos
ANCHOR_WEIGHT_CAP = 0.50       # peso máximo en la externa (nunca >50%)
ANCHOR_EXT_DIFF_THRESHOLD = 1.5  # |ext_diff| sobre este → empieza a blendear

UA = "weather-predictor/1 jose.rubio.uhy@gmail.com"

OM_BASE = "https://api.open-meteo.com/v1/forecast"
NWS_BASE = "https://api.weather.gov"

_cache: dict = {}
_NARRATIVE_TTL = 21600  # 6h · NWS narrative cambia ~4×/día
_MODELS_TTL = 3600      # 60 min · runs Open-Meteo cada 1-6h por modelo,
                        # 1h captura el run más rápido sin spam de cuota

# (open-meteo model id, etiqueta corta para UI)
# Modelos distintos para tener una opinión amplia. Open-Meteo aliasea
# `gfs_hrrr` a `gfs_seamless` (probado el 2026-04-28: valores idénticos),
# así que HRRR no añade independencia real — usamos UK Met Office y
# GraphCast (ML de DeepMind) como aportes US-relevantes adicionales.
OPEN_METEO_MODELS = [
    ("gfs_seamless", "GFS"),
    ("ecmwf_ifs025", "ECMWF"),
    ("icon_seamless", "ICON"),
    ("meteofrance_seamless", "MétéoFR"),
    ("ukmo_seamless", "UKMO"),
    ("gfs_graphcast025", "GraphCast"),
]


@dataclass
class MultiModelMax:
    by_model: list  # [(label, max_f or None), ...]
    median: Optional[float]
    spread: Optional[float]


def _cache_get(key: str, ttl: int):
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < ttl:
            return val
    return None


def _cache_set(key: str, val) -> None:
    _cache[key] = (time.time(), val)


def _phi(x: float) -> float:
    """CDF de la normal estándar."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def external_gaussian_p_bin(ext_med: float, ext_spread: float,
                            bin_lo: float, bin_hi: float) -> float:
    """P([bin_lo, bin_hi]) bajo Normal(ext_med, σ) donde σ se deriva del
    range entre modelos externos. Bordes ±0.5 para alinear con redondeo NWS."""
    sigma = max(ANCHOR_SIGMA_FLOOR_F, ext_spread / ANCHOR_SIGMA_FROM_RANGE)
    lo = (bin_lo - 0.5 - ext_med) / sigma if bin_lo > -1e6 else -10.0
    hi = (bin_hi + 0.5 - ext_med) / sigma if bin_hi < 1e6 else 10.0
    return max(0.0, _phi(hi) - _phi(lo))


def anchor_weight(ext_diff: Optional[float],
                  lam: Optional[float] = 0.0,
                  ext_used: float = 0.0) -> float:
    """Peso a aplicar a la externa, sólo función de |ext_diff|.

    Antes sumaba un bias_component basado en el EWMA del tracker, pero ese
    bias ya se restó del ensemble antes de llegar aquí — se contaba dos veces.
    Ahora el shift posterior (external_models.posterior_shift_weight, λ) hace
    el trabajo de corrección de ubicación; este peso atiende solo la forma
    por-bin de las colas. Cap combinado: w + λ + ext_used ≤ ANCHOR_WEIGHT_CAP.

    `ext_used` es la fracción de la brecha externa ya consumida por el
    sign-nudge del bias_tracker (que se aplica antes del shift+blend). Cierra
    el leak P3 (Codex 2026-06-18): nudge usaba ext_diff para atenuarse pero
    no descontaba del budget total.
    """
    if ext_diff is None or abs(ext_diff) < ANCHOR_EXT_DIFF_THRESHOLD:
        return 0.0
    w = (abs(ext_diff) - ANCHOR_EXT_DIFF_THRESHOLD) * 0.15
    headroom = ANCHOR_WEIGHT_CAP - (lam or 0.0) - max(0.0, ext_used)
    return min(ANCHOR_WEIGHT_CAP, max(0.0, w), max(0.0, headroom))


def blend_with_external(our_p: float, ext_med: Optional[float],
                        ext_spread: Optional[float],
                        bin_lo: float, bin_hi: float,
                        ext_diff: Optional[float],
                        lam: Optional[float] = 0.0,
                        ext_used: float = 0.0) -> tuple[float, float]:
    """Devuelve (p_blended, weight_used). Si no hay datos externos o peso=0,
    devuelve (our_p, 0.0). Garantiza p_blended en [0,1]. `ext_used` lo pasa
    a anchor_weight para descontar lo ya consumido por el sign-nudge."""
    if ext_med is None or ext_spread is None:
        return our_p, 0.0
    w = anchor_weight(ext_diff, lam, ext_used=ext_used)
    if w <= 0.0:
        return our_p, 0.0
    ext_p = external_gaussian_p_bin(ext_med, ext_spread, bin_lo, bin_hi)
    blended = (1 - w) * our_p + w * ext_p
    return max(0.0, min(1.0, blended)), w


def fetch_multi_model_max(station, timeout: float = 10.0,
                          day_offset: int = 0) -> Optional[MultiModelMax]:
    """Devuelve la máxima de hoy+day_offset según cada modelo, mediana y spread."""
    key = f"{station.id}|models|d{day_offset}"
    cached = _cache_get(key, _MODELS_TTL)
    if cached is not None:
        return cached

    models_param = ",".join(m for m, _ in OPEN_METEO_MODELS)
    try:
        r = requests.get(OM_BASE, params={
            "latitude": station.lat,
            "longitude": station.lon,
            "daily": "temperature_2m_max",
            "models": models_param,
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "forecast_days": max(1, day_offset + 1),
        }, headers={"User-Agent": UA}, timeout=timeout)
        try:
            import om_quota
            om_quota.count_call("multi_model_max")
        except Exception:
            pass
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    daily = data.get("daily") or {}
    by_model = []
    values = []
    for model_id, label in OPEN_METEO_MODELS:
        arr = daily.get(f"temperature_2m_max_{model_id}")
        v = None
        if isinstance(arr, list) and len(arr) > day_offset and arr[day_offset] is not None:
            v = float(arr[day_offset])
            values.append(v)
        by_model.append((label, v))

    result = None
    if values:
        s = sorted(values)
        n = len(s)
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        result = MultiModelMax(
            by_model=by_model,
            median=median,
            spread=max(values) - min(values),
        )
    _cache_set(key, result)
    return result


def fetch_nws_narrative(station, timeout: float = 10.0) -> Optional[str]:
    """Texto corto del primer periodo (e.g. 'Today: Sunny, high near 75°F')."""
    key = f"{station.id}|narrative"
    cached = _cache_get(key, _NARRATIVE_TTL)
    if cached is not None:
        return cached

    headers = {"User-Agent": UA, "Accept": "application/geo+json"}
    try:
        r = requests.get(f"{NWS_BASE}/points/{station.lat},{station.lon}",
                         headers=headers, timeout=timeout)
        if r.status_code != 200:
            _cache_set(key, None)
            return None
        forecast_url = (r.json().get("properties") or {}).get("forecast")
        if not forecast_url:
            _cache_set(key, None)
            return None

        r = requests.get(forecast_url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            _cache_set(key, None)
            return None
        periods = ((r.json().get("properties") or {}).get("periods") or [])
        if not periods:
            _cache_set(key, None)
            return None

        p = periods[0]
        name = p.get("name") or ""
        text = p.get("detailedForecast") or p.get("shortForecast") or ""
        result = f"{name}: {text}".strip(": ").strip() or None
    except (requests.RequestException, ValueError):
        _cache_set(key, None)
        return None

    _cache_set(key, result)
    return result


# ── Anchor en el POSTERIOR (no solo por-bin) ──
# Desplaza el ensemble entero hacia la mediana externa cuando discrepamos
# fuerte. ext_diff convención: pred_med - ext_med (negativo = vamos fríos).
POSTERIOR_SHIFT_THRESHOLD_F = 1.5
POSTERIOR_SHIFT_SLOPE = 0.25
POSTERIOR_SHIFT_CAP = 0.50
POSTERIOR_HEAT_BONUS = 0.15
POSTERIOR_HEAT_PCT = 80         # corte clim percentil: bajado de 85 a 80 tras
                                # ver clim real (KLAS 06-10 quedaba en p83 con
                                # 85; ahora entra al bonus correctamente)
POSTERIOR_MAX_SPREAD_F = 5.4    # 3°C; mismo umbral que MAX_MODELS_SPREAD_F de bets


def posterior_shift_weight(ext_diff, ext_spread, clim_percentile,
                           ext_used: float = 0.0):
    """Peso λ en [0, CAP − ext_used] para shift = λ · (ext_med − pred_med).

    Rampa continua sobre el umbral (sin acantilado). En régimen caliente
    subestimado (clim ≥ POSTERIOR_HEAT_PCT y vamos fríos) el umbral baja
    a 1.0°F y suma POSTERIOR_HEAT_BONUS. Si los externos discrepan más de
    POSTERIOR_MAX_SPREAD_F entre sí, su mediana no es ancla fiable.

    `ext_used` (P3 fix Codex 2026-06-18): fracción del budget externo ya
    consumida por el sign-nudge del bias_tracker, que también mueve pred
    hacia externals. Se descuenta del CAP para evitar doble cuenta.
    """
    if ext_diff is None:
        return 0.0
    if ext_spread is not None and ext_spread > POSTERIOR_MAX_SPREAD_F:
        return 0.0
    heat_under = (clim_percentile is not None
                  and clim_percentile >= POSTERIOR_HEAT_PCT
                  and ext_diff < 0)
    thr = 1.0 if heat_under else POSTERIOR_SHIFT_THRESHOLD_F
    if abs(ext_diff) < thr:
        return 0.0
    w = (abs(ext_diff) - thr) * POSTERIOR_SHIFT_SLOPE
    if heat_under:
        w += POSTERIOR_HEAT_BONUS
    cap = POSTERIOR_SHIFT_CAP - max(0.0, ext_used)
    return min(max(0.0, cap), max(0.0, w))
