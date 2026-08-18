"""La gráfica de /intraday abre por el bin que encabeza la tabla de movimiento.

Caso real (KMIA, 2026-08-18): a media mañana los seis bins llevaban n=5, así que
`max(move_bins, key=len(points))` devolvía el primero de la lista — "92° or
below", con el mercado en 0.5%, nuestro modelo en 3.0% y movimiento 0.0pp. La
gráfica principal abría con dos líneas planas mientras el bin interesante
(97-98: Kalshi +8.0pp, nosotros −6.8pp) quedaba a dos clics.
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


@pytest.fixture(scope="module")
def elegir():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pw_move", BASE / "predictor_web.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"predictor_web no importable: {e}")
    return m._default_move_bin


def _bins(**n_por_ticker):
    return [{"ticker": t, "label": t, "points": [{}] * n}
            for t, n in n_por_ticker.items()]


def _summary(*pares):
    """(ticker, k_delta) ya ordenados por |k_delta| como hace la ruta."""
    return [{"ticker": t, "k_delta": d} for t, d in pares]


def test_el_caso_kmia_abre_por_el_bin_que_se_movio(elegir):
    bins = _bins(**{"92_below": 5, "93_94": 5, "95_96": 5,
                    "97_98": 5, "99_100": 5, "101_above": 5})
    summary = _summary(("97_98", 0.080), ("99_100", -0.075),
                       ("95_96", 0.015), ("92_below", 0.0),
                       ("93_94", 0.0), ("101_above", 0.0))
    assert elegir(bins, summary)["ticker"] == "97_98"


def test_no_elige_un_bin_sin_linea_que_dibujar(elegir):
    """El de mayor movimiento tiene 1 punto: se pasa al siguiente."""
    bins = _bins(**{"a": 1, "b": 6, "c": 4})
    summary = _summary(("a", 0.40), ("b", 0.10), ("c", 0.05))
    assert elegir(bins, summary)["ticker"] == "b"


def test_sin_summary_cae_al_criterio_viejo(elegir):
    bins = _bins(**{"a": 2, "b": 9})
    assert elegir(bins, [])["ticker"] == "b"


def test_summary_con_tickers_desconocidos_no_rompe(elegir):
    bins = _bins(**{"a": 3})
    assert elegir(bins, _summary(("fantasma", 0.5)))["ticker"] == "a"


def test_sin_bins_devuelve_none(elegir):
    assert elegir([], _summary(("a", 0.1))) is None
