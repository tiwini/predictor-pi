#!/usr/bin/env python3
"""Artefacto 2 del corte 5B (2026-07-21) — ΔBrier modelo_adj vs Kalshi.

Regla del preregistro (5B punto 2):
  ΔBrier vs Kalshi > 0, CI bootstrap-block excluyendo cero.
  ΔBrier[R] = Brier_kalshi[R] - Brier_modelo_adj[R]  (positivo ⇔ modelo mejor)

Convención de outcome (preregistro 5B): `brti_proxy_price ≤ kalshi_strike` → 1.
Esto es "outcome NO" (settle a NO ganando). Ambos scoring functions se
evalúan contra la MISMA outcome. Modelo usa `model_no` = P(price ≤ strike).

Parámetros acordados 2026-07-21:
  - Bootstrap block size = 24
  - Replicas = 10000
  - CI 95%; pass si P2.5 > 0
  - basis EMA half-life = 72h at-call (misma definición Artefacto #1)

Filtros exclusión y df_switch: idénticos a Artefacto #1.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
import sys
from datetime import date, datetime, timezone

DB_PATH = "/home/popeye/crypto-predictor/calibration.db"
CUTOFF_START_ISO = "2026-07-09 00:00:00"
DF_SWITCH_ID = 966
EMA_HALFLIFE_H = 72.0
BLOCK_SIZE = 24
N_REPLICAS = 10000
ALPHA = 0.05
RNG_SEED = 20260721
SYMBOL = "BTCUSDT"

try:
    from scipy import stats  # type: ignore
    def t_cdf(x: float, df: int) -> float:
        return float(stats.t.cdf(x, df))
except ImportError:
    def t_cdf(x: float, df: int, n: int = 4000) -> float:
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


def model_no(strike: float, now_price: float, sigma_h: float, df: int) -> float:
    scale = math.sqrt((df - 2) / df)
    z = math.log(strike / now_price) / sigma_h
    return t_cdf(z / scale, df)


def basis_ema_at(basis_history, t_ref):
    ln2_over_hl_s = math.log(2) / (EMA_HALFLIFE_H * 3600.0)
    num = 0.0
    den = 0.0
    for settled_at, b in basis_history:
        if settled_at >= t_ref:
            break
        w = math.exp(-ln2_over_hl_s * (t_ref - settled_at))
        num += w * b
        den += w
    if den <= 0.0:
        return None
    return num / den


def bootstrap_block_ci(values, block, n_rep, alpha, rng):
    n = len(values)
    if n < block:
        raise ValueError(f"N={n} < block={block}")
    starts_max = n - block
    n_blocks_per_rep = math.ceil(n / block)
    means = []
    for _ in range(n_rep):
        sample = []
        for _b in range(n_blocks_per_rep):
            s0 = rng.randint(0, starts_max)
            sample.extend(values[s0:s0 + block])
        sample = sample[:n]
        means.append(sum(sample) / n)
    means.sort()
    p_lo = means[int(alpha / 2 * n_rep)]
    p_hi = means[int((1 - alpha / 2) * n_rep) - 1]
    return dict(n_replicas=n_rep, block_size=block,
                point_estimate=sum(values) / n,
                ci_lo=p_lo, ci_hi=p_hi, alpha=alpha,
                excludes_zero=p_lo > 0 or p_hi < 0,
                sign_positive=p_lo > 0)


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    hist_rows = con.execute("""
        SELECT settled_at, actual_price, brti_proxy_price
        FROM hourly_calls
        WHERE symbol = ? AND actual_price IS NOT NULL
          AND brti_proxy_price IS NOT NULL AND settled_at IS NOT NULL
          AND actual_price > 0
        ORDER BY settled_at ASC
    """, (SYMBOL,)).fetchall()
    basis_history = [
        (r["settled_at"], (r["actual_price"] - r["brti_proxy_price"]) / r["actual_price"])
        for r in hist_rows
    ]
    print(f"basis history: {len(basis_history)} rows")

    cutoff = datetime.fromisoformat(CUTOFF_START_ISO).replace(tzinfo=timezone.utc).timestamp()
    rows = con.execute("""
        SELECT id, made_at, now_price, sigma_h, kalshi_strike, kalshi_no_at_strike,
               brti_proxy_price, brti_proxy_n_venues, features_max_age_s,
               vol_regime_ratio, actual_price
        FROM hourly_calls
        WHERE symbol = ? AND actual_price IS NOT NULL AND made_at >= ?
          AND brti_proxy_n_venues >= 3
          AND features_max_age_s <= 120
          AND vol_regime_ratio IS NOT NULL
          AND kalshi_strike IS NOT NULL AND kalshi_no_at_strike IS NOT NULL
          AND kalshi_no_at_strike NOT IN (0.0, 1.0)
          AND sigma_h > 0
        ORDER BY made_at ASC
    """, (SYMBOL, cutoff)).fetchall()
    print(f"rows post-filtros: N = {len(rows)}")

    brier_raw = []
    brier_adj = []
    brier_kal = []
    delta_brier_adj = []  # kalshi - modelo_adj
    delta_brier_raw = []  # kalshi - modelo_raw (referencia)
    outcomes = []
    m_adj_list = []
    m_raw_list = []
    k_no_list = []
    n_no_basis = 0
    for r in rows:
        b_ema = basis_ema_at(basis_history, r["made_at"])
        if b_ema is None:
            n_no_basis += 1
            continue
        df = 5 if r["id"] >= DF_SWITCH_ID else 4
        # outcome NO: 1 si BRTI settleó ≤ strike; else 0
        outcome = 1.0 if r["brti_proxy_price"] <= r["kalshi_strike"] else 0.0
        m_raw = model_no(r["kalshi_strike"], r["now_price"], r["sigma_h"], df)
        m_adj = model_no(r["kalshi_strike"] * (1 + b_ema),
                         r["now_price"], r["sigma_h"], df)
        k_no = r["kalshi_no_at_strike"]

        br_raw = (m_raw - outcome) ** 2
        br_adj = (m_adj - outcome) ** 2
        br_k = (k_no - outcome) ** 2

        brier_raw.append(br_raw)
        brier_adj.append(br_adj)
        brier_kal.append(br_k)
        delta_brier_adj.append(br_k - br_adj)  # + = modelo adj mejor
        delta_brier_raw.append(br_k - br_raw)  # + = modelo raw mejor
        outcomes.append(outcome)
        m_adj_list.append(m_adj)
        m_raw_list.append(m_raw)
        k_no_list.append(k_no)

    if n_no_basis:
        print(f"warn: {n_no_basis} rows sin basis previo (dropped)")
    N = len(delta_brier_adj)
    print(f"N efectivo = {N}")

    # Aggregates
    br_raw_mean = statistics.mean(brier_raw)
    br_adj_mean = statistics.mean(brier_adj)
    br_kal_mean = statistics.mean(brier_kal)
    outcome_rate = statistics.mean(outcomes)
    print()
    print("=== Brier means ===")
    print(f"  modelo raw : {br_raw_mean:.5f}")
    print(f"  modelo adj : {br_adj_mean:.5f}")
    print(f"  kalshi     : {br_kal_mean:.5f}")
    print(f"  Δ(kalshi - modelo_raw) mean = {br_kal_mean - br_raw_mean:+.5f}")
    print(f"  Δ(kalshi - modelo_adj) mean = {br_kal_mean - br_adj_mean:+.5f}")
    print(f"  outcome rate (settle ≤ strike): {outcome_rate:.3f}")

    # Bootstrap sobre Δ(kalshi - modelo_adj)
    print("\n=== Bootstrap-block CI 95% sobre ΔBrier(kalshi - modelo_adj) ===")
    rng = random.Random(RNG_SEED)
    ci_adj = bootstrap_block_ci(delta_brier_adj, BLOCK_SIZE, N_REPLICAS, ALPHA, rng)
    print(f"  point estimate mean = {ci_adj['point_estimate']:+.5f}")
    print(f"  CI 95%              = [{ci_adj['ci_lo']:+.5f}, {ci_adj['ci_hi']:+.5f}]")
    print(f"  excludes zero       = {ci_adj['excludes_zero']}")
    print(f"  sign positive       = {ci_adj['sign_positive']}")

    print("\n=== (Referencia) Bootstrap-block ΔBrier(kalshi - modelo_raw) ===")
    rng2 = random.Random(RNG_SEED + 1)
    ci_raw = bootstrap_block_ci(delta_brier_raw, BLOCK_SIZE, N_REPLICAS, ALPHA, rng2)
    print(f"  point estimate mean = {ci_raw['point_estimate']:+.5f}")
    print(f"  CI 95%              = [{ci_raw['ci_lo']:+.5f}, {ci_raw['ci_hi']:+.5f}]")
    print(f"  excludes zero       = {ci_raw['excludes_zero']}")

    verdict = "PASS regla #2" if ci_adj["sign_positive"] else \
              "FAIL regla #2 (CI cola baja ≤ 0)"
    print(f"\n=== Veredicto regla #2 (5B): {verdict} ===")

    out_path = f"/tmp/artefacto2_delta_brier_{date.today().isoformat().replace('-','')}.json"
    payload = {
        "artefacto": "2_delta_brier_kalshi_vs_modelo_adj",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "preregistro_ref": "preregistro_5b_20260709.md",
        "parametros_sesion_20260721": {
            "block_size": BLOCK_SIZE, "n_replicas": N_REPLICAS,
            "alpha": ALPHA, "rng_seed": RNG_SEED,
            "ema_halflife_hours": EMA_HALFLIFE_H,
        },
        "outcome_definition": "brti_proxy_price <= kalshi_strike (outcome NO)",
        "n_rows_input": len(rows),
        "n_rows_sin_basis_previo": n_no_basis,
        "n_efectivo": N,
        "brier_means": {
            "modelo_raw": br_raw_mean,
            "modelo_adj": br_adj_mean,
            "kalshi": br_kal_mean,
        },
        "outcome_rate": outcome_rate,
        "delta_brier_kalshi_minus_adj_bootstrap": ci_adj,
        "delta_brier_kalshi_minus_raw_bootstrap": ci_raw,
        "veredicto_regla_2": verdict,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nartefacto JSON: {out_path}")


if __name__ == "__main__":
    main()
