#!/usr/bin/env python3
"""¿Empeora el calibrador con la ventana de pico CERRADA? Por estación.

Criterio pre-registrado en DECISIONES.md (commit 49c0145), antes de mirar esta
partición:
  (a) fuera de la ventana el calibrador empeora en ≥15 de 20 estaciones
  (b) dentro de la ventana sigue ayudando en el conjunto

Compara además contra el corte horario global de las 17h, que sí se había
visto, para saber cuál de los dos implementar.
"""
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ, PEAK_HOURS  # noqa: E402

CORTE_GLOBAL_H = 17


def p_signos(k, n):
    if n <= 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def contiene(settle, lo, hi):
    LO = lo if abs(lo) < 1e8 else float("-inf")
    HI = hi if abs(hi) < 1e8 else float("inf")
    return 1 if (LO - 0.5) <= settle <= (HI + 0.5) else 0


def main():
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(st, d): mx for st, d, mx in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE max_obs_f IS NOT NULL")}

    print("# El calibrador dentro y fuera de la ventana de pico\n")
    print("| est | pico | N dentro | Δ dentro | N fuera | Δ fuera | "
          "¿empeora fuera? |")
    print("|---|---|---|---|---|---|---|")
    a_favor = n_est = 0
    tot_dentro, tot_fuera = [], []
    glob_dentro, glob_fuera = [], []
    for st in sorted(STATION_TZ):
        if st not in PEAK_HOURS:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        _lo_p, hi_p = PEAK_HOURS[st]
        dentro, fuera = [], []
        for ts, lo, hi, p_raw, p_cal in an.execute(
                """SELECT ts, bin_lo, bin_hi, our_p, our_p_calibrated
                   FROM kalshi_snapshots WHERE station=? AND our_p IS NOT NULL
                     AND our_p_calibrated IS NOT NULL""", (st,)):
            t = datetime.fromisoformat(ts).astimezone(tz)
            key = (st, t.date().isoformat())
            if key not in settles:
                continue
            o = contiene(settles[key], lo, hi)
            par = ((p_raw - o) ** 2, (p_cal - o) ** 2)
            (fuera if t.hour >= hi_p else dentro).append(par)
            (glob_fuera if t.hour >= CORTE_GLOBAL_H else glob_dentro).append(par)
        if not dentro or not fuera:
            continue
        n_est += 1
        d_in = (statistics.mean([x[1] for x in dentro])
                - statistics.mean([x[0] for x in dentro]))
        d_out = (statistics.mean([x[1] for x in fuera])
                 - statistics.mean([x[0] for x in fuera]))
        if d_out > 0:
            a_favor += 1
        tot_dentro += dentro
        tot_fuera += fuera
        print(f"| {st} | cierra {hi_p}h | {len(dentro)} | {d_in:+.4f} | "
              f"{len(fuera)} | {d_out:+.4f} | "
              f"{'✅ sí' if d_out > 0 else '❌ no'} |")

    d_in = (statistics.mean([x[1] for x in tot_dentro])
            - statistics.mean([x[0] for x in tot_dentro]))
    d_out = (statistics.mean([x[1] for x in tot_fuera])
             - statistics.mean([x[0] for x in tot_fuera]))
    p = p_signos(a_favor, n_est)
    print(f"\n## Criterio\n")
    print(f"- (a) empeora fuera en **{a_favor} de {n_est}** estaciones "
          f"(pide 15; signos p={p:.4f}) {'✅' if a_favor >= 15 else '❌'}")
    print(f"- (b) dentro sigue ayudando: Δ **{d_in:+.4f}** "
          f"{'✅' if d_in < 0 else '❌'}")
    print(f"- Fuera de la ventana, Δ del conjunto: **{d_out:+.4f}** "
          f"sobre {len(tot_fuera)} bins")

    gd = (statistics.mean([x[1] for x in glob_dentro])
          - statistics.mean([x[0] for x in glob_dentro]))
    gf = (statistics.mean([x[1] for x in glob_fuera])
          - statistics.mean([x[0] for x in glob_fuera]))
    print(f"\n## Ventana de pico vs corte horario global de las {CORTE_GLOBAL_H}h\n")
    print("| corte | Δ donde se aplica | Δ donde se apagaría | bins apagados |")
    print("|---|---|---|---|")
    print(f"| ventana de pico (por estación) | {d_in:+.4f} | {d_out:+.4f} | "
          f"{len(tot_fuera)} |")
    print(f"| hora global {CORTE_GLOBAL_H}h | {gd:+.4f} | {gf:+.4f} | "
          f"{len(glob_fuera)} |")
    mejor = ("ventana de pico" if d_in <= gd else f"hora global {CORTE_GLOBAL_H}h")
    print(f"\nMenor Δ donde se sigue aplicando ⇒ **{mejor}**")

    print()
    if a_favor >= 15 and d_in < 0:
        print("🟢 **ADOPTAR** — se cumplen las dos condiciones.")
    else:
        print("🔴 **RECHAZADO** — no se cumplen las dos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
