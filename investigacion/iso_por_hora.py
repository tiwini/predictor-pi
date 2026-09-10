#!/usr/bin/env python3
"""¿A partir de qué hora el calibrador isotónico deja de ayudar?

Medido: mejora el Brier a la hora de referencia (−0.0142) y lo empeora en el
último snapshot del día (+0.0071). Se aplica en TODAS las horas, así que en
algún punto cruza de ayudar a estorbar. Esto busca dónde, por hora local.

El corte tiene que poder caer de los dos lados: si saliera que ayuda a todas
las horas, no hay nada que arreglar, y eso también es un resultado.
"""
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ  # noqa: E402

UTC = ZoneInfo("UTC")


def contiene(settle, lo, hi):
    LO = lo if abs(lo) < 1e8 else float("-inf")
    HI = hi if abs(hi) < 1e8 else float("inf")
    return 1 if (LO - 0.5) <= settle <= (HI + 0.5) else 0


def main():
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    settles = {}
    for st, d, mx in cal.execute(
            "SELECT station_id, date, max_obs_f FROM day_outcomes "
            "WHERE max_obs_f IS NOT NULL"):
        settles[(st, d)] = mx

    por_hora = {}
    for st in sorted(STATION_TZ):
        tz = ZoneInfo(STATION_TZ[st])
        for ts, lo, hi, p_raw, p_cal in an.execute(
                """SELECT ts, bin_lo, bin_hi, our_p, our_p_calibrated
                   FROM kalshi_snapshots
                   WHERE station=? AND our_p IS NOT NULL
                     AND our_p_calibrated IS NOT NULL""", (st,)):
            t = datetime.fromisoformat(ts).astimezone(tz)
            key = (st, t.date().isoformat())
            if key not in settles:
                continue
            o = contiene(settles[key], lo, hi)
            por_hora.setdefault(t.hour, []).append(
                ((p_raw - o) ** 2, (p_cal - o) ** 2))

    print("# El calibrador por hora local\n")
    print("| hora | N bins | Brier crudo | calibrado | Δ | |")
    print("|---|---|---|---|---|---|")
    acum_ayuda = acum_estorba = 0
    for h in sorted(por_hora):
        v = por_hora[h]
        if len(v) < 200:
            continue
        mc = statistics.mean([x[0] for x in v])
        mk = statistics.mean([x[1] for x in v])
        d = mk - mc
        if d < 0:
            acum_ayuda += len(v)
            flag = "🟢 ayuda"
        else:
            acum_estorba += len(v)
            flag = "🔴 estorba"
        print(f"| {h:02d}h | {len(v)} | {mc:.4f} | {mk:.4f} | {d:+.4f} | {flag} |")
    print(f"\nBins en horas donde ayuda: **{acum_ayuda}** · "
          f"donde estorba: **{acum_estorba}**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
