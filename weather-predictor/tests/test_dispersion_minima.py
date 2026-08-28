"""Dispersión mínima por estación: ensancha la banda sin mover la mediana.

Lo que este operador tiene que garantizar, y que un factor multiplicativo no
puede: que una distribución degenerada —un tercio de los días de KMIA llegan
con la banda por debajo de 0.3°F— acabe con anchura real. Multiplicar cero por
cuatro sigue siendo cero.
"""
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import predictor as P  # noqa: E402


def _p(vals, q):
    s = sorted(vals)
    return s[int(len(s) * q)]


def test_la_mediana_no_se_mueve():
    base = [80.0, 81.0, 81.0, 82.0, 83.0, 84.0, 85.0]
    out = P.widen_min_spread(base, 1.0)
    assert statistics.median(out) == statistics.median(base)


def test_una_distribucion_degenerada_acaba_con_anchura():
    """El caso que motivó el operador: 500 miembros idénticos."""
    base = [92.0] * 500
    out = P.widen_min_spread(base, 1.0)
    assert _p(out, 0.10) == 91.0
    assert _p(out, 0.90) == 93.0
    assert statistics.median(out) == 92.0


def test_un_factor_multiplicativo_no_habria_podido():
    """Contraprueba explícita de por qué no se usó un factor."""
    base = [92.0] * 500
    med = statistics.median(base)
    inflado = [med + 4.0 * (v - med) for v in base]
    assert _p(inflado, 0.90) - _p(inflado, 0.10) == 0.0


def test_es_un_MINIMO_de_dispersion_no_un_ensanche_uniforme():
    """Sobre una banda que ya es ancha, el operador casi no hace nada.

    Es lo que se quiere y conviene dejarlo escrito: `m` pone un SUELO a la
    anchura, no le suma 2m a todo el mundo. Un día que ya venía con 4.8°F de
    banda no necesita que le inventen dispersión.
    """
    estrecha = [92.0 + 0.6 * i / 499 for i in range(500)]
    ancha = [90.0 + 6.0 * i / 499 for i in range(500)]

    def ancho(v):
        return round(_p(v, 0.90) - _p(v, 0.10), 2)

    assert ancho(estrecha) == 0.48
    assert ancho(P.widen_min_spread(estrecha, 1.0)) == 2.12   # ×4.4
    assert ancho(ancha) == 4.81
    assert ancho(P.widen_min_spread(ancha, 1.0)) == 5.08      # ×1.06


def test_sin_m_no_toca_nada():
    base = [80.0, 81.0, 82.0]
    assert P.widen_min_spread(base, None) == base
    assert P.widen_min_spread(base, 0.0) == base


def test_el_registro_es_explicito_por_estacion():
    """Igual que ENABLED_STATIONS: entrar exige corrida propia, así que este
    assert se rompe a propósito si alguien añade una estación sin tocarlo."""
    assert P.SPREAD_MIN_F == {"KMIA": 1.0}
    assert "KLAS" not in P.SPREAD_MIN_F     # residuo +1.92°F: es nivel, no anchura


def test_el_piso_recorta_lo_que_sobra_por_abajo():
    """Ensanchar no puede meter miembros bajo el máximo ya observado."""
    base = [92.0] * 100
    anchos, _, _ = P.apply_obs_floor(P.widen_min_spread(base, 1.0), 91.5)
    assert min(anchos) >= 91.5
    assert max(anchos) == 93.0
