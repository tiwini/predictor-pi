#!/usr/bin/env python3
"""¿Debe el piso de observación incluir la temperatura ACTUAL?

CONTEXTO
--------
`apply_obs_floor` (2026-07-26) clampea la distribución contra `today_max_obs`,
que sale sólo de METARs horarios. `current_f` — el feed de 5 min — no entra.
Cuando el METAR va atrasado, que es media hora de cada ciclo, `current_f` puede
superarlo y la predicción del máximo del día queda por debajo de la temperatura
que hace en ese momento, que es imposible.

Medido (investigacion/pred_bajo_current.py, julio, 70750 snapshots):
  1254 violaciones (1.77%), déficit mediano 1.39°F, p90 5.10°F, peor 13.16°F
  KMDW concentra 657 (18.6% de sus snapshots); el resto por debajo del 3%
  el clamp de max_obs había actuado en sólo 5 de las 1254: el agujero no es
  que el piso se quede corto, es que nadie compara contra current.

Caso operativo: KPHL 2026-07-27, current 87.8, max_obs 86.0 (METAR de hace
90 min), predicción 86.0, y el sistema marcando ACTIONABLE la venta del bin
87-88 que ya contenía la temperatura, contra un mercado que le daba 92%.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

ESTIMADORES (por snapshot, no por día: el piso se aplica en cada uno)
  floor_A = today_max_obs                              <- lo actual
  floor_E = max(today_max_obs, current_f - MARGEN)     <- propuesto
  pred_A  = ens_med  (ya viene clampeado con A)
  pred_E  = max(ens_med, floor_E)                      <- el clamp sólo sube

  MARGEN = 0.9°F, medio escalón de °C, por el redondeo del feed. Mismo valor
  y misma justificación que en backtest_fallback_5min (donde D0.9 eliminó por
  completo el riesgo añadido). No es un parámetro libre.

ASIMETRÍA (idéntica a la del backtest del fallback, y por la misma razón)
  Pasarse del settle con el piso fuerza la distribución por encima de un valor
  que ya no puede subir: error garantizado. Quedarse corto sólo cuesta
  precisión. El listón de riesgo es más duro que el de beneficio.

MÉTRICAS
  Riesgo   : P(floor_E > settle + 0.05) y exceso mediano.
             Control: P(floor_A > settle), la base actual.
  Beneficio: |pred_E - settle| frente a |pred_A - settle|, mediano.
             Y cuántas de las 1254 violaciones del invariante desaparecen.

CRITERIO DE DECISIÓN
  ADOPTAR   si reduce el |error| mediano  Y  P(floor_E > settle) <= 5%
            Y  exceso mediano <= 0.5°F
  RECHAZAR  si P(floor_E > settle) > 15%  O  exceso mediano > 1.0°F
  ZONA GRIS en cualquier otro caso: no actuar.

  RIESGO ESPERADO Y POR QUÉ NO ES OBVIO: el settle es el CLI del NWS, otra
  fuente que el feed de 5 min. Ya se observaron gaps NEGATIVOS (KDEN -2.0°F:
  el feed leyó más alto que el CLI final). Si eso es común, `current - 0.9`
  puede superar al settle y el piso se rompería. Ese es justamente el riesgo
  que este backtest mide, no un detalle.

EXCLUSIONES
  - snapshots sin ens_med, sin current_f o sin settle del día local.
  - KIAH y cualquier id fuera del roster actual.

NOTA: sexto backtest sobre esta base hoy. Positivo = candidato, no cambio
automático de pipeline.
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
from stations import STATION_TZ    # noqa: E402

UTC = ZoneInfo("UTC")
MARGEN = 0.9


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    rows = an.execute(
        """SELECT station, ts, ens_med, current_f, today_max_obs
           FROM station_snapshots
           WHERE ens_med IS NOT NULL AND current_f IS NOT NULL
             AND today_max_obs IS NOT NULL AND today_max_obs > -900
             AND ts >= '2026-07-01'""").fetchall()

    errA: list[float] = []
    errE: list[float] = []
    overA = overE = 0
    excE: list[float] = []
    viol_antes = viol_despues = 0
    n = 0
    per_st: dict[str, list] = {}

    for r in rows:
        st = r["station"]
        if st not in STATION_TZ:
            continue
        ts = datetime.fromisoformat(r["ts"])
        day = ts.astimezone(ZoneInfo(STATION_TZ[st])).date().isoformat()
        settle = settles.get((st, day))
        if settle is None:
            continue
        n += 1
        fa = r["today_max_obs"]
        fe = max(fa, r["current_f"] - MARGEN)
        pa = r["ens_med"]
        pe = max(pa, fe)
        errA.append(abs(pa - settle))
        errE.append(abs(pe - settle))
        overA += fa > settle + 0.05
        if fe > settle + 0.05:
            overE += 1
            excE.append(fe - settle)
        if pa < r["current_f"] - MARGEN:
            viol_antes += 1
            if pe < r["current_f"] - MARGEN:
                viol_despues += 1
        per_st.setdefault(st, []).append((abs(pa - settle), abs(pe - settle),
                                          fe > settle + 0.05))

    if not n:
        print("sin datos")
        return 1
    pA, pE = 100 * overA / n, 100 * overE / n
    exc = statistics.median(excE) if excE else 0.0
    mA, mE = statistics.median(errA), statistics.median(errE)

    print(f"snapshots con settle: {n}\n")
    print("RIESGO — ¿el piso se pasa del settle?")
    print(f"  floor_A (max_obs)              {overA:5d}/{n} = {pA:5.2f}%")
    print(f"  floor_E (con current - {MARGEN}) {overE:5d}/{n} = {pE:5.2f}%"
          f"   exceso mediano {exc:+.2f}°F")
    print(f"  delta introducido: {pE - pA:+.2f} puntos")

    print("\nBENEFICIO")
    print(f"  |pred_A - settle| mediano   {mA:.3f}°F")
    print(f"  |pred_E - settle| mediano   {mE:.3f}°F   ({mE - mA:+.3f})")
    print(f"  violaciones del invariante: {viol_antes} -> {viol_despues} "
          f"({100 * (viol_antes - viol_despues) / max(viol_antes, 1):.0f}% eliminadas)")

    if mE < mA and pE <= 5 and exc <= 0.5:
        v = "ADOPTAR (candidata: confirmar con días frescos)"
    elif pE > 15 or exc > 1.0:
        v = "RECHAZAR"
    else:
        v = "ZONA GRIS — no actuar"
    print(f"\nVEREDICTO: {v}")

    print("\nPor estación (|err| A -> E, y % en que floor_E se pasa):")
    for st, v3 in sorted(per_st.items()):
        a = statistics.median([x[0] for x in v3])
        e = statistics.median([x[1] for x in v3])
        ov = 100 * sum(1 for x in v3 if x[2]) / len(v3)
        mark = " <-" if abs(e - a) > 0.05 else ""
        print(f"  {st:6s} N={len(v3):5d}  {a:5.2f} -> {e:5.2f}  "
              f"se pasa {ov:5.1f}%{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
