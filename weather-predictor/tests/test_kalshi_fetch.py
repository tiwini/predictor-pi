"""Tests para kalshi.fetch_bins con la API real mockeada."""
from datetime import date
from unittest.mock import patch

import kalshi


def _mk_response(status_code=200, payload=None):
    class R:
        def __init__(self):
            self.status_code = status_code
            self._payload = payload or {}
        def json(self):
            return self._payload
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")
    return R()


def test_series_for_known_and_unknown():
    assert kalshi.series_for("KPHX") == "KXHIGHTPHX"
    assert kalshi.series_for("klga") == "KXHIGHNY"
    assert kalshi.series_for("KJFK") is None


def test_event_ticker_format():
    assert kalshi.event_ticker_for("KXHIGHNY", date(2026, 4, 19)) == "KXHIGHNY-26APR19"
    assert kalshi.event_ticker_for("KXHIGHTPHX", date(2026, 12, 1)) == "KXHIGHTPHX-26DEC01"


def test_fetch_bins_unsupported_station_returns_empty():
    assert kalshi.fetch_bins("KJFK", date(2026, 4, 20)) == []


def test_fetch_bins_404_returns_empty():
    with patch("kalshi.requests.get", return_value=_mk_response(404)):
        assert kalshi.fetch_bins("KPHX", date(2026, 4, 20)) == []


def test_fetch_bins_parses_b_and_t_markets():
    payload = {"markets": [
        {"ticker": "KXHIGHTPHX-26APR20-B100.5",
         "yes_sub_title": "100° to 101°",
         "yes_bid_dollars": 0.40, "yes_ask_dollars": 0.42},
        {"ticker": "KXHIGHTPHX-26APR20-T98",
         "yes_sub_title": "97° or below",
         "yes_bid_dollars": 0.05, "yes_ask_dollars": 0.07},
        {"ticker": "KXHIGHTPHX-26APR20-T105",
         "yes_sub_title": "106° or above",
         "yes_bid_dollars": 0.10, "yes_ask_dollars": 0.12},
        {"ticker": "garbage-no-suffix", "yes_sub_title": "x"},
    ]}
    with patch("kalshi.requests.get", return_value=_mk_response(200, payload)):
        bins = kalshi.fetch_bins("KPHX", date(2026, 4, 20))
    assert len(bins) == 3
    assert bins[0].bin_lo == float("-inf") and bins[0].bin_hi == 97
    assert bins[1].bin_lo == 100 and bins[1].bin_hi == 101
    assert bins[2].bin_lo == 106 and bins[2].bin_hi == float("inf")
    assert abs(bins[1].yes_mid - 0.41) < 1e-9


def test_fetch_bins_handles_missing_prices():
    payload = {"markets": [
        {"ticker": "KXHIGHTPHX-26APR20-B100.5",
         "yes_sub_title": "100° to 101°"},
    ]}
    with patch("kalshi.requests.get", return_value=_mk_response(200, payload)):
        bins = kalshi.fetch_bins("KPHX", date(2026, 4, 20))
    assert len(bins) == 1
    assert bins[0].yes_mid is None
