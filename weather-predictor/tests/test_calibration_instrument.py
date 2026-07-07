"""Tests for calibration._instrument_kalshi_bins — el instrumento que espeja
kalshi_snapshots (analysis.db) → prediction_snapshots (calibration.db) en el
settle path para alimentar isotonic con pairs unbiased.
"""
import sqlite3
from datetime import date

import calibration


def _make_calib_db(path):
    """Empty calibration.db with prediction_snapshots + p_version column."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE prediction_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            slot INTEGER NOT NULL,
            is_auto INTEGER NOT NULL,
            expr TEXT NOT NULL,
            op TEXT NOT NULL,
            threshold REAL NOT NULL,
            bin_half REAL,
            predicted_p REAL NOT NULL,
            outcome INTEGER,
            p_version TEXT
        );
    """)
    c.commit()
    return c


def _make_analysis_db(path, rows):
    """rows: list of (ts, station, bin_lo, bin_hi, our_p)."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE kalshi_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            station TEXT NOT NULL,
            ticker TEXT NOT NULL,
            bin_lo REAL NOT NULL,
            bin_hi REAL NOT NULL,
            label TEXT,
            yes_mid REAL,
            our_p REAL,
            our_p_calibrated REAL
        );
    """)
    for ts, stn, lo, hi, p in rows:
        c.execute(
            """INSERT INTO kalshi_snapshots
               (ts, station, ticker, bin_lo, bin_hi, our_p)
               VALUES (?, ?, 'T', ?, ?, ?)""",
            (ts, stn, lo, hi, p))
    c.commit()
    c.close()


def test_instrument_writes_one_row_per_bin_last_snapshot(tmp_path, monkeypatch):
    calib = tmp_path / "calibration.db"
    analysis = tmp_path / "analysis.db"
    _make_analysis_db(analysis, [
        ("2026-07-02T06:00:00+00:00", "KPHX", 90.0, 91.0, 0.10),
        ("2026-07-02T17:00:00+00:00", "KPHX", 90.0, 91.0, 0.25),  # later
        ("2026-07-02T17:00:00+00:00", "KPHX", 91.0, 92.0, 0.60),
        ("2026-07-02T17:00:00+00:00", "KPHX", 92.0, 93.0, 0.15),
        ("2026-07-02T17:00:00+00:00", "KMIA", 88.0, 89.0, 0.40),  # otra station
    ])
    monkeypatch.setattr(calibration, "ANALYSIS_DB_PATH", analysis)
    c = _make_calib_db(calib)
    n = calibration._instrument_kalshi_bins(c, "KPHX", date(2026, 7, 2),
                                            max_f=91.0)
    c.commit()
    assert n == 3  # tres bins de KPHX
    rows = c.execute(
        "SELECT threshold, predicted_p, outcome FROM prediction_snapshots "
        "WHERE station_id='KPHX' ORDER BY threshold"
    ).fetchall()
    # 90-91: contains 91.0? _bin_contains usa [lo-0.5, hi+0.5) → [89.5, 91.5)
    #   → sí, outcome=1. Kept LAST snapshot with p=0.25.
    # 91-92: [90.5, 92.5) → 91.0 sí → outcome=1, p=0.60.
    # 92-93: [91.5, 93.5) → 91.0 no → outcome=0, p=0.15.
    assert rows == [(90.0, 0.25, 1), (91.0, 0.60, 1), (92.0, 0.15, 0)]


def test_instrument_tags_p_version_by_ts_cutoff(tmp_path, monkeypatch):
    calib = tmp_path / "calibration.db"
    analysis = tmp_path / "analysis.db"
    _make_analysis_db(analysis, [
        # Pre-Laplace: antes del cutoff 2026-07-01T22:00Z
        ("2026-06-30T18:00:00+00:00", "KPHX", 90.0, 91.0, 1.00),
        # Post-Laplace
        ("2026-07-02T18:00:00+00:00", "KPHX", 91.0, 92.0, 0.60),
    ])
    monkeypatch.setattr(calibration, "ANALYSIS_DB_PATH", analysis)
    c = _make_calib_db(calib)
    calibration._instrument_kalshi_bins(c, "KPHX", date(2026, 6, 30),
                                        max_f=91.0)
    calibration._instrument_kalshi_bins(c, "KPHX", date(2026, 7, 2),
                                        max_f=91.0)
    c.commit()
    versions = dict(c.execute(
        "SELECT date, p_version FROM prediction_snapshots "
        "WHERE station_id='KPHX' ORDER BY date"
    ).fetchall())
    assert versions["2026-06-30"] == "pre_laplace"
    assert versions["2026-07-02"] == "post_laplace"


def test_instrument_is_idempotent(tmp_path, monkeypatch):
    calib = tmp_path / "calibration.db"
    analysis = tmp_path / "analysis.db"
    _make_analysis_db(analysis, [
        ("2026-07-02T17:00:00+00:00", "KPHX", 90.0, 91.0, 0.30),
    ])
    monkeypatch.setattr(calibration, "ANALYSIS_DB_PATH", analysis)
    c = _make_calib_db(calib)
    n1 = calibration._instrument_kalshi_bins(c, "KPHX", date(2026, 7, 2), 91.0)
    c.commit()
    n2 = calibration._instrument_kalshi_bins(c, "KPHX", date(2026, 7, 2), 91.0)
    c.commit()
    total = c.execute(
        "SELECT COUNT(*) FROM prediction_snapshots"
    ).fetchone()[0]
    assert n1 == 1
    assert n2 == 0
    assert total == 1


def test_instrument_handles_tail_bins(tmp_path, monkeypatch):
    calib = tmp_path / "calibration.db"
    analysis = tmp_path / "analysis.db"
    _make_analysis_db(analysis, [
        ("2026-07-02T17:00:00+00:00", "KPHX", float("-inf"), 90.0, 0.05),
        ("2026-07-02T17:00:00+00:00", "KPHX", 100.0, float("inf"), 0.10),
        ("2026-07-02T17:00:00+00:00", "KPHX", 95.0, 96.0, 0.40),
    ])
    monkeypatch.setattr(calibration, "ANALYSIS_DB_PATH", analysis)
    c = _make_calib_db(calib)
    n = calibration._instrument_kalshi_bins(c, "KPHX", date(2026, 7, 2),
                                            max_f=95.0)
    c.commit()
    assert n == 3
    rows = c.execute(
        "SELECT threshold, bin_half, outcome FROM prediction_snapshots "
        "WHERE station_id='KPHX' ORDER BY threshold"
    ).fetchall()
    # bottom tail → threshold=hi=90.0, bin_half=None, contains 95? no → 0
    # normal → threshold=lo=95, bin_half=0.5, contains 95? [94.5, 96.5) yes → 1
    # top tail → threshold=lo=100.0, bin_half=None, contains 95? no → 0
    assert rows[0] == (90.0, None, 0)
    assert rows[1] == (95.0, 0.5, 1)
    assert rows[2] == (100.0, None, 0)


def test_instrument_returns_zero_when_no_analysis_db(tmp_path, monkeypatch):
    calib = tmp_path / "calibration.db"
    monkeypatch.setattr(calibration, "ANALYSIS_DB_PATH",
                        tmp_path / "does_not_exist.db")
    c = _make_calib_db(calib)
    n = calibration._instrument_kalshi_bins(c, "KPHX", date(2026, 7, 2), 91.0)
    assert n == 0
