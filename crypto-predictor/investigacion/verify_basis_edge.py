#!/usr/bin/env python3
"""Verifica sobre el DB completo los hallazgos del review Fable 2026-07-08:

  1. Basis Binance USDT vs Coinbase USD (proxy) — mean/std/SE en bps.
  2. Flips de `won` si se settleara con el proxy (espacio BRTI).
  3. edge_pp basis-ajustado: model_no evaluado en strike*(1+basis),
     con df correcto por era (df=4 para id<966, df=5 desde id 966).
  4. Brier al strike vs outcome proxy: modelo raw / modelo ajustado / Kalshi.

Correr en la Pi:
  cd ~/predictor-pi/crypto-predictor && ./venv/bin/python3 verify_basis_edge.py

Solo lee el DB (ninguna escritura).
"""
import math
import sqlite3

DB_PATH = "/home/popeye/crypto-predictor/calibration.db"
DF_SWITCH_ID = 966          # id >= 966 -> df=5 (cambio 2026-07-05)

try:
    from scipy import stats
    def t_cdf(x, df):
        return stats.t.cdf(x, df)
except ImportError:  # fallback sin scipy (integración numérica)
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
    """P(price_target <= strike) bajo t(df) unit-variance rescaled."""
    scale = math.sqrt((df - 2) / df)
    z = math.log(strike / now_price) / sigma_h
    return t_cdf(z / scale, df)


def pctl(sorted_xs, p):
    if not sorted_xs:
        return float("nan")
    k = (len(sorted_xs) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_xs) - 1)
    return sorted_xs[f] + (sorted_xs[c] - sorted_xs[f]) * (k - f)


def main():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row

    # ---------- 1. Basis ----------
    rows = c.execute(
        "SELECT id, actual_price, proxy_price_at_settle, call_value, won "
        "FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NOT NULL"
    ).fetchall()
    bps = [1e4 * (r["actual_price"] - r["proxy_price_at_settle"]) / r["actual_price"]
           for r in rows]
    n = len(bps)
    mean = sum(bps) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in bps) / (n - 1))
    srt = sorted(bps)
    print(f"[1] Basis Binance-Coinbase, N={n}")
    print(f"    mean={mean:+.2f} bps  std={std:.2f}  SE={std/math.sqrt(n):.2f}")
    print(f"    p5={pctl(srt,0.05):+.2f}  median={pctl(srt,0.5):+.2f}  "
          f"p95={pctl(srt,0.95):+.2f}")
    basis = mean / 1e4

    # ---------- 2. Flips de won bajo proxy ----------
    flips = [(r["id"], r["won"],
              1 if r["proxy_price_at_settle"] <= r["call_value"] else 0)
             for r in rows]
    diff = [f for f in flips if f[1] != f[2]]
    print(f"\n[2] won flips (Binance vs proxy): {len(diff)}/{n} "
          f"({100*len(diff)/n:.1f}%)")
    for fid, w, wa in diff:
        print(f"    id={fid}  won={w} -> won_proxy={wa}")

    # ---------- 3. edge_pp basis-ajustado ----------
    rows = c.execute(
        "SELECT id, kalshi_strike, kalshi_no_at_strike, now_price, sigma_h, "
        "edge_pp FROM hourly_calls "
        "WHERE kalshi_strike IS NOT NULL AND kalshi_no_at_strike IS NOT NULL "
        "AND edge_pp IS NOT NULL AND sigma_h > 0 "
        "AND kalshi_no_at_strike NOT IN (0.0, 1.0)"
    ).fetchall()
    raw, adj = [], []
    for r in rows:
        df = 5 if r["id"] >= DF_SWITCH_ID else 4
        m = model_no(r["kalshi_strike"] * (1 + basis),
                     r["now_price"], r["sigma_h"], df)
        raw.append(r["edge_pp"])
        adj.append((m - r["kalshi_no_at_strike"]) * 100)
    for name, xs in (("raw", raw), ("basis-adj", adj)):
        s = sorted(xs)
        m_ = sum(xs) / len(xs)
        print(f"\n[3] edge_pp {name}: N={len(xs)}  mean={m_:+.2f}  "
              f"median={pctl(s,0.5):+.2f}  p10={pctl(s,0.10):+.2f}  "
              f"p90={pctl(s,0.90):+.2f}  frac>0={sum(x>0 for x in xs)/len(xs):.2f}")

    # ---------- 4. Brier al strike vs outcome proxy ----------
    rows = c.execute(
        "SELECT id, kalshi_strike, kalshi_no_at_strike, now_price, sigma_h, "
        "proxy_price_at_settle FROM hourly_calls "
        "WHERE kalshi_strike IS NOT NULL AND kalshi_no_at_strike IS NOT NULL "
        "AND proxy_price_at_settle IS NOT NULL AND sigma_h > 0 "
        "AND kalshi_no_at_strike NOT IN (0.0, 1.0)"
    ).fetchall()
    br_raw = br_adj = br_k = 0.0
    for r in rows:
        df = 5 if r["id"] >= DF_SWITCH_ID else 4
        out = 1.0 if r["proxy_price_at_settle"] <= r["kalshi_strike"] else 0.0
        m_raw = model_no(r["kalshi_strike"], r["now_price"], r["sigma_h"], df)
        m_adj = model_no(r["kalshi_strike"] * (1 + basis),
                         r["now_price"], r["sigma_h"], df)
        br_raw += (m_raw - out) ** 2
        br_adj += (m_adj - out) ** 2
        br_k += (r["kalshi_no_at_strike"] - out) ** 2
    nn = len(rows)
    print(f"\n[4] Brier al strike vs outcome proxy, N={nn}")
    print(f"    modelo raw : {br_raw/nn:.4f}")
    print(f"    modelo adj : {br_adj/nn:.4f}")
    print(f"    kalshi     : {br_k/nn:.4f}")
    print("\nLectura: si 'modelo adj' <= 'kalshi', la conclusion 'Kalshi bate")
    print("centro' era mayormente artefacto de unidades (Binance vs BRTI).")


if __name__ == "__main__":
    main()
