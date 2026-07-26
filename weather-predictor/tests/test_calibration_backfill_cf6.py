"""Tests del backfill CF6: escribe settles que perdieron la ventana del CLI sin
tocar la calibración (no instrumenta pares ni refitea isotónica).
"""
import sqlite3
from datetime import date
from zoneinfo import ZoneInfo

import calibration


class _St:
    """Lo mínimo que usan settle/backfill: id y tz."""
    def __init__(self, sid="KPHX", tzname="America/Phoenix"):
        self.id = sid
        self.tz = ZoneInfo(tzname)


def _prep(tmp_path, monkeypatch, kalshi_rows=()):
    calib = tmp_path / "calibration.db"
    analysis = tmp_path / "analysis.db"
    monkeypatch.setattr(calibration, "DB_PATH", calib)
    monkeypatch.setattr(calibration, "ANALYSIS_DB_PATH", analysis)
    c = sqlite3.connect(analysis)
    c.executescript("""
        CREATE TABLE kalshi_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, station TEXT NOT NULL, ticker TEXT NOT NULL,
            bin_lo REAL NOT NULL, bin_hi REAL NOT NULL, label TEXT,
            yes_mid REAL, our_p REAL, our_p_calibrated REAL);
    """)
    for ts, stn, lo, hi, p in kalshi_rows:
        c.execute("""INSERT INTO kalshi_snapshots
                     (ts, station, ticker, bin_lo, bin_hi, our_p)
                     VALUES (?, ?, 'T', ?, ?, ?)""", (ts, stn, lo, hi, p))
    c.commit()
    c.close()
    calibration._conn().close()      # crea el schema de calibration.db
    return calib


def test_record_settle_persists_source(tmp_path, monkeypatch):
    calib = _prep(tmp_path, monkeypatch)
    calibration._record_settle(_St(), date(2026, 7, 24), 117.0, 97.0, "cf6",
                               instrument=False)
    c = sqlite3.connect(calib)
    row = c.execute("SELECT max_obs_f, min_obs_f, source FROM day_outcomes "
                    "WHERE station_id='KPHX' AND date='2026-07-24'").fetchone()
    c.close()
    assert row == (117.0, 97.0, "cf6")


def test_instrument_false_writes_no_isotonic_pairs(tmp_path, monkeypatch):
    """El settle entra, los pares de calibración no. Rellenar datos no es
    cambiar el modelo."""
    calib = _prep(tmp_path, monkeypatch, kalshi_rows=[
        ("2026-07-24T17:00:00+00:00", "KPHX", 116.0, 117.0, 0.30),
        ("2026-07-24T17:00:00+00:00", "KPHX", 118.0, 119.0, 0.10),
    ])
    calibration._record_settle(_St(), date(2026, 7, 24), 117.0, 97.0, "cf6",
                               instrument=False)
    c = sqlite3.connect(calib)
    n = c.execute("SELECT COUNT(*) FROM prediction_snapshots "
                  "WHERE station_id='KPHX' AND op='b'").fetchone()[0]
    c.close()
    assert n == 0


def test_instrument_true_writes_pairs(tmp_path, monkeypatch):
    calib = _prep(tmp_path, monkeypatch, kalshi_rows=[
        ("2026-07-24T17:00:00+00:00", "KPHX", 116.0, 117.0, 0.30),
        ("2026-07-24T17:00:00+00:00", "KPHX", 118.0, 119.0, 0.10),
    ])
    calibration._record_settle(_St(), date(2026, 7, 24), 117.0, 97.0, "cli",
                               instrument=True)
    c = sqlite3.connect(calib)
    rows = c.execute("SELECT threshold, outcome FROM prediction_snapshots "
                     "WHERE station_id='KPHX' AND op='b' "
                     "ORDER BY threshold").fetchall()
    c.close()
    assert rows == [(116.0, 1), (118.0, 0)]


def test_backfill_skips_existing_and_future(tmp_path, monkeypatch):
    """Sólo escribe días ausentes y ya cerrados: no sobrescribe un settle de CLI
    con el prelim del F-6, y no toma el día en curso."""
    calib = _prep(tmp_path, monkeypatch)
    st = _St()
    calibration._record_settle(st, date(2026, 7, 24), 117.0, 97.0, "cli")
    monkeypatch.setattr(calibration.nws_cli, "fetch_month_extremes",
                        lambda sid, y, m: {
                            "2026-07-23": (114.0, 92.0),   # falta -> escribe
                            "2026-07-24": (110.0, 90.0),   # ya está en cli
                            "2026-07-26": (118.0, 95.0),   # 'hoy' -> se salta
                        })

    class _FixedDT(calibration.datetime):
        @classmethod
        def now(cls, tz=None):
            return calibration.datetime(2026, 7, 26, 9, 0, tzinfo=tz)

    monkeypatch.setattr(calibration, "datetime", _FixedDT)
    written = calibration.backfill_month_cf6(st, 2026, 7)
    assert [d.isoformat() for d, _ in written] == ["2026-07-23"]

    c = sqlite3.connect(calib)
    rows = dict(c.execute("SELECT date, max_obs_f FROM day_outcomes "
                          "WHERE station_id='KPHX'").fetchall())
    c.close()
    assert rows["2026-07-24"] == 117.0     # el CLI sobrevive intacto
    assert rows["2026-07-23"] == 114.0
    assert "2026-07-26" not in rows
