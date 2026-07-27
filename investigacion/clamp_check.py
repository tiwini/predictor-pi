#!/usr/bin/env python3
"""¿Predecimos por debajo del máximo que ya observamos?

Si a las 18h local el pico del día ya ocurrió (convergencia_horaria tabla B da
100% en casi todas las estaciones) y además el settle del CLI queda SIEMPRE por
encima de nuestra obs (gap obs→CLI mediana −1.0°F), entonces una predicción por
debajo de `today_max_obs` es un error garantizado, no una apuesta.

Salida: por estación y por hora local, % de celdas con pred < max_obs y el
déficit mediano. Read-only.

Uso:  python3 clamp_check.py [--pred cal|ens] [--margin 0.5]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/popeye/predictor-pi/weather-predictor")
from stations import STATION_TZ  # noqa: E402

ANALYSIS_DB = "/home/popeye/predictor-pi/weather-predictor/analysis.db"
CALIB_DB = "/home/popeye/predictor-pi/weather-predictor/calibration.db"
HOURS = list(range(13, 21))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", choices=("ens", "iso"), default="ens")
    ap.add_argument("--margin", type=float, default=0.5)
    args = ap.parse_args()
    col = "ens_med" if args.pred == "ens" else "pred_iso_med_f"

    con = sqlite3.connect(f"file:{CALIB_DB}?mode=ro", uri=True, timeout=30)
    settles = {(s, d): m for s, d, m in con.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE max_obs_f IS NOT NULL")}
    con.close()

    con = sqlite3.connect(f"file:{ANALYSIS_DB}?mode=ro", uri=True, timeout=30)
    rows = con.execute(
        f"SELECT station, ts, {col}, today_max_obs FROM station_snapshots "
        f"WHERE {col} IS NOT NULL AND today_max_obs IS NOT NULL "
        f"  AND today_max_obs > -900").fetchall()
    con.close()

    latest: dict = {}
    for station, ts_s, pred, max_obs in rows:
        tzname = STATION_TZ.get(station)
        if tzname is None:
            continue
        try:
            loc = datetime.fromisoformat(ts_s).astimezone(ZoneInfo(tzname))
        except ValueError:
            continue
        if loc.hour not in HOURS:
            continue
        key = (station, loc.date().isoformat(), loc.hour)
        prev = latest.get(key)
        if prev is None or loc > prev[0]:
            latest[key] = (loc, pred, max_obs)

    per_st: dict = defaultdict(list)
    per_hour: dict = defaultdict(list)
    settle_confirms = defaultdict(lambda: [0, 0])
    for (st, day, hour), (_, pred, max_obs) in latest.items():
        deficit = max_obs - pred          # >0 => predecimos por debajo del max
        under = deficit > args.margin
        per_st[st].append(deficit)
        per_hour[(st, hour)].append(deficit)
        if under:
            s = settles.get((st, day))
            if s is not None:
                settle_confirms[st][1] += 1
                if s >= max_obs - 0.05:
                    settle_confirms[st][0] += 1

    print(f"Predicción: {col} · 'bajo' = pred < today_max_obs − {args.margin}°F")
    print(f"Celdas (estación × día × hora local {HOURS[0]}-{HOURS[-1]}): "
          f"{len(latest)}\n")

    head = "  ".join(f"{h:02d}" for h in HOURS)
    print(f"{'STA':5} {'N':>4}  {'%bajo':>6}  {'déficit_med':>11}   {head}")
    print(f"{'':5} {'':>4}  {'':>6}  {'':>11}   " + "  ".join("--" for _ in HOURS))
    order = sorted(per_st, key=lambda s: -sum(
        1 for d in per_st[s] if d > args.margin) / max(len(per_st[s]), 1))
    for st in order:
        vals = per_st[st]
        n_under = sum(1 for d in vals if d > args.margin)
        unders = [d for d in vals if d > args.margin]
        cells = []
        for h in HOURS:
            v = per_hour.get((st, h))
            if not v:
                cells.append(" ·")
                continue
            pct = sum(1 for d in v if d > args.margin) / len(v) * 100
            cells.append(f"{pct:2.0f}")
        med = statistics.median(unders) if unders else 0.0
        print(f"{st:5} {len(vals):>4}  {n_under / len(vals) * 100:5.0f}%  "
              f"{med:+10.1f}°   " + "  ".join(cells))

    print("\nDe las celdas 'bajo', ¿el settle del CLI terminó en o sobre el max\n"
          "que ya teníamos observado? (si sí, predecir por debajo era error seguro)")
    for st in order:
        ok, tot = settle_confirms[st]
        if tot:
            print(f"  {st:5} {ok:>4}/{tot:<4} ({ok / tot * 100:3.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
