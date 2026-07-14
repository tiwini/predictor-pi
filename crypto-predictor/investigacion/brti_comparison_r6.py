#!/usr/bin/env python3
"""R6 request #2 (final) — comparación ΔBrier median-of-2 vs Coinbase-solo.

Corre el análisis basis/edge/Brier con:
  A) proxy_price_at_settle (Coinbase-solo) — el original de R5.
  B) brti_proxy_price (median-of-2: Coinbase+Bitstamp) — el nuevo BRTI-proxy.

Con basis time-local (trailing median 14d por row) para ambos.
"""
import math
import sqlite3
from datetime import datetime, timezone

DB = "/home/popeye/crypto-predictor/calibration.db"
DF_SWITCH_ID = 966
TRAILING_DAYS = 14
LAG_SECONDS = 3600

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


def analyze(rows_all, proxy_col, label):
    """rows_all: iter rows (dict-like) que incluyen actual_price + proxy_col.
    Devuelve dict con basis stats + edge stats + Brier."""
    times = [float(r["made_at"]) for r in rows_all]
    bps = [1e4 * (r["actual_price"] - r[proxy_col]) / r["actual_price"]
           for r in rows_all]
    n_all = len(rows_all)
    m_bps = sum(bps) / n_all
    s_bps = sorted(bps)
    print(f"\n--- {label} ---")
    print(f"  basis global: N={n_all}  mean={m_bps:+.2f} bps  "
          f"median={median(bps):+.2f}  "
          f"std={math.sqrt(sum((b-m_bps)**2 for b in bps)/(n_all-1)):.2f}")
    print(f"  p5={s_bps[int(n_all*0.05)]:+.2f}  "
          f"p95={s_bps[int(n_all*0.95)]:+.2f}")

    span = TRAILING_DAYS * 86400
    lag = LAG_SECONDS
    basis_global = m_bps / 1e4
    br_raw = br_adj = br_k = 0.0
    br_n = 0
    edges_raw = []
    edges_adj = []

    left = 0
    for i, r in enumerate(rows_all):
        t = times[i]
        while left < n_all and times[left] < t - span:
            left += 1
        window = [bps[j] for j in range(left, i) if times[j] <= t - lag]
        b_local = (median(window) / 1e4) if len(window) >= 20 else basis_global

        knos = r["kalshi_no_at_strike"]
        strike = r["kalshi_strike"]
        if (knos is None or strike is None or knos in (0.0, 1.0)
                or r["sigma_h"] is None or r["sigma_h"] <= 0
                or r["now_price"] is None or r["now_price"] <= 0):
            continue
        df = 5 if r["id"] >= DF_SWITCH_ID else 4
        m_raw = model_no(strike, r["now_price"], r["sigma_h"], df)
        m_adj = model_no(strike * (1 + b_local),
                         r["now_price"], r["sigma_h"], df)
        out = 1.0 if r[proxy_col] <= strike else 0.0
        br_raw += (m_raw - out) ** 2
        br_adj += (m_adj - out) ** 2
        br_k += (knos - out) ** 2
        br_n += 1
        if r["edge_pp"] is not None:
            edges_raw.append(r["edge_pp"])
            edges_adj.append((m_adj - knos) * 100)

    e_s = sorted(edges_adj)
    print(f"  edge_adj_local: N={len(edges_adj)}  "
          f"mean={sum(edges_adj)/len(edges_adj):+.2f}  "
          f"median={median(edges_adj):+.2f}  "
          f"frac>0={sum(x>0 for x in edges_adj)/len(edges_adj):.2f}")
    print(f"  Brier (N={br_n}):  raw={br_raw/br_n:.4f}  "
          f"adj_local={br_adj/br_n:.4f}  kalshi={br_k/br_n:.4f}   "
          f"Δ(kalshi-adj)={br_k/br_n - br_adj/br_n:+.5f}")

    return {
        "basis_mean_bps": m_bps,
        "brier_raw": br_raw / br_n,
        "brier_adj": br_adj / br_n,
        "brier_kalshi": br_k / br_n,
        "n": br_n,
    }


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, made_at, actual_price, proxy_price_at_settle, "
        "  brti_proxy_price, brti_proxy_n_venues, "
        "  kalshi_strike, kalshi_no_at_strike, now_price, sigma_h, "
        "  call_value, won, edge_pp "
        "FROM hourly_calls "
        "WHERE actual_price IS NOT NULL "
        "  AND proxy_price_at_settle IS NOT NULL "
        "  AND brti_proxy_price IS NOT NULL "
        "ORDER BY made_at"
    ).fetchall()
    print(f"[input] N={len(rows)}")

    # Distribución de n_venues
    from collections import Counter
    nv = Counter(r["brti_proxy_n_venues"] for r in rows)
    print(f"  n_venues distribution: {dict(nv)}")

    A = analyze(rows, "proxy_price_at_settle",
                "A) Coinbase-solo (proxy_price_at_settle)")
    B = analyze(rows, "brti_proxy_price",
                "B) BRTI-proxy median-of-2 (Coinbase+Bitstamp)")

    print()
    print("=" * 78)
    print("Deltas A -> B (median-of-2 - coinbase-solo):")
    print("=" * 78)
    print(f"  Δbasis global mean = {B['basis_mean_bps'] - A['basis_mean_bps']:+.2f} bps")
    print(f"  ΔBrier_raw         = {B['brier_raw'] - A['brier_raw']:+.5f}")
    print(f"  ΔBrier_adj         = {B['brier_adj'] - A['brier_adj']:+.5f}")
    print(f"  ΔBrier_kalshi      = {B['brier_kalshi'] - A['brier_kalshi']:+.5f}")
    print()
    print("Lectura:")
    print(f"  - Si |ΔBrier| < ~0.001, la circularidad de C era irrelevante (edge robusto).")
    print(f"  - Si median-of-2 mueve Brier > 0.005, el hallazgo tenia sesgo Coinbase.")


if __name__ == "__main__":
    main()
