"""Grid search de hiperparámetros: para cada (λ, df) re-simula los últimos
N días y reporta tail stats. NO escribe en la DB de calibración.

Métrica clave: ratio observado/esperado de |z|>2 (queremos ≈1.0).
La actual config (λ=0.94, df=4) da ~2.6× → subestima cola.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass

import predictor as p
from backtest import fetch_range


@dataclass
class Sample:
    z: float       # |gaussian-equiv z| del actual respecto a la pred


def simulate(symbol: str, days: int, lead_min: int, lookback_min: int,
             lam: float) -> list[float]:
    """Devuelve lista de |z| (gaussian-equiv) por cada cierre simulado.
    z es independent del df: depende sólo de σ_h (que sí depende de λ)."""
    now = time.time()
    end_ts = math.floor(now / 3600) * 3600
    start_ts = end_ts - days * 86400
    pad = (lookback_min + lead_min + 5) * 60
    fetch_start_ms = int((start_ts - pad) * 1000)
    fetch_end_ms = int((end_ts + 120) * 1000)
    klines = fetch_range(symbol, fetch_start_ms, fetch_end_ms)
    by_t = {k.open_time: k for k in klines}
    ot_sorted = sorted(by_t.keys())
    closes = [by_t[t].close for t in ot_sorted]

    import bisect
    zs: list[float] = []
    t = start_ts
    while t <= end_ts:
        target_ms = int(t * 1000)
        if target_ms not in by_t:
            t += 3600
            continue
        made_at = t - lead_min * 60
        made_ms = int(made_at * 1000)
        idx = bisect.bisect_right(ot_sorted, made_ms) - 1
        if idx < lookback_min:
            t += 3600
            continue
        window = closes[idx - lookback_min + 1: idx + 1]
        rets = p.log_returns(window)
        sigma_1m = p.ewma_sigma(rets, lam=lam)
        if sigma_1m <= 0:
            t += 3600
            continue
        sigma_h = sigma_1m * math.sqrt(lead_min)
        now_price = window[-1]
        actual = by_t[target_ms].open
        z = abs(math.log(actual / now_price) / sigma_h)
        zs.append(z)
        t += 3600
    return zs


def tail_stats(zs: list[float], df: int) -> dict:
    """Frecuencia obs vs esperada bajo T_df var-matched."""
    n = len(zs)
    levels = [1.5, 2.0, 2.5, 3.0, 4.0]
    rows = []
    for k in levels:
        if df is None:
            expected = 2.0 * (1.0 - p._norm_cdf(k))
        elif df == 4:
            expected = 2.0 * (1.0 - p._t4_cdf(k * math.sqrt(df / (df - 2))))
        elif df == 3:
            expected = 2.0 * (1.0 - _t3_cdf(k * math.sqrt(df / (df - 2))))
        else:
            raise ValueError(f"df={df} no soportado (sólo 3, 4)")
        observed = sum(1 for z in zs if z > k) / n if n else 0.0
        rows.append({"k": k, "obs": observed, "exp": expected,
                     "ratio": observed / expected if expected > 0 else 0})
    mean_z = sum(zs) / n if n else 0.0
    return {"n": n, "mean_z": mean_z, "levels": rows}


def _t3_cdf(t: float) -> float:
    """CDF Student-t df=3 closed form."""
    return 0.5 + (math.atan(t / math.sqrt(3.0)) / math.pi) + \
           t / (math.pi * math.sqrt(3.0) * (1 + t * t / 3.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--lead", type=int, default=60)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--symbols",
                    default="BTCUSDT,ETHUSDT,XRPUSDT,DOGEUSDT,SOLUSDT")
    ap.add_argument("--lambdas", default="0.94,0.97,0.99")
    ap.add_argument("--dfs", default="3,4")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    lams = [float(x) for x in args.lambdas.split(",")]
    dfs = [int(x) for x in args.dfs.split(",")]

    # Pull klines y simular |z| una vez por (symbol, λ); reutilizamos para todos los df
    cache: dict[tuple[str, float], list[float]] = {}
    for s in symbols:
        for lam in lams:
            print(f"[{s}] λ={lam}...", file=sys.stderr)
            cache[(s, lam)] = simulate(s, args.days, args.lead,
                                       args.lookback, lam)

    # Pool global por (λ, df)
    print(f"\n{'config':<14} {'n':>5} {'mean|z|':>8} "
          f"{'|z|>1.5':>9} {'|z|>2':>9} {'|z|>2.5':>9} {'|z|>3':>9}")
    print(f"{'(target)':<14} {'':>5} {'0.80':>8} "
          f"{'1.0×':>9} {'1.0×':>9} {'1.0×':>9} {'1.0×':>9}")
    print("-" * 75)
    for lam in lams:
        for df in dfs:
            all_zs: list[float] = []
            for s in symbols:
                all_zs.extend(cache[(s, lam)])
            ts = tail_stats(all_zs, df)
            cells = [f"{r['ratio']:>7.2f}×" for r in ts['levels'][:4]]
            print(f"λ={lam} df={df}   {ts['n']:>5} {ts['mean_z']:>8.3f} "
                  f"{cells[0]:>9} {cells[1]:>9} {cells[2]:>9} {cells[3]:>9}")

    # Por símbolo con la mejor combo (la que tenga ratio |z|>2 más cercano a 1)
    print("\n--- por símbolo (λ=0.97, df=3 sugerido) ---")
    print(f"{'sym':<6} {'mean|z|':>8} {'|z|>2':>9} {'|z|>3':>9}")
    for s in symbols:
        zs = cache[(s, 0.97)] if (s, 0.97) in cache else cache[(s, lams[0])]
        ts = tail_stats(zs, 3 if 3 in dfs else dfs[0])
        z2 = ts['levels'][1]; z3 = ts['levels'][3]
        print(f"{s[:3]:<6} {ts['mean_z']:>8.3f} "
              f"{z2['ratio']:>7.2f}× {z3['ratio']:>7.2f}×")


if __name__ == "__main__":
    main()
