"""Corrector de nivel por mediana causal del sesgo histórico.

Qué es
------
Resta a la predicción la MEDIANA de los sesgos de días anteriores de esa
estación. Sustituye al EWMA del bias tracker en las estaciones habilitadas; no
se suma a él (eso sería corregir dos veces).

Por qué la mediana y no el EWMA
-------------------------------
Medido sobre 460 días-estación (`investigacion/backtest_corrector_nivel.py`,
pre-registrado):

    sin corrección (crudo)         2.00°F
    EWMA actual (lo que publica)   1.94°F
    mediana CAUSAL (sólo pasado)   1.31°F
    mediana LOO (cota optimista)   1.16°F

El EWMA usa 4-5 muestras ponderando lo reciente, así que un solo día de ruptura
lo vuelca (KPHX: un +8.91 puso el bias en +3.29 con las otras cuatro muestras
negativas). La mediana es robusta a eso por construcción, y el sesgo por
estación es estable: Spearman entre primera y segunda mitad = +0.70, con el
signo coincidiendo en 15 de 19 estaciones.

Alcance deliberadamente estrecho
--------------------------------
`ENABLED_STATIONS` cubre las dos estaciones con mayor sesgo medido, que son
además las de brisa marina de [[adveccion_descartada_2026_08_01]]:

    KSFO   EWMA 4.70 → causal 1.37°F   (la mayor mejora de las 20)
    KLAX   EWMA 3.30 → causal 1.95°F

El backtest apoya extenderlo más —14 de 20 estaciones mejoran— pero el
pre-registro pedía confirmar con días frescos antes de generalizar, así que se
amplía con datos y no de golpe. KDCA es el contraejemplo a vigilar: allí
empeora (1.29 → 1.69), y por eso no entra.

MISMA FUENTE QUE EL BACKTEST
----------------------------
Lee `analysis.db.station_snapshots` con las mismas constantes (2 h antes del
inicio de la ventana de pico, mínimo 5 días previos). Implementarlo contra
`calibration.db` habría cambiado la fuente y el resultado medido ya no aplicaría.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("level_corrector")

DB_PATH = Path(__file__).parent / "analysis.db"
CAL_DB_PATH = Path(__file__).parent / "calibration.db"

# Idénticas a analysis_poller._median_bias_causal y al backtest. No tocar una
# sin la otra: el número medido depende de ellas.
HOURS_BEFORE_PEAK = 2
MIN_PREV_DAYS = 5

# Estaciones donde el corrector sustituye al EWMA.
ENABLED_STATIONS: set[str] = {"KLAX", "KSFO"}

# Guarda de cordura: un corrector de nivel legítimo vive en pocos grados. Si
# sale algo mayor es que la historia está contaminada, y es preferible caer al
# EWMA que aplicar una corrección enorme a ciegas.
MAX_ABS_CORRECTION_F = 8.0

_cache: dict[tuple[str, str], tuple[Optional[float], int]] = {}


def clear_cache() -> None:
    _cache.clear()


def median_level_bias(station_id: str,
                      today: _date) -> tuple[Optional[float], int]:
    """(mediana de sesgos de días ANTERIORES, n_días). (None, n) si no aplica.

    Causal por construcción: `date < today` en la consulta de settles. Nunca
    puede ver el día que está prediciendo.
    """
    key = (station_id, today.isoformat())
    if key in _cache:
        return _cache[key]

    out: tuple[Optional[float], int] = (None, 0)
    try:
        from stations import STATION_TZ, PEAK_HOURS
        tz = ZoneInfo(STATION_TZ[station_id])
        peak_lo = PEAK_HOURS[station_id][0]

        cal = sqlite3.connect(f"file:{CAL_DB_PATH}?mode=ro", uri=True)
        try:
            settles = dict(cal.execute(
                "SELECT date, max_obs_f FROM day_outcomes "
                "WHERE station_id=? AND date < ?",
                (station_id, today.isoformat())).fetchall())
        finally:
            cal.close()

        an = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        an.execute("PRAGMA busy_timeout=5000")
        try:
            sesgos = []
            for day, settle in sorted(settles.items()):
                if settle is None:
                    continue
                try:
                    d = datetime.strptime(day, "%Y-%m-%d").date()
                except ValueError:
                    continue
                ref = (datetime.combine(d, datetime.min.time(), tz)
                       + timedelta(hours=peak_lo - HOURS_BEFORE_PEAK))
                lo = (ref - timedelta(minutes=30)).astimezone(timezone.utc)
                hi = (ref + timedelta(minutes=30)).astimezone(timezone.utc)
                r = an.execute(
                    """SELECT ens_med, bias_f, bias_applied
                       FROM station_snapshots
                       WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
                       ORDER BY ts LIMIT 1""",
                    (station_id, lo.strftime("%Y-%m-%dT%H:%M:%S"),
                     hi.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
                if r is None:
                    continue
                # Se deshace el bias que el sistema aplicó ese día para medir el
                # sesgo del ensemble CRUDO. Sin esto se mediría el residuo tras
                # corregir, que es otra cosa.
                b = r[1] if (r[2] and r[1] is not None) else 0.0
                sesgos.append((r[0] + b) - settle)
        finally:
            an.close()

        if len(sesgos) < MIN_PREV_DAYS:
            out = (None, len(sesgos))
        else:
            sesgos.sort()
            med = sesgos[len(sesgos) // 2]
            if abs(med) > MAX_ABS_CORRECTION_F:
                log.warning("%s: mediana de nivel %.2f fuera de rango, se ignora",
                            station_id, med)
                out = (None, len(sesgos))
            else:
                out = (med, len(sesgos))
    except Exception as e:
        log.warning("median_level_bias %s falló: %s", station_id, e)
        out = (None, 0)

    _cache[key] = out
    return out


def bias_info_for(station_id: str, today: _date) -> Optional[dict]:
    """`bias_info` compatible con el del tracker, o None si no aplica.

    Devolver None significa "usa el EWMA de siempre": estación no habilitada,
    historia insuficiente o valor fuera de rango.
    """
    if station_id not in ENABLED_STATIONS:
        return None
    med, n = median_level_bias(station_id, today)
    if med is None:
        return None
    return {
        "bias": med,
        "applied": True,
        "n": n,
        "mode": "median_level",
        # OJO: la clave es `bias_path`, que es la que lee
        # analysis_poller (bi.get("bias_path")). Con "path" a secas
        # la columna queda NULL y luego no hay forma de saber qué
        # días usaron el corrector.
        "bias_path": "median_causal",
        "reason": f"mediana causal de {n} días previos",
    }
