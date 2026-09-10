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
