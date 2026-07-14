#!/usr/bin/env python3
"""R6 request #1 de Fable — RMSE one-step-ahead para basis EMA time-aware.

Protocolo (pre-registrado, sin peeking a shadow data):
  - Half-lives probados: 3d, 5d, 7d, 14d.
  - EMA time-aware: w = 0.5^(Δt_hours / half_life_hours) aplicado al gap
    real entre settles (crítico por gap W24 = corrupción 2026-06-19).
  - Winsor observación a [-5, +25] bps ANTES de alimentar el EMA
    (banda corregida de Fable: W19 tuvo obs legítimas ~-1).
  - Métrica: RMSE one-step-ahead — predice basis en t usando estado EMA
    tras procesar hasta t-1, error contra basis observado (raw, no winsor).
  - Congelar ganador. Tie-break dentro 2% → half-life más largo.
  - Corre sobre 1041 obs históricas.
"""
import math
import sqlite3

DB = "/home/popeye/crypto-predictor/calibration.db"
HALF_LIVES_DAYS = [3, 5, 7, 14]
WINSOR_LO, WINSOR_HI = -5.0, 25.0


def winsorize(x):
    if x < WINSOR_LO:
        return WINSOR_LO, True
    if x > WINSOR_HI:
        return WINSOR_HI, True
    return x, False


def ema_rmse_time_aware(times, bps_raw, half_life_hours):
    """Devuelve (rmse, n_scored, n_winsor_lo, n_winsor_hi, mae).

    - times[i] en segundos unix, ordenados asc.
    - bps_raw[i] = observación cruda (para scoring).
    - EMA se alimenta de la observación winsorizada.
    - Primera obs = seed (no scored, se usa como estado inicial).
    """
    state = None
    state_t = None
    errs = []
    n_lo = n_hi = 0
    for t, x_raw in zip(times, bps_raw):
        x_w, clipped = winsorize(x_raw)
        if clipped:
            if x_raw < WINSOR_LO:
                n_lo += 1
            else:
                n_hi += 1
        if state is None:
            state = x_w
            state_t = t
            continue
        # Predicción one-step-ahead: pred = state (last EMA)
        pred = state
        err = x_raw - pred
        errs.append(err)
        # Update
        dt_h = (t - state_t) / 3600.0
        w = 0.5 ** (dt_h / half_life_hours)
        state = state * w + x_w * (1 - w)
        state_t = t
    n = len(errs)
    if n == 0:
        return float("nan"), 0, n_lo, n_hi, float("nan")
    rmse = math.sqrt(sum(e * e for e in errs) / n)
    mae = sum(abs(e) for e in errs) / n
    return rmse, n, n_lo, n_hi, mae


def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT made_at, actual_price, proxy_price_at_settle "
        "FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NOT NULL "
        "ORDER BY made_at"
    ).fetchall()
    times = [float(r[0]) for r in rows]
    bps = [1e4 * (r[1] - r[2]) / r[1] for r in rows]
    n = len(times)
    print(f"[input] N={n}  span={((times[-1]-times[0])/86400):.1f}d  "
          f"raw min={min(bps):+.2f}  max={max(bps):+.2f}")

    # Gap forense
    gaps = [(times[i+1] - times[i]) / 3600.0 for i in range(n - 1)]
    gap_max = max(gaps)
    imax = gaps.index(gap_max)
    from datetime import datetime, timezone
    print(f"[gap] mayor gap = {gap_max:.1f}h entre "
          f"{datetime.fromtimestamp(times[imax], tz=timezone.utc)} y "
          f"{datetime.fromtimestamp(times[imax+1], tz=timezone.utc)}")
    print(f"[gap] gaps > 4h: {sum(g > 4 for g in gaps)}   "
          f"gaps > 24h: {sum(g > 24 for g in gaps)}   "
          f"gaps > 96h: {sum(g > 96 for g in gaps)}")

    # RMSE por half-life
    print()
    print("=" * 78)
    print("[RMSE one-step-ahead] winsor [-5, +25] bps, EMA time-aware")
    print("=" * 78)
    print(f"{'half_life':>10} {'RMSE':>8} {'MAE':>8} {'n_scored':>9} "
          f"{'n_winsor_lo':>12} {'n_winsor_hi':>12}")
    results = []
    for hl_days in HALF_LIVES_DAYS:
        rmse, n_scored, n_lo, n_hi, mae = ema_rmse_time_aware(
            times, bps, hl_days * 24)
        results.append((hl_days, rmse, mae, n_scored, n_lo, n_hi))
        print(f"{hl_days:>7}d    {rmse:>8.4f} {mae:>8.4f} {n_scored:>9} "
              f"{n_lo:>12} {n_hi:>12}")

    # Ganador con tie-break Fable: si dos empatan dentro del 2%, más largo gana.
    print()
    best = min(results, key=lambda r: r[1])
    winner_rmse = best[1]
    within_2pct = [r for r in results if r[1] <= winner_rmse * 1.02]
    winner = max(within_2pct, key=lambda r: r[0])
    print(f"[winner] half-life = {winner[0]}d   RMSE = {winner[1]:.4f}   "
          f"(mejor absoluto = {best[0]}d @ {best[1]:.4f}, "
          f"delta {100*(winner[1]/best[1]-1):.2f}%)")
    if within_2pct[0][0] != winner[0]:
        empates = [r[0] for r in within_2pct if r[0] != winner[0]]
        print(f"[tie-break] hubo empates dentro del 2%: {empates}d — "
              f"gana el más largo ({winner[0]}d) por menor varianza en point-call.")

    # Reporte de peg-stress candidates (winsor events)
    total_wins = winner[4] + winner[5]
    print(f"[peg-stress] observaciones winsorizadas: "
          f"{total_wins}/{n} = {100*total_wins/n:.2f}%  "
          f"(lo={winner[4]} hi={winner[5]})")
    # Show them
    lo_events = [(t, b) for t, b in zip(times, bps) if b < WINSOR_LO]
    hi_events = [(t, b) for t, b in zip(times, bps) if b > WINSOR_HI]
    if lo_events:
        print(f"[peg-stress lo, first 5]")
        for t, b in lo_events[:5]:
            print(f"    {datetime.fromtimestamp(t, tz=timezone.utc)}  bps={b:+.2f}")
    if hi_events:
        print(f"[peg-stress hi, first 5]")
        for t, b in hi_events[:5]:
            print(f"    {datetime.fromtimestamp(t, tz=timezone.utc)}  bps={b:+.2f}")


if __name__ == "__main__":
    main()
