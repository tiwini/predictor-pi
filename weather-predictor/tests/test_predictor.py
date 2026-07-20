import predictor


def test_peak_hours_has_all_curated_stations():
    expected = {"KPHX", "KLAX", "KLAS", "KLGA"}
    assert expected.issubset(predictor.PEAK_HOURS.keys())


def test_peak_hours_well_formed():
    for sid, (lo, hi) in predictor.PEAK_HOURS.items():
        assert 0 <= lo < hi <= 23, f"{sid}: bad window {lo}-{hi}"


def test_sigma_tight_in_peak_window():
    # Each station's peak hours should use σ=1.5 (tightest).
    for sid, (lo, hi) in predictor.PEAK_HOURS.items():
        for h in range(lo, hi):
            assert predictor.sigma_for_hour(h, sid) == 1.5, (
                f"{sid} h={h}: expected σ=1.5 in peak"
            )


def test_sigma_widens_away_from_peak():
    # KPHX peak is 14-17. σ at 14 (peak) < σ at 9 (5h before) < σ at 3 (far).
    sid = "KPHX"
    assert predictor.sigma_for_hour(14, sid) == 1.5
    assert predictor.sigma_for_hour(12, sid) == 2.0   # ≤2h away
    assert predictor.sigma_for_hour(10, sid) == 2.5   # ≤4h away
    assert predictor.sigma_for_hour(3, sid) == 3.5    # far


def test_sigma_monotone_non_decreasing_with_distance():
    sid = "KLGA"
    lo, hi = predictor.PEAK_HOURS[sid]
    peak_mid = (lo + hi) // 2
    prev = 0.0
    for dist in range(0, 10):
        # Only check one side to avoid wraparound ambiguity.
        h = peak_mid + dist
        if h > 23:
            break
        s = predictor.sigma_for_hour(h, sid)
        assert s >= prev, f"σ decreased at h={h} dist={dist}"
        prev = s


def test_sigma_fallback_for_unknown_station():
    # Unknown stations use the default (12, 16) window.
    assert predictor.sigma_for_hour(13, "KUNKNOWN") == 1.5
    assert predictor.sigma_for_hour(3, "KUNKNOWN") == 3.5


def test_invalidate_obs_cache_is_targeted():
    # Only fetch_current / fetch_today_obs get cleared.
    predictor._FETCH_CACHE[("fetch_current", "KPHX")] = (0.0, "obs")
    predictor._FETCH_CACHE[("fetch_today_obs", "KPHX")] = (0.0, "obs")
    predictor._FETCH_CACHE[("fetch_ensemble", "KPHX")] = (0.0, "ens")
    predictor._FETCH_CACHE[("fetch_current", "KLAX")] = (0.0, "obs")

    predictor.invalidate_obs_cache("KPHX")

    assert ("fetch_current", "KPHX") not in predictor._FETCH_CACHE
    assert ("fetch_today_obs", "KPHX") not in predictor._FETCH_CACHE
    # Ensemble preserved (it's the expensive fetch).
    assert ("fetch_ensemble", "KPHX") in predictor._FETCH_CACHE
    # Other stations untouched.
    assert ("fetch_current", "KLAX") in predictor._FETCH_CACHE


# L2 Fable 2026-07-20: convective_ambient parser (TS/CB/TSRA/TCU/GR/VCTS)
def test_parse_convective_flags_ts():
    raw = "KMIA 191953Z 12010KT 6SM TSRA SCT035CB BKN060 27/24 A2988 RMK AO2 TSB19 SLP116 T02720239"
    assert predictor.parse_convective_flags(raw) is True

def test_parse_convective_flags_vcts():
    raw = "KMIA 191553Z 15008KT 10SM VCTS SCT045 30/22 A2990"
    assert predictor.parse_convective_flags(raw) is True

def test_parse_convective_flags_cb():
    raw = "KMIA 191553Z 15008KT 10SM SCT045CB 30/22 A2990"
    assert predictor.parse_convective_flags(raw) is True

def test_parse_convective_flags_tcu():
    raw = "KMIA 191553Z 15008KT 10SM SCT045TCU 30/22 A2990"
    assert predictor.parse_convective_flags(raw) is True

def test_parse_convective_flags_clear():
    raw = "KPHX 191553Z 00000KT 10SM CLR 42/05 A2988"
    assert predictor.parse_convective_flags(raw) is False

def test_parse_convective_flags_empty():
    assert predictor.parse_convective_flags("") is False
    assert predictor.parse_convective_flags(None) is False

def test_parse_convective_flags_scattered_no_convection():
    # SCT045 sin CB/TCU no debe disparar
    raw = "KLAX 191553Z 24006KT 10SM SCT045 22/15 A2998"
    assert predictor.parse_convective_flags(raw) is False
