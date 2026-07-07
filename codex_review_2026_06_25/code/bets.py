"""Simulador de apuestas hipotéticas (sin dinero real).

Cuando vemos |edge| ≥ EDGE_THR en un bin de Kalshi, "apostamos" STAKE al
lado donde nuestro modelo discrepa (yes si our_p > kalshi_p; no si al revés).
Al settlear el día, marcamos outcome y calculamos P&L. Sirve para medir si
el edge del ensemble se traduce en ganancias reales.

Supuestos Kalshi:
- yes contract: pagas `entry_price` por $1 si bin hit, 0 si no
- no  contract: pagas `1 - entry_price` por $1 si bin NO hit
- contracts = stake / effective_entry; winnings = contracts * 1 if win else 0
"""
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "calibration.db"
EDGE_THR = 0.05   # 5pp mínimo para disparar bet
STAKE = 10.0      # $ por bet (hipotético)
MAX_MODELS_SPREAD_F = 5.4  # 3°C; si modelos externos discrepan más, skip auto-bet
STREAK_LOOKBACK_DAYS = 30
STREAK_BLOCK_AT = 3  # ≥N pérdidas seguidas mismo (estación,dirección) → bloqueo
EXT_GATE_F = 1.5  # |pred − ext_med| sobre esto bloquea bets contra-externos.
                  # Espejo del gate manual de lectura.bias_blocks_bet.


@dataclass
class BetStats:
    n_total: int
    n_settled: int
    n_wins: int
    total_stake: float
    total_payoff: float
    pnl: float
    roi: float | None
    win_rate: float | None


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS simulated_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            bin_lo REAL NOT NULL,
            bin_hi REAL NOT NULL,
            bin_label TEXT,
            side TEXT NOT NULL,        -- 'yes' or 'no'
            our_p REAL NOT NULL,
            kalshi_p REAL NOT NULL,
            edge_pp REAL NOT NULL,
            stake REAL NOT NULL,
            entry_price REAL NOT NULL, -- what we paid per contract
            contracts REAL NOT NULL,   -- stake / entry_price
            entered_at TEXT NOT NULL,
            outcome INTEGER,           -- 1 if bin hit, 0 otherwise
            won INTEGER,               -- 1 if bet paid out
            payoff REAL,               -- gross $ received at settlement
            pnl REAL,                  -- payoff - stake
            settled_at TEXT,
            UNIQUE (station_id, date, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_bets_station_date
            ON simulated_bets(station_id, date);
        CREATE INDEX IF NOT EXISTS idx_bets_unsettled
            ON simulated_bets(outcome) WHERE outcome IS NULL;
    """)
    # Schema upgrade 2026-06-22: capturar contexto al momento de la entry
    # para sweeps multivariados (ext_gate, models_spread, pred_dist_to_bin).
    # Idempotente — ADD COLUMN solo si falta.
    existing_cols = {r[1] for r in c.execute(
        "PRAGMA table_info(simulated_bets)").fetchall()}
    for col, typ in (("ext_diff_at_entry", "REAL"),
                     ("models_spread_at_entry", "REAL"),
                     ("our_pred_at_entry", "REAL")):
        if col not in existing_cols:
            c.execute(f"ALTER TABLE simulated_bets ADD COLUMN {col} {typ}")
    c.commit()
    return c


def _bin_contains(max_f: float, lo: float, hi: float) -> bool:
    l = float("-inf") if lo == float("-inf") else lo - 0.5
    h = float("inf") if hi == float("inf") else hi + 0.5
    return l <= max_f < h


def _direction(side: str, bin_lo: float, bin_hi: float,
               our_pred_f: float | None = None) -> str:
    """'cold' (apuesta a temp baja) / 'hot' / 'mid'. Espejo de lectura._direction_from_db.

    Si our_pred_f está dado, también clasifica bins medios por posición vs pred:
    bin entero por encima de pred = "high direction" (YES=hot, NO=cold) y simétrico.
    """
    is_yes = (side or "").lower() == "yes"
    is_cold_tail = bin_lo == float("-inf")
    is_hot_tail = bin_hi == float("inf")
    if is_cold_tail and not is_hot_tail:
        return "cold" if is_yes else "hot"
    if is_hot_tail and not is_cold_tail:
        return "hot" if is_yes else "cold"
    if our_pred_f is not None:
        if bin_lo > our_pred_f:
            return "hot" if is_yes else "cold"
        if bin_hi < our_pred_f:
            return "cold" if is_yes else "hot"
    return "mid"


def _streak_blocks(station_id: str, direction: str,
                   our_pred_f: float | None = None) -> int:
    """Devuelve n_losses de la racha actual si ≥STREAK_BLOCK_AT, sino 0.

    `our_pred_f` se propaga a `_direction` para que las bets históricas en
    bins medios se clasifiquen direccionalmente y cuenten para la racha.
    """
    if direction == "mid":
        return 0
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(days=STREAK_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    c = _conn()
    try:
        rows = c.execute("""
            SELECT side, bin_lo, bin_hi, won FROM simulated_bets
            WHERE station_id=? AND won IS NOT NULL AND date >= ?
            ORDER BY settled_at DESC
        """, (station_id, cutoff)).fetchall()
    finally:
        c.close()
    streak = 0
    for side, lo, hi, won in rows:
        if _direction(side, lo, hi, our_pred_f) != direction:
            continue
        if won == 0:
            streak += 1
        else:
            break
    return streak if streak >= STREAK_BLOCK_AT else 0


def _cleanup_blocked(station_id: str, target_date: date,
                     direction: str | None) -> int:
    """Borra bets no-settled de (station, date) que coinciden con un bloqueo nuevo.

    Si direction is None, borra todas las del día (skip flag de estación entera).
    Si direction está dado, sólo borra las que pertenecen a esa dirección
    (necesita our_pred_f para clasificar bins medios — sin él, sólo se borran
    los tails y bins fuera del rango pred-vecino).
    """
    c = _conn()
    try:
        rows = c.execute("""SELECT id, side, bin_lo, bin_hi FROM simulated_bets
                            WHERE station_id=? AND date=? AND outcome IS NULL""",
                         (station_id, target_date.isoformat())).fetchall()
        ids = []
        for bid, side, lo, hi in rows:
            if direction is None or _direction(side, lo, hi) == direction:
                ids.append(bid)
        for bid in ids:
            c.execute("DELETE FROM simulated_bets WHERE id=?", (bid,))
        c.commit()
        return len(ids)
    finally:
        c.close()


def maybe_bet(station_id: str, target_date: date, ticker: str,
              bin_lo: float, bin_hi: float, bin_label: str,
              our_p: float, kalshi_p: float,
              edge_thr: float = EDGE_THR, stake: float = STAKE,
              models_spread_f: float | None = None,
              our_pred_f: float | None = None,
              ext_diff_f: float | None = None) -> bool:
    """Register a new hypothetical bet if |edge| is big enough and no bet yet
    exists for (station, date, ticker). Returns True if a bet was created.

    If `models_spread_f` is given (max - min of external Open-Meteo models for
    today's max), skip when it exceeds MAX_MODELS_SPREAD_F — when GFS, ECMWF,
    ICON, MétéoFR, UKMO and GraphCast disagree by >3°C the day is too volatile
    for our single-model GFS ensemble to claim edge over the market.
    """
    edge = our_p - kalshi_p
    if abs(edge) < edge_thr:
        return False
    if models_spread_f is not None and models_spread_f > MAX_MODELS_SPREAD_F:
        return False
    # Side: yes if we think more likely than market; no otherwise.
    if edge > 0:
        side = "yes"
        entry_price = kalshi_p
    else:
        side = "no"
        entry_price = 1.0 - kalshi_p
    # Avoid degenerate prices (0 or 1) which blow up contracts count.
    if entry_price <= 0.01 or entry_price >= 0.99:
        return False
    # Streak-block: salta si esta (estación, dirección) tiene racha de pérdidas.
    direction = _direction(side, bin_lo, bin_hi, our_pred_f)
    # Gate direccional vs externos (espejo de lectura.bias_blocks_bet, que
    # antes sólo existía en el CLI manual). ext_diff_f debe venir PRE-shift
    # del posterior — anchor_ctx["ext_diff"] post-shift atenúa la señal.
    if ext_diff_f is not None and direction != "mid":
        if direction == "cold" and ext_diff_f <= -EXT_GATE_F:
            return False
        if direction == "hot" and ext_diff_f >= EXT_GATE_F:
            return False
    if _streak_blocks(station_id, direction, our_pred_f):
        _cleanup_blocked(station_id, target_date, direction=direction)
        return False
    # Overnight skip flag (divergencia con ensemble detectada anoche).
    try:
        import overnight as _ov
        skipped, _ = _ov.is_skipped(station_id, target_date)
        if skipped:
            _cleanup_blocked(station_id, target_date, direction=None)
            return False
    except Exception:
        pass

    contracts = stake / entry_price
    c = _conn()
    try:
        c.execute("""INSERT INTO simulated_bets
            (station_id, date, ticker, bin_lo, bin_hi, bin_label,
             side, our_p, kalshi_p, edge_pp, stake, entry_price,
             contracts, entered_at,
             ext_diff_at_entry, models_spread_at_entry, our_pred_at_entry)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (station_id, target_date.isoformat(), ticker,
             float(bin_lo), float(bin_hi), bin_label,
             side, float(our_p), float(kalshi_p),
             100.0 * edge, float(stake), float(entry_price),
             float(contracts), datetime.utcnow().isoformat(),
             float(ext_diff_f) if ext_diff_f is not None else None,
             float(models_spread_f) if models_spread_f is not None else None,
             float(our_pred_f) if our_pred_f is not None else None))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        c.close()


def settle_day(station_id: str, target_date: date, max_f: float) -> int:
    """Mark outcome + pnl for all unsettled bets of (station, date).
    Returns number of bets settled."""
    c = _conn()
    cur = c.execute("""SELECT id, bin_lo, bin_hi, side, contracts, stake
                       FROM simulated_bets
                       WHERE station_id=? AND date=? AND outcome IS NULL""",
                    (station_id, target_date.isoformat()))
    rows = cur.fetchall()
    settled_at = datetime.utcnow().isoformat()
    n = 0
    for bid, lo, hi, side, contracts, stake in rows:
        outc = 1 if _bin_contains(max_f, lo, hi) else 0
        if side == "yes":
            won = 1 if outc == 1 else 0
        else:
            won = 1 if outc == 0 else 0
        payoff = contracts * 1.0 if won else 0.0
        pnl = payoff - stake
        c.execute("""UPDATE simulated_bets
                     SET outcome=?, won=?, payoff=?, pnl=?, settled_at=?
                     WHERE id=?""",
                  (outc, won, payoff, pnl, settled_at, bid))
        n += 1
    c.commit()
    c.close()
    return n


def list_bets(station_id: str | None = None,
              only: str = "all",  # 'all' | 'open' | 'settled'
              limit: int = 200) -> list[dict]:
    c = _conn()
    where = []
    params: list = []
    if station_id:
        where.append("station_id=?")
        params.append(station_id)
    if only == "open":
        where.append("outcome IS NULL")
    elif only == "settled":
        where.append("outcome IS NOT NULL")
    wclause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    cur = c.execute(f"""SELECT * FROM simulated_bets {wclause}
                        ORDER BY entered_at DESC LIMIT ?""", params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    c.close()
    return rows


def stats(station_id: str | None = None) -> BetStats:
    c = _conn()
    where = "WHERE station_id=?" if station_id else ""
    params = (station_id,) if station_id else ()
    total = c.execute(f"SELECT COUNT(*) FROM simulated_bets {where}",
                      params).fetchone()[0]
    settled = c.execute(f"""SELECT COUNT(*), COALESCE(SUM(won),0),
                                   COALESCE(SUM(stake),0),
                                   COALESCE(SUM(payoff),0),
                                   COALESCE(SUM(pnl),0)
                            FROM simulated_bets
                            WHERE outcome IS NOT NULL
                              {' AND station_id=?' if station_id else ''}""",
                        params).fetchone()
    c.close()
    ns, wins, ts, tp, pnl = settled
    roi = (pnl / ts) if ts > 0 else None
    win_rate = (wins / ns) if ns > 0 else None
    return BetStats(n_total=total, n_settled=ns, n_wins=wins,
                    total_stake=ts, total_payoff=tp, pnl=pnl,
                    roi=roi, win_rate=win_rate)
