"""Kelly se muestra fraccionado porque nuestras probabilidades no están calibradas.

Caso real (2026-08-17, KMIA): la mesa recomendaba **"NO ~87% del bankroll"** en
el umbral >99°F, sobre un desacuerdo de 20pp con el mercado. Kelly completo es
óptimo sólo si la probabilidad que le das es la verdadera, y la nuestra está
medida y no lo es: `our_p` 0.80 acierta 0.35, y el Brier de Kalshi nos gana 7 de
9 días. Con probabilidades sobreconfiadas, Kelly completo crece justo cuando más
equivocado está el modelo.

El cálculo completo se conserva (`kelly_*`, `rec_kelly_full`); lo que se muestra
y se recomienda es el fraccionado.
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


@pytest.fixture(scope="module")
def mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pw_kelly", BASE / "predictor_web.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"predictor_web no importable: {e}")
    return m


def test_el_caso_kmia_ya_no_recomienda_el_87_por_ciento(mod):
    """>99°F: nuestro 3% contra el 23% del mercado."""
    r = mod._ev_kelly(p_our=0.03, k_yes=0.23)
    assert r["rec"] == "no"
    assert r["rec_kelly_full"] == pytest.approx(0.87, abs=0.01)
    assert r["rec_kelly"] == pytest.approx(0.2174, abs=0.01)
    assert r["rec_kelly"] < 0.25, "lo que se recomienda cabe bajo un cuarto"


def test_el_fraccionado_es_exactamente_un_cuarto(mod):
    r = mod._ev_kelly(p_our=0.60, k_yes=0.40)
    assert r["kelly_yes_frac"] == pytest.approx(r["kelly_yes"] * 0.25)
    assert r["kelly_no_frac"] == pytest.approx(r["kelly_no"] * 0.25)
    assert mod.KELLY_FRACTION == 0.25


def test_el_calculo_completo_no_se_toca(mod):
    """La fracción es de presentación: f*_yes = (p-k)/(1-k) sigue intacto."""
    r = mod._ev_kelly(p_our=0.60, k_yes=0.40)
    assert r["kelly_yes"] == pytest.approx((0.60 - 0.40) / (1 - 0.40))
    assert r["ev_yes"] == pytest.approx((0.60 - 0.40) / 0.40)
    assert r["ev_no"] == pytest.approx((0.40 - 0.60) / (1 - 0.40))


def test_kelly_nunca_negativo(mod):
    """El lado perdedor va a 0, no a una fracción negativa."""
    r = mod._ev_kelly(p_our=0.10, k_yes=0.90)
    assert r["kelly_yes"] == 0.0 and r["kelly_yes_frac"] == 0.0


def test_precio_invalido_devuelve_todo_none(mod):
    for k in (None, 0.005, 0.995):
        r = mod._ev_kelly(p_our=0.5, k_yes=k)
        assert r["rec_kelly"] is None and r["kelly_no_frac"] is None
        assert r["rec_kelly_full"] is None
