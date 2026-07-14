#!/usr/bin/env python3
"""R5 requests #1 y #2 de Fable:

  1. Serie temporal semanal del basis (mean/median/std/N por semana ISO).
  2. Re-run de edge_adj y Brier usando basis_local = trailing_median(14d)
     por row (leaky trailing: ventana [row_made_at - 14d, row_made_at - 1h]).

Solo lee. df=4 para id<966, df=5 desde id 966.
"""
import math
import sqlite3
from datetime import datetime, timezone

DB = "/home/popeye/crypto-predictor/calibration.db"
DF_SWITCH_ID = 966
TRAILING_DAYS = 14
LAG_SECONDS = 3600  # basis fresh at t-1h (per Fable Q2)

try:
    from scipy import stats
    def t_cdf(x, df):
        return stats.t.cdf(x, df)
except ImportError:
    def t_cdf(x, df, n=4000):
        lo, hi = -12.0, float(x)
        if hi <= lo:
            return 0.0
        h = (hi - lo) / n
        c = math.gamma((df + 1) / 2) / (math.sqrt(df * math.pi) * math.gamma(df / 2))
        s = 0.0
        for i in range(n + 1):
            t = lo + i * h
            w = 1 if i in (0, n) else (4 if i % 2 else 2)
            s += w * (1 + t * t / df) ** (-(df + 1) / 2)
        return min(1.0, max(0.0, c * s * h / 3))


def model_no(strike, now_price, sigma_h, df):
    scale = math.sqrt((df - 2) / df)
    return t_cdf(math.log(strike / now_price) / sigma_h / scale, df)


def median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return float("nan")
    if n % 2:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    # --- pull master ---
    rows = c.execute(
        "SELECT id, made_at, actual_price, proxy_price_at_settle, "
        "  kalshi_strike, kalshi_no_at_strike, now_price, sigma_h, "
        "  call_value, won, edge_pp "
        "FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NOT NULL "
        "ORDER BY made_at"
    ).fetchall()
    made = [float(r["made_at"]) for r in rows]
    bps = [1e4 * (r["actual_price"] - r["proxy_price_at_settle"]) / r["actual_price"]
           for r in rows]

    # ==========================================================
    # 1. Serie temporal semanal (ISO week YYYY-WW)
    # ==========================================================
    buckets = {}
    for t, b in zip(made, bps):
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        iso_y, iso_w, _ = dt.isocalendar()
        key = f"{iso_y}-W{iso_w:02d}"
        buckets.setdefault(key, []).append(b)
    print("=" * 78)
    print("[1] Basis Binance-Coinbase por semana ISO (bps)")
    print("=" * 78)
    print(f"{'week':<10} {'N':>4} {'mean':>8} {'median':>8} {'std':>7} "
          f"{'min':>7} {'max':>7}")
    for k in sorted(buckets):
        xs = buckets[k]
        n = len(xs)
        m = sum(xs) / n
        med = median(xs)
        std = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
        print(f"{k:<10} {n:>4} {m:>+8.2f} {med:>+8.2f} {std:>7.2f} "
              f"{min(xs):>+7.2f} {max(xs):>+7.2f}")

    # ==========================================================
    # 2. edge_adj y Brier con basis_local trailing median 14d
    #    ventana: [row_made_at - 14d, row_made_at - 1h]
    # ==========================================================
    span = TRAILING_DAYS * 86400
    lag = LAG_SECONDS
    # index para búsqueda O(log N)
    n_all = len(made)
    # dos punteros (both sorted by made_at asc)
    edge_raw = []      # edge_pp original (Binance frame)
    edge_adj_global = []  # con basis constante global (verify_basis_edge)
    edge_adj_local = []   # con basis trailing median 14d
    br_raw = br_adj_g = br_adj_l = br_k = 0.0
    br_n = 0
    basis_local_stats = []
    basis_global = sum(bps) / len(bps) / 1e4  # el +10.64 bps del original

    left = 0
    for i, r in enumerate(rows):
        # ventana trailing
        t = made[i]
        lo_t, hi_t = t - span, t - lag
        # avanzar left hasta made_at >= lo_t
        while left < n_all and made[left] < lo_t:
            left += 1
        # tomar todos [left, i) con made_at <= hi_t
        window = [bps[j] for j in range(left, i) if made[j] <= hi_t]
        if len(window) < 20:
            # ventana insuficiente -> caemos al basis global para no perder rows
            b_local = basis_global
        else:
            b_local = median(window) / 1e4
        basis_local_stats.append(b_local * 1e4)

        # solo rows con kalshi book real y sigma>0
        knos = r["kalshi_no_at_strike"]
        strike = r["kalshi_strike"]
        if (knos is None or strike is None or knos in (0.0, 1.0)
                or r["sigma_h"] is None or r["sigma_h"] <= 0
                or r["now_price"] is None or r["now_price"] <= 0):
            continue
        df = 5 if r["id"] >= DF_SWITCH_ID else 4

        m_raw = model_no(strike, r["now_price"], r["sigma_h"], df)
        m_adj_g = model_no(strike * (1 + basis_global),
                           r["now_price"], r["sigma_h"], df)
        m_adj_l = model_no(strike * (1 + b_local),
                           r["now_price"], r["sigma_h"], df)

        if r["edge_pp"] is not None:
            edge_raw.append(r["edge_pp"])
            edge_adj_global.append((m_adj_g - knos) * 100)
            edge_adj_local.append((m_adj_l - knos) * 100)

        # Brier: outcome = proxy <= strike
        out = 1.0 if r["proxy_price_at_settle"] <= strike else 0.0
        br_raw += (m_raw - out) ** 2
        br_adj_g += (m_adj_g - out) ** 2
        br_adj_l += (m_adj_l - out) ** 2
        br_k += (knos - out) ** 2
        br_n += 1

    print()
    print("=" * 78)
    print(f"[2] Basis_local trailing median {TRAILING_DAYS}d (lag {lag}s) — resumen")
    print("=" * 78)
    bl = sorted(basis_local_stats)
    bl_mean = sum(bl) / len(bl)
    print(f"  N={len(bl)}  mean={bl_mean:+.2f}  median={median(bl):+.2f}  "
          f"p5={bl[int(len(bl)*0.05)]:+.2f}  p95={bl[int(len(bl)*0.95)]:+.2f}")
    print(f"  (basis global usado en verify_basis_edge fue {basis_global*1e4:+.2f} bps)")

    def summary(name, xs):
        s = sorted(xs)
        n = len(xs)
        m = sum(xs) / n
        p10 = s[max(0, int(n * 0.10))]
        p90 = s[min(n - 1, int(n * 0.90))]
        frac = sum(x > 0 for x in xs) / n
        print(f"  {name:<20} N={n}  mean={m:+.2f}  median={median(xs):+.2f}  "
              f"p10={p10:+.2f}  p90={p90:+.2f}  frac>0={frac:.2f}")

    print()
    print("[2a] edge_pp comparison")
    summary("edge_pp raw", edge_raw)
    summary("edge_adj global",  edge_adj_global)
    summary("edge_adj local",   edge_adj_local)

    print()
    print(f"[2b] Brier al strike vs outcome proxy, N={br_n}")
    print(f"  modelo raw        : {br_raw/br_n:.4f}")
    print(f"  modelo adj global : {br_adj_g/br_n:.4f}  (basis constante +{basis_global*1e4:.2f}bps)")
    print(f"  modelo adj local  : {br_adj_l/br_n:.4f}  (basis trailing median 14d/row)")
    print(f"  kalshi            : {br_k/br_n:.4f}")

    print()
    print("Lectura:")
    print("  - Si adj_local ≈ adj_global, la magnitud del edge_adj de verify_basis_edge")
    print("    era robusta al time-mismatch — el basis no derivó lo suficiente.")
    print("  - Si adj_local difiere >±20% en Brier, el hallazgo original tenia magnitud")
    print("    contaminada por el basis global aplicado a rows con basis real distinto.")


if __name__ == "__main__":
    main()
