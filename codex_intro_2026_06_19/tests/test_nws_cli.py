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
