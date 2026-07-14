#!/usr/bin/env python3
"""R5 requests #3 y #4 de Fable:

  3. Bootstrap pareado por row de (Brier_kalshi - Brier_adj_local),
     10k réplicas, block bootstrap bloques=24 (autocorrelación horaria),
     CI 95%. Basis local = trailing median 14d por row (mejor de #2).
  4. edge_pp_adj_local por hora del día UTC — artifact check madrugada
     (books vacíos, mids stale).

df=4 para id<966, df=5 desde id 966.
"""
import math
import random
import sqlite3
from datetime import datetime, timezone

DB = "/home/popeye/crypto-predictor/calibration.db"
DF_SWITCH_ID = 966
TRAILING_DAYS = 14
LAG_SECONDS = 3600
N_BOOT = 10000
BLOCK = 24
SEED = 42

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


def compute_per_row(rows_all, bps_all, made_all):
    """Retorna lista de dicts con br_raw, br_adj_l, br_k, edge_adj_l, hour_utc,
    solo para rows con kalshi book real."""
    span = TRAILING_DAYS * 86400
    lag = LAG_SECONDS
    n_all = len(rows_all)
    basis_global = sum(bps_all) / len(bps_all) / 1e4
    out = []
    left = 0
    for i, r in enumerate(rows_all):
        t = made_all[i]
        lo_t, hi_t = t - span, t - lag
        while left < n_all and made_all[left] < lo_t:
            left += 1
        window = [bps_all[j] for j in range(left, i) if made_all[j] <= hi_t]
        b_local = median(window) / 1e4 if len(window) >= 20 else basis_global

        knos = r["kalshi_no_at_strike"]
        strike = r["kalshi_strike"]
        if (knos is None or strike is None or knos in (0.0, 1.0)
                or r["sigma_h"] is None or r["sigma_h"] <= 0
                or r["now_price"] is None or r["now_price"] <= 0):
            continue
        df = 5 if r["id"] >= DF_SWITCH_ID else 4
        m_raw = model_no(strike, r["now_price"], r["sigma_h"], df)
        m_adj_l = model_no(strike * (1 + b_local),
                           r["now_price"], r["sigma_h"], df)
        outcome = 1.0 if r["proxy_price_at_settle"] <= strike else 0.0
        br_raw = (m_raw - outcome) ** 2
        br_adj_l = (m_adj_l - outcome) ** 2
        br_k = (knos - outcome) ** 2

        hour_utc = datetime.fromtimestamp(float(r["made_at"]),
                                          tz=timezone.utc).hour
        edge_adj_l = (m_adj_l - knos) * 100

        out.append({
            "row_id": r["id"],
            "made_at": float(r["made_at"]),
            "hour_utc": hour_utc,
            "br_raw": br_raw,
            "br_adj_l": br_adj_l,
            "br_k": br_k,
            "edge_adj_l": edge_adj_l,
            "b_local_bps": b_local * 1e4,
            "kalshi_no": knos,
        })
    return out


def block_bootstrap_mean_diff(diffs, block_size, n_boot, rng):
    """Diffs es lista de d_i (kalshi - adj) alineadas por row.
    Block bootstrap: sample con reposición de bloques contiguos hasta cubrir N.
    Retorna list of bootstrap means."""
    n = len(diffs)
    n_blocks = math.ceil(n / block_size)
    max_start = n - block_size
    boot_means = []
    for _ in range(n_boot):
        acc = 0.0
        count = 0
        for _ in range(n_blocks):
            s = rng.randint(0, max_start)
            for k in range(block_size):
                if count >= n:
                    break
                acc += diffs[s + k]
                count += 1
        boot_means.append(acc / n)
    return boot_means


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, made_at, actual_price, proxy_price_at_settle, "
        "  kalshi_strike, kalshi_no_at_strike, now_price, sigma_h "
        "FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NOT NULL "
        "ORDER BY made_at"
    ).fetchall()
    made = [float(r["made_at"]) for r in rows]
    bps = [1e4 * (r["actual_price"] - r["proxy_price_at_settle"]) / r["actual_price"]
           for r in rows]
    per = compute_per_row(rows, bps, made)
    n = len(per)
    print(f"[book real rows] N={n}")

    # ==========================================================
    # 3. Bootstrap pareado ΔBrier
    # ==========================================================
    diffs = [p["br_k"] - p["br_adj_l"] for p in per]  # positivo = adj mejor
    obs_mean = sum(diffs) / n
    rng = random.Random(SEED)
    means = block_bootstrap_mean_diff(diffs, BLOCK, N_BOOT, rng)
    means.sort()
    ci_lo = means[int(0.025 * N_BOOT)]
    ci_hi = means[int(0.975 * N_BOOT)]
    p_ge0 = sum(m > 0 for m in means) / N_BOOT
    # p-value bilateral aproximado (fracción bootstrap < 0)
    p_lt0 = sum(m < 0 for m in means) / N_BOOT
    p_two = 2 * min(p_ge0, p_lt0)

    print()
    print("=" * 78)
    print("[3] Bootstrap pareado ΔBrier = Brier_kalshi - Brier_adj_local")
    print("=" * 78)
    print(f"  positivo = modelo adj mejor que kalshi")
    print(f"  observed mean ΔBrier = {obs_mean:+.5f}")
    print(f"  block bootstrap: bloques={BLOCK}h, N_boot={N_BOOT}, seed={SEED}")
    print(f"  CI 95% = [{ci_lo:+.5f}, {ci_hi:+.5f}]")
    print(f"  frac(boot > 0) = {p_ge0:.4f}   p_two-sided ≈ {p_two:.4f}")
    print(f"  Interpretación: modelo adj bate kalshi con p={p_two:.4f} "
          f"(N_boot={N_BOOT}, block=24h)")

    # Comparación control: raw vs kalshi (deberia ser negativo)
    diffs_raw = [p["br_k"] - p["br_raw"] for p in per]
    obs_raw = sum(diffs_raw) / n
    rng2 = random.Random(SEED + 1)
    means_raw = block_bootstrap_mean_diff(diffs_raw, BLOCK, N_BOOT, rng2)
    means_raw.sort()
    print()
    print(f"  [control] observed mean (kalshi - raw) = {obs_raw:+.5f}")
    print(f"  CI 95% raw = [{means_raw[int(0.025*N_BOOT)]:+.5f}, "
          f"{means_raw[int(0.975*N_BOOT)]:+.5f}]")

    # ==========================================================
    # 4. edge_adj_local por hora del día UTC
    # ==========================================================
    print()
    print("=" * 78)
    print("[4] edge_pp_adj_local y Brier por hora del día UTC "
          "(made_at.hour, target ≈ made+1h)")
    print("=" * 78)
    print(f"{'hh_utc':<7} {'N':>4} {'edge_mean':>10} {'edge_med':>9} "
          f"{'fr>0':>5} {'br_adj':>8} {'br_k':>8} {'Δbr':>8} "
          f"{'basis_med':>10}")
    by_hour = {}
    for p in per:
        by_hour.setdefault(p["hour_utc"], []).append(p)
    for h in sorted(by_hour):
        xs = by_hour[h]
        edges = [x["edge_adj_l"] for x in xs]
        basis_local = [x["b_local_bps"] for x in xs]
        br_a = sum(x["br_adj_l"] for x in xs) / len(xs)
        br_ka = sum(x["br_k"] for x in xs) / len(xs)
        m_edge = sum(edges) / len(edges)
        frac = sum(e > 0 for e in edges) / len(edges)
        print(f"{h:>02d}:00   {len(xs):>4} {m_edge:>+10.2f} "
              f"{median(edges):>+9.2f} {frac:>5.2f} "
              f"{br_a:>8.4f} {br_ka:>8.4f} {br_ka - br_a:>+8.4f} "
              f"{median(basis_local):>+10.2f}")

    # Marcado especial: horas donde el edge es notablemente distinto
    all_mean = sum(p["edge_adj_l"] for p in per) / n
    all_std = math.sqrt(sum((p["edge_adj_l"] - all_mean) ** 2 for p in per) / (n - 1))
    print(f"\n  overall edge_adj_local: mean={all_mean:+.2f}  std={all_std:.2f}")
    print(f"  horas con |edge_hh - overall| > std ({all_std:.2f}):")
    for h in sorted(by_hour):
        xs = by_hour[h]
        m = sum(x["edge_adj_l"] for x in xs) / len(xs)
        if abs(m - all_mean) > all_std:
            flag = "HIGH" if m > all_mean else "LOW"
            print(f"    {h:>02d}:00  N={len(xs)}  edge_mean={m:+.2f}  "
                  f"delta={m - all_mean:+.2f}  {flag}")


if __name__ == "__main__":
    main()
