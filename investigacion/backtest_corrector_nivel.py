#!/usr/bin/env python3
"""¿Sustituir el EWMA del bias por la mediana histórica del sesgo?

CONTEXTO
--------
`backtest_por_que_baseline.py` (N=521) mostró dos cosas:
  - El ensemble CENTRADO baja su |error| de 1.93 a 1.40°F, y así le gana al
    baseline trivial (1.60). O sea discrimina bien y le sobra offset.
  - Los sesgos por estación son ESTABLES: Spearman(1ª mitad, 2ª mitad) = +0.70,
    signo coincide en 15 de 19 estaciones.

Existe por tanto una corrección de nivel que funcionaría, y el bias tracker
—que existe para eso— no la captura: su path EWMA acierta la dirección el 49.7%
de los días, azar puro ([[backtest_bias_ayuda_2026_07_28]]).

Sospecha de por qué: el tracker usa un EWMA de 4-5 muestras que pondera lo más
reciente y se deja arrastrar por cualquier día de ruptura (KPHX: un +8.91 volcó
el bias a +3.29 con las otras cuatro muestras negativas). La mediana de toda la
historia es robusta a eso por construcción.

Caso que lo ilustra: el sesgo real de KPHX es -1.79 (SUB-predice, estable en
las dos mitades) y el tracker le aplica +2.18, que RESTA. Van en direcciones
opuestas.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

TRES CORRECTORES, mismo momento y mismos días
  pred_crudo  = ens_med + bias_f_aplicado      (deshace el bias del sistema)
  pred_ewma   = ens_med                        (lo que el sistema publica hoy)
  pred_causal = pred_crudo - mediana(sesgos de días ANTERIORES de la estación)
  pred_loo    = pred_crudo - mediana(sesgos de todos menos el día evaluado)

  **La primaria es `pred_causal`**, que sólo usa el pasado y por tanto es
  aplicable en producción. `pred_loo` se reporta como COTA SUPERIOR optimista:
  usa días posteriores al que predice, así que no se puede lograr en vivo. Si
  la causal no mejora pero la LOO sí, la conclusión es que la corrección
  necesita más historia de la que hay, no que funcione.

  MIN_PREV = 5 días anteriores para que la mediana signifique algo. Los días
  sin ese mínimo se excluyen de todas las variantes por igual.

UNIDAD Y VENTANA
  Igual que los dos backtests anteriores: snapshot 2h antes de que abra
  PEAK_HOURS de la estación (±30 min), un station-day por fila.

CRITERIO DE DECISIÓN (sobre pred_causal)
  ADOPTAR    si |error| causal <= |error| ewma - 0.30°F  Y  N >= 100
  NEUTRO     si la diferencia es < 0.15°F
  RECHAZAR   si la causal es peor que el ewma

  Aunque salga ADOPTAR, es CANDIDATO: cambiar el corrector de nivel toca el
  pipeline de predicción y exige confirmación sobre días frescos antes de
  aplicarse. Es el undécimo backtest sobre esta base.

LIMITACIÓN QUE NO PUEDO CERRAR AQUÍ
  El sesgo se mide 2h antes de la ventana de pico, pero el bias del sistema se
  aplica a todas horas. Si el sesgo depende de la hora, esta medición no lo
  captura. Queda anotado; medirlo pediría repetir el ejercicio por franjas.
=============================================================================
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ, PEAK_HOURS   # noqa: E402

UTC = ZoneInfo("UTC")
TOL_MIN = 30
HOURS_BEFORE_PEAK = 2
MIN_PREV = 5


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    per: dict[str, list] = {}
    for (st, day), settle in settles.items():
        if st not in STATION_TZ or settle is None:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        ref = (datetime.combine(d, datetime.min.time(), tz)
               + timedelta(hours=PEAK_HOURS[st][0] - HOURS_BEFORE_PEAK))
        lo = (ref - timedelta(minutes=TOL_MIN)).astimezone(UTC)
        hi = (ref + timedelta(minutes=TOL_MIN)).astimezone(UTC)
        r = an.execute(
            """SELECT ens_med, bias_f, bias_applied FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             ref.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is None:
            continue
        b = r["bias_f"] if (r["bias_applied"] and r["bias_f"] is not None) else 0.0
        per.setdefault(st, []).append(
            {"day": day, "settle": settle, "ens": r["ens_med"],
             "crudo": r["ens_med"] + b})

    e_crudo, e_ewma, e_causal, e_loo = [], [], [], []
    por_st: dict[str, list] = {}
    for st, days in per.items():
        days.sort(key=lambda x: x["day"])
        sesgos = [x["crudo"] - x["settle"] for x in days]
        for i, x in enumerate(days):
            if i < MIN_PREV:
                continue
            prev = sesgos[:i]
            otros = sesgos[:i] + sesgos[i + 1:]
            c = x["crudo"] - statistics.median(prev)
            l = x["crudo"] - statistics.median(otros)
            e_crudo.append(abs(x["crudo"] - x["settle"]))
            e_ewma.append(abs(x["ens"] - x["settle"]))
            e_causal.append(abs(c - x["settle"]))
            e_loo.append(abs(l - x["settle"]))
            por_st.setdefault(st, []).append(
                (abs(x["ens"] - x["settle"]), abs(c - x["settle"])))

    if len(e_ewma) < 10:
        print(f"N insuficiente ({len(e_ewma)})")
        return 1
    m = statistics.median
    print(f"station-days: {len(e_ewma)}  (excluidos los primeros {MIN_PREV} "
          f"de cada estación)\n")
    print("|error| mediano por corrector de nivel")
    print(f"  sin corrección (crudo)        {m(e_crudo):5.2f}°F")
    print(f"  EWMA actual (lo que publica)  {m(e_ewma):5.2f}°F")
    print(f"  mediana CAUSAL (sólo pasado)  {m(e_causal):5.2f}°F   <- primaria")
    print(f"  mediana LOO (cota optimista)  {m(e_loo):5.2f}°F")

    d = m(e_causal) - m(e_ewma)
    print(f"\n  causal vs EWMA: {d:+.2f}°F")
    if len(e_ewma) < 100:
        v = f"N insuficiente ({len(e_ewma)})"
    elif d <= -0.30:
        v = "ADOPTAR la mediana causal (CANDIDATO: confirmar con días frescos)"
    elif abs(d) < 0.15:
        v = "NEUTRO"
    elif d > 0:
        v = "RECHAZAR — la causal es peor que el EWMA"
    else:
        v = "sin veredicto claro"
    print(f"  VEREDICTO: {v}")

    print("\npor estación (|err| EWMA -> causal)")
    for st in sorted(por_st):
        sub = por_st[st]
        a = m([x[0] for x in sub])
        b = m([x[1] for x in sub])
        mark = "  <- mejora" if b < a - 0.3 else ("  <- empeora" if b > a + 0.3 else "")
        print(f"  {st:6s} N={len(sub):3d}  {a:5.2f} -> {b:5.2f}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
