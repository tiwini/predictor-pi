"""¿Cuánto le gana el CLI de la tarde a nuestro feed 5-min a esa misma hora?

Para cada (estación, día) con settle en day_outcomes: toma el mayor
today_max_obs que teníamos hasta la hora local de emisión del CLI de tarde y
lo compara contra el settle. El CLI de tarde iguala el settle en 91% de los
días (probe 2026-07-26), así que esta diferencia ES la ganancia de leerlo.

Corre en el Pi (necesita analysis.db + calibration.db). Read-only.
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
from stations import STATION_TZ  # noqa: E402

# Hora local de emisión del CLI de tarde, medida en el probe del 2026-07-26.
CLI_LATE_HOUR = {
    "KHOU": 16.4, "KMIA": 16.5, "KMDW": 16.6, "KNYC": 16.6, "KMSY": 16.8,
    "KOKC": 17.2, "KPHX": 17.4, "KDCA": 17.4, "KBOS": 17.5, "KLAS": 17.5,
    "KDEN": 17.6, "KSFO": 17.6, "KPHL": 17.6, "KAUS": 17.8, "KSAT": 17.8,
    "KSEA": 18.3, "KLAX": 18.6, "KDFW": 19.5, "KMSP": 19.8, "KATL": 20.6,
}


def main() -> None:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    print(f"{'st':6s} {'N':>3s} {'gap med':>8s} {'gap p90':>8s} "
          f"{'ya igual':>9s}  {'gana ≥1°F':>10s}")
    all_gaps: list[float] = []
    for st, hour in sorted(CLI_LATE_HOUR.items(), key=lambda kv: kv[1]):
        tz = ZoneInfo(STATION_TZ[st])
        gaps: list[float] = []
        for (s_id, day), settle in settles.items():
            if s_id != st:
                continue
            try:
                d = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                continue
            cut_local = (datetime.combine(d, datetime.min.time(), tz)
                         + timedelta(hours=hour))
            cut_utc = cut_local.astimezone(ZoneInfo("UTC"))
            row = an.execute(
                """SELECT MAX(today_max_obs) AS mx FROM station_snapshots
                   WHERE station = ? AND ts >= ? AND ts <= ?
                     AND today_max_obs IS NOT NULL AND today_max_obs > -900""",
                (st,
                 datetime.combine(d, datetime.min.time(), tz)
                 .astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S"),
                 cut_utc.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
            if row is None or row["mx"] is None:
                continue
            gaps.append(settle - row["mx"])
        if not gaps:
            print(f"{st:6s}   0        —        —          —           —")
            continue
        gaps.sort()
        p90 = gaps[int(0.9 * (len(gaps) - 1))]
        eq = sum(1 for g in gaps if abs(g) < 0.05)
        win = sum(1 for g in gaps if g >= 1.0)
        print(f"{st:6s} {len(gaps):3d} {statistics.median(gaps):+8.1f} "
              f"{p90:+8.1f} {eq:4d}/{len(gaps):<4d} {win:5d}/{len(gaps):<4d}")
        all_gaps.extend(gaps)

    if all_gaps:
        all_gaps.sort()
        eq = sum(1 for g in all_gaps if abs(g) < 0.05)
        win = sum(1 for g in all_gaps if g >= 1.0)
        neg = sum(1 for g in all_gaps if g < -0.05)
        print(f"\nTOTAL N={len(all_gaps)}  mediana {statistics.median(all_gaps):+.1f}°F"
              f"  ·  ya igual {eq} ({100*eq/len(all_gaps):.0f}%)"
              f"  ·  gana ≥1°F {win} ({100*win/len(all_gaps):.0f}%)"
              f"  ·  obs POR ENCIMA del settle {neg} ({100*neg/len(all_gaps):.0f}%)")


if __name__ == "__main__":
    main()
