#!/usr/bin/env python3
"""R7 diagnóstico Fable — baseline naïve RMSE vs EMA 3d.

Fable R6: "el baseline naïve 'predecir el último basis observado'. El RMSE
one-step-ahead decrece casi siempre hacia half-lives cortos en series
persistentes — el límite half-life→0 es el naïve. Si el naïve da RMSE < 3.16,
no cambia la decisión (el EMA winsorizado se queda por robustez: un naïve se
traga entero el próximo −16.33), pero les dice cuánto del 20% de mejora sobre
5d es tracking de señal y cuánto es simplemente estar más cerca de la última
observación."

Protocolo: mismo dataset, misma métrica (RMSE one-step-ahead sobre raw), pero
la predicción es "usar la observación anterior sin transformar".
"""
import math
import sqlite3

DB = "/home/popeye/crypto-predictor/calibration.db"
WINSOR_LO, WINSOR_HI = -5.0, 25.0


def winsorize(x):
    if x < WINSOR_LO:
        return WINSOR_LO
    if x > WINSOR_HI:
        return WINSOR_HI
    return x


def naive_rmse(bps_raw, use_winsor=False):
    """pred_t = bps_{t-1} (winsor opcional). Error contra bps_raw_t."""
    errs = []
    prev = None
    for x in bps_raw:
        if prev is not None:
            errs.append(x - prev)
        prev = winsorize(x) if use_winsor else x
    if not errs:
        return float("nan"), 0
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
    return rmse, len(errs)


def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT actual_price, proxy_price_at_settle "
        "FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NOT NULL "
        "ORDER BY made_at"
    ).fetchall()
    bps = [1e4 * (r[0] - r[1]) / r[0] for r in rows]
    n = len(bps)
    print(f"[input] N={n}  raw range [{min(bps):+.2f}, {max(bps):+.2f}] bps")

    # std intra-serie (para "piso de ruido" que mencionó Fable)
    m = sum(bps) / n
    std = math.sqrt(sum((b - m) ** 2 for b in bps) / (n - 1))
    print(f"[input] mean={m:+.2f}  std={std:.2f}")

    rmse_r, nr = naive_rmse(bps, use_winsor=False)
    rmse_w, nw = naive_rmse(bps, use_winsor=True)

    print()
    print("=" * 66)
    print("[naive one-step-ahead]  pred_t = bps_{t-1}")
    print("=" * 66)
    print(f"  naive RAW      : RMSE = {rmse_r:.4f}  (N={nr})")
    print(f"  naive WINSOR   : RMSE = {rmse_w:.4f}  (N={nw})")
    print(f"  EMA 3d winsor  : RMSE = 3.1567   (ref R6, mismo dataset)")
    print()
    print("Lectura Fable:")
    print(f"  - Si naïve_raw < 3.16 → parte del 'edge de 3d' es proximidad, no")
    print(f"    señal. La decisión 3d NO cambia (EMA se traga el próximo −16.33")
    print(f"    outlier gracias al winsor; el naïve no).")
    print(f"  - Std intra-serie ≈ {std:.2f} = piso de ruido. Ningún estimador")
    print(f"    baja mucho de eso.")


if __name__ == "__main__":
    main()
