"""Detector de divergencia D+1/D+2.

A medida que se acerca un target_date, el ensemble debería estrecharse
(spread p10-p90 baja). Si crece, algo está mal: el modelo está perdiendo
confianza en vez de ganarla.

Mecanismo: en cada poll de /cross guardamos (station, target_date,
day_offset, p10, p50, p90, snapshot_at). El detector compara el spread
del último snapshot por day_offset y verifica monotonía: D+0 ≤ D+1 ≤ D+2.
Cualquier ruptura → divergencia.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "calibration.db"

# Tolerancia: ignoramos rupturas <0.5°F (ruido). Banda en °F.
NOISE_TOLERANCE = 0.5


def _con():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS day_band_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        station_id TEXT NOT NULL,
        target_date TEXT NOT NULL,
        day_offset INTEGER NOT NULL,
        snapshot_at TEXT NOT NULL,
        p10 REAL, p50 REAL, p90 REAL,
        n_members INTEGER
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_dbh_station_target
                 ON day_band_history(station_id, target_date, day_offset)""")
    return c


def record_band(station_id: str, target_date, day_offset: int,
                p10: Optional[float], p50: Optional[float],
                p90: Optional[float], n_members: int = 0) -> None:
    if p10 is None or p90 is None:
        return
    c = _con()
    try:
        c.execute(
            "INSERT INTO day_band_history "
            "(station_id, target_date, day_offset, snapshot_at, "
            " p10, p50, p90, n_members) VALUES (?,?,?,?,?,?,?,?)",
            (station_id, target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date),
             int(day_offset),
             datetime.now(timezone.utc).isoformat(),
             float(p10), float(p50) if p50 is not None else None, float(p90),
             int(n_members)),
        )
        c.commit()
    finally:
        c.close()


def latest_per_offset(station_id: str, target_date) -> dict:
    """Return {day_offset: (snapshot_at, spread, p10, p50, p90)} most recent
    per offset for a given (station, target_date)."""
    c = _con()
    try:
        cur = c.execute(
            """SELECT day_offset, snapshot_at, p10, p50, p90
               FROM day_band_history
               WHERE station_id=? AND target_date=?
               ORDER BY snapshot_at DESC""",
            (station_id, target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date)),
        )
        seen = {}
        for off, ts, p10, p50, p90 in cur.fetchall():
            if off in seen or p10 is None or p90 is None:
                continue
            seen[off] = (ts, p90 - p10, p10, p50, p90)
        return seen
    finally:
        c.close()


def detect(station_id: str, target_date) -> dict:
    """Detect divergence for (station, target_date).

    Returns dict:
      diverging   : True iff some pair breaks monotonicity beyond noise
      message     : short human label (en español)
      offsets     : {day_offset: spread} for what we have
      breaks      : list of (smaller_offset, larger_offset, smaller_spread, larger_spread)
    """
    snaps = latest_per_offset(station_id, target_date)
    offsets = {k: v[1] for k, v in snaps.items()}
    breaks = []
    if len(offsets) >= 2:
        keys = sorted(offsets.keys())  # e.g. [0,1,2]
        for i in range(len(keys) - 1):
            a, b = keys[i], keys[i + 1]
            sa, sb = offsets[a], offsets[b]
            if sa - sb > NOISE_TOLERANCE:
                breaks.append((a, b, sa, sb))

    if not breaks:
        return {"diverging": False, "message": "", "offsets": offsets, "breaks": []}

    a, b, sa, sb = breaks[0]
    return {
        "diverging": True,
        "message": f"D+{a} ({sa:.1f}°F) más ancho que D+{b} ({sb:.1f}°F)",
        "offsets": offsets,
        "breaks": breaks,
    }
