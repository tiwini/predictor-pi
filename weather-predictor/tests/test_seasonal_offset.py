"""SEASONAL_OFFSET_F: sus valores y la fecha de su revisión.

Van en archivo propio a propósito. `test_bias_tracker.py` tiene un fixture
`autouse` que vacía `SEASONAL_OFFSET_F` para que los asserts de bias no lo
vean, así que un test alojado allí comprobaría siempre un diccionario vacío y
pasaría diga lo que diga el código.
"""
from datetime import date
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bias_tracker as bt  # noqa: E402


def test_los_valores_del_offset_son_deliberados():
    """Como el roster del corrector: se assertea entero para que cambiarlo sea
    una decisión y no un descuido. Revisados el 2026-09-10 contra 30 días por
    estación — ver DECISIONES.md y el comentario del propio módulo."""
    assert bt.SEASONAL_OFFSET_F == {"KLAS": -1.70, "KPHX": -1.55, "KBOS": -0.99}


def test_el_offset_se_aplica_restando():
    """`predictor.py` hace `v - _seasonal`, así que un valor NEGATIVO suma
    grados. Es contraintuitivo y ya costó una lectura al revés: los tres son
    negativos porque los tres empujan hacia ARRIBA."""
    assert all(v < 0 for v in bt.SEASONAL_OFFSET_F.values())


def test_la_revision_del_offset_no_esta_vencida():
    """La nota «revisar Sep-Oct» de julio venció sin que nadie la viera durante
    dos meses, con el offset corrigiendo a ciegas mientras tanto. Un comentario
    no avisa; esto sí.

    Si falla: corre `investigacion/seasonal_revision.py --contrafactual`, anota
    el veredicto en DECISIONES.md y mueve `SEASONAL_OFFSET_REVISADO_HASTA`.
    """
    assert date.today().isoformat() <= bt.SEASONAL_OFFSET_REVISADO_HASTA, (
        "la revisión de SEASONAL_OFFSET_F está vencida — ver el docstring")


def test_la_pregunta_de_KPHX_no_se_queda_dormida():
    """El déficit de KPHX quedó confirmado en dos ventanas (−1.23 el 09-10 y
    −1.29 el 10-18) y la decisión de subirlo se aplazó a propósito, porque el
    tamaño del aumento se habría ajustado sobre el dato ya mirado.

    Un aplazamiento sin fecha es un olvido con buena letra; éste tiene las dos
    mitades que pide la doctrina: fecha Y destinatario que falla solo.

    Si falla: la serie ya tiene ~30 días posteriores al 2026-10-18. Mide el
    candidato −2.84 contra el −1.55 actual con el criterio YA ESCRITO en la
    fila ⏳ de DECISIONES.md —no uno nuevo— y cierra la fila con el número.
    """
    assert date.today().isoformat() < bt.KPHX_EMPUJE_DECIDE_DESDE, (
        "toca decidir el empuje de KPHX — ver el docstring y DECISIONES.md")
