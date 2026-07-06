"""Backfill columna `z` en hourly_calls (fable review 2026-07-04, Q1).

z = log(actual_price / now_price) / sigma_h — standardized log-return en
unidades de σ_h. Es la observación "cruda" de la que sale todo diagnóstico
de calibración: PIT bajo t_df=4, calidad de σ, drift E[z] per bucket.

Fable llamó esta info dark data hoy: won es 1 bit de un outcome continuo
que ya está en la tabla (actual_price + sigma_h). z lo destapa.

Idempotente: se puede correr varias veces sin duplicar.
"""
from __future__ import annotations

import math
import statistics
import sqlite3
import sys
from pathlib import Path

DB = Path("/home/popeye/crypto-predictor/calibration.db")


def _t4_inv_070() -> float:
    """t_df=4 ppf(0.70) via Newton sobre la closed form. Devuelve ≈ 0.5686."""
    def cdf(t: float) -> float:
        u = t / math.sqrt(t * t + 4.0)
        return 0.5 + 0.75 * u - 0.25 * u ** 3
    z = 0.5244 * math.sqrt(2.0)  # semilla: gauss·√2
    for _ in range(50):
        f = cdf(z) - 0.70
        if abs(f) < 1e-12:
            break
        pdf = 12.0 / (z * z + 4.0) ** 2.5
        z -= f / pdf
    return z


def main() -> int:
    if not DB.exists():
        print(f"ERROR: {DB} no existe", file=sys.stderr)
        return 1
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout = 30000")

    existing = [r[1] for r in c.execute("PRAGMA table_info(hourly_calls)")]
    if "z" not in existing:
        c.execute("ALTER TABLE hourly_calls ADD COLUMN z REAL")
        c.commit()
        print("added column z")
    else:
        print("column z already exists — will overwrite values")

    rows = c.execute(
        "SELECT id, now_price, sigma_h, actual_price "
        "FROM hourly_calls "
        "WHERE actual_price IS NOT NULL "
        "  AND sigma_h > 0 "
        "  AND now_price > 0 "
        "  AND actual_price > 0"
    ).fetchall()

    updates: list[tuple[float, int]] = []
    for rid, now, sig, actual in rows:
        z = math.log(actual / now) / sig
        updates.append((z, rid))

    c.executemany("UPDATE hourly_calls SET z=? WHERE id=?", updates)
    c.commit()
    print(f"backfilled {len(updates)} rows")

    zs = sorted(u[0] for u in updates)
    n = len(zs)
    if n == 0:
        print("no data")
        c.close()
        return 0
    mean_z = statistics.mean(zs)
    std_z = statistics.stdev(zs) if n > 1 else 0.0
    p05 = zs[n // 20]
    p25 = zs[n // 4]
    p50 = zs[n // 2]
    p75 = zs[3 * n // 4]
    p95 = zs[19 * n // 20]
    print("--- z distribution ---")
    print(f"  N        = {n}")
    print(f"  mean     = {mean_z:+.4f}  (esperado ~0 si drift=0)")
    print(f"  std      = {std_z:.4f}  (esperado ~1 si σ está bien calibrada)")
    print(f"  p05      = {p05:+.3f}")
    print(f"  p25      = {p25:+.3f}")
    print(f"  p50      = {p50:+.3f}")
    print(f"  p75      = {p75:+.3f}")
    print(f"  p95      = {p95:+.3f}")

    # predictor._dist_inv(0.70) para DIST_DF=4: _t4_inv(0.70)/√(df/(df-2)).
    # El modelo reescala Student-t para que Var(returns)=σ²_h; por eso la
    # threshold sobre z (= log(actual/now)/σ_h) NO es _t4_inv(0.70) directo.
    # Bug histórico: aquí había 0.5244 (que es _inv_norm(0.70), la Gaussiana),
    # producía brecha aparente de ~3.9pp vs WR que era artefacto puro.
    DF = 4
    SCALE = math.sqrt(DF / (DF - 2))          # = √2 para df=4
    dist_inv_070 = _t4_inv_070() / SCALE      # ≈ 0.4021
    frac_leq = sum(1 for z in zs if z <= dist_inv_070) / n
    print(f"  frac(z ≤ _dist_inv(0.70)={dist_inv_070:.4f}) = "
          f"{100 * frac_leq:.2f}%  (debe ≈ WR empírica)")

    # PIT bajo el MODELO real: F_dist(z) = F_{T_df}(z · √(df/(df-2))).
    # Sin ese √2 el histograma sale con pile-up central artificial.
    def _t4_cdf(t: float) -> float:
        u = t / math.sqrt(t * t + 4.0)
        return 0.5 + 0.75 * u - 0.25 * u ** 3
    pits = [_t4_cdf(z * SCALE) for z in zs]
    bins = [0] * 10
    for p in pits:
        idx = min(int(p * 10), 9)
        bins[idx] += 1
    print("--- PIT histogram bajo t_df=4 (10 bins, uniforme = ~{}/bin) ---"
          .format(n // 10))
    for i, b in enumerate(bins):
        bar = "█" * int(60 * b / max(bins))
        print(f"  [{i*0.1:.1f}-{(i+1)*0.1:.1f}) {b:4d} {bar}")

    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
