"""Test posterior_shift_weight: rampa continua, guard de spread, bonus heat."""
import external_models as em


def test_shift_zero_when_diff_below_threshold():
    assert em.posterior_shift_weight(ext_diff=-1.0, ext_spread=2.0, clim_percentile=50) == 0.0
    assert em.posterior_shift_weight(ext_diff=1.4, ext_spread=2.0, clim_percentile=50) == 0.0


def test_shift_zero_when_spread_too_wide():
    # ext_diff -3.0 sería >cap pero spread > MAX_MODELS_SPREAD_F debe matar a 0
    w = em.posterior_shift_weight(ext_diff=-3.0, ext_spread=10.0, clim_percentile=50)
    assert w == 0.0


def test_shift_ramps_continuously():
    # rampa 0.25/°F sobre umbral 1.5; ext_diff=-2.5 → exceso 1.0 → 0.25
    w = em.posterior_shift_weight(ext_diff=-2.5, ext_spread=2.0, clim_percentile=50)
    assert abs(w - 0.25) < 1e-6


def test_shift_caps_at_0_5():
    # ext_diff -5 da rampa 0.875 → cap 0.5
    w = em.posterior_shift_weight(ext_diff=-5.0, ext_spread=2.0, clim_percentile=50)
    assert w == 0.5


def test_heat_bonus_lowers_threshold_to_1_when_cold_under_heatwave():
    # ext_diff -1.2 normalmente daría 0 (bajo umbral 1.5), pero con p>=80 y
    # vamos fríos (ext_diff<0), umbral baja a 1.0 → exceso 0.2 → 0.05 + bonus 0.15 = 0.20
    w = em.posterior_shift_weight(ext_diff=-1.2, ext_spread=2.0, clim_percentile=85)
    assert abs(w - 0.20) < 1e-6


def test_heat_bonus_not_applied_when_hot_overshoot():
    # ext_diff +1.2 con p>=80: NO es "cold under heatwave", no aplica bonus
    # ext_diff 1.2 < 1.5 → w=0
    w = em.posterior_shift_weight(ext_diff=1.2, ext_spread=2.0, clim_percentile=85)
    assert w == 0.0


def test_none_inputs_safe():
    assert em.posterior_shift_weight(ext_diff=None, ext_spread=None, clim_percentile=None) == 0.0
