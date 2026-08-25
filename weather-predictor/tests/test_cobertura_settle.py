"""La ventana ciega de la fuente de liquidación (2026-08-22).

El feed de 5 min ve la temperatura de AHORA, pero el grupo ASOS de 6h —la fuente
con la que liquida el NWS— sólo cubre hasta su última publicación. En KDEN ese
hueco dura seis horas y se traga el pico entero, y hasta ahora no se veía.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import predictor_web


class _Est:
    def __init__(self, sid, tz):
        self.id = sid
        self.tz = ZoneInfo(tz)


KDEN = _Est("KDEN", "America/Denver")
KNYC = _Est("KNYC", "America/New_York")


def test_kden_a_las_15h_lleva_3h_sin_cobertura():
    """15:00 MDT = 21:00Z. La última publicación limpia fue 17:53Z = 11:53 MDT."""
    ahora = datetime(2026, 8, 22, 21, 0, tzinfo=timezone.utc)
    c = predictor_web._cobertura_settle(KDEN, ahora)
    assert c["cubierto_hasta"] == "11:53"
    assert c["minutos"] == 187, c["minutos"]
    assert c["proxima"] == "17:53", "la siguiente no llega hasta el final del pico"


def test_kden_marca_la_ventana_ciega_en_horas_de_pico():
    for h_utc, activa in ((21, True), (2, False)):   # 15h MDT vs 20h MDT
        c = predictor_web._cobertura_settle(
            KDEN, datetime(2026, 8, 22, h_utc, 0, tzinfo=timezone.utc))
        assert c["ciega"]["activa"] is activa, h_utc


def test_knyc_no_tiene_ventana_ciega_medida():
    """El ASOS le llega a las 13:53 y sí tapa el hueco: no lleva aviso."""
    c = predictor_web._cobertura_settle(
        KNYC, datetime(2026, 8, 22, 19, 0, tzinfo=timezone.utc))
    assert "ciega" not in c
    assert c["cubierto_hasta"] == "13:53", "15h EDT: cubierto hasta las 13:53"


def test_no_cuenta_como_cobertura_la_ventana_de_ayer():
    """A las 07:00 MDT la publicación de 11:53Z cubre 05:53→11:53Z, que en local
    es 23:53 de AYER → 05:53 de hoy. Cruza medianoche: no cuenta."""
    c = predictor_web._cobertura_settle(
        KDEN, datetime(2026, 8, 22, 13, 0, tzinfo=timezone.utc))
    assert c["cubierto_hasta"] != "05:53", (
        "esa ventana empieza el día anterior; es la guarda del piso")


def test_siempre_hay_una_proxima():
    """Incluso en el borde del día, para no dejar el hueco sin cerrar."""
    for h in range(0, 24, 3):
        c = predictor_web._cobertura_settle(
            KDEN, datetime(2026, 8, 22, h, 30, tzinfo=timezone.utc))
        assert c["proxima"], h
