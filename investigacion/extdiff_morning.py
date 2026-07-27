"""¿El ext_diff positivo generalizado de la mañana es normal, y predice algo?

Toma el ext_diff del snapshot más cercano a las 08h local de cada día-estación
y lo cruza con el error final (pred_de_esa_hora − settle).
"""
import sqlite3
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sys

BASE = "/home/popeye/predictor-pi/weather-predictor"
sys.path.insert(0, BASE)
from stations import STATION_TZ

an = sqlite3.connect(f"file:{BASE}/analysis.db?mode=ro", uri=True)
cal = sqlite3.connect(f"file:{BASE}/calibration.db?mode=ro", uri=True)
settles = {(r[0], r[1]): r[2] for r in cal.execute(
    "SELECT station_id, date, max_obs_f FROM day_outcomes")}

diffs, pairs = [], []
for st, tzname in STATION_TZ.items():
    tz = ZoneInfo(tzname)
    for (s_id, day), settle in settles.items():
        if s_id != st:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        lo = (datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=7, minutes=30))
        hi = lo + timedelta(hours=1, minutes=30)
        r = an.execute(
            """SELECT ext_diff_f, ens_med FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ext_diff_f IS NOT NULL
               ORDER BY ts LIMIT 1""",
            (st, lo.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S"),
             hi.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if not r or r[0] is None or r[1] is None:
            continue
        diffs.append(r[0])
        pairs.append((r[0], r[1] - settle))   # ext_diff mañana, error final

if not diffs:
    print("sin datos")
    raise SystemExit

diffs.sort()
pos = sum(1 for d in diffs if d > 0)
print(f"ext_diff a las ~08h local, N={len(diffs)} días-estación")
print(f"  mediana {statistics.median(diffs):+.2f}°F · positivo en {pos} "
      f"({100*pos/len(diffs):.0f}%) · p10 {diffs[len(diffs)//10]:+.1f} "
      f"p90 {diffs[9*len(diffs)//10]:+.1f}")
print(f"\nHoy: 18/20 positivo, media +2.3°F")

print("\n¿El ext_diff de la mañana predice el error final?")
for lo, hi, lab in [(-99, 0, "ext_diff < 0     "), (0, 1.5, "ext_diff 0 a +1.5"),
                    (1.5, 3, "ext_diff +1.5 a +3"), (3, 99, "ext_diff > +3    ")]:
    sub = [e for d, e in pairs if lo <= d < hi]
    if len(sub) < 5:
        print(f"  {lab}  N={len(sub):3d}  (N chico)")
        continue
    over = sum(1 for e in sub if e > 0)
    print(f"  {lab}  N={len(sub):3d}  error final mediano {statistics.median(sub):+.2f}°F"
          f"  · sobre-predijimos {100*over/len(sub):.0f}%")
