"""Quarter-streak tracker para BTC.

Cada cuarto de hora (xx:00/15/30/45) consulta /api/quarter-signal del BTC
predictor :8001 y congela una predicción UP/DOWN basada en el signo del
score de tensión. 15 min después valida contra el precio del siguiente
cierre y actualiza la racha.

Reglas:
- |tension| < FLAT_THRESHOLD (0.5) → FLAT, no se cuenta para racha
- UP gana si price_out > price_in (estricto)
- DOWN gana si price_out < price_in (estricto)
- price_out == price_in → won=NULL (excluído de win-rate, Codex Q4)

Schema btc_quarter.db (v2):
  quarter_predictions:
    id, locked_at_iso UNIQUE, price_in, price_in_brti, tension_score,
    p_above_next, side (UP|DOWN|FLAT), threshold_used,
    settle_at_iso, price_out, price_out_brti, won (0/1/NULL), streak_after
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
FLAT_THRESHOLD = 0.5  # Codex Q2: alineado con la etiqueta "neutral" del predictor

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
            locked_at_iso TEXT NOT NULL UNIQUE,
            price_in REAL NOT NULL,
            price_in_brti REAL,
            tension_score REAL,
            p_above_next REAL,
            side TEXT NOT NULL,
            threshold_used REAL,
            settle_at_iso TEXT NOT NULL,
            price_out REAL,
            price_out_brti REAL,
            won INTEGER,
            streak_after INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_qp_locked ON quarter_predictions(locked_at_iso);
        CREATE INDEX IF NOT EXISTS idx_qp_pending
            ON quarter_predictions(settle_at_iso) WHERE price_out IS NULL;
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
    """Racha = wins consecutivos contando desde la última fila settled.
    FLAT / NULL se ignoran (no rompen racha, no la suman)."""
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
    if score is None or abs(score) < FLAT_THRESHOLD:
        side = "FLAT"
    elif score > 0:
        side = "UP"
    else:
        side = "DOWN"
    settle_dt = lock_dt + timedelta(minutes=15)
    # Codex Q6: INSERT OR IGNORE para evitar duplicados en restart
    try:
        cur = c.execute("""INSERT OR IGNORE INTO quarter_predictions
            (locked_at_iso, price_in, price_in_brti, tension_score,
             p_above_next, side, threshold_used, settle_at_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (lock_dt.isoformat(), float(sig["price"]),
             float(sig["brti_mid"]) if sig.get("brti_mid") else None,
             float(score) if score is not None else None,
             float(sig["p_above_next"]) if sig.get("p_above_next") is not None else None,
             side, FLAT_THRESHOLD, settle_dt.isoformat()))
        c.commit()
    except sqlite3.IntegrityError:
        log.info("ya existía lock para %s, saltando", lock_dt.isoformat())
        return None
    if cur.rowcount == 0:
        log.info("ya existía lock para %s, saltando", lock_dt.isoformat())
        # Recuperar id existente para que el flujo de settle siga funcionando
        row = c.execute("SELECT id FROM quarter_predictions WHERE locked_at_iso = ?",
                        (lock_dt.isoformat(),)).fetchone()
        return row[0] if row else None
    log.info("lock #%d: %s @ $%.2f (tension=%.2f, p_above=%s) → settle %s",
             cur.lastrowid, side, sig["price"], score or 0,
             ("%.3f" % sig["p_above_next"]) if sig.get("p_above_next") is not None else "—",
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
    price_out_brti = float(sig["brti_mid"]) if sig.get("brti_mid") else None
    # Codex Q4: empate exacto → won=NULL, no cuenta para win-rate
    if side == "FLAT":
        won = None
    elif price_out == price_in:
        won = None
    elif side == "UP":
        won = 1 if price_out > price_in else 0
    else:  # DOWN
        won = 1 if price_out < price_in else 0
    c.execute("""UPDATE quarter_predictions
        SET price_out = ?, price_out_brti = ?, won = ?
        WHERE id = ?""", (price_out, price_out_brti, won, row_id))
    c.commit()
    streak = _current_streak(c)
    c.execute("UPDATE quarter_predictions SET streak_after = ? WHERE id = ?",
              (streak, row_id))
    c.commit()
    delta = price_out - price_in
    log.info("settle #%d: %s $%.2f → $%.2f (Δ%+.2f) won=%s streak=%d",
             row_id, side, price_in, price_out, delta,
             "—" if won is None else ("✓" if won else "✗"), streak)


def _drain_pending(c: sqlite3.Connection) -> None:
    """Codex Q6: al arrancar, liquidar filas con settle_at_iso <= now."""
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = c.execute("""SELECT id FROM quarter_predictions
                       WHERE price_out IS NULL AND settle_at_iso <= ?
                       ORDER BY id ASC""", (now_iso,))
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return
    log.info("drain pending: %d filas con settle vencido", len(ids))
    for rid in ids:
        _settle(c, rid)


def _sleep_until(target: datetime) -> None:
    while True:
        now = datetime.now(timezone.utc)
        delta = (target - now).total_seconds()
        if delta <= 0:
            return
        time.sleep(min(delta, 30))


def main() -> None:
    log.info("btc_quarter_poller started; db=%s threshold=%.2f",
             DB_PATH, FLAT_THRESHOLD)
    c = _conn()
    _drain_pending(c)
    c.close()
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
