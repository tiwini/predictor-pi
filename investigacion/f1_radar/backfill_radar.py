#!/usr/bin/env python3
"""F1 radar backfill — 3 semanas × 5 estaciones convectivas.

Spec Fable (memoria f1_radar_design_closed_2026_07_20):
- Estaciones: KMIA, KIAH, KAUS, KATL, KMSY (las 5 convectivas)
- Ventana temporal: peak 14-17 local. Cover 18:00-23:00 UTC (ET & CT juntas)
- Cadencia: 5-min (N0R)
- Ventanas espaciales: dbz_5x5 + dbz_9x9 (separadas — D1)
- Tabla: radar_snapshots (station_id, ts, dbz_5x5, dbz_9x9, source)
- Source: 'n0r_backfill'
- Streaming: no guardar PNGs (100-500 MB total)

Hipótesis del descriptivo (D1): la señal más informativa para convective_ambient
puede ser dbz_9x9 - dbz_5x5:
- 9x9 alto + 5x5 bajo → storm cerca (outflow) — KMIA 07-19 pattern
- 5x5 alto → precipitación sobre la estación

Ejecutar con: nohup ./venv/bin/python3 backfill_radar.py > backfill_radar.log 2>&1 &
"""
import io
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

UA = "predictor-pi-f1-backfill/0.1"

# Las 5 estaciones convectivas según spec Fable
STATIONS = {
    "KMIA": (25.79, -80.29),   # Miami
    "KIAH": (29.99, -95.34),   # Houston Intercontinental
    "KAUS": (30.19, -97.67),   # Austin
    "KATL": (33.64, -84.43),   # Atlanta
    "KMSY": (29.99, -90.26),   # New Orleans
}

# 3 semanas = 21 días atrás
DAYS_BACK = 21
# Ventana UTC: 18:00-23:00 covers 14-17 local para ET y CT
UTC_START_HOUR = 18
UTC_END_HOUR = 23  # exclusivo
CADENCE_MIN = 5

# World file constants (verificado en projection_check)
UL_LON = -126.0
UL_LAT = 50.0
PX_LON = 0.01
PX_LAT = -0.01

DB_PATH = Path(__file__).resolve().parent.parent.parent / "weather-predictor" / "analysis.db"


def create_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS radar_snapshots (
            station_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            dbz_5x5 INTEGER,
            dbz_9x9 INTEGER,
            source TEXT NOT NULL DEFAULT 'n0r_backfill',
            PRIMARY KEY (station_id, ts, source)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_radar_station_ts
            ON radar_snapshots(station_id, ts)
    """)
    conn.commit()


def lonlat_to_pixel(lon: float, lat: float) -> tuple[int, int]:
    col = int(round((lon - UL_LON) / PX_LON))
    row = int(round((lat - UL_LAT) / PX_LAT))
    return col, row


def palette_to_dbz(idx: int) -> int | None:
    """N0R palette (verificado): dBZ = -35 + 5*idx. idx=0 = missing."""
    if idx == 0:
        return None
    return -35 + 5 * idx


def extract_window_max(px, col: int, row: int, width: int, height: int,
                       size: int) -> int | None:
    """Extract (size x size) window centered at (col,row), return MAX dBZ."""
    half = size // 2
    best = None
    for dr in range(-half, half + 1):
        for dc in range(-half, half + 1):
            r, c = row + dr, col + dc
            if 0 <= r < height and 0 <= c < width:
                idx = px[c, r]
                if isinstance(idx, tuple):
                    idx = idx[0]
                if idx > 0:
                    dbz = -35 + 5 * idx
                    if best is None or dbz > best:
                        best = dbz
    return best


def process_frame(ts_str: str, conn) -> int:
    """Fetch frame, extract windows for all stations, insert. Return rows written."""
    dt = datetime.strptime(ts_str, "%Y%m%d%H%M")
    url = (f"https://mesonet.agron.iastate.edu/archive/data/"
           f"{dt.strftime('%Y/%m/%d')}/GIS/uscomp/n0r_{ts_str}.png")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return -1  # frame missing (common — return sentinel)
        raise
    except Exception as e:
        print(f"    ERR fetch {ts_str}: {e}", file=sys.stderr)
        return 0

    img = Image.open(io.BytesIO(data))
    px = img.load()
    W, H = img.size
    ts_iso = dt.replace(tzinfo=timezone.utc).isoformat()

    rows_written = 0
    for sid, (lat, lon) in STATIONS.items():
        col, row = lonlat_to_pixel(lon, lat)
        if not (0 <= col < W and 0 <= row < H):
            continue
        dbz5 = extract_window_max(px, col, row, W, H, 5)
        dbz9 = extract_window_max(px, col, row, W, H, 9)
        conn.execute(
            "INSERT OR IGNORE INTO radar_snapshots "
            "(station_id, ts, dbz_5x5, dbz_9x9, source) "
            "VALUES (?, ?, ?, ?, 'n0r_backfill')",
            (sid, ts_iso, dbz5, dbz9),
        )
        rows_written += 1
    conn.commit()
    return rows_written


def main():
    print(f"F1 RADAR BACKFILL — {DAYS_BACK} días × {len(STATIONS)} estaciones")
    print(f"Ventana UTC: {UTC_START_HOUR:02d}-{UTC_END_HOUR:02d}, cadencia {CADENCE_MIN} min")
    print(f"DB: {DB_PATH}")
    print()

    conn = sqlite3.connect(str(DB_PATH))
    create_table(conn)

    # Generate all timestamps to process
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=DAYS_BACK)

    frames = []
    d = start_date
    while d <= end_date:
        for hh in range(UTC_START_HOUR, UTC_END_HOUR):
            for mm in range(0, 60, CADENCE_MIN):
                frames.append(datetime(d.year, d.month, d.day, hh, mm).strftime("%Y%m%d%H%M"))
        d += timedelta(days=1)

    print(f"Total frames to process: {len(frames)}")
    print(f"Estimated: {len(frames) * 5}s = {len(frames)/12:.0f} min at 5s/frame")
    print()

    t0 = time.time()
    ok = missing = err = 0
    for i, ts in enumerate(frames):
        rows = process_frame(ts, conn)
        if rows > 0:
            ok += 1
        elif rows == -1:
            missing += 1
        else:
            err += 1
        # Rate limit: gentle to Iowa Mesonet
        time.sleep(0.2)
        # Progress cada 60 frames
        if (i + 1) % 60 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta_s = (len(frames) - (i + 1)) / rate
            print(f"  [{i+1:4}/{len(frames):4}] ok={ok:4} missing={missing:3} err={err:2} "
                  f"| {elapsed:.0f}s elapsed | rate={rate:.1f} fps | ETA {eta_s/60:.1f}min")

    total_rows = conn.execute(
        "SELECT COUNT(*) FROM radar_snapshots WHERE source='n0r_backfill'"
    ).fetchone()[0]
    print()
    print(f"DONE — ok={ok} missing={missing} err={err} | total_rows={total_rows}")
    conn.close()


if __name__ == "__main__":
    main()
