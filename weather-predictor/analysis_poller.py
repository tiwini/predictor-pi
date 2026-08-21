"""Background cycler que recorre las 20 estaciones cada N min y guarda
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

from predictor import (build_snapshot, fetch_station,
                       _compute_final_our_p_per_bin, obs_floor_from_snapshot)
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
    # WAL + timeout largo (2026-08-05): en modo `delete` un lector bloquea al
    # escritor, y los backtests que escanean cientos de miles de filas de
    # kalshi_snapshots tardan más de 5 s. El poller murió con "database is
    # locked" justo así. Con WAL los lectores no bloquean, y el timeout cubre
    # el checkpoint.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
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
                     ("today_min_obs", "REAL"),
                     ("convective_ambient", "INTEGER"),
                     ("narrative_line", "TEXT"),
                     # Piso de observación (2026-07-26): sin estas dos no hay
                     # forma de medir después si el clamp cambió algo ni cuántas
                     # veces actuó por estación.
                     ("obs_floor_n", "INTEGER"),
                     ("obs_floor_delta_f", "REAL"),
                     # F2 pieza 1 (2026-07-26): max del CLI parcial de la tarde
                     # y su issuanceTime. Separado de today_max_obs a propósito:
                     # esa columna es la base de las mediciones de gap y no se
                     # le cambia la semántica a mitad de serie.
                     ("today_max_cli", "REAL"),
                     ("today_max_cli_ts", "TEXT"),
                     # 2026-07-28: sin la hora del max, un max_obs de madrugada
                     # se lee como progreso hacia el pico. KMDW hoy tenía 75.0
                     # de las 00:53 con la temperatura en 69 a las 09:30.
                     ("today_max_obs_ts", "TEXT"),
                     # Instrumentación 2026-07-28: corrector de nivel por
                     # mediana histórica. TELEMETRÍA — no entra en la
                     # distribución ni en ningún gate. Se compara en vivo
                     # contra el EWMA antes de decidir si lo sustituye.
                     ("bias_median_causal_f", "REAL"),
                     ("bias_median_n", "INTEGER"),
                     # 2026-08-14: minutos que current lleva sin cambiar, de la
                     # serie METAR aceptada. Se persiste porque physical_gate lo
                     # necesita y derivarlo de los snapshots del poller pierde
                     # resolución: con cadencia de 10 min, una meseta de 67 min
                     # se veía como 35 (KNYC 08-12) y la guarda no disparaba.
                     ("current_stable_min", "INTEGER"),
                     # 2026-08-14: hora de la observación que produjo `current`.
                     # Sin esto no se puede distinguir "la temperatura está
                     # estable" de "la estación dejó de reportar": KNYC estuvo
                     # 70 min sin publicar con el pico ocurriendo y ninguna
                     # señal de la DB lo delataba.
                     ("current_obs_ts", "TEXT"),
                     # 2026-08-20: el grupo ASOS de 6h del METAR (`1sTTT`), que
                     # es la MISMA fuente con la que liquida el NWS. Se calcula
                     # desde el 2026-07-15 y se muestra, pero no se guardaba: al
                     # ir a medir si debe entrar en el piso no había histórico y
                     # hubo que reconstruirlo desde METARs crudos. Tercera vez
                     # que pasa, tras current_temp_stable_min y current_obs_ts.
                     ("today_max_asos_6h", "REAL"),
                     ("today_max_asos_6h_ts", "TEXT"),
                     # 2026-08-21: máximo corrido del feed de 5 min. Se guarda
                     # desde el primer día, no como las tres anteriores que hubo
                     # que reconstruir a mano cuando se fue a medirlas.
                     ("today_max_5min", "REAL")]:
        if col not in existing:
            c.execute(f"ALTER TABLE station_snapshots ADD COLUMN {col} {typ}")
    # 2026-07-27: `pred_calibrated_f` nunca estuvo calibrada — se poblaba con
    # la mediana de snap.ensemble_daily_maxes, la misma operación con la que la
    # línea de al lado calcula `ens_med`, y salieron idénticas en los 72910
    # snapshots que tenían ambas. El valor es correcto para lo único que lo
    # consume (`direction_of`, que quiere la mediana del ensemble y por eso
    # `bets` le pasa `our_pred_f=pred_med`); el nombre era el que mentía.
    # RENAME preserva el histórico — la serie no cambia de significado, sólo
    # deja de prometer una calibración que no ocurría. La calibración de verdad
    # vive ahora en `pred_iso_med_f`, más abajo.
    if "pred_calibrated_f" in existing and "our_pred_f" not in existing:
        c.execute("ALTER TABLE station_snapshots "
                  "RENAME COLUMN pred_calibrated_f TO our_pred_f")
        existing = {r[1] for r in c.execute(
            "PRAGMA table_info(station_snapshots)").fetchall()}

    # Codex Round 5 (2026-06-29): señales para que agent_monitor las lea sin
    # tener que recomputar el pipeline ni hacer fetch a predictor_web.
    for col, typ in [("our_pred_f", "REAL"),
                     # Mediana implícita de la distribución YA calibrada
                     # (isotonic + blend), vía agent_signals.implied_median_f.
                     # Telemetría: ningún gate la lee. Es la única forma de
                     # expresar en °F lo que la calibración hace, porque la
                     # isotónica opera sobre probabilidades por bin.
                     ("pred_iso_med_f", "REAL"),
                     ("bias_f", "REAL"), ("bias_applied", "INTEGER"),
                     ("bias_path", "TEXT"),
                     ("ext_med_f", "REAL"), ("ext_spread_f", "REAL"),
                     ("ext_diff_f", "REAL"),
                     # cuánto queda ext_med por debajo del piso ya
                     # observado; NULL = no pasa (lo normal)
                     ("ext_below_floor_f", "REAL"),
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


# ── corrector de nivel por mediana (instrumentación 2026-07-28) ──
#
# TELEMETRÍA: no entra en la distribución ni en ningún gate. Se persiste para
# comparar en vivo contra el EWMA del bias tracker antes de decidir si lo
# sustituye.
#
# Medido sobre 426 station-days (investigacion/backtest_corrector_nivel.py):
#   sin corrección 2.00°F · EWMA actual 1.92°F · mediana causal 1.35°F
# El EWMA aporta 0.08 sobre no hacer nada; la mediana baja 0.57 más. Y la
# versión causal iguala a la leave-one-out, o sea no necesita días futuros.
#
# CAUSAL a propósito: sólo usa días ANTERIORES, que es lo aplicable en
# producción. El sesgo se mide 2h antes de que abra la ventana de pico, el mismo
# momento del backtest.
MEDIAN_BIAS_MIN_DAYS = 5
MEDIAN_BIAS_HOURS_BEFORE_PEAK = 2
_median_bias_cache: dict[tuple, tuple] = {}


def _median_bias_causal(station_id: str, today, c: sqlite3.Connection):
    """(mediana del sesgo histórico, n_días) usando sólo días anteriores."""
    key = (station_id, today.isoformat())
    if key in _median_bias_cache:
        return _median_bias_cache[key]
    try:
        from zoneinfo import ZoneInfo
        from stations import STATION_TZ, PEAK_HOURS
        from datetime import timedelta
        tz = ZoneInfo(STATION_TZ[station_id])
        peak_lo = PEAK_HOURS[station_id][0]
        cal = sqlite3.connect(f"file:{CALIBRATION_DB}?mode=ro", uri=True)
        try:
            settles = dict(cal.execute(
                "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
                "AND date < ?", (station_id, today.isoformat())).fetchall())
        finally:
            cal.close()
        sesgos = []
        for day, settle in sorted(settles.items()):
            if settle is None:
                continue
            try:
                d = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                continue
            ref = (datetime.combine(d, datetime.min.time(), tz)
                   + timedelta(hours=peak_lo - MEDIAN_BIAS_HOURS_BEFORE_PEAK))
            lo = (ref - timedelta(minutes=30)).astimezone(timezone.utc)
            hi = (ref + timedelta(minutes=30)).astimezone(timezone.utc)
            r = c.execute(
                """SELECT ens_med, bias_f, bias_applied FROM station_snapshots
                   WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
                   ORDER BY ts LIMIT 1""",
                (station_id, lo.strftime("%Y-%m-%dT%H:%M:%S"),
                 hi.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
            if r is None:
                continue
            b = r[1] if (r[2] and r[1] is not None) else 0.0
            sesgos.append((r[0] + b) - settle)   # sesgo del ensemble CRUDO
        if len(sesgos) < MEDIAN_BIAS_MIN_DAYS:
            out = (None, len(sesgos))
        else:
            sesgos.sort()
            out = (sesgos[len(sesgos) // 2], len(sesgos))
    except Exception:
        out = (None, 0)
    _median_bias_cache.clear()        # una entrada basta: se recalcula al día
    _median_bias_cache[key] = out
    return out


def _compute_signals(station_id: str, snap) -> dict:
    """Deriva todas las señales del snapshot + lookups a calibration.db.

    Devuelve dict con las columnas nuevas listas para INSERT. En caso de
    error parcial, persiste lo que pueda y `signal_error` con la causa.
    """
    out: dict = {
        "our_pred_f": None, "pred_iso_med_f": None,
        "bias_median_causal_f": None, "bias_median_n": None,
        "bias_f": None, "bias_applied": None,
        "bias_path": None, "ext_med_f": None, "ext_spread_f": None,
        "ext_diff_f": None, "ext_below_floor_f": None,
        "difficulty_score": None, "difficulty_label": None,
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
            out["our_pred_f"] = maxes[len(maxes) // 2]
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
        out["ext_med_f"] = info.get("ext_med")      # crudo, sin tocar
        out["ext_spread_f"] = info.get("ext_spread")
        if out["our_pred_f"] is not None and out["ext_med_f"] is not None:
            # 2026-08-02: los externos a veces pronostican POR DEBAJO del
            # máximo que el día ya alcanzó (KATL: ext 85.6 con 91.04 observado
            # y CLI parcial 92.0). Ese ext_med no es una previsión discrepante,
            # es información ya refutada, y ext_diff calculado contra él marca
            # "sobre-predecimos" justo cuando el modelo va bien.
            # Se clampea al piso para el diff; `ext_med_f` se guarda crudo y la
            # distancia queda en `ext_below_floor_f` para poder auditarlo.
            piso = obs_floor_from_snapshot(snap)
            ext_eff = out["ext_med_f"]
            if piso is not None and ext_eff < piso:
                out["ext_below_floor_f"] = piso - ext_eff
                ext_eff = piso
            out["ext_diff_f"] = out["our_pred_f"] - ext_eff
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
                                    our_pred_f=out["our_pred_f"])
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

    # Los bins se fetchean acá arriba (antes iban después del INSERT) porque
    # `pred_iso_med_f` se deriva de la distribución calibrada por bin y tiene
    # que viajar en la misma fila que el resto de las señales.
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

    sig = _compute_signals(station_id, snap)
    try:
        sig["pred_iso_med_f"] = A.implied_median_f(bins, cal_ps)
    except Exception as e:
        log.warning("  implied_median_f %s failed: %s", station_id, e)
    try:
        mb, mn = _median_bias_causal(station_id, snap.station_local.date(), c)
        sig["bias_median_causal_f"] = mb
        sig["bias_median_n"] = mn
    except Exception as e:
        log.warning("  _median_bias_causal %s failed: %s", station_id, e)

    c.execute("""INSERT INTO station_snapshots
        (ts, station, current_f, today_max_obs, ens_med, ens_p10, ens_p90,
         ens_maxes_json, peak_status, regime_tag, regime_reason,
         wind_mph, wind_dir_deg, wind_dir_card, wind_gust_mph, wind_chill_f,
         pressure_inhg, pressure_trend_3h, dewpoint_f, humidity_pct,
         today_min_obs, convective_ambient, narrative_line,
         current_stable_min, current_obs_ts,
         today_max_asos_6h, today_max_asos_6h_ts, today_max_5min,
         obs_floor_n, obs_floor_delta_f, today_max_cli, today_max_cli_ts,
         today_max_obs_ts,
         our_pred_f, pred_iso_med_f, bias_median_causal_f, bias_median_n,
         bias_f, bias_applied, bias_path,
         ext_med_f, ext_spread_f, ext_diff_f, ext_below_floor_f,
         difficulty_score, difficulty_label, difficulty_reasons_json,
         cold_bias_block, streak_block_hot, streak_block_cold,
         roi_hist_pct, trades_settled, wins_settled,
         roi_cold_pct, trades_cold, roi_hot_pct, trades_hot,
         roi_mid_pct, trades_mid,
         brier_us_7d, brier_kalshi_7d, signal_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ts, station_id, snap.current_temp_f, snap.today_max_obs,
         med, p10, p90, json.dumps(maxes), snap.peak_status,
         regime_tag, regime_reason,
         snap.wind_mph, snap.wind_dir_deg, snap.wind_dir_card,
         snap.wind_gust_mph, snap.wind_chill_f,
         snap.pressure_inhg, snap.pressure_trend_3h,
         snap.dewpoint_f, snap.humidity_pct,
         snap.today_min_obs, 1 if snap.convective_ambient else 0,
         snap.narrative_line or None,
         getattr(snap, "current_temp_stable_min", None),
         (snap.current_obs_time.isoformat()
          if getattr(snap, "current_obs_time", None) else None),
         getattr(snap, "today_max_asos_6h", None),
         (snap.today_max_asos_6h_ts.isoformat()
          if getattr(snap, "today_max_asos_6h_ts", None) else None),
         getattr(snap, "today_max_5min", None),
         snap.obs_floor_n, snap.obs_floor_delta_f,
         snap.today_max_cli,
         snap.today_max_cli_ts.isoformat() if snap.today_max_cli_ts else None,
         snap.today_max_obs_ts.isoformat() if snap.today_max_obs_ts else None,
         sig["our_pred_f"], sig["pred_iso_med_f"],
         sig["bias_median_causal_f"], sig["bias_median_n"],
         sig["bias_f"], sig["bias_applied"], sig["bias_path"],
         sig["ext_med_f"], sig["ext_spread_f"], sig["ext_diff_f"],
         sig["ext_below_floor_f"],
         sig["difficulty_score"], sig["difficulty_label"], sig["difficulty_reasons_json"],
         sig["cold_bias_block"], sig["streak_block_hot"], sig["streak_block_cold"],
         sig["roi_hist_pct"], sig["trades_settled"], sig["wins_settled"],
         sig["roi_cold_pct"], sig["trades_cold"],
         sig["roi_hot_pct"], sig["trades_hot"],
         sig["roi_mid_pct"], sig["trades_mid"],
         sig["brier_us_7d"], sig["brier_kalshi_7d"], sig["signal_error"]))

    # bins y cal_ps ya vienen computados de arriba (los necesita pred_iso_med_f).
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
