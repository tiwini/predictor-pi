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

KNYC se añade el 2026-08-15, primera estación en entrar por esa vía
(`investigacion/backtest_corrector_knyc.py`, pre-registro en 6258a2d). Sobre 16
días frescos a las 11h local:

    publicado 3.21 → causal 1.72°F   acerca 13/16 (p=0.011)
    acierto de bin  0/16 → 5/16   (a las 14h, 2/16 → 10/16)

Lo que decidió el caso no fue la media sino el signo: los 16 días sobre-predice,
de +0.5 a +7.7°F. Eso es offset de nivel, no dispersión. Probablemente Central
Park corriendo más frío que el punto de rejilla del ensemble — pero toda su
historia cabe en julio-agosto, así que el sesgo NO está verificado fuera del
verano. Si en otoño el corrector empieza a alejar, ésa es la causa a mirar
primero.

KMIA se añade el 2026-08-28 y es la PRIMERA con corrección NEGATIVA: las tres
anteriores sobre-predicen y se les resta; a Miami la sub-predecimos y hay que
sumarle. Salió de la medición de dispersión del mismo día
(`investigacion/dispersion_banda.py`): el settle quedaba por encima del p90 el
**93% de los días**, con una banda de 0.90°F. Backtest de entrada en
`investigacion/backtest_corrector_kmia_klas.py`, sobre 24 días a las 12h local:

    publicado 2.39 → causal 0.78°F   acerca 21/24 (p=0.0001)
    acierto de bin  3/24 → 14/24     corrección mediana −2.37°F

⚠ Dos consecuencias del signo, verificadas antes de habilitarla:
  · `cap_by_floor` no actúa con bias<0 (sólo recorta correcciones que hunden la
    mediana bajo el piso), así que no interfiere.
  · El cold-bias guard de `agent_signals` bloquea YES cold/mid cuando el bias
    baja de −0.7°F. Con KMIA en −2.4 quedará **siempre activo** para esa
    estación. Se deja como está a propósito: sólo afecta a apuestas simuladas,
    nunca al pronóstico, y cambiar un gate revisado sin medirlo sería
    justamente lo que este archivo evita.

KLAS se evaluó a la vez y de día completo **no entraba**: a la hora primaria se
quedaba en ESPERAR (|err| 2.29 → 1.63, mejora 0.66 contra los 0.75 pedidos) y a
las 14h el corrector **empeora** (1.49 → 1.74, acierto de bin 14 → 9). Entra el
mismo día **sólo en su ventana horaria medida** (9-13h), por decisión del
usuario y con la frontera barrida hora a hora — ver `ENABLED_HOURS`. Es la
primera estación que lo lleva a ratos, y por eso el gate horario existe.

KHOU entra el 2026-09-01, única superviviente de las ocho candidatas que el
mapa del 08-30 marcó con el nivel descentrado
(`investigacion/backtest_corrector_nivel8.py`). Sobre 23 días:

    12h (primaria)  publicado 2.57 → causal 1.26°F   acerca 20/23 (p=0.0002)
                    acierto de bin  4/23 → 10/23
    14h (se opera)  publicado 2.34 → causal 1.16°F   acerca 19/23 (p=0.0013)

Pasa a las dos horas, así que entra el día completo y sin ventana horaria.

⚠ Las otras siete fallaron: KBOS, KDEN, KOKC y KPHL empeoran o no se mueven;
KMSP, KMSY y KPHX se quedan cerca. El mapa dice DÓNDE está el problema, no si
la mediana causal puede capturarlo: un sesgo puede ser real y no ser predecible
desde los días anteriores.

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

# Estaciones donde el corrector sustituye al EWMA. Cada una entra con su propio
# backtest pre-registrado, nunca por extrapolación del pool.
ENABLED_STATIONS: set[str] = {"KLAX", "KSFO", "KNYC", "KMIA", "KLAS", "KHOU"}

# Ventana horaria local, inclusive, para las estaciones que sólo lo llevan parte
# del día. Sin entrada aquí, el corrector aplica a todas las horas.
#
# KLAS (2026-08-28): su corrida de entrada dio ESPERAR de día completo —mejora
# 0.66°F contra los 0.75 pedidos— y a las 14h el corrector **empeoraba**
# (1.49 → 1.74). El usuario decidió habilitarla sólo en su hora primaria, y
# `investigacion/ventana_horaria_klas.py` midió la frontera en vez de suponerla:
#
#     9h  −0.77°F  18/24 (75%)      14h  +0.25°F  11/25 (44%)   ← estorba
#    10h  −0.74    17/24 (71%)      15h  +0.05    11/25 (44%)
#    11h  −0.74    18/25 (72%)      16h  +0.07    10/25 (40%)
#    12h  −0.66    17/25 (68%)      17h  −0.07    14/25 (56%)
#    13h  −0.58    17/25 (68%)
#
# ⚠ Encender y apagar el corrector mueve la serie publicada: a las 14h la
# predicción de KLAS baja ~1.8°F de golpe al dejar de corregirse, justo cuando
# abre su ventana de pico. Es el precio de no aplicarlo donde está medido que
# estorba, y se decidió a sabiendas.
ENABLED_HOURS: dict[str, tuple[int, int]] = {"KLAS": (9, 13)}

# Guarda de cordura: un corrector de nivel legítimo vive en pocos grados. Si
# sale algo mayor es que la historia está contaminada, y es preferible caer al
# EWMA que aplicar una corrección enorme a ciegas.
MAX_ABS_CORRECTION_F = 8.0

_cache: dict[tuple[str, str, Optional[int]],
             tuple[Optional[float], int]] = {}


def clear_cache() -> None:
    _cache.clear()


def median_level_bias(station_id: str, today: _date,
                      local_hour: Optional[int] = None
                      ) -> tuple[Optional[float], int]:
    """(mediana de sesgos de días ANTERIORES, n_días). (None, n) si no aplica.

    Causal por construcción: `date < today` en la consulta de settles. Nunca
    puede ver el día que está prediciendo.

    `local_hour` compara con la MISMA hora local de días anteriores. Importa
    mucho: el sesgo del ensemble decae según avanza el día porque va
    incorporando observaciones (medido 2026-08-05, N=30 días por hora):

        KLAX  +3.45 (10h) → +2.40 (13h) → +1.30 (17h) → +0.08 (19h)
        KSFO  +4.85 (10h) → +4.09 (13h) → +2.19 (17h) → +0.00 (19h)
        KPHX  -2.33 (6h)  → -0.91 (14h) → -0.36 (19h)

    Aplicar el sesgo matinal por la tarde sobre-corregía 2.3°F en KLAX y 2.7°F
    en KSFO a las 17h. Con `local_hour=None` se usa la hora de referencia del
    backtest (peak_lo - 2), que es lo que hay que pasar para reproducirlo.
    """
    key = (station_id, today.isoformat(), local_hour)
    if key in _cache:
        return _cache[key]

    out: tuple[Optional[float], int] = (None, 0)
    try:
        from stations import STATION_TZ, PEAK_HOURS
        tz = ZoneInfo(STATION_TZ[station_id])
        ref_hour = (PEAK_HOURS[station_id][0] - HOURS_BEFORE_PEAK
                    if local_hour is None else local_hour)

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
                       + timedelta(hours=ref_hour))
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


def hora_habilitada(station_id: str, local_hour: Optional[int]) -> bool:
    """¿Aplica el corrector a esta hora local en esta estación?

    Sin entrada en `ENABLED_HOURS`, todas las horas. Con entrada, sólo el tramo
    medido, inclusive por los dos extremos.

    `local_hour=None` significa "la hora de referencia del backtest"
    (`peak_lo − HOURS_BEFORE_PEAK`), la misma convención que usa
    `median_level_bias`. Resolverlo aquí evita que el gate diga que no a una
    llamada que en realidad apunta a la hora primaria.
    """
    ventana = ENABLED_HOURS.get(station_id)
    if ventana is None:
        return True
    if local_hour is None:
        try:
            from stations import PEAK_HOURS
            local_hour = PEAK_HOURS[station_id][0] - HOURS_BEFORE_PEAK
        except Exception:
            return False
    lo, hi = ventana
    return lo <= local_hour <= hi


def bias_info_for(station_id: str, today: _date,
                  local_hour: Optional[int] = None) -> Optional[dict]:
    """`bias_info` compatible con el del tracker, o None si no aplica.

    Devolver None significa "usa el EWMA de siempre": estación no habilitada,
    historia insuficiente o valor fuera de rango.
    """
    if station_id not in ENABLED_STATIONS:
        return None
    if not hora_habilitada(station_id, local_hour):
        return None
    med, n = median_level_bias(station_id, today, local_hour)
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
        "reason": (f"mediana causal de {n} días previos"
                   + (f" a las {local_hour}h local" if local_hour is not None
                      else "")),
    }


def cap_by_floor(bias_f: float, daily_maxes: list, floor_f: Optional[float],
                 station_id: str, now_local) -> tuple[float, Optional[str]]:
    """Recorta la corrección para que no hunda la mediana bajo el piso observado.

    Por qué
    -------
    Medido el 2026-08-14 (`investigacion/clavado_acertaba.py`): el corrector
    disparó la "clavada prematura" de KLAX del 0.1% al 8.4% de los snapshots, y
    pasó a ocurrir 10 de 10 días. En esas horas la predicción deja de ser un
    pronóstico y es literalmente el termómetro.

    Y la afirmación implícita es falsa: tras quedar clavado **el día siguió
    subiendo +1.90°F de mediana** (p90 +3.70). En KLAX, dentro de la ventana,
    no corregir habría dado |err| 1.22° frente a 1.90° del clavado (N=53).

    **Sólo actúa con la ventana de pico ABIERTA.** Pasado el pico, clavar en el
    piso es exactamente correcto — error 0.00° medido sobre cientos de casos en
    las 20 estaciones — y ahí no se toca nada.

    Nota: la evidencia es mixta. KSFO apunta al revés (clavado 1.80° contra
    2.30° sin corregir), pero con N=10 frente a los 53 de KLAX. Por eso el guard
    **recorta** en vez de anular: aplica toda la corrección que quepa por encima
    del piso, no cero.

    Devuelve (bias_efectivo, razón_del_recorte o None).
    """
    if bias_f is None or bias_f <= 0 or floor_f is None or not daily_maxes:
        return bias_f, None
    try:
        from stations import PEAK_HOURS
        _lo, hi_p = PEAK_HOURS[station_id]
        hora = now_local.hour + now_local.minute / 60.0
        if hora >= hi_p:
            return bias_f, None          # pasado el pico: clavar es correcto
        sm = sorted(daily_maxes)
        med = sm[len(sm) // 2]
        if med - bias_f >= floor_f - 1e-9:
            return bias_f, None          # no hunde la mediana
        permitido = max(0.0, med - floor_f)
        return permitido, (f"recortado {bias_f:.2f}->{permitido:.2f} "
                           f"(hundía la mediana bajo el piso {floor_f:.1f})")
    except Exception as e:
        log.warning("cap_by_floor %s falló: %s", station_id, e)
        return bias_f, None
