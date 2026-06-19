import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import predictor_web as web


BASE_TS = 1_800_000_000.0


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _iso(ts):
    return "2027-01-15T08:00:00Z" if ts == BASE_TS else "2027-01-15T07:59:57Z"


def _payloads(ts_by_name=None, mids=None, volumes=None):
    ts_by_name = ts_by_name or {}
    mids = mids or {}
    volumes = volumes or {}
    out = {}
    for name in ["CB", "KR", "BS", "GE"]:
        mid = mids.get(name, 100_000.0)
        bid = mid - 1.0
        ask = mid + 1.0
        vol = volumes.get(name, 10.0)
        ts = ts_by_name.get(name, BASE_TS)
        if name == "CB":
            out[name] = {"bid": str(bid), "ask": str(ask),
                         "volume": str(vol), "time": _iso(ts)}
        elif name == "KR":
            out[name] = {"result": {"XXBTZUSD": {"a": [str(ask)],
                                                    "b": [str(bid)],
                                                    "v": ["1", str(vol)]}}}
        elif name == "BS":
            out[name] = {"bid": str(bid), "ask": str(ask),
                         "volume": str(vol), "timestamp": str(int(ts))}
        elif name == "GE":
            out[name] = {"bid": str(bid), "ask": str(ask),
                         "volume": {"BTC": str(vol),
                                      "timestamp": int(ts * 1000)}}
    return out


def _mock_get(monkeypatch, payloads):
    def fake_get(url, *args, **kwargs):
        if "coinbase" in url:
            return FakeResponse(payloads["CB"])
        if "kraken" in url:
            return FakeResponse(payloads["KR"])
        if "bitstamp" in url:
            return FakeResponse(payloads["BS"])
        if "gemini" in url:
            return FakeResponse(payloads["GE"])
        raise AssertionError(url)

    monkeypatch.setattr(web.time, "time", lambda: BASE_TS)
    import requests
    monkeypatch.setattr(requests, "get", fake_get)


def test_brti_proxy_uses_equal_median_and_keeps_volume_meta(monkeypatch):
    payloads = _payloads(
        mids={"CB": 100_000.0, "KR": 100_010.0,
              "BS": 100_020.0, "GE": 101_000.0},
        volumes={"GE": 1_000_000.0},
    )
    _mock_get(monkeypatch, payloads)

    out = web._fetch_brti_proxy()

    assert out is not None
    assert out["n"] == 4
    assert out["mid"] == 100_015.0
    assert out["weighting"] == "equal_median"
    assert out["venues"]["GE"]["volume_24h"] == 1_000_000.0
    assert out["divergence_warning"] is True


def test_brti_proxy_filters_stale_venue_before_median(monkeypatch):
    payloads = _payloads(
        ts_by_name={"BS": BASE_TS - 3.0},
        mids={"CB": 100_000.0, "KR": 100_010.0,
              "BS": 99_000.0, "GE": 100_020.0},
    )
    _mock_get(monkeypatch, payloads)

    out = web._fetch_brti_proxy()

    assert out is not None
    assert out["sources"] == ["CB", "GE", "KR"]
    assert out["stale_sources"] == ["BS"]
    assert out["stale_warning_minor"] is True
    assert out["stale_warning_critical"] is False
    assert out["stale_warning"] is False
    assert out["mid"] == 100_010.0
    assert out["venues"]["BS"]["fresh"] is False


def test_brti_proxy_returns_none_when_fresh_sources_below_two(monkeypatch):
    payloads = _payloads(ts_by_name={
        "CB": BASE_TS - 3.0,
        "BS": BASE_TS - 4.0,
        "GE": BASE_TS - 5.0,
    })
    _mock_get(monkeypatch, payloads)

    assert web._fetch_brti_proxy() is None


def test_brti_proxy_divergence_warning_threshold(monkeypatch):
    payloads = _payloads(mids={
        "CB": 100_000.0,
        "KR": 100_010.0,
        "BS": 100_020.0,
        "GE": 100_030.0,
    })
    _mock_get(monkeypatch, payloads)

    out = web._fetch_brti_proxy()

    assert out is not None
    assert out["spread_bps"] < 5.0
    assert out["divergence_warning"] is False



def test_build_intra15_accepts_brti_meta_and_exposes_warnings():
    pred = web._pred.Prediction(
        symbol="BTCUSDT",
        now_price=100_000.0,
        sigma_1m=0.001,
        sigma_horizon=0.01,
        horizon_min=60,
        fetched_at=BASE_TS,
        n_candles=500,
        target_at=BASE_TS + 3600,
    )
    meta = {
        "spread_bps": 7.5,
        "stale_warning_minor": True,
        "stale_warning_critical": False,
        "divergence_warning": True,
    }

    out = web._build_intra15(pred, 100_500.0, brti_mid=99_900.0, brti_meta=meta)

    assert out is not None
    assert out["brti_spread_bps"] == 7.5
    assert out["stale_warning_minor"] is True
    assert out["stale_warning_critical"] is False
    assert out["stale_warning"] is False
    assert out["divergence_warning"] is True
