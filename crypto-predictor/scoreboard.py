"""Scoreboard modelo-vs-Kalshi (fable review 2026-07-05, primary question #1).

Responde: ¿mi modelo sabe algo que Kalshi no? Sobre las mismas horas settleadas,
computa log-loss + Brier de ambos como probabilistas del outcome binario
"BTC ≤ kalshi_strike" al target_at, y PIT de ambos como probabilistas del
outcome continuo (interpolando la CDF Kalshi persistida en kalshi_curve_json).

Uso: python3 scoreboard.py [--db calibration.db]

Requiere rows con: actual_price, kalshi_strike, kalshi_no_at_strike,
model_no_at_strike, z, kalshi_curve_json — todos post-instrumentación 2026-07-04.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from typing import Optional

import predictor as _pred


def _cdf_kalshi(actual: float, strikes: list[float],
                mids: list[float]) -> Optional[float]:
    """CDF Kalshi implícita: P(BTC ≤ actual). mids son P(YES=BTC>strike)
    decrecientes en strike. Interpolación lineal entre strikes adyacentes."""
    if not strikes or len(strikes) != len(mids):
        return None
    if actual <= strikes[0]:
        return 1.0 - mids[0]
    if actual >= strikes[-1]:
        return 1.0 - mids[-1]
    # bisect
    lo, hi = 0, len(strikes) - 1
    while lo + 1 < hi:
        m = (lo + hi) // 2
        if strikes[m] <= actual:
            lo = m
        else:
            hi = m
    a, b = strikes[lo], strikes[lo + 1]
    pa, pb = mids[lo], mids[lo + 1]
    t = (actual - a) / (b - a)
    p_above = pa + t * (pb - pa)
    return 1.0 - p_above


def _is_junk_at_strike(k_no_at_strike: float) -> bool:
    """Signal directo: si k_no ∈ {0, 1} exacto, el strike específico está
    sin book real (mid inferido de bid=0/ask=0 o cotización degenerada).
    Ver bug 2026-07-05 rows 956/957/958/967. Descarta al menos ese row del
    scoreboard binario porque el punto de comparación es fake."""
    return k_no_at_strike in (0.0, 1.0)


def _log_loss(p: float, y: int) -> float:
    """Binary cross-entropy. p ∈ (0,1)."""
    eps = 1e-12
    p = max(eps, min(1 - eps, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _brier(p: float, y: int) -> float:
    return (p - y) ** 2


def _pit_histogram(values: list[float], bins: int = 10) -> list[int]:
    counts = [0] * bins
    for v in values:
        i = min(bins - 1, int(v * bins))
        counts[i] += 1
    return counts


def scoreboard(db_path: str) -> dict:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, actual_price, kalshi_strike, kalshi_no_at_strike, "
            "model_no_at_strike, z, kalshi_curve_json "
            "FROM hourly_calls "
            "WHERE actual_price IS NOT NULL AND kalshi_strike IS NOT NULL "
            "AND kalshi_no_at_strike IS NOT NULL "
            "AND model_no_at_strike IS NOT NULL AND z IS NOT NULL "
            "AND kalshi_curve_json IS NOT NULL "
            "ORDER BY id"
        ).fetchall()

    n_total = len(rows)
    used_ids: list[int] = []
    junk_ids: list[int] = []

    ll_model, ll_kalshi = [], []
    br_model, br_kalshi = [], []
    pit_model, pit_kalshi = [], []

    for r in rows:
        curve = json.loads(r["kalshi_curve_json"])
        strikes = curve["s"]
        mids = curve["m"]
        if _is_junk_at_strike(r["kalshi_no_at_strike"]):
            junk_ids.append(r["id"])
            continue

        y = 1 if r["actual_price"] <= r["kalshi_strike"] else 0
        p_m = r["model_no_at_strike"]
        p_k = r["kalshi_no_at_strike"]
        ll_model.append(_log_loss(p_m, y))
        ll_kalshi.append(_log_loss(p_k, y))
        br_model.append(_brier(p_m, y))
        br_kalshi.append(_brier(p_k, y))

        pit_m = _pred._dist_cdf(r["z"])
        pit_k = _cdf_kalshi(r["actual_price"], strikes, mids)
        if pit_k is None:
            continue
        pit_model.append(pit_m)
        pit_kalshi.append(pit_k)
        used_ids.append(r["id"])

    n = len(used_ids)
    if n == 0:
        return {"n_total": n_total, "n_used": 0, "n_junk": len(junk_ids)}

    return {
        "n_total": n_total,
        "n_used": n,
        "n_junk": len(junk_ids),
        "junk_ids_sample": junk_ids[:5],
        "log_loss": {
            "model": sum(ll_model) / len(ll_model),
            "kalshi": sum(ll_kalshi) / len(ll_kalshi),
            "diff_kalshi_minus_model": (
                sum(ll_kalshi) / len(ll_kalshi)
                - sum(ll_model) / len(ll_model)),
        },
        "brier": {
            "model": sum(br_model) / len(br_model),
            "kalshi": sum(br_kalshi) / len(br_kalshi),
            "diff_kalshi_minus_model": (
                sum(br_kalshi) / len(br_kalshi)
                - sum(br_model) / len(br_model)),
        },
        "pit_model_hist": _pit_histogram(pit_model),
        "pit_kalshi_hist": _pit_histogram(pit_kalshi),
    }


def _print(res: dict) -> None:
    print(f"\n=== SCOREBOARD modelo-vs-Kalshi ===")
    print(f"N total rows candidatas: {res['n_total']}")
    print(f"N usadas: {res.get('n_used', 0)} · junk skipped: {res.get('n_junk', 0)}")
    if res.get("junk_ids_sample"):
        print(f"  junk ejemplos: {res['junk_ids_sample']}")
    if res.get("n_used", 0) == 0:
        print("Sin datos suficientes.")
        return
    ll = res["log_loss"]
    br = res["brier"]
    print(f"\nLog-loss @ kalshi_strike (menor = mejor):")
    print(f"  modelo:  {ll['model']:.5f}")
    print(f"  kalshi:  {ll['kalshi']:.5f}")
    print(f"  Δ (kalshi - modelo):  {ll['diff_kalshi_minus_model']:+.5f}"
          f"  → {'modelo gana' if ll['diff_kalshi_minus_model'] > 0 else 'KALSHI gana'}")
    print(f"\nBrier @ kalshi_strike:")
    print(f"  modelo:  {br['model']:.5f}")
    print(f"  kalshi:  {br['kalshi']:.5f}")
    print(f"  Δ (kalshi - modelo):  {br['diff_kalshi_minus_model']:+.5f}")
    print(f"\nPIT histogramas (uniforme = {res['n_used'] / 10:.1f}/bin):")
    print(f"  bin  modelo  kalshi")
    for i in range(10):
        m = res["pit_model_hist"][i]
        k = res["pit_kalshi_hist"][i]
        print(f"  [{i/10:.1f}-{(i+1)/10:.1f}) {m:4d}  {k:4d}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/home/popeye/crypto-predictor/calibration.db")
    args = ap.parse_args()
    _print(scoreboard(args.db))
