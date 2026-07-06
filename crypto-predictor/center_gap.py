"""Diagnóstico del gap del center Kalshi (fable review 2026-07-05 #3 follow-up).

decompose_edge.py mostró que Kalshi bate al modelo en la región center (mid_yes
30-70%) por −18.64pp (N=20 evals). Este script pregunta: ¿ese gap es sesgo
sistemático nuestro (μ mal, σ mal, cola mal) o info que Kalshi tiene que no
tenemos?

Estratifica el edge_pp del center por:
  1. Signo de z = (strike − now_price)/σ_h → sesgo direccional del modelo
  2. Magnitud de |z| bucketizada → si el gap crece con la cola cerca-del-center
  3. Horizonte restante (target_at − made_at) → si el gap depende del tiempo
  4. Nivel de σ_h relativo → si el gap correlaciona con vol

Uso: python3 center_gap.py [--db calibration.db]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import dataclass, field

import predictor as _pred


CENTER_LO, CENTER_HI = 0.30, 0.70


@dataclass
class Bucket:
    n: int = 0
    edges: list[float] = field(default_factory=list)
    def add(self, e: float) -> None:
        self.n += 1
        self.edges.append(e)
    def stats(self) -> dict:
        if not self.edges:
            return {"n": 0}
        s = sorted(self.edges)
        n = len(s)
        mean = sum(s) / n
        median = s[n // 2]
        pos = sum(1 for e in s if e > 0) / n
        return {"n": n, "mean_pp": mean, "median_pp": median,
                "pct_positive": pos, "p10": s[n // 10],
                "p90": s[(9 * n) // 10]}


def _z_bucket(z: float) -> str:
    a = abs(z)
    if a < 0.25: return "|z|<0.25"
    if a < 0.50: return "|z|<0.50"
    if a < 0.75: return "|z|<0.75"
    if a < 1.00: return "|z|<1.00"
    return "|z|≥1.00"


def analyze(db_path: str) -> dict:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, made_at, target_at, now_price, sigma_h, "
            "actual_price, kalshi_curve_json "
            "FROM hourly_calls "
            "WHERE actual_price IS NOT NULL AND kalshi_curve_json IS NOT NULL "
            "ORDER BY id"
        ).fetchall()

    by_sign = {"strike>now (z>0)": Bucket(), "strike<now (z<0)": Bucket()}
    by_mag = {name: Bucket() for name in
              ["|z|<0.25", "|z|<0.50", "|z|<0.75", "|z|<1.00", "|z|≥1.00"]}
    by_horizon = {"≤20min": Bucket(), "20-40min": Bucket(), ">40min": Bucket()}
    by_sigma = {"low_vol": Bucket(), "mid_vol": Bucket(), "high_vol": Bucket()}
    center_edges = []
    z_by_edge = []  # (z, edge_pp) tuples for the raw dump

    # Compute sigma_h tertiles first
    sigmas = [r["sigma_h"] for r in rows]
    sigmas_sorted = sorted(sigmas)
    n_s = len(sigmas_sorted)
    if n_s >= 3:
        s_lo = sigmas_sorted[n_s // 3]
        s_hi = sigmas_sorted[(2 * n_s) // 3]
    else:
        s_lo = s_hi = sigmas_sorted[-1] if sigmas_sorted else 0

    for r in rows:
        curve = json.loads(r["kalshi_curve_json"])
        strikes = curve["s"]
        mids = curve["m"]
        if not strikes:
            continue

        horizon_min = (r["target_at"] - r["made_at"]) / 60.0
        sigma_h = r["sigma_h"]
        now_price = r["now_price"]
        pred = _pred.Prediction(
            symbol="BTCUSDT", now_price=now_price,
            sigma_1m=sigma_h / (horizon_min ** 0.5),
            sigma_horizon=sigma_h, horizon_min=horizon_min,
            n_candles=500, fetched_at=r["made_at"], target_at=r["target_at"],
        )

        # Horizonte bucket
        if horizon_min <= 20:
            hb = "≤20min"
        elif horizon_min <= 40:
            hb = "20-40min"
        else:
            hb = ">40min"
        # Sigma bucket
        if sigma_h <= s_lo:
            sb = "low_vol"
        elif sigma_h <= s_hi:
            sb = "mid_vol"
        else:
            sb = "high_vol"

        for i, s in enumerate(strikes):
            mid = mids[i]
            if mid <= 0.0 or mid >= 1.0:
                continue
            if not (CENTER_LO <= mid < CENTER_HI):
                continue

            p_above = _pred.prob_above(pred, s)
            model_no = 1.0 - p_above
            kalshi_no = 1.0 - mid
            edge_pp = (model_no - kalshi_no) * 100.0
            # z en unidades log-return normalizadas
            z = math.log(s / now_price) / sigma_h if sigma_h > 0 else 0.0

            center_edges.append(edge_pp)
            z_by_edge.append((z, edge_pp, mid))
            if z > 0:
                by_sign["strike>now (z>0)"].add(edge_pp)
            else:
                by_sign["strike<now (z<0)"].add(edge_pp)
            by_mag[_z_bucket(z)].add(edge_pp)
            by_horizon[hb].add(edge_pp)
            by_sigma[sb].add(edge_pp)

    n = len(center_edges)
    overall = Bucket(n=n, edges=center_edges).stats() if n else {"n": 0}

    return {
        "n_rows": len(rows),
        "n_center_evals": n,
        "overall_center": overall,
        "by_sign": {k: v.stats() for k, v in by_sign.items()},
        "by_z_magnitude": {k: v.stats() for k, v in by_mag.items()},
        "by_horizon": {k: v.stats() for k, v in by_horizon.items()},
        "by_sigma_tertile": {k: v.stats() for k, v in by_sigma.items()},
        "raw_dump": sorted(z_by_edge),
    }


def _print_bucket(name: str, s: dict) -> None:
    if s["n"] == 0:
        print(f"  {name:22} n=0")
        return
    print(f"  {name:22} n={s['n']:4d}  mean={s['mean_pp']:+7.2f}pp  "
          f"med={s['median_pp']:+7.2f}pp  "
          f"pos={s['pct_positive']*100:5.1f}%  "
          f"[p10 {s['p10']:+6.2f} · p90 {s['p90']:+6.2f}]")


def _print(res: dict) -> None:
    print(f"\n=== DIAGNÓSTICO GAP CENTER (mid Kalshi 30-70%) ===")
    print(f"N rows: {res['n_rows']} · N center evals: {res['n_center_evals']}")
    if res["overall_center"]["n"] == 0:
        print("Sin datos center.")
        return
    o = res["overall_center"]
    print(f"\nCenter overall: mean {o['mean_pp']:+.2f}pp · "
          f"med {o['median_pp']:+.2f}pp · pos {o['pct_positive']*100:.1f}%")

    print(f"\nPor signo de z = log(strike/now)/σ_h:")
    for name in ["strike<now (z<0)", "strike>now (z>0)"]:
        _print_bucket(name, res["by_sign"][name])

    print(f"\nPor magnitud |z| (cola cerca-del-center):")
    for name in ["|z|<0.25", "|z|<0.50", "|z|<0.75", "|z|<1.00", "|z|≥1.00"]:
        _print_bucket(name, res["by_z_magnitude"][name])

    print(f"\nPor horizonte restante (target−made):")
    for name in ["≤20min", "20-40min", ">40min"]:
        _print_bucket(name, res["by_horizon"][name])

    print(f"\nPor tertil de σ_h:")
    for name in ["low_vol", "mid_vol", "high_vol"]:
        _print_bucket(name, res["by_sigma_tertile"][name])

    print(f"\nRaw dump (z, edge_pp, mid_kalshi) — center only:")
    print(f"  {'z':>7}  {'edge_pp':>8}  {'mid_k':>6}")
    for z, e, m in res["raw_dump"]:
        print(f"  {z:+7.3f}  {e:+8.2f}  {m:.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/home/popeye/crypto-predictor/calibration.db")
    args = ap.parse_args()
    _print(analyze(args.db))
