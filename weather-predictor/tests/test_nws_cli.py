"""Tests para nws_cli.fetch_max_for con NWS API mockeada."""
from datetime import date
from unittest.mock import patch

import nws_cli


SAMPLE_CLI = """
000
CDUS45 KPSR 081050
CLIPHX

CLIMATE REPORT
NATIONAL WEATHER SERVICE PHOENIX AZ
350 AM MST FRI MAY 8 2026

...................................

...THE PHOENIX SKY HARBOR INTERNATIONAL AP CLIMATE SUMMARY FOR MAY 7 2026...

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
                VALUE   (LST)  VALUE       VALUE  FROM      YEAR
                                                  NORMAL
...................................................................
TEMPERATURE (F)
 MAXIMUM        101    459 PM 110    1989  92      9       95
 MINIMUM         72    520 AM  47    1983  64      8       66
 AVERAGE         87                        78      9       81

PRECIPITATION (IN)
 YESTERDAY       0.00          1.20 1973   0.02  -0.02     0.00
"""


def _resp(status=200, payload=None):
    class R:
        def __init__(self):
            self.status_code = status
            self._p = payload or {}
        def json(self):
            return self._p
    return R()


def setup_function():
    nws_cli.clear_cache()


def test_unsupported_station_returns_none():
    assert nws_cli.fetch_max_for("KJFK", date(2026, 5, 7)) is None


def test_parse_summary_date():
    d = nws_cli._parse_summary_date(SAMPLE_CLI)
    assert d == date(2026, 5, 7)


def test_parse_max():
    assert nws_cli._parse_max(SAMPLE_CLI) == 101.0


def test_fetch_max_happy_path():
    list_payload = {"@graph": [
        {"id": "abc-final"},
        {"id": "abc-prelim"},
    ]}
    text_payload_final = {"productText": SAMPLE_CLI}

    def fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("/products"):
            return _resp(200, list_payload)
        if url.endswith("/products/abc-final"):
            return _resp(200, text_payload_final)
        return _resp(404, {})

    with patch("nws_cli.requests.get", side_effect=fake_get):
        result = nws_cli.fetch_max_for("KPHX", date(2026, 5, 7))
    assert result == 101.0


def test_fetch_max_returns_none_when_no_match():
    other_text = SAMPLE_CLI.replace("MAY 7 2026", "MAY 6 2026")
    list_payload = {"@graph": [{"id": "x"}]}

    def fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("/products"):
            return _resp(200, list_payload)
        return _resp(200, {"productText": other_text})

    with patch("nws_cli.requests.get", side_effect=fake_get):
        result = nws_cli.fetch_max_for("KPHX", date(2026, 5, 7))
    assert result is None


def test_fetch_max_caches_hits():
    list_payload = {"@graph": [{"id": "x"}]}
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if url.endswith("/products"):
            return _resp(200, list_payload)
        return _resp(200, {"productText": SAMPLE_CLI})

    with patch("nws_cli.requests.get", side_effect=fake_get):
        a = nws_cli.fetch_max_for("KPHX", date(2026, 5, 7))
        n_first = calls["n"]
        b = nws_cli.fetch_max_for("KPHX", date(2026, 5, 7))
        n_second = calls["n"]
    assert a == b == 101.0
    assert n_second == n_first  # segunda llamada no hizo HTTP


def test_fetch_max_handles_500_list():
    with patch("nws_cli.requests.get", return_value=_resp(500, {})):
        assert nws_cli.fetch_max_for("KPHX", date(2026, 5, 7)) is None


# --- sufijo de récord y reports parciales ------------------------------------
# Caso real KDEN 2026-07-25: el CLI final trae "MAXIMUM 101R" (R = record) y el
# CLI matinal del mismo día trae 77 (el día iba en curso). El settle guardó 77.
CLI_DEN_FINAL = """
...THE DENVER CO CLIMATE SUMMARY FOR JULY 25 2026...

TEMPERATURE (F)
 MAXIMUM        101R   240 PM  99    1931  90     11       92
 MINIMUM         71    519 AM  51    1915  61     10       57
"""

CLI_DEN_PARCIAL = """
...THE DENVER CO CLIMATE SUMMARY FOR JULY 25 2026...

TEMPERATURE (F)
 MAXIMUM         77    100 AM 100    1910  90    -13       97
 MINIMUM         71    519 AM  51    1915  61     10       57
"""


def test_parse_max_with_record_suffix():
    assert nws_cli._parse_max(CLI_DEN_FINAL) == 101.0


def test_parse_min_with_record_suffix_line_above():
    assert nws_cli._parse_min(CLI_DEN_FINAL) == 71.0


def test_fetch_prefers_final_over_partial_same_day():
    """El parcial se emite dentro del día objetivo; el final, pasada la
    medianoche local. Sólo el segundo cuenta."""
    list_payload = {"@graph": [
        {"id": "final", "issuanceTime": "2026-07-26T07:39:00+00:00"},
        {"id": "parcial", "issuanceTime": "2026-07-25T12:37:00+00:00"},
    ]}

    def fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("/products"):
            return _resp(200, list_payload)
        if url.endswith("/products/final"):
            return _resp(200, {"productText": CLI_DEN_FINAL})
        return _resp(200, {"productText": CLI_DEN_PARCIAL})

    with patch("nws_cli.requests.get", side_effect=fake_get):
        assert nws_cli.fetch_max_min_for("KDEN", date(2026, 7, 25)) == (101.0, 71.0)


def test_fetch_ignores_partial_when_final_missing():
    """Sin final todavía: None y reintentar, nunca el parcial (era el bug —
    77 en vez de 101)."""
    list_payload = {"@graph": [
        {"id": "parcial", "issuanceTime": "2026-07-25T12:37:00+00:00"},
    ]}

    def fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("/products"):
            return _resp(200, list_payload)
        return _resp(200, {"productText": CLI_DEN_PARCIAL})

    with patch("nws_cli.requests.get", side_effect=fake_get):
        assert nws_cli.fetch_max_min_for("KDEN", date(2026, 7, 25)) == (None, None)


# --- CLI intradía: el parcial como PISO, nunca como settle -------------------
# Mismo día KDEN 2026-07-25. El WFO emitió tres reports: matinal 06:37 (max 77),
# tarde 16:31 (max 101 = el final) y el definitivo 01:39 del día siguiente.
# `fetch_max_min_for` sólo acepta el tercero; `fetch_intraday_max` quiere el más
# reciente de HOY, que a las 16:31 ya sabe el número.
CLI_DEN_TARDE = """
...THE DENVER CO CLIMATE SUMMARY FOR JULY 25 2026...

TEMPERATURE (F)
 MAXIMUM        101R   240 PM  99    1931  90     11       92
 MINIMUM         71    519 AM  51    1915  61     10       57
"""

_LIST_DEN_SAME_DAY = {"@graph": [
    {"id": "tarde", "issuanceTime": "2026-07-25T22:31:00+00:00"},
    {"id": "matinal", "issuanceTime": "2026-07-25T12:37:00+00:00"},
]}


def _den_same_day_get(url, params=None, headers=None, timeout=None):
    if url.endswith("/products"):
        return _resp(200, _LIST_DEN_SAME_DAY)
    if url.endswith("/products/tarde"):
        return _resp(200, {"productText": CLI_DEN_TARDE})
    return _resp(200, {"productText": CLI_DEN_PARCIAL})


def test_intraday_takes_latest_partial_of_today():
    """El de la tarde gana al matinal — es el más reciente del mismo día."""
    with patch("nws_cli.requests.get", side_effect=_den_same_day_get):
        mx, issued = nws_cli.fetch_intraday_max("KDEN", date(2026, 7, 25))
    assert mx == 101.0
    assert issued is not None and issued.hour == 22


def test_intraday_none_when_no_product_for_today():
    list_payload = {"@graph": [
        {"id": "ayer", "issuanceTime": "2026-07-25T07:39:00+00:00"},
    ]}

    def fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("/products"):
            return _resp(200, list_payload)
        return _resp(200, {"productText": CLI_DEN_FINAL})  # reporta el 25

    with patch("nws_cli.requests.get", side_effect=fake_get):
        assert nws_cli.fetch_intraday_max("KDEN", date(2026, 7, 26)) == (None, None)


def test_intraday_cache_is_separate_from_settle_cache():
    """LA guarda de este feature. Si ambos compartieran cache, el parcial de la
    tarde se serviría como settle y volveríamos al bug que dejó KDEN liquidado
    en 77°F. Tras leer el intradía, el settle debe seguir diciendo 'todavía no'."""
    with patch("nws_cli.requests.get", side_effect=_den_same_day_get):
        assert nws_cli.fetch_intraday_max("KDEN", date(2026, 7, 25))[0] == 101.0
        # Ningún producto de la lista fue emitido después de la medianoche
        # local del 26, así que el settle no tiene material válido.
        assert nws_cli.fetch_max_min_for("KDEN", date(2026, 7, 25)) == (None, None)


def test_intraday_respects_ttl():
    calls = []

    def counting_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return _den_same_day_get(url, params, headers, timeout)

    with patch("nws_cli.requests.get", side_effect=counting_get):
        nws_cli.fetch_intraday_max("KDEN", date(2026, 7, 25), now=1000.0)
        n_first = len(calls)
        # dentro del TTL: servido de cache
        nws_cli.fetch_intraday_max("KDEN", date(2026, 7, 25), now=1000.0 + 60)
        assert len(calls) == n_first
        # pasado el TTL: vuelve a pegarle a la API (el CLI de la tarde puede
        # ser seguido por otro con el max ya subido)
        nws_cli.fetch_intraday_max(
            "KDEN", date(2026, 7, 25), now=1000.0 + nws_cli.INTRADAY_TTL_S + 1)
        assert len(calls) > n_first


def test_intraday_caches_negative_result():
    """Sin CLI de hoy no se repega a la API cada 3 min durante el pico."""
    calls = []

    def counting_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return _resp(200, {"@graph": []})

    with patch("nws_cli.requests.get", side_effect=counting_get):
        nws_cli.fetch_intraday_max("KDEN", date(2026, 7, 25), now=500.0)
        n = len(calls)
        nws_cli.fetch_intraday_max("KDEN", date(2026, 7, 25), now=500.0 + 30)
        assert len(calls) == n


def test_intraday_unsupported_station():
    assert nws_cli.fetch_intraday_max("KJFK", date(2026, 7, 25)) == (None, None)


# --- CF6 (F-6) ---------------------------------------------------------------
# Recorte del producto real CF6PHX del 2026-07-26 (filas 1, 20, 24, 25) más una
# fila sintética con MAX/MIN missing y el arranque de la página 2, que repite el
# header MONTH/YEAR y no debe contaminar el parseo.
SAMPLE_CF6 = """
000
CXUS55 KPSR 261005
CF6PHX
PRELIMINARY LOCAL CLIMATOLOGICAL DATA (WS FORM: F-6)

                                          STATION:   PHOENIX AZ
                                          MONTH:     JULY
                                          YEAR:      2026

  TEMPERATURE IN F:       :PCPN:    SNOW:  WIND      :SUNSHINE: SKY     :PK WND
================================================================================
1   2   3   4   5  6A  6B    7    8   9   10  11  12  13   14  15   16   17  18
                                     12Z  AVG MX 2MIN
DY MAX MIN AVG DEP HDD CDD  WTR  SNW DPTH SPD SPD DIR MIN PSBL S-S WX    SPD DR
================================================================================

 1 102  75  89  -6   0  24 0.00  0.0    0  7.2 18 190   M    M   1        27 160
20 107  82  95  -1   0  30    T  0.0    0  7.6  M   M   M    M   7 378     M   M
24 117  97 107  11   0  42 0.00  0.0    0  8.0 15 270   M    M   4        26 330
25 117  95 106  10   0  41 0.00  0.0    M 10.1 39  60   M    M   4 3      50  50
26   M   M   M   M   M   M    M    M    M    M  M   M   M    M   M         M   M
================================================================================
SM 2687 2135         0 792  0.44  0.0    194.1          M      123
================================================================================
NOTES:

PRELIMINARY LOCAL CLIMATOLOGICAL DATA (WS FORM: F-6) , PAGE 2

                                          STATION:   PHOENIX AZ
                                          MONTH:     JULY
                                          YEAR:      2026

AVERAGE MONTHLY: 96.4   TOTAL FOR MONTH:   0.44    1 = FOG OR MIST
HIGHEST:   117 ON 25,24  GRTST 24HR  0.38 ON 13-13
LOWEST:     75 ON  1                               3 = THUNDER
"""


def test_parse_cf6_month_and_days():
    ym, days = nws_cli._parse_cf6(SAMPLE_CF6)
    assert ym == (2026, 7)
    assert days[1] == (102.0, 75.0)
    assert days[24] == (117.0, 97.0)
    assert days[25] == (117.0, 95.0)


def test_parse_cf6_missing_is_none():
    _, days = nws_cli._parse_cf6(SAMPLE_CF6)
    assert days[26] == (None, None)


def test_parse_cf6_stops_before_page_two():
    """La página 2 trae '1 = FOG OR MIST' y 'LOWEST: 75 ON 1'; si el bloque no
    se cierra en el '====' esas líneas reescribirían el día 1."""
    _, days = nws_cli._parse_cf6(SAMPLE_CF6)
    assert days[1] == (102.0, 75.0)
    assert set(days) == {1, 20, 24, 25, 26}


def _cf6_fake_get(url, params=None, headers=None, timeout=None):
    if url.endswith("/products"):
        assert params["type"] == "CF6"
        return _resp(200, {"@graph": [{"id": "cf6-jul"}]})
    return _resp(200, {"productText": SAMPLE_CF6})


def test_fetch_month_extremes_happy_path():
    with patch("nws_cli.requests.get", side_effect=_cf6_fake_get):
        got = nws_cli.fetch_month_extremes("KPHX", 2026, 7)
    assert got["2026-07-24"] == (117.0, 97.0)
    assert got["2026-07-26"] == (None, None)


def test_fetch_max_min_cf6_recovers_expired_day():
    """El caso que motivó el feature: el CLI de KPHX 2026-07-24 ya no existe en
    /products, pero el CF6 del mes sigue trayendo el día."""
    with patch("nws_cli.requests.get", side_effect=_cf6_fake_get):
        assert nws_cli.fetch_max_min_cf6("KPHX", date(2026, 7, 24)) == (117.0, 97.0)


def test_fetch_month_extremes_other_month_is_empty():
    with patch("nws_cli.requests.get", side_effect=_cf6_fake_get):
        assert nws_cli.fetch_month_extremes("KPHX", 2026, 6) == {}


def test_fetch_month_extremes_caches_by_month():
    calls = {"n": 0}

    def counting_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        return _cf6_fake_get(url, params, headers, timeout)

    with patch("nws_cli.requests.get", side_effect=counting_get):
        nws_cli.fetch_month_extremes("KPHX", 2026, 7)
        n_first = calls["n"]
        nws_cli.fetch_max_min_cf6("KPHX", date(2026, 7, 25))
    assert calls["n"] == n_first  # el segundo día sale del cache del mes


def test_fetch_month_extremes_unsupported_station():
    assert nws_cli.fetch_month_extremes("KJFK", 2026, 7) == {}
