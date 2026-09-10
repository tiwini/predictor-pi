"""La guarda de vigilancia del corrector mira la dirección de la corrección.

Hasta el 2026-08-28 contaba errores NEGATIVOS a secas, porque las tres
estaciones habilitadas sobre-predecían y el corrector siempre restaba. KMIA es
la primera a la que se SUMA: allí una sobre-corrección produce errores
POSITIVOS y la guarda vieja no la habría visto nunca.

Los dos tests que importan son los de corrección negativa: son los que fallan
con la versión anterior.
"""
import sys

import pytest
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


# ─── criterio de reactivación (reescrito 2026-09-10 tras la auditoría) ──────
#
# El de la primera versión pedía «mejora ≥0.50°F y ≥7/10 días del lado que
# corrige», mirándolo a diario. Con la desviación real de KLAX (1.96°F) ese
# umbral valía 0.8 errores estándar: se cruzaba el 21% de las veces por ruido,
# y treinta miradas lo convertían en cuestión de tiempo.


def _fase(d_list):
    """Construye (e_pub, e_alt) con las diferencias `d` pedidas.

    `d_i = |err_pub| − |err_alt|` > 0 significa que el corrector habría quedado
    más cerca ese día. Se fija |err_alt| y se despeja |err_pub|.

    El nivel base se elige para que `err_alt + d` nunca cruce el cero: si
    cruza, el valor absoluto lo dobla y la diferencia deja de ser la pedida
    (con base 1.0, un d de −1.8 daba −0.8, o sea |d| real de 0.2 en vez de
    1.8, y el test medía otra cosa).
    """
    err_alt = max(1.0, -min(d_list) + 0.5)
    e_alt = [err_alt] * len(d_list)
    e_pub = [err_alt + d for d in d_list]
    return e_pub, e_alt


def test_reactiva_con_signos_significativos_y_mejora_material():
    """9 de 10 (p=0.011) y mejora 1.54°F sobre un listón de 0.50."""
    pub, alt = _fase([1.8] * 9 + [-0.8])
    estado, texto, a_favor = sg.veredicto_congelado(pub, alt, corr_mediana=2.5)
    assert (estado, a_favor) == ("reactivar", 9)
    assert "p=0.011" in texto


def test_ocho_de_diez_ya_no_basta():
    """p=0.055 — el criterio viejo se conformaba con 7/10, que es p=0.17."""
    pub, alt = _fase([1.8] * 8 + [-0.8] * 2)
    estado, _, _ = sg.veredicto_congelado(pub, alt, corr_mediana=2.5)
    assert estado == "congelado"


def test_consistente_pero_irrelevante_no_reactiva():
    """10 de 10 a favor, p=0.001, pero la mejora es de 0.30°F: significativa y
    sin importancia. El suelo de materialidad existe para esto."""
    pub, alt = _fase([0.3] * 10)
    estado, _, _ = sg.veredicto_congelado(pub, alt, corr_mediana=2.5)
    assert estado == "congelado"


def test_mejora_grande_pero_ahogada_en_varianza_no_reactiva():
    """9 de 10 y +1.60°F de media, pero un día de −20 dispara el error
    estándar a 2.40 y el listón a 3.95. Es justo el caso que el umbral fijo
    de 0.50 dejaba pasar."""
    pub, alt = _fase([4.0] * 9 + [-20.0])
    estado, _, _ = sg.veredicto_congelado(pub, alt, corr_mediana=2.5)
    assert estado == "congelado"


def test_corrector_apagado_es_irrelevante_no_reactivable():
    """La corrección mediana de la fase es 0.20°F: la mediana móvil se encogió
    sola al salir agosto de la ventana de 30 días. Ganar sin corregir nada no
    es motivo para encender nada."""
    pub, alt = _fase([1.8] * 9 + [-0.8])
    estado, texto, _ = sg.veredicto_congelado(pub, alt, corr_mediana=0.20)
    assert estado == "irrelevante"
    assert "no corrige nada" in texto


def test_retirar_es_el_espejo_exacto():
    """Mismo N que reactivar: la asimetría 10/20 de la primera versión
    favorecía volver a encender."""
    pub, alt = _fase([-1.8] * 9 + [0.8])
    estado, _, _ = sg.veredicto_congelado(pub, alt, corr_mediana=2.5)
    assert estado == "retirar"


def test_mirar_a_diario_no_cambia_el_veredicto():
    """EL test de esta reescritura. El veredicto sale de los N PRIMEROS días,
    así que en cuanto hay 10 el conjunto ya no cambia y el informe diario
    repite el resultado en vez de tirar otra moneda.

    Aquí los 10 primeros no reactivan (8/10) y los cuatro siguientes son
    inmejorables: con «los últimos 10» habría reactivado al cuarto día.
    """
    d = [1.8] * 8 + [-0.8] * 2
    pub10, alt10 = _fase(d)
    base, _, _ = sg.veredicto_congelado(pub10, alt10, corr_mediana=2.5)
    assert base == "congelado"
    for extra in range(1, 5):
        pub, alt = _fase(d + [3.0] * extra)
        estado, _, _ = sg.veredicto_congelado(pub, alt, corr_mediana=2.5)
        assert estado == base, f"cambió de veredicto al día {10 + extra}"


def test_la_segunda_ventana_es_N20():
    """Con 20 días se vuelve a decidir, y sólo entonces: es la última mirada."""
    d = [1.8] * 8 + [-0.8] * 2          # 8/10, no decide
    pub, alt = _fase(d + [1.8] * 10)     # 18/20 → p=0.0004
    estado, _, a_favor = sg.veredicto_congelado(pub, alt, corr_mediana=2.5)
    assert (estado, a_favor) == ("reactivar", 18)


def test_p_signos_es_la_binomial_exacta():
    assert sg._p_signos(9, 10) == pytest.approx(11 / 1024)
    assert sg._p_signos(8, 10) == pytest.approx(56 / 1024)
    assert sg._p_signos(10, 10) == pytest.approx(1 / 1024)


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
