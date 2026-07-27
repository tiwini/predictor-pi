#!/usr/bin/env python3
"""¿Cuántas veces predecimos un máximo por DEBAJO de la temperatura actual?

No es un test de hipótesis: es el conteo de una violación de invariante. El
máximo del día no puede ser menor que la temperatura que hace en ese momento,
así que cada caso es un error garantizado, igual que los 176 que motivaron
`apply_obs_floor` el 2026-07-26 — con la diferencia de que aquel clamp mira
`today_max_obs` (sólo METARs horarios) y NO mira `current_f`.

Cuando el METAR va atrasado, que es media hora de cada ciclo, `current_f` puede
superar a `today_max_obs`. Visto en KPHL 2026-07-27: current 87.8, max_obs 86.0
(METAR de hace 90 min), predicción 86.0, y el sistema recomendando vender el
bin 87-88 que ya contenía la temperatura.

MARGEN: el feed llega en °C enteros, así que un "87.8" representa
[86.9, 88.7)°F. Se cuenta la violación sólo si la predicción queda por debajo
del extremo INFERIOR (current - 0.9), para no acusar al redondeo.
"""
from __future__ import annotations

import sqlite3
import statistics
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
MARGEN = 0.9

an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
an.row_factory = sqlite3.Row

rows = an.execute(
    """SELECT station, ts, ens_med, current_f, today_max_obs, obs_floor_n
       FROM station_snapshots
       WHERE ens_med IS NOT NULL AND current_f IS NOT NULL
         AND ts >= '2026-07-01'""").fetchall()

viol = [r for r in rows if r["ens_med"] < r["current_f"] - MARGEN]
print(f"snapshots analizados (desde 2026-07-01): {len(rows)}")
print(f"con predicción por debajo de (current - {MARGEN}): {len(viol)} "
      f"({100 * len(viol) / len(rows):.2f}%)")

if viol:
    deltas = sorted(r["current_f"] - MARGEN - r["ens_med"] for r in viol)
    print(f"  déficit mediano {statistics.median(deltas):.2f}°F · "
          f"p90 {deltas[int(0.9 * (len(deltas) - 1))]:.2f}°F · "
          f"peor {deltas[-1]:.2f}°F")

    per: dict[str, list] = {}
    for r in viol:
        per.setdefault(r["station"], []).append(r)
    tot_per: dict[str, int] = {}
    for r in rows:
        tot_per[r["station"]] = tot_per.get(r["station"], 0) + 1
    print("\n  por estación (violaciones / snapshots):")
    for st, v in sorted(per.items(), key=lambda kv: -len(kv[1])):
        print(f"    {st:6s} {len(v):5d}/{tot_per.get(st, 0):5d}  "
              f"{100 * len(v) / tot_per.get(st, 1):5.1f}%")

    # ¿el clamp de piso estaba actuando en esos casos? Si obs_floor_n > 0, el
    # clamp actuó con max_obs y aun así la predicción quedó bajo current.
    con_clamp = sum(1 for r in viol if (r["obs_floor_n"] or 0) > 0)
    print(f"\n  de las violaciones, el clamp de max_obs SÍ había actuado en "
          f"{con_clamp} ({100 * con_clamp / len(viol):.0f}%)")
    print("  -> en esas, el piso de max_obs no bastó porque current iba por delante")
