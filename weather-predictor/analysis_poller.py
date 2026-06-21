"""Background cycler que recorre las 5 estaciones cada N min y guarda
snapshots a analysis.db para alimentar el tab /analysis del dashboard.

Reusa predictor.build_snapshot y kalshi.fetch_bins (que internamente
respetan TTL cache de 10 min, así que invocar cada 10 min está alineado).

Schema:
  station_snapshots: ts, station, current_f, ens_med, ens_p10, ens_p90, ens_maxes_json
  kalshi_snapshots: ts, station, ticker, bin_lo, bin_hi, label, yes_mid, our_p
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from predictor import build_snapshot, fetch_station
import kalshi

STATIONS = [
    "KPHX", "KLAX", "KLAS", "KLGA", "KBOS",
    "KMIA", "KMDW", "KIAH", "KSFO", "KAUS",
    "KDEN", "KSAT", "KDCA", "KDFW", "KPHL",
    "KSEA", "KATL", "KMSY", "KOKC", "KMSP",
]
INTERVAL_S = 600  # 10 min (20 estaciones × ~12s = ~4 min, deja 6 min margen)
DB_PATH = Path(__file__).parent / "analysis.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [analysis_poller] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("analysis_poller")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS station_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            station TEXT NOT NULL,
            current_f REAL,
            today_max_obs REAL,
            ens_med REAL,
            ens_p10 REAL,
            ens_p90 REAL,
            ens_maxes_json TEXT,
            peak_status TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ss_station_ts
            ON station_snapshots(station, ts);

        CREATE TABLE IF NOT EXISTS kalshi_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            station TEXT NOT NULL,
            ticker TEXT NOT NULL,
            bin_lo REAL NOT NULL,
            bin_hi REAL NOT NULL,
            label TEXT,
            yes_mid REAL,
            our_p REAL
        );
        CREATE INDEX IF NOT EXISTS idx_ks_station_ts
            ON kalshi_snapshots(station, ts);
        CREATE INDEX IF NOT EXISTS idx_ks_bin
            ON kalshi_snapshots(station, bin_lo, bin_hi, ts);
    """)
    return c


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * pct)))
    return sorted_vals[idx]


def poll_one(station_id: str, c: sqlite3.Connection) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    log.info("polling %s", station_id)
    try:
        station = fetch_station(station_id)
        snap = build_snapshot(station)
    except Exception as e:
        log.warning("  build_snapshot %s failed: %s", station_id, e)
        return

    maxes = sorted(snap.ensemble_daily_maxes)
    med = _percentile(maxes, 0.5)
    p10 = _percentile(maxes, 0.1)
    p90 = _percentile(maxes, 0.9)

    c.execute("""INSERT INTO station_snapshots
        (ts, station, current_f, today_max_obs, ens_med, ens_p10, ens_p90,
         ens_maxes_json, peak_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ts, station_id, snap.current_temp_f, snap.today_max_obs,
         med, p10, p90, json.dumps(maxes), snap.peak_status))

    # Kalshi bins (puede que no haya mercado abierto = []).
    today = snap.station_local.date()
    try:
        bins = kalshi.fetch_bins(station_id, today)
    except Exception as e:
        log.warning("  kalshi.fetch_bins %s failed: %s", station_id, e)
        bins = []

    for b in bins:
        our_p = kalshi.our_p_for_bin(snap.ensemble_daily_maxes, b.bin_lo, b.bin_hi)
        c.execute("""INSERT INTO kalshi_snapshots
            (ts, station, ticker, bin_lo, bin_hi, label, yes_mid, our_p)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, station_id, b.ticker, b.bin_lo, b.bin_hi, b.label,
             b.yes_mid, our_p))
    c.commit()
    log.info("  saved %s: current=%.1f med=%.1f kalshi_bins=%d",
             station_id, snap.current_temp_f or 0, med, len(bins))


def cleanup_old(c: sqlite3.Connection, keep_days: int = 30) -> None:
    """Borra snapshots > keep_days. ~260 MB/año con todo; 30 días sobra para
    el tab de análisis. Histórico largo va a calibration.db (otro proyecto)."""
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    c.execute("DELETE FROM station_snapshots WHERE ts < ?", (cutoff_iso,))
    c.execute("DELETE FROM kalshi_snapshots WHERE ts < ?", (cutoff_iso,))
    c.commit()


def main() -> None:
    log.info("analysis_poller started; interval=%ds stations=%s",
             INTERVAL_S, STATIONS)
    cycle = 0
    while True:
        c = _conn()
        for sid in STATIONS:
            poll_one(sid, c)
        cycle += 1
        if cycle % 144 == 0:  # ~1 vez al día
            cleanup_old(c)
            log.info("cleanup ejecutado")
        c.close()
        log.info("ciclo completo; durmiendo %ds", INTERVAL_S)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")
        sys.exit(0)
