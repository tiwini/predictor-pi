"""¿Es sistemática la diferencia entre el máximo de nuestro feed de 5 min y el CLI?

DESCRIPTIVO, no decide nada — sirve para saber si el margen de 0.9°F está bien
puesto o si alguna estación corre por encima del CLI de forma estructural.

Lo que se espera POR CUANTIZACIÓN, antes de mirar el dato: el METAR codifica °C
enteros, así que "27°C" = 80.6°F representa cualquier °F en [79.7, 81.5]. El CLI
reporta °F enteros desde el ASOS de 1 min. Así que `max5 − settle` debería
repartirse alrededor de 0 con dispersión de ±0.9, SIN centro desplazado.

Dos sesgos conocidos que empujan en direcciones opuestas:
  · a la BAJA: `max5` se muestrea al ritmo del poller (3-10 min), así que se
    pierde picos que el feed sí publicó.
  · al ALZA: nada obvio — el CLI agrega ASOS de 1 min, que ve MÁS que nosotros.
Por eso, si el centro sale por encima de 0, es que algo más está pasando.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import stations

con = sqlite3.connect("analysis.db")
cal = sqlite3.connect("calibration.db")
settles = {(s, d): m for s, d, m in cal.execute(
    "SELECT station_id, date, max_obs_f FROM day_outcomes WHERE source='cli'")}

por_dia = defaultdict(lambda: None)
for sid, ts, cur in con.execute(
        "SELECT station, ts, current_f FROM station_snapshots WHERE ts >= '2026-07-01'"):
    if cur is None or cur < -900 or sid not in stations.STATION_TZ:
        continue
    d = datetime.fromisoformat(ts).astimezone(
        ZoneInfo(stations.STATION_TZ[sid])).date().isoformat()
    k = (sid, d)
    if por_dia[k] is None or cur > por_dia[k]:
        por_dia[k] = cur

difs, por_est = [], defaultdict(list)
for (sid, d), mx in por_dia.items():
    st = settles.get((sid, d))
    if st is None:
        continue
    difs.append(mx - st)
    por_est[sid].append(mx - st)

difs.sort()
n = len(difs)
print("N station-days con settle del CLI: %d" % n)
print()
print("=== distribucion de (max5min - settle) ===")
print("  media   %+.3f F" % (sum(difs) / n))
print("  mediana %+.3f F" % difs[n // 2])
for p in (5, 25, 50, 75, 95):
    print("  p%-3d    %+.3f F" % (p, difs[int(p / 100 * (n - 1))]))
print()
sobre = [d for d in difs if d > 0]
grande = [d for d in difs if d > 0.9]
print("  dias con max5 POR ENCIMA del settle : %d (%.1f%%)" % (len(sobre), 100 * len(sobre) / n))
print("  dias con exceso > 0.9 (el margen)   : %d (%.1f%%)" % (len(grande), 100 * len(grande) / n))
print()
print("=== mediana por estacion (ordenada) ===")
filas = []
for sid, v in por_est.items():
    v = sorted(v)
    filas.append((v[len(v) // 2], sid, len(v), sum(v) / len(v)))
filas.sort(reverse=True)
for med, sid, k, mean in filas:
    marca = "  <-- por encima" if med > 0 else ""
    print("  %-6s n=%3d  mediana %+.2f  media %+.2f%s" % (sid, k, med, mean, marca))
