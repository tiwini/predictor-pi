#!/usr/bin/env python3
"""Mide el lag entre saltos de our_p (nuestro ensemble) y de yes_mid (Kalshi).

Motivacion: dos casos del 2026-07-24 (KMIA 14c->97c, KPHX 0.215->0.905)
sugirieron una latencia de ~11 min entre que nuestro p salta y el mercado
reacciona. PERO la cadencia del poller es ~12 min, asi que "11 minutos" puede
ser exactamente un tick de muestreo — indistinguible de latencia cero. Este
script primero mide la cadencia y luego el lag en TICKS, no en minutos
absolutos, y mide las dos direcciones para detectar si es asimetrico o ruido.

Metodo:
  - serie por (estacion, dia, bin): (ts, yes_mid, our_p) ordenada
  - "evento" = tick con |delta| >= JUMP en una de las series
  - para cada evento en una serie, buscar el mayor salto de la otra serie
    del mismo signo dentro de +-WINDOW ticks
  - lag en ticks: negativo = la serie ancla salto ANTES que la otra
"""
import os, sqlite3, statistics
from collections import Counter, defaultdict
from datetime import datetime

os.chdir(os.path.expanduser("~/predictor-pi/weather-predictor"))
JUMP = 0.30
WINDOW = 3

c = sqlite3.connect("analysis.db"); c.row_factory = sqlite3.Row
rows = c.execute("""
    SELECT ts, station, label, bin_lo, bin_hi, yes_mid, our_p
    FROM kalshi_snapshots
    WHERE yes_mid IS NOT NULL AND our_p IS NOT NULL
    ORDER BY station, bin_lo, bin_hi, ts""").fetchall()
c.close()
print(f"filas: {len(rows)}")

series = defaultdict(list)
for r in rows:
    ts = datetime.fromisoformat(r["ts"])
    key = (r["station"], ts.date().isoformat(), r["bin_lo"], r["bin_hi"])
    series[key].append((ts, r["yes_mid"], r["our_p"], r["label"]))

# ---------- 1. cadencia real del poller ----------
deltas = []
for k, s in series.items():
    s.sort()
    for i in range(1, len(s)):
        d = (s[i][0] - s[i - 1][0]).total_seconds() / 60
        if 0 < d < 60:
            deltas.append(d)
deltas.sort()
print(f"\n=== CADENCIA del poller (n={len(deltas)} intervalos) ===")
print(f"  mediana {statistics.median(deltas):.1f} min | p10 {deltas[len(deltas)//10]:.1f}"
      f" | p90 {deltas[9*len(deltas)//10]:.1f} | min {deltas[0]:.1f} | max {deltas[-1]:.1f}")
print(f"  --> un tick = ~{statistics.median(deltas):.0f} min. Cualquier lag menor")
print(f"      que esto es INDISTINGUIBLE de cero con estos datos.")


def jumps(s, idx):
    """Ticks con salto >= JUMP en la serie idx (1=kalshi, 2=our_p)."""
    out = []
    for i in range(1, len(s)):
        d = s[i][idx] - s[i - 1][idx]
        if abs(d) >= JUMP:
            out.append((i, d))
    return out


def match(anchor_idx, other_idx):
    """Para cada salto en anchor, el mayor salto del mismo signo en other
    dentro de +-WINDOW ticks. Devuelve lista de (lag_ticks, lag_min, sta, day, label)."""
    res = []
    for key, s in series.items():
        if len(s) < 4:
            continue
        oj = jumps(s, other_idx)
        if not oj:
            continue
        for i, d in jumps(s, anchor_idx):
            best = None
            for j, d2 in oj:
                if abs(j - i) > WINDOW:
                    continue
                if (d > 0) != (d2 > 0):
                    continue
                if best is None or abs(d2) > abs(best[1]):
                    best = (j, d2)
            if best is None:
                continue
            j = best[0]
            lag_min = (s[j][0] - s[i][0]).total_seconds() / 60
            res.append((j - i, lag_min, key[0], key[1], s[i][3], d, best[1]))
    return res


for anchor, other, label in ((1, 2, "ancla=SALTO KALSHI, busca nuestro our_p"),
                             (2, 1, "ancla=SALTO our_p, busca Kalshi")):
    res = match(anchor, other)
    print(f"\n{'='*70}\n{label}   (n={len(res)} pares)\n{'='*70}")
    if not res:
        print("  sin pares")
        continue
    cnt = Counter(r[0] for r in res)
    print("  lag en ticks (negativo = la OTRA serie salto antes que el ancla):")
    for t in sorted(cnt):
        bar = "#" * min(60, cnt[t])
        print(f"    {t:>+3} ticks  n={cnt[t]:>4}  {bar}")
    lags = [r[0] for r in res]
    print(f"  mediana {statistics.median(lags):+.1f} ticks | media {statistics.mean(lags):+.2f}")
    same = sum(1 for l in lags if l == 0)
    print(f"  lag 0 (mismo tick, latencia < 1 tick): {same}/{len(lags)} = {100*same/len(lags):.0f}%")
    neg = sum(1 for l in lags if l < 0)
    pos = sum(1 for l in lags if l > 0)
    print(f"  otra-antes: {neg}  ({100*neg/len(lags):.0f}%)   otra-despues: {pos}"
          f"  ({100*pos/len(lags):.0f}%)")

# ---------- 3. el caso concreto que motivo esto ----------
print(f"\n{'='*70}\nLos dos casos citados el 2026-07-24\n{'='*70}")
for sta, day, lo in (("KPHX", "2026-07-24", 117), ("KMIA", "2026-07-24", 93)):
    for key, s in series.items():
        if key[0] == sta and key[1] == day and key[2] == lo:
            s.sort()
            print(f"\n{sta} {day} bin {s[0][3]}:")
            for i in range(1, len(s)):
                dk = s[i][1] - s[i - 1][1]
                do = s[i][2] - s[i - 1][2]
                if abs(dk) >= JUMP or abs(do) >= JUMP:
                    dt = (s[i][0] - s[i - 1][0]).total_seconds() / 60
                    print(f"   {s[i-1][0].strftime('%H:%M')} -> {s[i][0].strftime('%H:%M')}"
                          f" ({dt:.0f}min):  kal {s[i-1][1]:.3f}->{s[i][1]:.3f} ({dk:+.3f})"
                          f"   our {s[i-1][2]:.3f}->{s[i][2]:.3f} ({do:+.3f})")
