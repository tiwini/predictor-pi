"""Tests for the regime-break alert trigger logic in predictor_web.

We exercise the decision tree by stubbing `notify` to capture calls, without
touching HTTP/ntfy or the database.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import predictor_web as pw


def _snap(breaks, eff_n):
    return SimpleNamespace(
        regime_break_hours=breaks,
        ensemble_eff_n=eff_n,
        station_local=datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc),
    )


def _station():
    return SimpleNamespace(id="KBOS")


def _call(snap):
    captured = {}

    class FakeNotify:
        @staticmethod
        def enabled():
            return True

        @staticmethod
        def alert_regime_break(station_id, target_date, break_hours,
                               eff_n=None, reason="p1-p99"):
            captured.update({
                "station_id": station_id,
                "break_hours": list(break_hours),
                "eff_n": eff_n,
                "reason": reason,
            })

    with patch.dict("sys.modules", {"notify": FakeNotify}):
        pw._check_regime_alerts(snap, _station())
    return captured


def test_two_breaks_triggers_p1_p99_reason():
    c = _call(_snap(breaks=[6, 7], eff_n=5.0))
    assert c["reason"] == "p1-p99"
    assert c["break_hours"] == [6, 7]


def test_one_break_with_low_eff_n_triggers_combo():
    # The KBOS 2026-04-24 case: eff_n=1.8, 1 break at hour 17.
    c = _call(_snap(breaks=[17], eff_n=1.8))
    assert c["reason"] == "combo"
    assert c["eff_n"] == 1.8


def test_one_break_with_healthy_eff_n_does_not_trigger():
    c = _call(_snap(breaks=[17], eff_n=8.0))
    assert c == {}


def test_no_breaks_but_eff_n_very_low_triggers():
    c = _call(_snap(breaks=[], eff_n=1.5))
    assert c["reason"] == "eff_n_low"


def test_no_breaks_and_eff_n_moderate_does_not_trigger():
    c = _call(_snap(breaks=[], eff_n=2.5))
    assert c == {}


def test_combo_threshold_is_strict_at_3():
    # eff_n == 3 exactly should NOT trigger combo (we want < 3)
    c = _call(_snap(breaks=[12], eff_n=3.0))
    assert c == {}


def test_eff_n_low_threshold_is_strict_at_2():
    c = _call(_snap(breaks=[], eff_n=2.0))
    assert c == {}


def test_eff_n_none_safely_ignored():
    # Early in day before any obs, eff_n may be None.
    c = _call(_snap(breaks=[6], eff_n=None))
    assert c == {}
