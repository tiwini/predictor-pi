import difficulty


def _call(**overrides):
    base = dict(
        ens_p10=70.0, ens_p90=74.0,  # tight spread
        eff_n=28.0, total_members=31,
        clim_percentile=50.0,
        p_notable_precip=0.05,
    )
    base.update(overrides)
    return difficulty.compute(**base)


def test_easy_day_when_all_quiet():
    d = _call()
    assert d.label == "fácil"
    assert d.recommend_skip is False
    assert d.reasons == []


def test_wide_spread_triggers_hard():
    d = _call(ens_p10=60.0, ens_p90=78.0)  # 18°F spread
    assert d.score >= 55
    assert d.recommend_skip is True
    assert any("ensemble" in r for r in d.reasons)


def test_low_eff_n_flags_regime_shift():
    d = _call(eff_n=5.0)  # heavy reweight collapse
    assert d.score >= 55
    assert any("reweight" in r for r in d.reasons)


def test_climatological_extreme_triggers():
    d = _call(clim_percentile=97.0)
    assert d.score >= 55
    assert any("anomalía" in r for r in d.reasons)


def test_rain_day_triggers():
    d = _call(p_notable_precip=0.50)
    assert d.score >= 55
    assert any("precipitación" in r for r in d.reasons)


def test_missing_inputs_do_not_crash():
    d = difficulty.compute(
        ens_p10=None, ens_p90=None,
        eff_n=None, total_members=0,
        clim_percentile=None, p_notable_precip=None,
    )
    assert d.score == 0.0
    assert d.label == "fácil"


def test_overall_takes_max_not_mean():
    # One strong red flag dominates even if others are clean.
    d = _call(ens_p10=60.0, ens_p90=80.0)  # 20°F → spread maxed
    assert d.score == 100.0


def test_score_monotonic_in_spread():
    a = _call(ens_p10=70.0, ens_p90=75.0)  # 5°F
    b = _call(ens_p10=68.0, ens_p90=78.0)  # 10°F
    c = _call(ens_p10=65.0, ens_p90=80.0)  # 15°F
    assert a.score <= b.score <= c.score


def test_very_hard_label_at_high_score():
    d = _call(ens_p10=60.0, ens_p90=80.0, eff_n=3.0,
              clim_percentile=98.0, p_notable_precip=0.7)
    assert d.label == "muy difícil"
    assert d.recommend_skip is True
    assert len(d.reasons) >= 3


def test_regime_break_forces_max_score():
    d = _call(regime_breaks=2)
    assert d.score == 100.0
    assert d.label == "muy difícil"
    assert d.recommend_skip is True
    assert any("ruptura" in r for r in d.reasons)


def test_regime_break_reason_leads():
    d = _call(ens_p10=60.0, ens_p90=78.0, regime_breaks=3)
    assert d.reasons[0].startswith("ruptura")


def test_single_regime_break_does_not_trigger():
    d = _call(regime_breaks=1)
    assert d.score < 100.0
    assert not any("ruptura" in r for r in d.reasons)
