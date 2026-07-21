#!/usr/bin/env python3
"""Artefacto 1 del corte 5B (2026-07-21) — edge_adj con basis EMA-3d at-call.

Regla del preregistro (5B punto 1):
  edge_adj con basis EMA-3d at-call > 0, CI bootstrap-block excluyendo cero.

Parámetros acordados 2026-07-21 (sesión):
  - Bootstrap block size = 24 (autocorrelación horaria natural)
  - Replicas = 10000
  - CI = 95% (α=0.05); pass si P2.5 > 0
  - basis EMA half-life = 72h continuous-time, look-back a todos los rows
    settled con settled_at < made_at[R] (incluye historia pre-07-09)

Filtros exclusión (preregistro):
  - brti_proxy_n_venues >= 3
  - features_max_age_s <= 120
  - vol_regime_ratio IS NOT NULL
  - kalshi_strike, kalshi_no_at_strike válidos; kalshi_no_at_strike ∉ {0,1}; sigma_h > 0

df_switch: df=5 desde id 966; df=4 antes (todos los rows del corte son id>=966).

Uso:
  ./venv/bin/python3 artefacto1_edge_adj.py

Salida: reporte a stdout + JSON en artefacto1_edge_adj_YYYYMMDD.json.
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

# t-CDF (scipy si disponible; Simpson fallback si no)
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
    """P(price_target <= strike) bajo t(df) unit-variance rescaled."""
    scale = math.sqrt((df - 2) / df)
    z = math.log(strike / now_price) / sigma_h
    return t_cdf(z / scale, df)


def basis_ema_at(basis_history: list[tuple[float, float]], t_ref: float) -> float | None:
    """EMA half-life 72h de basis observados con settled_at < t_ref.

    basis_history: list de (settled_at_epoch, basis_raw) ordenada por tiempo.
    Retorna None si no hay ningún basis histórico disponible al momento t_ref.
    """
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


def bootstrap_block_ci(values: list[float], block: int, n_rep: int,
                       alpha: float, rng: random.Random) -> dict:
    """Moving-block bootstrap: pega bloques contiguos aleatorios de tamaño `block`
    hasta cubrir N, trunca a N, promedia. Repite n_rep. Retorna P2.5/P97.5 sobre
    la mean.
    """
    n = len(values)
    if n < block:
        raise ValueError(f"N={n} < block={block}")
    starts_max = n - block  # start ∈ [0, starts_max] inclusive
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
    return {
        "n_replicas": n_rep,
        "block_size": block,
        "point_estimate": sum(values) / n,
        "ci_lo": p_lo,
        "ci_hi": p_hi,
        "alpha": alpha,
        "excludes_zero": p_lo > 0 or p_hi < 0,
        "sign_positive": p_lo > 0,
    }


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # ---------- 1) basis history: todos los rows settled con brti_proxy_price ----------
    hist_rows = con.execute("""
        SELECT settled_at, actual_price, brti_proxy_price
        FROM hourly_calls
        WHERE symbol = ?
          AND actual_price IS NOT NULL
          AND brti_proxy_price IS NOT NULL
          AND settled_at IS NOT NULL
          AND actual_price > 0
        ORDER BY settled_at ASC
    """, (SYMBOL,)).fetchall()
    basis_history = [
        (r["settled_at"], (r["actual_price"] - r["brti_proxy_price"]) / r["actual_price"])
        for r in hist_rows
    ]
    print(f"basis history: {len(basis_history)} rows, "
          f"range {datetime.fromtimestamp(basis_history[0][0], tz=timezone.utc).isoformat()[:19]} "
          f"→ {datetime.fromtimestamp(basis_history[-1][0], tz=timezone.utc).isoformat()[:19]}")

    # ---------- 2) rows del corte (post-07-09, filtros aplicados) ----------
    cutoff = datetime.fromisoformat(CUTOFF_START_ISO).replace(tzinfo=timezone.utc).timestamp()
    rows = con.execute("""
        SELECT id, made_at, now_price, sigma_h, kalshi_strike, kalshi_no_at_strike,
               brti_proxy_price, brti_proxy_n_venues, features_max_age_s,
               vol_regime_ratio, actual_price, edge_pp
        FROM hourly_calls
        WHERE symbol = ?
          AND actual_price IS NOT NULL
          AND made_at >= ?
          AND brti_proxy_n_venues >= 3
          AND features_max_age_s <= 120
          AND vol_regime_ratio IS NOT NULL
          AND kalshi_strike IS NOT NULL
          AND kalshi_no_at_strike IS NOT NULL
          AND kalshi_no_at_strike NOT IN (0.0, 1.0)
          AND sigma_h > 0
        ORDER BY made_at ASC
    """, (SYMBOL, cutoff)).fetchall()
    print(f"rows post-filtros post-07-09: N = {len(rows)}")
    if not rows:
        sys.exit("No rows after filters. Abort.")

    # ---------- 3) para cada row: basis_ema at-call + edge_adj ----------
    edge_raw_list = []
    edge_adj_list = []
    ema_list = []
    n_no_basis = 0
    for r in rows:
        b_ema = basis_ema_at(basis_history, r["made_at"])
        if b_ema is None:
            n_no_basis += 1
            continue
        df = 5 if r["id"] >= DF_SWITCH_ID else 4
        strike_adj = r["kalshi_strike"] * (1 + b_ema)
        m_adj = model_no(strike_adj, r["now_price"], r["sigma_h"], df)
        edge_adj = (m_adj - r["kalshi_no_at_strike"]) * 100  # pp
        edge_adj_list.append(edge_adj)
        edge_raw_list.append(r["edge_pp"])
        ema_list.append(b_ema)

    if n_no_basis:
        print(f"warn: {n_no_basis} rows without prior basis (dropped)")

    N = len(edge_adj_list)
    print(f"N efectivo edge_adj = {N}")

    # ---------- 4) stats descriptivos ----------
    def stats_line(name: str, xs: list[float]):
        s = sorted(xs)
        m = statistics.mean(xs)
        med = statistics.median(xs)
        sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
        p10 = s[int(0.10 * (len(s) - 1))]
        p90 = s[int(0.90 * (len(s) - 1))]
        frac_pos = sum(1 for x in xs if x > 0) / len(xs)
        print(f"  {name:<12}  mean={m:+7.3f}pp  median={med:+7.3f}pp  "
              f"sd={sd:5.2f}  p10={p10:+.2f}  p90={p90:+.2f}  frac>0={frac_pos:.2f}")
        return dict(mean=m, median=med, sd=sd, p10=p10, p90=p90, frac_pos=frac_pos)

    print("\n=== Stats descriptivos ===")
    stats_raw = stats_line("edge_raw", edge_raw_list)
    stats_adj = stats_line("edge_adj", edge_adj_list)
    stats_ema = stats_line("basis_ema", [b * 1e4 for b in ema_list])
    print("  (basis_ema en bps)")

    # ---------- 5) bootstrap-block CI sobre edge_adj mean ----------
    print("\n=== Bootstrap-block CI 95% sobre mean(edge_adj) ===")
    rng = random.Random(RNG_SEED)
    ci = bootstrap_block_ci(edge_adj_list, BLOCK_SIZE, N_REPLICAS, ALPHA, rng)
    print(f"  point estimate mean = {ci['point_estimate']:+.3f}pp")
    print(f"  CI 95%              = [{ci['ci_lo']:+.3f}, {ci['ci_hi']:+.3f}]pp")
    print(f"  excludes zero       = {ci['excludes_zero']}")
    print(f"  sign positive       = {ci['sign_positive']}")

    # ---------- 6) veredicto ----------
    verdict = "PASS regla #1" if ci["sign_positive"] else (
        "FAIL regla #1 (CI incluye o excluye por debajo de 0)")
    print(f"\n=== Veredicto regla #1 (5B): {verdict} ===")

    # ---------- 7) persistir artefacto ----------
    out_path = f"/tmp/artefacto1_edge_adj_{date.today().isoformat().replace('-','')}.json"
    payload = {
        "artefacto": "1_edge_adj_basis_ema3d_at_call",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "preregistro_ref": "preregistro_5b_20260709.md",
        "parametros_sesion_20260721": {
            "block_size": BLOCK_SIZE,
            "n_replicas": N_REPLICAS,
            "alpha": ALPHA,
            "rng_seed": RNG_SEED,
            "ema_halflife_hours": EMA_HALFLIFE_H,
        },
        "filtros_aplicados": [
            "brti_proxy_n_venues >= 3",
            "features_max_age_s <= 120",
            "vol_regime_ratio IS NOT NULL",
            "kalshi_strike/no_at_strike válidos, no_at_strike ∉ {0,1}",
            "sigma_h > 0",
        ],
        "corte_ventana_desde_iso": CUTOFF_START_ISO,
        "n_rows_input": len(rows),
        "n_rows_sin_basis_previo": n_no_basis,
        "n_efectivo": N,
        "stats_edge_raw_pp": stats_raw,
        "stats_edge_adj_pp": stats_adj,
        "stats_basis_ema_bps": stats_ema,
        "bootstrap_ci_edge_adj": ci,
        "veredicto_regla_1": verdict,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nartefacto JSON: {out_path}")


if __name__ == "__main__":
    main()
