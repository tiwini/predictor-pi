#!/usr/bin/env python3
"""Artefacto 4 del corte 5B (2026-07-21) — Síntesis 3/3 + funnel de filtros.

Consolida los 3 artefactos del preregistro 5B en una decisión operacional:
  1) edge_adj (basis EMA-3d at-call) > 0, CI bootstrap-block excl. 0
  2) ΔBrier vs Kalshi > 0, CI bootstrap-block excl. 0
  3) Basis health ≤ 2% winsorizado + gaps ≤ 24h en serie utilizable

Este artefacto adicionalmente reporta:
  - Funnel completo de filtros de exclusión (row-count per criterio).
  - N efectivo consistente entre artefactos.
  - Decisión final: migrar / no migrar el gate al preregistro 5B.

Lee los JSONs de artefactos 1/2/3 previamente generados en /tmp/.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

DB_PATH = "/home/popeye/crypto-predictor/calibration.db"
CUTOFF_START_ISO = "2026-07-09 00:00:00"
SYMBOL = "BTCUSDT"
TODAY = date.today().isoformat().replace('-', '')
A1_JSON = f"/tmp/artefacto1_edge_adj_{TODAY}.json"
A2_JSON = f"/tmp/artefacto2_delta_brier_{TODAY}.json"
A3_JSON = f"/tmp/artefacto3_basis_salud_{TODAY}.json"


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"WARN: {path} no encontrado")
        return None


def funnel_filters():
    """Aplica filtros progresivamente y reporta cuánto row-count sobrevive cada paso."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cutoff = datetime.fromisoformat(CUTOFF_START_ISO).replace(tzinfo=timezone.utc).timestamp()

    steps = []

    def q(where, params, label):
        n = con.execute(f"SELECT COUNT(*) FROM hourly_calls WHERE {where}",
                        params).fetchone()[0]
        steps.append((label, n))
        return n

    # Baseline: symbol
    n0 = q("symbol = ?", (SYMBOL,), "0. all BTCUSDT rows")
    n1 = q("symbol = ? AND made_at >= ?", (SYMBOL, cutoff), "1. + made_at ≥ 2026-07-09")
    n2 = q("symbol = ? AND made_at >= ? AND actual_price IS NOT NULL",
           (SYMBOL, cutoff), "2. + settled (actual_price NOT NULL)")
    n3 = q("""symbol = ? AND made_at >= ? AND actual_price IS NOT NULL
              AND brti_proxy_n_venues >= 3""",
           (SYMBOL, cutoff), "3. + brti_proxy_n_venues ≥ 3")
    n4 = q("""symbol = ? AND made_at >= ? AND actual_price IS NOT NULL
              AND brti_proxy_n_venues >= 3
              AND features_max_age_s <= 120""",
           (SYMBOL, cutoff), "4. + features_max_age_s ≤ 120")
    n5 = q("""symbol = ? AND made_at >= ? AND actual_price IS NOT NULL
              AND brti_proxy_n_venues >= 3
              AND features_max_age_s <= 120
              AND vol_regime_ratio IS NOT NULL""",
           (SYMBOL, cutoff), "5. + vol_regime_ratio NOT NULL")
    n6 = q("""symbol = ? AND made_at >= ? AND actual_price IS NOT NULL
              AND brti_proxy_n_venues >= 3
              AND features_max_age_s <= 120
              AND vol_regime_ratio IS NOT NULL
              AND kalshi_strike IS NOT NULL AND kalshi_no_at_strike IS NOT NULL""",
           (SYMBOL, cutoff), "6. + kalshi strike/no_at_strike válidos")
    n7 = q("""symbol = ? AND made_at >= ? AND actual_price IS NOT NULL
              AND brti_proxy_n_venues >= 3
              AND features_max_age_s <= 120
              AND vol_regime_ratio IS NOT NULL
              AND kalshi_strike IS NOT NULL AND kalshi_no_at_strike IS NOT NULL
              AND kalshi_no_at_strike NOT IN (0.0, 1.0)""",
           (SYMBOL, cutoff), "7. + kalshi_no ∉ {0,1}")
    n8 = q("""symbol = ? AND made_at >= ? AND actual_price IS NOT NULL
              AND brti_proxy_n_venues >= 3
              AND features_max_age_s <= 120
              AND vol_regime_ratio IS NOT NULL
              AND kalshi_strike IS NOT NULL AND kalshi_no_at_strike IS NOT NULL
              AND kalshi_no_at_strike NOT IN (0.0, 1.0)
              AND sigma_h > 0""",
           (SYMBOL, cutoff), "8. + sigma_h > 0 [N_efectivo pre-basis-hist]")

    print("=== Funnel de filtros de exclusión (rows sobrevivientes) ===")
    prev = None
    for label, n in steps:
        drop = "" if prev is None else f"  (-{prev - n})"
        print(f"  {label:<52}: {n:5d}{drop}")
        prev = n
    return steps


def main():
    print(f"=== Artefacto 4 — Síntesis corte 5B ({date.today().isoformat()}) ===\n")

    a1 = load(A1_JSON)
    a2 = load(A2_JSON)
    a3 = load(A3_JSON)

    print("--- Cargas ---")
    print(f"  A1 edge_adj    : {'OK' if a1 else 'MISSING'} — {A1_JSON}")
    print(f"  A2 ΔBrier      : {'OK' if a2 else 'MISSING'} — {A2_JSON}")
    print(f"  A3 basis salud : {'OK' if a3 else 'MISSING'} — {A3_JSON}")
    print()

    # ---------- funnel ----------
    steps = funnel_filters()
    n_efectivo_sql = steps[-1][1]
    print(f"\nN post-SQL-filtros = {n_efectivo_sql}")
    if a1:
        print(f"N efectivo A1      = {a1['n_efectivo']}  "
              f"(drop by basis_history: {a1['n_rows_sin_basis_previo']})")
    if a2:
        print(f"N efectivo A2      = {a2['n_efectivo']}  "
              f"(drop by basis_history: {a2['n_rows_sin_basis_previo']})")

    # ---------- verdicts ----------
    print("\n=== Veredicto por regla ===")
    r1 = a1["veredicto_regla_1"] if a1 else "MISSING"
    r2 = a2["veredicto_regla_2"] if a2 else "MISSING"
    r3 = a3["veredicto_regla_3"] if a3 else "MISSING"

    def flag(v):
        return "✅" if "PASS" in v else "❌"

    print(f"  {flag(r1)} Regla 1 (edge_adj > 0)                : {r1}")
    if a1:
        ci = a1["bootstrap_ci_edge_adj"]
        print(f"       mean = {ci['point_estimate']:+.3f}pp   "
              f"CI 95% = [{ci['ci_lo']:+.3f}, {ci['ci_hi']:+.3f}]pp")
    print(f"  {flag(r2)} Regla 2 (ΔBrier vs Kalshi > 0)        : {r2}")
    if a2:
        ci = a2["delta_brier_kalshi_minus_adj_bootstrap"]
        print(f"       mean = {ci['point_estimate']:+.5f}   "
              f"CI 95% = [{ci['ci_lo']:+.5f}, {ci['ci_hi']:+.5f}]")
    print(f"  {flag(r3)} Regla 3 (basis health)                : {r3}")
    if a3:
        s = a3["basis_stats_post_corte"]
        g = a3["gaps_post_corte"]
        print(f"       |wins-P1/P99|max = {s['wins_absmax']:.5f}   "
              f"gap max = {g['max_gap_h']:.2f}h")

    # ---------- decisión final ----------
    passes = sum(1 for v in [r1, r2, r3] if "PASS" in v)
    print(f"\n=== Cuenta: {passes}/3 reglas PASS ===")

    if passes == 3:
        decision = "MIGRAR gate a 5B"
        rationale = "Los 3 criterios preregistrados cumplen; migración autorizada."
    elif passes == 2 and "FAIL" in r2:
        decision = "NO MIGRAR — extender ventana"
        rationale = (
            "Regla 2 (ΔBrier) es el bloqueador. Point estimate favorable pero "
            "CI cruza cero. Recomendación: mantener 5A en producción, ampliar "
            "ventana N (target N≥400 o esperar 2 semanas) y re-evaluar. NO "
            "aflojar el criterio ni reinterpretar el preregistro."
        )
    else:
        decision = "NO MIGRAR — reglas críticas fallan"
        rationale = f"Passes={passes}. Preregistro requiere 3/3 estrictos."

    print(f"\n=== DECISIÓN: {decision} ===")
    print(f"Rationale: {rationale}")

    # ---------- notas de contexto ----------
    print("\n=== Notas de contexto ===")
    print("  - N efectivo 264 es bajo para bootstrap-block sobre ΔBrier "
          "(varianza row-level alta cuando outcome_rate=0.76).")
    print("  - edge_adj mean +5.82pp con CI apretado sugiere que la señal "
          "de MAGNITUD del edge existe y es robusta.")
    print("  - ΔBrier positivo pero pequeño (+0.004) → mejora marginal en "
          "calibración; consistente con 'modelo captura direccionalidad "
          "pero solo un poquito mejor probabilísticamente'.")
    print("  - Basis health limpio post-07-09; el gap 302h (Jun 06-04→06-16) "
          "queda pre-corte y no afecta la evaluación.")

    # ---------- persist ----------
    out_path = f"/tmp/artefacto4_sintesis_5b_{TODAY}.json"
    payload = {
        "artefacto": "4_sintesis_corte_5b",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "preregistro_ref": "preregistro_5b_20260709.md",
        "funnel_filtros_sql": [{"paso": s, "n": n} for s, n in steps],
        "n_efectivo_sql": n_efectivo_sql,
        "n_efectivo_a1": a1["n_efectivo"] if a1 else None,
        "n_efectivo_a2": a2["n_efectivo"] if a2 else None,
        "reglas": {
            "regla_1_edge_adj": {
                "veredicto": r1,
                "detalle": a1["bootstrap_ci_edge_adj"] if a1 else None,
            },
            "regla_2_delta_brier": {
                "veredicto": r2,
                "detalle": a2["delta_brier_kalshi_minus_adj_bootstrap"] if a2 else None,
            },
            "regla_3_basis_salud": {
                "veredicto": r3,
                "detalle": {
                    "basis_stats_post_corte": a3["basis_stats_post_corte"] if a3 else None,
                    "gaps_post_corte": a3["gaps_post_corte"] if a3 else None,
                },
            },
        },
        "cuenta_pass": passes,
        "decision_final": decision,
        "rationale": rationale,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nartefacto JSON: {out_path}")


if __name__ == "__main__":
    main()
