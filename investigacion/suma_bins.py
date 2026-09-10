#!/usr/bin/env python3
"""Las probabilidades por bin, ¿cuánto se alejan de sumar 1?

Los bins de Kalshi son mutuamente excluyentes y exhaustivos (los de los
extremos son colas), así que la suma DEBERÍA valer 1. `our_p_for_bin` la
respeta —es la fracción de miembros del ensemble en cada bin— pero después:

  · la isotónica se aplica **bin a bin**, y una transformación monótona
    aplicada por separado a cada p no conserva la suma;
  · `blend_with_external` mezcla cada bin con una gaussiana externa;
  · `zero_impossible_bins` redistribuye la masa muerta **preservando la suma**
    en vez de normalizar — su docstring lo dice y lo justifica con que «la suma
    calibrada no vale 1 de todos modos».

Esto mide cuánto vale de verdad, y si normalizar mejoraría el Brier o lo
empeoraría. El resultado puede caer de los dos lados: si la suma ya ronda 1,
no hay nada que arreglar.
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


def contiene(settle, lo, hi):
    LO = lo if abs(lo) < 1e8 else float("-inf")
    HI = hi if abs(hi) < 1e8 else float("inf")
    return 1 if (LO - 0.5) <= settle <= (HI + 0.5) else 0


def pct(v, q):
    s = sorted(v)
    return s[min(len(s) - 1, int(q * len(s)))]


def main():
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(st, d): mx for st, d, mx in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE max_obs_f IS NOT NULL")}

    # snapshot → lista de (lo, hi, p_raw, p_cal)
    snaps = {}
    for st, ts, lo, hi, praw, pcal in an.execute(
            """SELECT station, ts, bin_lo, bin_hi, our_p, our_p_calibrated
               FROM kalshi_snapshots
               WHERE our_p IS NOT NULL AND our_p_calibrated IS NOT NULL"""):
        snaps.setdefault((st, ts), []).append((lo, hi, praw, pcal))

    sum_raw, sum_cal, por_hora = [], [], {}
    for (st, ts), bins in snaps.items():
        if len(bins) < 3:
            continue
        sr = sum(b[2] for b in bins)
        sc = sum(b[3] for b in bins)
        sum_raw.append(sr)
        sum_cal.append(sc)
        if st in STATION_TZ:
            h = datetime.fromisoformat(ts).astimezone(ZoneInfo(STATION_TZ[st])).hour
            por_hora.setdefault(h, []).append(sc)

    print("# ¿Suman 1 las probabilidades por bin?\n")
    print(f"Snapshots con ≥3 bins: **{len(sum_raw)}**\n")
    print("| serie | mediana | p10 | p90 | mín | máx | % dentro de ±0.05 |")
    print("|---|---|---|---|---|---|---|")
    for nombre, v in (("our_p crudo", sum_raw), ("our_p calibrado", sum_cal)):
        dentro = 100.0 * sum(1 for x in v if abs(x - 1.0) <= 0.05) / len(v)
        print(f"| {nombre} | {statistics.median(v):.3f} | {pct(v, 0.10):.3f} | "
              f"{pct(v, 0.90):.3f} | {min(v):.3f} | {max(v):.3f} | "
              f"{dentro:.1f}% |")

    print("\n## La suma calibrada por hora local\n")
    print("| hora | N | suma mediana |")
    print("|---|---|---|")
    for h in sorted(por_hora):
        v = por_hora[h]
        if len(v) < 100:
            continue
        print(f"| {h:02d}h | {len(v)} | {statistics.median(v):.3f} |")

    # ── ¿mejoraría el Brier normalizando? ────────────────────────────────────
    print("\n## Brier: tal cual contra normalizado a 1\n")
    print("Sólo snapshots con settle. `normalizado` = cada p dividida por la "
          "suma de su snapshot.\n")
    print("| serie | N bins | Brier | Δ vs actual |")
    print("|---|---|---|---|")
    b_cal, b_norm, b_raw = [], [], []
    for (st, ts), bins in snaps.items():
        if st not in STATION_TZ or len(bins) < 3:
            continue
        d = datetime.fromisoformat(ts).astimezone(
            ZoneInfo(STATION_TZ[st])).date().isoformat()
        settle = settles.get((st, d))
        if settle is None:
            continue
        sc = sum(b[3] for b in bins)
        if sc <= 0:
            continue
        for lo, hi, praw, pcal in bins:
            o = contiene(settle, lo, hi)
            b_raw.append((praw - o) ** 2)
            b_cal.append((pcal - o) ** 2)
            b_norm.append((pcal / sc - o) ** 2)
    if b_cal:
        m_cal = statistics.mean(b_cal)
        for nombre, v in (("our_p crudo", b_raw),
                          ("calibrado (actual)", b_cal),
                          ("calibrado y normalizado", b_norm)):
            m = statistics.mean(v)
            print(f"| {nombre} | {len(v)} | {m:.4f} | {m - m_cal:+.4f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
