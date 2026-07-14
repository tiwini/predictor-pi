"""Análisis del residuo empírico (fable review 2026-07-05 #3 follow-up B).

Test directo de la distribución subyacente: para cada row settleada, computa
el log-return realizado normalizado z_emp = log(actual/now) / σ_h. Si el
modelo Student-t(df=4) reescalado está bien calibrado:

  z_emp / √((df-2)/df) ~ t(df)

Reporta:
  - Media, std, skew, kurtosis empíricas
  - PIT bajo t(4), t(3), t(5), Gaussiano — para elegir mejor familia
  - Cuenta por cola: cuántos z_emp caen |·|>1, >1.5, >2

N pequeño esperado (~12 rows). No para conclusiones firmes; sí para
elegir dirección de N≥50.

Uso: python3 residuals.py [--db calibration.db]
"""
from __future__ import annotations

import argparse
import math
import sqlite3

import predictor as _pred


def _t_cdf_df4(z: float) -> float:
    """CDF t(df=4) usando forma cerrada."""
    x = z / math.sqrt(4.0)
    a = 0.5 + (3.0 / 8.0) * (x / math.sqrt(1 + x*x)) * (1 - x*x / (3 * (1 + x*x)))
    return a


def _t_cdf_generic(z: float, df: int, n_steps: int = 2000) -> float:
    """CDF t(df) por integración trapezoidal — suficiente para diagnóstico."""
    if z == 0:
        return 0.5
    coef = math.gamma((df + 1) / 2.0) / (
        math.sqrt(df * math.pi) * math.gamma(df / 2.0))

    def pdf(t):
        return coef * (1 + t*t / df) ** (-(df + 1) / 2.0)

    # Integra desde 0 a |z|
    a, b = 0.0, abs(z)
    h = (b - a) / n_steps
    s = 0.5 * (pdf(a) + pdf(b))
    for i in range(1, n_steps):
        s += pdf(a + i * h)
    integral = s * h
    return 0.5 + math.copysign(integral, z)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def analyze(db_path: str, min_id: int = 0) -> dict:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, made_at, target_at, now_price, sigma_h, actual_price "
            "FROM hourly_calls "
            "WHERE actual_price IS NOT NULL AND sigma_h > 0 AND id >= ? "
            "ORDER BY id", (min_id,)
        ).fetchall()

    z_emps = []
    z_rescaled = []
    for r in rows:
        actual = r["actual_price"]
        now = r["now_price"]
        sigma_h = r["sigma_h"]
        if actual <= 0 or now <= 0 or sigma_h <= 0:
            continue
        z = math.log(actual / now) / sigma_h
        z_emps.append(z)
        # z reescalado (dividido por escala del modelo) para comparar con t(df)
        # scale = sqrt((df-2)/df) para df=4 → 1/√2
        z_rescaled.append(z / math.sqrt((4 - 2) / 4))

    n = len(z_emps)
    if n < 3:
        return {"n": n}

    mean = sum(z_emps) / n
    var = sum((x - mean) ** 2 for x in z_emps) / n
    std = math.sqrt(var)
    # skewness y kurtosis muestrales
    m3 = sum((x - mean) ** 3 for x in z_emps) / n
    m4 = sum((x - mean) ** 4 for x in z_emps) / n
    skew = m3 / (std ** 3) if std > 0 else 0
    kurt_excess = (m4 / (std ** 4) - 3) if std > 0 else 0

    # Contadores por cola en z_emp (no reescalado)
    tails = {
        "|z|>1.0": sum(1 for x in z_emps if abs(x) > 1.0),
        "|z|>1.5": sum(1 for x in z_emps if abs(x) > 1.5),
        "|z|>2.0": sum(1 for x in z_emps if abs(x) > 2.0),
    }

    # PIT bajo distintas familias — necesitamos usar z_emp normalizado por
    # la escala de cada distribución candidata para que la comparación sea
    # apples-to-apples. La escala del modelo es sqrt((df-2)/df) para df>2;
    # el z_emp del predictor ya divide por σ_h, así que para pit t(df) hacemos:
    #   z_std = z_emp / sqrt((df-2)/df)
    # y PIT = t_cdf(z_std, df)
    pits = {"t3": [], "t4": [], "t5": [], "norm": []}
    for z in z_emps:
        for df, name in [(3, "t3"), (4, "t4"), (5, "t5")]:
            scale = math.sqrt((df - 2) / df)
            z_std = z / scale
            pits[name].append(_t_cdf_generic(z_std, df))
        pits["norm"].append(_norm_cdf(z))

    # Uniformidad de PIT: mean debería ≈0.5, std ≈ 1/√12 = 0.289
    pit_stats = {}
    for k, ps in pits.items():
        m = sum(ps) / len(ps)
        v = sum((p - m) ** 2 for p in ps) / len(ps)
        # binning para chequeo visual
        bins = [0] * 5
        for p in ps:
            bi = min(4, int(p * 5))
            bins[bi] += 1
        pit_stats[k] = {
            "mean": m, "std": math.sqrt(v),
            "bins_5": bins,
        }

    return {
        "n": n,
        "z_emp_stats": {
            "mean": mean, "std": std, "skew": skew,
            "kurt_excess": kurt_excess,
        },
        "z_emp_tails": tails,
        "pit_stats": pit_stats,
        "z_emp_sorted": sorted(z_emps),
    }


def _print(res: dict) -> None:
    print(f"\n=== ANÁLISIS DEL RESIDUO EMPÍRICO ===")
    n = res.get("n", 0)
    print(f"N = {n}")
    if n < 3:
        print("Insuficiente.")
        return
    z = res["z_emp_stats"]
    print(f"\nz_emp = log(actual/now) / σ_h  (esperado ~t(df=4) reescalado)")
    print(f"  mean:    {z['mean']:+.4f}  (esperado 0)")
    print(f"  std:     {z['std']:.4f}  (esperado √(df/(df-2))=√2≈1.414 para df=4)")
    print(f"  skew:    {z['skew']:+.4f}  (esperado 0)")
    print(f"  kurt_ex: {z['kurt_excess']:+.4f}  (t(4) tiene kurt∞; t(5)=6; t(6)=3; gauss=0)")

    print(f"\nColas empíricas:")
    for k, v in res["z_emp_tails"].items():
        print(f"  {k}:  {v}/{n} = {v/n*100:.1f}%")

    print(f"\nPIT bajo distintas familias (bins uniformes = {n/5:.1f}/bin):")
    print(f"  familia   mean    std     [0-.2 .2-.4 .4-.6 .6-.8 .8-1]")
    for k in ["t3", "t4", "t5", "norm"]:
        s = res["pit_stats"][k]
        b = s["bins_5"]
        print(f"  {k:8}  {s['mean']:.3f}  {s['std']:.3f}   "
              f"[{b[0]:3d}  {b[1]:3d}  {b[2]:3d}  {b[3]:3d}  {b[4]:3d}]")

    print(f"\nz_emp ordenado (para inspección visual):")
    for x in res["z_emp_sorted"]:
        print(f"  {x:+.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/home/popeye/crypto-predictor/calibration.db")
    ap.add_argument("--min-id", type=int, default=0)
    args = ap.parse_args()
    _print(analyze(args.db, min_id=args.min_id))
