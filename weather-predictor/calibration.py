"""Calibration tracking: record predicted probabilities and settle outcomes.

For every poll, we snapshot (assertion, predicted_p) per slot. After a day
closes, we fetch the observed daily max from Open-Meteo archive and mark
each snapshot as hit/miss. Aggregated over time → reliability diagram and
Brier score, so we can see if "70%" really means 70%.
"""
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import nws_cli

DB_PATH = Path(__file__).parent / "calibration.db"
ANALYSIS_DB_PATH = Path(__file__).parent / "analysis.db"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
UA = "weather-predictor/0.1"

# Poller PID 330050 restart deployed Laplace fix ~2026-07-01 22:00 UTC
# (kalshi.py:146). Anterior a esto our_p en kalshi_snapshots viene saturado.
LAPLACE_CUTOFF_TS = "2026-07-01T22:00:00+00:00"


@dataclass
class ReliabilityBucket:
    low: float      # predicted-p bucket lower edge (0-1)
    high: float
    n: int
    mean_pred: float
    hit_rate: float


@dataclass
class ReliabilityReport:
    buckets: list
    total_n: int
    settled_n: int
    brier: float | None     # mean squared error of prob vs outcome
    station_id: str | None  # None = across all stations


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS prediction_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            slot INTEGER NOT NULL,
            is_auto INTEGER NOT NULL,
            expr TEXT NOT NULL,
            op TEXT NOT NULL,
            threshold REAL NOT NULL,
            bin_half REAL,
            predicted_p REAL NOT NULL,
            outcome INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_snap_station_date
            ON prediction_snapshots(station_id, date);
        CREATE INDEX IF NOT EXISTS idx_snap_outcome_null
            ON prediction_snapshots(outcome) WHERE outcome IS NULL;

        CREATE TABLE IF NOT EXISTS day_outcomes (
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            max_obs_f REAL NOT NULL,
            settled_at TEXT NOT NULL,
            PRIMARY KEY (station_id, date)
        );

        CREATE TABLE IF NOT EXISTS day_summary (
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            actual_max_f REAL NOT NULL,
            our_n INTEGER NOT NULL,
            our_brier REAL,
            kalshi_n INTEGER,
            kalshi_brier REAL,
            winning_bin_label TEXT,
            our_p_winning REAL,
            kalshi_p_winning REAL,
            best_edge_abs REAL,
            best_edge_bin_label TEXT,
            best_edge_our_p REAL,
            best_edge_kalshi_p REAL,
            best_edge_correct INTEGER,
            computed_at TEXT NOT NULL,
            PRIMARY KEY (station_id, date)
        );

        -- Fable 2026-07-10 post-KIAH: telemetría de lecturas rechazadas por
        -- el filtro rawMessage/`:53` en fetch_today_obs. ~2 semanas para
        -- distribución sólida de |lectura_5min - accepted|; el percentil
        -- decide el umbral X°F al graduar a guarda por vecinos (opción C).
        CREATE TABLE IF NOT EXISTS obs_rejects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            temp_f REAL,
            minute INTEGER,
            has_rawmsg INTEGER,
            dist_to_accepted_min REAL,
            ens_p10 REAL,
            ens_p90 REAL,
            logged_at TEXT NOT NULL,
            UNIQUE(station_id, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_rej_sid_ts
            ON obs_rejects(station_id, ts);

        -- F8 fase 0 (2026-07-10): snapshots de forecast de min diario.
        -- Zero UI, sólo captura. Backtest ~Ago cuando N≥30 días/estación.
        CREATE TABLE IF NOT EXISTS prediction_min_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            ts TEXT NOT NULL,
            p10 REAL,
            p50 REAL,
            p90 REAL,
            n_members INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_min_snap_sid_date
            ON prediction_min_snapshots(station_id, date);

        -- Señal externa observada por la mañana (primer auto-snapshot del día).
        -- Pareada con day_outcomes.max_obs_f permite backtestear umbrales de
        -- posterior_shift y del gate de bets sobre datos reales.
        CREATE TABLE IF NOT EXISTS daily_ext_signal (
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            ext_med REAL,
            ext_spread REAL,
            ext_diff_pre REAL,    -- pred_med - ext_med antes del shift
            clim_pct REAL,
            ext_shift_f REAL,     -- shift que aplicó el posterior (0 si no entró)
            pred_pre_shift REAL,  -- mediana del ensemble post-bias, pre-shift
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY (station_id, date)
        );
    """)
    # Migración no-destructiva: p_version en prediction_snapshots. Distingue
    # predicted_p pre-Laplace (kalshi.our_p_for_bin saturado en [0,1] hasta
    # 2026-07-01) de post-Laplace ([0.03, 0.97]). PAV mezcla sin romper pero
    # pairs saturados viejos dominan bloques extremos; filtrar cuando N lo
    # permita.
    ps_cols = {r[1] for r in c.execute("PRAGMA table_info(prediction_snapshots)").fetchall()}
    if "p_version" not in ps_cols:
        c.execute("ALTER TABLE prediction_snapshots ADD COLUMN p_version TEXT")

    # Migración no-destructiva: añade columnas a day_summary si faltan
    cols = {r[1] for r in c.execute("PRAGMA table_info(day_summary)").fetchall()}
    for col, ctype in [("ext_med", "REAL"), ("ext_spread", "REAL"),
                       ("ext_diff_pre", "REAL"), ("clim_pct", "REAL"),
                       ("ext_shift_f", "REAL")]:
        if col not in cols:
            c.execute(f"ALTER TABLE day_summary ADD COLUMN {col} {ctype}")
    # Telemetría del sign-nudge (Fable round 3): permite responder en 4 semanas
    # ΔMAE en días-nudge, tasa de flips falsos, y comportamiento acorde vs
    # contra-externos. Una sola fila por (station,date), columnas no joins.
    sig_cols = {r[1] for r in c.execute("PRAGMA table_info(daily_ext_signal)").fetchall()}
    for col, ctype in [("pred_pre_bias", "REAL"), ("sign_nudge_applied", "INTEGER"),
                       ("nudge_f", "REAL"), ("streak_len", "INTEGER"),
                       ("ewma_pre", "REAL"), ("bias_path", "TEXT")]:
        if col not in sig_cols:
            c.execute(f"ALTER TABLE daily_ext_signal ADD COLUMN {col} {ctype}")
    # F8 fase 0: min diario observado. NWS CLI ya lo trae en el mismo producto
    # que el max — persistirlo es gratis en red.
    do_cols = {r[1] for r in c.execute("PRAGMA table_info(day_outcomes)").fetchall()}
    if "min_obs_f" not in do_cols:
        c.execute("ALTER TABLE day_outcomes ADD COLUMN min_obs_f REAL")
    # Fable Round 2 (2026-07-10 tras snapshot 4h): la guarda de fase 2
    # compara vs interpolación lineal entre METARs flanqueantes (no vs
    # nearest accepted). Delta 5-min ≠ delta 90-min: rampa matinal KPHX
    # sube +4-6°F en 90 min, un X calibrado sobre 5-min rechazaría rampas
    # legítimas. Loggeamos vs-interpolada desde ahora — antes eran deltas
    # 5-min, distribución equivocada para fijar X.
    or_cols = {r[1] for r in c.execute("PRAGMA table_info(obs_rejects)").fetchall()}
    for col, ctype in [("interp_ref_f", "REAL"), ("delta_vs_interp_f", "REAL"),
                       ("interp_kind", "TEXT")]:
        if col not in or_cols:
            c.execute(f"ALTER TABLE obs_rejects ADD COLUMN {col} {ctype}")
    return c


def outcome_for(max_f: float, op: str, threshold: float,
                bin_half: float | None = None) -> bool:
    if op == ">":
        return max_f > threshold
    if op == ">=":
        return max_f >= threshold
    if op == "<":
        return max_f < threshold
    if op == "<=":
        return max_f <= threshold
    if op == "~":
        h = bin_half if bin_half is not None else 0.5
        return (threshold - h) <= max_f <= (threshold + h)
    raise ValueError(f"unknown op {op!r}")


def record(station_id: str, target_date: date, slot: int, assertion,
           predicted_p: float, snapshot_time: datetime | None = None) -> None:
    """Insert one snapshot of (assertion, predicted probability)."""
    ts = (snapshot_time or datetime.utcnow()).isoformat()
    c = _conn()
    c.execute("""INSERT INTO prediction_snapshots
        (station_id, date, snapshot_time, slot, is_auto, expr, op,
         threshold, bin_half, predicted_p, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (station_id, target_date.isoformat(), ts, slot,
         1 if assertion.auto else 0, assertion.expr, assertion.op,
         assertion.threshold,
         assertion.bin_half if assertion.op == "~" else None,
         float(predicted_p)))
    c.commit()
    c.close()


def record_obs_reject(station_id: str, ts: datetime, temp_f: float | None,
                      minute: int, has_rawmsg: bool,
                      dist_to_accepted_min: float | None,
                      ens_p10: float | None = None,
                      ens_p90: float | None = None,
                      interp_ref_f: float | None = None,
                      delta_vs_interp_f: float | None = None,
                      interp_kind: str | None = None) -> None:
    """Fable 2026-07-10 R1+R2: log lectura rechazada por el filtro.
    UNIQUE(station_id, ts) previene duplicados entre polls (INSERT OR IGNORE).

    interp_ref_f/delta_vs_interp_f (R2): cantidad correcta para elegir X°F
    de la guarda de fase 2 — interpolación lineal entre METARs flanqueantes
    dentro de ±60 min. interp_kind ∈ {linear, before, after, none} marca
    el caso de solo-un-flanker o sin data suficiente."""
    c = _conn()
    try:
        c.execute("""INSERT OR IGNORE INTO obs_rejects
                     (station_id, ts, temp_f, minute, has_rawmsg,
                      dist_to_accepted_min, ens_p10, ens_p90, logged_at,
                      interp_ref_f, delta_vs_interp_f, interp_kind)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (station_id, ts.isoformat(),
                   float(temp_f) if temp_f is not None else None,
                   int(minute), 1 if has_rawmsg else 0,
                   float(dist_to_accepted_min) if dist_to_accepted_min is not None else None,
                   float(ens_p10) if ens_p10 is not None else None,
                   float(ens_p90) if ens_p90 is not None else None,
                   datetime.utcnow().isoformat(),
                   float(interp_ref_f) if interp_ref_f is not None else None,
                   float(delta_vs_interp_f) if delta_vs_interp_f is not None else None,
                   interp_kind))
        c.commit()
    finally:
        c.close()


def record_min_snapshot(station_id: str, target_date: date,
                        p10: float | None, p50: float | None,
                        p90: float | None, n_members: int | None,
                        ts: datetime | None = None) -> None:
    """F8 fase 0: persistir un snapshot del forecast de min diario.

    Cero validación agresiva — si vienen todos None, insertamos igual como
    marca temporal (permite ver gaps de captura). Backtest a partir de N≥30
    filas/estación pareadas con day_outcomes.min_obs_f."""
    ts_s = (ts or datetime.utcnow()).isoformat()
    c = _conn()
    try:
        c.execute("""INSERT INTO prediction_min_snapshots
                     (station_id, date, ts, p10, p50, p90, n_members)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (station_id, target_date.isoformat(), ts_s,
                   float(p10) if p10 is not None else None,
                   float(p50) if p50 is not None else None,
                   float(p90) if p90 is not None else None,
                   int(n_members) if n_members is not None else None))
        c.commit()
    finally:
        c.close()


def _fetch_archive_max(station, target_date: date) -> float | None:
    """Fetch observed daily max from Open-Meteo archive for one date."""
    r = requests.get(ARCHIVE_URL, params={
        "latitude": station.lat,
        "longitude": station.lon,
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "daily": "temperature_2m_max",
        "timezone": station.tz.key,
        "temperature_unit": "fahrenheit",
    }, timeout=30, headers={"User-Agent": UA})
    r.raise_for_status()
    d = r.json().get("daily", {})
    vals = d.get("temperature_2m_max") or []
    return vals[0] if vals and vals[0] is not None else None


def settle_day(station, target_date: date,
               allow_archive_fallback: bool = False) -> float | None:
    """Fetch max for target_date, mark all snapshots for that (station, date).

    Sólo NWS CLI por defecto (mismo source que Kalshi liquida). Open-Meteo
    desviaba los settles del simulador respecto a Kalshi (sub-grado vs entero
    redondeado), así que el fallback de archive se quedó opt-in y reservado
    para retro-llenar series de calibración cuando estamos seguros que NWS
    nunca va a publicar (días viejos). Devuelve None si NWS no tiene final.
    """
    max_f, min_f = nws_cli.fetch_max_min_for(station.id, target_date)
    if max_f is None and allow_archive_fallback:
        max_f = _fetch_archive_max(station, target_date)
    if max_f is None:
        return None
    c = _conn()
    c.execute("""INSERT OR REPLACE INTO day_outcomes
                 (station_id, date, max_obs_f, settled_at, min_obs_f)
                 VALUES (?, ?, ?, ?, ?)""",
              (station.id, target_date.isoformat(), float(max_f),
               datetime.utcnow().isoformat(),
               float(min_f) if min_f is not None else None))
    # update outcomes on all snapshots for that day
    cur = c.execute("""SELECT id, op, threshold, bin_half
                       FROM prediction_snapshots
                       WHERE station_id=? AND date=? AND outcome IS NULL""",
                    (station.id, target_date.isoformat()))
    rows = cur.fetchall()
    for sid, op, thr, bh in rows:
        hit = outcome_for(max_f, op, thr, bh)
        c.execute("UPDATE prediction_snapshots SET outcome=? WHERE id=?",
                  (1 if hit else 0, sid))
    # Instrumentar bins de Kalshi como (predicted_p, outcome) pairs para
    # isotonic. Fuente unbiased (todos los bins del poller, no solo apostados).
    try:
        n_bins = _instrument_kalshi_bins(c, station.id, target_date,
                                         float(max_f))
    except Exception:
        n_bins = 0
    c.commit()
    c.close()
    try:
        compute_day_summary(station.id, target_date)
    except Exception:
        pass
    try:
        import bets as _bets
        _bets.settle_day(station.id, target_date, float(max_f))
    except Exception:
        pass
    # Refit isotonic cuando llegaron pairs nuevos — sin esto, el proceso 24/7
    # sigue con el calibrador cargado al arranque.
    if n_bins > 0:
        try:
            import isotonic
            isotonic.refit(station.id)
        except Exception:
            pass
    return max_f


def _bin_contains(max_f: float, lo: float, hi: float) -> bool:
    """Match kalshi.our_p_for_bin semantics (half-integer padding for
    open/closed edges)."""
    l = float("-inf") if lo == float("-inf") else lo - 0.5
    h = float("inf") if hi == float("inf") else hi + 0.5
    return l <= max_f < h


def _instrument_kalshi_bins(c, station_id: str, target_date: date,
                            max_f: float) -> int:
    """Espeja la LAST snapshot de kalshi_snapshots por (bin_lo, bin_hi) en
    prediction_snapshots como pares (predicted_p, outcome) para isotonic.

    Fuente unbiased: analysis_poller escribe TODOS los bins cada ciclo, no
    solo los apostados. Backfillear desde simulated_bets tendría sesgo de
    selección (solo bins donde el sistema decidió apostar con edge > mín).

    Escribe una row por bin con op='b' (bin-shaped), threshold estable
    (finite side de tail bins), p_version tag pre/post-Laplace. Idempotente
    en (station, date, op, threshold, snapshot_time).
    """
    if not ANALYSIS_DB_PATH.exists():
        return 0
    # Read from analysis.db in a separate connection para no atrapar la
    # transacción de calibration.db (ATTACH+DETACH pelea con writes pending
    # y da 'database is locked' cuando el mismo conn hace ambos).
    src = sqlite3.connect(ANALYSIS_DB_PATH)
    try:
        rows = src.execute("""
            SELECT k.bin_lo, k.bin_hi, k.our_p, k.ts
            FROM kalshi_snapshots k
            WHERE k.station=? AND DATE(k.ts)=?
              AND k.our_p IS NOT NULL
              AND k.ts = (
                SELECT MAX(k2.ts) FROM kalshi_snapshots k2
                WHERE k2.station=k.station
                  AND k2.bin_lo=k.bin_lo AND k2.bin_hi=k.bin_hi
                  AND DATE(k2.ts)=DATE(k.ts)
                  AND k2.our_p IS NOT NULL
              )
        """, (station_id, target_date.isoformat())).fetchall()
    finally:
        src.close()
    n = 0
    for lo, hi, our_p, ts in rows:
        lo_f = float(lo)
        hi_f = float(hi)
        outc = 1 if _bin_contains(max_f, lo_f, hi_f) else 0
        if lo_f == float("-inf"):
            threshold = hi_f
            bin_half = None
        elif hi_f == float("inf"):
            threshold = lo_f
            bin_half = None
        else:
            threshold = lo_f
            bin_half = (hi_f - lo_f) / 2.0
        p_version = ("post_laplace" if ts >= LAPLACE_CUTOFF_TS
                     else "pre_laplace")
        existing = c.execute(
            """SELECT id FROM prediction_snapshots
               WHERE station_id=? AND date=? AND op='b'
                 AND threshold=? AND snapshot_time=?""",
            (station_id, target_date.isoformat(), threshold, ts)
        ).fetchone()
        if existing:
            continue
        expr = f"kalshi_bin[{lo_f},{hi_f}]"
        c.execute(
            """INSERT INTO prediction_snapshots
               (station_id, date, snapshot_time, slot, is_auto, expr,
                op, threshold, bin_half, predicted_p, outcome, p_version)
               VALUES (?, ?, ?, 0, 1, ?, 'b', ?, ?, ?, ?, ?)""",
            (station_id, target_date.isoformat(), ts, expr,
             threshold, bin_half, float(our_p), outc, p_version))
        n += 1
    return n


def record_ext_signal(station_id: str, target_date: date,
                      ext_shift_info: dict | None,
                      pred_pre_shift: float | None,
                      bias_info: dict | None = None) -> None:
    """Upsert la señal externa de la mañana en daily_ext_signal.

    UPSERT con COALESCE: primer write del día gana para los campos "morning
    signal" (ext_med, ext_diff_pre, etc.), pero las columnas nuevas
    (pred_pre_bias, sign_nudge_applied, nudge_f, streak_len, ewma_pre,
    bias_path) se backfillean si quedaron NULL en una fila previa creada
    antes de añadir esas columnas al schema. Si ext_shift_info es None o
    no aporta ext_med, no escribimos nada.
    """
    if not ext_shift_info or ext_shift_info.get("ext_med") is None:
        return
    bi = bias_info or {}
    c = _conn()
    try:
        c.execute(
            """INSERT INTO daily_ext_signal
               (station_id, date, ext_med, ext_spread, ext_diff_pre,
                clim_pct, ext_shift_f, pred_pre_shift, first_seen_at,
                pred_pre_bias, sign_nudge_applied, nudge_f, streak_len,
                ewma_pre, bias_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(station_id, date) DO UPDATE SET
                   pred_pre_bias = COALESCE(daily_ext_signal.pred_pre_bias, excluded.pred_pre_bias),
                   sign_nudge_applied = COALESCE(daily_ext_signal.sign_nudge_applied, excluded.sign_nudge_applied),
                   nudge_f = COALESCE(daily_ext_signal.nudge_f, excluded.nudge_f),
                   streak_len = COALESCE(daily_ext_signal.streak_len, excluded.streak_len),
                   ewma_pre = COALESCE(daily_ext_signal.ewma_pre, excluded.ewma_pre),
                   bias_path = COALESCE(daily_ext_signal.bias_path, excluded.bias_path)""",
            (station_id, target_date.isoformat(),
             ext_shift_info.get("ext_med"),
             ext_shift_info.get("ext_spread"),
             ext_shift_info.get("ext_diff_pre"),
             ext_shift_info.get("clim_pct"),
             ext_shift_info.get("shift_f"),
             pred_pre_shift,
             datetime.utcnow().isoformat(),
             ext_shift_info.get("pred_pre_bias"),
             1 if bi.get("sign_nudge") else 0,
             bi.get("nudge_f"),
             bi.get("streak_len"),
             bi.get("ewma_pre"),
             bi.get("bias_path") or "none"))
        c.commit()
    finally:
        c.close()


def compute_day_summary(station_id: str, target_date: date) -> dict | None:
    """Build the per-day activity log row. Requires day_outcomes already
    populated (called right after settle_day). Joins kalshi.db if available.
    """
    ds = target_date.isoformat()
    c = _conn()
    row = c.execute(
        "SELECT max_obs_f FROM day_outcomes WHERE station_id=? AND date=?",
        (station_id, ds)).fetchone()
    if not row:
        c.close()
        return None
    actual_max = float(row[0])

    our_n = 0
    our_brier = None
    kalshi_n = 0
    kalshi_brier = None
    winning_label = None
    our_p_win = None
    kalshi_p_win = None
    best_edge_abs = None
    best_edge_label = None
    best_edge_our = None
    best_edge_kalshi = None
    best_edge_correct = None

    try:
        from kalshi import DB_PATH as KALSHI_DB
        if KALSHI_DB.exists():
            c.execute("ATTACH DATABASE ? AS k", (str(KALSHI_DB),))
            # COALESCE: our_p_final (post iso + blend, lo que el usuario ve)
            # cae a our_p (pre-blend) en rows anteriores a 2026-06-19.
            kcur = c.execute("""SELECT bin_lo, bin_hi, label,
                                       yes_mid,
                                       COALESCE(our_p_final, our_p) AS our_p
                                FROM k.market_prices
                                WHERE station_id=? AND date=?
                                  AND yes_mid IS NOT NULL
                                  AND COALESCE(our_p_final, our_p) IS NOT NULL""",
                             (station_id, ds))
            krows = kcur.fetchall()
            c.execute("DETACH DATABASE k")

            if krows:
                # Same per-bin methodology for both → Brier comparable.
                kalshi_n = len(krows)
                our_n = len(krows)
                sse_k = 0.0
                sse_o = 0.0
                win_ours = []
                win_kalshi = []
                for lo, hi, lbl, ym, op_ in krows:
                    outc = 1 if _bin_contains(actual_max, lo, hi) else 0
                    sse_k += (ym - outc) ** 2
                    sse_o += (op_ - outc) ** 2
                    if outc == 1:
                        win_ours.append(op_)
                        win_kalshi.append(ym)
                        if winning_label is None:
                            winning_label = lbl
                    edge = abs(op_ - ym)
                    if best_edge_abs is None or edge > best_edge_abs:
                        best_edge_abs = edge
                        best_edge_label = lbl
                        best_edge_our = op_
                        best_edge_kalshi = ym
                        if op_ > ym:
                            best_edge_correct = 1 if outc == 1 else 0
                        elif op_ < ym:
                            best_edge_correct = 1 if outc == 0 else 0
                        else:
                            best_edge_correct = None
                kalshi_brier = sse_k / kalshi_n
                our_brier = sse_o / our_n
                if win_ours:
                    our_p_win = sum(win_ours) / len(win_ours)
                    kalshi_p_win = sum(win_kalshi) / len(win_kalshi)
    except Exception:
        pass

    ext_row = c.execute(
        """SELECT ext_med, ext_spread, ext_diff_pre, clim_pct, ext_shift_f
           FROM daily_ext_signal WHERE station_id=? AND date=?""",
        (station_id, ds)).fetchone()
    ext_med = ext_row[0] if ext_row else None
    ext_spread = ext_row[1] if ext_row else None
    ext_diff_pre = ext_row[2] if ext_row else None
    clim_pct = ext_row[3] if ext_row else None
    ext_shift_f = ext_row[4] if ext_row else None

    c.execute("""INSERT OR REPLACE INTO day_summary
        (station_id, date, actual_max_f, our_n, our_brier,
         kalshi_n, kalshi_brier, winning_bin_label,
         our_p_winning, kalshi_p_winning,
         best_edge_abs, best_edge_bin_label,
         best_edge_our_p, best_edge_kalshi_p, best_edge_correct,
         computed_at,
         ext_med, ext_spread, ext_diff_pre, clim_pct, ext_shift_f)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (station_id, ds, actual_max, our_n, our_brier,
         kalshi_n or None, kalshi_brier, winning_label,
         our_p_win, kalshi_p_win,
         best_edge_abs, best_edge_label,
         best_edge_our, best_edge_kalshi, best_edge_correct,
         datetime.utcnow().isoformat(),
         ext_med, ext_spread, ext_diff_pre, clim_pct, ext_shift_f))
    c.commit()
    c.close()
    return {
        "station_id": station_id,
        "date": ds,
        "actual_max_f": actual_max,
        "our_n": our_n,
        "our_brier": our_brier,
        "kalshi_n": kalshi_n or None,
        "kalshi_brier": kalshi_brier,
        "winning_bin_label": winning_label,
        "our_p_winning": our_p_win,
        "kalshi_p_winning": kalshi_p_win,
        "best_edge_abs": best_edge_abs,
        "best_edge_bin_label": best_edge_label,
        "best_edge_our_p": best_edge_our,
        "best_edge_kalshi_p": best_edge_kalshi,
        "best_edge_correct": best_edge_correct,
    }


def list_summaries(station_id: str | None = None, limit: int = 60) -> list:
    c = _conn()
    if station_id:
        cur = c.execute("""SELECT * FROM day_summary
                           WHERE station_id=?
                           ORDER BY date DESC LIMIT ?""",
                        (station_id, limit))
    else:
        cur = c.execute("""SELECT * FROM day_summary
                           ORDER BY date DESC, station_id LIMIT ?""",
                        (limit,))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    c.close()
    return rows


def settle_pending(station, max_days_back: int = 14) -> list:
    """Settle any unsettled past dates for this station.

    Returns list of (date, max_f) for days settled this call.
    """
    c = _conn()
    # tz de la estación: PR-host adelantado vs US causaría settle prematuro.
    today = datetime.now(station.tz).date()
    # find dates with pending snapshots, excluding today
    cur = c.execute("""SELECT DISTINCT date FROM prediction_snapshots
                       WHERE station_id=? AND outcome IS NULL AND date<?
                       ORDER BY date""",
                    (station.id, today.isoformat()))
    dates = [row[0] for row in cur.fetchall()]
    c.close()
    cutoff = today - timedelta(days=max_days_back)
    settled = []
    for ds in dates:
        d = date.fromisoformat(ds)
        if d < cutoff:
            continue
        try:
            max_f = settle_day(station, d)
            if max_f is not None:
                settled.append((d, max_f))
        except requests.RequestException:
            # archive not ready or transient error — try next time
            continue
    return settled


def reliability(station_id: str | None = None,
                n_buckets: int = 10) -> ReliabilityReport:
    """Bucket settled snapshots by predicted_p and compute hit rate per bucket."""
    c = _conn()
    if station_id:
        cur = c.execute("""SELECT predicted_p, outcome FROM prediction_snapshots
                           WHERE station_id=? AND outcome IS NOT NULL""",
                        (station_id,))
    else:
        cur = c.execute("""SELECT predicted_p, outcome FROM prediction_snapshots
                           WHERE outcome IS NOT NULL""")
    rows = cur.fetchall()
    if station_id:
        total = c.execute("""SELECT COUNT(*) FROM prediction_snapshots
                             WHERE station_id=?""", (station_id,)).fetchone()[0]
    else:
        total = c.execute("SELECT COUNT(*) FROM prediction_snapshots").fetchone()[0]
    c.close()

    buckets = []
    width = 1.0 / n_buckets
    for i in range(n_buckets):
        low = i * width
        high = low + width
        in_bucket = [(p, o) for p, o in rows
                     if (low <= p < high) or (i == n_buckets - 1 and p == 1.0)]
        if not in_bucket:
            buckets.append(ReliabilityBucket(low, high, 0, 0.0, 0.0))
            continue
        n = len(in_bucket)
        mean_pred = sum(p for p, _ in in_bucket) / n
        hit_rate = sum(o for _, o in in_bucket) / n
        buckets.append(ReliabilityBucket(low, high, n, mean_pred, hit_rate))

    if rows:
        brier = sum((p - o) ** 2 for p, o in rows) / len(rows)
    else:
        brier = None

    return ReliabilityReport(
        buckets=buckets,
        total_n=total,
        settled_n=len(rows),
        brier=brier,
        station_id=station_id,
    )
