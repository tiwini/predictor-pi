"""Tests del external anchor blend en external_models.py.

Mira `blend_with_external`, `anchor_weight`, `external_gaussian_p_bin`. La motivación
y rationale viven en external_models.py (constantes ANCHOR_*)."""
import math

import external_models as em


def test_gaussian_p_bin_centered():
    # Bin [89,90] se expande a [88.5, 90.5] por redondeo NWS (±0.5°F).
    # σ derived: max(1.5, 3/2.5)=1.5 → bin 2°F ancho centrado en 90 → ~47%
    p = em.external_gaussian_p_bin(90.0, 3.0, 89.0, 90.0)
    assert 0.40 <= p <= 0.55


def test_gaussian_p_bin_cold_tail():
    # ext_med=90, bin ≤89: prob below 89.5 with σ=1.5 → ~37%
    p = em.external_gaussian_p_bin(90.0, 3.0, float("-inf"), 89.0)
    assert 0.30 <= p <= 0.44


def test_gaussian_p_bin_hot_tail():
    # ext_med=90, bin ≥92: prob above 91.5 with σ=1.5 → ~16%
    p = em.external_gaussian_p_bin(90.0, 3.0, 92.0, float("inf"))
    assert 0.10 <= p <= 0.22


def test_gaussian_uses_sigma_floor():
    # Aun con spread=0 (todos los modelos coinciden), σ floor=1.5 evita
    # colapso a prob 0/1. Bin [89,90] expandido → ~47% como el caso normal.
    p = em.external_gaussian_p_bin(90.0, 0.0, 89.0, 90.0)
    assert 0.40 <= p <= 0.55


def test_anchor_weight_zero_below_thresholds():
    # |ext_diff|<1.5 → no anchor
    assert em.anchor_weight(-0.5) == 0.0
    assert em.anchor_weight(None) == 0.0
    assert em.anchor_weight(1.4) == 0.0


def test_anchor_weight_grows_with_discrepancy():
    w_small = em.anchor_weight(-2.0)
    w_large = em.anchor_weight(-4.0)
    assert w_small > 0
    assert w_large > w_small


def test_anchor_weight_capped():
    # No matter how extreme, never exceed cap
    assert em.anchor_weight(-100.0) == em.ANCHOR_WEIGHT_CAP
    assert em.anchor_weight(50.0) == em.ANCHOR_WEIGHT_CAP


def test_anchor_weight_combined_ceiling_with_lambda():
    # Si el shift ya consumió λ=0.3 del CAP=0.5, el blend tiene headroom 0.2
    w = em.anchor_weight(-10.0, lam=0.3)
    assert w == 0.20
    # λ ≥ CAP cierra el blend (shift ya saturó la influencia externa)
    assert em.anchor_weight(-10.0, lam=em.ANCHOR_WEIGHT_CAP) == 0.0
    assert em.anchor_weight(-10.0, lam=0.6) == 0.0


def test_blend_no_external_data_passthrough():
    # Sin ext_med devuelve our_p tal cual, weight=0
    p, w = em.blend_with_external(0.7, None, None, 89.0, 90.0, -3.0, 0.0)
    assert p == 0.7
    assert w == 0.0


def test_blend_zero_weight_passthrough():
    # Discrepancia chica: weight=0 → our_p sin cambios
    p, w = em.blend_with_external(0.7, 90.0, 2.0, 89.0, 90.0,
                                  ext_diff=-0.3, lam=0.0)
    assert p == 0.7
    assert w == 0.0


def test_blend_klas_today_scenario():
    """KLAS 2026-05-26: pred 87.6, ext_med 90.4. Bin [≤89] our 80%.
    Blend debe bajar la confianza (externos dan mucho menos) sin borrar el edge."""
    p, w = em.blend_with_external(0.80, 90.4, 2.4, float("-inf"), 89.0,
                                  ext_diff=-2.8, lam=0.0)
    assert 0.0 < w <= em.ANCHOR_WEIGHT_CAP
    # blended debe estar entre our_p y ext_p
    ext_p = em.external_gaussian_p_bin(90.4, 2.4, float("-inf"), 89.0)
    assert ext_p < p < 0.80


def test_blend_knyc_tail_negation_scenario():
    """KNYC 2026-05-25: ext_med ~75.6, bin [≤71] our 95% (cold tail muy lejos).
    Discrepancia chica → anchor weight 0 → blended igual a our_p."""
    p, w = em.blend_with_external(0.95, 75.6, 2.5, float("-inf"), 71.0,
                                  ext_diff=-0.5, lam=0.0)
    assert w == 0.0  # discrepancia chica
    assert p == 0.95


def test_blend_result_in_unit_interval():
    # Edge cases con our_p extremo
    for op in (0.0, 0.01, 0.5, 0.99, 1.0):
        p, _ = em.blend_with_external(op, 90.0, 3.0, 88.0, 89.0,
                                      ext_diff=-3.0, lam=0.0)
        assert 0.0 <= p <= 1.0


def test_blend_combined_with_shift_respects_cap():
    """Con λ=0.3 ya aplicado al shift, el blend solo añade hasta 0.2 más
    (total ≤ ANCHOR_WEIGHT_CAP=0.5)."""
    _, w = em.blend_with_external(0.80, 90.4, 2.4, float("-inf"), 89.0,
                                  ext_diff=-2.8, lam=0.3)
    assert w <= em.ANCHOR_WEIGHT_CAP - 0.3 + 1e-9


def test_anchor_weight_discounts_nudge_ext_used():
    """P3 fix Codex 2026-06-18: si el sign-nudge ya consumió 0.2 del budget
    externo, anchor_weight tiene headroom CAP − λ − 0.2."""
    # ext_diff grande, sin λ, ext_used=0.2 → w ≤ CAP − 0.2 = 0.3
    w = em.anchor_weight(-10.0, lam=0.0, ext_used=0.2)
    assert w == 0.30
    # λ=0.2 + nudge_used=0.3 → headroom 0
    assert em.anchor_weight(-10.0, lam=0.2, ext_used=0.3) == 0.0


def test_posterior_shift_weight_discounts_nudge_ext_used():
    """ext_used baja el CAP de λ: si nudge ya usó 0.3, λ ≤ 0.5 − 0.3 = 0.2."""
    w = em.posterior_shift_weight(ext_diff=-5.0, ext_spread=2.0,
                                  clim_percentile=50, ext_used=0.3)
    assert w == 0.20
    # nudge consumió todo el CAP → λ=0
    w = em.posterior_shift_weight(ext_diff=-5.0, ext_spread=2.0,
                                  clim_percentile=50, ext_used=0.5)
    assert w == 0.0


def test_blend_with_external_passes_ext_used():
    """blend_with_external propaga ext_used a anchor_weight."""
    _, w = em.blend_with_external(0.80, 90.4, 2.4, float("-inf"), 89.0,
                                  ext_diff=-10.0, lam=0.1, ext_used=0.2)
    # CAP 0.5 − λ 0.1 − ext_used 0.2 = 0.2
    assert w <= 0.20 + 1e-9
