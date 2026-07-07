"""One-shot backfill: prediction_snapshots (op='b') desde market_cache.db.

Fable/Codex retro 2026-07-06 P0 #2 — la reliability rota tenía como causa
raíz que 6/8 estaciones no tienen training pairs para isotonic. Origen:
`_instrument_kalshi_bins` lee de analysis.db/kalshi_snapshots que solo
existe desde 2026-06-21, mientras que market_cache.db/market_prices tiene
41-63 días históricos por estación. Este script rehidrata prediction_snapshots
directamente desde market_prices para cada (station, date) con day_outcome.

Idempotente en (station_id, date, op='b', threshold, snapshot_time).
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

CALIB_DB = Path("/home/popeye/predictor-pi/weather-predictor/calibration.db")
MARKET_DB = Path("/home/popeye/predictor-pi/weather-predictor/market_cache.db")

LAPLACE_CUTOFF_TS = "2026-07-01T22:00:00+00:00"


def _bin_contains(max_f: float, lo: float, hi: float) -> bool:
    """Half-integer padding: mirrors kalshi.our_p_for_bin / calibration._bin_contains."""
    l = float("-inf") if lo == float("-inf") else lo - 0.5
    h = float("inf") if hi == float("inf") else hi + 0.5
    return l <= max_f < h


def main() -> None:
    cal = sqlite3.connect(CALIB_DB)
    mkt = sqlite3.connect(MARKET_DB)

    day_outcomes = cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes"
    ).fetchall()
    print(f"Loaded {len(day_outcomes)} day_outcomes.")

    n_inserted = 0
    n_skipped = 0
    n_no_market = 0
    per_station: dict[str, int] = {}

    for station_id, target_date, max_f in day_outcomes:
        if max_f is None:
            continue
        rows = mkt.execute("""
            SELECT bin_lo, bin_hi, our_p, fetched_at, ticker
            FROM market_prices mp1
            WHERE station_id=? AND date=? AND our_p IS NOT NULL
              AND fetched_at = (
                SELECT MAX(fetched_at) FROM market_prices mp2
                WHERE mp2.station_id=mp1.station_id
                  AND mp2.date=mp1.date
                  AND mp2.ticker=mp1.ticker
                  AND mp2.our_p IS NOT NULL
              )
        """, (station_id, target_date)).fetchall()
        if not rows:
            n_no_market += 1
            continue
        for lo, hi, our_p, fetched_at, ticker in rows:
            lo_f = float(lo)
            hi_f = float(hi)
            outc = 1 if _bin_contains(float(max_f), lo_f, hi_f) else 0
            if lo_f == float("-inf"):
                threshold = hi_f
                bin_half = None
            elif hi_f == float("inf"):
                threshold = lo_f
                bin_half = None
            else:
                threshold = lo_f
                bin_half = (hi_f - lo_f) / 2.0
            p_version = ("post_laplace" if fetched_at >= LAPLACE_CUTOFF_TS
                         else "pre_laplace")
            existing = cal.execute(
                """SELECT id FROM prediction_snapshots
                   WHERE station_id=? AND date=? AND op='b'
                     AND threshold=? AND snapshot_time=?""",
                (station_id, target_date, threshold, fetched_at)
            ).fetchone()
            if existing:
                n_skipped += 1
                continue
            expr = f"kalshi_bin[{lo_f},{hi_f}]"
            cal.execute(
                """INSERT INTO prediction_snapshots
                   (station_id, date, snapshot_time, slot, is_auto, expr,
                    op, threshold, bin_half, predicted_p, outcome, p_version)
                   VALUES (?, ?, ?, 0, 1, ?, 'b', ?, ?, ?, ?, ?)""",
                (station_id, target_date, fetched_at, expr, threshold,
                 bin_half, float(our_p), outc, p_version))
            n_inserted += 1
            per_station[station_id] = per_station.get(station_id, 0) + 1

    cal.commit()
    cal.close()
    mkt.close()

    print(f"\nInserted: {n_inserted}")
    print(f"Skipped (already present): {n_skipped}")
    print(f"Days with day_outcome but no market_prices: {n_no_market}")
    print("\nPer-station inserts:")
    for s in sorted(per_station, key=lambda k: -per_station[k]):
        print(f"  {s}: +{per_station[s]}")


if __name__ == "__main__":
    main()
