"""¿Cuántas estaciones tienen la serie de obs aceptadas parada, y cuánto
max_obs se está perdiendo frente al feed de 5 minutos?"""
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, "/home/popeye/predictor-pi/weather-predictor")
from stations import STATION_IDS

UA = "weather-predictor/0.1 (educational; contact=local)"
now = datetime.now(timezone.utc)
print(f"{'st':6s} {'ult.METAR':>10s} {'edad':>6s} {'max METAR':>10s} "
      f"{'max 5-min':>10s} {'gap':>7s}  raw")
peor = []
for st in STATION_IDS:
    try:
        r = requests.get(f"https://api.weather.gov/stations/{st}/observations",
                         params={"limit": 40}, headers={"User-Agent": UA},
                         timeout=20)
        feats = r.json().get("features", [])
    except Exception as e:
        print(f"{st:6s} error {e}")
        continue
    metar, cinco = [], []
    n_raw = 0
    for f in feats:
        p = f.get("properties", {})
        t = p.get("timestamp", "")
        tc = (p.get("temperature") or {}).get("value")
        if tc is None or not t:
            continue
        try:
            dt = datetime.fromisoformat(t)
        except Exception:
            continue
        if dt.date() != now.date():
            continue
        tf = tc * 9 / 5 + 32
        if p.get("rawMessage"):
            n_raw += 1
        if dt.minute % 5 != 0:
            metar.append((dt, tf))
        else:
            cinco.append((dt, tf))
    if not metar and not cinco:
        print(f"{st:6s} (sin obs de hoy)")
        continue
    last_m = max(metar)[0] if metar else None
    age = (now - last_m).total_seconds() / 60 if last_m else 999
    mx_m = max(v for _, v in metar) if metar else None
    mx_5 = max(v for _, v in cinco) if cinco else None
    gap = (mx_5 - mx_m) if (mx_m is not None and mx_5 is not None) else None
    flag = "⚠" if age > 90 else " "
    print(f"{st:6s} {last_m.strftime('%H:%M') if last_m else '—':>10s} "
          f"{age:5.0f}m{flag} "
          f"{mx_m if mx_m is not None else float('nan'):10.1f} "
          f"{mx_5 if mx_5 is not None else float('nan'):10.1f} "
          f"{gap if gap is not None else float('nan'):+7.1f}  {n_raw}/{len(feats)}")
    if gap is not None and gap >= 1.0:
        peor.append((gap, st))
print()
for g, st in sorted(peor, reverse=True):
    print(f"  {st}: el feed de 5-min vio {g:+.1f}°F más que el METAR aceptado")
