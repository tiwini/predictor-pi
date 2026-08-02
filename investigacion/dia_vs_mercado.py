#!/usr/bin/env python3
"""Cierre del día: ¿quién estuvo más cerca, el modelo o el mercado?

⚠ DEFECTO DE LA PRIMERA VERSIÓN, corregido el 2026-08-01
   Comparaba `ens_med` del último snapshot contra el CLI parcial. Pero el CLI
   **es el piso de `ens_med`**: cuando entra, la predicción queda clavada en él.
   El resultado era 14-1 a nuestro favor con |error| 0.00, y en 11 de 15 casos
   `modelo == ref` exactamente. No medía acierto, medía el clamp.

   Ahora compara la predicción del MEDIODÍA local —antes de que exista CLI— con
   el settle real del día. Eso sí es una pregunta legítima: con la información
   de mediodía, ¿quién estaba más cerca de lo que acabó pasando?

Uso:  ./venv/bin/python3 investigacion/dia_vs_mercado.py --settle 2026-07-31
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", required=True,
                    help="YYYY-MM-DD del día a evaluar (usa el settle real)")
    args = ap.parse_args()

    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    ref_map = {}
    if args.settle:
        cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
        ref_map = {r[0]: r[1] for r in cal.execute(
            "SELECT station_id, max_obs_f FROM day_outcomes WHERE date=?",
            (args.settle,))}

    # snapshot del MEDIODÍA local, antes de que exista CLI parcial
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    UTC = ZoneInfo("UTC")
    d = datetime.strptime(args.settle, "%Y-%m-%d").date()
    rows = []
    for st in STATION_TZ:
        tz = ZoneInfo(STATION_TZ[st])
        noon = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=12)
        lo = (noon - timedelta(minutes=90)).astimezone(UTC)
        hi = (noon + timedelta(minutes=90)).astimezone(UTC)
        r = an.execute(
            """SELECT * FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"),
             hi.strftime("%Y-%m-%dT%H:%M:%S"),
             noon.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is not None:
            rows.append(r)

    print(f"{'st':6s} {'ref':>7s} {'fnt':>7s} {'maxobs':>7s} {'modelo':>7s} "
          f"{'mercado':>8s} {'Δnos':>6s} {'Δmk':>6s}  mejor")
    us, mk_e, gu, gm = [], [], 0, 0
    for r in sorted(rows, key=lambda x: x["station"]):
        st = r["station"]
        if st not in STATION_TZ:
            continue
        ref, fnt = ref_map.get(st), "settle"
        if ref is None:
            continue
        bins = an.execute(
            """SELECT bin_lo, bin_hi, yes_mid FROM kalshi_snapshots
               WHERE station=? AND ts=(SELECT ts FROM kalshi_snapshots
                                       WHERE station=? AND ts<=?
                                       ORDER BY ts DESC LIMIT 1)
                 AND yes_mid IS NOT NULL""",
            (st, st, r["ts"])).fetchall()
        if not bins:
            continue
        b = max(bins, key=lambda x: x["yes_mid"])
        lo, hi = b["bin_lo"], b["bin_hi"]
        if abs(lo) < 1e8 and abs(hi) < 1e8:
            centro = (lo + hi) / 2
        else:
            centro = hi if abs(lo) > 1e8 else lo
        du = r["ens_med"] - ref
        dm = centro - ref
        us.append(abs(du))
        mk_e.append(abs(dm))
        if abs(du) < abs(dm) - 0.05:
            mejor, gu = "NOSOTROS", gu + 1
        elif abs(dm) < abs(du) - 0.05:
            mejor, gm = "mercado", gm + 1
        else:
            mejor = "="
        mo = r["today_max_obs"] if r["today_max_obs"] else float("nan")
        print(f"{st:6s} {ref:7.1f} {fnt:>7s} {mo:7.1f} {r['ens_med']:7.1f} "
              f"{centro:8.1f} {du:+6.1f} {dm:+6.1f}  {mejor}")

    if not us:
        print("\n  ninguna estación con referencia disponible todavía")
        return 0
    print(f"\n  con referencia: {len(us)}/20   mejor: nosotros {gu} · mercado {gm}")
    print(f"  |error| mediano   nosotros {statistics.median(us):.2f}"
          f"   mercado {statistics.median(mk_e):.2f}")
    print("  (predicción y precios del mediodía local; referencia = settle NWS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
