"""Tests for divergence detector."""
from datetime import date

import pytest

import divergence as dv


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dv, "DB_PATH", tmp_path / "calib.db")


def test_no_data_not_diverging():
    r = dv.detect("KBOS", date(2026, 5, 1))
    assert r["diverging"] is False
    assert r["offsets"] == {}


def test_single_offset_not_diverging():
    dv.record_band("KBOS", date(2026, 5, 1), 0, 50, 55, 60, 31)
    r = dv.detect("KBOS", date(2026, 5, 1))
    assert r["diverging"] is False
    assert 0 in r["offsets"]


def test_monotone_band_not_diverging():
    # Normal: D+0 tightest, D+2 widest
    dv.record_band("KBOS", date(2026, 5, 1), 0, 52, 55, 58, 31)  # spread 6
    dv.record_band("KBOS", date(2026, 5, 1), 1, 50, 55, 60, 31)  # spread 10
    dv.record_band("KBOS", date(2026, 5, 1), 2, 48, 55, 62, 31)  # spread 14
    r = dv.detect("KBOS", date(2026, 5, 1))
    assert r["diverging"] is False
    assert r["offsets"][0] == pytest.approx(6.0)
    assert r["offsets"][2] == pytest.approx(14.0)


def test_diverging_d0_wider_than_d1():
    # D+0 should be tighter; if it is wider, that's divergence
    dv.record_band("KBOS", date(2026, 5, 1), 0, 45, 55, 65, 31)  # spread 20
    dv.record_band("KBOS", date(2026, 5, 1), 1, 52, 55, 58, 31)  # spread 6
    r = dv.detect("KBOS", date(2026, 5, 1))
    assert r["diverging"] is True
    assert r["breaks"]
    assert "D+0" in r["message"]


def test_noise_below_tolerance_not_flagged():
    # Tiny inversion within noise tolerance — ignored
    dv.record_band("KBOS", date(2026, 5, 1), 0, 50, 55, 60.2, 31)  # spread 10.2
    dv.record_band("KBOS", date(2026, 5, 1), 1, 50, 55, 60.0, 31)  # spread 10
    r = dv.detect("KBOS", date(2026, 5, 1))
    assert r["diverging"] is False


def test_uses_most_recent_per_offset():
    # Old wrong record then correct new record — detector must use the new one
    dv.record_band("KBOS", date(2026, 5, 1), 0, 30, 55, 80, 31)  # old, spread 50
    dv.record_band("KBOS", date(2026, 5, 1), 0, 52, 55, 58, 31)  # new, spread 6
    dv.record_band("KBOS", date(2026, 5, 1), 1, 50, 55, 60, 31)  # spread 10
    r = dv.detect("KBOS", date(2026, 5, 1))
    assert r["diverging"] is False
    assert r["offsets"][0] == pytest.approx(6.0)


def test_record_skips_when_p10_none():
    dv.record_band("KBOS", date(2026, 5, 1), 0, None, None, None, 0)
    r = dv.detect("KBOS", date(2026, 5, 1))
    assert r["offsets"] == {}


def test_isolated_per_station():
    dv.record_band("KBOS", date(2026, 5, 1), 0, 45, 55, 65, 31)  # wide
    dv.record_band("KBOS", date(2026, 5, 1), 1, 52, 55, 58, 31)  # narrow
    dv.record_band("KPHX", date(2026, 5, 1), 0, 70, 80, 90, 31)
    dv.record_band("KPHX", date(2026, 5, 1), 1, 65, 80, 95, 31)
    r_bos = dv.detect("KBOS", date(2026, 5, 1))
    r_phx = dv.detect("KPHX", date(2026, 5, 1))
    assert r_bos["diverging"] is True
    assert r_phx["diverging"] is False
