"""Paridad entre bets.maybe_bet y agent_signals.evaluate_bin.

Contexto: agent_signals.evaluate_bin bloquea bins que el LLM ve como
[blocked]. bets.maybe_bet dispara auto-bet. Si divergen, el usuario ve
"blocked" en la UI pero simulated_bets acumula pérdidas de bins que las
mismas reglas ya rechazaron.

fable review 2026-07-02 pidió esta prueba tras el hallazgo de que bets.py
NO chequeaba `difficulty_score` mientras evaluate_bin sí — auto-bet
ejecutaba en régimen roto (difficulty=100) mientras el LLM veía todo
bloqueado. Fix: bets.DIFFICULTY_BLOCK_THR + parámetro difficulty_score.

Los umbrales de EDGE son deliberadamente asimétricos (bets 5pp global vs
signals 10pp) — el LLM es más conservador que el simulador. Este test
NO verifica edge min; solo los guards de estado (difficulty, cold_bias,
streak, ext_diff) que sí deben coincidir.
"""
import sqlite3
from datetime import date

import bets
import agent_signals as sig

# bias neutro compartido — evita que bets._cold_bias_blocks_yes caiga al
# bias_tracker real (calibration.db no-monkeypatched) durante tests
# donde no queremos que la guard cold-bias se dispare.
NEUTRAL_BIAS = {"bias": 0.0, "applied": False,
                "sign_nudge": False, "streak_len": 0}


def _temp_db(tmp_path, monkeypatch):
    db = tmp_path / "calibration.db"
    c = sqlite3.connect(db)
    c.executescript("""
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


# ─── difficulty parity — el drift real que este ciclo capturó ──────────

def test_difficulty_block_parity_both_reject(tmp_path, monkeypatch):
    """difficulty_score=100 > 70 debe bloquear en ambos módulos."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "KX-MAX-B95.5",
        95.0, 96.0, "95° to 96°",
        our_p=0.60, kalshi_p=0.10,          # edge +50pp (bien por encima de todo)
        our_pred_f=95.5,
        bias_info=NEUTRAL_BIAS,
        difficulty_score=1000.0, station_local_hour=10)
    assert placed is False, "bets.maybe_bet debe bloquear difficulty>70"

    ev = sig.evaluate_bin(
        station_id="KLAS", bin_lo=95.0, bin_hi=96.0, bin_label="95° to 96°",
        kalshi_yes_price=0.10, model_p_calibrated=0.60,
        pred_calibrated_f=95.5, difficulty_score=1000.0,
    )
    assert ev["actionable"] is False
    assert any("difficulty" in r for r in ev["blocked_reasons"])


def test_difficulty_allow_parity_both_accept(tmp_path, monkeypatch):
    """difficulty=50 (bajo el umbral) no bloquea en ninguno; ambos actionable."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "KX-MAX-B95.5",
        95.0, 96.0, "95° to 96°",
        our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5,
        bias_info=NEUTRAL_BIAS,
        difficulty_score=50.0, station_local_hour=10)
    assert placed is True

    ev = sig.evaluate_bin(
        station_id="KLAS", bin_lo=95.0, bin_hi=96.0, bin_label="95° to 96°",
        kalshi_yes_price=0.10, model_p_calibrated=0.60,
        pred_calibrated_f=95.5, difficulty_score=50.0,
    )
    assert ev["actionable"] is True


# ─── cold-bias YES cold/mid parity ─────────────────────────────────────

def test_cold_bias_block_parity(tmp_path, monkeypatch):
    """bias EWMA -1.0 bloquea YES en bin mid (contiene pred) en ambos."""
    _temp_db(tmp_path, monkeypatch)
    bias_info = {"bias": -1.0, "applied": True,
                 "sign_nudge": False, "streak_len": 0}
    placed = bets.maybe_bet(
        "KPHX", date(2025, 1, 17), "KX-MAX-B105.5",
        105.0, 106.0, "105° to 106°",
        our_p=0.90, kalshi_p=0.40,          # edge +50pp → YES cold/mid
        our_pred_f=105.5,
        bias_info=bias_info, station_local_hour=10)
    assert placed is False

    ev = sig.evaluate_bin(
        station_id="KPHX", bin_lo=105.0, bin_hi=106.0, bin_label="105° to 106°",
        kalshi_yes_price=0.40, model_p_calibrated=0.90,
        pred_calibrated_f=105.5, bias_info=bias_info,
    )
    assert ev["actionable"] is False
    assert any("cold_bias" in r or "cold-side" in r for r in ev["blocked_reasons"])


# ─── ext_diff direccional parity ───────────────────────────────────────

def test_ext_diff_block_parity_cold(tmp_path, monkeypatch):
    """ext_diff=-1.6 bloquea NO sobre bin alto (cold direction) en ambos."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KX", date(2025, 1, 16), "KX-MAX-B105.5",
        105.0, 106.0, "105° to 106°",
        our_p=0.10, kalshi_p=0.64,          # edge -54pp → NO cold
        our_pred_f=102.9,
        bias_info=NEUTRAL_BIAS,
        ext_diff_f=-1.6, station_local_hour=10)
    assert placed is False

    ev = sig.evaluate_bin(
        station_id="KX", bin_lo=105.0, bin_hi=106.0, bin_label="105° to 106°",
        kalshi_yes_price=0.64, model_p_calibrated=0.10,
        pred_calibrated_f=102.9, ext_diff_f=-1.6,
    )
    assert ev["actionable"] is False
    assert any("cold" in r for r in ev["blocked_reasons"])


def test_ext_diff_allow_parity_within_threshold(tmp_path, monkeypatch):
    """ext_diff=-1.0 (bajo umbral) no bloquea en ninguno."""
    _temp_db(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KX", date(2025, 1, 16), "KX-MAX-B105.5",
        105.0, 106.0, "105° to 106°",
        our_p=0.10, kalshi_p=0.64,
        our_pred_f=102.9,
        bias_info=NEUTRAL_BIAS,
        ext_diff_f=-1.0, station_local_hour=10)
    assert placed is True

    ev = sig.evaluate_bin(
        station_id="KX", bin_lo=105.0, bin_hi=106.0, bin_label="105° to 106°",
        kalshi_yes_price=0.64, model_p_calibrated=0.10,
        pred_calibrated_f=102.9, ext_diff_f=-1.0,
    )
    assert ev["actionable"] is True


# ─── streak parity — bets consulta calibration.db, signals recibe int ──

def test_streak_block_parity_cold(tmp_path, monkeypatch):
    """Racha cold 3+ pérdidas: bets consulta DB, signals recibe streak_cold_n=3."""
    db = _temp_db(tmp_path, monkeypatch)
    # Sembrar 3 pérdidas cold recientes en simulated_bets.
    c = sqlite3.connect(db)
    for i in range(3):
        c.execute("""INSERT INTO simulated_bets
            (station_id, date, ticker, bin_lo, bin_hi, bin_label, side,
             our_p, kalshi_p, edge_pp, stake, entry_price, contracts,
             entered_at, outcome, won, payoff, pnl, settled_at)
            VALUES ('KX', ?, ?, ?, ?, '', 'no', 0.2, 0.5, -30, 10, 0.5, 20,
                    ?, 1, 0, 0, -10, ?)""",
            (f"2026-06-{20+i:02d}", f"T{i}", 108.0, 109.0,
             f"2026-06-{20+i:02d}T12:00:00", f"2026-06-{20+i:02d}T22:00:00"))
    c.commit()
    c.close()

    placed = bets.maybe_bet(
        "KX", date(2025, 1, 17), "KX-MAX-B108.5",
        108.0, 109.0, "108° to 109°",
        our_p=0.10, kalshi_p=0.50,
        our_pred_f=105.0,                    # bin > pred → side=no → cold
        bias_info=NEUTRAL_BIAS, station_local_hour=10)
    assert placed is False, "bets debe bloquear por streak"

    # agent_signals recibe streak_cold_n calculado por el caller (poller).
    ev = sig.evaluate_bin(
        station_id="KX", bin_lo=108.0, bin_hi=109.0, bin_label="108° to 109°",
        kalshi_yes_price=0.50, model_p_calibrated=0.10,
        pred_calibrated_f=105.0,
        streak_cold_n=3,
    )
    assert ev["actionable"] is False
    assert any("streak cold" in r for r in ev["blocked_reasons"])


# ─── constantes espejo (guard contra drift silente) ────────────────────

def test_thresholds_mirrored_between_modules():
    """Los thresholds compartidos deben coincidir literalmente."""
    assert bets.DIFFICULTY_BLOCK_THR == sig.DIFFICULTY_BLOCK_THRESHOLD
    assert bets.EXT_GATE_F == sig.EXT_DIFF_BLOCK_THRESHOLD
    assert bets.COLD_BIAS_BLOCK_F == sig.COLD_BIAS_BLOCK_F
    assert bets.COLD_STREAK_BLOCK_N == sig.COLD_STREAK_BLOCK_N
    assert bets.STREAK_BLOCK_AT == sig.STREAK_BLOCK_AT
