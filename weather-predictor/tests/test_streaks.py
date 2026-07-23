"""Tests for streaks: rachas de precisión por estación × ventana local.

Sembramos una DB temporal con (date, obs) y snapshots auto cerca de horas
ancla locales. La función compute_streaks debe encontrar la racha actual
desde ayer hacia atrás, saltando días sin snapshot/obs, rompiendo solo
cuando |err| > threshold.
"""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

import streaks


def _seed_schema(db_path: Path):
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE day_outcomes (
            station_id TEXT, date TEXT, max_obs_f REAL, settled_at TEXT,
            PRIMARY KEY (station_id, date)
        );
        CREATE TABLE prediction_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT, date TEXT, snapshot_time TEXT,
            slot INTEGER, is_auto INTEGER,
            expr TEXT, op TEXT, threshold REAL, bin_half REAL,
            predicted_p REAL, outcome INTEGER
        );
    """)
    con.commit()
    con.close()


def _insert_outcome(db_path: Path, station: str, d: date, obs: float):
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO day_outcomes (station_id,date,max_obs_f,settled_at) "
                "VALUES (?,?,?,?)",
                (station, d.isoformat(), obs, "2026-01-01T00:00:00+00:00"))
    con.commit()
    con.close()


def _insert_snapshot(db_path: Path, station: str, d: date,
                     local_hour: int, threshold: float,
                     offset_min: int = 0):
    """Inserta snapshot auto a `local_hour` (en tz de la estación) + offset."""
    tz = ZoneInfo(streaks.STATION_TZ[station])
    local_dt = datetime.combine(d, datetime.min.time()).replace(
        hour=local_hour, tzinfo=tz) + timedelta(minutes=offset_min)
    utc = local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO prediction_snapshots "
                "(station_id,date,snapshot_time,slot,is_auto,expr,op,threshold,predicted_p) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (station, d.isoformat(), utc.isoformat() + "+00:00",
                 0, 1, "auto", "~", threshold, 0.5))
    con.commit()
    con.close()


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "calibration.db"
    _seed_schema(p)
    return p


def test_streak_counts_consecutive_hits(db):
    today = date(2026, 6, 25)
    for i, (obs, pred) in enumerate([(80, 80.0), (81, 80.5), (82, 81.5)], start=1):
        d = today - timedelta(days=i)
        _insert_outcome(db, "KNYC", d, obs)
        _insert_snapshot(db, "KNYC", d, 15, pred)
    out = streaks.compute_streaks(str(db), today=today, stations=["KNYC"],
                                  windows=(15,))
    assert out[15][0].station_id == "KNYC"
    assert out[15][0].streak_days == 3


def test_streak_broken_by_error_above_threshold(db):
    today = date(2026, 6, 25)
    # ayer hit, anteayer fail, antes-de-antier hit → racha = 1
    plan = [(80, 80.0), (90, 80.0), (78, 78.0)]
    for i, (obs, pred) in enumerate(plan, start=1):
        d = today - timedelta(days=i)
        _insert_outcome(db, "KNYC", d, obs)
        _insert_snapshot(db, "KNYC", d, 15, pred)
    out = streaks.compute_streaks(str(db), today=today, stations=["KNYC"],
                                  windows=(15,))
    assert out[15][0].streak_days == 1


def test_missing_snapshot_skips_does_not_break(db):
    today = date(2026, 6, 25)
    # ayer hit, anteayer sin snapshot, antes hit → racha = 2 (salta el hueco)
    d1, d2, d3 = today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=3)
    _insert_outcome(db, "KNYC", d1, 80); _insert_snapshot(db, "KNYC", d1, 15, 80.0)
    _insert_outcome(db, "KNYC", d2, 81)  # sin snapshot
    _insert_outcome(db, "KNYC", d3, 79); _insert_snapshot(db, "KNYC", d3, 15, 79.5)
    out = streaks.compute_streaks(str(db), today=today, stations=["KNYC"],
                                  windows=(15,))
    assert out[15][0].streak_days == 2


def test_snapshot_outside_tolerance_skips(db):
    today = date(2026, 6, 25)
    d = today - timedelta(days=1)
    _insert_outcome(db, "KNYC", d, 80)
    # snapshot 3 horas DESPUÉS de las 15:00 local → fuera de ±2h
    _insert_snapshot(db, "KNYC", d, 15, 80.0, offset_min=180)
    out = streaks.compute_streaks(str(db), today=today, stations=["KNYC"],
                                  windows=(15,))
    assert out.get(15, []) == []  # no califica


def test_snapshot_inside_tolerance_counts(db):
    today = date(2026, 6, 25)
    d = today - timedelta(days=1)
    _insert_outcome(db, "KNYC", d, 80)
    # snapshot 90 min después → dentro de ±120
    _insert_snapshot(db, "KNYC", d, 15, 80.0, offset_min=90)
    out = streaks.compute_streaks(str(db), today=today, stations=["KNYC"],
                                  windows=(15,))
    assert out[15][0].streak_days == 1


def test_to_json_shape(db):
    today = date(2026, 6, 25)
    d = today - timedelta(days=1)
    _insert_outcome(db, "KNYC", d, 80)
    _insert_snapshot(db, "KNYC", d, 15, 80.5)
    out = streaks.compute_streaks(str(db), today=today, stations=["KNYC"],
                                  windows=(15,))
    payload = streaks.to_json(out, top_n=3)
    assert payload["threshold_f"] == streaks.THRESH_F
    assert len(payload["windows"]) == 1
    w = payload["windows"][0]
    assert w["window_local"] == 15
    assert w["top"][0]["station_id"] == "KNYC"
    assert w["top"][0]["streak_days"] == 1
    assert w["top"][0]["details"][0]["err_f"] == 0.5


def test_threshold_break_strict(db):
    """Exactamente 1.5 pasa, 1.6 rompe."""
    today = date(2026, 6, 25)
    d1 = today - timedelta(days=1)
    d2 = today - timedelta(days=2)
    _insert_outcome(db, "KNYC", d1, 80); _insert_snapshot(db, "KNYC", d1, 15, 81.5)
    _insert_outcome(db, "KNYC", d2, 80); _insert_snapshot(db, "KNYC", d2, 15, 81.6)
    out = streaks.compute_streaks(str(db), today=today, stations=["KNYC"],
                                  windows=(15,))
    assert out[15][0].streak_days == 1  # d1 pasa, d2 rompe
