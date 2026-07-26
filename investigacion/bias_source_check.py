#!/usr/bin/env python3
"""¿De qué sirve el bias tracker si su `early_pred` sale de donde sale?

Hoy `compute_bias` lee `prediction_snapshots`, que sólo se escriben cuando la UI
construye un snapshot para la estación ACTIVA (`_calibration.record` desde el
CSV logger de predictor.py). Consecuencias medidas 2026-07-26:

  - 19 de 20 estaciones tienen 0-3 muestras en los últimos 7 días con settle.
  - El filtro `snapshot_time > date || 'T08:00'` es UTC: para estaciones en
    UTC-4..UTC-7 eso es 01:00-04:00 local, así que NO filtra nada y el
    "early_pred" puede venir de las 15:36 local, con el día ya resuelto.

Este script compara, por estación:

  A) el bias que el tracker computa hoy (fuente vieja),
  B) el que saldría de `station_snapshots` (analysis.db) — las 20 estaciones
     cada ~10 min — tomando la mediana de `ens_med` en una ventana LOCAL fija,
  C) el error real de la tarde (ens_med − settle en 15-18h local), que es la
     dirección que la corrección debería tener.

Convención de signo del tracker: bias positivo = predecimos alto, y
`predictor.py` hace `v - bias`, o sea baja la predicción. Entonces una
corrección sana necesita `signo(bias) == signo(error real)`.

Read-only. Uso: python3 bias_source_check.py [--win 8 10]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/popeye/predictor-pi/weather-predictor")
import bias_tracker as bt  # noqa: E402
from stations import STATION_IDS, STATION_TZ  # noqa: E402

ANALYSIS_DB = "/home/popeye/predictor-pi/weather-predictor/analysis.db"
CALIB_DB = "/home/popeye/predictor-pi/weather-predictor/calibration.db"
N_DAYS = 7


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--win", nargs=2, type=int, default=(8, 10),
                    metavar=("DESDE", "HASTA"),
                    help="ventana horaria LOCAL para el early_pred nuevo")
    args = ap.parse_args()
    w_lo, w_hi = args.win

    con = sqlite3.connect(f"file:{CALIB_DB}?mode=ro", uri=True, timeout=30)
    settles = {(s, d): m for s, d, m in con.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE max_obs_f IS NOT NULL")}
    con.close()

    con = sqlite3.connect(f"file:{ANALYSIS_DB}?mode=ro", uri=True, timeout=30)
    rows = con.execute("SELECT station, ts, ens_med FROM station_snapshots "
                       "WHERE ens_med IS NOT NULL").fetchall()
    con.close()

    early: dict = defaultdict(list)     # (st, day) -> ens_med en la ventana
    tarde: dict = defaultdict(list)
    for st, ts_s, ens in rows:
        tzname = STATION_TZ.get(st)
        if tzname is None:
            continue
        try:
            loc = datetime.fromisoformat(ts_s).astimezone(ZoneInfo(tzname))
        except ValueError:
            continue
        key = (st, loc.date().isoformat())
        if w_lo <= loc.hour < w_hi:
            early[key].append(ens)
        if 15 <= loc.hour < 19:
            tarde[key].append(ens)

    print(f"Ventana local del early_pred nuevo: {w_lo:02d}-{w_hi:02d}h · "
          f"últimos {N_DAYS} días con settle\n")
    print(f"{'STA':6}{'viejo':>8}{'n_v':>4}  {'nuevo':>8}{'n_n':>4}  "
          f"{'err_tarde':>10}  signo")
    n_ok = n_bad = n_none = 0
    for sid in STATION_IDS:
        old = bt.compute_bias(sid)
        days = sorted({d for (s, d) in settles if s == sid}, reverse=True)[:N_DAYS]
        errs_new = [statistics.median(early[(sid, d)]) - settles[(sid, d)]
                    for d in days if early.get((sid, d))]
        errs_pm = [statistics.median(tarde[(sid, d)]) - settles[(sid, d)]
                   for d in days if tarde.get((sid, d))]
        new_bias = statistics.median(errs_new) if errs_new else None
        err_pm = statistics.median(errs_pm) if errs_pm else None

        if new_bias is None or err_pm is None:
            verdict = "sin datos"
            n_none += 1
        elif abs(new_bias) < 0.3 or abs(err_pm) < 0.3:
            verdict = "~cero"
        elif (new_bias > 0) == (err_pm > 0):
            verdict = "OK"
            n_ok += 1
        else:
            verdict = "INVERTIDO"
            n_bad += 1
        print(f"{sid:6}{old['bias']:>+8.2f}{old['n']:>4}  "
              f"{(f'{new_bias:+8.2f}' if new_bias is not None else '       ·')}"
              f"{len(errs_new):>4}  "
              f"{(f'{err_pm:+10.1f}' if err_pm is not None else '         ·')}"
              f"  {verdict}")

    print(f"\nsigno del bias nuevo vs error real de la tarde: {n_ok} OK · "
          f"{n_bad} invertido · {n_none} sin datos")
    print("El viejo aplica sólo con n>=3 muestras oportunistas; el nuevo tiene\n"
          "una muestra por día para las 20 porque station_snapshots corre cada\n"
          "10 min desde semanas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
