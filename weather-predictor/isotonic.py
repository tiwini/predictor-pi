"""Isotonic regression calibrator (Pool-Adjacent-Violators, sin sklearn).

Input: (predicted_p, outcome) pairs desde prediction_snapshots settleados.
Fit: PAV produce una función monótona no-decreciente p_raw → p_cal.
Apply: interpolación lineal entre centros de bloques.

Se mantiene un calibrador en memoria por station_id; refit() lo regenera
desde la DB. Solo retorna un calibrador si hay al menos MIN_N ejemplos.
"""
from dataclasses import dataclass
from typing import Optional

MIN_N = 20     # mínimo de pares (p, outcome) settleados
MIN_DAYS = 7   # mínimo de días únicos settleados (diversidad, más importante que N)


@dataclass
class Block:
    x_min: float
    x_max: float
    y: float
    n: int


@dataclass
class Calibrator:
    blocks: list  # list[Block], monótono no-decreciente en y
    n_fit: int
    n_days: int
    station_id: Optional[str]


def fit(samples: list, n_days: int = 0) -> Optional[Calibrator]:
    """PAV sobre lista de (x, y), y en {0,1} (o real 0..1).
    Retorna Calibrator o None si samples vacío. n_days opcional — nº de
    días únicos que cubren los samples (para decidir si el fit es confiable).
    """
    if not samples:
        return None
    pts = sorted(samples, key=lambda s: s[0])
    raw = []   # [[x_min, x_max, sum_y, n]]
    for x, y in pts:
        raw.append([float(x), float(x), float(y), 1])
        while len(raw) >= 2:
            a = raw[-2]
            b = raw[-1]
            if a[2] / a[3] <= b[2] / b[3]:
                break
            a[1] = b[1]
            a[2] += b[2]
            a[3] += b[3]
            raw.pop()
    blocks = [Block(b[0], b[1], b[2] / b[3], b[3]) for b in raw]
    return Calibrator(blocks=blocks, n_fit=len(pts), n_days=n_days,
                      station_id=None)


def apply(cal: Optional[Calibrator], p: float) -> float:
    """Linear interp entre centros de bloques; extrapolación clampeada a primera/última y."""
    if cal is None or not cal.blocks:
        return p
    # Center of each block
    centers = [((b.x_min + b.x_max) / 2.0, b.y) for b in cal.blocks]
    if p <= centers[0][0]:
        return centers[0][1]
    if p >= centers[-1][0]:
        return centers[-1][1]
    for i in range(1, len(centers)):
        x0, y0 = centers[i - 1]
        x1, y1 = centers[i]
        if p <= x1:
            if x1 == x0:
                return y1
            t = (p - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return centers[-1][1]


def fit_from_db(station_id: Optional[str] = None,
                p_version: Optional[str] = None,
                op: tuple = ("b",)) -> Optional[Calibrator]:
    """Pull (predicted_p, outcome) from calibration.db prediction_snapshots
    where outcome is known. Returns Calibrator (or None if no data).

    Filters protecting the fit from uncalibratable noise:
      1. Dedupes to one row per (station, date, op, threshold), keeping the
         LAST snapshot before settle. Without this, intra-day repeated polls
         (often with identical p) flood PAV with correlated samples.
      2. Default op=('b',) — solo bins Kalshi post-instrumentation. Legacy
         op='>' viene del sistema pre-Kalshi de point-threshold predictions
         (predicted_p semánticamente distinto a bin_p → PAV no debería
         poolearlos). Callers que quieran incluir legacy pasan
         op=('>','<','b'). Excluye siempre op='~' (closeness a decimal
         target; NWS settles a integer °F → outcome=0 casi siempre).
      3. Optional p_version filter: pass 'post_laplace' cuando N lo permita
         para excluir pairs pre-Laplace donde predicted_p vive en [0,1]
         saturada; mezclar no rompe PAV pero saturados dominan bloques
         extremos.
    """
    import sqlite3
    from calibration import DB_PATH
    c = sqlite3.connect(DB_PATH)
    ops_ph = ",".join("?" * len(op))
    base = f"""
        SELECT predicted_p, outcome, date FROM prediction_snapshots ps1
        WHERE outcome IS NOT NULL
          AND op IN ({ops_ph})
          AND snapshot_time = (
            SELECT MAX(snapshot_time) FROM prediction_snapshots ps2
            WHERE ps2.station_id = ps1.station_id
              AND ps2.date = ps1.date
              AND ps2.op = ps1.op
              AND ps2.threshold = ps1.threshold
              AND ps2.outcome IS NOT NULL
          )
    """
    params: list = list(op)
    if station_id:
        base += " AND station_id=?"
        params.append(station_id)
    if p_version is not None:
        base += " AND p_version=?"
        params.append(p_version)
    cur = c.execute(base, params)
    rows = cur.fetchall()
    c.close()
    pairs = [(p, o) for p, o, _ in rows]
    days = len({d for _, _, d in rows})
    cal = fit(pairs, n_days=days)
    if cal is not None:
        cal.station_id = station_id
    return cal


# In-memory cache: station_id (or "__ALL__") → Calibrator | None | sentinel
_cache: dict = {}
_NOT_FIT = object()


def get(station_id: Optional[str] = None) -> Optional[Calibrator]:
    key = station_id or "__ALL__"
    hit = _cache.get(key, _NOT_FIT)
    if hit is _NOT_FIT:
        hit = fit_from_db(station_id)
        _cache[key] = hit
    return hit


def refit(station_id: Optional[str] = None) -> Optional[Calibrator]:
    key = station_id or "__ALL__"
    _cache.pop(key, None)
    return get(station_id)


def invalidate_all() -> None:
    _cache.clear()


def reliability_curve(cal: Optional[Calibrator], n_buckets: int = 10) -> list:
    """Para visualizar: de 0..1 en n_buckets puntos evaluar apply()."""
    if cal is None or not cal.blocks:
        return []
    out = []
    for i in range(n_buckets + 1):
        p = i / n_buckets
        out.append((p, apply(cal, p)))
    return out


def brier(samples: list, cal: Optional[Calibrator]) -> float:
    if not samples:
        return 0.0
    if cal is None:
        return sum((p - o) ** 2 for p, o in samples) / len(samples)
    return sum((apply(cal, p) - o) ** 2 for p, o in samples) / len(samples)
