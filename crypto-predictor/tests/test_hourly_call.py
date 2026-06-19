"""Tests del módulo hourly_call. Sin red — DB en tmp y Kalshi/price stubeado."""
import time

import pytest

import hourly_call as hc
import kalshi as _kalshi
import predictor as p


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "hcalls.db")
    hc.init_db(path)
    return path


@pytest.fixture(autouse=True)
def stub_kalshi(monkeypatch):
    """Por defecto Kalshi devuelve None (sin red). Tests específicos lo overridean."""
    monkeypatch.setattr(_kalshi, "nearest_strike", lambda t, v: None)
    monkeypatch.setattr(_kalshi, "implied_above", lambda t, v: None)


def _fake_pred(now_price=80_000.0, made_at=None, horizon_min=60.0,
               sigma_h=0.01):
    if made_at is None:
        made_at = time.time()
    return p.Prediction(
        symbol="BTCUSDT", now_price=now_price,
        sigma_1m=sigma_h / (horizon_min ** 0.5),
        sigma_horizon=sigma_h, horizon_min=horizon_min,
        n_candles=500, fetched_at=made_at,
        target_at=made_at + horizon_min * 60,
    )


def test_init_db_creates_table(db):
    import sqlite3
    c = sqlite3.connect(db)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "hourly_calls" in tables


def test_make_call_inserts_row_with_p70(db):
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    cid = hc.make_call(pred, db_path=db)
    assert cid is not None
    # call_value debería ser p70: now_price * exp(z70 * sigma_h), z70 > 0
    cur = hc.current_call(db_path=db)
    assert cur.call_value > pred.now_price
    assert cur.quantile == 0.70


def test_make_call_dedupe_per_target_at(db):
    pred = _fake_pred()
    a = hc.make_call(pred, db_path=db)
    b = hc.make_call(pred, db_path=db)  # mismo target_at
    assert a is not None
    assert b is None  # IntegrityError → None


def test_make_call_rejects_short_horizon(db):
    pred = _fake_pred(horizon_min=10.0)  # < MIN_HORIZON_MIN (55)
    cid = hc.make_call(pred, db_path=db)
    assert cid is None


def test_make_call_rejects_non_btc(db):
    pred = _fake_pred()
    pred.symbol = "ETHUSDT"
    assert hc.make_call(pred, db_path=db) is None


def test_settle_win_when_actual_below_call(db):
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    n = hc.settle_due(db_path=db, now=pred.target_at + 1,
                      price_fn=lambda s, t: 79_500.0)
    assert n == 1
    cur = hc.recent(db_path=db)[0]
    assert cur.won == 1
    assert cur.actual_price == 79_500.0


def test_settle_loss_when_actual_above_call(db):
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    cur_before = hc.current_call(db_path=db)
    # actual sobrepasa el call_value (p70 está por encima de now_price ~+0.5%)
    actual_above = cur_before.call_value + 100
    n = hc.settle_due(db_path=db, now=pred.target_at + 1,
                      price_fn=lambda s, t: actual_above)
    assert n == 1
    cur = hc.recent(db_path=db)[0]
    assert cur.won == 0


def test_settle_idempotent(db):
    pred = _fake_pred()
    hc.make_call(pred, db_path=db)
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: pred.now_price)
    n2 = hc.settle_due(db_path=db, now=pred.target_at + 1,
                       price_fn=lambda s, t: pred.now_price)
    assert n2 == 0


def test_settle_skips_unripe(db):
    pred = _fake_pred(made_at=time.time())  # target_at en futuro
    hc.make_call(pred, db_path=db)
    n = hc.settle_due(db_path=db, now=time.time(),
                      price_fn=lambda s, t: pred.now_price)
    assert n == 0


def test_streak_counts_consecutive_wins_from_latest(db):
    """3 wins seguidos, 1 loss antes de eso → streak=3."""
    base = time.time() - 7 * 3600
    outcomes = [1, 0, 1, 1, 1]  # cronológico viejo→nuevo: W,L,W,W,W
    for i, won in enumerate(outcomes):
        pred = _fake_pred(made_at=base + i * 3600)
        hc.make_call(pred, db_path=db)
        # Forzar outcome elegido: si won=1, actual ≤ call_value; si 0, actual >
        cur = hc.current_call(db_path=db)
        actual = cur.call_value - 1 if won else cur.call_value + 1
        hc.settle_due(db_path=db, now=cur.target_at + 1,
                      price_fn=lambda s, t, a=actual: a)
    assert hc.streak(db_path=db) == 3


def test_streak_zero_if_latest_is_loss(db):
    pred = _fake_pred()
    hc.make_call(pred, db_path=db)
    cur = hc.current_call(db_path=db)
    hc.settle_due(db_path=db, now=cur.target_at + 1,
                  price_fn=lambda s, t: cur.call_value + 1)
    assert hc.streak(db_path=db) == 0


def test_streak_zero_when_no_settled(db):
    assert hc.streak(db_path=db) == 0


def test_empirical_rate(db):
    """3 wins + 1 loss → rate = 0.75."""
    base = time.time() - 5 * 3600
    for i, won in enumerate([1, 1, 0, 1]):
        pred = _fake_pred(made_at=base + i * 3600)
        hc.make_call(pred, db_path=db)
        cur = hc.current_call(db_path=db)
        actual = cur.call_value - 1 if won else cur.call_value + 1
        hc.settle_due(db_path=db, now=cur.target_at + 1,
                      price_fn=lambda s, t, a=actual: a)
    e = hc.empirical_rate(db_path=db)
    assert e["n"] == 4
    assert e["wins"] == 3
    assert e["rate"] == 0.75


def test_empirical_rate_none_when_empty(db):
    e = hc.empirical_rate(db_path=db)
    assert e["n"] == 0
    assert e["rate"] is None


def test_kalshi_strike_and_edge_persisted(db, monkeypatch):
    """Si Kalshi devuelve strike, edge se calcula y persiste."""
    monkeypatch.setattr(_kalshi, "nearest_strike",
                        lambda t, v: (80_500.0, 0.40))  # mid YES = 0.40
    monkeypatch.setattr(_kalshi, "implied_above", lambda t, v: 0.30)
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    cur = hc.current_call(db_path=db)
    assert cur.kalshi_strike == 80_500.0
    assert cur.kalshi_no_at_strike == pytest.approx(0.60)
    # model_no_at_strike = 1 - prob_above(pred, 80500). 80500 está sobre
    # el precio actual con sigma_h=0.01, así que prob_above < 0.5.
    assert cur.model_no_at_strike is not None
    assert 0.0 < cur.model_no_at_strike < 1.0
    assert cur.edge_pp is not None
    assert cur.kalshi_no_at_call == pytest.approx(0.70)


def test_recent_orders_desc_by_target(db):
    base = time.time() - 5 * 3600
    for i in range(3):
        pred = _fake_pred(made_at=base + i * 3600)
        hc.make_call(pred, db_path=db)
    rows = hc.recent(db_path=db)
    assert len(rows) == 3
    targets = [r.target_at for r in rows]
    assert targets == sorted(targets, reverse=True)
