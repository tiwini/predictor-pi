"""One-shot: rankea estaciones Kalshi por 'predecibilidad' climatológica.

Métrica: std (y IQR) del max diario histórico en ventana ±7 días alrededor
de hoy, sobre 30 años. Std bajo = clima estable en esta época = modelo
ensemble acierta más en promedio.
"""
import sqlite3
from datetime import date
from statistics import stdev, median

from kalshi import STATION_TO_SERIES
from predictor import fetch_station
from climatology import ensure_cache, _conn

TODAY = date.today()
WINDOW = 7

# Dedupe: keep unique series (KORD covers KMDW, KIAH covers KHOU, etc.)
seen_series = set()
unique_stations = []
for sid, series in STATION_TO_SERIES.items():
    if series is None:
        continue
    if series in seen_series:
        continue
    seen_series.add(series)
    unique_stations.append(sid)

print(f"Analizando {len(unique_stations)} estaciones. "
      f"Ventana: ±{WINDOW} días alrededor de {TODAY}.")
print()

target_ref = date(2000, TODAY.month, TODAY.day)
results = []

for sid in unique_stations:
    try:
        station = fetch_station(sid)
        ensure_cache(station)
    except Exception as e:
        print(f"  {sid}: ERROR fetching/caching: {e}")
        continue

    c = _conn()
    cur = c.execute(
        "SELECT date, temp_max_f FROM climatology WHERE station_id=?",
        (station.id,))
    vals = []
    years = set()
    for dstr, v in cur:
        try:
            y, m, d = (int(x) for x in dstr.split("-"))
        except ValueError:
            continue
        ref = date(2000, m, d)
        diff = abs((ref - target_ref).days)
        if diff > 183:
            diff = 365 - diff
        if diff <= WINDOW:
            vals.append(v)
            years.add(y)
    c.close()

    if len(vals) < 10:
        print(f"  {sid}: pocos datos ({len(vals)})")
        continue

    vals.sort()
    n = len(vals)
    p25 = vals[n // 4]
    p75 = vals[3 * n // 4]
    iqr = p75 - p25
    sd = stdev(vals)
    med = median(vals)
    results.append({
        "station": sid,
        "name": station.name,
        "n": n,
        "years": f"{min(years)}-{max(years)}",
        "median": med,
        "std": sd,
        "iqr": iqr,
        "p10": vals[int(n * 0.1)],
        "p90": vals[int(n * 0.9)],
        "range_p10_p90": vals[int(n * 0.9)] - vals[int(n * 0.1)],
    })
    print(f"  {sid}: n={n}, std={sd:.2f}°F, mediana={med:.1f}°F")

print()
print("=" * 72)
print(f"RANKING — Top 6 más predecibles (std más bajo) alrededor de {TODAY}:")
print("=" * 72)
results.sort(key=lambda r: r["std"])
print(f"{'rank':<4} {'station':<8} {'nombre':<35} {'std':>6} {'mediana':>8} {'p10-p90':>8}")
for i, r in enumerate(results, 1):
    marker = "  ★" if i <= 6 else "   "
    print(f"{marker}{i:<4} {r['station']:<8} {r['name'][:35]:<35} "
          f"{r['std']:>5.2f}°F {r['median']:>6.1f}°F "
          f"{r['range_p10_p90']:>6.1f}°F")
