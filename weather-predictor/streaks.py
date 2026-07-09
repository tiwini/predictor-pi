"""Rachas de precisión por estación × ventana horaria local.

Para cada estación curada, mira los últimos `LOOKBACK` días con outcome final
y reconstruye qué predicción se tenía a cada hora ancla local (06/09/12/15/17).
La "racha actual" cuenta días consecutivos hacia atrás (desde ayer) con
`|pred - obs| ≤ THRESH_F`. Días sin snapshot dentro de ±TOL_MIN se saltan
(no rompen); solo un error fuera de threshold rompe.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from stations import STATION_IDS


# IANA timezone por estación (DST-aware vía zoneinfo).
STATION_TZ: dict[str, str] = {
    "KPHX": "America/Phoenix",
    "KLAX": "America/Los_Angeles",
    "KLAS": "America/Los_Angeles",
    "KLGA": "America/New_York",
    "KBOS": "America/New_York",
    "KMIA": "America/New_York",
    "KDCA": "America/New_York",
    "KPHL": "America/New_York",
    "KATL": "America/New_York",
    "KMDW": "America/Chicago",
    "KIAH": "America/Chicago",
    "KAUS": "America/Chicago",
    "KSAT": "America/Chicago",
    "KDFW": "America/Chicago",
    "KMSY": "America/Chicago",
    "KOKC": "America/Chicago",
    "KMSP": "America/Chicago",
    "KDEN": "America/Denver",
    "KSFO": "America/Los_Angeles",
    "KSEA": "America/Los_Angeles",
}

WINDOWS_LOCAL: tuple[int, ...] = (6, 9, 12, 15, 17)
THRESH_F: float = 1.5
TOL_MIN: int = 120
LOOKBACK: int = 14


@dataclass
class StreakDay:
    date: date
    pred_f: float
    obs_f: float
    err_f: float


@dataclass
class StreakRow:
    station_id: str
    window_local: int
    streak_days: int
    details: list[StreakDay]


def _snapshot_near(cur: sqlite3.Cursor, station_id: str, d: date,
                   local_hour: int, tol_min: int) -> float | None:
    """Devuelve el threshold del snapshot auto más cercano a `local_hour`
    en la zona local de la estación, si está dentro de ±tol_min minutos."""
    tz = ZoneInfo(STATION_TZ.get(station_id, "UTC"))
    local_dt = datetime.combine(d, datetime.min.time()).replace(
        hour=local_hour, tzinfo=tz)
    target_utc = local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    # busca el snapshot más cercano por |Δt|
    cur.execute("""
        SELECT threshold, snapshot_time
          FROM prediction_snapshots
         WHERE station_id = ?
           AND date = ?
           AND is_auto = 1
           AND (op IS NULL OR op != 'b')
           AND threshold IS NOT NULL
         ORDER BY ABS(julianday(snapshot_time) - julianday(?)) ASC
         LIMIT 1
    """, (station_id, d.isoformat(), target_utc.isoformat()))
    row = cur.fetchone()
    if not row:
        return None
    pred, stime = row
    # normaliza snapshot a naive UTC
    snap_dt = datetime.fromisoformat(stime.replace("Z", "").split("+")[0])
    if abs((snap_dt - target_utc).total_seconds()) > tol_min * 60:
        return None
    return float(pred)


def _streak_for(cur: sqlite3.Cursor, station_id: str, today: date,
                window_local: int, lookback: int,
                thresh_f: float, tol_min: int) -> StreakRow:
    """Computa racha actual para (station, window). Recorre días ayer→atrás."""
    streak = 0
    details: list[StreakDay] = []
    for i in range(1, lookback + 1):
        d = today - timedelta(days=i)
        cur.execute("""SELECT max_obs_f FROM day_outcomes
                       WHERE station_id = ? AND date = ?""",
                    (station_id, d.isoformat()))
        row = cur.fetchone()
        if not row or row[0] is None:
            continue  # día sin obs final → salta
        obs = float(row[0])
        pred = _snapshot_near(cur, station_id, d, window_local, tol_min)
        if pred is None:
            continue  # sin snapshot en la ventana → salta
        err = pred - obs
        if abs(err) <= thresh_f:
            streak += 1
            details.append(StreakDay(d, pred, obs, err))
        else:
            break  # solo error > umbral rompe la racha
    return StreakRow(station_id, window_local, streak, details)


def compute_streaks(
    db_path: str,
    today: date | None = None,
    *,
    stations: list[str] | None = None,
    windows: tuple[int, ...] = WINDOWS_LOCAL,
    lookback: int = LOOKBACK,
    thresh_f: float = THRESH_F,
    tol_min: int = TOL_MIN,
) -> dict[int, list[StreakRow]]:
    """Devuelve {window_local: [StreakRow ordenado por streak desc]}.

    Solo incluye estaciones con streak ≥ 1. Usa stations curados de
    `stations.STATION_IDS` por defecto.
    """
    if today is None:
        today = date.today()
    if stations is None:
        stations = STATION_IDS
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        out: dict[int, list[StreakRow]] = {}
        for w in windows:
            rows: list[StreakRow] = []
            for st in stations:
                r = _streak_for(cur, st, today, w, lookback, thresh_f, tol_min)
                if r.streak_days >= 1:
                    rows.append(r)
            rows.sort(key=lambda r: (-r.streak_days, r.station_id))
            out[w] = rows
        return out
    finally:
        con.close()


def to_json(streaks: dict[int, list[StreakRow]], top_n: int = 3) -> dict:
    """Serializa para el endpoint /api/streak."""
    return {
        "threshold_f": THRESH_F,
        "tolerance_min": TOL_MIN,
        "lookback_days": LOOKBACK,
        "windows": [
            {
                "window_local": w,
                "top": [
                    {
                        "station_id": r.station_id,
                        "streak_days": r.streak_days,
                        "details": [
                            {"date": dd.date.isoformat(),
                             "pred_f": round(dd.pred_f, 1),
                             "obs_f": round(dd.obs_f, 1),
                             "err_f": round(dd.err_f, 1)}
                            for dd in r.details[:5]
                        ],
                    }
                    for r in rows[:top_n]
                ],
            }
            for w, rows in streaks.items()
        ],
    }
