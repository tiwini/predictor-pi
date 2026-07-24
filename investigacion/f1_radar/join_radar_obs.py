#!/usr/bin/env python3
"""Join helper: radar_snapshots ⋈ station_snapshots con tolerancia ±5min.

Fable spec (memoria f1_radar_design_closed_2026_07_20 D3-b):
"El join con tolerancia ±5min es función/vista versionada, NO SQL ad-hoc
repetido — evita 'dos análisis con Ns distintos y una tarde perdida
reconciliándolos'."

Version: v1 (2026-07-24)

Uso desde notebook:
    from investigacion.f1_radar.join_radar_obs import join_radar_obs
    df = join_radar_obs(conn, station='KMIA', date_range=('2026-07-01','2026-07-23'))
"""
import sqlite3
from datetime import datetime, timedelta
from typing import Optional


JOIN_TOLERANCE_MIN = 5

# SQL versionado — SI cambia, incrementar version arriba y documentar.
# Estrategia: para cada station_snapshot, encontrar el radar_snapshot más
# cercano en tiempo por misma estación, dentro de ±5min. Left join — sin
# radar disponible retorna NULL (frame missing o pre-backfill).
JOIN_SQL_V1 = """
WITH
ss AS (
    SELECT id, ts, station, current_f, today_max_obs,
           pred_calibrated_f, ens_med, ens_p10, ens_p90,
           bias_f, ext_med_f, ext_diff_f,
           wind_mph, wind_dir_deg, wind_gust_mph,
           dewpoint_f, humidity_pct,
           regime_tag, regime_reason,
           convective_ambient,
           narrative_line
    FROM station_snapshots
    WHERE station = :station
      AND date(ts) BETWEEN :start_date AND :end_date
),
rs AS (
    SELECT station_id, ts, dbz_5x5, dbz_9x9, source
    FROM radar_snapshots
    WHERE station_id = :station
      AND date(ts) BETWEEN :start_date AND :end_date
),
paired AS (
    SELECT
        ss.*,
        rs.ts AS radar_ts,
        rs.dbz_5x5,
        rs.dbz_9x9,
        rs.source AS radar_source,
        ABS((julianday(ss.ts) - julianday(rs.ts)) * 24 * 60) AS dt_min,
        ROW_NUMBER() OVER (
            PARTITION BY ss.id
            ORDER BY ABS(julianday(ss.ts) - julianday(rs.ts))
        ) AS rn
    FROM ss
    LEFT JOIN rs
        ON rs.station_id = ss.station
        AND ABS((julianday(ss.ts) - julianday(rs.ts)) * 24 * 60) <= :tolerance_min
)
SELECT
    id, ts, station, current_f, today_max_obs,
    pred_calibrated_f, ens_med, ens_p10, ens_p90,
    bias_f, ext_med_f, ext_diff_f,
    wind_mph, wind_dir_deg, wind_gust_mph,
    dewpoint_f, humidity_pct,
    regime_tag, regime_reason,
    convective_ambient,
    narrative_line,
    radar_ts, dbz_5x5, dbz_9x9, radar_source, dt_min
FROM paired
WHERE rn = 1 OR radar_ts IS NULL
ORDER BY ts
"""


def join_radar_obs(conn: sqlite3.Connection,
                   station: str,
                   date_range: tuple[str, str],
                   tolerance_min: int = JOIN_TOLERANCE_MIN):
    """Return list of dicts. Each row = station_snapshot con radar match si hay."""
    cur = conn.execute(JOIN_SQL_V1, {
        "station": station,
        "start_date": date_range[0],
        "end_date": date_range[1],
        "tolerance_min": tolerance_min,
    })
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def summary_stats(rows: list[dict]) -> dict:
    """Quick sanity: cuántos rows, cuántos tienen radar match, distrib dt_min."""
    total = len(rows)
    with_radar = sum(1 for r in rows if r.get("radar_ts") is not None)
    dt_mins = [r["dt_min"] for r in rows if r.get("dt_min") is not None]
    dt_mins.sort()
    if dt_mins:
        p50 = dt_mins[len(dt_mins) // 2]
        p90 = dt_mins[int(len(dt_mins) * 0.90)]
    else:
        p50 = p90 = None
    return {
        "total_rows": total,
        "with_radar_match": with_radar,
        "match_rate": with_radar / total if total else 0.0,
        "dt_min_p50": p50,
        "dt_min_p90": p90,
    }


if __name__ == "__main__":
    # Standalone smoke test
    import sys
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent.parent / "weather-predictor" / "analysis.db"
    conn = sqlite3.connect(str(db))
    for sid in ["KMIA", "KIAH", "KAUS", "KATL", "KMSY"]:
        rows = join_radar_obs(conn, sid, ("2026-07-03", "2026-07-24"))
        s = summary_stats(rows)
        print(f"{sid}: {s}")
