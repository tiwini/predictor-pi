"""Descomposición del edge modelo-vs-Kalshi por región de la curva
(fable review 2026-07-05 #3).

Para cada row settleada con curve completa, computa el edge por cada strike de
la escalera Kalshi (~188 puntos) y agrupa por:
  1. Región de la curva Kalshi (mid_yes: tail_low, low, center, high, tail_high)
  2. Spread bid/ask (tight, wide, very_wide, no_book)

Fable pregunta específica: ¿el edge vive en strikes lejanos (mid ∈ 1-3¢) donde
el mid es ficción o en el centro con book real? Con dark data #4 (bid/ask
persistidos) podemos responder directamente.

Uso: python3 decompose_edge.py [--db calibration.db]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field

import predictor as _pred


REGIONS = [
    ("tail_low",   0.00, 0.10),
    ("low",        0.10, 0.30),
    ("center",     0.30, 0.70),
    ("high",       0.70, 0.90),
    ("tail_high",  0.90, 1.01),
]

SPREAD_BUCKETS = [
    ("no_book",     None,   None),   # bid=0 or ask=0
    ("tight",       0.0,    0.03),
    ("wide",        0.03,   0.10),
    ("very_wide",   0.10,   1.01),
]


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


def _region_of(mid: float) -> str:
    for name, lo, hi in REGIONS:
        if lo <= mid < hi:
            return name
    return "unknown"


def _spread_bucket(bid: float | None, ask: float | None) -> str:
    # Kalshi convention: 0.0 = sin lado. Sin book real = untradeable.
    b_real = bid if (bid is not None and bid > 0) else None
    a_real = ask if (ask is not None and ask > 0) else None
    if b_real is None or a_real is None:
        return "no_book"
    spread = a_real - b_real
    for name, lo, hi in SPREAD_BUCKETS[1:]:
        if lo <= spread < hi:
            return name
    return "unknown"


def decompose(db_path: str) -> dict:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, made_at, target_at, now_price, sigma_h, call_value, "
            "actual_price, kalshi_curve_json "
            "FROM hourly_calls "
            "WHERE actual_price IS NOT NULL AND kalshi_curve_json IS NOT NULL "
            "ORDER BY id"
        ).fetchall()

    by_region: dict[str, Bucket] = {name: Bucket() for name, _, _ in REGIONS}
    by_spread: dict[str, Bucket] = {name: Bucket() for name, _, _ in SPREAD_BUCKETS}
    all_edges: list[float] = []
    n_rows_used = 0

    for r in rows:
        curve = json.loads(r["kalshi_curve_json"])
        strikes = curve["s"]
        mids = curve["m"]
        bids = curve.get("b", [None] * len(strikes))
        asks = curve.get("a", [None] * len(strikes))
        if not strikes:
            continue

        horizon_min = (r["target_at"] - r["made_at"]) / 60.0
        pred = _pred.Prediction(
            symbol="BTCUSDT", now_price=r["now_price"],
            sigma_1m=r["sigma_h"] / (horizon_min ** 0.5),
            sigma_horizon=r["sigma_h"], horizon_min=horizon_min,
            n_candles=500, fetched_at=r["made_at"], target_at=r["target_at"],
        )

        for i, s in enumerate(strikes):
            mid = mids[i]
            if mid <= 0.0 or mid >= 1.0:
                continue  # strike sin book real (bid=0/ask=0 → mid=0/1)
            p_above = _pred.prob_above(pred, s)
            model_no = 1.0 - p_above
            kalshi_no = 1.0 - mid
            edge_pp = (model_no - kalshi_no) * 100.0

            by_region[_region_of(mid)].add(edge_pp)
            by_spread[_spread_bucket(bids[i], asks[i])].add(edge_pp)
            all_edges.append(edge_pp)

        n_rows_used += 1

    return {
        "n_rows": n_rows_used,
        "n_strike_evals": len(all_edges),
        "by_region": {k: v.stats() for k, v in by_region.items()},
        "by_spread": {k: v.stats() for k, v in by_spread.items()},
        "overall": Bucket(n=len(all_edges), edges=all_edges).stats(),
    }


def _print_bucket(name: str, s: dict) -> None:
    if s["n"] == 0:
        print(f"  {name:12} n=0")
        return
    print(f"  {name:12} n={s['n']:5d}  mean={s['mean_pp']:+6.2f}pp  "
          f"med={s['median_pp']:+6.2f}pp  "
          f"pos={s['pct_positive']*100:5.1f}%  "
          f"[p10 {s['p10']:+6.2f} · p90 {s['p90']:+6.2f}]")


def _print(res: dict) -> None:
    print(f"\n=== DESCOMPOSICIÓN EDGE modelo-vs-Kalshi ===")
    print(f"N rows: {res['n_rows']} · N strike evals: {res['n_strike_evals']}")
    o = res["overall"]
    if o["n"] > 0:
        print(f"\nOverall edge_pp: mean {o['mean_pp']:+.2f} · "
              f"median {o['median_pp']:+.2f} · "
              f"pos {o['pct_positive']*100:.1f}%")

    print(f"\nPor región Kalshi mid_yes (edge_pp = model_no − kalshi_no):")
    for name, _, _ in REGIONS:
        _print_bucket(name, res["by_region"][name])

    print(f"\nPor spread bid/ask:")
    for name, _, _ in SPREAD_BUCKETS:
        _print_bucket(name, res["by_spread"][name])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/home/popeye/crypto-predictor/calibration.db")
    args = ap.parse_args()
    _print(decompose(args.db))
