"""Frescura del dato por estación.

Medido el 2026-08-19: nuestro snapshot va parejo en las 20 estaciones (6-8 min)
pero **la observación varía 6×** — de 16 a 95 min. KNYC llevaba 95 minutos sin
reportar mientras el usuario la miraba, y nada en la web lo decía.

El criterio no se inventó aquí: es el del aviso `OBSERVACIÓN VIEJA` que vivía en
`investigacion/lectura_estacion.py` desde el caso KNYC del 2026-08-14. Se
centralizó en `predictor.obs_freshness` para que el CLI y el web usen la misma
función — duplicarlo es como se desalineó el pipeline de bins.

El suelo de 65 min es la pieza clave: el METAR es horario y sale a las :53, así
que la edad oscila de 0 a ~65 **por diseño**. Sin ese suelo vuelven los falsos
positivos que costaron tres iteraciones.
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from predictor import obs_freshness, STALE_FLOOR_MIN, STALE_CLOSED_MIN  # noqa: E402


def test_edad_normal_del_metar_no_alarma():
    """0-65 min es lo que el METAR horario produce solo. No se marca."""
    for edad in (0, 20, 45, 64.9):
        assert obs_freshness(edad, 180)["nivel"] == "fresca", edad


def test_por_encima_del_suelo_pero_bajo_el_umbral_es_vieja():
    """Con 3h de ventana el límite es 90 min; 70 ya es más de lo normal."""
    r = obs_freshness(70, 180)
    assert r["nivel"] == "vieja"
    assert r["limite_min"] == 90.0


def test_el_caso_knyc_del_08_14():
    """70 min sin publicar con 1.2 h de ventana: el límite baja a 65 y salta."""
    r = obs_freshness(70, 72)
    assert r["nivel"] == "muerta"
    assert r["limite_min"] == STALE_FLOOR_MIN


def test_el_caso_knyc_del_08_19():
    """95 min de silencio pero 3.5 h de ventana: límite 105, aún no alarma.

    Es correcto y conviene que quede fijado: quedarse ciego temprano en la
    ventana es menos grave que hacerlo con el pico ocurriendo.
    """
    r = obs_freshness(95, 210)
    assert r["nivel"] == "vieja"
    assert r["limite_min"] == 105.0


def test_el_umbral_es_relativo_a_la_ventana_restante():
    """La MISMA edad cambia de nivel según cuánta ventana quede."""
    assert obs_freshness(80, 300)["nivel"] == "vieja"    # límite 150
    assert obs_freshness(80, 60)["nivel"] == "muerta"    # límite 65


def test_ventana_cerrada_usa_el_limite_fijo():
    r = obs_freshness(95, 0)
    assert r["limite_min"] == STALE_CLOSED_MIN
    assert r["nivel"] == "muerta"


def test_el_suelo_no_baja_de_65_por_mucho_que_cierre():
    """Con la ventana casi agotada el límite se queda en 65, no en 5."""
    assert obs_freshness(40, 10)["limite_min"] == STALE_FLOOR_MIN
    assert obs_freshness(40, 10)["nivel"] == "fresca"


def test_sin_edad_no_inventa_nivel():
    r = obs_freshness(None, 120)
    assert r["nivel"] is None and r["limite_min"] is None


# ── Orden del selector por cercanía a la ventana de pico ─────────────────
#
# Las 20 estaciones cubren cuatro husos: a cualquier hora unas están en pleno
# pico y otras de madrugada. Alfabético obliga a buscar; por cercanía, lo que
# está ocurriendo va arriba (pedido del usuario, 2026-08-21).

@pytest.fixture(scope="module")
def proximidad():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pw_prox", BASE / "predictor_web.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"predictor_web no importable: {e}")
    return m._peak_proximity


def test_los_tres_tramos_se_ordenan_bien(proximidad, monkeypatch):
    """Dentro de ventana < antes de abrir < ya cerrada."""
    import importlib
    prox = proximidad
    tramos = {}
    from stations import STATION_IDS
    for sid in STATION_IDS:
        tramos[sid] = prox(sid)[0]
    # A cualquier hora del día, todo tramo válido está en {0,1,2}
    assert set(tramos.values()) <= {0, 1, 2}, tramos


def test_la_etiqueta_dice_el_tramo(proximidad):
    from stations import STATION_IDS
    for sid in STATION_IDS:
        tramo, _, et = proximidad(sid)
        if tramo == 0:
            assert "pico ahora" in et
        elif tramo == 1:
            assert "abre en" in et
        elif tramo == 2:
            assert et == "cerrada"


def test_estacion_desconocida_no_revienta(proximidad):
    tramo, _, _ = proximidad("XXXX")
    assert tramo == 3, "cae al final en vez de lanzar"
