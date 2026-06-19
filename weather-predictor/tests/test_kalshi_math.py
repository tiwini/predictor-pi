import math

import kalshi


def _bin(lo, hi, yes_mid, ticker="T"):
    return {"bin_lo": lo, "bin_hi": hi, "yes_mid": yes_mid,
            "ticker": ticker, "label": ""}


def test_our_p_for_bin_uniform_ensemble():
    ens = [60.0, 61.0, 62.0, 63.0, 64.0]
    # Bin covering 61-62 should catch 2 of 5 members.
    assert kalshi.our_p_for_bin(ens, 61, 62) == 2 / 5


def test_our_p_for_bin_uses_half_integer_edges():
    # NWS integers; ensemble values exactly at edge go to the bin below it.
    ens = [60.5, 61.5, 62.5]
    p = kalshi.our_p_for_bin(ens, 61, 62)
    assert math.isclose(p, 2 / 3)


def test_our_p_for_bin_open_tails():
    ens = [50.0, 70.0, 90.0]
    assert kalshi.our_p_for_bin(ens, float("-inf"), 60) == 1 / 3
    assert kalshi.our_p_for_bin(ens, 80, float("inf")) == 1 / 3


def test_our_p_for_bin_empty_ensemble():
    assert kalshi.our_p_for_bin([], 60, 62) == 0.0


def test_implied_prob_above_simple():
    bins = [
        _bin(60, 61, 0.5),
        _bin(62, 63, 0.3),
        _bin(64, 65, 0.2),
    ]
    # Threshold 61: bin (60,61) contributes 0 (no t > 61 in it);
    # (62,63) contributes full 0.3; (64,65) contributes full 0.2 → 0.5.
    assert math.isclose(kalshi.implied_prob_above(bins, 61), 0.5)


def test_implied_prob_above_is_clamped():
    # Malformed bins summing > 1 should still return ≤ 1.
    bins = [_bin(60, 61, 0.9), _bin(62, 63, 0.9)]
    assert kalshi.implied_prob_above(bins, 59) <= 1.0


def test_implied_prob_above_tail_guard_returns_none():
    # Threshold falls inside a heavy tail → ambiguous, return None.
    bins = [
        _bin(float("-inf"), 50, 0.5),
        _bin(60, 61, 0.3),
    ]
    assert kalshi.implied_prob_above(bins, 40) is None


def test_implied_prob_above_tiny_tail_ignored():
    # Tail with < 2% mass should not trigger the guard.
    bins = [
        _bin(float("-inf"), 50, 0.005),
        _bin(60, 61, 0.5),
        _bin(62, 63, 0.4),
    ]
    out = kalshi.implied_prob_above(bins, 40)
    assert out is not None


def test_implied_prob_above_empty_bins():
    assert kalshi.implied_prob_above([], 50) is None


def test_implied_prob_above_accepts_marketbin_objects():
    bins = [
        kalshi.MarketBin("a", 60, 61, "", None, None, 0.4),
        kalshi.MarketBin("b", 62, 63, "", None, None, 0.6),
    ]
    # Threshold 61: first contributes 0, second full 0.6.
    assert math.isclose(kalshi.implied_prob_above(bins, 61), 0.6)


def test_parse_ticker_bin_integer_pair():
    assert kalshi._parse_ticker_bin("KXHIGHNY-26APR20-B54.5", "54° to 55°") == (54.0, 55.0)


def test_parse_ticker_bin_low_tail():
    assert kalshi._parse_ticker_bin("KXHIGHNY-26APR20-T54", "53° or below") == (float("-inf"), 53.0)


def test_parse_ticker_bin_high_tail():
    assert kalshi._parse_ticker_bin("KXHIGHNY-26APR20-T61", "62° or above") == (61.0, float("inf"))


def test_parse_ticker_bin_malformed_returns_none():
    assert kalshi._parse_ticker_bin("garbage", "whatever") is None
