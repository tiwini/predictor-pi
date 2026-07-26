#!/usr/bin/env python3
"""Backfill de `day_outcomes` con el CF6 mensual, para las 20 estaciones.

Los días que perdieron la ventana del CLI (~2-3 días en /products) quedaban
huérfanos: 301 en julio 2026, y sin settle no son backtesteables. El CF6 los
recupera con 2 requests por estación.

Por defecto NO instrumenta pares de Kalshi ni refitea la isotónica: esto
rellena datos, no cambia el modelo. Ver calibration._record_settle.

Uso:  python3 backfill_cf6.py [YYYY-MM] [--apply] [--instrument]
Sin --apply es dry-run: lista qué escribiría y no toca la DB.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/popeye/predictor-pi/weather-predictor")
import calibration  # noqa: E402
import nws_cli  # noqa: E402
from predictor import Station  # noqa: E402
from stations import STATION_IDS, STATION_TZ  # noqa: E402


def _station(sid: str) -> Station:
    """settle/backfill sólo usan .id y .tz; lat/lon son para el fallback de
    Open-Meteo, que aquí no se usa."""
    return Station(id=sid, name=sid, lat=0.0, lon=0.0,
                   tz=ZoneInfo(STATION_TZ[sid]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("month", nargs="?",
                    default=date.today().strftime("%Y-%m"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--instrument", action="store_true",
                    help="además alimenta pares de isotónica (decisión aparte)")
    args = ap.parse_args()
    year, month = int(args.month[:4]), int(args.month[5:7])

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode} · {args.month} · instrument={args.instrument}\n")

    con = calibration._conn()
    have = {(r[0], r[1]) for r in con.execute(
        "SELECT station_id, date FROM day_outcomes WHERE max_obs_f IS NOT NULL")}
    con.close()

    total = 0
    for sid in STATION_IDS:
        st = _station(sid)
        today = datetime.now(st.tz).date()
        if args.apply:
            written = calibration.backfill_month_cf6(
                st, year, month, instrument=args.instrument)
            days = [d.isoformat() for d, _ in written]
        else:
            got = nws_cli.fetch_month_extremes(sid, year, month)
            days = [ds for ds, (mx, _) in sorted(got.items())
                    if mx is not None and date.fromisoformat(ds) < today
                    and (sid, ds) not in have]
        total += len(days)
        if days:
            print(f"{sid}: {len(days)} días  {days[0]} → {days[-1]}")
        else:
            print(f"{sid}: nada que escribir")

    print(f"\nTotal: {total} días {'escritos' if args.apply else 'a escribir'}")
    if not args.apply:
        print("Correr con --apply para aplicar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
