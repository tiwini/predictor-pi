"""Gate de auto-bet: skip cuando los modelos externos discrepan demasiado."""
import sqlite3
from datetime import date

import bets


def _temp_db(tmp_path, monkeypatch):
    db = tmp_path / "calibration.db"
    c = sqlite3.connect(db)
    c.executescript(bets.SCHEMA if hasattr(bets, "SCHEMA") else """
        CREATE TABLE simulated_bets (
            id INTEGER PRIMARY KEY,
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            bin_lo REAL, bin_hi REAL, bin_label TEXT,
            side TEXT NOT NULL,
            our_p REAL NOT NULL, kalshi_p REAL NOT NULL,
            edge_pp REAL NOT NULL,
            stake REAL NOT NULL, entry_price REAL NOT NULL,
            contracts REAL NOT NULL,
            entered_at TEXT NOT NULL,
            outcome INTEGER, won INTEGER, payoff REAL, pnl REAL, settled_at TEXT,
            UNIQUE(station_id, date, ticker)
        )
    """)
    c.commit()
    c.close()
    monkeypatch.setattr(bets, "DB_PATH", db)
    return db


def test_gate_blocks_when_models_diverge(tmp_path, monkeypatch):
    _temp_db(tmp_path, monkeypatch)
    # edge = 0.20 (way over 0.05 threshold) — would normally fire
    placed = bets.maybe_bet("KX", date(2026, 5, 7), "KX-MAX-T70",
                            70.0, 71.0, "70°", 0.70, 0.50,
                            models_spread_f=8.0)  # > 5.4°F threshold
    assert placed is False


def test_gate_allows_when_models_agree(tmp_path, monkeypatch):
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KX", date(2026, 5, 7), "KX-MAX-T70",
                            70.0, 71.0, "70°", 0.70, 0.50,
                            models_spread_f=2.0)  # within threshold
    assert placed is True


def test_gate_passthrough_when_spread_unknown(tmp_path, monkeypatch):
    _temp_db(tmp_path, monkeypatch)
    # spread=None means external_models fetch failed; fall back to old behavior
    placed = bets.maybe_bet("KX", date(2026, 5, 7), "KX-MAX-T70",
                            70.0, 71.0, "70°", 0.70, 0.50,
                            models_spread_f=None)
    assert placed is True


def test_gate_strict_at_5_4(tmp_path, monkeypatch):
    _temp_db(tmp_path, monkeypatch)
    placed1 = bets.maybe_bet("KX", date(2026, 5, 7), "KX-MAX-T70",
                             70.0, 71.0, "70°", 0.70, 0.50,
                             models_spread_f=5.4)
    assert placed1 is True  # exactly at the threshold passes
    placed2 = bets.maybe_bet("KX", date(2026, 5, 8), "KX-MAX-T70",
                             70.0, 71.0, "70°", 0.70, 0.50,
                             models_spread_f=5.41)
    assert placed2 is False  # just above blocks


# ─── Gate direccional vs externos (ext_diff_f) ──────────────────────────

def test_ext_diff_blocks_cold_no_bet_when_ext_runs_hot(tmp_path, monkeypatch):
    """Reproduce KLAS 06-10: pred 102.9, ext_med 104.5, ext_diff = -1.6.
    NO sobre bin [105-106] (entero por encima de pred) es cold-side y
    debe bloquearse cuando ext_diff ≤ -1.5."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KX", date(2026, 6, 10), "KX-MAX-B105.5",
                            105.0, 106.0, "105° to 106°",
                            0.10, 0.64,                # edge -0.54 → side=no
                            our_pred_f=102.9,
                            ext_diff_f=-1.6)
    assert placed is False


def test_ext_diff_blocks_hot_yes_bet_when_ext_runs_cold(tmp_path, monkeypatch):
    """Caso simétrico: pred 90, ext_med 87 → ext_diff = +3.
    YES sobre bin [92-93] (encima de pred) es hot-side y debe bloquearse."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KX", date(2026, 6, 10), "KX-MAX-B92.5",
                            92.0, 93.0, "92° to 93°",
                            0.40, 0.10,                # edge +0.30 → side=yes
                            our_pred_f=90.0,
                            ext_diff_f=3.0)
    assert placed is False


def test_ext_diff_passes_when_externals_agree(tmp_path, monkeypatch):
    """ext_diff dentro de ±1.5 no debe bloquear nada."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KX", date(2026, 6, 10), "KX-MAX-B105.5",
                            105.0, 106.0, "105° to 106°",
                            0.10, 0.64,
                            our_pred_f=102.9,
                            ext_diff_f=-1.0)            # bajo el umbral 1.5
    assert placed is True


def test_ext_diff_ignored_for_mid_bins(tmp_path, monkeypatch):
    """Si el bin contiene la pred (dirección 'mid'), el gate no aplica."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KX", date(2026, 6, 10), "KX-MAX-B70.5",
                            70.0, 71.0, "70° to 71°",
                            0.70, 0.50,
                            our_pred_f=70.5,            # pred dentro del bin
                            ext_diff_f=-3.0)
    assert placed is True


# ─── YES cold-bias guard (Codex Round 4 2026-06-25) ─────────────────────

def test_cold_bias_blocks_yes_on_modal_bin(tmp_path, monkeypatch):
    """KPHX-style: pred 105, bias -1.0°F (frío), YES sobre bin modal [105-106].
    Direction='mid' (bin contiene pred), pero el guard cold-bias bloquea."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KPHX", date(2026, 6, 25), "KX-MAX-B105.5",
                            105.0, 106.0, "105° to 106°",
                            0.65, 0.50,                # edge +0.15 → side=yes
                            our_pred_f=105.5,           # pred dentro → mid
                            bias_info={"bias": -1.0, "applied": True,
                                       "sign_nudge": False, "streak_len": 0})
    assert placed is False


def test_cold_bias_allows_yes_on_hot_direction(tmp_path, monkeypatch):
    """YES sobre bin entero por encima de pred es hot-direction: el guard
    cold-bias NO aplica (cold-bias + hot YES = bet contraria a la dirección
    sesgada, válida)."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KPHX", date(2026, 6, 25), "KX-MAX-B108.5",
                            108.0, 109.0, "108° to 109°",
                            0.40, 0.10,                # edge +0.30 → side=yes
                            our_pred_f=105.0,           # bin > pred → hot
                            bias_info={"bias": -1.0, "applied": True,
                                       "sign_nudge": False, "streak_len": 0})
    assert placed is True


def test_cold_bias_blocks_yes_on_streak(tmp_path, monkeypatch):
    """Aun con bias EWMA > -0.7, una racha sign_nudge ≥3 días fríos bloquea."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KPHX", date(2026, 6, 25), "KX-MAX-B105.5",
                            105.0, 106.0, "105° to 106°",
                            0.65, 0.50,
                            our_pred_f=105.5,
                            bias_info={"bias": -0.4, "applied": True,
                                       "sign_nudge": True, "streak_len": 3})
    assert placed is False


def test_cold_bias_allows_yes_when_no_bias(tmp_path, monkeypatch):
    """bias=0 → guard no dispara, YES pasa normalmente."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KPHX", date(2026, 6, 25), "KX-MAX-B105.5",
                            105.0, 106.0, "105° to 106°",
                            0.65, 0.50,
                            our_pred_f=105.5,
                            bias_info={"bias": 0.0, "applied": False,
                                       "sign_nudge": False, "streak_len": 0})
    assert placed is True


# ─── Per-station-direction min edge (KPHX cold audit 2026-07-01) ────────

def test_kphx_cold_blocks_below_15pp_edge(tmp_path, monkeypatch):
    """KPHX cold audit: N=21 trades ROI -72.9% justifica edge mínimo 15pp
    para (KPHX, cold). Edge de 10pp debe bloquearse aunque supere el 5pp
    global."""
    _temp_db(tmp_path, monkeypatch)
    # NO sobre bin alto [110-111], our_pred=105 → bin > pred → side=no, dir=cold
    # our_p=0.30, kalshi=0.40 → edge=-0.10 (10pp NO)
    placed = bets.maybe_bet("KPHX", date(2026, 6, 25), "KX-MAX-B110.5",
                            110.0, 111.0, "110° to 111°",
                            0.30, 0.40,
                            our_pred_f=105.0)
    assert placed is False


def test_kphx_cold_passes_above_15pp_edge(tmp_path, monkeypatch):
    """Con edge ≥15pp para (KPHX, cold) el guard nuevo no dispara."""
    _temp_db(tmp_path, monkeypatch)
    # NO sobre bin alto, edge -0.20 (20pp NO) > 15pp mínimo
    placed = bets.maybe_bet("KPHX", date(2026, 6, 25), "KX-MAX-B110.5",
                            110.0, 111.0, "110° to 111°",
                            0.20, 0.40,
                            our_pred_f=105.0)
    assert placed is True


def test_kphx_hot_unaffected_by_cold_guard(tmp_path, monkeypatch):
    """El guard es para (KPHX, cold); hot-direction en KPHX no cambia y
    sigue con edge_thr global 5pp."""
    _temp_db(tmp_path, monkeypatch)
    # YES sobre bin alto, our_pred=105 → bin > pred → side=yes, dir=hot
    # edge +0.10 (10pp YES) supera 5pp global; sin override para (KPHX,hot).
    placed = bets.maybe_bet("KPHX", date(2026, 6, 25), "KX-MAX-B110.5",
                            110.0, 111.0, "110° to 111°",
                            0.60, 0.50,
                            our_pred_f=105.0)
    assert placed is True


def test_other_stations_cold_unaffected(tmp_path, monkeypatch):
    """El override es específico a KPHX; otras estaciones cold con edge
    10pp deben pasar (>5pp global). Fecha lejana para evitar overnight state
    real filesystem que podría marcar el día como skip."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet("KLAS", date(2025, 1, 15), "KX-MAX-B110.5",
                            110.0, 111.0, "110° to 111°",
                            0.30, 0.40,
                            our_pred_f=105.0)
    assert placed is True
