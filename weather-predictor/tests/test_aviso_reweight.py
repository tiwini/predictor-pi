"""El veredicto del reweight aparece donde se mira todos los días.

Un experimento que vence en un fichero que nadie abre no ha vencido para nadie:
la nota «revisar Sep-Oct» de SEASONAL_OFFSET_F estuvo dos meses caducada. Por
eso el estado de las dos ramas se cuela en el informe diario del corrector.
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                       / "investigacion"))

import corrector_watchdog as cw  # noqa: E402
import reweight_veredicto as rw  # noqa: E402


def test_el_informe_diario_incluye_el_reweight():
    src = inspect.getsource(cw.main)
    assert "resumen_corto" in src, (
        "sin esto el veredicto vence en un fichero que nadie abre")


def test_el_umbral_de_decision_es_deliberado():
    assert rw.MIN_DIAS == 10


def test_el_resumen_es_una_linea():
    """Se pega dentro de un informe: no puede reventar ni traer saltos."""
    txt = rw.resumen_corto()
    assert isinstance(txt, str) and txt
    assert "\n" not in txt


def test_el_resumen_dice_reweight():
    """Va suelto entre bloques del corrector, así que tiene que nombrarse."""
    assert "reweight" in rw.resumen_corto().lower()
