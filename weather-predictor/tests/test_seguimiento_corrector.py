"""La guarda de vigilancia del corrector mira la dirección de la corrección.

Hasta el 2026-08-28 contaba errores NEGATIVOS a secas, porque las tres
estaciones habilitadas sobre-predecían y el corrector siempre restaba. KMIA es
la primera a la que se SUMA: allí una sobre-corrección produce errores
POSITIVOS y la guarda vieja no la habría visto nunca.

Los dos tests que importan son los de corrección negativa: son los que fallan
con la versión anterior.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                       / "investigacion"))

import seguimiento_corrector as sg  # noqa: E402


def _errores(n_en_contra: int, signo_contra: float, n: int = 10):
    """n_en_contra errores del lado `signo_contra`, el resto del otro."""
    return ([1.5 * signo_contra] * n_en_contra
            + [-1.5 * signo_contra] * (n - n_en_contra))


def test_correccion_positiva_y_errores_negativos_es_amarillo():
    estado, _, contra = sg.veredicto_por_signos(
        _errores(8, -1.0), corr_mediana=+2.5, m_pub=1.0, m_sin=2.0)
    assert (estado, contra) == ("amarillo", 8)


def test_correccion_positiva_que_ya_no_compensa_es_rojo():
    estado, _, _ = sg.veredicto_por_signos(
        _errores(8, -1.0), corr_mediana=+2.5, m_pub=2.5, m_sin=2.0)
    assert estado == "rojo"


def test_correccion_NEGATIVA_se_vigila_por_el_lado_positivo():
    """KMIA: se suma, así que pasarse deja errores POSITIVOS.

    Con la guarda vieja (contar negativos) esto salía 🟢 SEGUIR.
    """
    estado, _, contra = sg.veredicto_por_signos(
        _errores(8, +1.0), corr_mediana=-2.4, m_pub=1.0, m_sin=2.0)
    assert (estado, contra) == ("amarillo", 8)


def test_correccion_NEGATIVA_con_errores_negativos_no_alarma():
    """Quedarse corto en la dirección que ya se corrige no es sobre-corregir."""
    estado, _, contra = sg.veredicto_por_signos(
        _errores(8, -1.0), corr_mediana=-2.4, m_pub=1.0, m_sin=2.0)
    assert (estado, contra) == ("verde", 2)


def test_menos_de_diez_dias_no_decide():
    estado, texto, _ = sg.veredicto_por_signos(
        _errores(6, -1.0, n=6), corr_mediana=+2.5, m_pub=1.0, m_sin=2.0)
    assert estado == "n_bajo"
    assert "faltan 4" in texto


def test_solo_cuentan_los_ultimos_diez():
    """Veinte días buenos y diez malos al final: la guarda mira los últimos."""
    e = _errores(0, -1.0, n=20) + _errores(8, -1.0)
    estado, _, contra = sg.veredicto_por_signos(
        e, corr_mediana=+2.5, m_pub=1.0, m_sin=2.0)
    assert (estado, contra) == ("amarillo", 8)
