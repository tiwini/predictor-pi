"""Calibration tracking: persistir predicciones y compararlas con outcomes
reales 1h después. Brier score por bucket de probabilidad para ver si el
modelo está bien calibrado (si decimos "60% prob" deberían cumplirse 60%).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import predictor as _pred

DB_PATH = "/home/popeye/crypto-predictor/calibration.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    made_at     REAL NOT NULL,           -- unix epoch seconds (cuando se hizo)
    target_at   REAL NOT NULL,           -- unix epoch del cierre objetivo (XX:00 UTC)
    horizon_min REAL NOT NULL,           -- (target_at - made_at)/60
    now_price   REAL NOT NULL,
    sigma_h     REAL NOT NULL,
    ladder_json TEXT NOT NULL,           -- list of {threshold, p_above, delta_pct}
    kalshi_json TEXT                     -- list of {threshold, kalshi_p} si BTC y hay quote
);
CREATE INDEX IF NOT EXISTS idx_pred_target
    ON predictions(symbol, target_at);

CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id INTEGER PRIMARY KEY,
    settled_at    REAL NOT NULL,
    actual_price  REAL NOT NULL,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);
"""


def _migrate(c: sqlite3.Connection) -> None:
    """Añade columnas nuevas a tablas viejas."""
    cols = [r[1] for r in c.execute("PRAGMA table_info(predictions)")]
    if "target_at" not in cols:
        c.execute("ALTER TABLE predictions ADD COLUMN target_at REAL")
        c.execute(
            "UPDATE predictions SET target_at = made_at + horizon_min*60 "
            "WHERE target_at IS NULL"
        )
    if "kalshi_json" not in cols:
        c.execute("ALTER TABLE predictions ADD COLUMN kalshi_json TEXT")


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
        _migrate(c)


def record_prediction(pred: _pred.Prediction, ladder: list[dict],
                      kalshi_curve: Optional[list[dict]] = None,
                      db_path: str = DB_PATH) -> int:
    target_at = pred.target_at if pred.target_at else (
        pred.fetched_at + pred.horizon_min * 60)
    with _conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO predictions(symbol, made_at, target_at, horizon_min, "
            "now_price, sigma_h, ladder_json, kalshi_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pred.symbol, pred.fetched_at, target_at, pred.horizon_min,
             pred.now_price, pred.sigma_horizon, json.dumps(ladder),
             json.dumps(kalshi_curve) if kalshi_curve else None),
        )
        return cur.lastrowid


def _due_predictions(c: sqlite3.Connection, now: float) -> list[sqlite3.Row]:
    """Predictions cuyo target_at ya pasó y que no tienen outcome aún."""
    return c.execute(
        "SELECT p.* FROM predictions p "
        "LEFT JOIN outcomes o ON o.prediction_id = p.id "
        "WHERE o.prediction_id IS NULL "
        "AND ? >= p.target_at",
        (now,),
    ).fetchall()


def settle_due(db_path: str = DB_PATH,
               now: Optional[float] = None,
               price_fn=None) -> int:
    """Look up predictions past their target, fetch actual price at target_at,
    write outcome. `price_fn(symbol, target_at) -> float` overrideable."""
    if now is None:
        now = time.time()
    if price_fn is None:
        price_fn = _price_at
    settled = 0
    with _conn(db_path) as c:
        rows = _due_predictions(c, now)
        # Agrupar por (symbol, target_at) → 1 fetch sirve para todas
        by_key: dict[tuple, list[sqlite3.Row]] = {}
        for r in rows:
            by_key.setdefault((r["symbol"], r["target_at"]), []).append(r)
        for (sym, tgt), preds in by_key.items():
            try:
                price = price_fn(sym, tgt)
            except Exception:
                continue
            for r in preds:
                c.execute(
                    "INSERT INTO outcomes(prediction_id, settled_at, actual_price) "
                    "VALUES (?,?,?)",
                    (r["id"], now, price),
                )
                settled += 1
    return settled


def _price_at(symbol: str, target_at: float) -> float:
    """Open del candle 1m que arranca en target_at — precio exacto en XX:00:00."""
    import requests
    r = requests.get(
        f"{_pred.BINANCE_BASE}/klines",
        params={
            "symbol": symbol, "interval": "1m",
            "startTime": int(target_at * 1000),
            "limit": 1,
        },
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        # Aún no hay candle (viene con delay) → fallback al último close
        kl = _pred.fetch_klines(symbol=symbol, interval="1m", limit=1)
        return kl[-1].close
    return float(data[0][1])  # open


@dataclass
class BucketStat:
    bucket: str          # e.g. "0.5-0.6"
    n: int
    avg_predicted: float
    avg_actual: float    # fraction of times event occurred
    brier: float         # mean (p - outcome)^2


# Buckets de probabilidad para reliability curve
BUCKETS = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
           (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def _bucket_for(p: float) -> str:
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return f"{lo:.1f}-{min(hi,1.0):.1f}"
    return "1.0-1.0"


def reliability(symbol: Optional[str] = None,
                db_path: str = DB_PATH) -> list[BucketStat]:
    """Para cada threshold de cada predicción settleada, registramos
    (p_above_predicho, 1 si actual>threshold else 0) y agregamos por bucket."""
    pairs: dict[str, list[tuple[float, int]]] = {}
    with _conn(db_path) as c:
        q = ("SELECT p.ladder_json, o.actual_price FROM predictions p "
             "JOIN outcomes o ON o.prediction_id = p.id")
        params: tuple = ()
        if symbol:
            q += " WHERE p.symbol = ?"
            params = (symbol,)
        for row in c.execute(q, params):
            ladder = json.loads(row["ladder_json"])
            actual = row["actual_price"]
            for r in ladder:
                p_pred = r["p_above"]
                outcome = 1 if actual > r["threshold"] else 0
                pairs.setdefault(_bucket_for(p_pred), []).append((p_pred, outcome))

    out: list[BucketStat] = []
    for lo, hi in BUCKETS:
        key = f"{lo:.1f}-{min(hi,1.0):.1f}"
        items = pairs.get(key, [])
        if not items:
            out.append(BucketStat(key, 0, 0.0, 0.0, 0.0))
            continue
        n = len(items)
        avg_p = sum(p for p, _ in items) / n
        avg_a = sum(o for _, o in items) / n
        brier = sum((p - o) ** 2 for p, o in items) / n
        out.append(BucketStat(key, n, avg_p, avg_a, brier))
    return out


@dataclass
class HourResult:
    symbol: str
    target_at: float
    actual_price: float
    pred_price: float        # now_price del fetch con MAYOR lead (más temprano)
    lead_min: float          # min antes del cierre cuando se hizo la pred
    sigma_h_pct: float       # σ horizonte (escala de la pred)
    diff_pct: float          # (actual - pred) / pred * 100
    z_actual: float          # |Δ%| / σ_h%  — cuántos σ se movió
    p_above_actual: float    # P que el modelo le dio a "≥ actual"


def recent_outcomes(symbol: Optional[str] = None,
                    limit: int = 24,
                    db_path: str = DB_PATH) -> list[HourResult]:
    """Para cada cierre settleado: predicción más TEMPRANA (lead alto) vs actual.
    La temprana es la que tenía σ amplia; ahí se valida la capacidad predictiva."""
    out: list[HourResult] = []
    with _conn(db_path) as c:
        # MIN(made_at) por (symbol, target_at) → la pred más temprana
        q = ("SELECT p.symbol, p.target_at, p.made_at, p.now_price, p.sigma_h, "
             "       p.ladder_json, o.actual_price "
             "FROM predictions p "
             "JOIN outcomes o ON o.prediction_id = p.id "
             "JOIN ( "
             "   SELECT symbol, target_at, MIN(made_at) AS min_made "
             "   FROM predictions GROUP BY symbol, target_at "
             ") first ON first.symbol = p.symbol "
             "      AND first.target_at = p.target_at "
             "      AND first.min_made = p.made_at ")
        params: tuple = ()
        if symbol:
            q += "WHERE p.symbol = ? "
            params = (symbol,)
        q += "ORDER BY p.target_at DESC LIMIT ?"
        params = params + (limit,)
        for row in c.execute(q, params):
            ladder = json.loads(row["ladder_json"])
            actual = row["actual_price"]
            p_above_actual = _interp_p_above(ladder, actual)
            diff_pct = (actual - row["now_price"]) / row["now_price"] * 100.0
            sigma_pct = row["sigma_h"] * 100.0
            z = abs(diff_pct) / sigma_pct if sigma_pct > 0 else 0.0
            out.append(HourResult(
                symbol=row["symbol"],
                target_at=row["target_at"],
                actual_price=actual,
                pred_price=row["now_price"],
                lead_min=(row["target_at"] - row["made_at"]) / 60.0,
                sigma_h_pct=sigma_pct,
                diff_pct=diff_pct,
                z_actual=z,
                p_above_actual=p_above_actual,
            ))
    return out


def _interp_p_above(ladder: list[dict], x: float) -> float:
    """P(>x) interpolada de la ladder (lineal entre thresholds)."""
    sorted_l = sorted(ladder, key=lambda r: r["threshold"])
    if x <= sorted_l[0]["threshold"]:
        return sorted_l[0]["p_above"]
    if x >= sorted_l[-1]["threshold"]:
        return sorted_l[-1]["p_above"]
    for i in range(len(sorted_l) - 1):
        a, b = sorted_l[i], sorted_l[i + 1]
        if a["threshold"] <= x <= b["threshold"]:
            t = (x - a["threshold"]) / (b["threshold"] - a["threshold"])
            return a["p_above"] + t * (b["p_above"] - a["p_above"])
    return 0.5


def history_for_target(symbol: str, target_at: float,
                       db_path: str = DB_PATH) -> dict:
    """Toda la trayectoria de predicciones para un (symbol, target_at).
    Devuelve: lista ordenada por made_at + actual_price del outcome (si hay)."""
    rows: list[dict] = []
    actual: Optional[float] = None
    with _conn(db_path) as c:
        for r in c.execute(
            "SELECT p.id, p.made_at, p.target_at, p.now_price, p.sigma_h, "
            "       p.ladder_json, o.actual_price "
            "FROM predictions p "
            "LEFT JOIN outcomes o ON o.prediction_id = p.id "
            "WHERE p.symbol = ? AND p.target_at = ? "
            "ORDER BY p.made_at ASC",
            (symbol, target_at),
        ):
            if r["actual_price"] is not None:
                actual = r["actual_price"]
            rows.append({
                "made_at": r["made_at"],
                "lead_min": (r["target_at"] - r["made_at"]) / 60.0,
                "now_price": r["now_price"],
                "sigma_h_pct": r["sigma_h"] * 100.0,
                "ladder": json.loads(r["ladder_json"]),
            })
    # Para cada row, calcular P(≥actual) si tenemos actual
    if actual is not None:
        for row in rows:
            row["p_above_actual"] = _interp_p_above(row["ladder"], actual)
    return {"symbol": symbol, "target_at": target_at,
            "actual_price": actual, "rows": rows}


def top_shocks(symbol: Optional[str] = None, limit: int = 15,
               min_z: float = 2.0, db_path: str = DB_PATH) -> list[HourResult]:
    """Cierres con |z| más alto — sorpresas que el modelo no anticipó.
    Toma la pred MÁS TEMPRANA de cada cierre (igual que recent_outcomes)."""
    out: list[HourResult] = []
    with _conn(db_path) as c:
        q = ("SELECT p.symbol, p.target_at, p.made_at, p.now_price, p.sigma_h, "
             "       p.ladder_json, o.actual_price "
             "FROM predictions p "
             "JOIN outcomes o ON o.prediction_id = p.id "
             "JOIN ( SELECT symbol, target_at, MIN(made_at) AS min_made "
             "       FROM predictions GROUP BY symbol, target_at ) f "
             "  ON f.symbol = p.symbol AND f.target_at = p.target_at "
             " AND f.min_made = p.made_at ")
        params: tuple = ()
        if symbol:
            q += "WHERE p.symbol = ? "
            params = (symbol,)
        for row in c.execute(q, params):
            ladder = json.loads(row["ladder_json"])
            actual = row["actual_price"]
            sigma_pct = row["sigma_h"] * 100.0
            if sigma_pct <= 0:
                continue
            diff_pct = (actual - row["now_price"]) / row["now_price"] * 100.0
            z = abs(diff_pct) / sigma_pct
            if z < min_z:
                continue
            out.append(HourResult(
                symbol=row["symbol"], target_at=row["target_at"],
                actual_price=actual, pred_price=row["now_price"],
                lead_min=(row["target_at"] - row["made_at"]) / 60.0,
                sigma_h_pct=sigma_pct, diff_pct=diff_pct, z_actual=z,
                p_above_actual=_interp_p_above(ladder, actual),
            ))
    out.sort(key=lambda h: h.z_actual, reverse=True)
    return out[:limit]


def tail_stats(symbol: Optional[str] = None,
               db_path: str = DB_PATH) -> dict:
    """Frecuencia observada de |z|>k vs esperada (T_4 var-matched).
    k ∈ {1.5, 2, 2.5, 3, 4}."""
    import math as _math
    zs: list[float] = []
    with _conn(db_path) as c:
        q = ("SELECT p.now_price, p.sigma_h, o.actual_price "
             "FROM predictions p JOIN outcomes o ON o.prediction_id = p.id "
             "JOIN ( SELECT symbol, target_at, MIN(made_at) AS min_made "
             "       FROM predictions GROUP BY symbol, target_at ) f "
             "  ON f.symbol = p.symbol AND f.target_at = p.target_at "
             " AND f.min_made = p.made_at ")
        params: tuple = ()
        if symbol:
            q += "WHERE p.symbol = ? "
            params = (symbol,)
        for row in c.execute(q, params):
            if row["sigma_h"] <= 0:
                continue
            z = abs(_math.log(row["actual_price"] / row["now_price"])
                    / row["sigma_h"])
            zs.append(z)
    n = len(zs)
    levels = [1.5, 2.0, 2.5, 3.0, 4.0]
    rows = []
    for k in levels:
        arg = k * _math.sqrt(2)
        expected = 2.0 * (1.0 - _pred._t4_cdf(arg))
        observed = sum(1 for z in zs if z > k) / n if n else 0.0
        rows.append({"k": k, "expected": expected,
                     "observed": observed, "n_above": int(observed * n)})
    mean_abs_z = sum(zs) / n if n else 0.0
    return {"n_total": n, "mean_abs_z": mean_abs_z, "levels": rows}


def kalshi_compare(divergence_pp: float = 5.0,
                   db_path: str = DB_PATH) -> dict:
    """Compara modelo vs Kalshi sobre predicciones settleadas que tienen
    kalshi_json. Sólo BTC.
    Devuelve:
      n_total, n_diverge (|model-kalshi| > divergence_pp),
      mean_abs_align, brier_model, brier_kalshi,
      diverge_model_wins / kalshi_wins (count cuando difieren).
    """
    n_total = 0
    diffs: list[float] = []   # |model - kalshi| en pp para alineación
    brier_m_sum = 0.0
    brier_k_sum = 0.0
    n_pairs = 0
    div_model_wins = 0
    div_kalshi_wins = 0
    n_diverge = 0
    with _conn(db_path) as c:
        # 1 fila por (symbol, target_at) — tomamos la pred MÁS TEMPRANA
        for row in c.execute(
            "SELECT p.ladder_json, p.kalshi_json, o.actual_price "
            "FROM predictions p "
            "JOIN outcomes o ON o.prediction_id = p.id "
            "JOIN ( SELECT symbol, target_at, MIN(made_at) AS mn "
            "       FROM predictions WHERE kalshi_json IS NOT NULL "
            "       GROUP BY symbol, target_at ) f "
            "  ON f.symbol=p.symbol AND f.target_at=p.target_at AND f.mn=p.made_at "
            "WHERE p.kalshi_json IS NOT NULL AND p.symbol='BTCUSDT'"
        ):
            ladder = {r["threshold"]: r["p_above"]
                      for r in json.loads(row["ladder_json"])}
            kcurve = {r["threshold"]: r["kalshi_p"]
                      for r in json.loads(row["kalshi_json"])
                      if r.get("kalshi_p") is not None}
            actual = row["actual_price"]
            if not kcurve:
                continue
            n_total += 1
            for thr, kp in kcurve.items():
                mp = ladder.get(thr)
                if mp is None:
                    continue
                outcome = 1 if actual > thr else 0
                bm = (mp - outcome) ** 2
                bk = (kp - outcome) ** 2
                brier_m_sum += bm
                brier_k_sum += bk
                n_pairs += 1
                diff = abs(mp - kp) * 100  # pp
                diffs.append(diff)
                if diff > divergence_pp:
                    n_diverge += 1
                    if bm < bk:
                        div_model_wins += 1
                    elif bk < bm:
                        div_kalshi_wins += 1
    return {
        "n_total": n_total,
        "n_pairs": n_pairs,
        "n_diverge": n_diverge,
        "mean_abs_align_pp": sum(diffs) / len(diffs) if diffs else 0.0,
        "brier_model": brier_m_sum / n_pairs if n_pairs else None,
        "brier_kalshi": brier_k_sum / n_pairs if n_pairs else None,
        "div_model_wins": div_model_wins,
        "div_kalshi_wins": div_kalshi_wins,
        "div_threshold_pp": divergence_pp,
    }


def auto_bet_backtest(min_edge_pp: float = 5.0,
                      db_path: str = DB_PATH) -> dict:
    """Simula auto-bet sobre el histórico settleado de BTC.

    Para cada cierre con Kalshi data, toma la predicción MÁS TEMPRANA y
    calcula edge_pp = (our_p − kalshi_p) × 100 por threshold. Si
    |edge_pp| ≥ min_edge_pp y kalshi_p ∈ (0.01, 0.99), abre 1 contrato
    YES (edge>0) o NO (edge<0). Kalshi paga $1 si el contrato gana.

    PnL por contrato = payout − cost ∈ [−1, +1].
    """
    n_bets = 0
    n_wins = 0
    pnl_sum = 0.0
    cost_sum = 0.0
    rows: list[dict] = []
    with _conn(db_path) as c:
        for row in c.execute(
            "SELECT p.target_at, p.ladder_json, p.kalshi_json, o.actual_price "
            "FROM predictions p "
            "JOIN outcomes o ON o.prediction_id=p.id "
            "JOIN ( SELECT symbol, target_at, MIN(made_at) AS mn "
            "       FROM predictions WHERE kalshi_json IS NOT NULL "
            "       GROUP BY symbol, target_at ) f "
            "  ON f.symbol=p.symbol AND f.target_at=p.target_at "
            " AND f.mn=p.made_at "
            "WHERE p.kalshi_json IS NOT NULL AND p.symbol='BTCUSDT' "
            "ORDER BY p.target_at DESC"
        ):
            ladder = {r["threshold"]: r["p_above"]
                      for r in json.loads(row["ladder_json"])}
            kcurve = {r["threshold"]: r["kalshi_p"]
                      for r in json.loads(row["kalshi_json"])
                      if r.get("kalshi_p") is not None}
            actual = row["actual_price"]
            target = row["target_at"]
            for thr, kp in kcurve.items():
                mp = ladder.get(thr)
                if mp is None or not (0.01 < kp < 0.99):
                    continue
                edge_pp = (mp - kp) * 100
                if abs(edge_pp) < min_edge_pp:
                    continue
                hit = actual > thr
                if edge_pp > 0:
                    cost = kp
                    payout = 1.0 if hit else 0.0
                    side = "YES"
                else:
                    cost = 1.0 - kp
                    payout = 0.0 if hit else 1.0
                    side = "NO"
                pnl = payout - cost
                pnl_sum += pnl
                cost_sum += cost
                n_bets += 1
                if pnl > 0:
                    n_wins += 1
                rows.append({
                    "target_at": target,
                    "threshold": thr,
                    "edge_pp": edge_pp,
                    "side": side,
                    "cost": cost,
                    "pnl": pnl,
                    "won": pnl > 0,
                })
    return {
        "n_bets": n_bets,
        "n_wins": n_wins,
        "win_rate": n_wins / n_bets if n_bets else 0.0,
        "gross_pnl": pnl_sum,
        "cost_sum": cost_sum,
        "roi": pnl_sum / cost_sum if cost_sum else 0.0,
        "min_edge_pp": min_edge_pp,
        "rows": rows[:20],
    }


def overall_brier(symbol: Optional[str] = None,
                  db_path: str = DB_PATH) -> Optional[float]:
    total, n = 0.0, 0
    with _conn(db_path) as c:
        q = ("SELECT p.ladder_json, o.actual_price FROM predictions p "
             "JOIN outcomes o ON o.prediction_id = p.id")
        params: tuple = ()
        if symbol:
            q += " WHERE p.symbol = ?"
            params = (symbol,)
        for row in c.execute(q, params):
            ladder = json.loads(row["ladder_json"])
            actual = row["actual_price"]
            for r in ladder:
                p_pred = r["p_above"]
                outcome = 1 if actual > r["threshold"] else 0
                total += (p_pred - outcome) ** 2
                n += 1
    return None if n == 0 else total / n
