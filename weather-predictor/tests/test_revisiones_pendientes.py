"""Revisiones pendientes: que venzan avisando, no en silencio.

KLAS entró FALLANDO su criterio (mejora 0.66 contra 0.75, p=0.054) y se
habilitó restringiendo la ventana a 9-13h sobre la misma muestra que había
fallado. Su fila decía «se revisa con N≥40». La nota equivalente de
SEASONAL_OFFSET_F («revisar Sep-Oct») venció y estuvo dos meses sin que nadie
la mirara, así que ésta no se deja en un comentario.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                       / "investigacion"))

import level_corrector as lc  # noqa: E402
import seguimiento_corrector as sg  # noqa: E402


def test_klas_tiene_su_revision_registrada():
    assert sg.REVISIONES_PENDIENTES["KLAS"][0] == 40
    assert "9-13h" in sg.REVISIONES_PENDIENTES["KLAS"][1]


def _estado(n, debida):
    return {"st": "KLAS", "estado": "verde", "veredicto": "🟢 SEGUIR",
            "desde": "2026-08-28", "n": n, "neg": 2, "corr_med": -2.0,
            "n_reciente": 10, "m_pub": 1.28, "m_sin": 2.37,
            "filas": [{"dia": "2026-09-09", "settle": 104.0, "pub": 104.9,
                       "alt": 102.8, "corr": -2.1, "mk": None}],
            "revision_debida": debida, "revision_n": 40,
            "revision_motivo": "entró fallando su criterio"}


def test_el_informe_avisa_cuando_vence():
    txt = sg.render(_estado(40, True))
    assert "🔔 REVISIÓN DEBIDA (N≥40)" in txt


def test_antes_de_vencer_dice_cuanto_falta():
    txt = sg.render(_estado(13, False))
    assert "faltan 27 días" in txt
    assert "🔔" not in txt


# ─── el registro en sombra fuera de la ventana horaria ──────────────────────

def test_fuera_de_su_ventana_klas_registra_pero_no_aplica(monkeypatch):
    """Sin esto, dentro de un mes no habría con qué juzgar si la ventana 9-13h
    estaba bien puesta: fuera de ella no queda ni rastro de lo que el corrector
    habría hecho, y tocaría reconstruirlo con una réplica."""
    monkeypatch.setattr(lc, "median_level_bias", lambda *a, **k: (-2.0, 25))
    info = lc.bias_info_for("KLAS", date(2026, 9, 10), local_hour=16)
    assert info is not None, "devolver None borraría el rastro"
    assert info["applied"] is False, "fuera de su ventana no debe corregir"
    assert info["frozen"] is True
    assert info["bias"] == -2.0
    assert "FUERA DE VENTANA" in info["reason"]


def test_dentro_de_su_ventana_klas_si_aplica(monkeypatch):
    monkeypatch.setattr(lc, "median_level_bias", lambda *a, **k: (-2.0, 25))
    info = lc.bias_info_for("KLAS", date(2026, 9, 10), local_hour=11)
    assert info["applied"] is True
    assert info["frozen"] is False


def test_una_estacion_sin_ventana_no_se_ve_afectada(monkeypatch):
    """El cambio no puede colarse en las que corrigen el día entero."""
    monkeypatch.setattr(lc, "median_level_bias", lambda *a, **k: (3.0, 25))
    activa = sorted(lc.ENABLED_STATIONS - lc.FROZEN_STATIONS - {"KLAS"})[0]
    for hora in (7, 12, 19):
        info = lc.bias_info_for(activa, date(2026, 9, 10), local_hour=hora)
        assert info["applied"] is True, f"{activa} dejó de corregir a las {hora}h"
