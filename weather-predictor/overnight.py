"""Overnight divergence check + daily skip flag.

A las ~22:00 AST cada día, computa la divergencia entre nuestra pred D+1 y la
mediana de modelos externos para mañana. Si |diff| ≥ DIVERGENCE_THR_F, push
ntfy y graba un skip flag que bets.maybe_bet consulta para no auto-betear esa
estación al día siguiente.
"""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "calibration.db"
DIVERGENCE_THR_F = 2.0  # |our_pred - ext_med| sobre este → skip
AST = ZoneInfo("America/Puerto_Rico")
SWEEP_HOUR_AST = 22  # hora AST a la que dispara el sweep


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS daily_skip_flags (
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            reason TEXT NOT NULL,
            our_pred_f REAL,
            ext_med_f REAL,
            diff_f REAL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (station_id, date)
        );
    """)
    return c


def set_skip(station_id: str, target_date: date, reason: str,
             our_pred_f: float | None = None,
             ext_med_f: float | None = None,
             diff_f: float | None = None) -> None:
    c = _conn()
    try:
        c.execute("""INSERT OR REPLACE INTO daily_skip_flags
                     (station_id, date, reason, our_pred_f, ext_med_f, diff_f, created_at)
                     VALUES (?,?,?,?,?,?,?)""",
                  (station_id, target_date.isoformat(), reason,
                   our_pred_f, ext_med_f, diff_f, datetime.utcnow().isoformat()))
        c.commit()
    finally:
        c.close()


def is_skipped(station_id: str, target_date: date) -> tuple[bool, str | None]:
    c = _conn()
    try:
        row = c.execute("""SELECT reason FROM daily_skip_flags
                           WHERE station_id=? AND date=?""",
                        (station_id, target_date.isoformat())).fetchone()
    finally:
        c.close()
    if row:
        return True, row[0]
    return False, None


def check_divergence(station_id: str, target_date: date,
                     day_offset: int = 1) -> dict | None:
    """Compara nuestra pred D+offset vs mediana de modelos externos. None si no se puede."""
    from predictor import fetch_station
    import multi_day as _md
    import external_models as _em
    try:
        station = fetch_station(station_id)
    except Exception:
        return None
    fc = _md.day_forecast(station, day_offset)
    our_pred = fc.get("p50")
    if our_pred is None:
        return None
    mm = _em.fetch_multi_model_max(station, day_offset=day_offset)
    if mm is None:
        return None
    diff = our_pred - mm.median
    return {
        "station_id": station_id,
        "target_date": target_date,
        "our_pred_f": our_pred,
        "ext_med_f": mm.median,
        "ext_spread_f": mm.spread,
        "diff_f": diff,
        "divergent": abs(diff) >= DIVERGENCE_THR_F,
    }


def run_sweep(station_ids: list[str], target_date: date | None = None) -> list[dict]:
    """Itera estaciones, alerta y flagea las divergentes. Devuelve los resultados."""
    import notify as _notify
    if target_date is None:
        target_date = datetime.now(AST).date() + timedelta(days=1)
    results = []
    for sid in station_ids:
        r = check_divergence(sid, target_date, day_offset=1)
        if r is None:
            continue
        results.append(r)
        if r["divergent"]:
            set_skip(sid, target_date, "overnight_divergence",
                     our_pred_f=r["our_pred_f"], ext_med_f=r["ext_med_f"],
                     diff_f=r["diff_f"])
            if _notify.enabled():
                sign = "+" if r["diff_f"] > 0 else ""
                _notify.send(
                    f"Divergencia overnight · {sid}",
                    f"Mañana {target_date.isoformat()}: nuestra pred "
                    f"{r['our_pred_f']:.1f}°F vs ensemble {r['ext_med_f']:.1f}°F "
                    f"({sign}{r['diff_f']:.1f}°F). Auto-bet bloqueado.",
                    priority="high",
                    tags=["warning"],
                )
    return results
