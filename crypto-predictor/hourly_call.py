"""BTC hourly point-call.

A cada hora en punto (XX:00 UTC) el sistema publica un valor decimal —
el cuantil p70 de la distribución log-Student-t a 1h — tal que BTC tiene
~70% de probabilidad de NO sobrepasarlo en XX+1:00. Pasada la hora,
settleamos con el open del candle 1m en target_at: win si actual ≤ call.

Métricas:
  - streak: hits consecutivos desde la última settled call hacia atrás.
  - empirical_rate: wins / settled (debe converger a `quantile`).
  - Kalshi: strike discreto más cercano al call_value + edge en pp
    (modelo NO al strike vs Kalshi NO al strike).
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import calibration as _cal
import kalshi as _kalshi
import predictor as _pred

DB_PATH = _cal.DB_PATH
QUANTILE = 0.70           # p70 inicial; tunable luego
SYMBOL = "BTCUSDT"
MIN_HORIZON_MIN = 55      # solo aceptamos calls cerca de XX:00 (horizon ~60m)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    made_at REAL NOT NULL,
    target_at REAL NOT NULL,
    now_price REAL NOT NULL,
    sigma_h REAL NOT NULL,
    quantile REAL NOT NULL,
    call_value REAL NOT NULL,
    kalshi_strike REAL,
    kalshi_no_at_strike REAL,
    kalshi_no_at_call REAL,
    model_no_at_strike REAL,
    edge_pp REAL,
    actual_price REAL,
    won INTEGER,
    settled_at REAL,
    z REAL,
    proxy_price_at_settle REAL,
    kalshi_null_reason TEXT,
    kalshi_curve_json TEXT,
    UNIQUE(symbol, target_at)
);
CREATE INDEX IF NOT EXISTS idx_hcalls_unsettled
  ON hourly_calls(symbol, target_at) WHERE actual_price IS NULL;
"""


@contextmanager
def _conn(db_path: str = DB_PATH):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db(db_path: str = DB_PATH) -> None:
    with _conn(db_path) as c:
        c.executescript(SCHEMA)
        existing = [r[1] for r in c.execute("PRAGMA table_info(hourly_calls)")]
        if "z" not in existing:
            c.execute("ALTER TABLE hourly_calls ADD COLUMN z REAL")
        if "proxy_price_at_settle" not in existing:
            c.execute("ALTER TABLE hourly_calls ADD COLUMN "
                      "proxy_price_at_settle REAL")
        if "kalshi_null_reason" not in existing:
            c.execute("ALTER TABLE hourly_calls ADD COLUMN "
                      "kalshi_null_reason TEXT")
        if "kalshi_curve_json" not in existing:
            c.execute("ALTER TABLE hourly_calls ADD COLUMN "
                      "kalshi_curve_json TEXT")


@dataclass
class CallRow:
    id: int
    symbol: str
    made_at: float
    target_at: float
    now_price: float
    sigma_h: float
    quantile: float
    call_value: float
    kalshi_strike: Optional[float]
    kalshi_no_at_strike: Optional[float]
    kalshi_no_at_call: Optional[float]
    model_no_at_strike: Optional[float]
    edge_pp: Optional[float]
    actual_price: Optional[float]
    won: Optional[int]
    settled_at: Optional[float]
    z: Optional[float] = None
    proxy_price_at_settle: Optional[float] = None
    kalshi_null_reason: Optional[str] = None
    kalshi_curve_json: Optional[str] = None


def _row_to_call(r: sqlite3.Row) -> CallRow:
    return CallRow(**{k: r[k] for k in r.keys()})


def make_call(pred: _pred.Prediction, q: float = QUANTILE,
              min_horizon_min: float = MIN_HORIZON_MIN,
              db_path: str = DB_PATH) -> Optional[int]:
    """Inserta un nuevo call para (symbol, target_at). Idempotente: si ya
    existe call para ese (symbol, target_at) devuelve None.

    Solo dispara cuando horizon_min ≥ min_horizon_min — así garantizamos
    que la call se hizo "cerca de la hora en punto" (no a 5 min del cierre).
    """
    if pred.symbol != SYMBOL:
        return None
    if pred.horizon_min < min_horizon_min:
        return None

    call_value = _pred.quantile(pred, q)

    kalshi_strike = None
    kalshi_no_at_strike = None
    kalshi_no_at_call = None
    model_no_at_strike = None
    edge_pp = None
    kalshi_null_reason = None
    kalshi_curve_json = None
    try:
        ns, curve, kalshi_null_reason = _kalshi.curve_and_strike_with_reason(
            pred.target_at, call_value)
    except Exception as e:
        ns = None
        curve = None
        kalshi_null_reason = f"unhandled:{type(e).__name__}"
    if ns is not None:
        kalshi_strike, mid_yes = ns
        kalshi_no_at_strike = 1.0 - mid_yes
        p_above_strike = _pred.prob_above(pred, kalshi_strike)
        model_no_at_strike = 1.0 - p_above_strike
        edge_pp = (model_no_at_strike - kalshi_no_at_strike) * 100.0
    if curve is not None:
        strikes, mids, bids, asks = curve
        kalshi_curve_json = json.dumps(
            {"s": strikes, "m": mids, "b": bids, "a": asks},
            separators=(",", ":"))
    try:
        kp_above_at_call = _kalshi.implied_above(pred.target_at, call_value)
        if kp_above_at_call is not None:
            kalshi_no_at_call = 1.0 - kp_above_at_call
    except Exception:
        pass

    with _conn(db_path) as c:
        try:
            cur = c.execute(
                "INSERT INTO hourly_calls(symbol, made_at, target_at, now_price, "
                "sigma_h, quantile, call_value, kalshi_strike, kalshi_no_at_strike, "
                "kalshi_no_at_call, model_no_at_strike, edge_pp, "
                "kalshi_null_reason, kalshi_curve_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pred.symbol, pred.fetched_at, pred.target_at, pred.now_price,
                 pred.sigma_horizon, q, call_value, kalshi_strike,
                 kalshi_no_at_strike, kalshi_no_at_call,
                 model_no_at_strike, edge_pp, kalshi_null_reason,
                 kalshi_curve_json),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def _coinbase_price_at(symbol: str, target_at: float) -> float:
    """Open del candle 1m en Coinbase Pro (proxy alternativo al Binance USDT
    que usa settle_due para actual_price). BTCUSDT → BTC-USD.

    Kalshi liquida con CFB BRTI (mediana de CB/KR/BS/GE). Coinbase es uno
    de los 4 constituyentes y su USD open es la fuente pública más limpia
    para el timestamp exacto. Sirve como oracle secundario: si
    `actual_price - proxy_price_at_settle` es materialmente >0 sistemáticamente,
    Binance USDT tiene premium y los settles nuestros discrepan del outcome
    Kalshi real.
    """
    import requests
    if symbol != "BTCUSDT":
        raise ValueError(f"proxy_price_at solo BTC — got {symbol}")
    r = requests.get(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        params={"granularity": 60,
                "start": int(target_at),
                "end": int(target_at) + 60},
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"coinbase kline at {target_at} not available")
    # Coinbase devuelve [[time, low, high, open, close, volume], ...] descendente.
    for k in data:
        if int(k[0]) == int(target_at):
            return float(k[3])
    raise ValueError(f"coinbase kline open_time mismatch for {target_at}")


def settle_due(db_path: str = DB_PATH, now: Optional[float] = None,
               price_fn=None, proxy_price_fn=None) -> int:
    """Settlea calls con target_at ≤ now. Win si actual ≤ call_value.

    proxy_price_fn (opcional): fuente alternativa consultada en el mismo
    target_at para poblar `proxy_price_at_settle`. Errores no bloquean el
    settle — la columna queda NULL y el resto de la fila procede.
    """
    if now is None:
        now = time.time()
    if price_fn is None:
        price_fn = _cal._price_at
    if proxy_price_fn is None:
        proxy_price_fn = _coinbase_price_at
    n = 0
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT id, symbol, target_at, call_value, now_price, sigma_h "
            "FROM hourly_calls "
            "WHERE actual_price IS NULL AND target_at <= ?", (now,)
        ).fetchall()
        for r in rows:
            try:
                price = price_fn(r["symbol"], r["target_at"])
            except Exception:
                continue
            won = 1 if price <= r["call_value"] else 0
            # z = log(actual/now) / σ_h — outcome continuo standardized.
            # Fable 2026-07-04: la calibración honesta se hace sobre PIT(z),
            # no sobre won (1 bit del outcome). z NULL sólo si σ_h≤0.
            try:
                z = (math.log(float(price) / r["now_price"]) / r["sigma_h"]
                     if r["sigma_h"] > 0 and r["now_price"] > 0 and price > 0
                     else None)
            except (ValueError, ZeroDivisionError):
                z = None
            try:
                proxy = float(proxy_price_fn(r["symbol"], r["target_at"]))
            except Exception:
                proxy = None
            c.execute(
                "UPDATE hourly_calls SET actual_price=?, won=?, settled_at=?, "
                "z=?, proxy_price_at_settle=? WHERE id=?",
                (float(price), won, now, z, proxy, r["id"]),
            )
            n += 1
        # Retry pass: rows settleadas recientemente cuyo proxy quedó NULL
        # (típico: settle_due disparó pocos segundos post-target y el candle
        # 1m Coinbase aún no estaba disponible). Filtro settled_at ≥ now-3600
        # evita hammer sobre las N=953 filas históricas pre-instrumentación.
        retry_rows = c.execute(
            "SELECT id, symbol, target_at FROM hourly_calls "
            "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NULL "
            "AND settled_at >= ? AND target_at <= ?",
            (now - 3600, now - 90),
        ).fetchall()
        for r in retry_rows:
            try:
                proxy = float(proxy_price_fn(r["symbol"], r["target_at"]))
            except Exception:
                continue
            c.execute(
                "UPDATE hourly_calls SET proxy_price_at_settle=? WHERE id=?",
                (proxy, r["id"]),
            )
    return n


def streak(db_path: str = DB_PATH, symbol: str = SYMBOL) -> int:
    """Hits consecutivos desde la call settleada más reciente hacia atrás."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT won FROM hourly_calls WHERE symbol=? AND won IS NOT NULL "
            "ORDER BY target_at DESC", (symbol,)
        ).fetchall()
    n = 0
    for r in rows:
        if r["won"] == 1:
            n += 1
        else:
            break
    return n


def empirical_rate(db_path: str = DB_PATH, symbol: str = SYMBOL) -> dict:
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(won),0) AS w FROM hourly_calls "
            "WHERE symbol=? AND won IS NOT NULL", (symbol,)
        ).fetchone()
    n, w = row["n"], row["w"]
    return {"n": n, "wins": w, "rate": (w / n) if n > 0 else None}


def current_call(db_path: str = DB_PATH,
                 symbol: str = SYMBOL) -> Optional[CallRow]:
    """Call activa más reciente (sin settlear), si existe."""
    with _conn(db_path) as c:
        r = c.execute(
            "SELECT * FROM hourly_calls WHERE symbol=? AND actual_price IS NULL "
            "ORDER BY target_at DESC LIMIT 1", (symbol,)
        ).fetchone()
    return _row_to_call(r) if r else None


def recent(db_path: str = DB_PATH, symbol: str = SYMBOL,
           limit: int = 30) -> list[CallRow]:
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM hourly_calls WHERE symbol=? "
            "ORDER BY target_at DESC LIMIT ?", (symbol, limit)
        ).fetchall()
    return [_row_to_call(r) for r in rows]
