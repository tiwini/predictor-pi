"""Per-station rolling bias tracker.

Compares the early-morning auto snapshot of each settled day against the
actual observed max, computes an exponentially-weighted average of the
last N_DAYS of errors, and returns a correction to subtract from the
prior. Applied only when |bias| ≥ APPLY_THRESHOLD AND ≥ MIN_DAYS of data.

Why this exists: empirically (see /reweight/bias panel) some stations have
strong consistent biases that the morning ensemble can't see — KBOS runs
+2°F warm, KPHX runs −2°F cold. The Bayesian reweight corrects by
end-of-day once obs accumulate, but morning predictions stay biased.
This tracker shifts the prior so morning predictions start closer to truth.
"""
from __future__ import annotations

import sqlite3
from datetime import date as _date
from pathlib import Path
from typing import Optional

DEFAULT_DB = Path(__file__).parent / "calibration.db"

N_DAYS = 7
ALPHA = 0.4              # weight on most recent day; older = (1-α)^k * α
MIN_DAYS = 3             # need ≥ this many settled days with early prediction
APPLY_THRESHOLD = 0.7    # only apply correction if |bias| ≥ this (skip noise)

# Regime-shift detector: si los últimos REGIME_K días tienen residuos del mismo
# signo y |media| ≥ REGIME_MIN_ABS, restringimos el EWMA a esos K para no dilatar
# con días templados anteriores. Captura olas de calor / frentes que el ensemble
# subestima persistentemente.
REGIME_K = 4
REGIME_MIN_ABS = 1.5
REGIME_SHRINK = 0.5      # autocorr lag-1 del error diario ≈ −0.25 pooled:
                         # corregir 1:1 sobreajusta. Shrink baja el riesgo
                         # de fabricar errores opuestos al día siguiente.
REGIME_CAP_F = 2.5       # tope absoluto de la corrección histórica

# Sign-nudge: cuando la magnitud rebota pero el signo persiste, el EWMA
# se queda por debajo de APPLY_THRESHOLD y no se corrige nada — aunque
# sabemos hacia dónde va el error. KPHX (round 2 Fable): P(mismo signo)=1.00
# sobre 8 transiciones consecutivas con r de magnitud −0.66; E[err|día frío]
# = −1.2°F. Si los últimos SIGN_NUDGE_K errores son del mismo signo y el
# EWMA no aplica, empujamos ±SIGN_NUDGE_F (constante, no proporcional al
# error de ayer — la magnitud no se hereda; el signo sí).
SIGN_NUDGE_K = 3
SIGN_NUDGE_F = 1.0
# Atenuación por externos (Fable round 3): el nudge es información de ayer,
# los externos son información de hoy — cuando chocan, gana hoy.
#   |ext_diff| < 0.5°F  → externos ≈ pred, nudge completo
#   ext_diff y nudge concuerdan (mueven hacia ext_med) → completo
#   |ext_diff| ∈ [0.5, 1.5) y nudge se aleja de ext_med → medio nudge
#   |ext_diff| ≥ 1.5°F y nudge se aleja de ext_med → veto (shift estaría
#                                                    disparando contrario)
NUDGE_ATTEN_NEAR = 0.5
NUDGE_ATTEN_FAR = 1.5
NUDGE_ATTEN_HALF = 0.5

# Conditional bias by climatology percentile. KLGA is bimodal: cold on warm
# days and warm on cold days. Splitting samples by climatology percentile
# of the predicted max lets us correct each regime separately, but only if
# we have enough data in the matching bucket.
CONDITIONAL_MIN_DAYS = 3
COND_BUCKET_EDGE = 50.0  # below = "cold regime", at/above = "warm regime"


def compute_bias(station_id: str, today: Optional[_date] = None,
                 db_path: Path = DEFAULT_DB,
                 ext_diff: Optional[float] = None) -> dict:
    """Return rolling bias info for `station_id`.

    Bias convention: positive = we predict too high (warm bias), so the
    correction to apply is `-bias` (subtract from ensemble distribution).

    Returns a dict with:
      bias        : weighted-mean error in °F (early_pred - actual)
      n           : number of (date, error) pairs used
      applied     : True iff bias should be applied to the prior
      reason      : "" if applied, else why not
      samples     : list of (date_str, err_f) for visibility
    """
    today = today or _date.today()
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT o.date, o.max_obs_f,
                   (SELECT threshold FROM prediction_snapshots p
                    WHERE p.station_id = o.station_id
                      AND p.date = o.date
                      AND p.is_auto = 1
                      AND p.snapshot_time > o.date || 'T08:00'
                    ORDER BY p.snapshot_time ASC LIMIT 1) AS early_pred
            FROM day_outcomes o
            WHERE o.station_id = ? AND o.date < ?
            ORDER BY o.date DESC LIMIT ?
            """,
            (station_id, today.isoformat(), N_DAYS),
        )
        rows = [
            (d, float(actual), float(pred))
            for d, actual, pred in cur.fetchall()
            if pred is not None
        ]
    finally:
        con.close()

    samples = [(d, pred - actual) for d, actual, pred in rows]

    if len(rows) < MIN_DAYS:
        return {
            "bias": 0.0, "n": len(rows), "applied": False,
            "reason": f"insufficient data ({len(rows)}/{MIN_DAYS} días)",
            "samples": samples,
            "regime_break": False,
            "sign_nudge": False, "nudge_f": 0.0, "streak_len": 0,
            "ewma_pre": 0.0, "bias_path": "none",
        }

    regime = _detect_regime_break(rows)
    active = rows[:REGIME_K] if regime else rows
    if regime:
        bias = _extreme_bias(active)
        bias_path = "regime"
    else:
        bias = _weighted_bias(active)
        bias_path = "ewma"
    applied = abs(bias) >= APPLY_THRESHOLD
    sign_nudge = False
    nudge_f = 0.0
    streak_len = 0
    ewma_pre = bias if bias_path == "ewma" else _weighted_bias(rows)
    if not applied:
        nudge_f, streak_len = _sign_nudge(rows, ext_diff=ext_diff)
        if nudge_f != 0.0:
            bias = max(-REGIME_CAP_F, min(REGIME_CAP_F, nudge_f))
            applied = True
            sign_nudge = True
            bias_path = "nudge"
        else:
            bias_path = "none"
    reason = ""
    if not applied:
        reason = f"|bias|={abs(bias):.2f}°F < umbral {APPLY_THRESHOLD}°F"
    return {
        "bias": bias,
        "n": len(active),
        "applied": applied,
        "reason": reason,
        "samples": samples,
        "regime_break": regime,
        "sign_nudge": sign_nudge,
        "nudge_f": nudge_f,
        "streak_len": streak_len,
        "ewma_pre": ewma_pre,
        "bias_path": bias_path,
    }


def _weighted_bias(rows: list) -> float:
    """rows: [(date, actual, pred), ...] in order most-recent-first.
    Returns exponentially weighted mean of (pred - actual)."""
    if not rows:
        return 0.0
    weights = [(1 - ALPHA) ** k * ALPHA for k in range(len(rows))]
    errs = [pred - actual for _, actual, pred in rows]
    return sum(w * e for w, e in zip(weights, errs)) / sum(weights)


def _extreme_bias(rows: list) -> float:
    """Bias de régimen: media del bloque, encogida y con tope.

    Antes usábamos el peor caso (max |err|), pero la autocorrelación lag-1
    del error diario es ≈ −0.25 pooled (Fable round 2): el error de ayer
    NO se repite entero hoy. Media × SHRINK con cap es estrictamente mejor
    en KPHX (-0.44 MAE) y KLGA (-0.12), y nunca empeora en la muestra."""
    if not rows:
        return 0.0
    errs = [pred - actual for _, actual, pred in rows]
    b = REGIME_SHRINK * (sum(errs) / len(errs))
    return max(-REGIME_CAP_F, min(REGIME_CAP_F, b))


def _sign_nudge(rows: list, ext_diff: Optional[float] = None) -> tuple[float, int]:
    """Devuelve (nudge_f, streak_len). nudge_f es ±SIGN_NUDGE_F si los últimos
    SIGN_NUDGE_K errores comparten signo, atenuado por ext_diff. rows en orden
    most-recent-first.

    Convención: nudge positivo = predecimos alto, el caller lo resta del prior.
    'Mueve hacia ext_med' = sign(nudge) == sign(ext_diff) (mismo signo).
    """
    if len(rows) < SIGN_NUDGE_K:
        return 0.0, 0
    recent = rows[:SIGN_NUDGE_K]
    errs = [pred - actual for _, actual, pred in recent]
    if all(e > 0 for e in errs):
        raw = SIGN_NUDGE_F
    elif all(e < 0 for e in errs):
        raw = -SIGN_NUDGE_F
    else:
        return 0.0, 0

    streak = SIGN_NUDGE_K
    for d, actual, pred in rows[SIGN_NUDGE_K:]:
        e = pred - actual
        if (e > 0) == (raw > 0) and e != 0:
            streak += 1
        else:
            break

    if ext_diff is None or abs(ext_diff) < NUDGE_ATTEN_NEAR:
        return raw, streak
    toward_ext = (ext_diff > 0) == (raw > 0)  # both signs match → mueve a ext_med
    if toward_ext:
        return raw, streak
    # Smoothstep invertido entre NEAR (atten=1) y FAR (atten=0). Sin saltos:
    # 0.5°F→1.0×, 1.0°F→0.5×, 1.5°F→0×.
    x = max(0.0, min(1.0, (abs(ext_diff) - NUDGE_ATTEN_NEAR)
                          / (NUDGE_ATTEN_FAR - NUDGE_ATTEN_NEAR)))
    atten = 1.0 - (3.0 * x * x - 2.0 * x * x * x)
    return raw * atten, streak


def _detect_regime_break(rows: list) -> bool:
    """True si los últimos REGIME_K días tienen residuos del mismo signo y
    |media| ≥ REGIME_MIN_ABS. rows en orden most-recent-first."""
    if len(rows) < REGIME_K:
        return False
    recent = rows[:REGIME_K]
    errs = [pred - actual for _, actual, pred in recent]
    same_sign = all(e > 0 for e in errs) or all(e < 0 for e in errs)
    return same_sign and abs(sum(errs) / REGIME_K) >= REGIME_MIN_ABS


def compute_bias_conditional(station_id: str, predicted_max_f: float,
                             today_percentile: Optional[float],
                             percentile_for_pred,
                             today: Optional[_date] = None,
                             db_path: Path = DEFAULT_DB,
                             ext_diff: Optional[float] = None) -> dict:
    """Bias condicional por régimen climatológico.

    Args:
      predicted_max_f      : prediction for today (used to pick a bucket)
      today_percentile     : climatology percentile of predicted_max_f
      percentile_for_pred  : callable(date_str, pred_f) -> percentile|None
                             (lookup historical pred's clim pct on that date)
      today, db_path       : as in compute_bias

    Falls back to global compute_bias when:
      - today_percentile is None
      - matching bucket has < CONDITIONAL_MIN_DAYS samples
    """
    base = compute_bias(station_id, today=today, db_path=db_path,
                        ext_diff=ext_diff)
    if today_percentile is None or not base.get("samples"):
        base["mode"] = "global"
        base["reason"] = base.get("reason") or ""
        return base

    today = today or _date.today()
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT o.date, o.max_obs_f,
                   (SELECT threshold FROM prediction_snapshots p
                    WHERE p.station_id = o.station_id
                      AND p.date = o.date
                      AND p.is_auto = 1
                      AND p.snapshot_time > o.date || 'T08:00'
                    ORDER BY p.snapshot_time ASC LIMIT 1) AS early_pred
            FROM day_outcomes o
            WHERE o.station_id = ? AND o.date < ?
            ORDER BY o.date DESC LIMIT ?
            """,
            (station_id, today.isoformat(), N_DAYS),
        )
        all_rows = [
            (d, float(actual), float(pred))
            for d, actual, pred in cur.fetchall()
            if pred is not None
        ]
    finally:
        con.close()

    today_warm = today_percentile >= COND_BUCKET_EDGE
    bucket = []
    for d, actual, pred in all_rows:
        try:
            pct = percentile_for_pred(d, pred)
        except Exception:
            pct = None
        if pct is None:
            continue
        if (pct >= COND_BUCKET_EDGE) == today_warm:
            bucket.append((d, actual, pred))

    if len(bucket) < CONDITIONAL_MIN_DAYS:
        base["mode"] = "global"
        base["reason"] = (base.get("reason") or "") + (
            f" · régimen {'cálido' if today_warm else 'frío'} con "
            f"{len(bucket)}/{CONDITIONAL_MIN_DAYS} días — usando global"
        ).strip(" ·")
        return base

    regime_break = _detect_regime_break(bucket)
    active = bucket[:REGIME_K] if regime_break else bucket
    if regime_break:
        bias = _extreme_bias(active)
        bias_path = "regime"
    else:
        bias = _weighted_bias(active)
        bias_path = "ewma"
    applied = abs(bias) >= APPLY_THRESHOLD
    sign_nudge = False
    nudge_f = 0.0
    streak_len = 0
    ewma_pre = bias if bias_path == "ewma" else _weighted_bias(bucket)
    if not applied:
        nudge_f, streak_len = _sign_nudge(bucket, ext_diff=ext_diff)
        if nudge_f != 0.0:
            bias = max(-REGIME_CAP_F, min(REGIME_CAP_F, nudge_f))
            applied = True
            sign_nudge = True
            bias_path = "nudge"
        else:
            bias_path = "none"
    return {
        "bias": bias,
        "n": len(active),
        "applied": applied,
        "mode": "conditional",
        "regime": "cálido" if today_warm else "frío",
        "regime_break": regime_break,
        "sign_nudge": sign_nudge,
        "nudge_f": nudge_f,
        "streak_len": streak_len,
        "ewma_pre": ewma_pre,
        "bias_path": bias_path,
        "today_percentile": today_percentile,
        "reason": "" if applied else f"|bias|={abs(bias):.2f}°F < umbral {APPLY_THRESHOLD}°F (régimen)",
        "samples": [(d, pred - actual) for d, actual, pred in bucket],
        "global_bias": base.get("bias", 0.0),
        "global_n": base.get("n", 0),
    }
