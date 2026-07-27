"""¿A qué hora sale el PRIMER CLI de la tarde, y cuánto adelanta al que tenemos?

CLI_LATE_HOUR se pobló con el ÚLTIMO CLI del mismo día (el probe del 07-26
tomaba `same_day[-1]`). Pero varias estaciones emiten uno intermedio que ya
trae el max, y como el CLI se consume como PISO —sólo sube— tomar el primero
es estrictamente mejor: da piso antes y se actualiza si el siguiente trae más.
"""
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, "/home/popeye/predictor-pi/weather-predictor")
import nws_cli
from stations import STATION_TO_LOCATION, STATION_TZ, CLI_LATE_HOUR

HEADERS = {"User-Agent": nws_cli.UA, "Accept": "application/ld+json"}
print(f"{'st':6s} {'1er tarde':>10s} {'ultimo':>8s} {'CLI_LATE_HOUR':>14s} "
      f"{'adelanto':>9s}  1ero==final")
adelantos = []
for st in STATION_TO_LOCATION:
    tz = ZoneInfo(STATION_TZ[st])
    try:
        r = requests.get(f"{nws_cli.API}/products",
                         params={"type": "CLI", "location": STATION_TO_LOCATION[st],
                                 "limit": 16},
                         headers=HEADERS, timeout=25)
        items = r.json().get("@graph", [])
    except Exception:
        print(f"{st:6s} error")
        continue
    by_day = defaultdict(list)
    for it in items:
        pid, iss = it.get("id"), it.get("issuanceTime")
        if not (pid and iss):
            continue
        try:
            r2 = requests.get(f"{nws_cli.API}/products/{pid}", headers=HEADERS,
                              timeout=25)
            text = r2.json().get("productText", "")
        except Exception:
            continue
        d = nws_cli._parse_summary_date(text)
        mx = nws_cli._parse_max(text)
        if d is None or mx is None:
            continue
        loc = datetime.fromisoformat(iss).astimezone(tz)
        by_day[d].append((loc, mx))

    firsts, agree, tot = [], 0, 0
    for d, lst in by_day.items():
        lst.sort()
        same = [(t, m) for t, m in lst if t.date() == d and t.hour >= 12]
        final = [(t, m) for t, m in lst if t.date() > d]
        if not same:
            continue
        firsts.append(same[0][0].hour + same[0][1] * 0 + same[0][0].minute / 60)
        if final:
            tot += 1
            agree += abs(same[0][1] - final[-1][1]) < 0.01
    if not firsts:
        print(f"{st:6s} sin datos")
        continue
    f_med = statistics.median(firsts)
    cur = CLI_LATE_HOUR[st]
    adel = cur - f_med
    adelantos.append((adel, st))
    print(f"{st:6s} {int(f_med):02d}:{int(f_med%1*60):02d}      "
          f"{'':>6s} {cur:14.1f} {adel:8.1f}h  {agree}/{tot}")

print("\nmayores adelantos perdidos:")
for a, st in sorted(adelantos, reverse=True)[:8]:
    if a > 0.3:
        print(f"  {st}: el CLI llega {a:.1f}h antes de lo que dice la tabla")
