#!/usr/bin/env python3
"""Artefacto 3 del corte 5B (2026-07-21) — Salud del basis operativo.

Regla del preregistro (5B punto 3):
  Salud del basis ≤2% winsorizado, sin gaps >24h en la serie utilizable.
  Sub-features (OB, taker, funding, features_max_age): consistencia sobre el corte.

Métrica primaria: |basis| winsorizado al P99 debe ser ≤ 2% (0.02).
Métrica secundaria: gap máximo en la serie de basis histórico usable ≤ 24h.
Verificación auxiliar: cobertura de features intrahora (OB, taker, funding) y
distribución de features_max_age_s.

Parámetros:
  - Ventana de análisis: rows post-2026-07-09 (corte 5B) + basis history global.
  - Winsorización: two-sided al P1/P99 para robustness al reportar |basis| max.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import date, datetime, timezone

DB_PATH = "/home/popeye/crypto-predictor/calibration.db"
CUTOFF_START_ISO = "2026-07-09 00:00:00"
SYMBOL = "BTCUSDT"
BASIS_LIMIT = 0.02  # 2% winsorized
GAP_LIMIT_H = 24.0


def percentile(sorted_xs, p):
    if not sorted_xs:
        return None
    if p <= 0:
        return sorted_xs[0]
    if p >= 1:
        return sorted_xs[-1]
    idx = p * (len(sorted_xs) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_xs[lo]
    frac = idx - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cutoff = datetime.fromisoformat(CUTOFF_START_ISO).replace(tzinfo=timezone.utc).timestamp()

    # ---------- 1) basis history global ----------
    hist_rows = con.execute("""
        SELECT settled_at, actual_price, brti_proxy_price
        FROM hourly_calls
        WHERE symbol = ? AND actual_price IS NOT NULL
          AND brti_proxy_price IS NOT NULL AND settled_at IS NOT NULL
          AND actual_price > 0
        ORDER BY settled_at ASC
    """, (SYMBOL,)).fetchall()
    basis_all = [
        (r["settled_at"], (r["actual_price"] - r["brti_proxy_price"]) / r["actual_price"])
        for r in hist_rows
    ]
    print(f"basis history global: {len(basis_all)} rows")
    if basis_all:
        t0 = datetime.fromtimestamp(basis_all[0][0], tz=timezone.utc).isoformat()[:19]
        t1 = datetime.fromtimestamp(basis_all[-1][0], tz=timezone.utc).isoformat()[:19]
        print(f"  ventana: {t0} → {t1}")

    # ---------- 2) basis post-corte (5B window) ----------
    basis_corte = [(t, b) for t, b in basis_all if t >= cutoff]
    print(f"basis post-corte 5B (≥ 2026-07-09): {len(basis_corte)} rows")

    # ---------- 3) descriptivos + winsorización ----------
    def describe(name, series):
        if not series:
            print(f"  {name}: EMPTY")
            return None
        s = sorted(series)
        mean = statistics.mean(series)
        med = statistics.median(series)
        p01 = percentile(s, 0.01)
        p99 = percentile(s, 0.99)
        p05 = percentile(s, 0.05)
        p95 = percentile(s, 0.95)
        # winsorized abs max (two-sided al P1/P99)
        wins_absmax = max(abs(p01), abs(p99))
        print(f"  {name}:")
        print(f"    n={len(series)}  mean={mean:+.5f}  med={med:+.5f}")
        print(f"    p1={p01:+.5f}  p5={p05:+.5f}  p95={p95:+.5f}  p99={p99:+.5f}")
        print(f"    |wins-P1/P99| max = {wins_absmax:.5f} "
              f"({'PASS' if wins_absmax <= BASIS_LIMIT else 'FAIL'} vs {BASIS_LIMIT})")
        return dict(n=len(series), mean=mean, median=med,
                    p01=p01, p05=p05, p95=p95, p99=p99,
                    wins_absmax=wins_absmax,
                    pass_2pct=wins_absmax <= BASIS_LIMIT)

    print("\n=== Descriptivos basis (raw fraction, no bps) ===")
    stats_all = describe("basis GLOBAL", [b for _, b in basis_all])
    stats_corte = describe("basis POST-CORTE 5B", [b for _, b in basis_corte])

    # ---------- 4) gaps en basis ----------
    def gap_analysis(name, series_tb):
        if len(series_tb) < 2:
            print(f"  {name}: <2 rows, no gaps")
            return None
        diffs_h = []
        for i in range(1, len(series_tb)):
            dt_h = (series_tb[i][0] - series_tb[i - 1][0]) / 3600.0
            diffs_h.append(dt_h)
        s = sorted(diffs_h)
        max_gap = s[-1]
        # top-5 gaps con timestamp de arranque
        top = []
        for i in range(1, len(series_tb)):
            dt_h = (series_tb[i][0] - series_tb[i - 1][0]) / 3600.0
            if dt_h > 6:  # solo gaps notables
                t_prev = datetime.fromtimestamp(
                    series_tb[i - 1][0], tz=timezone.utc).isoformat()[:19]
                t_next = datetime.fromtimestamp(
                    series_tb[i][0], tz=timezone.utc).isoformat()[:19]
                top.append((dt_h, t_prev, t_next))
        top.sort(reverse=True)
        print(f"  {name}: max gap = {max_gap:.2f}h  "
              f"({'PASS' if max_gap <= GAP_LIMIT_H else 'FAIL'} vs {GAP_LIMIT_H}h)")
        print(f"    median gap = {percentile(s, 0.5):.2f}h  "
              f"p95 gap = {percentile(s, 0.95):.2f}h")
        if top[:5]:
            print(f"    top gaps (>6h):")
            for g, tp, tn in top[:5]:
                print(f"      {g:6.2f}h : {tp} → {tn}")
        return dict(max_gap_h=max_gap,
                    median_gap_h=percentile(s, 0.5),
                    p95_gap_h=percentile(s, 0.95),
                    top_gaps=[
                        {"gap_h": g, "from": tp, "to": tn}
                        for g, tp, tn in top[:5]
                    ],
                    pass_24h=max_gap <= GAP_LIMIT_H)

    print("\n=== Gaps en la serie basis ===")
    gaps_all = gap_analysis("basis GLOBAL", basis_all)
    gaps_corte = gap_analysis("basis POST-CORTE 5B", basis_corte)

    # ---------- 5) features intrahora / features_max_age_s sobre el corte ----------
    print("\n=== Cobertura features intrahora (rows post-corte, filtros base) ===")
    feat_rows = con.execute("""
        SELECT made_at, features_max_age_s, ob_imbalance, taker_buy_ratio,
               funding_rate, momentum_pct_per_min, brti_proxy_n_venues
        FROM hourly_calls
        WHERE symbol = ? AND made_at >= ?
        ORDER BY made_at ASC
    """, (SYMBOL, cutoff)).fetchall()
    n_total = len(feat_rows)
    n_ob = sum(1 for r in feat_rows if r["ob_imbalance"] is not None)
    n_taker = sum(1 for r in feat_rows if r["taker_buy_ratio"] is not None)
    n_fund = sum(1 for r in feat_rows if r["funding_rate"] is not None)
    n_mom = sum(1 for r in feat_rows if r["momentum_pct_per_min"] is not None)
    n_age = sum(1 for r in feat_rows if r["features_max_age_s"] is not None)
    n_age_le120 = sum(1 for r in feat_rows
                      if r["features_max_age_s"] is not None
                      and r["features_max_age_s"] <= 120)
    n_venues_ge3 = sum(1 for r in feat_rows
                       if r["brti_proxy_n_venues"] is not None
                       and r["brti_proxy_n_venues"] >= 3)
    print(f"  N total rows post-corte = {n_total}")
    print(f"  ob_imbalance non-null       : {n_ob}/{n_total} = {n_ob/n_total:.1%}")
    print(f"  taker_buy_ratio non-null    : {n_taker}/{n_total} = {n_taker/n_total:.1%}")
    print(f"  funding_rate non-null       : {n_fund}/{n_total} = {n_fund/n_total:.1%}")
    print(f"  momentum non-null           : {n_mom}/{n_total} = {n_mom/n_total:.1%}")
    print(f"  features_max_age_s non-null : {n_age}/{n_total} = {n_age/n_total:.1%}")
    print(f"  features_max_age_s ≤ 120    : {n_age_le120}/{n_total} = {n_age_le120/n_total:.1%}")
    print(f"  brti_proxy_n_venues ≥ 3     : {n_venues_ge3}/{n_total} = {n_venues_ge3/n_total:.1%}")

    ages = [r["features_max_age_s"] for r in feat_rows
            if r["features_max_age_s"] is not None]
    if ages:
        s = sorted(ages)
        print(f"  features_max_age_s dist: "
              f"med={percentile(s,0.5):.0f}s  p90={percentile(s,0.9):.0f}s  "
              f"p99={percentile(s,0.99):.0f}s  max={s[-1]:.0f}s")

    # ---------- 6) veredicto ----------
    fails = []
    if stats_corte and not stats_corte["pass_2pct"]:
        fails.append(f"basis wins-|P99| = {stats_corte['wins_absmax']:.4f} > {BASIS_LIMIT}")
    if gaps_corte and not gaps_corte["pass_24h"]:
        fails.append(f"gap max = {gaps_corte['max_gap_h']:.2f}h > {GAP_LIMIT_H}h")

    verdict = "PASS regla #3" if not fails else f"FAIL regla #3: {'; '.join(fails)}"
    print(f"\n=== Veredicto regla #3 (5B): {verdict} ===")

    out_path = f"/tmp/artefacto3_basis_salud_{date.today().isoformat().replace('-','')}.json"
    payload = {
        "artefacto": "3_salud_basis_operativo",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "preregistro_ref": "preregistro_5b_20260709.md",
        "corte_ventana_desde_iso": CUTOFF_START_ISO,
        "criterios": {
            "basis_wins_absmax_max": BASIS_LIMIT,
            "gap_max_horas": GAP_LIMIT_H,
        },
        "basis_stats_global": stats_all,
        "basis_stats_post_corte": stats_corte,
        "gaps_global": gaps_all,
        "gaps_post_corte": gaps_corte,
        "cobertura_features_post_corte": {
            "n_total": n_total,
            "ob_imbalance_non_null_frac": n_ob / n_total if n_total else None,
            "taker_buy_ratio_non_null_frac": n_taker / n_total if n_total else None,
            "funding_rate_non_null_frac": n_fund / n_total if n_total else None,
            "momentum_non_null_frac": n_mom / n_total if n_total else None,
            "features_max_age_non_null_frac": n_age / n_total if n_total else None,
            "features_max_age_le_120_frac": n_age_le120 / n_total if n_total else None,
            "brti_venues_ge_3_frac": n_venues_ge3 / n_total if n_total else None,
        },
        "veredicto_regla_3": verdict,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nartefacto JSON: {out_path}")


if __name__ == "__main__":
    main()
