"""Tests de calibration sin red — usamos DB en tmp y price_fn fake."""
import os
import time

import pytest

import calibration as cal
import predictor as p


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "calib.db")


def _fake_pred(symbol="BTCUSDT", now_price=50_000.0, made_at=None,
               horizon_min=60.0):
    if made_at is None:
        made_at = time.time()
    pred = p.Prediction(
        symbol=symbol, now_price=now_price,
        sigma_1m=0.001, sigma_horizon=0.01,
        horizon_min=horizon_min, n_candles=500,
        fetched_at=made_at,
        target_at=made_at + horizon_min * 60,
    )
    return pred


def test_init_db_creates_tables(db):
    cal.init_db(db)
    import sqlite3
    c = sqlite3.connect(db)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "predictions" in tables
    assert "outcomes" in tables


def test_record_and_settle_round_trip(db):
    cal.init_db(db)
    pred = _fake_pred(now_price=50_000.0, made_at=time.time() - 3700)
    ladder = p.threshold_ladder(pred, n=3, step_pct=0.005)
    pid = cal.record_prediction(pred, ladder, db_path=db)
    assert pid > 0

    # Actual settled at 50_500 → above any threshold ≤ 50_500
    n = cal.settle_due(db_path=db, price_fn=lambda s, t: 50_500.0)
    assert n == 1

    # Idempotent: ya tiene outcome
    n2 = cal.settle_due(db_path=db, price_fn=lambda s, t: 50_500.0)
    assert n2 == 0


def test_settle_skips_unripe_predictions(db):
    cal.init_db(db)
    pred = _fake_pred(made_at=time.time() - 60)  # only 1 min old, horizon 60
    ladder = p.threshold_ladder(pred, n=3, step_pct=0.005)
    cal.record_prediction(pred, ladder, db_path=db)
    assert cal.settle_due(db_path=db, price_fn=lambda s, t: 50_500.0) == 0


def test_reliability_perfect_predictor(db):
    """Si actual_price = now_price exacto, los thresholds < now tienen
    p_above ≈ 1 y outcome=1; los > now tienen p_above ≈ 0 y outcome=0.
    Brier debería ser muy bajo."""
    cal.init_db(db)
    for _ in range(20):
        pred = _fake_pred(made_at=time.time() - 3700)
        ladder = p.threshold_ladder(pred, n=10, step_pct=0.01)
        cal.record_prediction(pred, ladder, db_path=db)
    cal.settle_due(db_path=db, price_fn=lambda s, t: 50_000.0)

    brier = cal.overall_brier(db_path=db)
    assert brier is not None
    # Random binario daría 0.25; un modelo decente << 0.25
    assert brier < 0.2


def test_reliability_buckets_have_data(db):
    cal.init_db(db)
    for _ in range(5):
        pred = _fake_pred(made_at=time.time() - 3700)
        ladder = p.threshold_ladder(pred, n=10, step_pct=0.005)
        cal.record_prediction(pred, ladder, db_path=db)
    cal.settle_due(db_path=db, price_fn=lambda s, t: 50_100.0)

    stats = cal.reliability(db_path=db)
    assert len(stats) == len(cal.BUCKETS)
    total_n = sum(s.n for s in stats)
    assert total_n > 0


def test_recent_outcomes_picks_earliest_pred(db):
    """Para validar capacidad predictiva, usamos la pred más TEMPRANA (lead alto)."""
    cal.init_db(db)
    base = time.time() - 3700
    target = base + 3600
    # Pred temprana (lead 60min) — debería ganar
    early = _fake_pred(now_price=50_000.0, made_at=base)
    early.target_at = target
    cal.record_prediction(early, p.threshold_ladder(early, n=5, step_pct=0.005),
                          db_path=db)
    # Pred tardía (lead 2min)
    late = _fake_pred(now_price=50_400.0, made_at=base + 3480)
    late.target_at = target
    cal.record_prediction(late, p.threshold_ladder(late, n=5, step_pct=0.005),
                          db_path=db)
    cal.settle_due(db_path=db, now=base + 3700,
                   price_fn=lambda s, t: 50_500.0)

    res = cal.recent_outcomes(db_path=db)
    assert len(res) == 1
    h = res[0]
    assert h.pred_price == 50_000.0   # ganó la temprana
    assert h.actual_price == 50_500.0
    assert h.lead_min == pytest.approx(60.0, abs=0.5)
    assert h.diff_pct == pytest.approx(500/50000*100, abs=0.01)
    assert h.z_actual > 0


def test_history_for_target(db):
    cal.init_db(db)
    base = time.time() - 3700
    target = base + 3600
    # 3 predicciones para mismo target
    for offset, price in [(0, 50_000), (1800, 50_200), (3500, 50_400)]:
        pred = _fake_pred(now_price=price, made_at=base + offset)
        pred.target_at = target
        cal.record_prediction(pred,
                              p.threshold_ladder(pred, n=5, step_pct=0.005),
                              db_path=db)
    cal.settle_due(db_path=db, now=base + 3700,
                   price_fn=lambda s, t: 50_500.0)

    h = cal.history_for_target("BTCUSDT", target, db_path=db)
    assert h["actual_price"] == 50_500.0
    assert len(h["rows"]) == 3
    # Orden ascendente por made_at
    leads = [r["lead_min"] for r in h["rows"]]
    assert leads == sorted(leads, reverse=True)
    # P(≥actual) presente en cada row
    for r in h["rows"]:
        assert "p_above_actual" in r


def test_recent_outcomes_empty(db):
    cal.init_db(db)
    assert cal.recent_outcomes(db_path=db) == []


def test_overall_brier_none_when_empty(db):
    cal.init_db(db)
    assert cal.overall_brier(db_path=db) is None


def test_settle_groups_by_symbol_and_target(db):
    """price_fn called once per unique (symbol, target_at)."""
    cal.init_db(db)
    base = time.time() - 3700
    # 2 BTC mismo target, 1 BTC otro target, 1 ETH
    for made_at in [base, base + 5]:  # mismo target_at (made_at+3600)
        pred = _fake_pred(symbol="BTCUSDT", made_at=made_at)
        # Forzar mismo target_at para los dos
        pred.target_at = base + 3600
        ladder = p.threshold_ladder(pred, n=2, step_pct=0.005)
        cal.record_prediction(pred, ladder, db_path=db)
    pred = _fake_pred(symbol="BTCUSDT", made_at=base + 100)
    pred.target_at = base + 7200  # diferente target
    cal.record_prediction(pred, p.threshold_ladder(pred, n=2, step_pct=0.005),
                          db_path=db)
    pred = _fake_pred(symbol="ETHUSDT", made_at=base)
    pred.target_at = base + 3600
    cal.record_prediction(pred, p.threshold_ladder(pred, n=2, step_pct=0.005),
                          db_path=db)

    calls = []
    def fake(sym, tgt):
        calls.append((sym, tgt))
        return 50_000.0

    cal.settle_due(db_path=db, now=base + 7300, price_fn=fake)
    # 3 grupos únicos: (BTC, base+3600), (BTC, base+7200), (ETH, base+3600)
    assert len(calls) == 3
    assert ("BTCUSDT", base + 3600) in calls
    assert ("BTCUSDT", base + 7200) in calls
    assert ("ETHUSDT", base + 3600) in calls
