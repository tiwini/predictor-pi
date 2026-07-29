#!/usr/bin/env python3
"""Subida restante condicionada por la PENDIENTE reciente.

Por qué existe
--------------
El 2026-07-28 predije KDFW `102-103` y KATL `97-98` usando la mediana histórica
de subida restante a esa hora. Fallaron las dos: settlearon 100.0 y 94.0.

El error: esa mediana mezcla días en pleno ascenso con días ya aplanados. Ambas
llevaban **una hora sin subir** cuando predije, y la mediana seguía prometiendo
+2.2 y +1.4°F. Acabaron subiendo +1.0 y +0.1.

Este script separa los días históricos por la pendiente de la última hora y usa
sólo los comparables.

Uso:  ./venv/bin/python3 investigacion/subida_condicionada.py KMIA
      ... --hora 15.5        evalúa como si fueran las 15:30 local
      ... --fecha 2026-07-28 evalúa un día pasado (para validar)
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ   # noqa: E402

UTC = ZoneInfo("UTC")
PLANO_MAX = 0.5      # °F en la última hora: por debajo, la estación está plana
TOL_MIN = 25


def _snap(an, st, ref_utc, tol=TOL_MIN):
    lo = (ref_utc - timedelta(minutes=tol)).strftime("%Y-%m-%dT%H:%M:%S")
    hi = (ref_utc + timedelta(minutes=tol)).strftime("%Y-%m-%dT%H:%M:%S")
    return an.execute(
        """SELECT current_f, today_max_obs FROM station_snapshots
           WHERE station=? AND ts>=? AND ts<=? AND current_f IS NOT NULL
           ORDER BY ts LIMIT 1""", (st, lo, hi)).fetchone()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("station")
    ap.add_argument("--hora", type=float, default=None,
                    help="hora local decimal; por defecto, ahora")
    ap.add_argument("--fecha", default=None, help="YYYY-MM-DD para validar")
    args = ap.parse_args()
    st = args.station.upper()
    if st not in STATION_TZ:
        print(f"estación desconocida: {st}")
        return 1
    tz = ZoneInfo(STATION_TZ[st])

    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {d: m for d, m in cal.execute(
        "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=?", (st,))}

    hoy = (datetime.strptime(args.fecha, "%Y-%m-%d").date() if args.fecha
           else datetime.now(tz).date())
    if args.hora is not None:
        h = args.hora
    elif args.fecha:
        h = 15.0
    else:
        n = datetime.now(tz)
        h = n.hour + n.minute / 60

    # histórico: (delta hasta el settle, pendiente de la última hora)
    datos = []
    for day, se in sorted(settles.items()):
        if se is None or day >= hoy.isoformat():
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=h)
        a = _snap(an, st, ref.astimezone(UTC))
        b = _snap(an, st, (ref - timedelta(hours=1)).astimezone(UTC))
        if not a or not b:
            continue
        base = max(a[0], a[1] or -999)
        datos.append((se - base, a[0] - b[0]))

    # hoy
    ref = datetime.combine(hoy, datetime.min.time(), tz) + timedelta(hours=h)
    a = _snap(an, st, ref.astimezone(UTC))
    b = _snap(an, st, (ref - timedelta(hours=1)).astimezone(UTC))
    if not a or not b:
        print("sin snapshot de referencia para hoy")
        return 1
    base_hoy = max(a[0], a[1] or -999)
    pend_hoy = a[0] - b[0]

    plano = pend_hoy < PLANO_MAX
    print(f"{st} — {hoy} a las {int(h):02d}:{int(h % 1 * 60):02d} local")
    print(f"  base {base_hoy:.1f}   pendiente última hora {pend_hoy:+.1f}°F/h"
          f"   -> {'PLANO' if plano else 'SUBIENDO'}")

    comp = [d for d, p in datos if (p < PLANO_MAX) == plano]
    todos = [d for d, _ in datos]
    print(f"\n  días históricos: {len(todos)}   comparables (misma pendiente): "
          f"{len(comp)}")
    if todos:
        print(f"  subida restante SIN condicionar  mediana {statistics.median(todos):+.1f}°F"
              f"  -> settle ~{base_hoy + statistics.median(todos):.1f}")
    if len(comp) >= 5:
        m = statistics.median(comp)
        comp_s = sorted(comp)
        print(f"  subida restante CONDICIONADA     mediana {m:+.1f}°F"
              f"  -> settle ~{base_hoy + m:.1f}")
        print(f"    p10 {comp_s[len(comp_s)//10]:+.1f}  "
              f"p90 {comp_s[9*len(comp_s)//10]:+.1f}  "
              f"min {comp_s[0]:+.1f}  max {comp_s[-1]:+.1f}")
        print(f"\n  distribución del settle (días comparables):")
        vals = sorted(round(base_hoy + d) for d in comp)
        from collections import Counter
        for v, n in sorted(Counter(vals).items()):
            print(f"    {v:5.0f}°F  {'█' * n} {100*n/len(vals):.0f}%")
    else:
        print(f"  ⚠ sólo {len(comp)} días comparables: no alcanza para condicionar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
