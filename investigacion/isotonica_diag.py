#!/usr/bin/env python3
"""¿A qué hora aprende el calibrador isotónico, y a qué hora se le hace caso?

`_instrument_kalshi_bins` (calibration.py:448) graba como par de entrenamiento
el **último snapshot del día** de cada bin: `k.ts = MAX(k2.ts)`. A esa hora el
máximo ya está puesto y el piso ha aplastado la distribución, así que `our_p`
vale casi 0 o casi 1. El calibrador se aplica, en cambio, en TODOS los
snapshots — incluido el de media mañana, donde el día está abierto y las
probabilidades son intermedias.

Si el entrenamiento vive en los extremos, la parte del medio de la curva
—la única que se usa cuando la predicción todavía dice algo— está determinada
por casi ningún dato.

Mide tres cosas, sin tocar nada:
  1. a qué hora local caen los pares de entrenamiento y cómo se reparte su `p`
  2. el Brier de `our_p` (crudo) contra `our_p_calibrated`, a la hora de
     referencia (donde se aplica) y al final del día (donde se entrena)
  3. la curva de fiabilidad por tramo de p, a la hora de referencia
"""
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ, PEAK_HOURS  # noqa: E402

UTC = ZoneInfo("UTC")
VENTANA_MIN = 45


def contiene(settle, lo, hi):
    LO = lo if abs(lo) < 1e8 else float("-inf")
    HI = hi if abs(hi) < 1e8 else float("inf")
    return 1 if (LO - 0.5) <= settle <= (HI + 0.5) else 0


def main():
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    print("# La isotónica: dónde aprende y dónde se aplica\n")

    # ── 1. los pares de entrenamiento ────────────────────────────────────────
    print("## 1. Los pares con los que se entrena\n")
    filas = cal.execute(
        """SELECT station_id, date, snapshot_time, predicted_p, outcome
           FROM prediction_snapshots
           WHERE op='b' AND outcome IS NOT NULL""").fetchall()
    print(f"- Pares con outcome: **{len(filas)}**")
    if not filas:
        return 0
    horas, ps = [], []
    for st, d, sts, p, outc in filas:
        if st not in STATION_TZ:
            continue
        try:
            ts = datetime.fromisoformat(sts)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        horas.append(ts.astimezone(ZoneInfo(STATION_TZ[st])).hour)
        ps.append(p)
    if horas:
        from collections import Counter
        c = Counter(horas)
        print(f"- Hora local del par: mediana **{statistics.median(horas):.0f}h**, "
              f"rango {min(horas)}-{max(horas)}h")
        print("- Reparto por hora: "
              + ", ".join(f"{h}h={n}" for h, n in sorted(c.items())))
    extremos = sum(1 for p in ps if p <= 0.02 or p >= 0.98)
    medio = sum(1 for p in ps if 0.2 <= p <= 0.8)
    print(f"- De {len(ps)} pares: **{100*extremos/len(ps):.1f}%** están en los "
          f"extremos (p≤0.02 o p≥0.98) y sólo **{100*medio/len(ps):.1f}%** "
          f"caen en la zona media (0.2–0.8)")

    # ── 2. Brier crudo vs calibrado, a dos horas ─────────────────────────────
    print("\n## 2. ¿Mejora el calibrador el Brier, a la hora en que se usa?\n")
    print("| momento | N bins | Brier crudo | Brier calibrado | Δ |")
    print("|---|---|---|---|---|")
    for etiqueta, usar_ref in (("hora de referencia (se aplica)", True),
                               ("último del día (se entrena)", False)):
        crudo, calib = [], []
        for st in sorted(STATION_TZ):
            if st not in PEAK_HOURS:
                continue
            tz = ZoneInfo(STATION_TZ[st])
            ref_h = PEAK_HOURS[st][0] - 2
            for dia, settle in cal.execute(
                    "SELECT date, max_obs_f FROM day_outcomes WHERE "
                    "station_id=? AND max_obs_f IS NOT NULL", (st,)):
                d = datetime.strptime(dia, "%Y-%m-%d").date()
                fmt = "%Y-%m-%dT%H:%M:%S"
                if usar_ref:
                    ref = datetime.combine(d, datetime.min.time(), tz) \
                        + timedelta(hours=ref_h)
                    lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
                    hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
                    r = an.execute(
                        """SELECT ts FROM kalshi_snapshots WHERE station=?
                           AND ts>=? AND ts<=? AND our_p_calibrated IS NOT NULL
                           ORDER BY ABS(JULIANDAY(ts)-JULIANDAY(?)) LIMIT 1""",
                        (st, lo.strftime(fmt), hi.strftime(fmt),
                         ref.astimezone(UTC).strftime(fmt))).fetchone()
                else:
                    r = an.execute(
                        """SELECT MAX(ts) FROM kalshi_snapshots WHERE station=?
                           AND DATE(ts)=? AND our_p_calibrated IS NOT NULL""",
                        (st, dia)).fetchone()
                if not r or not r[0]:
                    continue
                for lo_b, hi_b, p_raw, p_cal in an.execute(
                        """SELECT bin_lo, bin_hi, our_p, our_p_calibrated
                           FROM kalshi_snapshots WHERE station=? AND ts=?
                           AND our_p IS NOT NULL AND our_p_calibrated IS NOT NULL""",
                        (st, r[0])):
                    o = contiene(settle, lo_b, hi_b)
                    crudo.append((p_raw - o) ** 2)
                    calib.append((p_cal - o) ** 2)
        if crudo:
            mc, mk = statistics.mean(crudo), statistics.mean(calib)
            print(f"| {etiqueta} | {len(crudo)} | {mc:.4f} | {mk:.4f} | "
                  f"{mk - mc:+.4f} {'🔴 empeora' if mk > mc else '🟢 mejora'} |")

    # ── 3. fiabilidad por tramo, a la hora de referencia ─────────────────────
    print("\n## 3. Fiabilidad por tramo de probabilidad (hora de referencia)\n")
    print("| tramo de our_p | N | frecuencia real | crudo dice | calibrado dice |")
    print("|---|---|---|---|---|")
    tramos = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8),
              (0.8, 0.9), (0.9, 1.01)]
    datos = {t: [] for t in tramos}
    for st in sorted(STATION_TZ):
        if st not in PEAK_HOURS:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        ref_h = PEAK_HOURS[st][0] - 2
        for dia, settle in cal.execute(
                "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
                "AND max_obs_f IS NOT NULL", (st,)):
            d = datetime.strptime(dia, "%Y-%m-%d").date()
            ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=ref_h)
            fmt = "%Y-%m-%dT%H:%M:%S"
            lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
            hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
            r = an.execute(
                """SELECT ts FROM kalshi_snapshots WHERE station=? AND ts>=?
                   AND ts<=? AND our_p_calibrated IS NOT NULL
                   ORDER BY ABS(JULIANDAY(ts)-JULIANDAY(?)) LIMIT 1""",
                (st, lo.strftime(fmt), hi.strftime(fmt),
                 ref.astimezone(UTC).strftime(fmt))).fetchone()
            if not r or not r[0]:
                continue
            for lo_b, hi_b, p_raw, p_cal in an.execute(
                    """SELECT bin_lo, bin_hi, our_p, our_p_calibrated
                       FROM kalshi_snapshots WHERE station=? AND ts=?
                       AND our_p IS NOT NULL""", (st, r[0])):
                o = contiene(settle, lo_b, hi_b)
                for t in tramos:
                    if t[0] <= p_raw < t[1]:
                        datos[t].append((p_raw, p_cal, o))
                        break
    for t in tramos:
        v = datos[t]
        if not v:
            continue
        print(f"| {t[0]:.1f}–{t[1]:.1f} | {len(v)} | "
              f"{statistics.mean([x[2] for x in v]):.3f} | "
              f"{statistics.mean([x[0] for x in v]):.3f} | "
              f"{statistics.mean([x[1] for x in v]):.3f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
