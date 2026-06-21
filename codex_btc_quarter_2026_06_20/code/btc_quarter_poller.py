"""Quarter-streak tracker para BTC.

Cada cuarto de hora (xx:00/15/30/45) consulta /api/quarter-signal del BTC
predictor :8001 y congela una predicción UP/DOWN basada en el signo del
score de tensión. 15 min después valida contra el precio del siguiente
cierre y actualiza la racha.

UP gana si precio_out >= precio_in. DOWN gana si precio_out < precio_in.

Schema btc_quarter.db:
  quarter_predictions:
    id, locked_at_iso, price_in, tension_score, side (UP|DOWN|FLAT),
    settle_at_iso, price_out, won (0/1/NULL), streak_after
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import time
import urllib.request
import json as _json
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "btc_quarter.db"
SIGNAL_URL = "http://127.0.0.1:8001/api/quarter-signal"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [btc_quarter] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("btc_quarter")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS quarter_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            locked_at_iso TEXT NOT NULL,
            price_in REAL NOT NULL,
            tension_score REAL,
            side TEXT NOT NULL,
            settle_at_iso TEXT NOT NULL,
            price_out REAL,
            won INTEGER,
            streak_after INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_qp_locked ON quarter_predictions(locked_at_iso);
    """)
    return c


def _fetch_signal() -> dict | None:
    try:
        req = urllib.request.Request(SIGNAL_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read().decode("utf-8"))
        return data
    except Exception as e:
        log.warning("fetch_signal failed: %s", e)
        return None


def _next_quarter(now: datetime) -> datetime:
    minute = ((now.minute // 15) + 1) * 15
    add_h, m = divmod(minute, 60)
    base = now.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(hours=add_h, minutes=m)


def _current_streak(c: sqlite3.Connection) -> int:
    cur = c.execute("""SELECT won FROM quarter_predictions
                       WHERE won IS NOT NULL
                       ORDER BY id DESC LIMIT 50""")
    streak = 0
    for (won,) in cur:
        if won == 1:
            streak += 1
        else:
            break
    return streak


def _capture(c: sqlite3.Connection, lock_dt: datetime) -> int | None:
    sig = _fetch_signal()
    if not sig or sig.get("price") is None:
        log.warning("no signal at %s; skipping", lock_dt.isoformat())
        return None
    score = sig.get("tension_score")
    if score is None or abs(score) < 0.1:
        side = "FLAT"
    elif score > 0:
        side = "UP"
    else:
        side = "DOWN"
    settle_dt = lock_dt + timedelta(minutes=15)
    cur = c.execute("""INSERT INTO quarter_predictions
        (locked_at_iso, price_in, tension_score, side, settle_at_iso)
        VALUES (?, ?, ?, ?, ?)""",
        (lock_dt.isoformat(), float(sig["price"]),
         float(score) if score is not None else None,
         side, settle_dt.isoformat()))
    c.commit()
    log.info("lock #%d: %s @ $%.2f (tension=%.2f) → settle %s",
             cur.lastrowid, side, sig["price"], score or 0,
             settle_dt.strftime("%H:%M"))
    return cur.lastrowid


def _settle(c: sqlite3.Connection, row_id: int) -> None:
    row = c.execute("""SELECT locked_at_iso, price_in, side
                       FROM quarter_predictions WHERE id = ?""",
                    (row_id,)).fetchone()
    if not row:
        return
    _, price_in, side = row
    sig = _fetch_signal()
    if not sig or sig.get("price") is None:
        log.warning("no settle price for #%d; leaving NULL", row_id)
        return
    price_out = float(sig["price"])
    if side == "FLAT":
        won = None
    elif side == "UP":
        won = 1 if price_out >= price_in else 0
    else:  # DOWN
        won = 1 if price_out < price_in else 0
    if won is None:
        c.execute("""UPDATE quarter_predictions
            SET price_out = ?, won = NULL, streak_after = ?
            WHERE id = ?""",
            (price_out, _current_streak(c), row_id))
    else:
        c.execute("""UPDATE quarter_predictions
            SET price_out = ?, won = ?
            WHERE id = ?""", (price_out, won, row_id))
        c.commit()
        streak = _current_streak(c)
        c.execute("UPDATE quarter_predictions SET streak_after = ? WHERE id = ?",
                  (streak, row_id))
    c.commit()
    delta = price_out - price_in
    log.info("settle #%d: %s $%.2f → $%.2f (Δ%+.2f) won=%s streak=%d",
             row_id, side, price_in, price_out, delta,
             "—" if won is None else ("✓" if won else "✗"),
             _current_streak(c))


def _sleep_until(target: datetime) -> None:
    while True:
        now = datetime.now(timezone.utc)
        delta = (target - now).total_seconds()
        if delta <= 0:
            return
        time.sleep(min(delta, 30))


def main() -> None:
    log.info("btc_quarter_poller started; db=%s", DB_PATH)
    while True:
        now = datetime.now(timezone.utc)
        lock_dt = _next_quarter(now)
        log.info("siguiente lock: %s UTC (en %ds)",
                 lock_dt.strftime("%H:%M"), int((lock_dt - now).total_seconds()))
        _sleep_until(lock_dt)
        c = _conn()
        row_id = _capture(c, lock_dt)
        c.close()
        if row_id is None:
            continue
        settle_dt = lock_dt + timedelta(minutes=15)
        _sleep_until(settle_dt)
        c = _conn()
        _settle(c, row_id)
        c.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")
        sys.exit(0)
