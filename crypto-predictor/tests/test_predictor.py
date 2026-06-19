"""Tests sin red — entradas directas a las funciones puras."""
import math

import pytest

import predictor as p


def test_log_returns_basic():
    closes = [100.0, 101.0, 100.0, 102.0]
    rets = p.log_returns(closes)
    assert len(rets) == 3
    assert rets[0] == pytest.approx(math.log(101 / 100))
    assert rets[1] == pytest.approx(math.log(100 / 101))


def test_ewma_sigma_zero_returns():
    rets = [0.0] * 50
    assert p.ewma_sigma(rets) == pytest.approx(0.0)


def test_ewma_sigma_constant_magnitude():
    # Si todos los |r| = c, la varianza converge a c² → σ ≈ c
    rets = [0.01] * 100
    sigma = p.ewma_sigma(rets)
    assert sigma == pytest.approx(0.01, abs=1e-6)


def test_ewma_sigma_too_few():
    assert p.ewma_sigma([0.01, -0.01]) == 0.0


def test_norm_cdf_known_points():
    assert p._norm_cdf(0.0) == pytest.approx(0.5)
    assert p._norm_cdf(1.96) == pytest.approx(0.975, abs=0.001)
    assert p._norm_cdf(-1.96) == pytest.approx(0.025, abs=0.001)


def test_inv_norm_round_trip():
    for q in [0.05, 0.1, 0.5, 0.9, 0.95, 0.99]:
        z = p._inv_norm(q)
        assert p._norm_cdf(z) == pytest.approx(q, abs=1e-4)


def test_prob_above_at_now_is_half():
    pred = p.Prediction(symbol="BTCUSDT", now_price=50_000.0,
                        sigma_1m=0.001, sigma_horizon=0.01,
                        horizon_min=60, n_candles=500)
    assert p.prob_above(pred, 50_000.0) == pytest.approx(0.5, abs=1e-6)


def test_prob_above_higher_threshold_lower_prob():
    pred = p.Prediction(symbol="BTCUSDT", now_price=50_000.0,
                        sigma_1m=0.001, sigma_horizon=0.01,
                        horizon_min=60, n_candles=500)
    p_50500 = p.prob_above(pred, 50_500.0)
    p_51000 = p.prob_above(pred, 51_000.0)
    assert 0 < p_51000 < p_50500 < 0.5


def test_prob_above_zero_sigma():
    pred = p.Prediction(symbol="BTCUSDT", now_price=50_000.0,
                        sigma_1m=0, sigma_horizon=0,
                        horizon_min=60, n_candles=500)
    assert p.prob_above(pred, 49_999.0) == 1.0
    assert p.prob_above(pred, 50_001.0) == 0.0


def test_quantile_median_is_now():
    pred = p.Prediction(symbol="BTCUSDT", now_price=50_000.0,
                        sigma_1m=0.001, sigma_horizon=0.01,
                        horizon_min=60, n_candles=500)
    assert p.quantile(pred, 0.5) == pytest.approx(50_000.0, rel=1e-4)


def test_quantile_monotone():
    pred = p.Prediction(symbol="BTCUSDT", now_price=50_000.0,
                        sigma_1m=0.001, sigma_horizon=0.02,
                        horizon_min=60, n_candles=500)
    qs = [p.quantile(pred, q) for q in [0.05, 0.25, 0.5, 0.75, 0.95]]
    assert qs == sorted(qs)


def test_next_clock_hour():
    # 14:23:45 UTC → 15:00:00
    t = 1_700_000_000  # arbitrario
    nxt = p.next_clock_hour(t)
    assert nxt > t
    assert nxt - t <= 3600
    assert nxt % 3600 == 0


def test_next_clock_hour_strict_at_boundary():
    # Si now es exactamente XX:00:00, target = XX+1:00:00
    aligned = 1_700_000_000 - (1_700_000_000 % 3600)
    nxt = p.next_clock_hour(aligned)
    assert nxt == aligned + 3600


def test_t4_cdf_known_points():
    # Mediana 0, simetría, asintotas
    assert p._t4_cdf(0.0) == pytest.approx(0.5)
    assert p._t4_cdf(10.0) == pytest.approx(1.0, abs=1e-3)
    assert p._t4_cdf(-10.0) == pytest.approx(0.0, abs=1e-3)
    # Simetría: F(-t) = 1 - F(t)
    for t in [0.5, 1.0, 2.5]:
        assert p._t4_cdf(-t) == pytest.approx(1 - p._t4_cdf(t), abs=1e-12)


def test_t4_inv_round_trip():
    for q in [0.05, 0.1, 0.5, 0.9, 0.95, 0.99]:
        t = p._t4_inv(q)
        assert p._t4_cdf(t) == pytest.approx(q, abs=1e-9)


def test_t4_has_fatter_tails_than_gaussian():
    # En la cola, P(T_4 > 3) > P(Z > 3) (variance-matched: igual σ pero más cola)
    pred = p.Prediction(symbol="BTCUSDT", now_price=100.0,
                        sigma_1m=0.001, sigma_horizon=0.10,
                        horizon_min=60, n_candles=500)
    # Threshold ~ 3σ por encima
    thr = 100.0 * math.exp(3 * 0.10)
    p_t = p.prob_above(pred, thr)
    # Comparación con gaussiana pura
    z = math.log(thr / 100.0) / 0.10
    p_gauss = 1 - p._norm_cdf(z)
    assert p_t > p_gauss
    # Ambas pequeñas pero t > gauss
    assert p_t < 0.05


def test_nice_step_per_symbol():
    # BTC ~$79k → $100
    assert p.nice_step(79_000) == 100
    # ETH ~$2.3k → $2 or $5
    assert p.nice_step(2_300) in (2, 5)
    # SOL ~$84 → $0.1 or $0.2
    assert p.nice_step(84) in (0.1, 0.2)
    # XRP ~$1.4 → $0.002
    assert p.nice_step(1.4) == pytest.approx(0.002, rel=1e-9)
    # DOGE ~$0.11 → $0.0001
    assert p.nice_step(0.11) == pytest.approx(0.0001, rel=1e-9)


def test_threshold_ladder_abs_centers_on_round_number():
    pred = p.Prediction(symbol="BTCUSDT", now_price=79_068.0,
                        sigma_1m=0.001, sigma_horizon=0.01,
                        horizon_min=60, n_candles=500)
    rows = p.threshold_ladder_abs(pred, n=3, step_abs=100)
    assert len(rows) == 7
    # Centro debe ser múltiplo de 100 cercano a 79_068 → 79_100
    center = [r for r in rows if r["is_center"]][0]
    assert center["threshold"] == 79_100
    # Pasos uniformes de 100
    thresholds = [r["threshold"] for r in rows]
    diffs = [thresholds[i+1] - thresholds[i] for i in range(len(thresholds)-1)]
    assert all(d == pytest.approx(100) for d in diffs)
    # P(>X) monotone decreasing
    probs = [r["p_above"] for r in rows]
    assert probs == sorted(probs, reverse=True)


def test_threshold_ladder_centered_at_now():
    pred = p.Prediction(symbol="BTCUSDT", now_price=50_000.0,
                        sigma_1m=0.001, sigma_horizon=0.01,
                        horizon_min=60, n_candles=500)
    rows = p.threshold_ladder(pred, n=5, step_pct=0.005)
    assert len(rows) == 11
    center = [r for r in rows if r["delta_pct"] == 0][0]
    assert center["threshold"] == pytest.approx(50_000.0)
    assert center["p_above"] == pytest.approx(0.5, abs=1e-6)
    # P(>X) is monotone decreasing as threshold rises
    probs = [r["p_above"] for r in rows]
    assert probs == sorted(probs, reverse=True)
