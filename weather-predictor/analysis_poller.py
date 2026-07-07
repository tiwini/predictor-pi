"""Background cycler que recorre las 5 estaciones cada N min y guarda
snapshots a analysis.db para alimentar el tab /analysis del dashboard.

Reusa predictor.build_snapshot y kalshi.fetch_bins (que internamente
respetan TTL cache de 10 min, así que invocar cada 10 min está alineado).

Schema:
  station_snapshots: ts, station, current_f, ens_med, ens_p10, ens_p90, ens_maxes_json
  kalshi_snapshots: ts, station, ticker, bin_lo, bin_hi, label, yes_mid, our_p
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from predictor import build_snapshot, fetch_station, _compute_final_our_p_per_bin
import kalshi
import agent_signals as A

from stations import STATION_IDS as STATIONS  # noqa: E402
INTERVAL_S = 600  # 10 min (20 estaciones × ~12s = ~4 min, deja 6 min margen)
DB_PATH = Path(__file__).parent / "analysis.db"
CALIBRATION_DB = Path(__file__).parent / "calibration.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [analysis_poller] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("analysis_poller")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA busy_timeout=5000")  # race vs agent_monitor reads
    c.executescript("""
        CREATE TABLE IF NOT EXISTS station_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            station TEXT NOT NULL,
            current_f REAL,
            today_max_obs REAL,
            ens_med REAL,
            ens_p10 REAL,
            ens_p90 REAL,
            ens_maxes_json TEXT,
            peak_status TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ss_station_ts
            ON station_snapshots(station, ts);
        -- regime_tag añadido 2026-06-22: stable | heatwave | cold_snap |
        -- marine_bimodal | transition | regime_break (ver regime.py).

        CREATE TABLE IF NOT EXISTS kalshi_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            station TEXT NOT NULL,
            ticker TEXT NOT NULL,
            bin_lo REAL NOT NULL,
            bin_hi REAL NOT NULL,
            label TEXT,
            yes_mid REAL,
            our_p REAL
        );
        CREATE INDEX IF NOT EXISTS idx_ks_station_ts
            ON kalshi_snapshots(station, ts);
        CREATE INDEX IF NOT EXISTS idx_ks_bin
            ON kalshi_snapshots(station, bin_lo, bin_hi, ts);
    """)
    # Idempotent ADD COLUMN para regime_tag (poblada por regime.classify).
    existing = {r[1] for r in c.execute(
        "PRAGMA table_info(station_snapshots)").fetchall()}
    if "regime_tag" not in existing:
        c.execute("ALTER TABLE station_snapshots ADD COLUMN regime_tag TEXT")
    if "regime_reason" not in existing:
        c.execute("ALTER TABLE station_snapshots ADD COLUMN regime_reason TEXT")
    # Wind + termodinámica (NWS METAR), persisted desde 2026-06-25 para
    # backtest viento-vs-error (marine layer, sea breeze, chinook) y
    # presión/dewpoint-vs-error (frentes sinópticos, techo termodinámico).
    for col, typ in [("wind_mph", "REAL"), ("wind_dir_deg", "REAL"),
                     ("wind_dir_card", "TEXT"), ("wind_gust_mph", "REAL"),
                     ("wind_chill_f", "REAL"),
                     ("pressure_inhg", "REAL"),
                     ("pressure_trend_3h", "REAL"),
                     ("dewpoint_f", "REAL"),
                     ("humidity_pct", "REAL"),
                     ("today_min_obs", "REAL")]:
        if col not in existing:
            c.execute(f"ALTER TABLE station_snapshots ADD COLUMN {col} {typ}")
    # Codex Round 5 (2026-06-29): señales para que agent_monitor las lea sin
    # tener que recomputar el pipeline ni hacer fetch a predictor_web.
    for col, typ in [("pred_calibrated_f", "REAL"),
                     ("bias_f", "REAL"), ("bias_applied", "INTEGER"),
                     ("bias_path", "TEXT"),
                     ("ext_med_f", "REAL"), ("ext_spread_f", "REAL"),
                     ("ext_diff_f", "REAL"),
                     ("difficulty_score", "REAL"),
                     ("difficulty_label", "TEXT"),
                     ("difficulty_reasons_json", "TEXT"),
                     ("cold_bias_block", "INTEGER"),
                     ("streak_block_hot", "INTEGER"),
                     ("streak_block_cold", "INTEGER"),
                     ("roi_hist_pct", "REAL"),
                     ("trades_settled", "INTEGER"),
                     ("wins_settled", "INTEGER"),
                     ("roi_cold_pct", "REAL"), ("trades_cold", "INTEGER"),
                     ("roi_hot_pct", "REAL"), ("trades_hot", "INTEGER"),
                     ("roi_mid_pct", "REAL"), ("trades_mid", "INTEGER"),
                     ("brier_us_7d", "REAL"),
                     ("brier_kalshi_7d", "REAL"),
                     ("signal_error", "TEXT")]:
        if col not in existing:
            c.execute(f"ALTER TABLE station_snapshots ADD COLUMN {col} {typ}")
    # kalshi_snapshots: añadimos our_p_calibrated (raw `our_p` se mantiene).
    existing_k = {r[1] for r in c.execute(
        "PRAGMA table_info(kalshi_snapshots)").fetchall()}
    if "our_p_calibrated" not in existing_k:
        c.execute("ALTER TABLE kalshi_snapshots ADD COLUMN our_p_calibrated REAL")
    c.commit()
    return c


def _brier_7d(station_id: str) -> tuple[float | None, float | None]:
    """AVG(our_brier), AVG(kalshi_brier) en los últimos 7 días (day_summary)."""
    if not CALIBRATION_DB.exists():
        return None, None
    try:
        cc = sqlite3.connect(CALIBRATION_DB)
        cc.execute("PRAGMA busy_timeout=5000")
        row = cc.execute(
            "SELECT AVG(our_brier), AVG(kalshi_brier) FROM day_summary "
            "WHERE station_id=? AND date >= date('now', '-7 days')",
            (station_id,),
        ).fetchone()
        cc.close()
        return (row[0], row[1]) if row else (None, None)
    except Exception:
        return None, None


def _compute_signals(station_id: str, snap) -> dict:
    """Deriva todas las señales del snapshot + lookups a calibration.db.

    Devuelve dict con las columnas nuevas listas para INSERT. En caso de
    error parcial, persiste lo que pueda y `signal_error` con la causa.
    """
    out: dict = {
        "pred_calibrated_f": None, "bias_f": None, "bias_applied": None,
        "bias_path": None, "ext_med_f": None, "ext_spread_f": None,
        "ext_diff_f": None, "difficulty_score": None, "difficulty_label": None,
        "difficulty_reasons_json": None, "cold_bias_block": None,
        "streak_block_hot": None, "streak_block_cold": None,
        "roi_hist_pct": None, "trades_settled": None, "wins_settled": None,
        "roi_cold_pct": None, "trades_cold": None,
        "roi_hot_pct": None, "trades_hot": None,
        "roi_mid_pct": None, "trades_mid": None,
        "brier_us_7d": None, "brier_kalshi_7d": None, "signal_error": None,
    }
    errors: list[str] = []

    try:
        maxes = sorted(snap.ensemble_daily_maxes or [])
        if maxes:
            out["pred_calibrated_f"] = maxes[len(maxes) // 2]
    except Exception as e:
        errors.append(f"pred_calibrated:{e}")

    try:
        out["bias_f"] = float(snap.bias_correction_f or 0.0)
        bi = snap.bias_info or {}
        out["bias_applied"] = 1 if bi.get("applied") else 0
        out["bias_path"] = bi.get("bias_path")
    except Exception as e:
        errors.append(f"bias:{e}")

    try:
        info = snap.ext_shift_info or {}
        out["ext_med_f"] = info.get("ext_med")
        out["ext_spread_f"] = info.get("ext_spread")
        if out["pred_calibrated_f"] is not None and out["ext_med_f"] is not None:
            out["ext_diff_f"] = out["pred_calibrated_f"] - out["ext_med_f"]
    except Exception as e:
        errors.append(f"ext:{e}")

    try:
        import difficulty as _diff
        clim_pct = None
        if snap.climatology is not None:
            clim_pct = getattr(snap.climatology, "percentile", None)
        n_members = len(snap.ensemble_raw_maxes) or len(snap.ensemble_daily_maxes) or 31
        dd = _diff.compute(
            ens_p10=sorted(snap.ensemble_daily_maxes)[int(len(snap.ensemble_daily_maxes) * 0.1)]
                    if snap.ensemble_daily_maxes else None,
            ens_p90=sorted(snap.ensemble_daily_maxes)[int(len(snap.ensemble_daily_maxes) * 0.9)]
                    if snap.ensemble_daily_maxes else None,
            eff_n=snap.ensemble_eff_n, total_members=n_members,
            clim_percentile=clim_pct, p_notable_precip=None,
            regime_breaks=len(snap.regime_break_hours or []),
        )
        out["difficulty_score"] = dd.score
        out["difficulty_label"] = dd.label
        out["difficulty_reasons_json"] = json.dumps(dd.reasons)
    except Exception as e:
        errors.append(f"difficulty:{e}")

    try:
        out["cold_bias_block"] = 1 if A.cold_bias_blocks_yes(snap.bias_info) else 0
    except Exception as e:
        errors.append(f"cold_bias:{e}")

    try:
        sb = A.streaks_by_direction(station_id, str(CALIBRATION_DB),
                                    our_pred_f=out["pred_calibrated_f"])
        out["streak_block_hot"] = sb.get("hot", 0)
        out["streak_block_cold"] = sb.get("cold", 0)
    except Exception as e:
        errors.append(f"streak:{e}")

    try:
        r = A.historical_roi(station_id, str(CALIBRATION_DB))
        out["roi_hist_pct"] = r["roi_pct"]
        out["trades_settled"] = r["trades"]
        out["wins_settled"] = r["wins"]
        by_dir = r.get("by_direction") or {}
        for d in ("cold", "hot", "mid"):
            dd = by_dir.get(d)
            if dd:
                out[f"roi_{d}_pct"] = dd["roi_pct"]
                out[f"trades_{d}"] = dd["trades"]
    except Exception as e:
        errors.append(f"roi:{e}")

    try:
        bu, bk = _brier_7d(station_id)
        out["brier_us_7d"] = bu
        out["brier_kalshi_7d"] = bk
    except Exception as e:
        errors.append(f"brier:{e}")

    if errors:
        out["signal_error"] = "; ".join(errors)[:500]
    return out


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * pct)))
    return sorted_vals[idx]


def poll_one(station_id: str, c: sqlite3.Connection) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    log.info("polling %s", station_id)
    try:
        station = fetch_station(station_id)
        snap = build_snapshot(station)
    except Exception as e:
        log.warning("  build_snapshot %s failed: %s", station_id, e)
        return

    maxes = sorted(snap.ensemble_daily_maxes)
    med = _percentile(maxes, 0.5)
    p10 = _percentile(maxes, 0.1)
    p90 = _percentile(maxes, 0.9)

    try:
        import regime
        rt = regime.classify(snap, station_id, snap.station_local)
        regime_tag, regime_reason = rt.tag, rt.reason
    except Exception as e:
        log.warning("  regime.classify %s failed: %s", station_id, e)
        regime_tag, regime_reason = None, None

    sig = _compute_signals(station_id, snap)

    c.execute("""INSERT INTO station_snapshots
        (ts, station, current_f, today_max_obs, ens_med, ens_p10, ens_p90,
         ens_maxes_json, peak_status, regime_tag, regime_reason,
         wind_mph, wind_dir_deg, wind_dir_card, wind_gust_mph, wind_chill_f,
         pressure_inhg, pressure_trend_3h, dewpoint_f, humidity_pct,
         today_min_obs,
         pred_calibrated_f, bias_f, bias_applied, bias_path,
         ext_med_f, ext_spread_f, ext_diff_f,
         difficulty_score, difficulty_label, difficulty_reasons_json,
         cold_bias_block, streak_block_hot, streak_block_cold,
         roi_hist_pct, trades_settled, wins_settled,
         roi_cold_pct, trades_cold, roi_hot_pct, trades_hot,
         roi_mid_pct, trades_mid,
         brier_us_7d, brier_kalshi_7d, signal_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ts, station_id, snap.current_temp_f, snap.today_max_obs,
         med, p10, p90, json.dumps(maxes), snap.peak_status,
         regime_tag, regime_reason,
         snap.wind_mph, snap.wind_dir_deg, snap.wind_dir_card,
         snap.wind_gust_mph, snap.wind_chill_f,
         snap.pressure_inhg, snap.pressure_trend_3h,
         snap.dewpoint_f, snap.humidity_pct,
         snap.today_min_obs,
         sig["pred_calibrated_f"], sig["bias_f"], sig["bias_applied"], sig["bias_path"],
         sig["ext_med_f"], sig["ext_spread_f"], sig["ext_diff_f"],
         sig["difficulty_score"], sig["difficulty_label"], sig["difficulty_reasons_json"],
         sig["cold_bias_block"], sig["streak_block_hot"], sig["streak_block_cold"],
         sig["roi_hist_pct"], sig["trades_settled"], sig["wins_settled"],
         sig["roi_cold_pct"], sig["trades_cold"],
         sig["roi_hot_pct"], sig["trades_hot"],
         sig["roi_mid_pct"], sig["trades_mid"],
         sig["brier_us_7d"], sig["brier_kalshi_7d"], sig["signal_error"]))

    # Kalshi bins (puede que no haya mercado abierto = []).
    today = snap.station_local.date()
    try:
        bins = kalshi.fetch_bins(station_id, today)
    except Exception as e:
        log.warning("  kalshi.fetch_bins %s failed: %s", station_id, e)
        bins = []

    # our_p_calibrated: full pipeline (isotonic + blend_with_external) — lo que
    # ve el usuario en /comparison y debería ver el AI. raw `our_p` se mantiene
    # por backwards-compat con código que asume conteo crudo.
    try:
        cal_ps = _compute_final_our_p_per_bin(station_id, snap, bins) if bins else []
    except Exception as e:
        log.warning("  _compute_final_our_p_per_bin %s failed: %s", station_id, e)
        cal_ps = [None] * len(bins)

    for i, b in enumerate(bins):
        our_p = kalshi.our_p_for_bin(snap.ensemble_daily_maxes, b.bin_lo, b.bin_hi)
        our_p_cal = cal_ps[i] if i < len(cal_ps) else None
        c.execute("""INSERT INTO kalshi_snapshots
            (ts, station, ticker, bin_lo, bin_hi, label, yes_mid, our_p, our_p_calibrated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, station_id, b.ticker, b.bin_lo, b.bin_hi, b.label,
             b.yes_mid, our_p, our_p_cal))
    c.commit()
    log.info("  saved %s: current=%.1f med=%.1f kalshi_bins=%d diff=%s%s",
             station_id, snap.current_temp_f or 0, med, len(bins),
             f"{sig['difficulty_score']:.0f}" if sig['difficulty_score'] is not None else "?",
             f" [signal_err: {sig['signal_error'][:40]}]" if sig['signal_error'] else "")


def cleanup_old(c: sqlite3.Connection, keep_days: int = 30) -> None:
    """Borra snapshots > keep_days. ~260 MB/año con todo; 30 días sobra para
    el tab de análisis. Histórico largo va a calibration.db (otro proyecto)."""
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    c.execute("DELETE FROM station_snapshots WHERE ts < ?", (cutoff_iso,))
    c.execute("DELETE FROM kalshi_snapshots WHERE ts < ?", (cutoff_iso,))
    c.commit()


def main() -> None:
    log.info("analysis_poller started; interval=%ds stations=%s",
             INTERVAL_S, STATIONS)
    cycle = 0
    while True:
        c = _conn()
        for sid in STATIONS:
            poll_one(sid, c)
        cycle += 1
        if cycle % 144 == 0:  # ~1 vez al día
            cleanup_old(c)
            log.info("cleanup ejecutado")
        c.close()
        log.info("ciclo completo; durmiendo %ds", INTERVAL_S)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")
        sys.exit(0)
