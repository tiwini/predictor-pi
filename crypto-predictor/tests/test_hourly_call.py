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
    monkeypatch.setattr(_kalshi, "nearest_strike_with_reason",
                        lambda t, v: (None, "no_event"))
    monkeypatch.setattr(_kalshi, "curve_and_strike_with_reason",
                        lambda t, v: (None, None, "no_event"))
    monkeypatch.setattr(_kalshi, "implied_above", lambda t, v: None)


@pytest.fixture(autouse=True)
def stub_coinbase_proxy(monkeypatch):
    """Por defecto el proxy Coinbase falla (sin red). Los tests que necesitan
    validar proxy_price_at_settle pasan el fn explícito a settle_due."""
    def _fail(sym, tgt):
        raise ValueError("network stubbed")
    monkeypatch.setattr(hc, "_coinbase_price_at", _fail)


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


def _reset_retry_gate():
    """Reset del module-level throttle antes de cada test que ejerce el
    retry pass, para que la ventana de 5 min no interfiera entre asserts."""
    hc._last_retry_scan_ts = 0.0


def _mvf(cb=None, bs=None, kr=None, gm=None):
    """Multi-venue fn stub: devuelve los precios pasados como fijos."""
    def _fn(sym, tgt, per_timeout=5.0, budget_s=15.0, skip=None):
        out = {"coinbase": cb, "bitstamp": bs, "kraken": kr, "gemini": gm}
        if skip:
            for k in skip:
                out[k] = None
        return out
    return _fn


def test_settle_persists_proxy_price_when_fn_ok(db):
    """proxy_price_at_settle se puebla con Coinbase del multi_venue_fn."""
    _reset_retry_gate()
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=79_498.5, bs=79_497.0,
                                      kr=79_499.0, gm=79_500.5))
    cur = hc.recent(db_path=db)[0]
    assert cur.actual_price == 79_500.0
    assert cur.proxy_price_at_settle == 79_498.5
    assert cur.bitstamp_price_at_settle == 79_497.0
    assert cur.brti_proxy_n_venues == 4


def test_settle_proxy_none_when_all_venues_fail_does_not_block_settle(db):
    """multi_venue_fn que devuelve todo None no debe bloquear el settle."""
    _reset_retry_gate()
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    n = hc.settle_due(db_path=db, now=pred.target_at + 1,
                      price_fn=lambda s, t: 79_500.0,
                      multi_venue_fn=_mvf())
    assert n == 1
    cur = hc.recent(db_path=db)[0]
    assert cur.actual_price == 79_500.0
    assert cur.proxy_price_at_settle is None
    assert cur.brti_proxy_price is None
    assert cur.brti_proxy_n_venues == 0


def test_settle_retry_ignores_old_null_proxy_rows(db):
    """Rows con settled_at > 3600s atrás NO se reintentan (evita hammer
    sobre históricas N=953 pre-instrumentación)."""
    _reset_retry_gate()
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: 79_500.0, multi_venue_fn=_mvf())
    _reset_retry_gate()
    calls = []
    def _track(s, t, per_timeout=5.0, budget_s=15.0, skip=None):
        calls.append(t)
        return _mvf(cb=79_498.5)(s, t, skip=skip)
    hc.settle_due(db_path=db, now=pred.target_at + 7200,
                  price_fn=lambda s, t: 79_500.0, multi_venue_fn=_track)
    assert calls == []
    cur = hc.recent(db_path=db)[0]
    assert cur.proxy_price_at_settle is None


# ---- Regression tests Fable R8-review 2026-07-09 ----

def test_r7_retry_bug_immortalized_coinbase_rescue_when_brti_populated(db):
    """R7 bug: row settled con brti poblado pero Coinbase NULL (Coinbase
    falló en settle window; Bitstamp+Kraken+Gemini rescataron mediana).
    El retry pre-fix ignoraba estas rows por el filtro `brti IS NULL`.
    Post-fix (R7-review) las alcanza y rellena Coinbase cuando recupera."""
    _reset_retry_gate()
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    # Settle inicial: Coinbase falla, otros 3 responden → brti n=3
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=None, bs=79_497.0,
                                      kr=79_499.0, gm=79_500.5))
    cur = hc.recent(db_path=db)[0]
    assert cur.proxy_price_at_settle is None
    assert cur.brti_proxy_price is not None
    assert cur.brti_proxy_n_venues == 3
    # 5 min después: Coinbase recupera. Retry rellena proxy_price sin pisar mediana.
    prev_brti = cur.brti_proxy_price
    _reset_retry_gate()
    hc.settle_due(db_path=db, now=pred.target_at + 301,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=79_498.5, bs=79_497.0,
                                      kr=79_499.0, gm=79_500.5))
    cur = hc.recent(db_path=db)[0]
    assert cur.proxy_price_at_settle == 79_498.5
    # n=3 pero mediana intacta (no upgrade porque combined saved+new = 4 pero
    # el cur_n=3, cb es nuevo → n_new sí es 4, upgrade dispara).
    # Actualmente cur_n=3, combined=cb(new)+bs(saved)+kraken(refetch)+gemini(refetch)=4
    assert cur.brti_proxy_n_venues == 4


def test_upgrade_n3_to_n4_recomputes_median(db):
    """Row con n=3 dentro de la ventana upgrade y venue extra respondiendo
    → mediana se recalcula desde el conjunto completo (saved+new)."""
    _reset_retry_gate()
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    # Settle: Gemini timeout, n=3 con cb+bs+kraken
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=100.0, bs=200.0, kr=300.0, gm=None))
    cur = hc.recent(db_path=db)[0]
    assert cur.brti_proxy_n_venues == 3
    assert cur.brti_proxy_price == 200.0  # median(100,200,300)
    # Retry 5 min después: Gemini responde. Mediana recompuesta con 4 venues.
    _reset_retry_gate()
    hc.settle_due(db_path=db, now=pred.target_at + 301,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=100.0, bs=200.0, kr=300.0, gm=400.0))
    cur = hc.recent(db_path=db)[0]
    assert cur.brti_proxy_n_venues == 4
    assert cur.brti_proxy_price == 250.0  # median(100,200,300,400)


def test_upgrade_no_new_venues_does_not_touch_median(db):
    """n_new ≤ cur_n: mediana existente intacta (no pisar)."""
    _reset_retry_gate()
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=100.0, bs=200.0, kr=300.0, gm=None))
    cur = hc.recent(db_path=db)[0]
    saved_median = cur.brti_proxy_price
    saved_n = cur.brti_proxy_n_venues
    # Retry: Kraken tampoco responde esta vez. combined = saved∪new = 3, cur_n=3.
    _reset_retry_gate()
    hc.settle_due(db_path=db, now=pred.target_at + 301,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=None, bs=None, kr=None, gm=None))
    cur = hc.recent(db_path=db)[0]
    assert cur.brti_proxy_price == saved_median
    assert cur.brti_proxy_n_venues == saved_n


def test_retry_gate_suppresses_consecutive_scans(db):
    """Módulo-level _last_retry_scan_ts: dos settle_due consecutivos dentro
    de 5 min NO deben llamar a multi_venue_fn en el retry pass. Fable R8:
    con POLL_SEC=5, sin gate serían 60 refetches en 5 min por row."""
    _reset_retry_gate()
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=None, bs=79_497.0,
                                      kr=79_499.0, gm=79_500.5))
    cur = hc.recent(db_path=db)[0]
    assert cur.proxy_price_at_settle is None
    calls = []
    def _tracked(sym, tgt, per_timeout=5.0, budget_s=15.0, skip=None):
        calls.append(tgt)
        return _mvf(cb=79_498.5, bs=79_497.0, kr=79_499.0, gm=79_500.5)(
            sym, tgt, skip=skip)
    # Segundo scan 30s después (dentro del gate 300s) — no debe correr retry.
    hc.settle_due(db_path=db, now=pred.target_at + 31,
                  price_fn=lambda s, t: 79_500.0, multi_venue_fn=_tracked)
    assert calls == []  # gate suprimió el retry pass
    cur = hc.recent(db_path=db)[0]
    assert cur.proxy_price_at_settle is None
    # Tercer scan 5 min después — gate expira, retry corre.
    hc.settle_due(db_path=db, now=pred.target_at + 301,
                  price_fn=lambda s, t: 79_500.0, multi_venue_fn=_tracked)
    assert len(calls) == 1
    cur = hc.recent(db_path=db)[0]
    assert cur.proxy_price_at_settle == 79_498.5


def test_retry_skip_populated_venues_avoids_refetch(db):
    """El retry pasa `skip=` a multi_venue_fn con los venues ya poblados
    (Coinbase + Bitstamp cuando persistieron) para no gastar requests
    deterministas en candles históricos."""
    _reset_retry_gate()
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    # Settle: cb+bs OK, kr+gm None → n=2, brti = median(cb,bs)
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: 79_500.0,
                  multi_venue_fn=_mvf(cb=100.0, bs=200.0))
    seen_skips = []
    def _tracker(sym, tgt, per_timeout=5.0, budget_s=15.0, skip=None):
        seen_skips.append(set(skip or ()))
        return _mvf(cb=None, bs=None, kr=300.0, gm=400.0)(
            sym, tgt, skip=skip)
    _reset_retry_gate()
    hc.settle_due(db_path=db, now=pred.target_at + 301,
                  price_fn=lambda s, t: 79_500.0, multi_venue_fn=_tracker)
    assert seen_skips == [{"coinbase", "bitstamp"}]
    cur = hc.recent(db_path=db)[0]
    assert cur.brti_proxy_n_venues == 4  # 2 saved + 2 fetched
    assert cur.brti_proxy_price == 250.0  # median(100, 200, 300, 400)


def test_settle_persists_z_standardized_log_return(db):
    """z = log(actual/now)/σ_h — fable 2026-07-04 outcome continuo bajo σ."""
    import math
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    hc.settle_due(db_path=db, now=pred.target_at + 1,
                  price_fn=lambda s, t: 79_500.0)
    cur = hc.recent(db_path=db)[0]
    expected_z = math.log(79_500.0 / 80_000.0) / 0.01
    assert cur.z is not None
    assert abs(cur.z - expected_z) < 1e-9


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
    """Si Kalshi devuelve strike, edge se calcula, persiste y curve serializa
    con bids/asks (fable dark data #4, 2026-07-05)."""
    strikes = [79_000.0, 79_500.0, 80_000.0, 80_500.0, 81_000.0]
    mids = [0.85, 0.65, 0.50, 0.40, 0.20]
    bids = [0.83, 0.63, 0.48, 0.38, None]
    asks = [0.87, 0.67, 0.52, 0.42, 0.20]
    monkeypatch.setattr(
        _kalshi, "curve_and_strike_with_reason",
        lambda t, v: ((80_500.0, 0.40),
                      (strikes, mids, bids, asks), None))
    monkeypatch.setattr(_kalshi, "implied_above", lambda t, v: 0.30)
    pred = _fake_pred(now_price=80_000.0, sigma_h=0.01)
    hc.make_call(pred, db_path=db)
    cur = hc.current_call(db_path=db)
    assert cur.kalshi_strike == 80_500.0
    assert cur.kalshi_no_at_strike == pytest.approx(0.60)
    assert cur.model_no_at_strike is not None
    assert 0.0 < cur.model_no_at_strike < 1.0
    assert cur.edge_pp is not None
    assert cur.kalshi_no_at_call == pytest.approx(0.70)
    assert cur.kalshi_null_reason is None
    import json
    curve = json.loads(cur.kalshi_curve_json)
    assert curve["s"] == strikes
    assert curve["m"] == mids
    assert curve["b"] == bids
    assert curve["a"] == asks


@pytest.mark.parametrize("reason", ["no_event", "events_error",
                                    "markets_error", "empty_curve"])
def test_kalshi_null_reason_persisted(db, monkeypatch, reason):
    """Cuando curve_and_strike_with_reason devuelve (None, None, reason),
    la razón queda registrada y la curva queda NULL."""
    monkeypatch.setattr(_kalshi, "curve_and_strike_with_reason",
                        lambda t, v: (None, None, reason))
    pred = _fake_pred()
    hc.make_call(pred, db_path=db)
    cur = hc.current_call(db_path=db)
    assert cur.kalshi_strike is None
    assert cur.kalshi_null_reason == reason
    assert cur.kalshi_curve_json is None


def test_kalshi_null_reason_captures_unhandled_exception(db, monkeypatch):
    """Si curve_and_strike_with_reason levanta, reason etiqueta el tipo."""
    def _boom(t, v):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(_kalshi, "curve_and_strike_with_reason", _boom)
    pred = _fake_pred()
    hc.make_call(pred, db_path=db)
    cur = hc.current_call(db_path=db)
    assert cur.kalshi_strike is None
    assert cur.kalshi_null_reason == "unhandled:RuntimeError"
    assert cur.kalshi_curve_json is None


def test_recent_orders_desc_by_target(db):
    base = time.time() - 5 * 3600
    for i in range(3):
        pred = _fake_pred(made_at=base + i * 3600)
        hc.make_call(pred, db_path=db)
    rows = hc.recent(db_path=db)
    assert len(rows) == 3
    targets = [r.target_at for r in rows]
    assert targets == sorted(targets, reverse=True)
