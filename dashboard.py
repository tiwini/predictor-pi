"""Landing page dashboard en :8080 — links a BTC predictor (:8001) y weather (:8000).

Sin estado, sin polling. Sólo HTML estático servido por Flask para tener un
único punto de entrada cuando entras desde otra PC / cel (Tailscale o LAN).

/analysis : tab de análisis con 6 aseveraciones (estación, rango, YES/NO,
tu prob) comparadas contra el modelo y Kalshi. Lee de analysis.db poblado
por analysis_poller.py.

Templates viven en `templates/` (Jinja2) y CSS compartido en `static/dashboard.css`.
Cada tab extiende `base.html`.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for

try:
    from agent_monitor import ask as agent_ask, PROMPTS as ASK_PROMPTS
except Exception as _e:
    agent_ask = None
    ASK_PROMPTS = {}

app = Flask(__name__)


def station_label(code: str | None) -> str:
    """Devuelve 'KBOS (Boston)' o 'KBOS' si no hay ciudad mapeada."""
    if not code:
        return ""
    city = STATION_CITY.get(code, "") if "STATION_CITY" in globals() else ""
    return f"{code} ({city})" if city else code


@app.context_processor
def _inject_helpers():
    return {"station_label": station_label,
            "station_city": STATION_CITY if "STATION_CITY" in globals() else {}}

WEATHER_STATIONS = [
    ("KPHX", "Phoenix"),
    ("KLAX", "Los Angeles"),
    ("KLAS", "Las Vegas"),
    ("KLGA", "New York LGA→CP"),
    ("KBOS", "Boston"),
    ("KMIA", "Miami"),
    ("KMDW", "Chicago Midway"),
    ("KIAH", "Houston"),
    ("KSFO", "San Francisco"),
    ("KAUS", "Austin"),
    ("KDEN", "Denver"),
    ("KSAT", "San Antonio"),
    ("KDCA", "Washington DC"),
    ("KDFW", "Dallas-Fort Worth"),
    ("KPHL", "Philadelphia"),
    ("KSEA", "Seattle"),
    ("KATL", "Atlanta"),
    ("KMSY", "New Orleans"),
    ("KOKC", "Oklahoma City"),
    ("KMSP", "Minneapolis-St. Paul"),
]

CRYPTO_SYMBOLS = [
    ("BTCUSDT", "Bitcoin"),
    ("ETHUSDT", "Ethereum"),
    ("XRPUSDT", "XRP"),
    ("DOGEUSDT", "Dogecoin"),
    ("SOLUSDT", "Solana"),
]

STATION_CITY = {sid: name for sid, name in WEATHER_STATIONS}

ANALYSIS_DB = Path(__file__).resolve().parent / "weather-predictor" / "analysis.db"
ASSERTIONS_JSON = Path.home() / "dashboard_assertions.json"
BTC_QUARTER_DB = Path(__file__).resolve().parent / "btc_quarter.db"
AGENT_DB = Path(__file__).resolve().parent / "agent.db"
N_SLOTS = 6
PR_TZ = timezone(timedelta(hours=-4), name="AST")


def _load_assertions() -> dict:
    if not ASSERTIONS_JSON.exists():
        return {}
    try:
        return json.loads(ASSERTIONS_JSON.read_text())
    except Exception:
        return {}


def _save_assertions(d: dict) -> None:
    ASSERTIONS_JSON.write_text(json.dumps(d, indent=2))


def _conn() -> sqlite3.Connection | None:
    if not ANALYSIS_DB.exists():
        return None
    return sqlite3.connect(ANALYSIS_DB)


def _latest_station_snapshot(c: sqlite3.Connection, station: str) -> dict | None:
    cur = c.execute("""SELECT ts, current_f, today_max_obs, ens_med, ens_p10,
                              ens_p90, ens_maxes_json, peak_status
                       FROM station_snapshots WHERE station = ?
                       ORDER BY ts DESC LIMIT 1""", (station,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "ts": row[0], "current_f": row[1], "today_max_obs": row[2],
        "ens_med": row[3], "ens_p10": row[4], "ens_p90": row[5],
        "ens_maxes": json.loads(row[6]) if row[6] else [],
        "peak_status": row[7],
    }


def _bin_prob(maxes: list[float], lo: float, hi: float) -> float:
    """Misma convención que kalshi.our_p_for_bin: ±0.5 redondeo NWS."""
    if not maxes:
        return 0.0
    edge_lo = lo - 0.5
    edge_hi = hi + 0.5
    cnt = sum(1 for v in maxes if edge_lo <= v < edge_hi)
    return cnt / len(maxes)


def _kalshi_mid_for_bin(c: sqlite3.Connection, station: str,
                        lo: float, hi: float) -> tuple[float | None, str]:
    """Devuelve (yes_mid, ts) del bin Kalshi más cercano al rango, o (None, "").

    Convención tail: lo=-1 → bin_lo=-Inf ("X or below"), hi=131 → bin_hi=+Inf ("X or above").
    """
    sql_lo = float("-inf") if lo == -1 else float(lo)
    sql_hi = float("inf") if hi == 131 else float(hi)
    cur = c.execute("""SELECT yes_mid, ts FROM kalshi_snapshots
                       WHERE station = ? AND bin_lo = ? AND bin_hi = ?
                       ORDER BY ts DESC LIMIT 1""", (station, sql_lo, sql_hi))
    row = cur.fetchone()
    if not row:
        return None, ""
    return row[0], row[1]


def _is_settled(snap: dict | None) -> bool:
    """Mercado settled: ens_spread≈0 + obs presente + obs≈ens_med."""
    if not snap:
        return False
    obs = snap.get("today_max_obs")
    ens_med = snap.get("ens_med")
    p10 = snap.get("ens_p10")
    p90 = snap.get("ens_p90")
    if obs is None or ens_med is None or p10 is None or p90 is None:
        return False
    spread = (p90 or 0) - (p10 or 0)
    return spread <= 0.5 and abs(obs - ens_med) <= 1.0


def _archive_assertion(slot: str, a: dict, snap: dict | None, reason: str) -> None:
    """Guarda la aseveración en agent.db tabla assertion_archive antes de removerla del tab."""
    try:
        ac = sqlite3.connect(AGENT_DB)
        ac.execute("""CREATE TABLE IF NOT EXISTS assertion_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot TEXT, station TEXT, side TEXT,
            lo INTEGER, hi INTEGER, user_prob INTEGER,
            created_at TEXT, archived_at TEXT, reason TEXT,
            today_max_obs REAL, ens_med REAL, ens_p10 REAL, ens_p90 REAL,
            peak_status TEXT
        )""")
        snap = snap or {}
        ac.execute("""INSERT INTO assertion_archive
            (slot, station, side, lo, hi, user_prob, created_at, archived_at, reason,
             today_max_obs, ens_med, ens_p10, ens_p90, peak_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slot, a.get("station"), a.get("side"), a.get("lo"), a.get("hi"),
             a.get("prob"), a.get("created_at"),
             datetime.now(timezone.utc).isoformat(), reason,
             snap.get("today_max_obs"), snap.get("ens_med"),
             snap.get("ens_p10"), snap.get("ens_p90"), snap.get("peak_status")))
        ac.commit()
        ac.close()
    except Exception as e:
        print(f"archive_assertion fail: {e}")


def _sweep_settled_assertions(c: sqlite3.Connection, assertions: dict) -> list[str]:
    """Mueve a archive las aseveraciones settled o de días pasados. Devuelve slots removidos."""
    today_ast = datetime.now(PR_TZ).date()
    removed = []
    for slot, a in list(assertions.items()):
        snap = _latest_station_snapshot(c, a["station"])
        reason = None
        try:
            created = datetime.fromisoformat(a.get("created_at", "")).astimezone(PR_TZ).date()
            if created < today_ast:
                reason = "past_day"
        except Exception:
            pass
        if reason is None and _is_settled(snap):
            reason = "settled"
        if reason is not None:
            _archive_assertion(slot, a, snap, reason)
            removed.append(slot)
            del assertions[slot]
    if removed:
        _save_assertions(assertions)
    return removed


def _eval_assertion(c: sqlite3.Connection, a: dict) -> dict:
    """Calcula prob modelo y Kalshi para una aseveración."""
    snap = _latest_station_snapshot(c, a["station"])
    if snap is None:
        return {"model_p": None, "kalshi_p": None, "snap_ts": None,
                "diff_model": None, "diff_kalshi": None,
                "current_f": None, "ens_med": None}
    lo, hi = float(a["lo"]), float(a["hi"])
    p_yes = _bin_prob(snap["ens_maxes"], lo, hi)
    model_p = p_yes if a["side"] == "YES" else (1.0 - p_yes)

    kalshi_yes, _k_ts = _kalshi_mid_for_bin(c, a["station"], lo, hi)
    kalshi_p = None
    if kalshi_yes is not None:
        kalshi_p = kalshi_yes if a["side"] == "YES" else (1.0 - kalshi_yes)

    user_p = float(a["prob"]) / 100.0
    return {
        "model_p": model_p,
        "kalshi_p": kalshi_p,
        "snap_ts": snap["ts"],
        "current_f": snap["current_f"],
        "ens_med": snap["ens_med"],
        "diff_model": (user_p - model_p) * 100,
        "diff_kalshi": ((user_p - kalshi_p) * 100) if kalshi_p is not None else None,
    }


# ───────────────────── routes ─────────────────────

@app.route("/")
def home():
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M AST")
    host = request.host.split(":")[0]
    btc_base = f"http://{host}:8001"
    wx_base = f"http://{host}:8000"
    return render_template(
        "home.html", host=host, now=now, btc_base=btc_base, wx_base=wx_base,
        cryptos=CRYPTO_SYMBOLS, stations=WEATHER_STATIONS,
        station_modes=_load_station_modes(),
    )


@app.route("/analysis")
def analysis():
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M AST")
    assertions = _load_assertions()
    c = _conn()
    swept = []
    if c is not None and assertions:
        swept = _sweep_settled_assertions(c, assertions)

    poller_status = "sin datos"
    if c is not None:
        cur = c.execute("SELECT MAX(ts) FROM station_snapshots")
        latest = cur.fetchone()[0]
        if latest:
            try:
                t = datetime.fromisoformat(latest)
                age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60
                poller_status = f"último snap hace {age_min:.0f} min"
            except Exception:
                poller_status = "último snap: " + latest

    slots = []
    for i in range(1, N_SLOTS + 1):
        a = assertions.get(str(i))
        slot = {"idx": i, "a": a, "diff_class_m": "diff-flat",
                "diff_class_k": "diff-flat"}
        if a and c is not None:
            e = _eval_assertion(c, a)
            dm = e["diff_model"]
            if dm is not None and abs(dm) >= 3:
                slot["diff_class_m"] = "diff-pos" if dm > 0 else "diff-neg"
            dk = e["diff_kalshi"]
            if dk is not None and abs(dk) >= 3:
                slot["diff_class_k"] = "diff-pos" if dk > 0 else "diff-neg"
            slot["e"] = e
            if e["snap_ts"]:
                try:
                    t = datetime.fromisoformat(e["snap_ts"])
                    age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60
                    slot["snap_age"] = f"hace {age_min:.0f} min"
                    slot["stale_class"] = "stale" if age_min > 30 else ""
                except Exception:
                    slot["snap_age"] = e["snap_ts"]
                    slot["stale_class"] = ""
            else:
                slot["snap_age"] = "sin snapshot aún"
                slot["stale_class"] = "stale"
        else:
            slot["e"] = {}
            slot["snap_age"] = ""
            slot["stale_class"] = ""
        slots.append(slot)

    if c:
        c.close()

    return render_template(
        "analysis.html", now=now, slots=slots, stations=WEATHER_STATIONS,
        poller_status=poller_status, swept=swept,
    )


@app.route("/analysis/set", methods=["POST"])
def analysis_set():
    slot = request.form.get("slot", "").strip()
    if slot not in {str(i) for i in range(1, N_SLOTS + 1)}:
        return "slot inválido", 400
    try:
        lo = int(request.form["lo"])
        hi = int(request.form["hi"])
        prob = int(request.form["prob"])
    except (KeyError, ValueError):
        return "valores inválidos", 400
    if not (-1 <= lo <= 131) or not (-1 <= hi <= 131):
        return "lo/hi fuera de rango", 400
    if lo > hi:
        lo, hi = hi, lo
    if not (1 <= prob <= 99):
        return "prob fuera de rango", 400
    station = request.form.get("station", "").strip().upper()
    side = request.form.get("side", "").strip().upper()
    if station not in {s for s, _ in WEATHER_STATIONS}:
        return "estación inválida", 400
    if side not in {"YES", "NO"}:
        return "side inválido", 400

    d = _load_assertions()
    d[slot] = {
        "station": station, "side": side,
        "lo": lo, "hi": hi, "prob": prob,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_assertions(d)
    return redirect(url_for("analysis"))


@app.route("/analysis/clear", methods=["POST"])
def analysis_clear():
    slot = request.form.get("slot", "").strip()
    d = _load_assertions()
    if slot in d:
        del d[slot]
        _save_assertions(d)
    return redirect(url_for("analysis"))


def _btc_quarter_conn() -> sqlite3.Connection | None:
    if not BTC_QUARTER_DB.exists():
        return None
    return sqlite3.connect(BTC_QUARTER_DB)


def _rate(wins: int, settled: int) -> float:
    return (wins / settled * 100.0) if settled else 0.0


@app.route("/btc-quarter")
def btc_quarter():
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M AST")
    c = _btc_quarter_conn()
    streak = 0
    best_streak = 0
    wins = settled = up_wins = up_settled = 0
    down_wins = down_settled = 0
    lean_wins = lean_settled = conv_wins = conv_settled = 0
    flat_count = tie_count = 0
    flat_threshold = 0.5
    active = None
    rows = []
    if c is not None:
        cur = c.execute("""SELECT id, locked_at_iso, price_in, side,
                                  settle_at_iso, price_out, won, streak_after,
                                  tension_score, p_above_next
                           FROM quarter_predictions
                           ORDER BY id DESC LIMIT 20""")
        for (rid, locked, p_in, side, settle, p_out, won, st_after,
             score, p_above) in cur:
            try:
                t_lock = datetime.fromisoformat(locked).astimezone(PR_TZ)
                locked_hhmm = t_lock.strftime("%H:%M")
            except Exception:
                locked_hhmm = locked
            delta = (p_out - p_in) if p_out is not None else None
            rows.append({
                "id": rid, "locked_hhmm": locked_hhmm,
                "side": side, "price_in": p_in, "price_out": p_out,
                "delta": delta, "won": won, "streak_after": st_after or 0,
                "score": score, "p_above": p_above,
            })
        cur = c.execute("""SELECT id, locked_at_iso, price_in, side, settle_at_iso
                           FROM quarter_predictions
                           WHERE won IS NULL AND price_out IS NULL
                           ORDER BY id DESC LIMIT 1""")
        row = cur.fetchone()
        if row:
            try:
                settle_dt = datetime.fromisoformat(row[4]).astimezone(PR_TZ)
                settle_hhmm = settle_dt.strftime("%H:%M")
            except Exception:
                settle_hhmm = row[4]
            active = {"side": row[3], "price_in": row[2], "settle_hhmm": settle_hhmm}

        row = c.execute("SELECT COUNT(*), COALESCE(SUM(won),0) FROM quarter_predictions WHERE won IS NOT NULL").fetchone()
        settled, wins = row[0] or 0, row[1] or 0
        row = c.execute("SELECT COUNT(*), COALESCE(SUM(won),0) FROM quarter_predictions WHERE won IS NOT NULL AND side='UP'").fetchone()
        up_settled, up_wins = row[0] or 0, row[1] or 0
        row = c.execute("SELECT COUNT(*), COALESCE(SUM(won),0) FROM quarter_predictions WHERE won IS NOT NULL AND side='DOWN'").fetchone()
        down_settled, down_wins = row[0] or 0, row[1] or 0
        row = c.execute("""SELECT COUNT(*), COALESCE(SUM(won),0) FROM quarter_predictions
                           WHERE won IS NOT NULL
                             AND ABS(tension_score) >= 0.5 AND ABS(tension_score) < 1.5""").fetchone()
        lean_settled, lean_wins = row[0] or 0, row[1] or 0
        row = c.execute("""SELECT COUNT(*), COALESCE(SUM(won),0) FROM quarter_predictions
                           WHERE won IS NOT NULL AND ABS(tension_score) >= 1.5""").fetchone()
        conv_settled, conv_wins = row[0] or 0, row[1] or 0
        flat_count = c.execute("SELECT COUNT(*) FROM quarter_predictions WHERE side='FLAT'").fetchone()[0] or 0
        tie_count = c.execute("""SELECT COUNT(*) FROM quarter_predictions
                                  WHERE side IN ('UP','DOWN') AND price_out IS NOT NULL
                                    AND won IS NULL""").fetchone()[0] or 0

        best_streak = c.execute("SELECT COALESCE(MAX(streak_after),0) FROM quarter_predictions").fetchone()[0]
        cur = c.execute("""SELECT won FROM quarter_predictions
                           WHERE won IS NOT NULL ORDER BY id DESC LIMIT 50""")
        for (won,) in cur:
            if won == 1:
                streak += 1
            else:
                break
        c.close()
    return render_template(
        "btc_quarter.html", now=now, rows=rows, active=active,
        streak=streak, best_streak=best_streak,
        wins=wins, settled=settled, win_rate=_rate(wins, settled),
        up_wins=up_wins, up_settled=up_settled, up_rate=_rate(up_wins, up_settled),
        down_wins=down_wins, down_settled=down_settled, down_rate=_rate(down_wins, down_settled),
        lean_wins=lean_wins, lean_settled=lean_settled, lean_rate=_rate(lean_wins, lean_settled),
        conv_wins=conv_wins, conv_settled=conv_settled, conv_rate=_rate(conv_wins, conv_settled),
        flat_count=flat_count, tie_count=tie_count, flat_threshold=flat_threshold,
    )


def _agent_conn() -> sqlite3.Connection | None:
    if not AGENT_DB.exists():
        return None
    return sqlite3.connect(AGENT_DB)


@app.route("/ai")
def ai_view():
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M AST")
    c = _agent_conn()
    cap = 15.00
    spent = 0.0
    spent_today = 0.0
    calls_today = 0
    paused = False
    interval_min = "15"
    night_off = True
    burst_remaining = 0
    last_ts = last_ts_human = last_summary = None
    briefing = briefing_ts = None
    decisions = []
    last_ask_error = None
    if c is not None:
        row = c.execute("SELECT value FROM agent_state WHERE key='last_ask_error'").fetchone()
        if row and row[0]:
            last_ask_error = row[0]
            c.execute("DELETE FROM agent_state WHERE key='last_ask_error'")
            c.commit()
        row = c.execute("SELECT value FROM agent_state WHERE key='budget_cap'").fetchone()
        if row:
            cap = float(row[0])
        row = c.execute("SELECT value FROM agent_state WHERE key='paused'").fetchone()
        paused = bool(row and row[0] == "1")
        row = c.execute("SELECT value FROM agent_state WHERE key='interval_min'").fetchone()
        if row:
            interval_min = row[0]
        row = c.execute("SELECT value FROM agent_state WHERE key='night_off'").fetchone()
        night_off = bool(row and row[0] == "1")
        row = c.execute("SELECT value FROM agent_state WHERE key='burst_until'").fetchone()
        if row and row[0]:
            try:
                until = datetime.fromisoformat(row[0])
                rem = (until - datetime.now(timezone.utc)).total_seconds() / 60.0
                if rem > 0:
                    burst_remaining = int(rem) + 1
            except Exception:
                pass
        row = c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM agent_decisions").fetchone()
        spent = float(row[0] or 0)
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = c.execute("""SELECT COALESCE(SUM(cost_usd),0), COUNT(*)
                           FROM agent_decisions WHERE ts >= ?""",
                        (today_start,)).fetchone()
        spent_today = float(row[0] or 0)
        calls_today = int(row[1] or 0)
        briefing = briefing_ts = None
        try:
            cur = c.execute("""SELECT ts, briefing_text FROM agent_decisions
                WHERE is_briefing=1 ORDER BY ts DESC LIMIT 1""")
            row = cur.fetchone()
            if row:
                try:
                    t = datetime.fromisoformat(row[0]).astimezone(PR_TZ)
                    briefing_ts = t.strftime("%Y-%m-%d %H:%M AST")
                except Exception:
                    briefing_ts = row[0]
                briefing = row[1] or ""
        except sqlite3.OperationalError:
            pass
        cur = c.execute("""SELECT ts, summary, n_opportunities, cost_usd,
                                  opportunities_json, ask_kind
                           FROM agent_decisions WHERE COALESCE(is_briefing,0)=0
                           ORDER BY id DESC LIMIT 20""")
        for ts, summary, n_opps, cost, opps_json, ask_kind in cur:
            try:
                t = datetime.fromisoformat(ts).astimezone(PR_TZ)
                ts_hhmm = t.strftime("%m-%d %H:%M")
            except Exception:
                ts_hhmm = ts
            try:
                opps = json.loads(opps_json) if opps_json else []
            except Exception:
                opps = []
            ask_label = ASK_PROMPTS.get(ask_kind, {}).get("label") if ask_kind else None
            decisions.append({
                "ts_hhmm": ts_hhmm, "summary": summary or "",
                "n_opportunities": n_opps or 0, "cost": cost or 0,
                "opps": opps, "ask_kind": ask_kind, "ask_label": ask_label,
            })
        if decisions:
            last_ts = decisions[0]["ts_hhmm"]
            last_ts_human = last_ts
            last_summary = decisions[0]["summary"]
        c.close()
    pct = (spent / cap * 100.0) if cap else 0.0
    projected_monthly = spent_today * 30
    cost_per_call = 0.002
    try:
        mins = 1 if burst_remaining else (0 if interval_min == "off" else int(interval_min))
        if mins > 0:
            active_hours = 18 if night_off else 24
            projected_at_interval = cost_per_call * (active_hours * 60 / mins)
        else:
            projected_at_interval = 0.0
    except Exception:
        projected_at_interval = 0.0
    return render_template(
        "ai.html", now=now, cap=cap, spent=spent, pct=pct,
        spent_today=spent_today, calls_today=calls_today,
        projected_monthly=projected_monthly, paused=paused,
        interval_min=interval_min, night_off=night_off,
        burst_remaining=burst_remaining,
        projected_at_interval=projected_at_interval,
        last_ts=last_ts, last_ts_human=last_ts_human, last_summary=last_summary,
        decisions=decisions, briefing=briefing, briefing_ts=briefing_ts,
        memory_path="~/.claude/.../MEMORY.md",
        ask_prompts=ASK_PROMPTS, last_ask_error=last_ask_error,
    )


@app.route("/ai/toggle-pause", methods=["POST"])
def ai_toggle_pause():
    c = _agent_conn()
    if c is None:
        return redirect(url_for("ai_view"))
    row = c.execute("SELECT value FROM agent_state WHERE key='paused'").fetchone()
    new_val = "0" if (row and row[0] == "1") else "1"
    c.execute("UPDATE agent_state SET value=? WHERE key='paused'", (new_val,))
    c.commit()
    c.close()
    return redirect(url_for("ai_view"))


VALID_INTERVALS_STR = {"1","5","10","30","60","120","240","480","600","800","1000","off"}


def _ensure_state_keys(c: sqlite3.Connection) -> None:
    for k, v in [("interval_min", "15"), ("night_off", "1"), ("burst_until", "")]:
        if not c.execute("SELECT value FROM agent_state WHERE key=?", (k,)).fetchone():
            c.execute("INSERT INTO agent_state(key,value) VALUES(?,?)", (k, v))
    c.commit()


@app.route("/ai/set-interval", methods=["POST"])
def ai_set_interval():
    c = _agent_conn()
    if c is None:
        return redirect(url_for("ai_view"))
    _ensure_state_keys(c)
    v = (request.form.get("interval_min") or "").strip()
    if v not in VALID_INTERVALS_STR:
        c.close()
        return redirect(url_for("ai_view"))
    c.execute("UPDATE agent_state SET value=? WHERE key='interval_min'", (v,))
    c.execute("UPDATE agent_state SET value='' WHERE key='burst_until'")
    c.commit()
    c.close()
    return redirect(url_for("ai_view"))


@app.route("/ai/toggle-night-off", methods=["POST"])
def ai_toggle_night_off():
    c = _agent_conn()
    if c is None:
        return redirect(url_for("ai_view"))
    _ensure_state_keys(c)
    row = c.execute("SELECT value FROM agent_state WHERE key='night_off'").fetchone()
    new_val = "0" if (row and row[0] == "1") else "1"
    c.execute("UPDATE agent_state SET value=? WHERE key='night_off'", (new_val,))
    c.commit()
    c.close()
    return redirect(url_for("ai_view"))


@app.route("/ai/burst", methods=["POST"])
def ai_burst():
    c = _agent_conn()
    if c is None:
        return redirect(url_for("ai_view"))
    _ensure_state_keys(c)
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    c.execute("UPDATE agent_state SET value=? WHERE key='burst_until'", (until,))
    c.commit()
    c.close()
    return redirect(url_for("ai_view"))


@app.route("/ai/ask", methods=["POST"])
def ai_ask():
    """Botón 'Preguntar AI ahora' — ejecuta canned prompt one-off."""
    kind = (request.form.get("kind") or "").strip()
    if agent_ask is None or kind not in ASK_PROMPTS:
        return redirect(url_for("ai_view"))
    result = agent_ask(kind)
    c = _agent_conn()
    if c is not None:
        if not result.get("ok"):
            c.execute(
                "INSERT OR REPLACE INTO agent_state(key,value) VALUES('last_ask_error', ?)",
                (result.get("error", "error desconocido"),))
        else:
            c.execute("DELETE FROM agent_state WHERE key='last_ask_error'")
        c.commit()
        c.close()
    return redirect(url_for("ai_view"))


def _comments_conn() -> sqlite3.Connection:
    c = sqlite3.connect(AGENT_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'note',
        body TEXT NOT NULL
    )""")
    c.commit()
    return c


def _station_modes_conn() -> sqlite3.Connection:
    c = sqlite3.connect(AGENT_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS station_modes (
        station TEXT PRIMARY KEY,
        observation INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    )""")
    c.commit()
    return c


def _load_station_modes() -> dict:
    """Devuelve {station: observation_int} para todas las estaciones marcadas."""
    try:
        c = _station_modes_conn()
        rows = c.execute("SELECT station, observation FROM station_modes").fetchall()
        c.close()
        return {sid: obs for sid, obs in rows}
    except Exception:
        return {}


@app.route("/station/observe/<sid>", methods=["POST"])
def station_toggle_observe(sid: str):
    sid = (sid or "").strip().upper()
    if sid not in {s for s, _ in WEATHER_STATIONS}:
        return redirect(url_for("home"))
    c = _station_modes_conn()
    row = c.execute("SELECT observation FROM station_modes WHERE station=?", (sid,)).fetchone()
    new_val = 0 if (row and row[0] == 1) else 1
    now = datetime.now(timezone.utc).isoformat()
    if row:
        c.execute("UPDATE station_modes SET observation=?, updated_at=? WHERE station=?",
                  (new_val, now, sid))
    else:
        c.execute("INSERT INTO station_modes(station, observation, updated_at) VALUES(?, ?, ?)",
                  (sid, new_val, now))
    c.commit()
    c.close()
    return redirect(request.referrer or url_for("home"))


@app.route("/comments")
def comments_view():
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M AST")
    c = _comments_conn()
    cur = c.execute("SELECT id, ts, category, body FROM comments ORDER BY id DESC")
    comments = []
    for cid, ts, cat, body in cur:
        try:
            t = datetime.fromisoformat(ts).astimezone(PR_TZ)
            ts_human = t.strftime("%Y-%m-%d %H:%M AST")
        except Exception:
            ts_human = ts
        comments.append({"id": cid, "ts_human": ts_human,
                         "category": cat or "note", "body": body})
    total = c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    c.close()
    return render_template("comments.html", now=now, comments=comments, total=total)


@app.route("/comments/add", methods=["POST"])
def comments_add():
    body = (request.form.get("body") or "").strip()
    if not body:
        return redirect(url_for("comments_view"))
    category = (request.form.get("category") or "note").strip()
    if category not in ("note", "bug", "idea"):
        category = "note"
    c = _comments_conn()
    c.execute("INSERT INTO comments(ts, category, body) VALUES (?, ?, ?)",
              (datetime.now(timezone.utc).isoformat(), category, body[:5000]))
    c.commit()
    c.close()
    return redirect(url_for("comments_view"))


@app.route("/comments/delete/<int:cid>", methods=["POST"])
def comments_delete(cid: int):
    c = _comments_conn()
    c.execute("DELETE FROM comments WHERE id=?", (cid,))
    c.commit()
    c.close()
    return redirect(url_for("comments_view"))


# ─────── Q1 — bookmark preservation cross-port (:8080 → :8000) ───────
# Rutas dueñas del predictor weather. Bookmark viejo en :8080 → 301 al :8000.
_PREDICTOR_ROUTES = {
    "/comparison", "/edge", "/timing", "/movement", "/history",
    "/bets", "/intraday", "/stations", "/ladder", "/reweight",
    "/cross", "/grid", "/calibration", "/precip",
    "/system", "/status", "/notify", "/alerts", "/about",
}
_PREDICTOR_PREFIXES = ("/station/",)


def _predictor_base() -> str:
    """Prefiere env PREDICTOR_URL; fallback: mismo host que el request, puerto 8000."""
    env = os.environ.get("PREDICTOR_URL", "").strip().rstrip("/")
    if env:
        return env
    host = request.host.split(":")[0]
    return f"http://{host}:8000"


@app.errorhandler(404)
def _redirect_predictor_bookmarks(_e):
    path = request.path
    is_predictor = (path in _PREDICTOR_ROUTES
                    or any(path.startswith(p) for p in _PREDICTOR_PREFIXES))
    if is_predictor:
        qs = request.query_string.decode("utf-8")
        target = f"{_predictor_base()}{path}" + (f"?{qs}" if qs else "")
        return redirect(target, code=301)
    return "Not Found", 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
