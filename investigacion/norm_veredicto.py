#!/usr/bin/env python3
"""¿Normalizar la masa después de la isotónica? Criterio en el commit 4865bc7:
(a) el Brier mejora, (b) la suma pasa a 1.000±0.001, (c) la fiabilidad no
empeora más de 0.010 de error medio en los tramos con N≥200.
"""
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ  # noqa: E402

TRAMOS = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8),
          (0.8, 0.9), (0.9, 1.01)]
MIN_N_TRAMO = 200


def contiene(settle, lo, hi):
    LO = lo if abs(lo) < 1e8 else float("-inf")
    HI = hi if abs(hi) < 1e8 else float("inf")
    return 1 if (LO - 0.5) <= settle <= (HI + 0.5) else 0


def fiabilidad(pares):
    """[(p, outcome)] → error medio |lo que dice − frecuencia real| por tramo."""
    errs, detalle = [], []
    for t in TRAMOS:
        v = [x for x in pares if t[0] <= x[0] < t[1]]
        if len(v) < MIN_N_TRAMO:
            continue
        dice = statistics.mean([x[0] for x in v])
        real = statistics.mean([x[1] for x in v])
        errs.append(abs(dice - real))
        detalle.append((t, len(v), dice, real))
    return (statistics.mean(errs) if errs else float("nan")), detalle


def main():
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(st, d): mx for st, d, mx in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE max_obs_f IS NOT NULL")}

    snaps = {}
    for st, ts, lo, hi, pcal in an.execute(
            """SELECT station, ts, bin_lo, bin_hi, our_p_calibrated
               FROM kalshi_snapshots WHERE our_p_calibrated IS NOT NULL"""):
        snaps.setdefault((st, ts), []).append((lo, hi, pcal))

    b_act, b_norm, sumas = [], [], []
    par_act, par_norm = [], []
    for (st, ts), bins in snaps.items():
        if st not in STATION_TZ or len(bins) < 3:
            continue
        s = sum(b[2] for b in bins)
        if s <= 0:
            continue
        sumas.append(s)
        d = datetime.fromisoformat(ts).astimezone(
            ZoneInfo(STATION_TZ[st])).date().isoformat()
        settle = settles.get((st, d))
        if settle is None:
            continue
        for lo, hi, p in bins:
            o = contiene(settle, lo, hi)
            b_act.append((p - o) ** 2)
            b_norm.append((p / s - o) ** 2)
            par_act.append((p, o))
            par_norm.append((p / s, o))

    m_act, m_norm = statistics.mean(b_act), statistics.mean(b_norm)
    f_act, det_act = fiabilidad(par_act)
    f_norm, det_norm = fiabilidad(par_norm)

    print("# Normalizar después de la isotónica\n")
    print(f"Bins con settle: **{len(b_act)}**\n")
    print("| serie | Brier | suma mediana | error medio de fiabilidad |")
    print("|---|---|---|---|")
    print(f"| actual | {m_act:.4f} | {statistics.median(sumas):.4f} | "
          f"{f_act:.4f} |")
    print(f"| normalizada | {m_norm:.4f} | 1.0000 | {f_norm:.4f} |")

    print("\n## Fiabilidad por tramo\n")
    print("| tramo | N | dice ahora | real | dice normalizada | real |")
    print("|---|---|---|---|---|---|")
    dn = {t: (n, dice, real) for t, n, dice, real in det_norm}
    for t, n, dice, real in det_act:
        n2, d2, r2 = dn.get(t, (0, float("nan"), float("nan")))
        print(f"| {t[0]:.1f}–{t[1]:.1f} | {n} | {dice:.3f} | {real:.3f} | "
              f"{d2:.3f} | {r2:.3f} |")

    ok_a = m_norm < m_act
    ok_b = True   # normalizar da 1 por construcción
    ok_c = (f_norm - f_act) <= 0.010
    print("\n## Criterio\n")
    print(f"- (a) el Brier mejora: {m_act:.4f} → {m_norm:.4f} "
          f"({m_norm - m_act:+.4f}) {'✅' if ok_a else '❌'}")
    print(f"- (b) la suma pasa a 1.000: por construcción ✅")
    print(f"- (c) la fiabilidad no empeora >0.010: {f_act:.4f} → {f_norm:.4f} "
          f"({f_norm - f_act:+.4f}) {'✅' if ok_c else '❌'}")
    print()
    print("🟢 **ADOPTAR**" if (ok_a and ok_b and ok_c) else "🔴 **RECHAZADO**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
