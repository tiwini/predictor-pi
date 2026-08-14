#!/usr/bin/env python3
"""¿Cuántas veces la predicción queda clavada en el piso de observación?

"Clavada" = `ens_med` coincide con el piso (max_obs, CLI parcial o current-0.9).
Eso significa que el sistema predice que el día NO sube más — afirmación fuerte
si la ventana de pico aún no ha cerrado.

Motivo: KLAX el 2026-08-14 a las 10:52 tenía ens_med 74.3 con el current en 75.2
y la ventana de pico (12-15h) sin abrir, contra un mercado en 77-78 a 0.81. El
corrector de nivel aplicaba +2.60 en un día `cold_snap` de percentil 8.

La pregunta: ¿es un caso aislado o el corrector clava la predicción a menudo?
Se compara ANTES y DESPUÉS del 2026-08-05, cuando se activó en KLAX y KSFO, y
contra estaciones sin corrector como control.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ, PEAK_HOURS   # noqa: E402

CORRECTOR_DESDE = "2026-08-05"
CON_CORRECTOR = {"KLAX", "KSFO"}
TOL = 0.15


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row

    # clavado = ens_med igual al piso; "prematuro" = además con la ventana
    # de pico todavía abierta, que es cuando la afirmación es insostenible
    stats = defaultdict(lambda: {"n": 0, "clav": 0, "prem": 0, "dias": set(),
                                 "dias_clav": set()})
    q = """SELECT station, ts, ens_med, today_max_obs, today_max_cli, current_f
           FROM station_snapshots WHERE ens_med IS NOT NULL AND ts >= ?"""
    for r in an.execute(q, ("2026-07-20",)):
        st = r["station"]
        if st not in STATION_TZ:
            continue
        cands = [x for x in (r["today_max_obs"], r["today_max_cli"])
                 if x is not None and x > -900]
        if r["current_f"] is not None and r["current_f"] > -900:
            cands.append(r["current_f"] - 0.9)
        if not cands:
            continue
        piso = max(cands)
        local = datetime.fromisoformat(r["ts"]).astimezone(ZoneInfo(STATION_TZ[st]))
        dia = local.date().isoformat()
        periodo = "post" if dia >= CORRECTOR_DESDE else "pre"
        k = (st, periodo)
        s = stats[k]
        s["n"] += 1
        s["dias"].add(dia)
        if abs(r["ens_med"] - piso) <= TOL:
            s["clav"] += 1
            s["dias_clav"].add(dia)
            if local.hour + local.minute / 60 < PEAK_HOURS[st][1]:
                s["prem"] += 1

    print("Predicción clavada en el piso de observación")
    print("  'prematuro' = clavada con la ventana de pico aún ABIERTA\n")
    print(f"  {'st':6s} {'periodo':>8s} {'snapshots':>10s} {'clavada':>9s} "
          f"{'prematura':>10s} {'días con clavada':>17s}")
    for st in sorted({k[0] for k in stats}):
        for per in ("pre", "post"):
            s = stats.get((st, per))
            if not s or s["n"] < 20:
                continue
            marca = " ←corrector" if (st in CON_CORRECTOR and per == "post") else ""
            print(f"  {st:6s} {per:>8s} {s['n']:10d} "
                  f"{100*s['clav']/s['n']:8.1f}% {100*s['prem']/s['n']:9.1f}% "
                  f"{len(s['dias_clav']):8d}/{len(s['dias']):<8d}{marca}")
        print()

    print("\nRESUMEN: clavada prematura, con corrector vs sin él (periodo post)")
    con_c, sin_c = [], []
    for (st, per), s in stats.items():
        if per != "post" or s["n"] < 20:
            continue
        (con_c if st in CON_CORRECTOR else sin_c).append(100 * s["prem"] / s["n"])
    if con_c and sin_c:
        con_c.sort(); sin_c.sort()
        print(f"  con corrector (KLAX/KSFO): "
              f"{sum(con_c)/len(con_c):.1f}%  {[f'{x:.1f}' for x in con_c]}")
        print(f"  sin corrector (n={len(sin_c)}): mediana "
              f"{sin_c[len(sin_c)//2]:.1f}%   rango {sin_c[0]:.1f}-{sin_c[-1]:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
