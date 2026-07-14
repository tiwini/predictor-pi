"""Tests para _vol_regime_ratio_ewma (predictor_web.py) — Fable R8-review.

Warm-up guard: seed con varianza muestral de primeros 30 r², mínimo 330
klines de historia (30 + 5×60 = 5 slow-half-lives). NULL antes que valor
envenenado.
"""
import math
import random

import predictor_web as pw
from predictor import Kline


def _fk(n, sigma=0.001, base=60_000.0, seed=42):
    """Genera n klines 1m consecutivas con vol constante σ."""
    rng = random.Random(seed)
    kls = []
    price = base
    t0 = 1_780_000_000_000  # ms epoch fijo para reproducibilidad
    for i in range(n):
        r = rng.gauss(0, sigma)
        price *= (1 + r)
        kls.append(Kline(open_time=t0 + i * 60_000, open=price,
                         high=price * 1.001, low=price * 0.999,
                         close=price, volume=1.0))
    return kls


def test_vol_regime_returns_none_below_min_klines():
    """Menos de 330 klines (30 warmup + 5×60 slow-hl) → None."""
    assert pw._vol_regime_ratio_ewma(_fk(100)) is None
    assert pw._vol_regime_ratio_ewma(_fk(329)) is None


def test_vol_regime_valid_with_constant_vol_returns_near_one():
    """Con σ constante ambos EWMAs convergen al mismo valor → ratio ≈ 1."""
    v = pw._vol_regime_ratio_ewma(_fk(400))
    assert v is not None
    assert 0.7 < v < 1.4  # ruido muestral aceptable


def test_vol_regime_seed_from_sample_variance_not_first_r2():
    """El seed debe usar la varianza muestral de los primeros 30 r², no el
    primer r². Un cambio de régimen tempano no debería sesgar drásticamente
    el slow (que es lo que el bug pre-fix hacía)."""
    # Ventana calmada 300 klines + spike final: slow debe estar cerca del
    # nivel calmado gracias al warm-up largo.
    quiet = _fk(400, sigma=0.0005, seed=1)
    v = pw._vol_regime_ratio_ewma(quiet)
    assert v is not None
    # Con σ constante en la ventana entera el ratio debe estar cerca de 1;
    # si el seed fuera el primer r² individual, el slow tendría alta
    # varianza y el ratio se dispararía.
    assert 0.6 < v < 1.6


def test_vol_regime_none_when_all_klines_have_zero_close():
    """Klines corruptas (close=0) → None sin excepción."""
    kls = _fk(400)
    for k in kls:
        k.close = 0.0
    assert pw._vol_regime_ratio_ewma(kls) is None
