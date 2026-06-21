"""Excerpt: the /btc-quarter dashboard route + DB read helpers.

The dashboard (port 8080) is just a thin SQLite reader — all state is
written by btc_quarter_poller.py. This file is included so Codex can see
how stats (streak, win_rate, best_streak) are computed from the persisted rows.
"""

BTC_QUARTER_DB = Path(__file__).resolve().parent / "btc_quarter.db"


def _btc_quarter_conn() -> sqlite3.Connection | None:
    if not BTC_QUARTER_DB.exists():
        return None
    return sqlite3.connect(BTC_QUARTER_DB)


@app.route("/btc-quarter")
def btc_quarter():
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M AST")
    c = _btc_quarter_conn()
    streak = 0
    best_streak = 0
    wins = 0
    settled = 0
    win_rate = 0.0
    active = None
    rows = []
    if c is not None:
        cur = c.execute("""SELECT id, locked_at_iso, price_in, side,
                                  settle_at_iso, price_out, won, streak_after
                           FROM quarter_predictions
                           ORDER BY id DESC LIMIT 15""")
        for (rid, locked, p_in, side, settle, p_out, won, st_after) in cur:
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
        cur = c.execute("SELECT COUNT(*), SUM(won) FROM quarter_predictions WHERE won IS NOT NULL")
        row = cur.fetchone()
        settled = row[0] or 0
        wins = row[1] or 0
        win_rate = (wins / settled * 100.0) if settled else 0.0
        cur = c.execute("SELECT MAX(streak_after) FROM quarter_predictions")
        best_streak = cur.fetchone()[0] or 0
        cur = c.execute("""SELECT won FROM quarter_predictions
                           WHERE won IS NOT NULL ORDER BY id DESC LIMIT 50""")
        for (won,) in cur:
            if won == 1:
                streak += 1
            else:
                break
        c.close()
    return render_template_string(
        BTC_QUARTER_TMPL, now=now, rows=rows, active=active,
        streak=streak, best_streak=best_streak,
        wins=wins, settled=settled, win_rate=win_rate,
    )
