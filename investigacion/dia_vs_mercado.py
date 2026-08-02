#!/usr/bin/env python3
"""Cierre del día: ¿quién estuvo más cerca, el modelo o el mercado?

Referencia = CLI parcial (piso duro, iguala el settle el 91% de los días). Se
compara contra `ens_med` y contra el centro del bin favorito del mercado.

Pensado para correrlo al final de la tarde, cuando ya hay CLI en buena parte del
roster. Con `--settle` usa `day_outcomes` en vez del CLI, para días ya cerrados.

Uso:  ./venv/bin/python3 investigacion/dia_vs_mercado.py
      ... --settle 2026-07-31
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
    ap.add_argument("--settle", default=None,
                    help="YYYY-MM-DD: usa el settle real en vez del CLI parcial")
    args = ap.parse_args()

    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    ref_map = {}
    if args.settle:
        cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
        ref_map = {r[0]: r[1] for r in cal.execute(
            "SELECT station_id, max_obs_f FROM day_outcomes WHERE date=?",
            (args.settle,))}

    rows = an.execute(
        """SELECT * FROM station_snapshots s
           WHERE ts = (SELECT MAX(ts) FROM station_snapshots
                       WHERE station = s.station)""").fetchall()

    print(f"{'st':6s} {'ref':>7s} {'fnt':>7s} {'maxobs':>7s} {'modelo':>7s} "
          f"{'mercado':>8s} {'Δnos':>6s} {'Δmk':>6s}  mejor")
    us, mk_e, gu, gm = [], [], 0, 0
    for r in sorted(rows, key=lambda x: x["station"]):
        st = r["station"]
        if st not in STATION_TZ:
            continue
        if args.settle:
            ref, fnt = ref_map.get(st), "settle"
        else:
            ref, fnt = r["today_max_cli"], "CLI"
        if ref is None:
            continue
        bins = an.execute(
            """SELECT bin_lo, bin_hi, yes_mid FROM kalshi_snapshots
               WHERE station=? AND ts=(SELECT MAX(ts) FROM kalshi_snapshots
                                       WHERE station=?) AND yes_mid IS NOT NULL""",
            (st, st)).fetchall()
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
    sobre = sum(1 for x in us if x > 0)
    print(f"  ⚠ ojo: donde el clamp del piso ata la predicción a la observación,"
          f" Δnos sale 0.0 por construcción, no por acierto")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
