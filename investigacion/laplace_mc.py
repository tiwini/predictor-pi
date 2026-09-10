#!/usr/bin/env python3
"""Laplace multiclase: el pipeline entero, contra el actual.

Criterio pre-registrado en DECISIONES.md (commit 9e2b275), antes de esto:
  (a) el Brier NO empeora (Δ ≤ +0.0005 sobre los bins con settle)
  (b) la suma pasa a 1.000 ± 0.001

El cambio de escala es determinista: `p_mc = p_actual · (eff_n+2)/(eff_n+B)`
= ×33/37 con eff_n=31 y B=6. Así que los pares históricos se convierten al
vuelo y PAV se reajusta sobre ellos — sin migración destructiva.

**Comparación declarada**: los dos pipelines se evalúan SIN `blend_with_external`
ni `zero_impossible_bins`, porque los datos por bin del blend no están
persistidos. La omisión es la misma en los dos lados, así que aísla el efecto
del cambio de escala; no reproduce el `our_p_calibrated` publicado, que sí los
lleva, y por eso ése va aparte como referencia.
"""
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
import isotonic as iso  # noqa: E402
from stations import STATION_TZ  # noqa: E402

EFF_N = 31
B_BINS = 6
FACTOR = (EFF_N + 2) / (EFF_N + B_BINS)      # 33/37


def contiene(settle, lo, hi):
    LO = lo if abs(lo) < 1e8 else float("-inf")
    HI = hi if abs(hi) < 1e8 else float("inf")
    return 1 if (LO - 0.5) <= settle <= (HI + 0.5) else 0


def pares_entrenamiento():
    """La misma consulta que `fit_from_db`, para que el calibrador viejo del
    backtest sea el desplegado y no una versión parecida."""
    c = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    rows = c.execute("""
        SELECT predicted_p, outcome, date FROM prediction_snapshots ps1
        WHERE outcome IS NOT NULL AND op IN ('b')
          AND snapshot_time = (
            SELECT MAX(snapshot_time) FROM prediction_snapshots ps2
            WHERE ps2.station_id = ps1.station_id AND ps2.date = ps1.date
              AND ps2.op = ps1.op AND ps2.threshold = ps1.threshold
              AND ps2.outcome IS NOT NULL)
    """).fetchall()
    c.close()
    return rows


def main():
    rows = pares_entrenamiento()
    dias = len({r[2] for r in rows})
    viejo = iso.fit([(r[0], r[1]) for r in rows], n_days=dias)
    nuevo = iso.fit([(r[0] * FACTOR, r[1]) for r in rows], n_days=dias)
    print("# Laplace multiclase — pipeline entero\n")
    print(f"- Factor de escala: **×{FACTOR:.4f}** "
          f"(({EFF_N}+2)/({EFF_N}+{B_BINS}) = 33/37)")
    print(f"- Pares de entrenamiento: **{len(rows)}** sobre {dias} días")
    print(f"- PAV: {len(viejo.blocks)} bloques → {len(nuevo.blocks)} tras "
          f"reajustar\n")

    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(st, d): mx for st, d, mx in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE max_obs_f IS NOT NULL")}

    snaps = {}
    for st, ts, lo, hi, praw, pcal in an.execute(
            """SELECT station, ts, bin_lo, bin_hi, our_p, our_p_calibrated
               FROM kalshi_snapshots
               WHERE our_p IS NOT NULL AND our_p_calibrated IS NOT NULL"""):
        snaps.setdefault((st, ts), []).append((lo, hi, praw, pcal))

    b_act, b_new, b_pub = [], [], []
    s_act, s_new = [], []
    for (st, ts), bins in snaps.items():
        if st not in STATION_TZ or len(bins) < 3:
            continue
        p_act = [iso.apply(viejo, b[2]) for b in bins]
        p_new = [iso.apply(nuevo, b[2] * FACTOR) for b in bins]
        s_act.append(sum(p_act))
        s_new.append(sum(p_new))
        d = datetime.fromisoformat(ts).astimezone(
            ZoneInfo(STATION_TZ[st])).date().isoformat()
        settle = settles.get((st, d))
        if settle is None:
            continue
        for (lo, hi, praw, pcal), pa, pn in zip(bins, p_act, p_new):
            o = contiene(settle, lo, hi)
            b_act.append((pa - o) ** 2)
            b_new.append((pn - o) ** 2)
            b_pub.append((pcal - o) ** 2)

    m_act, m_new = statistics.mean(b_act), statistics.mean(b_new)
    print("## Brier (mismo dato, sin blend en ninguno de los dos)\n")
    print("| pipeline | N bins | Brier | Δ |")
    print("|---|---|---|---|")
    print(f"| actual (Laplace binario) | {len(b_act)} | {m_act:.4f} | — |")
    print(f"| multiclase | {len(b_new)} | {m_new:.4f} | {m_new - m_act:+.4f} |")
    print(f"| _referencia: `our_p_calibrated` publicado (con blend)_ | "
          f"{len(b_pub)} | {statistics.mean(b_pub):.4f} | _n/a_ |")

    print("\n## La suma por snapshot\n")
    print("| pipeline | mediana | p10 | p90 |")
    print("|---|---|---|---|")
    for nombre, v in (("actual", s_act), ("multiclase", s_new)):
        s = sorted(v)
        print(f"| {nombre} | {statistics.median(v):.4f} | "
              f"{s[int(0.1*len(s))]:.4f} | {s[int(0.9*len(s))]:.4f} |")

    # la suma del CRUDO, que es donde el axioma se cumple o no
    crudo_act = [sum(b[2] for b in bins) for bins in snaps.values()
                 if len(bins) >= 3]
    crudo_new = [x * FACTOR for x in crudo_act]
    print(f"\nSuma del **crudo**: actual "
          f"{statistics.median(crudo_act):.4f} → multiclase "
          f"{statistics.median(crudo_new):.4f}")

    print("\n## Criterio\n")
    ok_a = (m_new - m_act) <= 0.0005
    ok_b = abs(statistics.median(crudo_new) - 1.0) <= 0.001
    print(f"- (a) el Brier no empeora: Δ **{m_new - m_act:+.4f}** "
          f"(tolera +0.0005) {'✅' if ok_a else '❌'}")
    print(f"- (b) la suma cruda pasa a 1.000±0.001: "
          f"**{statistics.median(crudo_new):.4f}** {'✅' if ok_b else '❌'}")
    print()
    print("🟢 **ADOPTAR**" if (ok_a and ok_b) else "🔴 **RECHAZADO**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
