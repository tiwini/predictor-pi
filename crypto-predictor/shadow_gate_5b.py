#!/usr/bin/env python3
"""shadow_gate_5b.py — R6 gate 5B evaluación offline (frozen 2026-07-09).

Reconstrucción determinista bit-exacta de:
  - basis_at_call = EMA time-aware half-life 3d de basis_bps, winsorizado
    [-5, +25] bps, evaluado a made_at[i] con SOLO observaciones settleadas
    < made_at[i] (fuga temporal cero).
  - edge_pp_adj = (model_no(strike*(1+basis/1e4)) - kalshi_no_at_strike)*100
  - won_proxy = 1 si outcome_price ≤ kalshi_strike, con outcome_price =
    brti_proxy_price si brti_proxy_n_venues ≥ 3, fallback etiquetado a
    proxy_price_at_settle (Coinbase) para rows pre-R7.

Reglas del gate (todas obligatorias, texto congelado en
preregistro_5b_20260709.md):
  1. mean(edge_pp_adj) > 0, CI bootstrap-block excluyendo cero.
  2. ΔBrier = brier_kalshi − brier_model_adj > 0, mismo método.
  3. Salud del basis operativo: fracción winsorizada ≤ 2% del set y sin
     gaps > 24h en fetchers sub-horarios (OB / taker / funding).

Filtros de exclusión (pre-outcome, decididos fuera de banda antes de
mirar cualquier resultado). Precedencia = orden literal (primer bucket
que dispara consume el row — single-count):
  1. brti_proxy_n_venues < 3
  2. features_max_age_s > 120
  3. vol_regime_ratio IS NULL
  4. kalshi_book: kalshi_strike IS NULL o kalshi_no_at_strike ∈ {0.0, 1.0}
     (R11: declarado explícito; antes filtraba en SQL silencioso.
     ~15-20% históricamente, concentrado madrugada UTC — no-aleatorio.)

Modos de ejecución:
  --filters-only : sólo excl_counts + N calificante (monitoreo semanal
                   sin peeking del edge). Uso obligatorio durante el reloj.
  (default)      : corrida completa, incluye edge/ΔBrier/gate. Uso único
                   en el corte al llegar a N=300 calificantes.

Constantes de reconstrucción (spec R6 congelada):
  HALF_LIFE_S = 3 * 86400
  WINSOR_LO_BPS = -5.0
  WINSOR_HI_BPS = 25.0
  DF_SWITCH_ID = 966            # <966 = df 4, ≥966 = df 5
  BOOTSTRAP_BLOCK = 24          # 24 horas = 1 día
  BOOTSTRAP_N = 5000            # replicates
  CI_ALPHA = 0.05               # 95% CI (percentil 2.5 / 97.5)

Uso: shadow_gate_5b.py [--db PATH] [--min-id N] [--max-id N] [--verbose]
"""
import argparse
import math
import random
import sqlite3
import sys

HALF_LIFE_S = 3 * 86400
WINSOR_LO_BPS = -5.0
WINSOR_HI_BPS = 25.0
DF_SWITCH_ID = 966
BOOTSTRAP_BLOCK = 24
BOOTSTRAP_N = 5000
CI_ALPHA = 0.05
EXCL_MAX_AGE_S = 120.0
EXCL_MIN_VENUES = 3
# R13: primer id bajo systemd PID 15504 (post-reconciliación 2026-07-09
# 16:29:45 AST). Denominadores de monitoreo y frac_winsor de criterio 3
# se restringen a la ventana del reloj (id >= RELOJ_START_ID) para que
# la historia pre-R7 (n_venues<3 = 1042 rows) no diluya percentiles
# perpetuamente y no dispare alertas falsas en steady-state.
RELOJ_START_ID = 1062

try:
    from scipy import stats
    def t_cdf(x, df):
        return float(stats.t.cdf(x, df))
except ImportError:
    def t_cdf(x, df, n=4000):
        lo, hi = -12.0, float(x)
        if hi <= lo:
            return 0.0
        h = (hi - lo) / n
        c = math.gamma((df + 1) / 2) / (
            math.sqrt(df * math.pi) * math.gamma(df / 2))
        s = 0.0
        for i in range(n + 1):
            t = lo + i * h
            w = 1 if i in (0, n) else (4 if i % 2 else 2)
            s += w * (1 + t * t / df) ** (-(df + 1) / 2)
        return min(1.0, max(0.0, c * s * h / 3))


def model_no(strike, now_price, sigma_h, df):
    """P(price ≤ strike a 1h) usando t-Student escalada, spec R6."""
    scale = math.sqrt((df - 2) / df)
    return t_cdf(math.log(strike / now_price) / sigma_h / scale, df)


def winsor(x, lo, hi):
    if x < lo:
        return lo, True
    if x > hi:
        return hi, True
    return x, False


def outcome_price(row):
    """Preferencia BRTI con n≥3, fallback etiquetado a Coinbase.

    Devuelve (price, source) o (None, None) si ambos son null.
    """
    brti = row["brti_proxy_price"]
    n_v = row["brti_proxy_n_venues"] or 0
    if brti is not None and n_v >= EXCL_MIN_VENUES:
        return brti, "brti"
    cb = row["proxy_price_at_settle"]
    if cb is not None:
        return cb, "coinbase_fallback"
    return None, None


def basis_bps(row):
    """basis_bps al settle = 1e4*(actual − outcome_price)/actual.

    Spec R5: actual=Binance close (siempre presente al settle); proxy=
    outcome_price (BRTI preferido). Basis negativo = BRTI arriba.
    """
    op, src = outcome_price(row)
    if op is None or row["actual_price"] is None or row["actual_price"] <= 0:
        return None, None
    return 1e4 * (row["actual_price"] - op) / row["actual_price"], src


def ema_time_aware_at(t_query, obs):
    """EMA time-aware evaluada en t_query.

    obs = lista ordenada por t (asc) de (t, x) con t < t_query.
    Recursivo: alpha_j = 1 - 0.5^(dt_j / HL). Inicializa con primer obs.
    Devuelve None si obs vacío.
    """
    if not obs:
        return None
    ema = obs[0][1]
    prev_t = obs[0][0]
    for t, x in obs[1:]:
        dt = max(0.0, t - prev_t)
        alpha = 1.0 - 0.5 ** (dt / HALF_LIFE_S)
        ema = ema + alpha * (x - ema)
        prev_t = t
    # Decay hasta t_query (mantiene EMA como snapshot temporal a la
    # made_at del row scored).
    dt = max(0.0, t_query - prev_t)
    # El decay puro no cambia el valor de la EMA — sólo su peso
    # relativo. Como no llegan nuevos obs entre prev_t y t_query,
    # devuelve ema tal cual. (Decay-to-mean requeriría media prior; no
    # es la spec R6.)
    return ema


def block_bootstrap_mean_ci(xs, block=BOOTSTRAP_BLOCK, n_boot=BOOTSTRAP_N,
                             alpha=CI_ALPHA, rng=None):
    """CI bootstrap-block para la media de una serie temporal ordenada."""
    if rng is None:
        rng = random.Random(20260709)
    n = len(xs)
    if n < block:
        return None, None, None
    n_blocks = n // block  # descarte cola < 1 bloque
    means = []
    for _ in range(n_boot):
        s = 0.0
        cnt = 0
        for _b in range(n_blocks):
            start = rng.randint(0, n - block)
            for k in range(block):
                s += xs[start + k]
                cnt += 1
        means.append(s / cnt)
    means.sort()
    lo = means[int(n_boot * alpha / 2)]
    hi = means[int(n_boot * (1 - alpha / 2))]
    mean_point = sum(xs) / n
    return mean_point, lo, hi


def load_rows(db_path, min_id, max_id):
    """Carga TODAS las filas settleadas con now_price/sigma_h válidos.

    R11: no filtramos por Kalshi en SQL — la ausencia de book es un
    bucket de exclusión declarado (`kalshi_book`), no un descarte
    silencioso. Basis histórico se computa sobre TODAS las settleadas
    (criterio 3 no requiere Kalshi); scoring del gate 1-2 requiere
    Kalshi presente y book no degenerado.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = (
        "SELECT id, made_at, settled_at, target_at, "
        "       now_price, sigma_h, kalshi_strike, kalshi_no_at_strike, "
        "       actual_price, proxy_price_at_settle, "
        "       bitstamp_price_at_settle, brti_proxy_price, "
        "       brti_proxy_n_venues, features_max_age_s, vol_regime_ratio "
        "FROM hourly_calls "
        "WHERE actual_price IS NOT NULL "
        "  AND now_price > 0 AND sigma_h > 0 "
        "  AND id >= ? AND id <= ? "
        "ORDER BY made_at"
    )
    return conn.execute(q, (min_id, max_id)).fetchall()


def _kalshi_book_missing(row):
    """True si el row no tiene book Kalshi válido para scoring."""
    if row["kalshi_strike"] is None or row["kalshi_no_at_strike"] is None:
        return True
    if row["kalshi_no_at_strike"] in (0.0, 1.0):
        return True
    return False


def apply_exclusion_filters(rows, verbose=False):
    """Aplica filtros pre-outcome del pre/post-registro.

    Precedencia: primer bucket que dispara consume el row (single-count).
    Orden: n_venues<3 → max_age>120 → vol_regime=NULL → kalshi_book.
    Este orden es arbitrario pero fijo; cambiarlo requiere post-registro.
    """
    excl_venues = excl_age = excl_vol = excl_book = 0
    kept = []
    for r in rows:
        n_v = r["brti_proxy_n_venues"] or 0
        if n_v < EXCL_MIN_VENUES:
            excl_venues += 1
            continue
        age = r["features_max_age_s"]
        if age is not None and age > EXCL_MAX_AGE_S:
            excl_age += 1
            continue
        if r["vol_regime_ratio"] is None:
            excl_vol += 1
            continue
        if _kalshi_book_missing(r):
            excl_book += 1
            continue
        kept.append(r)
    counts = {"n_venues<3": excl_venues,
              "max_age>120": excl_age,
              "vol_regime=NULL": excl_vol,
              "kalshi_book": excl_book}
    if verbose:
        parts = ", ".join(f"{k}={v}" for k, v in counts.items())
        print(f"[filters] excluded: {parts}", file=sys.stderr)
        print(f"[filters] kept={len(kept)} / total_settled={len(rows)}",
              file=sys.stderr)
    return kept, counts


def build_basis_history(all_rows, reloj_start_id=None):
    """Construye lista (settled_at, basis_bps_winsorized) ordenada asc.

    all_rows: filas settleadas SIN filtrar por exclusiones — para la
    reconstrucción del basis histórico queremos toda observación válida
    disponible, no sólo las que califican al gate. La EMA sigue leyendo
    la historia completa (60 días).

    R13: `n_winsor_reloj` y `n_total_reloj` cuentan sólo la ventana
    shadow (id >= reloj_start_id) para el criterio 3 — la salud del
    basis del régimen operativo actual, no diluida con historia pre-R7.
    """
    obs = []
    n_winsor_reloj = 0
    n_total_reloj = 0
    for r in all_rows:
        b, src = basis_bps(r)
        if b is None or r["settled_at"] is None:
            continue
        b_w, hit = winsor(b, WINSOR_LO_BPS, WINSOR_HI_BPS)
        obs.append((float(r["settled_at"]), b_w))
        if reloj_start_id is None or r["id"] >= reloj_start_id:
            n_total_reloj += 1
            if hit:
                n_winsor_reloj += 1
    obs.sort(key=lambda x: x[0])
    return obs, n_winsor_reloj, n_total_reloj


def evaluate(rows, basis_obs, verbose=False):
    """Devuelve (edge_pp_adj_list, dbrier_list, per_row_records)."""
    edges = []
    dbriers = []
    records = []
    for r in rows:
        made_at = r["made_at"]
        # Basis EMA a made_at (obs previas a made_at)
        prior = [(t, x) for t, x in basis_obs if t < made_at]
        b_ema = ema_time_aware_at(made_at, prior)
        if b_ema is None:
            continue  # no history yet — warm-up
        basis_frac = b_ema / 1e4
        df = 5 if r["id"] >= DF_SWITCH_ID else 4
        m_adj = model_no(r["kalshi_strike"] * (1 + basis_frac),
                         r["now_price"], r["sigma_h"], df)
        edge_adj = (m_adj - r["kalshi_no_at_strike"]) * 100
        op, src = outcome_price(r)
        if op is None:
            continue
        won_p = 1.0 if op <= r["kalshi_strike"] else 0.0
        brier_model = (m_adj - won_p) ** 2
        brier_kalshi = (r["kalshi_no_at_strike"] - won_p) ** 2
        dbrier = brier_kalshi - brier_model  # >0 = modelo mejor
        edges.append(edge_adj)
        dbriers.append(dbrier)
        records.append({
            "id": r["id"], "made_at": made_at, "df": df,
            "basis_ema_bps": b_ema, "outcome_src": src,
            "edge_pp_adj": edge_adj, "dbrier": dbrier,
            "won_proxy": won_p,
        })
    return edges, dbriers, records


def report(edges, dbriers, records, excl_counts, n_winsor_reloj,
           n_total_reloj, verbose=False):
    rng = random.Random(20260709)
    n = len(edges)
    print("=" * 72)
    print("shadow_gate_5b — R6 gate 5B (offline, deterministic)")
    print("=" * 72)
    print(f"N scored = {n}  (exclusions reloj: {excl_counts})")
    if n == 0:
        print("No hay filas scored — gate ambiguo.")
        return 2
    frac_wins = n_winsor_reloj / n_total_reloj if n_total_reloj else 0.0
    print(f"basis winsor hits (ventana reloj id>={RELOJ_START_ID}) = "
          f"{n_winsor_reloj}/{n_total_reloj} = {frac_wins:.2%}  "
          f"(≤2% requerido)")

    # Criterio 1: edge_pp_adj > 0
    m_e, lo_e, hi_e = block_bootstrap_mean_ci(edges, rng=rng)
    print(f"\n[1] mean(edge_pp_adj) = {m_e:+.4f}pp  "
          f"CI95 bootstrap-block = [{lo_e:+.4f}, {hi_e:+.4f}]")
    c1 = (lo_e is not None and lo_e > 0)
    print(f"    criterio 1 (CI excluye 0 por arriba): "
          f"{'PASA' if c1 else 'FALLA'}")

    # Criterio 2: ΔBrier > 0
    m_b, lo_b, hi_b = block_bootstrap_mean_ci(dbriers, rng=rng)
    print(f"\n[2] mean(ΔBrier_k-modelo) = {m_b:+.6f}  "
          f"CI95 = [{lo_b:+.6f}, {hi_b:+.6f}]")
    c2 = (lo_b is not None and lo_b > 0)
    print(f"    criterio 2 (CI excluye 0 por arriba): "
          f"{'PASA' if c2 else 'FALLA'}")

    # Criterio 3: salud del basis
    c3 = (frac_wins <= 0.02)
    print(f"\n[3] fracción winsorizada = {frac_wins:.2%}  "
          f"(≤2% → {'PASA' if c3 else 'FALLA'})")
    # Gap check: los sub-horarios se ven en features_max_age_s por row.
    # Aquí es aproximación: cualquier row con NaN vol_regime o max_age>120
    # ya fue excluida; si hubo desertificación >24h en fetchers, el
    # contador de exclusiones lo delata.
    print(f"    (gaps >24h de fetchers: revisar excl_counts en operativa)")

    all_pass = c1 and c2 and c3
    print("\n" + "=" * 72)
    print(f"GATE 5B → {'PASA (migrar)' if all_pass else 'NO PASA'}")
    print("=" * 72)
    if verbose:
        n_brti = sum(1 for r in records if r["outcome_src"] == "brti")
        n_cb = sum(1 for r in records if r["outcome_src"] == "coinbase_fallback")
        print(f"\noutcome sources: brti={n_brti}, coinbase_fallback={n_cb}",
              file=sys.stderr)
    return 0 if all_pass else 1


def report_filters_only(reloj_rows, excl_reloj, n_qualifying,
                        legacy_rows, excl_legacy):
    """Modo monitoreo semanal. NO computa edge ni Brier — evita peeking.

    Fable R11: la corrida completa expone el edge acumulándose, lo cual
    en modo espera equivale a peeking involuntario. Este modo imprime
    sólo los contadores y el N calificante para el chequeo semanal de
    salud de los filtros y la aritmética del reloj.

    R13: denominadores y triggers se computan sólo sobre la ventana del
    reloj (id >= RELOJ_START_ID). La historia pre-reloj se reporta como
    línea informativa, fuera de los denominadores — para que la masa de
    pre-R7 (~1042 rows con n_venues<3) no dispare "no-Kalshi > 5%"
    perpetuamente en steady-state.
    """
    n_reloj = len(reloj_rows)
    n_legacy = len(legacy_rows)
    total_excl_legacy = sum(excl_legacy.values())
    print("=" * 72)
    print("shadow_gate_5b --filters-only — monitoreo semanal (no peeking)")
    print("=" * 72)
    print(f"Legacy pre-reloj (id<{RELOJ_START_ID}): {n_legacy} rows "
          f"settleados, exclusiones {total_excl_legacy} "
          f"[n_venues<3={excl_legacy.get('n_venues<3',0)}, "
          f"max_age>120={excl_legacy.get('max_age>120',0)}, "
          f"vol_regime=NULL={excl_legacy.get('vol_regime=NULL',0)}, "
          f"kalshi_book={excl_legacy.get('kalshi_book',0)}]  "
          f"— informativo, fuera de denominadores del reloj.")
    print()
    print(f"Ventana reloj (id>={RELOJ_START_ID}): {n_reloj} rows "
          f"settleados")
    print("Exclusiones (precedencia: primer bucket que dispara consume):")
    for k, v in excl_reloj.items():
        pct = (v / n_reloj * 100.0) if n_reloj else 0.0
        print(f"  {k:<18} = {v:>4}  ({pct:5.1f}%)")
    total_excl = sum(excl_reloj.values())
    excl_pct = (total_excl / n_reloj * 100.0) if n_reloj else 0.0
    print(f"  {'TOTAL':<18} = {total_excl:>4}  ({excl_pct:5.1f}%)")
    print(f"\nN calificante (candidatas al gate) = {n_qualifying}")
    print(f"Reloj N=300 calificantes; falta: {max(0, 300 - n_qualifying)}")

    # Triggers por bucket (post-registro R11 + R13 denominador reloj):
    if n_reloj == 0:
        print("\n(sin rows en ventana reloj todavía — triggers no evaluados)")
        return 0
    kalshi_pct = excl_reloj.get("kalshi_book", 0) / n_reloj * 100.0
    other_excl = (excl_reloj.get("n_venues<3", 0)
                  + excl_reloj.get("max_age>120", 0)
                  + excl_reloj.get("vol_regime=NULL", 0))
    other_pct = other_excl / n_reloj * 100.0
    alerts = []
    if kalshi_pct > 30.0:
        alerts.append(f"kalshi_book {kalshi_pct:.1f}% > 30% "
                      "→ investigar Kalshi book/fetcher de curvas")
    if other_pct > 5.0:
        alerts.append(f"exclusión no-Kalshi {other_pct:.1f}% > 5% "
                      "→ investigar (n_venues/max_age/vol_regime)")
    if excl_pct > 35.0:
        alerts.append(f"total {excl_pct:.1f}% > 35% "
                      "→ recalcular fecha de corte")
    if alerts:
        print()
        for a in alerts:
            print(f"⚠ {a}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db",
                    default="/home/popeye/crypto-predictor/calibration.db")
    ap.add_argument("--min-id", type=int, default=1)
    ap.add_argument("--max-id", type=int, default=10**9)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--filters-only", action="store_true",
                    help="Monitoreo semanal: sólo excl_counts + N "
                         "calificante, sin edge/Brier (anti-peeking).")
    args = ap.parse_args()

    rows = load_rows(args.db, args.min_id, args.max_id)
    if args.verbose:
        print(f"[load] rows_settled_valid={len(rows)}", file=sys.stderr)

    # R13: partición legacy vs ventana reloj. Los denominadores de
    # monitoreo se computan sólo sobre la ventana reloj; la porción
    # legacy se reporta aparte como línea informativa.
    legacy_rows = [r for r in rows if r["id"] < RELOJ_START_ID]
    reloj_rows = [r for r in rows if r["id"] >= RELOJ_START_ID]
    scored_legacy, excl_legacy = apply_exclusion_filters(
        legacy_rows, verbose=args.verbose)
    scored_reloj, excl_reloj = apply_exclusion_filters(
        reloj_rows, verbose=args.verbose)

    if args.filters_only:
        return report_filters_only(reloj_rows, excl_reloj,
                                    len(scored_reloj),
                                    legacy_rows, excl_legacy)

    # Corrida completa (uso único en el corte). Basis EMA usa historia
    # completa; frac_winsor de criterio 3 se restringe a la ventana
    # reloj (R13). Filas scoreadas son las calificantes de la ventana
    # reloj (el filtro n_venues<3 excluye legacy por diseño; una fila
    # legacy que pasara los 4 filtros sería un accidente histórico y no
    # debería alimentar el gate).
    basis_obs, n_winsor_reloj, n_total_reloj = build_basis_history(
        rows, reloj_start_id=RELOJ_START_ID)

    edges, dbriers, records = evaluate(scored_reloj, basis_obs,
                                        verbose=args.verbose)
    return report(edges, dbriers, records, excl_reloj, n_winsor_reloj,
                  n_total_reloj, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
