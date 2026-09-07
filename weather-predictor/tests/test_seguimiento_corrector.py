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


def test_congelada_con_las_dos_condiciones_se_reactiva():
    """Mejora >= 0.50°F Y >= 7/10 días con el sesgo del lado que corrige."""
    e_pub = [2.0] * 8 + [-2.0] * 2          # 8/10 sobre-prediciendo
    e_alt = [0.5] * 10                       # el corrector lo habría centrado
    estado, _, a_favor = sg.veredicto_congelado(e_pub, e_alt, corr_mediana=+2.5)
    assert (estado, a_favor) == ("reactivar", 8)


def test_congelada_que_solo_mejora_la_media_no_se_reactiva():
    """Un día raro muy bien acertado sube la media sin que el sesgo haya vuelto.

    Es la razón de que las dos condiciones sean AND: aquí el corrector gana
    1.30°F de media y aun así sólo 4 de 10 días llevan el sesgo que corrige.
    """
    e_pub = [2.0] * 4 + [-2.0] * 5 + [-9.0]
    e_alt = [0.5] * 4 + [-0.5] * 5 + [-0.5]
    estado, _, a_favor = sg.veredicto_congelado(e_pub, e_alt, corr_mediana=+2.5)
    assert estado == "congelado" and a_favor == 4


def test_congelada_que_solo_acierta_el_signo_no_se_reactiva():
    """El sesgo está del lado que corrige, pero es tan pequeño que corregirlo
    no compensa: 8/10 a favor y sólo 0.20°F de mejora."""
    e_pub = [0.7] * 8 + [-0.7] * 2
    e_alt = [0.5] * 10
    estado, _, _ = sg.veredicto_congelado(e_pub, e_alt, corr_mediana=+2.5)
    assert estado == "congelado"


def test_congelada_que_hace_daño_sostenido_se_retira():
    """Con N>=20 y medio grado de daño, el sesgo que medía ya no existe."""
    e_pub = [0.5] * 20
    e_alt = [2.0] * 20
    estado, texto, _ = sg.veredicto_congelado(e_pub, e_alt, corr_mediana=+2.5)
    assert estado == "retirar" and "1.50" in texto


def test_congelada_con_N_bajo_no_decide_aunque_el_dato_apunte():
    """Nueve días buenísimos no descongelan: el listón es N>=10, escrito antes
    de ver ninguno."""
    e_pub = [3.0] * 9
    e_alt = [0.1] * 9
    estado, texto, _ = sg.veredicto_congelado(e_pub, e_alt, corr_mediana=+2.5)
    assert estado == "congelado_n_bajo" and "faltan 1" in texto


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
