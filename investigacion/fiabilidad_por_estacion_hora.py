#!/usr/bin/env python3
"""¿Qué estaciones son fiables y desde qué hora acertamos?

Dos preguntas distintas y las dos importan:

  1. |error| en °F  — cómo de cerca queda la predicción del settle.
  2. ACIERTO DE BIN — si el bin de Kalshi que contiene nuestra predicción a esa
     hora acaba siendo el ganador. Ésta es la que se opera: un error de 0.6°F
     puede perder el bin y uno de 1.4°F ganarlo, según dónde caiga el corte.

Se usa `ens_med` publicado (lo que el sistema dice a esa hora, ya con corrector
donde aplica) contra el settle real del NWS CLI.

Ventana: los últimos N días con settle. NO es un backtest de hipótesis, es una
foto operativa de qué mirar y cuándo.

Uso:  ./venv/bin/python3 ../investigacion/fiabilidad_por_estacion_hora.py [dias]
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ, PEAK_HOURS   # noqa: E402

HORAS = list(range(8, 20))


def dentro(lo, hi, v) -> bool:
    return (((lo - 0.5) <= v if abs(lo) < 1e8 else True)
            and (v <= (hi + 0.5) if abs(hi) < 1e8 else True))


def main() -> int:
    dias_n = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    dias = [d for (d,) in cal.execute(
        "SELECT DISTINCT date FROM day_outcomes WHERE max_obs_f IS NOT NULL "
        "ORDER BY date DESC LIMIT ?", (dias_n,))]
    dias.sort()
    if not dias:
        print("sin settles")
        return 1
    settles = {(s, d): m for s, d, m in cal.execute(
        f"SELECT station_id, date, max_obs_f FROM day_outcomes "
        f"WHERE date >= ? AND max_obs_f IS NOT NULL", (dias[0],))}

    print(f"ventana: {dias[0]} .. {dias[-1]}  ({len(dias)} días)\n")

    resumen = []
    detalle: dict[str, dict[int, tuple[int, int]]] = {}
    for st in sorted(STATION_TZ):
        tz = ZoneInfo(STATION_TZ[st])
        errs, por_hora = [], {}
        for h in HORAS:
            ok = n = 0
            for d in dias:
                s = settles.get((st, d))
                if s is None:
                    continue
                ref = (datetime.combine(datetime.strptime(d, "%Y-%m-%d").date(),
                                        datetime.min.time(), tz)
                       + timedelta(hours=h))
                a = (ref - timedelta(minutes=20)).astimezone(timezone.utc)
                b = (ref + timedelta(minutes=20)).astimezone(timezone.utc)
                r = an.execute(
                    """SELECT ts, ens_med FROM station_snapshots
                       WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
                       ORDER BY ts LIMIT 1""",
                    (st, a.strftime("%Y-%m-%dT%H:%M:%S"),
                     b.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
                if r is None:
                    continue
                if h == 12:
                    errs.append(abs(r["ens_med"] - s))
                bins = an.execute(
                    """SELECT bin_lo, bin_hi FROM kalshi_snapshots
                       WHERE station=? AND ts=(SELECT ts FROM kalshi_snapshots
                             WHERE station=? AND ts<=? ORDER BY ts DESC LIMIT 1)""",
                    (st, st, r["ts"])).fetchall()
                nuestro = [x for x in bins
                           if dentro(x["bin_lo"], x["bin_hi"], r["ens_med"])]
                if not nuestro:
                    continue
                n += 1
                if dentro(nuestro[0]["bin_lo"], nuestro[0]["bin_hi"], s):
                    ok += 1
            if n:
                por_hora[h] = (ok, n)
        detalle[st] = por_hora
        if errs and por_hora:
            tot_ok = sum(o for o, _ in por_hora.values())
            tot_n = sum(nn for _, nn in por_hora.values())
            resumen.append((st, statistics.median(errs), tot_ok / tot_n, tot_n))

    print("RANKING de fiabilidad (|error| a mediodía local, y acierto de bin)")
    print(f"  {'st':6s} {'|err| 12h':>10s} {'acierto bin':>12s} {'N':>5s}   fiabilidad")
    for st, e, acc, n in sorted(resumen, key=lambda x: x[1]):
        barra = "#" * int(acc * 24)
        print(f"  {st:6s} {e:9.2f}° {100*acc:11.0f}% {n:5d}   {barra}")

    print("\n\nACIERTO DE BIN POR HORA LOCAL  (·<40%  ▪40-60%  ▮60-75%  █>75%)")
    print(f"  {'st':6s} " + " ".join(f"{h:>3d}" for h in HORAS) + "   pico")
    for st, _, _, _ in sorted(resumen, key=lambda x: x[1]):
        fila = []
        for h in HORAS:
            if h not in detalle[st]:
                fila.append("  .")
                continue
            ok, n = detalle[st][h]
            p = ok / n
            fila.append("  ·" if p < .40 else "  ▪" if p < .60
                        else "  ▮" if p < .75 else "  █")
        lo, hi = PEAK_HOURS[st]
        print(f"  {st:6s} " + " ".join(fila) + f"   {lo}-{hi}h")

    print("\n\n¿DESDE QUÉ HORA es fiable cada estación? "
          "(primera hora con ≥70% y que se sostiene)")
    print(f"  {'st':6s} {'desde':>7s}  {'acierto a esa hora':>19s}")
    nunca = []
    for st, _, _, _ in sorted(resumen, key=lambda x: x[1]):
        hs = sorted(detalle[st])
        encontrada = None
        for i, h in enumerate(hs):
            ok, n = detalle[st][h]
            if n < 3 or ok / n < 0.70:
                continue
            resto = [detalle[st][x] for x in hs[i:] if detalle[st][x][1] >= 3]
            if resto and all(o / nn >= 0.60 for o, nn in resto):
                encontrada = (h, ok / n, n)
                break
        if encontrada:
            h, p, n = encontrada
            print(f"  {st:6s} {h:6d}h  {100*p:17.0f}%  (N={n})")
        else:
            nunca.append(st)
    if nunca:
        print(f"\n  NUNCA sostienen 70%: {', '.join(nunca)}")
        print("  (en éstas la hora no arregla nada — o no operar, o esperar "
              "al CLI parcial)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
