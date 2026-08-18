"""Una distribución clavada en el piso NO es "alta confianza".

Caso real (KMIA, 2026-08-18 14:19 EDT): el hero mostraba

    97.70   ·   banda p10-p90 97.7–97.7°F (0.0°F)   ·   "alta confianza"

con el termómetro ya en **98.6°F** y la ventana de pico abierta hasta las 17h.

`apply_obs_floor` clampea la distribución entera a propósito, para que
percentiles y bins queden coherentes. Cuando TODO el ensemble está por debajo
del piso, eso la aplasta a un punto: la banda cae a 0.0°F y las reglas de
confianza (`band <= 2.0` ⇒ alta) la premiaban como certeza máxima.

O sea **cuanto más equivocado estaba el modelo, más seguro se veía** — el mismo
defecto de forma que el Kelly completo.

No se toca el piso: está medido y backtesteado (|error| 1.400 → 1.283). Lo que
cambia es lo que la página afirma sobre él.
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


@pytest.fixture(scope="module")
def hero():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pw_hero", BASE / "predictor_web.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"predictor_web no importable: {e}")
    return m._build_hero


def test_el_caso_kmia_no_dice_alta_confianza(hero):
    """31 miembros aplastados contra el piso de 97.7."""
    h = hero([97.7] * 31, prev_med=97.7, floor_n=31, floor_f=97.7,
             current_f=98.6)
    assert h["conf_class"] == "conf-low"
    assert h["conf_label"] == "sin señal propia"
    assert "piso" in h["conf_str"]
    assert "98.6" in h["hint"], "debe decir lo que marca el termómetro"


def test_banda_estrecha_legitima_sigue_siendo_alta_confianza(hero):
    """Sin clampeo, un ensemble genuinamente concentrado no se degrada."""
    h = hero([95.0] * 15 + [95.1] * 16, prev_med=95.0, floor_n=0)
    assert h["conf_class"] == "conf-high"
    assert "clavada" not in h["conf_str"]


def test_clampeo_parcial_no_degrada(hero):
    """Si sólo parte del ensemble tocó el piso, el resto sigue informando."""
    dist = [96.0] * 5 + [96.5, 97.0, 97.5, 98.0, 98.5] * 5
    h = hero(dist, prev_med=97.0, floor_n=5)
    assert h["conf_label"] != "sin señal propia"


def test_sin_datos_de_piso_se_comporta_como_antes(hero):
    """Compatibilidad: los llamadores que no pasan floor_n no cambian."""
    h = hero([94.0, 95.0, 96.0] * 10, prev_med=95.0)
    assert h["conf_label"] in ("alta confianza", "confianza media",
                              "baja confianza")


def test_el_valor_publicado_no_se_toca(hero):
    """El piso sigue mandando en el NÚMERO; sólo cambia lo que se afirma."""
    h = hero([97.7] * 31, prev_med=97.7, floor_n=31, floor_f=97.7,
             current_f=98.6)
    assert h["value"] == "97.70"
