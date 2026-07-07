"""Signals + per-bin gates compartidos por lectura.py, analysis_poller.py y agent_monitor.py.

Single source of truth para:
  - thresholds de bloqueo (bias / ext_diff / difficulty / streak)
  - clasificacion direccional (cold / hot / mid) de un bin
  - lookups de calibration.db (ROI historico, streak por direccion)
  - evaluacion per-bin: recommended_side, edge_pp, blocked_reasons, actionable

Diseno (Codex Round 5 2026-06-29):
  - Sin dependencia de predictor_web (no _anchor_context, no state global).
  - Sin fetches de red (externos los pasan los callers).
  - bias_tracker.compute_bias() si que se importa (es pure y lee calibration.db).
  - Los privates de bets.py (_cold_bias_blocks_yes, _streak_blocks) se replican
    aqui en forma publica; bets.py podra migrar a importar de aca en un paso
    posterior para eliminar la duplicacion.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# === Thresholds — centralizado. Lectura, poller y agent importan de aca. ===

BIAS_BLOCK_THRESHOLD = 0.7
EXT_DIFF_BLOCK_THRESHOLD = 1.5
DIFFICULTY_BLOCK_THRESHOLD = 999.0  # DESACTIVADO 2026-07-06 (Fable retro: rho=0.004). Ver bets.DIFFICULTY_BLOCK_THR.

STREAK_BLOCK_AT = 3
STREAK_LOOKBACK_DAYS = 30

COLD_BIAS_BLOCK_F = -0.7
COLD_STREAK_BLOCK_N = 3

STATION_DEBUFF: dict[str, float] = {"KPHX": -4.0}
EDGE_MIN_BY_STATION: dict[str, float] = {"KPHX": 30.0}
EDGE_MIN_DEFAULT = 10.0


# === Connection helper con busy_timeout (race con poller writes) ===

def _conn(db_path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
    c.execute("PRAGMA busy_timeout=5000")
    return c


# === Clasificacion direccional ===

def direction_of(side: str, bin_lo: float, bin_hi: float,
                 our_pred_f: Optional[float] = None) -> str:
    """Clasifica un bet como 'cold' / 'hot' / 'mid'.

    Espejo exacto de bets._direction() — replicado aca para que el modulo sea
    standalone. Si bets.py se migra a importar esta funcion, eliminar la copia.
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


# === Bias guard direccional ===

def bias_blocks_direction(bias_f: Optional[float],
                          ext_diff_f: Optional[float],
                          direction: str) -> tuple[bool, str]:
    """True si bias o ext_diff bloquean apostar en esta direccion.

    Espejo de lectura.bias_blocks_bet(). bias_f convencion: positivo = modelo
    predice mas alto que real (warm bias); negativo = cold bias.
      - direction=cold: bloquea si modelo ya esta frio (no apostar mas frio).
      - direction=hot:  bloquea si modelo ya esta caliente.
      - direction=mid:  no bloquea.
    """
    if direction == "cold":
        bias_bad = bias_f is not None and bias_f <= -BIAS_BLOCK_THRESHOLD
        ext_bad = ext_diff_f is not None and ext_diff_f <= -EXT_DIFF_BLOCK_THRESHOLD
        if bias_bad or ext_bad:
            parts = []
            if bias_f is not None:
                parts.append(f"bias {bias_f:+.2f}")
            if ext_diff_f is not None:
                parts.append(f"ext {ext_diff_f:+.1f}")
            return True, f"cold-side con modelo ya frio ({', '.join(parts)})"
    elif direction == "hot":
        bias_bad = bias_f is not None and bias_f >= BIAS_BLOCK_THRESHOLD
        ext_bad = ext_diff_f is not None and ext_diff_f >= EXT_DIFF_BLOCK_THRESHOLD
        if bias_bad or ext_bad:
            parts = []
            if bias_f is not None:
                parts.append(f"bias {bias_f:+.2f}")
            if ext_diff_f is not None:
                parts.append(f"ext {ext_diff_f:+.1f}")
            return True, f"hot-side con modelo ya caliente ({', '.join(parts)})"
    return False, ""


# === Cold-bias guard (Codex Round 4 — bets.py:_cold_bias_blocks_yes) ===

def cold_bias_blocks_yes(bias_info: Optional[dict]) -> bool:
    """True si el cold-bias guard se dispara para apuestas YES cold/mid.

    Acepta el dict que devuelve bias_tracker.compute_bias() — usa keys
    'bias', 'sign_nudge', 'streak_len'.
    """
    if not bias_info:
        return False
    bias = bias_info.get("bias") or 0.0
    if bias <= COLD_BIAS_BLOCK_F:
        return True
    if (bias_info.get("sign_nudge")
            and bias < 0
            and (bias_info.get("streak_len") or 0) >= COLD_STREAK_BLOCK_N):
        return True
    return False


# === Streak por direccion (calibration.db.simulated_bets) ===

def streak_blocks(station_id: str, direction: str,
                  calibration_db_path: str | Path,
                  our_pred_f: Optional[float] = None,
                  lookback_days: int = STREAK_LOOKBACK_DAYS) -> int:
    """Devuelve n_losses de la racha actual si >= STREAK_BLOCK_AT, sino 0.

    Espejo de bets._streak_blocks. our_pred_f permite clasificar bins medios
    direccionalmente en el lookback historico.
    """
    if direction == "mid":
        return 0
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    c = _conn(calibration_db_path)
    try:
        # Excluir shadow bets (blocked_by NOT NULL): la racha refleja
        # pérdidas reales, no bets que otro guard ya frenó.
        rows = c.execute(
            "SELECT side, bin_lo, bin_hi, won FROM simulated_bets "
            "WHERE station_id=? AND won IS NOT NULL AND date >= ? "
            "  AND blocked_by IS NULL "
            "ORDER BY settled_at DESC",
            (station_id, cutoff),
        ).fetchall()
    finally:
        c.close()
    streak = 0
    for side, lo, hi, won in rows:
        if direction_of(side, lo, hi, our_pred_f) != direction:
            continue
        if won == 0:
            streak += 1
        else:
            break
    return streak if streak >= STREAK_BLOCK_AT else 0


def streaks_by_direction(station_id: str,
                         calibration_db_path: str | Path,
                         our_pred_f: Optional[float] = None,
                         lookback_days: int = STREAK_LOOKBACK_DAYS) -> dict:
    """Convenience: corre streak_blocks() para 'hot' y 'cold' en una sola pasada
    (evita query duplicada). Devuelve {'hot': n, 'cold': n}; 0 si no bloqueado.
    """
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    c = _conn(calibration_db_path)
    try:
        rows = c.execute(
            "SELECT side, bin_lo, bin_hi, won FROM simulated_bets "
            "WHERE station_id=? AND won IS NOT NULL AND date >= ? "
            "  AND blocked_by IS NULL "
            "ORDER BY settled_at DESC",
            (station_id, cutoff),
        ).fetchall()
    finally:
        c.close()
    out = {"hot": 0, "cold": 0}
    streaks_open = {"hot": 0, "cold": 0}
    closed = {"hot": False, "cold": False}
    for side, lo, hi, won in rows:
        d = direction_of(side, lo, hi, our_pred_f)
        if d not in ("hot", "cold") or closed[d]:
            continue
        if won == 0:
            streaks_open[d] += 1
        else:
            closed[d] = True
    for d in ("hot", "cold"):
        if streaks_open[d] >= STREAK_BLOCK_AT:
            out[d] = streaks_open[d]
    return out


# === ROI historico per-station ===

def historical_roi(station_id: str,
                   calibration_db_path: str | Path,
                   lookback_days: Optional[int] = None,
                   include_shadow: bool = False) -> dict:
    """Aggregate sobre simulated_bets settled. Devuelve {pl, trades, wins,
    roi_pct} + `by_direction`: {cold, hot, mid} cada uno con las mismas keys.

    `by_direction` requiere columna `direction` (persistida al insertar
    bet desde 2026-07-01). Rows sin `direction` NULL contribuyen al
    agregado pero no al split — se reportan bajo key `unknown`.

    Por defecto excluye shadow bets (blocked_by NOT NULL) — el P&L "real"
    es lo que se ejecutó tras pasar todos los guards. include_shadow=True
    para debug/backtest de umbrales. La EV honesta de cada guard vive en
    guard_ev().

    lookback_days=None usa toda la historia. None-safe: devuelve trades=0 si vacio.
    """
    c = _conn(calibration_db_path)
    try:
        args: tuple
        where = "station_id=? AND won IS NOT NULL"
        if not include_shadow:
            where += " AND blocked_by IS NULL"
        if lookback_days is not None:
            cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            where += " AND date >= ?"
            args = (station_id, cutoff)
        else:
            args = (station_id,)
        row = c.execute(
            "SELECT COALESCE(SUM(pnl),0), COUNT(*), "
            "       COALESCE(SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),0), "
            "       COALESCE(SUM(stake),0) "
            f"FROM simulated_bets WHERE {where}",
            args,
        ).fetchone()
        rows = c.execute(
            "SELECT COALESCE(direction,'unknown'), "
            "       COALESCE(SUM(pnl),0), COUNT(*), "
            "       COALESCE(SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),0), "
            "       COALESCE(SUM(stake),0) "
            f"FROM simulated_bets WHERE {where} "
            "GROUP BY COALESCE(direction,'unknown')",
            args,
        ).fetchall()
    finally:
        c.close()
    pl, trades, wins, stake_total = row
    roi_pct = (100.0 * pl / stake_total) if stake_total > 0 else 0.0
    by_direction: dict = {}
    for d, d_pl, d_trades, d_wins, d_stake in rows:
        d_roi = (100.0 * d_pl / d_stake) if d_stake > 0 else 0.0
        by_direction[d] = {"pl": d_pl, "trades": d_trades,
                           "wins": d_wins, "roi_pct": d_roi}
    return {"pl": pl, "trades": trades, "wins": wins, "roi_pct": roi_pct,
            "by_direction": by_direction}


# === Guard EV (shadow bets aggregate, fable 2026-07-03) ===

def _ex_ante_labels(blocked_by: str) -> list[str]:
    """Devuelve guard labels ex-ante (first token pre ':') excluyendo tokens
    retroactivos.

    Retroactive tags (`streak:retroactive`, `overnight:retroactive`) se
    excluyen del EV porque son sesgo de selección puro: se etiquetan tras
    saber que perdieron. Un guard evaluado contra bets marcadas ex-post luce
    siempre justificado.

    fable 2026-07-03: retroactive sirve para narrativa del día, NO para EV.
    """
    labels: list[str] = []
    seen: set[str] = set()
    for tok in blocked_by.split(","):
        tok = tok.strip()
        if not tok or ":retroactive" in tok:
            continue
        label = tok.split(":", 1)[0]
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def guard_ev(station_id: str,
             calibration_db_path: str | Path,
             lookback_days: Optional[int] = None) -> dict:
    """ROI que TENDRÍA cada guard si no operara — mide la EV honesta de cada
    guard bloqueando bets. Agrega shadow bets (`blocked_by IS NOT NULL`)
    settled, split-eadas en `sole` (guard fue único bloqueador ex-ante) y
    `shared` (co-firmaron ≥2 guards).

    **Decisión de relajar sólo mira `sole`** (fable 2026-07-03): si relajas
    un guard cuyo shadow tiene múltiples guards, la bet sigue bloqueada por
    los otros — el ROI atribuido incluye bets que relajarlo no liberaría.

    Retroactive tags se excluyen ANTES del split — son ex-post, sesgados por
    outcome. Ver `_ex_ante_labels`.

    Regla de acción asimétrica (fable):
      - Apretar/mantener: N_sole ≥ 20 (misma que backtest_before_tuning).
      - Relajar: N_sole ≥ 40 + ROI positivo + sobrevive trim de las 2 mejores
        bets (protege contra longshots que dominan el sample).
      - Una relajación a la vez, con ventana de re-observación antes de tocar
        la siguiente.

    Returns {guard: {sole: {pl,trades,wins,stake,roi_pct}, shared: {...}}}.
    """
    where = ("station_id=? AND won IS NOT NULL "
             "AND blocked_by IS NOT NULL AND blocked_by <> ''")
    args: tuple
    if lookback_days is not None:
        cutoff = (datetime.utcnow()
                  - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        where += " AND date >= ?"
        args = (station_id, cutoff)
    else:
        args = (station_id,)
    c = _conn(calibration_db_path)
    try:
        rows = c.execute(
            f"SELECT blocked_by, pnl, won, stake FROM simulated_bets "
            f"WHERE {where}",
            args,
        ).fetchall()
    finally:
        c.close()

    def _empty() -> dict:
        return {"pl": 0.0, "trades": 0, "wins": 0, "stake": 0.0,
                "pnl_samples": []}

    agg: dict = {}
    for blocked_by, pnl, won, stake in rows:
        labels = _ex_ante_labels(blocked_by or "")
        if not labels:
            continue
        bucket = "sole" if len(labels) == 1 else "shared"
        pnl_f = float(pnl or 0.0)
        for label in labels:
            slot = agg.setdefault(label, {"sole": _empty(), "shared": _empty()})
            slot[bucket]["pl"] += pnl_f
            slot[bucket]["trades"] += 1
            slot[bucket]["wins"] += 1 if won == 1 else 0
            slot[bucket]["stake"] += float(stake or 0.0)
            slot[bucket]["pnl_samples"].append(pnl_f)
    for slot in agg.values():
        for b in ("sole", "shared"):
            s = slot[b]
            s["roi_pct"] = (100.0 * s["pl"] / s["stake"]) if s["stake"] > 0 else 0.0
    return agg


def guard_relax_candidate(guard_slot: dict,
                          min_n: int = 40,
                          trim_top: int = 2,
                          pnl_samples: Optional[list[float]] = None) -> dict:
    """Aplica la regla de fable para decidir si un guard es candidato a
    relajarse. Recibe el slot devuelto por guard_ev()[label]; el bucket
    `sole` ya trae `pnl_samples` embebido por default (fable 2026-07-03: sin
    forzar dos rutas API que podrían quedar desincronizadas). El param
    `pnl_samples` sigue disponible por override en tests.

    Returns {"candidate": bool, "reason": str}.
    """
    sole = guard_slot.get("sole", {})
    n = sole.get("trades", 0)
    roi = sole.get("roi_pct", 0.0)
    if pnl_samples is None:
        pnl_samples = sole.get("pnl_samples")
    if n < min_n:
        return {"candidate": False,
                "reason": f"N_sole={n} < {min_n} (relajar exige más N que apretar)"}
    if roi <= 0:
        return {"candidate": False,
                "reason": f"ROI_sole={roi:.1f}% ≤0 (guard cubre coste)"}
    if pnl_samples is None:
        return {"candidate": True,
                "reason": f"N={n} ROI={roi:.1f}% — falta trim check (pasar pnl_samples)"}
    if len(pnl_samples) != n:
        return {"candidate": False,
                "reason": f"pnl_samples len {len(pnl_samples)} ≠ N_sole {n}"}
    trimmed = sorted(pnl_samples)[:-trim_top] if len(pnl_samples) > trim_top else []
    stake_est = sole.get("stake", 0.0) * (len(trimmed) / n) if n > 0 else 0.0
    trim_pl = sum(trimmed)
    trim_roi = (100.0 * trim_pl / stake_est) if stake_est > 0 else 0.0
    if trim_roi <= 0:
        return {"candidate": False,
                "reason": f"trim-{trim_top} ROI={trim_roi:.1f}% ≤0 — top bets dominaban"}
    return {"candidate": True,
            "reason": f"N={n} ROI={roi:.1f}% trim-{trim_top} ROI={trim_roi:.1f}%"}


# === Per-bin evaluator (el core: blocked_reasons + actionable) ===

def evaluate_bin(*,
                 station_id: str,
                 bin_lo: float,
                 bin_hi: float,
                 bin_label: str,
                 kalshi_yes_price: Optional[float],
                 model_p_calibrated: Optional[float],
                 model_p_raw: Optional[float] = None,
                 pred_calibrated_f: Optional[float] = None,
                 bias_info: Optional[dict] = None,
                 ext_diff_f: Optional[float] = None,
                 difficulty_score: Optional[float] = None,
                 streak_hot_n: int = 0,
                 streak_cold_n: int = 0,
                 cold_bias_block: Optional[bool] = None) -> dict:
    """Per-bin decision: recommended_side, edge, blocked_reasons, actionable.

    Politicas:
      - Prefiere model_p_calibrated; cae a raw con prob_source='raw_fallback'.
      - Recommended side por signo del edge sobre el precio Kalshi.
      - Direccion clasificada con pred_calibrated_f (mid bins) si esta disponible.
      - actionable=False si HAY blocked_reasons o difficulty supera el umbral.
      - min_edge_required_pp aplica el debuff per-station (KPHX exige 30pp).

    Si cold_bias_block is None, se deriva de bias_info via cold_bias_blocks_yes().
    """
    blocked: list[str] = []

    p = model_p_calibrated
    prob_source = "calibrated"
    if p is None:
        p = model_p_raw
        prob_source = "raw_fallback"
        if p is not None:
            blocked.append("our_p raw (sin calibrado) — confianza reducida")

    if p is None or kalshi_yes_price is None:
        return {
            "recommended_side": None,
            "edge_pp": None,
            "prob_source": prob_source,
            "direction": None,
            "blocked_reasons": blocked + ["faltan inputs (model_p o kalshi)"],
            "actionable": False,
            "min_edge_required_pp": EDGE_MIN_BY_STATION.get(station_id, EDGE_MIN_DEFAULT),
        }

    edge_signed = (p - kalshi_yes_price) * 100.0
    side = "YES" if edge_signed >= 0 else "NO"
    edge_pp = abs(edge_signed)

    direction = direction_of(side, bin_lo, bin_hi, pred_calibrated_f)

    if difficulty_score is not None and difficulty_score > DIFFICULTY_BLOCK_THRESHOLD:
        blocked.append(f"difficulty {difficulty_score:.0f} > {DIFFICULTY_BLOCK_THRESHOLD:.0f}")

    bias_bad, bias_reason = bias_blocks_direction(
        bias_info.get("bias") if bias_info else None,
        ext_diff_f, direction)
    if bias_bad:
        blocked.append(bias_reason)

    if cold_bias_block is None:
        cold_bias_block = cold_bias_blocks_yes(bias_info)
    if cold_bias_block and side == "YES" and direction in ("cold", "mid"):
        blocked.append("cold_bias_guard (YES cold/mid bloqueado)")

    if direction == "cold" and streak_cold_n >= STREAK_BLOCK_AT:
        blocked.append(f"streak cold-side {streak_cold_n} losses")
    if direction == "hot" and streak_hot_n >= STREAK_BLOCK_AT:
        blocked.append(f"streak hot-side {streak_hot_n} losses")

    min_edge = EDGE_MIN_BY_STATION.get(station_id, EDGE_MIN_DEFAULT)
    if edge_pp < min_edge:
        blocked.append(
            f"edge {edge_pp:.1f}pp < minimo {min_edge:.0f}pp para {station_id}")

    return {
        "recommended_side": side,
        "edge_pp": edge_pp,
        "prob_source": prob_source,
        "direction": direction,
        "blocked_reasons": blocked,
        "actionable": not blocked,
        "min_edge_required_pp": min_edge,
    }
