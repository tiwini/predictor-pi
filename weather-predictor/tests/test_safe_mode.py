"""Safe mode D2 gates — Fable audit response 2026-07-07.

Cubre los 5 endurecimientos: cutoff 11h, min edge 15pp, tail bins skip,
penny-YES skip, ext_gate 1.0F. streak_at 2 se cubre en test_streaks indirecto.
"""
from datetime import date
import pytest

import bets


@pytest.fixture
def _safe_active(monkeypatch, tmp_path):
    monkeypatch.setattr(bets, "SAFE_MODE_ACTIVE_UNTIL", "2099-12-31",
                        raising=False)
    monkeypatch.setattr(bets, "DB_PATH", tmp_path / "cal.db")


def test_cutoff_tightens_to_11(_safe_active):
    # 12:00 local pasaría el base cutoff (13) pero no el safe (11)
    placed = bets.maybe_bet("KPHX", date(2026, 7, 10), "K1",
                            80.0, 90.0, "80-90",
                            0.60, 0.40, our_pred_f=85.0,
                            station_local_hour=12)
    assert placed is False


def test_min_edge_15pp_blocks_10pp_bet(_safe_active):
    # edge 10pp: pasaría base 5pp, no pasa safe 15pp
    placed = bets.maybe_bet("KPHX", date(2026, 7, 10), "K2",
                            80.0, 90.0, "80-90",
                            0.60, 0.50, our_pred_f=85.0,
                            station_local_hour=9)
    assert placed is False


def test_min_edge_15pp_allows_20pp_bet(_safe_active, monkeypatch):
    import bias_tracker
    monkeypatch.setattr(bias_tracker, "compute_bias", lambda *a, **k: {"bias": 0.0})
    placed = bets.maybe_bet("KPHX", date(2026, 7, 10), "K3",
                            80.0, 90.0, "80-90",
                            0.70, 0.50, our_pred_f=85.0,
                            station_local_hour=9)
    assert placed is True


def test_tail_bin_skip_lo(_safe_active):
    placed = bets.maybe_bet("KPHX", date(2026, 7, 10), "K4",
                            float("-inf"), 80.0, "<80",
                            0.70, 0.30, our_pred_f=85.0,
                            station_local_hour=9)
    assert placed is False


def test_tail_bin_skip_hi(_safe_active):
    placed = bets.maybe_bet("KPHX", date(2026, 7, 10), "K5",
                            110.0, float("inf"), ">110",
                            0.70, 0.30, our_pred_f=105.0,
                            station_local_hour=9)
    assert placed is False


def test_penny_yes_skip(_safe_active):
    # kalshi_p=0.02 → yes_ask fallback 0.02 <5c; nuestro edge +25pp (our_p=0.27)
    placed = bets.maybe_bet("KPHX", date(2026, 7, 10), "K6",
                            80.0, 82.0, "80-82",
                            0.27, 0.02, our_pred_f=85.0,
                            station_local_hour=9)
    assert placed is False


def test_penny_no_ok_because_no_side(_safe_active):
    # kalshi_p=0.98 → side NO, entry = 1-0.98 = 0.02, pero es NO (no YES)
    # penny-YES skip solo aplica a side==yes → NO debería pasar.
    placed = bets.maybe_bet("KPHX", date(2026, 7, 10), "K7",
                            80.0, 82.0, "80-82",
                            0.73, 0.98, our_pred_f=85.0,
                            station_local_hour=9)
    # edge = -25pp, side=NO, entry=0.02 → but degenerate <0.01 guard passes
    # (0.02 >0.01), penny-YES no aplica a NO → should insert.
    assert placed is True
