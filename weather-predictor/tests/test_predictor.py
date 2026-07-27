import predictor


def test_peak_hours_has_all_curated_stations():
    expected = {"KPHX", "KLAX", "KLAS", "KNYC"}
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
    sid = "KNYC"
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


# ── slot METAR (fix 2026-07-25: whitelist (51,53,54) perdía :52 y :56) ──

def test_metar_slot_accepts_measured_station_minutes():
    # Medidos sobre 3 días × 20 estaciones, obs CON rawMessage.
    for m in (51, 52, 53, 54, 56):
        assert predictor.is_metar_slot_minute(m), f":{m} debe aceptarse"


def test_metar_slot_rejects_five_minute_feed():
    # El feed automático publica en múltiplos de 5 — esos no son METAR.
    for m in (0, 5, 15, 30, 45, 50, 55):
        assert not predictor.is_metar_slot_minute(m), f":{m} no es METAR"


def test_metar_slot_rejects_first_three_quarters_of_hour():
    for m in (1, 12, 26, 33, 44):
        assert not predictor.is_metar_slot_minute(m)


# ── high-water mark de max_obs (el feed retira obs entre polls) ──

def test_max_obs_high_water_holds_after_feed_retracts():
    from datetime import date, datetime, timezone
    predictor._MAX_OBS_HWM.clear()
    day = date(2026, 7, 25)
    t1 = datetime(2026, 7, 25, 21, 56, tzinfo=timezone.utc)
    v, ts = predictor._max_obs_high_water("KLAS", day, 111.9, t1)
    assert (v, ts) == (111.9, t1)
    # Siguiente poll: el feed ya no sirve la obs de 111.9.
    v, ts = predictor._max_obs_high_water("KLAS", day, 109.0, None)
    assert v == 111.9 and ts == t1, "el max del día no puede bajar"


def test_max_obs_high_water_still_rises():
    from datetime import date, datetime, timezone
    predictor._MAX_OBS_HWM.clear()
    day = date(2026, 7, 25)
    predictor._max_obs_high_water("KLAS", day, 109.0, None)
    t2 = datetime(2026, 7, 25, 22, 56, tzinfo=timezone.utc)
    v, ts = predictor._max_obs_high_water("KLAS", day, 113.0, t2)
    assert (v, ts) == (113.0, t2)


def test_max_obs_high_water_resets_next_day():
    from datetime import date
    predictor._MAX_OBS_HWM.clear()
    predictor._max_obs_high_water("KLAS", date(2026, 7, 25), 113.0, None)
    v, _ = predictor._max_obs_high_water("KLAS", date(2026, 7, 26), 80.0, None)
    assert v == 80.0, "el día nuevo arranca limpio"
    assert all(k[1] == date(2026, 7, 26) for k in predictor._MAX_OBS_HWM)


def test_max_obs_high_water_ignores_sentinel():
    from datetime import date
    predictor._MAX_OBS_HWM.clear()
    v, _ = predictor._max_obs_high_water("KLAS", date(2026, 7, 25), -999, None)
    assert v == -999
    assert not predictor._MAX_OBS_HWM


def test_obs_floor_lifts_members_below_observed_max():
    """Los miembros nacen con piso en max_obs, pero bias/ext_shift restan grados
    y lo rompen. El clamp lo re-impone sobre la distribución entera."""
    out, n, delta = predictor.apply_obs_floor([88.0, 90.0, 91.0, 93.0], 91.0)
    assert out == [91.0, 91.0, 91.0, 93.0]
    assert n == 2
    assert delta == 3.0          # el peor miembro venía 3°F por debajo


def test_obs_floor_noop_when_all_above():
    out, n, delta = predictor.apply_obs_floor([92.0, 93.0], 91.0)
    assert out == [92.0, 93.0]
    assert (n, delta) == (0, None)


def test_obs_floor_ignores_missing_floor():
    """Sin obs válida (sentinel -999 ya filtrado por el caller) no se toca nada."""
    out, n, delta = predictor.apply_obs_floor([88.0, 90.0], None)
    assert out == [88.0, 90.0]
    assert (n, delta) == (0, None)


def test_obs_floor_empty_distribution():
    assert predictor.apply_obs_floor([], 91.0) == ([], 0, None)


def test_obs_floor_preserves_length_for_weighted_members():
    """daily_maxes viene con repetición por peso bayesiano: el clamp no puede
    cambiar el tamaño de la distribución o los percentiles se corren."""
    dist = [85.0] * 10 + [92.0] * 23
    out, n, _ = predictor.apply_obs_floor(dist, 90.0)
    assert len(out) == len(dist) == 33
    assert n == 10
    assert min(out) == 90.0


# --- ventana de fetch del CLI intradía ---------------------------------------

def _local(station, hour, minute=0):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from stations import STATION_TZ
    return datetime(2026, 7, 25, hour, minute,
                    tzinfo=ZoneInfo(STATION_TZ[station]))


def test_cli_window_opens_half_hour_before_expected_issue():
    # KDEN emite ~16:32 local, o sea la ventana abre 16:02.
    assert predictor.cli_window_open("KDEN", _local("KDEN", 16, 10))
    assert not predictor.cli_window_open("KDEN", _local("KDEN", 15, 50))


def test_cli_window_closed_at_morning_cli_hour():
    """El CLI matinal (~06:30) trae el max hasta esa hora. Entra vía max() sin
    hacer daño, pero pedirlo es un request regalado."""
    assert not predictor.cli_window_open("KPHX", _local("KPHX", 6, 30))


def test_cli_window_is_per_station():
    """KOKC emite a las 16:11 y KLAX a las 18:38: una hora fija no sirve."""
    assert predictor.cli_window_open("KOKC", _local("KOKC", 16, 30))
    assert not predictor.cli_window_open("KLAX", _local("KLAX", 16, 30))
    assert predictor.cli_window_open("KLAX", _local("KLAX", 19))


def test_cli_window_stays_open_until_end_of_local_day():
    """Cerrarla 4h después de la emisión dejó a KMSY y KDCA prediciendo 1°F por
    debajo del CLI ya publicado, a las 21-22h local. El settle todavía no
    existe a esa hora, así que el parcial sigue siendo la mejor fuente."""
    assert predictor.cli_window_open("KMIA", _local("KMIA", 23, 0))
    assert predictor.cli_window_open("KDCA", _local("KDCA", 22, 14))
    assert predictor.cli_window_open("KMSY", _local("KMSY", 21, 10))


def test_cli_window_unknown_station_never_opens():
    assert not predictor.cli_window_open("KJFK", _local("KPHX", 17, 30))


# --- el piso incluye la temperatura actual -----------------------------------

def test_current_floor_margin_is_half_a_celsius_step():
    """0.9°F = medio escalón de °C. Si alguien lo "afina" a ojo, el piso pasa a
    apoyarse en el redondeo del feed en vez de en el extremo inferior real."""
    assert predictor.CURRENT_FLOOR_MARGIN_F == 0.9


def test_floor_lifts_when_metar_lags_behind_current():
    """Caso KPHL 2026-07-27: METAR de hace 90 min en 86.0 y la temperatura
    actual en 87.8. El máximo del día no puede ser 86.0."""
    max_obs, current = 86.0, 87.8
    floor = max(max_obs, current - predictor.CURRENT_FLOOR_MARGIN_F)
    assert abs(floor - 86.9) < 1e-9      # 87.8-0.9 no es exacto en binario
    out, n, _ = predictor.apply_obs_floor([86.0, 86.5, 88.0], floor)
    assert n == 2
    assert all(abs(v - floor) < 1e-9 for v in out[:2])
    assert out[2] == 88.0


def test_current_floor_never_lowers_the_floor():
    """Con el METAR al día, `current` por debajo no debe bajar nada."""
    max_obs, current = 91.0, 88.0
    floor = max(max_obs, current - predictor.CURRENT_FLOOR_MARGIN_F)
    assert floor == 91.0


def test_current_floor_respects_the_rounding_margin():
    """Un current de 87.8 NO justifica un piso de 87.8: el valor real puede ser
    86.9 (el feed llega en °C enteros)."""
    floor = 0.0 - predictor.CURRENT_FLOOR_MARGIN_F + 87.8
    assert abs(floor - 86.9) < 1e-9
    assert floor < 87.8


def test_cli_hour_is_the_FIRST_afternoon_report_not_the_last():
    """Varios WFO emiten 2-3 CLI al día. La tabla original guardaba el ÚLTIMO y
    el poller perdía hasta 3.9h de piso duro (KATL: 16:40 vs 20:35). Tomar el
    primero es seguro porque el CLI sólo sube el piso y nunca sobreestima."""
    from stations import CLI_LATE_HOUR
    # las 7 que estaban mal, con la hora del primer report de la tarde
    for st, hi in (("KATL", 17.0), ("KMSP", 17.0), ("KDFW", 17.0),
                   ("KSAT", 17.0), ("KAUS", 17.0), ("KDEN", 17.0),
                   ("KOKC", 17.0)):
        assert CLI_LATE_HOUR[st] < hi, (
            f"{st}: {CLI_LATE_HOUR[st]} parece el CLI tardío, no el primero")
