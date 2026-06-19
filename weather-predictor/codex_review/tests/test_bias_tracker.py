"""Tests for the per-station rolling bias tracker.

Use a temporary SQLite file so we can seed (date, actual, early_pred)
triples directly without depending on the live calibration DB.
"""
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import bias_tracker as bt


def _seed(db_path: Path, station: str, rows):
    """rows: list of (date_str, actual_max_f, early_pred_f).
    early_pred_f may be None to simulate missing snapshot."""
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE day_outcomes (
            station_id TEXT, date TEXT, max_obs_f REAL, settled_at TEXT,
            PRIMARY KEY (station_id, date)
        );
        CREATE TABLE prediction_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT, date TEXT, snapshot_time TEXT,
            slot INTEGER, is_auto INTEGER,
            expr TEXT, op TEXT, threshold REAL, bin_half REAL,
            predicted_p REAL, outcome INTEGER
        );
    """)
    for d, actual, pred in rows:
        cur.execute(
            "INSERT INTO day_outcomes (station_id,date,max_obs_f,settled_at) VALUES (?,?,?,?)",
            (station, d, float(actual), "2026-01-01T00:00:00+00:00"),
        )
        if pred is not None:
            cur.execute(
                "INSERT INTO prediction_snapshots "
                "(station_id,date,snapshot_time,slot,is_auto,expr,op,threshold,predicted_p) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (station, d, f"{d}T12:00:00+00:00", 0, 1,
                 "auto", "~", float(pred), 0.5),
            )
    con.commit()
    con.close()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "calibration.db"


def test_no_data_returns_zero_not_applied(db):
    _seed(db, "KBOS", [])
    r = bt.compute_bias("KBOS", today=date(2026, 5, 1), db_path=db)
    assert r["bias"] == 0.0
    assert r["applied"] is False
    assert r["n"] == 0


def test_below_min_days_not_applied(db):
    _seed(db, "KBOS", [
        ("2026-04-28", 50, 53),
        ("2026-04-27", 52, 55),
    ])
    r = bt.compute_bias("KBOS", today=date(2026, 5, 1), db_path=db)
    assert r["applied"] is False
    assert r["n"] == 2
    assert "insufficient" in r["reason"]


def test_consistent_warm_bias_applied(db):
    _seed(db, "KBOS", [
        ("2026-04-28", 50, 52),
        ("2026-04-27", 52, 54),
        ("2026-04-26", 54, 56),
        ("2026-04-25", 48, 50),
        ("2026-04-24", 50, 52),
    ])
    r = bt.compute_bias("KBOS", today=date(2026, 5, 1), db_path=db)
    assert r["applied"] is True
    # Regime-break activates → mean × SHRINK (0.5) → 2.0 × 0.5 = 1.0
    assert r["bias"] == pytest.approx(1.0, abs=0.01)
    assert r["n"] == bt.REGIME_K
    assert r["regime_break"] is True


def test_consistent_cold_bias_negative(db):
    _seed(db, "KPHX", [
        ("2026-04-28", 86, 82),
        ("2026-04-27", 81, 77),
        ("2026-04-26", 79, 75),
        ("2026-04-25", 84, 80),
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 5, 1), db_path=db)
    assert r["applied"] is True
    # mean -4 × SHRINK 0.5 = -2.0 (no llega al cap de -2.5)
    assert r["bias"] == pytest.approx(-2.0, abs=0.01)


def test_balanced_signs_below_threshold(db):
    # +2, -2, +2, -2 averages to 0 → below threshold → not applied
    _seed(db, "KLGA", [
        ("2026-04-28", 50, 52),
        ("2026-04-27", 60, 58),
        ("2026-04-26", 55, 57),
        ("2026-04-25", 65, 63),
    ])
    r = bt.compute_bias("KLGA", today=date(2026, 5, 1), db_path=db)
    assert r["applied"] is False
    assert abs(r["bias"]) < bt.APPLY_THRESHOLD


def test_recent_days_weighted_more(db):
    # Old days bias +5, last day bias 0 → exponential weight should pull
    # the average noticeably below 5 (recent dominates)
    _seed(db, "KBOS", [
        ("2026-04-28", 50, 50),  # most recent: 0 err
        ("2026-04-27", 50, 55),  # +5
        ("2026-04-26", 50, 55),
        ("2026-04-25", 50, 55),
        ("2026-04-24", 50, 55),
    ])
    r = bt.compute_bias("KBOS", today=date(2026, 5, 1), db_path=db)
    assert r["applied"] is True
    # plain mean would be 4.0; exponential with α=0.4 puts more weight on
    # the 0-err day → result must be < plain mean
    assert r["bias"] < 4.0
    assert r["bias"] > 0  # but still positive overall


def test_excludes_other_stations(db):
    # Only KBOS data — querying KPHX should return no data
    _seed(db, "KBOS", [
        ("2026-04-28", 50, 52),
        ("2026-04-27", 52, 54),
        ("2026-04-26", 54, 56),
        ("2026-04-25", 48, 50),
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 5, 1), db_path=db)
    assert r["applied"] is False
    assert r["n"] == 0


def test_excludes_today_and_future(db):
    # Settled day with today's date should NOT be used (we're predicting today)
    _seed(db, "KBOS", [
        ("2026-04-28", 50, 52),
        ("2026-04-27", 52, 54),
        ("2026-04-26", 54, 56),
        ("2026-04-25", 48, 50),
        ("2026-05-01", 99, 99),  # "today" — must be excluded
    ])
    r = bt.compute_bias("KBOS", today=date(2026, 5, 1), db_path=db)
    assert r["n"] == 4   # excluded today
    assert r["applied"] is True


def test_conditional_uses_only_same_regime(db):
    # Bimodal: warm regime has +3 bias, cold regime has -3 bias.
    # Today's predicted max sits in cold regime (pct 30) → expect -3 bias,
    # not the global avg ~0.
    _seed(db, "KLGA", [
        ("2026-04-28", 50, 47),  # cold regime: pred 47 → pct ~25
        ("2026-04-27", 52, 49),  # cold regime
        ("2026-04-26", 51, 48),  # cold regime
        ("2026-04-25", 70, 73),  # warm regime
        ("2026-04-24", 72, 75),  # warm regime
        ("2026-04-23", 71, 74),  # warm regime
    ])

    def pct_lookup(d, pred):
        # Anything < 60°F is cold regime, ≥ 60 is warm
        return 25.0 if pred < 60 else 80.0

    r = bt.compute_bias_conditional(
        "KLGA", predicted_max_f=50.0, today_percentile=30.0,
        percentile_for_pred=pct_lookup,
        today=date(2026, 5, 1), db_path=db,
    )
    assert r["mode"] == "conditional"
    assert r["regime"] == "frío"
    assert r["bias"] == pytest.approx(-3.0, abs=0.01)
    assert r["n"] == 3


def test_conditional_falls_back_when_bucket_thin(db):
    # Today is warm regime (pct 80), but only 1 historical warm day → fallback
    _seed(db, "KLGA", [
        ("2026-04-28", 50, 47),
        ("2026-04-27", 52, 49),
        ("2026-04-26", 51, 48),
        ("2026-04-25", 53, 50),
        ("2026-04-24", 70, 73),  # only one warm day
    ])

    def pct_lookup(d, pred):
        return 25.0 if pred < 60 else 80.0

    r = bt.compute_bias_conditional(
        "KLGA", predicted_max_f=72.0, today_percentile=80.0,
        percentile_for_pred=pct_lookup,
        today=date(2026, 5, 1), db_path=db,
    )
    assert r["mode"] == "global"
    assert "régimen cálido" in r["reason"]


def test_conditional_no_today_pct_falls_back(db):
    _seed(db, "KLGA", [
        ("2026-04-28", 50, 52),
        ("2026-04-27", 52, 54),
        ("2026-04-26", 54, 56),
        ("2026-04-25", 48, 50),
    ])
    r = bt.compute_bias_conditional(
        "KLGA", predicted_max_f=55.0, today_percentile=None,
        percentile_for_pred=lambda d, p: 50.0,
        today=date(2026, 5, 1), db_path=db,
    )
    assert r["mode"] == "global"
    # mean +2 × SHRINK 0.5 = +1.0 (regime-break activa shrink)
    assert r["bias"] == pytest.approx(1.0, abs=0.01)


def test_regime_break_restricts_to_recent_k(db):
    # Last 4 días: residuos persistentemente fríos (-2.0 a -3.7) — ola de calor.
    # Días 5-7: templados (+0.7, +2.7, +1.0). EWMA sobre 7 diluye el shift;
    # detector debería restringir a los últimos 4 → bias ≈ -2.7.
    _seed(db, "KPHX", [
        ("2026-05-07", 105, 103.0),  # -2.0  (most recent)
        ("2026-05-06", 104, 100.0),  # -4.0
        ("2026-05-05", 102, 101.0),  # -1.0
        ("2026-05-04", 100, 96.3),   # -3.7
        ("2026-05-03",  90, 90.7),   # +0.7
        ("2026-05-02",  85, 87.7),   # +2.7
        ("2026-05-01",  88, 89.0),   # +1.0
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 5, 8), db_path=db)
    assert r["regime_break"] is True
    assert r["n"] == bt.REGIME_K
    # mean(-2,-4,-1,-3.7) = -2.675, × SHRINK 0.5 = -1.34
    # Sigue siendo claramente frío vs EWMA-7 que diluiría con días templados.
    assert r["bias"] < -1.0
    assert r["applied"] is True


def test_no_regime_when_signs_mixed(db):
    # 7 días con signos mezclados → no hay régimen, EWMA-7 normal
    _seed(db, "KBOS", [
        ("2026-05-07", 50, 52),   # +2
        ("2026-05-06", 50, 48),   # -2
        ("2026-05-05", 50, 53),   # +3
        ("2026-05-04", 50, 47),   # -3
        ("2026-05-03", 50, 51),   # +1
        ("2026-05-02", 50, 49),   # -1
        ("2026-05-01", 50, 52),   # +2
    ])
    r = bt.compute_bias("KBOS", today=date(2026, 5, 8), db_path=db)
    assert r["regime_break"] is False
    assert r["n"] == 7


def test_no_regime_when_drift_too_small(db):
    # 4 días mismo signo pero |media|=0.5°F < 1.5°F → no es régimen, sólo ruido
    _seed(db, "KLGA", [
        ("2026-05-07", 50, 50.5),   # +0.5
        ("2026-05-06", 50, 50.5),   # +0.5
        ("2026-05-05", 50, 50.5),   # +0.5
        ("2026-05-04", 50, 50.5),   # +0.5
    ])
    r = bt.compute_bias("KLGA", today=date(2026, 5, 8), db_path=db)
    assert r["regime_break"] is False
    assert r["n"] == 4


def test_regime_uses_shrunk_mean_not_extreme(db):
    # Heatwave aguda con un día extremo (-7°F). Antes usábamos el peor caso,
    # pero autocorr lag-1 negativa (~-0.25) hace que el peor caso de ayer
    # sobreajuste el shift de hoy. Ahora aplicamos media × SHRINK 0.5, capada.
    _seed(db, "KPHX", [
        ("2026-05-12", 108, 101.0),  # -7
        ("2026-05-11", 109, 106.0),  # -3
        ("2026-05-10", 106, 104.0),  # -2
        ("2026-05-09", 105, 104.0),  # -1
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 5, 13), db_path=db)
    assert r["regime_break"] is True
    assert r["n"] == bt.REGIME_K
    # mean = -3.25 × SHRINK 0.5 = -1.625 (sin tocar el cap -2.5)
    assert r["bias"] == pytest.approx(-1.625, abs=0.01)
    assert r["applied"] is True


def test_regime_caps_extreme_mean(db):
    # 4 días muy fríos: mean = -6, × SHRINK 0.5 = -3.0 → cap a -2.5
    _seed(db, "KBOS", [
        ("2026-05-12", 90, 84.0),
        ("2026-05-11", 92, 86.0),
        ("2026-05-10", 88, 82.0),
        ("2026-05-09", 86, 80.0),
    ])
    r = bt.compute_bias("KBOS", today=date(2026, 5, 13), db_path=db)
    assert r["regime_break"] is True
    assert r["bias"] == pytest.approx(-bt.REGIME_CAP_F, abs=0.01)


def test_sign_nudge_fires_when_ewma_small_but_signs_persistent(db):
    # KPHX patrón Fable round 2: signo del error persiste aunque la magnitud
    # rebote. EWMA termina cerca de 0 pero los 3 últimos son del mismo signo.
    # Sin nudge no se corregía nada — ahora aplica ±SIGN_NUDGE_F.
    _seed(db, "KPHX", [
        ("2026-06-09", 106, 105.5),  # -0.5 (frío)
        ("2026-06-08", 100, 99.7),   # -0.3 (frío)
        ("2026-06-07", 98, 97.6),    # -0.4 (frío)
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 6, 10), db_path=db)
    assert r["sign_nudge"] is True
    assert r["applied"] is True
    assert r["bias"] == pytest.approx(-bt.SIGN_NUDGE_F, abs=0.01)


def test_sign_nudge_skipped_when_signs_mixed(db):
    # Si en los últimos 3 hay un día caliente, no se activa nudge ni corrección.
    _seed(db, "KPHX", [
        ("2026-06-09", 106, 105.5),  # -0.5
        ("2026-06-08", 99, 99.7),    # +0.7 (signo opuesto)
        ("2026-06-07", 98, 97.6),    # -0.4
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 6, 10), db_path=db)
    assert r["sign_nudge"] is False
    assert r["applied"] is False


def test_nudge_full_when_externals_near_pred(db):
    # |ext_diff| < 0.5 → atenuación off, nudge completo.
    _seed(db, "KPHX", [
        ("2026-06-09", 106, 105.5),
        ("2026-06-08", 100, 99.7),
        ("2026-06-07", 98, 97.6),
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 6, 10), db_path=db,
                        ext_diff=0.2)
    assert r["sign_nudge"] is True
    assert r["bias"] == pytest.approx(-bt.SIGN_NUDGE_F, abs=0.01)
    assert r["bias_path"] == "nudge"


def test_nudge_full_when_toward_ext_med(db):
    # Errores fríos (nudge negativo) y ext_diff negativo (vamos fríos): el
    # nudge mueve la pred hacia ext_med → completo aunque |ext_diff|=2.
    _seed(db, "KPHX", [
        ("2026-06-09", 106, 105.5),
        ("2026-06-08", 100, 99.7),
        ("2026-06-07", 98, 97.6),
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 6, 10), db_path=db,
                        ext_diff=-2.0)
    assert r["sign_nudge"] is True
    assert r["bias"] == pytest.approx(-bt.SIGN_NUDGE_F, abs=0.01)


def test_nudge_half_when_away_in_mid_band(db):
    # Caso KPHX 2026-06-11: nudge frío (-) pero ext_diff=+0.5 → atenuación
    # 50%, bias = -0.5 en vez de -1.0.
    _seed(db, "KPHX", [
        ("2026-06-09", 106, 105.5),
        ("2026-06-08", 100, 99.7),
        ("2026-06-07", 98, 97.6),
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 6, 10), db_path=db,
                        ext_diff=0.5)
    assert r["sign_nudge"] is True
    assert r["bias"] == pytest.approx(-bt.SIGN_NUDGE_F * bt.NUDGE_ATTEN_HALF, abs=0.01)


def test_nudge_vetoed_when_away_with_large_ext_diff(db):
    # |ext_diff| ≥ 1.5 y nudge se aleja → veto completo.
    _seed(db, "KPHX", [
        ("2026-06-09", 106, 105.5),
        ("2026-06-08", 100, 99.7),
        ("2026-06-07", 98, 97.6),
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 6, 10), db_path=db,
                        ext_diff=1.6)
    assert r["sign_nudge"] is False
    assert r["bias_path"] == "none"


def test_streak_len_counts_consecutive_same_sign(db):
    # Racha de 5 fríos seguidos: streak_len debe llegar a 5 (no solo K=3).
    _seed(db, "KPHX", [
        ("2026-06-09", 106, 105.5),
        ("2026-06-08", 100, 99.7),
        ("2026-06-07", 98, 97.6),
        ("2026-06-06", 95, 94.5),
        ("2026-06-05", 93, 92.8),
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 6, 10), db_path=db,
                        ext_diff=None)
    assert r["sign_nudge"] is True
    assert r["streak_len"] == 5


def test_sign_nudge_does_not_override_strong_ewma(db):
    # Con EWMA ya por encima del umbral, applied=True por la vía normal;
    # sign_nudge se queda en False y la magnitud queda como la del EWMA.
    _seed(db, "KPHX", [
        ("2026-06-09", 100, 102.0),  # +2
        ("2026-06-08", 100, 102.0),  # +2
        ("2026-06-07", 100, 102.0),  # +2
    ])
    r = bt.compute_bias("KPHX", today=date(2026, 6, 10), db_path=db)
    assert r["applied"] is True
    assert r["sign_nudge"] is False
    assert r["bias"] == pytest.approx(2.0, abs=0.01)


def test_skips_pre_dawn_snapshot(db):
    # Snapshot before T08:00 should not count as "early prediction"; we
    # simulate that by NOT seeding any prediction at all (None pred). The
    # tracker should treat that day as unusable.
    _seed(db, "KBOS", [
        ("2026-04-28", 50, 52),
        ("2026-04-27", 52, 54),
        ("2026-04-25", 48, None),  # no usable early pred
    ])
    r = bt.compute_bias("KBOS", today=date(2026, 5, 1), db_path=db)
    assert r["n"] == 2
    assert r["applied"] is False  # below MIN_DAYS=3
