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
COLD_BIAS_BLOCK_F = -0.7    # bias EWMA ≤ esto → bloquear YES side cold/mid
COLD_STREAK_BLOCK_N = 3     # sign_nudge cold streak ≥ esto → bloquear YES
# Difficulty score gate: DESACTIVADO 2026-07-06 (Fable/Codex retro).
# Spearman(p90−p10, |err|) = +0.004 (p=0.97, N=134); en KPHX marginalmente
# negativo. La regla "no bet si difficulty>70" filtraba sobre ruido puro y
# ciega N para el análisis honesto de reliability. Readmisión solo si
# ρ > 0.3 out-of-sample sobre ≥100 station-days nuevos.
# Reversible: bajar a 70.0 restaura el guard.
DIFFICULTY_BLOCK_THR = 999.0

# Fable/Codex retro 2026-07-06: 31 shadow bets entrados ≥15:00 local aportaron
# +$1,019 = 35% del pnl headline — look-ahead puro (max verano se realiza
# 14-17h; a las 15:00 ya casi hay respuesta). Cutoff conservador a 13:00 local
# elimina la ventana con margen. Reversible: subir a 99 desactiva sin más.
LOCAL_HOUR_CUTOFF = 13

# Per-(station, direction) minimum edge override. Se aplica encima de EDGE_THR
# global cuando (station_id, direction) matchea. KPHX cold audit 2026-07-01
# (agent_signals.historical_roi by_direction) mostró N=21 trades ROI -72.9%
# — cumple regla N≥20 de feedback_backtest_before_tuning.md, autoriza guard
# más agresivo. 15pp = 3× el global; suficiente para filtrar convicción baja
# sin cortar los casos de convicción alta.
EDGE_THR_BY_STATION_DIR: dict[tuple[str, str], float] = {
    ("KPHX", "cold"): 0.15,
}

# ---- Safe mode D2 (Fable audit response 2026-07-07) ----
# Ventana de verificación post-ledger-fix hasta N≥50 bets limpios. Endurece
# filtros para evitar re-contaminar la nueva reliability curve mientras se
# materializa. Al expirar SAFE_MODE_ACTIVE_UNTIL vuelve al comportamiento
# base sin cambio de código. Reversible: bajar la fecha a hoy-1 desactiva.
SAFE_MODE_ACTIVE_UNTIL = "2026-07-20"
SAFE_MODE_MIN_EDGE_PP = 15.0
SAFE_MODE_MAX_ENTRY_HOUR_LOCAL = 11
SAFE_MODE_STREAK_BLOCK_AT = 2
SAFE_MODE_EXT_GATE_F = 1.0
SAFE_MODE_PENNY_YES_MIN_PRICE = 0.05


def _safe_mode_active(target_date: date | None = None) -> bool:
    t = target_date or date.today()
    return t.isoformat() <= SAFE_MODE_ACTIVE_UNTIL


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
                     ("our_pred_at_entry", "REAL"),
                     ("yes_bid_at_entry", "REAL"),
                     ("yes_ask_at_entry", "REAL")):
        if col not in existing_cols:
            c.execute(f"ALTER TABLE simulated_bets ADD COLUMN {col} {typ}")
    # 2026-07-01: columna `direction` (cold/hot/mid) persistida al insertar
    # bet — pre-requisito para ROI per-direction (Q5 Claude Web review).
    # Guardarla en storage evita reclasificar historia con `our_pred_f` actual
    # (mid bins dependen del pred del momento). Backfill one-shot: para bets
    # antes de 2026-06-22 (sin our_pred_at_entry) sólo tail bins son
    # inequivocos; mid → "mid" es best-effort.
    if "direction" not in existing_cols:
        c.execute("ALTER TABLE simulated_bets ADD COLUMN direction TEXT")
        rows = c.execute(
            "SELECT id, side, bin_lo, bin_hi, our_pred_at_entry "
            "FROM simulated_bets"
        ).fetchall()
        for rid, side, lo, hi, pred in rows:
            d = _direction(side, lo, hi, pred)
            c.execute("UPDATE simulated_bets SET direction=? WHERE id=?",
                      (d, rid))
    # 2026-07-03: shadow bets (fable review). blocked_by NULL = bet real que
    # cuenta al P&L. blocked_by='<reason1>,<reason2>' = bet que un guard
    # habría bloqueado; se inserta y liquida igual pero stats la excluye.
    # Motivación: sin esto, cada guard evita pérdidas pero también ciega la
    # N que las decisiones pendientes (KLAS cold N=15/20, KBOS single-digit,
    # pilar 4 hot-guard, isotónico KPHX post-Laplace) necesitan acumular.
    # guard_ev() en agent_signals mide honestamente la EV de cada guard.
    if "blocked_by" not in existing_cols:
        c.execute("ALTER TABLE simulated_bets ADD COLUMN blocked_by TEXT")
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
                   our_pred_f: float | None = None,
                   threshold: int | None = None) -> int:
    """Devuelve n_losses de la racha actual si ≥threshold, sino 0.

    `our_pred_f` se propaga a `_direction` para que las bets históricas en
    bins medios se clasifiquen direccionalmente y cuenten para la racha.
    `threshold` override lo usa safe mode D2 (2026-07-07) para endurecer a 2.
    """
    if direction == "mid":
        return 0
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(days=STREAK_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    c = _conn()
    try:
        # Filtrar shadow bets (blocked_by IS NOT NULL): la racha refleja
        # pérdidas reales, no bets que otro guard ya frenó.
        rows = c.execute("""
            SELECT side, bin_lo, bin_hi, won FROM simulated_bets
            WHERE station_id=? AND won IS NOT NULL AND date >= ?
              AND blocked_by IS NULL
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
    effective_thr = threshold if threshold is not None else STREAK_BLOCK_AT
    return streak if streak >= effective_thr else 0


def _cold_bias_blocks_yes(station_id: str, target_date: date,
                          bias_info: dict | None = None) -> bool:
    """True si la estación está en régimen frío persistente — YES sería
    apostar al bin que el modelo asigna p alta pero históricamente no hit.

    Codex Round 4 (2026-06-25): KPHX YES bets perdieron $391 en 66 apuestas
    (WR 7.6%). El edge≥5pp se calculaba sobre our_p de un modelo frío; la
    corrección de bias_tracker no era suficiente para revertirlo.

    Bloquea cuando:
      - bias rolling ≤ -0.7°F (modelo subestima el calor por ≥ 0.7°F), o
      - sign_nudge cold streak ≥ 3 días (encadenamos pred frías recientes).
    """
    if bias_info is None:
        try:
            import bias_tracker as _bt
            bias_info = _bt.compute_bias(station_id, target_date)
        except Exception:
            return False
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


def _cleanup_blocked(station_id: str, target_date: date,
                     direction: str | None, reason: str,
                     our_pred_f: float | None = None) -> int:
    """Marca bets no-settled de (station, date) como shadow retroactivo.

    Cuando un guard (streak, overnight) se activa DESPUÉS de haber colocado
    una bet real, esa bet retroactivamente debería haber sido shadow. En
    lugar de borrarla (versión previa a 2026-07-03), le pintamos
    `blocked_by='<reason>:retroactive'` para que:
      - siga liquidando y no pierda evidencia sobre la EV del guard
      - stats/historical_roi la excluyan del P&L real
      - guard_ev() la agregue bajo la etiqueta del guard causante

    Si direction is None, marca todas las del día (skip flag de estación).
    Si direction está dado, sólo las que pertenecen a esa dirección.
    """
    c = _conn()
    try:
        rows = c.execute(
            """SELECT id, side, bin_lo, bin_hi, blocked_by
               FROM simulated_bets
               WHERE station_id=? AND date=? AND outcome IS NULL""",
            (station_id, target_date.isoformat())).fetchall()
        tag = f"{reason}:retroactive"
        marked = 0
        for bid, side, lo, hi, existing in rows:
            if direction is not None and _direction(side, lo, hi,
                                                   our_pred_f) != direction:
                continue
            existing_tags = (existing or "").split(",") if existing else []
            if tag in existing_tags:
                continue
            new_by = tag if not existing else f"{existing},{tag}"
            c.execute("UPDATE simulated_bets SET blocked_by=? WHERE id=?",
                      (new_by, bid))
            marked += 1
        c.commit()
        return marked
    finally:
        c.close()


def maybe_bet(station_id: str, target_date: date, ticker: str,
              bin_lo: float, bin_hi: float, bin_label: str,
              our_p: float, kalshi_p: float,
              *,
              station_local_hour: int,
              edge_thr: float = EDGE_THR, stake: float = STAKE,
              models_spread_f: float | None = None,
              our_pred_f: float | None = None,
              ext_diff_f: float | None = None,
              bias_info: dict | None = None,
              difficulty_score: float | None = None,
              yes_bid: float | None = None,
              yes_ask: float | None = None) -> bool:
    """Registra una bet hipotética. Returns True si es bet REAL (blocked_by
    NULL); False si es shadow (algún guard disparó) o hard skip.

    Shadow bets (fable 2026-07-03): en vez de silenciar bets bloqueadas por
    guards, se insertan con `blocked_by='<reason1>,<reason2>'` y se liquidan
    igual. stats/historical_roi las excluyen del P&L real; guard_ev() mide
    ROI que TENDRÍA cada guard si no operara. Los únicos hard-skips (sin
    insert) son:
      - station_local_hour ≥ LOCAL_HOUR_CUTOFF: look-ahead (Fable retro 07-06)
      - |edge| < edge_thr: ruido, no queremos poblar shadow con todo bin
      - entry_price degenerado (≤0.01 o ≥0.99): rompe count de contracts
      - IntegrityError: bet ya existe para (station, date, ticker)

    Guards que producen shadow:
      models_spread, difficulty, station_dir_min, cold_bias, ext_diff,
      streak, overnight.

    Honest fill (Fable retro 2026-07-06): si yes_ask/yes_bid disponibles,
    entry_price usa ask (YES) o 1-bid (NO); fallback a mid si None. Mid-fill
    de 89 penny longshots había inflado el pnl histórico artificialmente.
    """
    # Look-ahead cutoff: entradas en la ventana donde el max diario ya se
    # realizó o casi. Fable/Codex retro 2026-07-06 mostró que 31 bets ≥15:00
    # local aportaron $1,019 (35% del pnl) — imposible que sea edge del modelo.
    # Safe mode D2 (2026-07-07): tighten a 11:00 hasta 2026-07-20.
    _safe = _safe_mode_active(target_date)
    _eff_cutoff = SAFE_MODE_MAX_ENTRY_HOUR_LOCAL if _safe else LOCAL_HOUR_CUTOFF
    if station_local_hour >= _eff_cutoff:
        return False

    edge = our_p - kalshi_p
    _eff_edge_thr = max(edge_thr, SAFE_MODE_MIN_EDGE_PP / 100.0) if _safe else edge_thr
    if abs(edge) < _eff_edge_thr:
        return False
    # Side + honest fill: YES rellena al ask, NO al 1-bid. Sin bid/ask cae a mid.
    if edge > 0:
        side = "yes"
        entry_price = yes_ask if yes_ask is not None else kalshi_p
    else:
        side = "no"
        entry_price = (1.0 - yes_bid) if yes_bid is not None else (1.0 - kalshi_p)
    # Avoid degenerate prices (0 or 1) which blow up contracts count.
    if entry_price <= 0.01 or entry_price >= 0.99:
        return False

    # Safe mode D2 (Fable 2026-07-07): skip tail bins (n bajo → luck domina)
    # y penny-YES fills (89 bets pre-fix con 6 winners inflaron ROI). Hard
    # skip, no shadow — no queremos poblar shadow con estas categorías.
    if _safe:
        if bin_lo == float("-inf") or bin_hi == float("inf"):
            return False
        if edge > 0 and entry_price < SAFE_MODE_PENNY_YES_MIN_PRICE:
            return False

    direction = _direction(side, bin_lo, bin_hi, our_pred_f)
    reasons: list[str] = []

    # models_spread: si GFS/ECMWF/ICON/... discrepan >3°C el día es demasiado
    # volátil para que nuestro ensemble single-model reclame edge.
    if models_spread_f is not None and models_spread_f > MAX_MODELS_SPREAD_F:
        reasons.append(f"models_spread:{models_spread_f:.1f}F")

    if difficulty_score is not None and difficulty_score > DIFFICULTY_BLOCK_THR:
        reasons.append(f"difficulty:{difficulty_score:.0f}")
        import sys
        print(f"[difficulty_guard] SHADOW {station_id} {target_date} "
              f"diff={difficulty_score:.0f} > {DIFFICULTY_BLOCK_THR:.0f} "
              f"bin={bin_label!r} edge={100*edge:+.1f}pp",
              file=sys.stderr, flush=True)

    # Per-(station, direction) min edge (KPHX cold audit 2026-07-01: N=21
    # ROI -72.9% justifica 15pp mínimo). 3× el global; filtra convicción baja.
    station_dir_min = EDGE_THR_BY_STATION_DIR.get((station_id, direction))
    if station_dir_min is not None and abs(edge) < station_dir_min:
        reasons.append(f"station_dir_min:{direction}:{100*station_dir_min:.0f}pp")
        import sys
        print(f"[station_dir_guard] SHADOW {station_id} {target_date} "
              f"dir={direction} edge={100*edge:+.1f}pp "
              f"< min={100*station_dir_min:.0f}pp bin={bin_label!r}",
              file=sys.stderr, flush=True)

    # YES cold-bias guard (Codex Round 4 2026-06-25): KPHX 5/66 WR YES side
    # (-$391) fue el agujero principal sin gate.
    if side == "yes" and direction in ("cold", "mid"):
        if _cold_bias_blocks_yes(station_id, target_date, bias_info):
            reasons.append("cold_bias")
            import sys
            _b = (bias_info or {}).get("bias", 0.0)
            _s = (bias_info or {}).get("streak_len", 0)
            print(f"[cold_bias_guard] SHADOW {station_id} {target_date} "
                  f"side=yes dir={direction} bin={bin_label!r} "
                  f"bias={_b:+.2f} streak={_s} edge={100*edge:+.1f}pp",
                  file=sys.stderr, flush=True)

    # Gate direccional vs externos (espejo de lectura.bias_blocks_bet).
    # ext_diff_f pre-shift del posterior — post-shift atenúa la señal.
    # Safe mode D2: tighten gate a 1.0°F.
    if ext_diff_f is not None and direction != "mid":
        _eff_ext = SAFE_MODE_EXT_GATE_F if _safe else EXT_GATE_F
        if direction == "cold" and ext_diff_f <= -_eff_ext:
            reasons.append(f"ext_diff:cold:{ext_diff_f:+.1f}")
        elif direction == "hot" and ext_diff_f >= _eff_ext:
            reasons.append(f"ext_diff:hot:{ext_diff_f:+.1f}")

    _eff_streak = SAFE_MODE_STREAK_BLOCK_AT if _safe else STREAK_BLOCK_AT
    if _streak_blocks(station_id, direction, our_pred_f, threshold=_eff_streak):
        reasons.append(f"streak:{direction}")
        _cleanup_blocked(station_id, target_date, direction, "streak",
                         our_pred_f)

    # Overnight skip flag (divergencia con ensemble detectada anoche).
    try:
        import overnight as _ov
        skipped, _ = _ov.is_skipped(station_id, target_date)
        if skipped:
            reasons.append("overnight")
            _cleanup_blocked(station_id, target_date, None, "overnight",
                             our_pred_f)
    except Exception:
        pass

    blocked_by = ",".join(reasons) if reasons else None
    contracts = stake / entry_price
    c = _conn()
    try:
        c.execute("""INSERT INTO simulated_bets
            (station_id, date, ticker, bin_lo, bin_hi, bin_label,
             side, our_p, kalshi_p, edge_pp, stake, entry_price,
             contracts, entered_at,
             ext_diff_at_entry, models_spread_at_entry, our_pred_at_entry,
             direction, blocked_by,
             yes_bid_at_entry, yes_ask_at_entry)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (station_id, target_date.isoformat(), ticker,
             float(bin_lo), float(bin_hi), bin_label,
             side, float(our_p), float(kalshi_p),
             100.0 * edge, float(stake), float(entry_price),
             float(contracts), datetime.utcnow().isoformat(),
             float(ext_diff_f) if ext_diff_f is not None else None,
             float(models_spread_f) if models_spread_f is not None else None,
             float(our_pred_f) if our_pred_f is not None else None,
             direction, blocked_by,
             float(yes_bid) if yes_bid is not None else None,
             float(yes_ask) if yes_ask is not None else None))
        c.commit()
        return blocked_by is None
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


def stats(station_id: str | None = None,
          include_shadow: bool = False) -> BetStats:
    """P&L agregado. Por defecto excluye shadow bets (blocked_by IS NOT NULL).
    Pasa include_shadow=True para inspeccionar toda la tabla (debug)."""
    c = _conn()
    parts: list[str] = []
    params: list = []
    if station_id:
        parts.append("station_id=?")
        params.append(station_id)
    if not include_shadow:
        parts.append("blocked_by IS NULL")
    where = f"WHERE {' AND '.join(parts)}" if parts else ""
    total = c.execute(f"SELECT COUNT(*) FROM simulated_bets {where}",
                      tuple(params)).fetchone()[0]
    settled_parts = list(parts) + ["outcome IS NOT NULL"]
    settled_where = f"WHERE {' AND '.join(settled_parts)}"
    settled = c.execute(f"""SELECT COUNT(*), COALESCE(SUM(won),0),
                                   COALESCE(SUM(stake),0),
                                   COALESCE(SUM(payoff),0),
                                   COALESCE(SUM(pnl),0)
                            FROM simulated_bets {settled_where}""",
                        tuple(params)).fetchone()
    c.close()
    ns, wins, ts, tp, pnl = settled
    roi = (pnl / ts) if ts > 0 else None
    win_rate = (wins / ns) if ns > 0 else None
    return BetStats(n_total=total, n_settled=ns, n_wins=wins,
                    total_stake=ts, total_payoff=tp, pnl=pnl,
                    roi=roi, win_rate=win_rate)
