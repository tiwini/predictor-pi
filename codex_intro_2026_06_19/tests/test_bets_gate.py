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
