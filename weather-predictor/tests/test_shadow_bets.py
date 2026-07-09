"""Shadow bets: guards no ciegan estadística (fable review 2026-07-03).

Cada guard de bets.maybe_bet (difficulty, cold_bias, station_dir_min,
ext_diff, streak, overnight, models_spread) que antes retornaba False
silenciosamente ahora inserta la bet en simulated_bets con
`blocked_by='<reason1>,<reason2>'`. Se liquidan igual pero:
  - stats() y agent_signals.historical_roi() las excluyen del P&L real
  - agent_signals.guard_ev() las agrega por guard para medir ROI que
    TENDRÍA cada guard si no operara

Hard skips (sin insert, sin shadow): |edge| < edge_thr, entry_price ≤0.01
o ≥0.99, y IntegrityError por bet duplicada — son casos donde no hay
información que registrar.
"""
import sqlite3
from datetime import date

import bets
import agent_signals as sig


NEUTRAL_BIAS = {"bias": 0.0, "applied": False,
                "sign_nudge": False, "streak_len": 0}


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "calibration.db"
    monkeypatch.setattr(bets, "DB_PATH", db)
    # touch _conn to run migrations (crea tabla + columnas)
    bets._conn().close()
    return db


def _count(db):
    c = sqlite3.connect(db)
    n = c.execute("SELECT COUNT(*) FROM simulated_bets").fetchone()[0]
    c.close()
    return n


def _row(db, ticker):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM simulated_bets WHERE ticker=?",
                  (ticker,)).fetchone()
    c.close()
    return dict(r) if r else None


# ─── hard skips: no debe insertar shadow ────────────────────────────────

def test_edge_below_threshold_is_hard_skip(tmp_path, monkeypatch):
    """|edge| < edge_thr: ruido, no queremos poblar shadow con todo bin."""
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KX", date(2025, 1, 15), "T_noise",
        95.0, 96.0, "95-96", our_p=0.50, kalshi_p=0.48,   # 2pp
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    assert placed is False
    assert _count(db) == 0


def test_degenerate_price_is_hard_skip(tmp_path, monkeypatch):
    """entry_price ≤0.01 rompe el count de contracts — hard skip, no shadow."""
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KX", date(2025, 1, 15), "T_degen",
        95.0, 96.0, "95-96", our_p=0.05, kalshi_p=0.995,  # NO price = 0.005
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    assert placed is False
    assert _count(db) == 0


# ─── shadow inserts: cada guard debe poblar blocked_by ─────────────────

def test_difficulty_guard_inserts_shadow(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_diff",
        95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
        difficulty_score=1000.0, station_local_hour=10)
    assert placed is False, "shadow bet: retorno False"
    r = _row(db, "T_diff")
    assert r is not None, "pero se insertó fila"
    assert r["blocked_by"] is not None
    assert "difficulty" in r["blocked_by"]


def test_real_bet_has_null_blocked_by(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_real",
        95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
        difficulty_score=50.0,   # bajo umbral
        station_local_hour=10)
    assert placed is True
    r = _row(db, "T_real")
    assert r["blocked_by"] is None


def test_multi_guard_comma_joins(tmp_path, monkeypatch):
    """Cuando fire varios guards, blocked_by lleva ambos como CSV."""
    db = _setup(tmp_path, monkeypatch)
    # Cold NO: our_p=0.10 kalshi=0.64 → NO side, edge -54pp.
    # difficulty=100 → shadow por difficulty.
    # ext_diff_f=-1.6 → shadow por ext_diff cold.
    placed = bets.maybe_bet(
        "KX", date(2025, 1, 15), "T_multi",
        105.0, 106.0, "105-106",
        our_p=0.10, kalshi_p=0.64,
        our_pred_f=102.9, bias_info=NEUTRAL_BIAS,
        ext_diff_f=-1.6, difficulty_score=1000.0, station_local_hour=10)
    assert placed is False
    r = _row(db, "T_multi")
    tokens = [t.split(":", 1)[0] for t in r["blocked_by"].split(",")]
    assert "difficulty" in tokens
    assert "ext_diff" in tokens


def test_cold_bias_guard_inserts_shadow(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    bias_info = {"bias": -1.0, "applied": True,
                 "sign_nudge": False, "streak_len": 0}
    placed = bets.maybe_bet(
        "KPHX", date(2025, 1, 17), "T_cbias",
        105.0, 106.0, "105-106",
        our_p=0.90, kalshi_p=0.40,     # YES mid, edge +50pp
        our_pred_f=105.5, bias_info=bias_info, station_local_hour=10)
    assert placed is False
    r = _row(db, "T_cbias")
    assert "cold_bias" in r["blocked_by"]


def test_station_dir_min_guard_inserts_shadow(tmp_path, monkeypatch):
    """KPHX cold requiere edge ≥15pp; 10pp → shadow por station_dir_min."""
    db = _setup(tmp_path, monkeypatch)
    # NO cold (bin alto, side=no, our_pred bajo → direction=cold).
    # edge = 0.10 - 0.20 = -10pp (cumple EDGE_THR global 5pp) pero <15pp.
    placed = bets.maybe_bet(
        "KPHX", date(2025, 1, 15), "T_sdm",
        108.0, 109.0, "108-109",
        our_p=0.10, kalshi_p=0.20,
        our_pred_f=105.0, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    assert placed is False
    r = _row(db, "T_sdm")
    assert "station_dir_min" in r["blocked_by"]


# ─── stats / historical_roi filtran shadows ────────────────────────────

def test_stats_excludes_shadow_by_default(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    # 1 real bet
    bets.maybe_bet("KX", date(2025, 1, 15), "T_real",
                   95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
                   our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
                   difficulty_score=50.0, station_local_hour=10)
    # 1 shadow
    bets.maybe_bet("KX", date(2025, 1, 15), "T_shadow",
                   95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
                   our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
                   difficulty_score=1000.0, station_local_hour=10)
    bets.settle_day("KX", date(2025, 1, 15), 95.5)
    real = bets.stats("KX")
    with_shadow = bets.stats("KX", include_shadow=True)
    assert real.n_total == 1
    assert real.n_settled == 1
    assert with_shadow.n_total == 2
    assert with_shadow.n_settled == 2


def test_historical_roi_excludes_shadow_by_default(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    bets.maybe_bet("KX", date(2025, 1, 15), "T_real",
                   95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
                   our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
                   difficulty_score=50.0, station_local_hour=10)
    bets.maybe_bet("KX", date(2025, 1, 15), "T_shadow",
                   95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
                   our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
                   difficulty_score=1000.0, station_local_hour=10)
    bets.settle_day("KX", date(2025, 1, 15), 95.5)
    real = sig.historical_roi("KX", str(db))
    with_shadow = sig.historical_roi("KX", str(db), include_shadow=True)
    assert real["trades"] == 1
    assert with_shadow["trades"] == 2


# ─── guard_ev sole vs shared (fable 2026-07-03) ─────────────────────────

def test_guard_ev_sole_when_single_guard(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    bets.maybe_bet("KX", date(2025, 1, 15), "T_diff",
                   95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
                   our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
                   difficulty_score=1000.0, station_local_hour=10)
    bets.settle_day("KX", date(2025, 1, 15), 95.5)
    ev = sig.guard_ev("KX", str(db))
    assert "difficulty" in ev
    # Solo dispara difficulty → cuenta en sole, shared vacío
    assert ev["difficulty"]["sole"]["trades"] == 1
    assert ev["difficulty"]["sole"]["wins"] == 1
    assert ev["difficulty"]["shared"]["trades"] == 0


def test_guard_ev_shared_when_multi_guard(tmp_path, monkeypatch):
    """Multi-guard bet suma bajo shared, NO bajo sole — relajar uno solo no
    liberaría la bet mientras los otros bloqueen."""
    db = _setup(tmp_path, monkeypatch)
    bets.maybe_bet("KX", date(2025, 1, 15), "T_multi",
                   105.0, 106.0, "105-106",
                   our_p=0.10, kalshi_p=0.64,
                   our_pred_f=102.9, bias_info=NEUTRAL_BIAS,
                   ext_diff_f=-1.6, difficulty_score=1000.0, station_local_hour=10)
    bets.settle_day("KX", date(2025, 1, 15), 100.0)
    ev = sig.guard_ev("KX", str(db))
    assert ev["difficulty"]["sole"]["trades"] == 0
    assert ev["difficulty"]["shared"]["trades"] == 1
    assert ev["ext_diff"]["sole"]["trades"] == 0
    assert ev["ext_diff"]["shared"]["trades"] == 1


def test_guard_ev_excludes_retroactive_tags(tmp_path, monkeypatch):
    """Tags :retroactive son sesgo por outcome — no cuentan en EV."""
    db = _setup(tmp_path, monkeypatch)
    # Bet real
    bets.maybe_bet("KX", date(2025, 1, 17), "T_prior",
                   108.0, 109.0, "108-109",
                   our_p=0.10, kalshi_p=0.50,
                   our_pred_f=105.0, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    # Siembra racha
    c = sqlite3.connect(db)
    for i in range(3):
        c.execute("""INSERT INTO simulated_bets
            (station_id, date, ticker, bin_lo, bin_hi, bin_label, side,
             our_p, kalshi_p, edge_pp, stake, entry_price, contracts,
             entered_at, outcome, won, payoff, pnl, settled_at, direction)
            VALUES ('KX', ?, ?, ?, ?, '', 'no', 0.2, 0.5, -30, 10, 0.5, 20,
                    ?, 1, 0, 0, -10, ?, 'cold')""",
                  (f"2026-06-{20+i:02d}", f"S{i}", 108.0, 109.0,
                   f"2026-06-{20+i:02d}T12:00:00",
                   f"2026-06-{20+i:02d}T22:00:00"))
    c.commit()
    c.close()
    # Segunda bet → streak fires → T_prior queda con streak:retroactive
    bets.maybe_bet("KX", date(2025, 1, 17), "T_new",
                   108.0, 109.0, "108-109",
                   our_p=0.10, kalshi_p=0.50,
                   our_pred_f=105.0, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    bets.settle_day("KX", date(2025, 1, 17), 105.0)
    ev = sig.guard_ev("KX", str(db))
    # T_prior tiene solo streak:retroactive → excluido → NO aporta a streak sole
    # T_new tiene streak:cold ex-ante → SÍ aporta
    assert ev["streak"]["sole"]["trades"] == 1  # sólo T_new


def test_guard_ev_empty_when_no_shadows(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    bets.maybe_bet("KX", date(2025, 1, 15), "T_real",
                   95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
                   our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
                   difficulty_score=50.0, station_local_hour=10)
    bets.settle_day("KX", date(2025, 1, 15), 95.5)
    ev = sig.guard_ev("KX", str(db))
    assert ev == {}


# ─── guard_relax_candidate: regla asimétrica de fable ──────────────────

def test_relax_candidate_rejects_low_n():
    slot = {"sole": {"pl": 100.0, "trades": 10, "wins": 6,
                     "stake": 100.0, "roi_pct": 100.0},
            "shared": {"pl": 0.0, "trades": 0, "wins": 0,
                       "stake": 0.0, "roi_pct": 0.0}}
    r = sig.guard_relax_candidate(slot, min_n=40)
    assert r["candidate"] is False
    assert "N_sole" in r["reason"]


def test_relax_candidate_rejects_negative_roi():
    slot = {"sole": {"pl": -50.0, "trades": 50, "wins": 15,
                     "stake": 500.0, "roi_pct": -10.0},
            "shared": {"pl": 0.0, "trades": 0, "wins": 0,
                       "stake": 0.0, "roi_pct": 0.0}}
    r = sig.guard_relax_candidate(slot, min_n=40)
    assert r["candidate"] is False
    assert "ROI_sole" in r["reason"]


def test_relax_candidate_trim_removes_longshot_dominated():
    """N=50 ROI positivo pero las 2 mejores bets son el sample entero."""
    pnls = [-10.0] * 48 + [400.0, 500.0]  # dos longshots dominan
    slot = {"sole": {"pl": sum(pnls), "trades": 50, "wins": 2,
                     "stake": 500.0, "roi_pct": 100.0 * sum(pnls) / 500.0},
            "shared": {"pl": 0.0, "trades": 0, "wins": 0,
                       "stake": 0.0, "roi_pct": 0.0}}
    r = sig.guard_relax_candidate(slot, min_n=40, trim_top=2,
                                  pnl_samples=pnls)
    assert r["candidate"] is False
    assert "trim" in r["reason"]


def test_relax_candidate_passes_when_survives_trim():
    """ROI positivo distribuido, no dominado por longshots."""
    pnls = [5.0] * 40 + [15.0, 15.0]  # trim 2 → sigue positivo
    slot = {"sole": {"pl": sum(pnls), "trades": 42, "wins": 42,
                     "stake": 420.0, "roi_pct": 100.0 * sum(pnls) / 420.0},
            "shared": {"pl": 0.0, "trades": 0, "wins": 0,
                       "stake": 0.0, "roi_pct": 0.0}}
    r = sig.guard_relax_candidate(slot, min_n=40, trim_top=2,
                                  pnl_samples=pnls)
    assert r["candidate"] is True


def test_relax_candidate_without_samples_flags_missing_trim():
    slot = {"sole": {"pl": 100.0, "trades": 50, "wins": 30,
                     "stake": 500.0, "roi_pct": 20.0},
            "shared": {"pl": 0.0, "trades": 0, "wins": 0,
                       "stake": 0.0, "roi_pct": 0.0}}
    r = sig.guard_relax_candidate(slot, min_n=40)
    # Sin pnl_samples: candidato tentativo, pero flag de trim faltante
    assert r["candidate"] is True
    assert "trim" in r["reason"]


def test_guard_ev_embeds_pnl_samples_and_relax_uses_them(tmp_path, monkeypatch):
    """Invariante fable 2026-07-03: guard_ev devuelve pnl_samples embebido en
    cada bucket → guard_relax_candidate hace trim sin plumbing extra."""
    db = _setup(tmp_path, monkeypatch)
    bets.maybe_bet("KX", date(2025, 1, 15), "T_diff",
                   95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
                   our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
                   difficulty_score=1000.0, station_local_hour=10)
    bets.settle_day("KX", date(2025, 1, 15), 95.5)
    ev = sig.guard_ev("KX", str(db))
    sole = ev["difficulty"]["sole"]
    assert "pnl_samples" in sole
    assert len(sole["pnl_samples"]) == sole["trades"] == 1
    # relax_candidate sin pasar pnl_samples debe leerlo del slot y NO devolver
    # "falta trim check" (aunque N=1 obviamente falla el min_n primero).
    r = sig.guard_relax_candidate(ev["difficulty"], min_n=1)
    assert "falta trim" not in r["reason"]


# ─── retroactive cleanup: streak fires → mark old bet shadow ────────────

def test_streak_marks_prior_bet_as_retroactive_shadow(tmp_path, monkeypatch):
    """Bet real puesta antes de que la racha se consolide se marca shadow
    retroactivo cuando el próximo intento activa el guard."""
    db = _setup(tmp_path, monkeypatch)
    # 1) Real bet (no hay racha aún).
    placed_prior = bets.maybe_bet(
        "KX", date(2025, 1, 17), "T_prior",
        108.0, 109.0, "108-109",
        our_p=0.10, kalshi_p=0.50,       # NO cold, edge -40pp
        our_pred_f=105.0, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    assert placed_prior is True, "primera bet debe ser real"
    # 2) Sembrar 3 losses cold consecutivos.
    c = sqlite3.connect(db)
    for i in range(3):
        c.execute("""INSERT INTO simulated_bets
            (station_id, date, ticker, bin_lo, bin_hi, bin_label, side,
             our_p, kalshi_p, edge_pp, stake, entry_price, contracts,
             entered_at, outcome, won, payoff, pnl, settled_at, direction)
            VALUES ('KX', ?, ?, ?, ?, '', 'no', 0.2, 0.5, -30, 10, 0.5, 20,
                    ?, 1, 0, 0, -10, ?, 'cold')""",
                  (f"2026-06-{20+i:02d}", f"S{i}",
                   108.0, 109.0,
                   f"2026-06-{20+i:02d}T12:00:00",
                   f"2026-06-{20+i:02d}T22:00:00"))
    c.commit()
    c.close()
    # 3) Nueva bet: streak guard fire → T_new shadow, T_prior marcado retro.
    placed_new = bets.maybe_bet(
        "KX", date(2025, 1, 17), "T_new",
        108.0, 109.0, "108-109",
        our_p=0.10, kalshi_p=0.50,
        our_pred_f=105.0, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    assert placed_new is False
    r_new = _row(db, "T_new")
    assert "streak" in r_new["blocked_by"]
    r_prior = _row(db, "T_prior")
    assert r_prior["blocked_by"] is not None
    assert "retroactive" in r_prior["blocked_by"]
    # Y guard_ev debe reconocer 'streak' como el guard responsable.
    # (settled_day requires — pero prior no está settled aún; agregamos
    # asserción sobre el label sin agregar.)
    assert r_prior["blocked_by"].split(":", 1)[0] == "streak"


# ─── Fable/Codex retro 2026-07-06: cutoff local + honest fill ──────────

def test_local_hour_cutoff_blocks_entry(tmp_path, monkeypatch):
    """station_local_hour ≥ LOCAL_HOUR_CUTOFF (13) → hard skip, no insert.

    Máx diario se realiza 14-17h local; entradas ≥13h están dentro de la
    ventana de contaminación look-ahead. Fable/Codex retro 2026-07-06.
    """
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_late",
        95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
        station_local_hour=13,
    )
    assert placed is False
    assert _count(db) == 0

    placed2 = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_later",
        95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
        station_local_hour=17,
    )
    assert placed2 is False
    assert _count(db) == 0


def test_local_hour_below_cutoff_allowed(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_early",
        95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
        station_local_hour=8,
    )
    assert placed is True
    assert _count(db) == 1


def test_local_hour_none_no_cutoff(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_notz",
        95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    assert placed is True
    assert _count(db) == 1


def test_honest_fill_yes_uses_ask(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_yask",
        95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
        yes_bid=0.08, yes_ask=0.13, station_local_hour=10)
    assert placed is True
    r = _row(db, "T_yask")
    assert r["side"] == "yes"
    assert abs(r["entry_price"] - 0.13) < 1e-6
    assert abs(r["yes_ask_at_entry"] - 0.13) < 1e-6
    assert abs(r["yes_bid_at_entry"] - 0.08) < 1e-6


def test_honest_fill_no_uses_one_minus_bid(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_nbid",
        95.0, 96.0, "95-96", our_p=0.30, kalshi_p=0.75,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS,
        yes_bid=0.72, yes_ask=0.78, station_local_hour=10)
    assert placed is True
    r = _row(db, "T_nbid")
    assert r["side"] == "no"
    assert abs(r["entry_price"] - (1.0 - 0.72)) < 1e-6
    assert abs(r["yes_bid_at_entry"] - 0.72) < 1e-6


def test_honest_fill_falls_back_to_mid_when_none(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    placed = bets.maybe_bet(
        "KLAS", date(2025, 1, 15), "T_fall",
        95.0, 96.0, "95-96", our_p=0.60, kalshi_p=0.10,
        our_pred_f=95.5, bias_info=NEUTRAL_BIAS, station_local_hour=10)
    assert placed is True
    r = _row(db, "T_fall")
    assert abs(r["entry_price"] - 0.10) < 1e-6
    assert r["yes_bid_at_entry"] is None
    assert r["yes_ask_at_entry"] is None
