"""Revisiones pendientes: que venzan avisando, no en silencio.

KLAS entró FALLANDO su criterio (mejora 0.66 contra 0.75, p=0.054) y se
habilitó restringiendo la ventana a 9-13h sobre la misma muestra que había
fallado. Su fila decía «se revisa con N≥40». La nota equivalente de
SEASONAL_OFFSET_F («revisar Sep-Oct») venció y estuvo dos meses sin que nadie
la mirara, así que ésta no se deja en un comentario.
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                       / "investigacion"))

import level_corrector as lc  # noqa: E402
import seguimiento_corrector as sg  # noqa: E402


def test_klas_cerrada_y_no_sigue_pendiente():
    """Cerrada el 2026-10-12 con N=29, su techo, pasando el criterio sin rebajas.

    Dejarla en el registro la haría salir en el informe cada día pidiendo una
    revisión ya hecha, que es la otra forma de que un aviso deje de significar
    algo.
    """
    assert "KLAS" not in sg.REVISIONES_PENDIENTES


def test_ningun_objetivo_por_encima_del_techo_de_retencion():
    """El N que se pida tiene que caber en los ~30 días que retiene la DB.

    KLAS pidió 40 y el aviso no llegó nunca: N se quedó en 29 y el informe
    anunció «faltan 11 días» dos semanas seguidas. Un objetivo inalcanzable no
    es una revisión pendiente, es una revisión que no se hará.
    """
    for st, (n, _motivo) in sg.REVISIONES_PENDIENTES.items():
        assert n <= 30, (
            f"{st} pide N={n} y analysis.db retiene ~30 días: "
            f"ese aviso no vencería nunca")


def _db_con_dias(dias):
    """DB en memoria con un snapshot por día, para el suelo de retención."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE station_snapshots (station TEXT, ts TEXT, "
                "bias_path TEXT, bias_applied INT, bias_frozen_f REAL)")
    for d in dias:
        con.execute("INSERT INTO station_snapshots VALUES (?,?,?,?,?)",
                    ("KTEST", f"{d.isoformat()}T12:00:00", "median_causal",
                     1, None))
    return con


def test_techo_detecta_la_fase_que_toca_el_suelo_de_retencion():
    # Sembrado relativo a hoy: una fecha literal caducaría.
    hoy = date.today()
    dias = [hoy - timedelta(days=k) for k in range(29, -1, -1)]
    con = _db_con_dias(dias)
    suelo = con.execute(
        "SELECT MIN(date(datetime(ts,'localtime'))) FROM station_snapshots "
        "WHERE station=?", ("KTEST",)).fetchone()[0]

    # La fase empieza en el día más viejo que QUEDA -> es el techo.
    assert sg.techo_por_retencion(con, "KTEST", suelo) is True
    # Una fase estrenada después del suelo todavía puede crecer.
    reciente = (date.fromisoformat(suelo) + timedelta(days=5)).isoformat()
    assert sg.techo_por_retencion(con, "KTEST", reciente) is False
    # Sin fase no hay nada que decidir.
    assert sg.techo_por_retencion(con, "KTEST", None) is False


def test_el_informe_dice_TECHO_y_no_un_countdown_falso():
    e = _estado(29, False)
    e["revision_techo"] = True
    txt = sg.render(e)
    assert "🔔 REVISIÓN DEBIDA YA" in txt
    assert "TECHO" in txt
    # Lo que no puede volver a aparecer: fingir que faltan días.
    assert "faltan" not in txt


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
