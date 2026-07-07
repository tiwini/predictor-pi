"""Historical climatology via Open-Meteo archive API with SQLite cache.

Provides percentile of a given temperature vs the same-date-of-year across
the last ~30 years at a station. Cache is keyed by station id; first call
per station fetches ~11k daily maxes (takes 1-2s); subsequent calls are
served from disk.
"""
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "climate_cache.db"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
UA = "weather-predictor/0.1"


@dataclass
class ClimateStats:
    percentile: float   # 0-100, where current value falls
    n_samples: int      # days in window across years
    p10: float
    p50: float
    p90: float
    record: float
    record_low: float
    year_span: str      # e.g. "1995-2024"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS climatology (
            station_id TEXT,
            date TEXT,
            temp_max_f REAL,
            PRIMARY KEY (station_id, date)
        );
        CREATE TABLE IF NOT EXISTS station_meta (
            station_id TEXT PRIMARY KEY,
            last_fetch TEXT,
            start_year INTEGER,
            end_year INTEGER
        );
    """)
    return c


def ensure_cache(station, years: int = 30, force: bool = False) -> None:
    """Fetch daily max history if we don't already have enough cached."""
    c = _conn()
    cur = c.execute(
        "SELECT start_year, end_year FROM station_meta WHERE station_id=?",
        (station.id,))
    row = cur.fetchone()
    current_year = date.today().year
    target_start = current_year - years
    target_end = current_year - 1
    if row and not force:
        start_y, end_y = row
        if start_y <= target_start and end_y >= target_end:
            c.close()
            return
    # fetch
    r = requests.get(ARCHIVE_URL, params={
        "latitude": station.lat,
        "longitude": station.lon,
        "start_date": f"{target_start}-01-01",
        "end_date": f"{target_end}-12-31",
        "daily": "temperature_2m_max",
        "timezone": station.tz.key,
        "temperature_unit": "fahrenheit",
    }, timeout=60, headers={"User-Agent": UA})
    try:
        import om_quota
        om_quota.count_call("climatology_archive")
    except Exception:
        pass
    r.raise_for_status()
    d = r.json()["daily"]
    rows = [(station.id, t, v) for t, v in zip(d["time"], d["temperature_2m_max"])
            if v is not None]
    c.executemany("INSERT OR REPLACE INTO climatology VALUES (?, ?, ?)", rows)
    c.execute("""INSERT OR REPLACE INTO station_meta
                 VALUES (?, ?, ?, ?)""",
              (station.id, datetime.utcnow().isoformat(), target_start, target_end))
    c.commit()
    c.close()


def percentile_of(station, target_date: date, temp_f: float,
                  window_days: int = 7) -> ClimateStats | None:
    """Compute percentile of temp_f vs historical max on ±window_days
    around the target date's (month, day) across all cached years."""
    ensure_cache(station)
    c = _conn()
    cur = c.execute(
        "SELECT date, temp_max_f FROM climatology WHERE station_id=?",
        (station.id,))
    target_ref = date(2000, target_date.month, target_date.day)
    vals = []
    years_seen = set()
    for date_str, v in cur:
        try:
            y, m, d = (int(x) for x in date_str.split("-"))
        except ValueError:
            continue
        ref = date(2000, m, d)
        diff = abs((ref - target_ref).days)
        if diff > 183:
            diff = 365 - diff
        if diff <= window_days:
            vals.append(v)
            years_seen.add(y)
    c.close()
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    below = sum(1 for v in vals if v < temp_f)
    pct = below / n * 100
    years = sorted(years_seen)
    return ClimateStats(
        percentile=pct,
        n_samples=n,
        p10=vals[int(n * 0.1)],
        p50=vals[n // 2],
        p90=vals[int(n * 0.9)],
        record=vals[-1],
        record_low=vals[0],
        year_span=f"{years[0]}-{years[-1]}" if years else "?",
    )
