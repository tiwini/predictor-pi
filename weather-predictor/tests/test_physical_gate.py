"""Techo físico: los cuatro casos reales que motivaron la guarda.

Tres son operaciones que el gate marcó ACTIONABLE y perdieron; el cuarto (KLAS)
es el contraejemplo que fija el límite — allí la ventana seguía abierta y el
gate ACERTÓ, así que la guarda NO debe bloquearlo. Si algún día un cambio hace
que KLAS se bloquee, la guarda se ha vuelto demasiado agresiva.
"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import physical_gate as pg  # noqa: E402
import agent_signals as sig  # noqa: E402
from stations import STATION_TZ  # noqa: E402


def _snap(st, hora, minuto=0, max_obs=None, cur=None, peak=None, estable=None):
    tz = ZoneInfo(STATION_TZ[st])
    return SimpleNamespace(
        today_max_obs=max_obs, current_temp_f=cur, peak_state=peak,
        current_temp_stable_min=estable,
        station_local=datetime(2026, 8, 14, hora, minuto, tzinfo=tz))


@pytest.fixture(autouse=True)
def _limpia():
    pg.clear_cache()
    yield
    pg.clear_cache()


# ─── vetos por bin, con el techo dado ────────────────────────────────────

def test_veta_comprar_bin_inalcanzable():
    """KATL 08-07: comprar 89-90 con el pico confirmado en 87.1."""
    assert pg.blocks_bin("YES", 89, 90, ceiling=88.1) is not None
    # el bin que contiene el techo sigue siendo comprable
    assert pg.blocks_bin("YES", 87, 88, ceiling=88.1) is None


def test_veta_vender_el_bin_donde_cae_el_techo():
    """KSEA 08-10: vender "75 or below" con el techo en 75."""
    assert pg.blocks_bin("NO", float("-inf"), 75, ceiling=75.0) is not None
    # vender un bin muy por encima del techo es correcto, no se veta
    assert pg.blocks_bin("NO", 80, 81, ceiling=75.0) is None


def test_knyc_comprar_87_88_con_82_9_plano():
    """KNYC 08-12: +45.4pp de edge sobre un bin fuera de alcance."""
    assert pg.blocks_bin("YES", 87, 88, ceiling=84.9) is not None


def test_sin_techo_no_bloquea_nada():
    for side in ("YES", "NO"):
        assert pg.blocks_bin(side, 89, 90, ceiling=None) is None


def test_respeta_el_medio_grado_del_redondeo():
    """El settle es entero: el bin cubre [lo-0.5, hi+0.5]."""
    # techo 88.6 con bin 89-90 -> suelo real 88.5, alcanzable por poco
    assert pg.blocks_bin("YES", 89, 90, ceiling=88.6) is None
    assert pg.blocks_bin("YES", 89, 90, ceiling=88.4) is not None


# ─── cálculo del techo ───────────────────────────────────────────────────

def test_pico_confirmado_acota_con_el_gap_del_cli():
    c, why = pg.ceiling_f("KATL", _snap("KATL", 17, 25, max_obs=87.1,
                                        cur=86.0, peak="PeakState.CONFIRMED"))
    assert c == pytest.approx(87.1 + pg.CLI_GAP_F)
    assert "confirmado" in why


def test_ventana_cerrada_acota_aunque_no_este_confirmado():
    c, _ = pg.ceiling_f("KATL", _snap("KATL", 18, 0, max_obs=90.0, cur=89.0))
    assert c == pytest.approx(90.0 + pg.CLI_GAP_F)


def test_plana_con_poca_ventana_acota():
    """KNYC 08-12: 82.9 plano 1h07m con 1 h de ventana (13-16h)."""
    c, why = pg.ceiling_f("KNYC", _snap("KNYC", 15, 3, max_obs=82.9, cur=82.9,
                                        estable=67))
    assert c == pytest.approx(82.9 + pg.FLAT_HEADROOM_F)
    assert "plana" in why


def test_klas_ventana_abierta_no_se_acota_por_las_vias_duras():
    """CONTRAEJEMPLO. KLAS 08-07 a las 14:29, ventana 14-17h abierta 2.6 h y
    subiendo: el gate acertó. Las vías duras (pico/ventana/plana) no deben
    disparar; sólo podría acotar la vía 3, que necesita historia."""
    snap = _snap("KLAS", 14, 29, max_obs=107.1, cur=108.0,
                 peak="PeakState.RISING", estable=28)
    c, why = pg.ceiling_f("KLAS", snap)
    assert "confirmado" not in why and "cerrada" not in why and "plana" not in why


def test_usa_el_mayor_de_max_obs_y_current():
    """El feed de 5 min va por delante del METAR horario."""
    c, _ = pg.ceiling_f("KATL", _snap("KATL", 18, 0, max_obs=85.0, cur=88.0))
    assert c == pytest.approx(88.0 + pg.CLI_GAP_F)


def test_sin_max_obs_no_acota():
    c, why = pg.ceiling_f("KATL", _snap("KATL", 18, 0, max_obs=None))
    assert c is None and "sin max_obs" in why
    c, _ = pg.ceiling_f("KATL", _snap("KATL", 18, 0, max_obs=-999.0))
    assert c is None


# ─── integración con evaluate_bin ────────────────────────────────────────

def test_evaluate_bin_bloquea_con_techo_y_no_sin_el():
    kw = dict(station_id="KNYC", bin_lo=87, bin_hi=88, bin_label="87-88",
              kalshi_yes_price=0.05, model_p_calibrated=0.504, our_pred_f=87.5)
    libre = sig.evaluate_bin(**kw)
    assert libre["actionable"] is True, "sin techo debe seguir siendo accionable"
    vetado = sig.evaluate_bin(**kw, physical_ceiling_f=84.9)
    assert vetado["actionable"] is False
    assert any("techo físico" in r for r in vetado["blocked_reasons"])
