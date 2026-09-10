"""Los experimentos abiertos, en la home.

Las tres cosas que corren en sombra vencen por MUESTRA, no por fecha, así que
nadie sabe qué día toca mirar. La nota «revisar Sep-Oct» de SEASONAL_OFFSET_F
estuvo dos meses caducada con el offset corrigiendo a ciegas. El contador va
donde se mira todos los días — y no puede tumbar la home si falla.
"""
import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import experimentos  # noqa: E402


def test_el_resumen_es_una_lista_de_experimentos():
    r = experimentos.resumen(force=True)
    assert isinstance(r, list)
    for e in r:
        assert {"titulo", "n", "objetivo", "listo", "detalle"} <= set(e)
        assert isinstance(e["n"], int) and isinstance(e["objetivo"], int)
        assert isinstance(e["listo"], bool)


def test_no_lanza_si_las_fuentes_revientan(monkeypatch):
    """La home no puede caerse porque un experimento no se deje leer.

    Se hace fallar a las dos fuentes directamente. Parchear `BASE` no sirve:
    `_estado_reweight` lee del `BASE` de SU módulo, no del de éste, así que el
    test pasaba por la razón equivocada — y lo delató la suite completa, no la
    corrida aislada.
    """
    def _revienta(*a, **k):
        raise sqlite3.OperationalError("no such table")
    monkeypatch.setattr(experimentos, "_estado_reweight", _revienta)
    monkeypatch.setattr(experimentos, "_estado_corrector", _revienta)
    experimentos._cache["datos"] = None
    assert experimentos.resumen(force=True) == []


def test_cachea():
    """Estos números se mueven una vez al día; la home se recarga sola."""
    a = experimentos.resumen(force=True)
    b = experimentos.resumen()
    assert a is b
    assert experimentos.TTL_S >= 300


def test_la_home_no_se_cae_si_falla():
    import predictor_web
    src = inspect.getsource(predictor_web._experimentos_home)
    assert "except Exception" in src and "return []" in src


def test_la_home_lo_pasa_al_template():
    import predictor_web
    src = inspect.getsource(predictor_web.index)
    assert "experimentos=_experimentos_home()" in src
