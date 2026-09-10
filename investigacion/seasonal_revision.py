#!/usr/bin/env python3
"""¿Sobrevive SEASONAL_OFFSET_F a la revisión que su propio comentario pidió?

`bias_tracker.SEASONAL_OFFSET_F` es una constante de julio (KLAS −1.70,
KPHX −1.55, KBOS −0.99) que se RESTA de la distribución en `predictor.py:1412`,
o sea que suma esos grados. Su comentario dice, literal:

    Revisar Sep-Oct si el sesgo residual > 0.5°F sobre 30 días — puede ser
    regime shift estacional que exige recalibrar.

Estamos en septiembre. Esto mide el sesgo residual POST-offset a la hora de
referencia de cada estación, con dos cortes que hacen falta para no confundir
la causa:

  · **test de signos** por día, porque una media de 30 días la puede mover un
    par de días raros; el proyecto ya decide así.
  · **si actuó el piso** (`obs_floor_n > 0`). El piso sólo empuja hacia
    ARRIBA: si el sesgo positivo apareciera sólo en los días en que mordió, la
    culpa no sería del offset y quitarlo no arreglaría nada.

Las 17 estaciones sin offset van como control: si el sesgo positivo es general,
tampoco es del offset.

Sólo lee.
"""
import math
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ, PEAK_HOURS  # noqa: E402
from bias_tracker import SEASONAL_OFFSET_F, SEASONAL_OFFSET_ACTIVE_SINCE  # noqa: E402

UTC = ZoneInfo("UTC")
VENTANA_MIN = 45


def p_signos(k, n):
    if n <= 0:
        return 1.0
    k = max(k, n - k)
    return 2.0 * sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def serie(an, cal, st):
    tz = ZoneInfo(STATION_TZ[st])
    ref_h = PEAK_HOURS[st][0] - 2
    out = []
    for dia, settle in cal.execute(
            "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
            "AND max_obs_f IS NOT NULL ORDER BY date", (st,)):
        d = datetime.strptime(dia, "%Y-%m-%d").date()
        ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=ref_h)
        lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
        hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
        fmt = "%Y-%m-%dT%H:%M:%S"
        r = an.execute(
            """SELECT ens_med, obs_floor_n FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts)-JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime(fmt), hi.strftime(fmt),
             ref.astimezone(UTC).strftime(fmt))).fetchone()
        if r is None:
            continue
        out.append({"dia": dia, "err": r[0] - settle,
                    "piso": bool(r[1] and r[1] > 0)})
    return out


def resumen(errs):
    if not errs:
        return None
    pos = sum(1 for e in errs if e > 0)
    return {"n": len(errs), "media": statistics.mean(errs),
            "mediana": statistics.median(errs), "pos": pos,
            "p": p_signos(pos, len(errs))}


def main():
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    print(f"# SEASONAL_OFFSET_F — revisión de septiembre\n")
    print(f"_offset activo desde {SEASONAL_OFFSET_ACTIVE_SINCE}; "
          f"generado {datetime.now().isoformat(timespec='seconds')}_\n")
    print("El offset se **resta** de la distribución, así que un valor negativo "
          "SUMA grados. `sesgo` es del `ens_med` publicado contra el settle: "
          "positivo = sobre-predecimos.\n")

    print("## Las tres con offset\n")
    print("| est | offset | efecto | N | sesgo residual | mediana | días + | "
          "signos p | ¿mismo signo que el empuje? |")
    print("|---|---|---|---|---|---|---|---|---|")
    detalle = {}
    for st, off in sorted(SEASONAL_OFFSET_F.items()):
        s = serie(an, cal, st)
        detalle[st] = s
        r = resumen([x["err"] for x in s])
        if r is None:
            print(f"| {st} | {off:+.2f} | — | sin datos |")
            continue
        empuje = -off                       # lo que suma de verdad
        mismo = (empuje > 0 and r["media"] > 0) or (empuje < 0 and r["media"] < 0)
        print(f"| {st} | {off:+.2f} | {empuje:+.2f}°F | {r['n']} | "
              f"{r['media']:+.2f}°F | {r['mediana']:+.2f} | "
              f"{r['pos']}/{r['n']} | {r['p']:.4f} | "
              f"{'🔴 SÍ — empuja hacia el error' if mismo else '🟢 no'} |")

    print("\n## ¿Es el piso el que produce el sesgo?\n")
    print("| est | días con piso | sesgo esos días | días sin piso | "
          "sesgo esos días |")
    print("|---|---|---|---|---|")
    for st in sorted(SEASONAL_OFFSET_F):
        s = detalle.get(st) or []
        con = resumen([x["err"] for x in s if x["piso"]])
        sin = resumen([x["err"] for x in s if not x["piso"]])
        print(f"| {st} | {con['n'] if con else 0} | "
              f"{con['media']:+.2f}°F" if con else f"| {st} | 0 | — ", end="")
        print(f" | {sin['n'] if sin else 0} | "
              + (f"{sin['media']:+.2f}°F |" if sin else "— |"))

    print("\n## Control: las que NO llevan offset\n")
    ctrl = []
    for st in sorted(STATION_TZ):
        if st in SEASONAL_OFFSET_F or st not in PEAK_HOURS:
            continue
        r = resumen([x["err"] for x in serie(an, cal, st)])
        if r:
            ctrl.append((st, r))
    if ctrl:
        medias = [r["media"] for _, r in ctrl]
        print(f"{len(ctrl)} estaciones · sesgo medio **{statistics.mean(medias):+.2f}°F** "
              f"· mediana {statistics.median(medias):+.2f}°F · "
              f"rango {min(medias):+.2f} a {max(medias):+.2f}")
        print("\n| est | N | sesgo | días + |")
        print("|---|---|---|---|")
        for st, r in sorted(ctrl, key=lambda x: -x[1]["media"]):
            print(f"| {st} | {r['n']} | {r['media']:+.2f}°F | "
                  f"{r['pos']}/{r['n']} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
