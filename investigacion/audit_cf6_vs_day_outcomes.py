#!/usr/bin/env python3
"""Audita `day_outcomes` contra el CF6 (F-6) mensual del mismo NWS.

Hasta 2026-07-26 no había forma de revisar un settle ya guardado: el CLI
desaparece de /products en ~2-3 días. El CF6 trae el mes entero, así que
permite contestar "¿lo que guardamos coincide con el registro oficial?".

Motivación: KPHX 2026-07-25 quedó con min 97.0 cuando el CF6 dice 95.0 — y
97.0 es exactamente el min del día 24, o sea el settle pudo haber leído un
producto preliminar (o el del día anterior). Un caso no dice si es sistemático.

Read-only sobre la DB. Uso:  python3 audit_cf6_vs_day_outcomes.py [YYYY-MM]
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date

sys.path.insert(0, "/home/popeye/predictor-pi/weather-predictor")
import nws_cli  # noqa: E402
from stations import STATION_IDS  # noqa: E402

CALIB_DB = "/home/popeye/predictor-pi/weather-predictor/calibration.db"


def main() -> int:
    ym = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y-%m")
    year, month = int(ym[:4]), int(ym[5:7])

    con = sqlite3.connect(f"file:{CALIB_DB}?mode=ro", uri=True, timeout=30)
    rows = con.execute(
        "SELECT station_id, date, max_obs_f, min_obs_f, source FROM day_outcomes "
        "WHERE date LIKE ? ORDER BY station_id, date", (f"{ym}-%",)).fetchall()
    con.close()

    have: dict[str, dict[str, tuple]] = {}
    for sid, d, mx, mn, src in rows:
        have.setdefault(sid, {})[d] = (mx, mn, src or "cli")

    n_ok = n_max = n_min = n_missing = 0
    print(f"Auditoría {ym} — day_outcomes vs CF6 mensual\n")
    for sid in STATION_IDS:
        cf6 = nws_cli.fetch_month_extremes(sid, year, month)
        if not cf6:
            print(f"{sid}: CF6 no disponible")
            continue
        mine = have.get(sid, {})
        lines = []
        for d in sorted(cf6):
            ref_mx, ref_mn = cf6[d]
            if ref_mx is None:
                continue          # día en curso / missing en el F-6
            if d not in mine:
                n_missing += 1
                lines.append(f"    {d}  FALTA en day_outcomes (CF6 {ref_mx:.0f})")
                continue
            mx, mn, src = mine[d]
            bad_max = mx is not None and abs(mx - ref_mx) > 0.01
            bad_min = (mn is not None and ref_mn is not None
                       and abs(mn - ref_mn) > 0.01)
            if bad_max:
                n_max += 1
            if bad_min:
                n_min += 1
            if bad_max or bad_min:
                lines.append(
                    f"    {sid} {d}  max {mx} vs CF6 {ref_mx}"
                    f"{'  <-- MAX' if bad_max else ''}"
                    f" · min {mn} vs CF6 {ref_mn}"
                    f"{'  <-- min' if bad_min else ''}  [{src}]")
            else:
                n_ok += 1
        print(f"{sid}: {len(mine)} días guardados, {len(cf6)} en el CF6"
              + (":" if lines else " — todo cuadra"))
        for ln in lines:
            print(ln)

    print(f"\nTotales: {n_ok} cuadran · {n_max} con MAX distinto · "
          f"{n_min} con min distinto · {n_missing} faltan en day_outcomes")
    print("El MAX es lo que liquida Kalshi; una discrepancia ahí es grave.\n"
          "El min sólo alimenta F8. CF6 es PRELIMINARY: si difiere del CLI\n"
          "guardado, el sospechoso es el prelim vs final, no el CF6.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
