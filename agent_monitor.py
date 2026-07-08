"""Monitor de oportunidades Kalshi vía Claude Haiku.

Corre cada 15 min vía cron. Lee analysis.db (20 estaciones · snapshots de
ensemble + bins Kalshi) + assertions del usuario, llama a Haiku con contexto
estructurado + reglas duras de memoria, parsea respuesta JSON y guarda
decisiones + alertas en agent.db.

Hard cap soft: si total_spend ≥ SOFT_CAP, exit con log.
Pause flag: si agent_state.paused=1, exit silencioso.

Diseño: 1 call por cron, ~3.5K in + 500 out = ~$0.0015/call. 96 calls/día →
~$0.15/día. $15 dura ~100 días.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PR_TZ = ZoneInfo("America/Puerto_Rico")
VALID_INTERVALS = {1, 5, 10, 30, 60, 120, 240, 480, 600, 800, 1000}

STATION_CITY = {
    "KPHX": "Phoenix", "KLAX": "Los Angeles", "KLAS": "Las Vegas",
    "KLGA": "New York", "KBOS": "Boston", "KMIA": "Miami",
    "KMDW": "Chicago", "KIAH": "Houston", "KSFO": "San Francisco",
    "KAUS": "Austin", "KDEN": "Denver", "KSAT": "San Antonio",
    "KDCA": "Washington DC", "KDFW": "Dallas", "KPHL": "Philadelphia",
    "KSEA": "Seattle", "KATL": "Atlanta", "KMSY": "New Orleans",
    "KOKC": "Oklahoma City", "KMSP": "Minneapolis",
}

PROJECT_DIR = Path(__file__).resolve().parent
ANALYSIS_DB = PROJECT_DIR / "weather-predictor" / "analysis.db"
ASSERTIONS_JSON = Path.home() / "dashboard_assertions.json"
AGENT_DB = PROJECT_DIR / "agent.db"
ENV_FILE = Path.home() / ".config" / "anthropic.env"

sys.path.insert(0, str(PROJECT_DIR / "weather-predictor"))
try:
    import agent_signals as _A
except Exception:
    _A = None  # pre-deployment fallback

MODEL = "claude-haiku-4-5-20251001"
PRICE_IN = 0.25 / 1_000_000
PRICE_OUT = 1.25 / 1_000_000
DEFAULT_BUDGET_CAP = 15.00
SOFT_CAP = 14.50
MAX_TOKENS = 1500


def _load_api_key() -> str:
    if not ENV_FILE.exists():
        print(f"ERROR: {ENV_FILE} no existe", file=sys.stderr)
        sys.exit(1)
    for ln in ENV_FILE.read_text().splitlines():
        if ln.startswith("ANTHROPIC_API_KEY="):
            return ln.split("=", 1)[1].strip()
    print(f"ERROR: ANTHROPIC_API_KEY no encontrado en {ENV_FILE}", file=sys.stderr)
    sys.exit(1)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(AGENT_DB)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS agent_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            n_opportunities INTEGER,
            summary TEXT,
            opportunities_json TEXT,
            raw_response TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ad_ts ON agent_decisions(ts);

        CREATE TABLE IF NOT EXISTS agent_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    defaults = [
        ("budget_cap", str(DEFAULT_BUDGET_CAP)),
        ("paused", "0"),
        ("interval_min", "15"),
        ("night_off", "1"),
        ("burst_until", ""),
    ]
    for k, v in defaults:
        if not c.execute("SELECT value FROM agent_state WHERE key=?", (k,)).fetchone():
            c.execute("INSERT INTO agent_state(key,value) VALUES(?,?)", (k, v))
    # Idempotent ALTER para distinguir canned prompts on-demand (ask_kind)
    cols = {r[1] for r in c.execute(
        "PRAGMA table_info(agent_decisions)").fetchall()}
    if "ask_kind" not in cols:
        c.execute("ALTER TABLE agent_decisions ADD COLUMN ask_kind TEXT")
    c.commit()
    return c


def _state(c: sqlite3.Connection, key: str, default: str = "") -> str:
    row = c.execute("SELECT value FROM agent_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _should_skip(c: sqlite3.Connection) -> tuple[bool, str]:
    """Devuelve (skip, motivo)."""
    interval_raw = _state(c, "interval_min", "15")
    burst_until = _state(c, "burst_until", "")
    night_off = _state(c, "night_off", "1") == "1"
    now_utc = datetime.now(timezone.utc)
    now_ast = now_utc.astimezone(PR_TZ)

    in_burst = False
    if burst_until:
        try:
            if datetime.fromisoformat(burst_until) > now_utc:
                in_burst = True
        except Exception:
            pass

    if not in_burst:
        if interval_raw == "off":
            return True, "interval=off"
        if night_off and 0 <= now_ast.hour < 6:
            return True, f"night_off (AST {now_ast.hour}h)"

    try:
        interval_min = 1 if in_burst else int(interval_raw)
    except ValueError:
        interval_min = 15
    if interval_min not in VALID_INTERVALS and interval_min != 1:
        interval_min = 15

    row = c.execute("""SELECT ts FROM agent_decisions
        WHERE COALESCE(is_briefing,0)=0 ORDER BY id DESC LIMIT 1""").fetchone()
    if row:
        try:
            last = datetime.fromisoformat(row[0])
            elapsed = (now_utc - last).total_seconds() / 60.0
            if elapsed < interval_min - 0.5:
                return True, f"throttle ({elapsed:.1f}/{interval_min} min)"
        except Exception:
            pass
    return False, f"run ({'burst' if in_burst else f'every {interval_min}m'})"


def _total_spend(c: sqlite3.Connection) -> float:
    row = c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM agent_decisions").fetchone()
    return float(row[0])


def _is_paused(c: sqlite3.Connection) -> bool:
    row = c.execute("SELECT value FROM agent_state WHERE key='paused'").fetchone()
    return bool(row and row[0] == "1")


def _budget_cap(c: sqlite3.Connection) -> float:
    row = c.execute("SELECT value FROM agent_state WHERE key='budget_cap'").fetchone()
    return float(row[0]) if row else DEFAULT_BUDGET_CAP


def _gather_context() -> dict:
    """Lee analysis.db y assertions. Devuelve dict serializable para prompt.

    Codex Round 5 (2026-06-29): expone bloque `signals` per-station (bias,
    ext_diff, difficulty, cold_bias_block, ROI historico, streaks, Brier 7d)
    y per-bin (`model_p_calibrated`, `edge_calibrated_pp`, `actionable`,
    `blocked_reasons`). Cargado vía agent_signals.evaluate_bin para que
    lectura, poller y agent compartan misma decisión.
    """
    def _r(v, n):
        return round(v, n) if v is not None else None

    out: dict = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                 "stations": [], "assertions": []}
    if not ANALYSIS_DB.exists():
        return out
    db = sqlite3.connect(ANALYSIS_DB)
    db.execute("PRAGMA busy_timeout=5000")  # race vs poller writes
    db.row_factory = sqlite3.Row
    # detectar columnas disponibles (fallback si poller no migró aún)
    ss_cols = {r[1] for r in db.execute("PRAGMA table_info(station_snapshots)").fetchall()}
    ks_cols = {r[1] for r in db.execute("PRAGMA table_info(kalshi_snapshots)").fetchall()}
    has_signals = "pred_calibrated_f" in ss_cols
    has_cal_p = "our_p_calibrated" in ks_cols

    base_cols = ("s.station, s.ts, s.current_f, s.today_max_obs, "
                 "s.ens_med, s.ens_p10, s.ens_p90, s.peak_status")
    has_dir_roi = "roi_cold_pct" in ss_cols
    dir_roi_cols = (", s.roi_cold_pct, s.trades_cold, "
                    "s.roi_hot_pct, s.trades_hot, "
                    "s.roi_mid_pct, s.trades_mid") if has_dir_roi else ""
    sig_cols = (("", ", s.pred_calibrated_f, s.bias_f, s.bias_path, "
                     "s.ext_med_f, s.ext_spread_f, s.ext_diff_f, "
                     "s.difficulty_score, s.difficulty_label, "
                     "s.cold_bias_block, s.streak_block_hot, s.streak_block_cold, "
                     "s.roi_hist_pct, s.trades_settled, s.wins_settled, "
                     "s.brier_us_7d, s.brier_kalshi_7d, s.signal_error"
                )[1 if has_signals else 0]) + (dir_roi_cols if has_signals else "")
    cur = db.execute(f"""
        WITH latest AS (
            SELECT station, MAX(ts) AS ts FROM station_snapshots GROUP BY station
        )
        SELECT {base_cols}{sig_cols}
        FROM station_snapshots s
        JOIN latest l ON s.station=l.station AND s.ts=l.ts
        ORDER BY s.station
    """)
    stations: dict = {}
    now_utc = datetime.now(timezone.utc)
    for r in cur:
        stn = r["station"]
        spread = (r["ens_p90"] or 0) - (r["ens_p10"] or 0)
        # snapshot_age_min: si el poller lleva atrasado el LLM debe verlo.
        # Fable review 2026-07-02: "datos de hace 4h presentados sin edad es
        # el mismo bug de vigencia en versión suave" — exponemos edad para
        # que reasoning y decisiones puedan ponderar staleness.
        snap_age_min = None
        try:
            _snap_dt = datetime.fromisoformat((r["ts"] or "").replace("Z", "+00:00"))
            if _snap_dt.tzinfo is None:
                _snap_dt = _snap_dt.replace(tzinfo=timezone.utc)
            snap_age_min = round((now_utc - _snap_dt).total_seconds() / 60.0, 1)
        except Exception:
            pass
        st = {
            "station": stn,
            "snap_ts": r["ts"],
            "snapshot_age_min": snap_age_min,
            "current_f": r["current_f"],
            "today_max_obs": r["today_max_obs"],
            "ens_med": round(r["ens_med"], 1) if r["ens_med"] else None,
            "ens_p10": round(r["ens_p10"], 1) if r["ens_p10"] else None,
            "ens_p90": round(r["ens_p90"], 1) if r["ens_p90"] else None,
            "ens_spread": round(spread, 1),
            "peak_status": r["peak_status"],
            "bins": [],
        }
        if has_signals:
            st["signals"] = {
                "pred_calibrated_f": _r(r["pred_calibrated_f"], 1),
                "bias_f": _r(r["bias_f"], 2),
                "bias_path": r["bias_path"],
                "ext_med_f": _r(r["ext_med_f"], 1),
                "ext_spread_f": _r(r["ext_spread_f"], 1),
                "ext_diff_f": _r(r["ext_diff_f"], 1),
                "difficulty_score": _r(r["difficulty_score"], 0),
                "difficulty_label": r["difficulty_label"],
                "cold_bias_block": bool(r["cold_bias_block"]),
                "streak_block_hot": r["streak_block_hot"] or 0,
                "streak_block_cold": r["streak_block_cold"] or 0,
                "roi_hist_pct": _r(r["roi_hist_pct"], 1),
                "trades_settled": r["trades_settled"],
                "wins_settled": r["wins_settled"],
                "roi_cold_pct": _r(r["roi_cold_pct"], 1) if has_dir_roi else None,
                "trades_cold": (r["trades_cold"] or 0) if has_dir_roi else 0,
                "roi_hot_pct": _r(r["roi_hot_pct"], 1) if has_dir_roi else None,
                "trades_hot": (r["trades_hot"] or 0) if has_dir_roi else 0,
                "roi_mid_pct": _r(r["roi_mid_pct"], 1) if has_dir_roi else None,
                "trades_mid": (r["trades_mid"] or 0) if has_dir_roi else 0,
                "brier_us_7d": _r(r["brier_us_7d"], 4),
                "brier_kalshi_7d": _r(r["brier_kalshi_7d"], 4),
                "signal_error": r["signal_error"],
            }
        stations[stn] = st
    # kalshi bins latest per (station, bin) — con filtro de vigencia:
    # sólo bins presentes en el último ciclo del poller (± 5min tolerancia).
    # Kalshi retira bins durante el día (series par/impar, tail edges); sin
    # este filtro, MAX(ts) per-bin quedaba anclado a snapshots de horas atrás
    # y el prompt veía "actionable" fantasmas. Tolerancia cubre ciclo parcial
    # si el poller muere a mitad. Ver claude_review_2026_07_01 §freshness.
    cal_sel = ", k.our_p_calibrated" if has_cal_p else ""
    cur = db.execute(f"""
        WITH station_ts AS (
            SELECT station, MAX(ts) AS max_ts FROM kalshi_snapshots GROUP BY station
        ),
        latest AS (
            SELECT k.station, k.bin_lo, k.bin_hi, MAX(k.ts) AS ts
            FROM kalshi_snapshots k
            JOIN station_ts st ON st.station = k.station
            WHERE k.ts >= datetime(st.max_ts, '-5 minutes')
            GROUP BY k.station, k.bin_lo, k.bin_hi
        )
        SELECT k.station, k.label, k.bin_lo, k.bin_hi, k.yes_mid, k.our_p{cal_sel}
        FROM kalshi_snapshots k
        JOIN latest l ON k.station=l.station AND k.bin_lo=l.bin_lo
                     AND k.bin_hi=l.bin_hi AND k.ts=l.ts
        ORDER BY k.station, k.bin_lo
    """)
    for r in cur:
        stn = r["station"]
        if stn not in stations:
            continue
        raw_p = r["our_p"]
        cal_p = r["our_p_calibrated"] if has_cal_p else None
        yes_mid = r["yes_mid"]
        edge_raw_pp = round((raw_p - yes_mid) * 100, 1) if (raw_p is not None and yes_mid is not None) else None
        bin_entry = {
            "label": r["label"],
            "kalshi_yes": _r(yes_mid, 2),
            "model_p_raw": _r(raw_p, 2),
            "model_p_calibrated": _r(cal_p, 2),
            "edge_raw_pp": edge_raw_pp,
        }
        # evaluate_bin para añadir actionable + blocked_reasons + recommended_side.
        # Usa cal si está, fallback a raw (el módulo marca prob_source).
        if _A is not None:
            sig = stations[stn].get("signals") or {}
            bias_info = None
            if sig.get("bias_f") is not None:
                bias_info = {
                    "bias": sig["bias_f"],
                    "sign_nudge": (sig.get("bias_path") == "nudge"),
                    "streak_len": 0,  # no lo persistimos; cold_bias_block ya capturó la señal
                }
            ev = _A.evaluate_bin(
                station_id=stn,
                bin_lo=r["bin_lo"], bin_hi=r["bin_hi"],
                bin_label=r["label"],
                kalshi_yes_price=yes_mid,
                model_p_calibrated=cal_p,
                model_p_raw=raw_p,
                pred_calibrated_f=sig.get("pred_calibrated_f"),
                bias_info=bias_info,
                ext_diff_f=sig.get("ext_diff_f"),
                difficulty_score=sig.get("difficulty_score"),
                streak_hot_n=sig.get("streak_block_hot", 0),
                streak_cold_n=sig.get("streak_block_cold", 0),
                cold_bias_block=sig.get("cold_bias_block"),
            )
            bin_entry["recommended_side"] = ev["recommended_side"]
            bin_entry["edge_calibrated_pp"] = round(ev["edge_pp"], 1) if ev["edge_pp"] is not None else None
            bin_entry["direction"] = ev["direction"]
            bin_entry["actionable"] = ev["actionable"]
            bin_entry["blocked_reasons"] = ev["blocked_reasons"]
            bin_entry["min_edge_required_pp"] = ev["min_edge_required_pp"]
            bin_entry["prob_source"] = ev["prob_source"]
        stations[stn]["bins"].append(bin_entry)
    out["stations"] = list(stations.values())
    db.close()
    # assertions
    if ASSERTIONS_JSON.exists():
        try:
            d = json.loads(ASSERTIONS_JSON.read_text())
            for slot, a in sorted(d.items()):
                out["assertions"].append({
                    "slot": slot, "station": a["station"], "side": a["side"],
                    "lo": a["lo"], "hi": a["hi"], "user_prob": a["prob"],
                })
        except Exception:
            pass
    return out


SYSTEM_PROMPT = """Eres un analista de oportunidades en mercados Kalshi de temperatura máxima diaria.
Tu trabajo: revisar snapshots cada 15 min y decidir si hay edges accionables que reportar al usuario.

DETECTAR MERCADO SETTLED (skip completo):
Si para una estación: `ens_spread == 0.0` Y `today_max_obs is not None` Y `today_max_obs ≈ ens_med` → el día YA TERMINÓ.
Los snapshots de Kalshi en este caso reflejan la liquidación, no precios live. NUNCA flagees opps en estaciones settled.

REGLAS DURAS (NUNCA flag opportunity si se viola):
1. Spread ens_p90-ens_p10 > 5°F → modelo incierto, no flag
2. Modelo crudo `model_p_raw` viene del ensemble GFS sin calibración bayesiana/bias/isotonic. Es ruidoso. Solo flag si modelo Y otra señal coinciden.
3. KLGA settle es Central Park (KNYC), no LGA. LGA suele ser ~1.5°F más caliente que CP en verano. Si flageas KLGA, ajusta.
4. Conviction "high" solo si edge ≥ 30pp + 3 señales convergen. "med" si edge 15-30pp. "low" no se reportan.

GUARDRAIL DURO (Codex Round 5):
Cada bin del prompt trae tag `[ACTIONABLE]` o `[blocked]`. Si dice
`[blocked]` ya fallaron nuestras reglas (difficulty, cold-bias, streak,
edge mínimo, etc.) — NUNCA lo recomiendes, ni siquiera "casi seguro".
Las razones aparecen tras "—". El bloque SIGNALS por estación muestra
bias, ext_diff, difficulty, cold_bias_block, ROI histórico: úsalos
para entender por qué algo está blocked y para tu razonamiento. Si
todos los bins de todas las estaciones están blocked, devuelve
opportunities=[] con no_opp_reason claro.

LÓGICA SIDE (CRÍTICO — no equivocar dirección):
- `edge_raw_pp = (model_p_raw - kalshi_yes) × 100`
- Si `edge_raw_pp > 0` → modelo dice MÁS prob que mercado → recomienda **BUY YES** (side="YES")
- Si `edge_raw_pp < 0` → modelo dice MENOS prob que mercado → recomienda **BUY NO** (side="NO")
- Ejemplo: Kalshi YES 0.14, model 0.96, edge +82pp → side="YES" (compra YES barato)
- Ejemplo: Kalshi YES 0.66, model 0.10, edge -56pp → side="NO" (vende YES caro = compra NO)

OUTPUT: JSON puro, sin markdown fences, sin texto antes/después. MÁXIMO 5 opportunities (las de mayor edge). Schema:
{
  "opportunities": [
    {
      "station": "KMIA",
      "side": "YES|NO",
      "bin": "label exacto del bin (de los datos)",
      "kalshi_yes": 0.14,
      "model_p_raw": 0.96,
      "conviction": "high|med",
      "reasoning": "1-2 frases concretas, mencionar señales convergentes"
    }
  ],
  "summary": "1 frase resumen del ciclo (cuántas estaciones live vs settled, observación clave)",
  "no_opp_reason": "si opportunities=[], 1 frase por qué"
}

Si no hay oportunidad accionable, devuelve opportunities=[]. Es mejor no flagear que flagear ruido.

FORMATO ESTACIÓN: cuando menciones una estación en `summary`, `reasoning` o `no_opp_reason`, escribe siempre "KXXX (Ciudad)". Ejemplos: "KBOS (Boston)", "KMIA (Miami)", "KPHX (Phoenix)". El campo `station` del JSON queda como código solo (KBOS).
"""


def _build_user_prompt(ctx: dict) -> str:
    lines = [f"TIMESTAMP UTC: {ctx['timestamp_utc']}", ""]
    lines.append("=== ESTACIONES (último snapshot) ===")
    for s in ctx["stations"]:
        city = STATION_CITY.get(s["station"], "")
        label = f"{s['station']} ({city})" if city else s["station"]
        age = s.get("snapshot_age_min")
        age_tag = ""
        if age is not None:
            if age >= 30:
                age_tag = f" [STALE snap {age:.0f}min]"
            elif age >= 10:
                age_tag = f" [snap {age:.0f}min]"
        lines.append(
            f"{label}{age_tag}: current={s['current_f']}°F, today_max_obs={s['today_max_obs']}, "
            f"ens_med={s['ens_med']}, ens_p10-p90={s['ens_p10']}-{s['ens_p90']} "
            f"(spread {s['ens_spread']}°F), peak={s['peak_status']}"
        )
        sig = s.get("signals") or {}
        if sig:
            parts = []
            if sig.get("pred_calibrated_f") is not None:
                parts.append(f"pred_cal={sig['pred_calibrated_f']}°F")
            if sig.get("bias_f") is not None:
                parts.append(f"bias={sig['bias_f']:+.2f}({sig.get('bias_path','?')})")
            if sig.get("ext_diff_f") is not None:
                parts.append(f"ext_diff={sig['ext_diff_f']:+.1f}")
            if sig.get("difficulty_score") is not None:
                parts.append(f"diff={int(sig['difficulty_score'])}({sig.get('difficulty_label','')})")
            if sig.get("cold_bias_block"):
                parts.append("cold_bias_BLOCK")
            if sig.get("streak_block_hot"):
                parts.append(f"streak_hot{sig['streak_block_hot']}")
            if sig.get("streak_block_cold"):
                parts.append(f"streak_cold{sig['streak_block_cold']}")
            if sig.get("roi_hist_pct") is not None and sig.get("trades_settled"):
                parts.append(f"ROI={sig['roi_hist_pct']:+.1f}%(n={sig['trades_settled']})")
            dir_bits = []
            for d in ("cold", "hot", "mid"):
                n = sig.get(f"trades_{d}") or 0
                roi = sig.get(f"roi_{d}_pct")
                if n > 0 and roi is not None:
                    dir_bits.append(f"{d}:{roi:+.1f}%(n={n})")
            if dir_bits:
                parts.append("ROI_by_dir=" + " ".join(dir_bits))
            if parts:
                lines.append("  SIGNALS: " + " · ".join(parts))
        for b in s["bins"]:
            if b["kalshi_yes"] is None:
                continue
            actionable = b.get("actionable")
            shown_edge = b.get("edge_calibrated_pp")
            if shown_edge is None:
                shown_edge = b.get("edge_raw_pp")
            if abs(shown_edge or 0) >= 10 or actionable is True:
                cal_p = b.get("model_p_calibrated")
                p_str = (f"model_cal {cal_p:.2f}" if cal_p is not None
                         else f"model_raw {b['model_p_raw']:.2f}")
                side = b.get("recommended_side") or "?"
                tag = "ACTIONABLE" if actionable else "blocked"
                line = (f"  · {b['label']} | Kalshi YES {b['kalshi_yes']:.2f} | "
                        f"{p_str} | edge {shown_edge:+.0f}pp → {side} [{tag}]")
                if actionable is False:
                    reasons = b.get("blocked_reasons") or []
                    if reasons:
                        line += " — " + "; ".join(reasons[:2])
                lines.append(line)
    lines.append("")
    if ctx["assertions"]:
        lines.append("=== ASEVERACIONES ACTIVAS DEL USUARIO ===")
        for a in ctx["assertions"]:
            rng = f"{a['lo']}-{a['hi']}" if a["lo"] != -1 else f"≤{a['hi']}"
            lines.append(f"Slot {a['slot']}: {a['station']} {a['side']} {rng}°F @ user {a['user_prob']}%")
        lines.append("")
    lines.append("Aplica las reglas duras. Devuelve JSON puro.")
    return "\n".join(lines)


def _call_haiku(api_key: str, system: str, user: str) -> dict:
    payload = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code} from Anthropic: {body}", file=sys.stderr)
        raise


PICK_KINDS = {"top_picks_now", "best_setup", "skip_or_bet"}


def _filter_actionable_opps(opps: list, ctx: dict) -> tuple[list, list]:
    """Drop opps cuyo (station,bin) ya está marcado actionable=False.

    Hard guardrail Codex Round 5: el agente no debe surface picks que
    nuestras propias reglas (difficulty, cold-bias, streak, edge mínimo)
    ya bloquearon. Si el LLM las sugiere igualmente, las eliminamos aquí
    y dejamos rastro en summary.
    """
    by_key = {}
    for s in ctx.get("stations", []):
        for b in s.get("bins", []):
            by_key[(s["station"], b.get("label"))] = b
    kept, dropped = [], []
    for o in opps or []:
        stn = o.get("station")
        bin_label = o.get("bin")
        b = by_key.get((stn, bin_label))
        # Sin match: dejamos pasar (probablemente label normalizado distinto;
        # mejor no-falso-positivo que comerse picks legítimos).
        if b is None:
            kept.append(o)
            continue
        if b.get("actionable") is False:
            dropped.append({
                "station": stn, "bin": bin_label,
                "reasons": b.get("blocked_reasons") or [],
            })
        else:
            kept.append(o)
    return kept, dropped


def _parse_response(resp: dict) -> tuple[dict, int, int]:
    usage = resp.get("usage", {})
    tin = usage.get("input_tokens", 0)
    tout = usage.get("output_tokens", 0)
    text = (resp.get("content") or [{}])[0].get("text", "").strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {"opportunities": [], "summary": "JSON parse error",
                  "no_opp_reason": text[:200]}
    return parsed, tin, tout


# Canned prompts disponibles vía /ai/ask (botones en dashboard).
# Cada entry: label (UI) + instructions (apéndice al user_prompt regular).
# El SYSTEM_PROMPT no cambia — solo se añaden instrucciones específicas.
PROMPTS = {
    "best_setup": {
        "label": "🎯 Setup más fuerte",
        "instructions": (
            "Identifica EL setup más fuerte ahora mismo. Solo 1 oportunidad "
            "(la de mayor convicción + edge). Convicción 'high' o 'med'. "
            "Razón en 2-3 frases concretas."
        ),
    },
    "regime_broken": {
        "label": "💥 Régimen roto",
        "instructions": (
            "¿Hay estaciones con regime_break, divergencia mercado-modelo "
            "≥3°F, o señales de modelo blown? Lista cada caso en opportunities "
            "(o vacío con no_opp_reason si no hay). NO recomiendes bets en "
            "estaciones con régimen roto — sólo flagealas como riesgo."
        ),
    },
    "summary_3bullets": {
        "label": "📊 Resumen 3 bullets",
        "instructions": (
            "Devuelve summary largo con 3 bullets numerados: "
            "(1) estado general del día (cuántas live vs settled, regímenes "
            "raros), (2) top oportunidad accionable si la hay, "
            "(3) principal riesgo del día. opportunities=[]."
        ),
    },
    "skip_or_bet": {
        "label": "❓ ¿Saltar el día?",
        "instructions": (
            "Recomendación binaria: ¿es buen día para apostar o mejor "
            "sentarse? Pon la respuesta clara en summary + razón. Si hay "
            "1 setup que justifique apostar, inclúyelo en opportunities "
            "(máx 1, conviction high/med). Si no, opportunities=[]."
        ),
    },
    "top_picks_now": {
        "label": "💎 Top 3 oportunidades",
        "instructions": (
            "Escanea TODAS las estaciones del contexto y devuelve las 3 "
            "mejores oportunidades 'casi seguras con edge'. Criterio de "
            "aceptación (cualquiera de los dos): "
            "(A) Bin límite (label 'X or below' o 'X or above') con our_p ≤ 10% "
            "o ≥ 90% y |edge| ≥ 30pp; "
            "(B) YES en bin bound ('X or below' / 'X or above') con "
            "our_p ≥ 95%, kalshi ≤ 10¢ y models_spread ≤ 2°F. "
            "Excluye estaciones bloqueadas por cold-bias, streak o "
            "régimen roto. Devuelve hasta 3 opportunities ordenadas por "
            "|edge| descendente; cada una con station, bin, side, "
            "our_p%, kalshi%, edge_pp y una sola frase explicando por "
            "qué es segura (obs ya cerca del bound, ensemble apretado, "
            "racha activa, etc.). En summary: una línea por pick + al "
            "final 'Si elegís solo una: <pick> porque <razón corta>'. "
            "Si no hay ninguna que cumpla el criterio, opportunities=[] "
            "y summary='Sin picks casi-seguros ahora mismo'."
        ),
    },
}


STATION_PROMPTS = {
    "max_today": {
        "label": "🌡 Max hoy",
        "question": (
            "¿Cuál crees que será la temperatura máxima HOY en esta estación? "
            "Da un número específico (°F) y rango razonable. Toma en cuenta el "
            "ensemble, observación parcial si la hay, y consideraciones locales."
        ),
    },
    "panorama": {
        "label": "🔭 Panorama",
        "question": (
            "¿Cómo ves el panorama de HOY en esta estación? Resume en 2-3 frases: "
            "patrón sinóptico que ves, confianza del modelo, y si hay banderas "
            "rojas (régimen raro, divergencia, etc.)."
        ),
    },
    "market_aligned": {
        "label": "🎯 ¿Mercado alineado?",
        "question": (
            "¿El mercado Kalshi está alineado con nuestro modelo en esta estación? "
            "Compara el bin con más volumen vs nuestro ens_med. Dime: alineado / "
            "desalineado y de qué lado. Si desalineado, ¿quién parece tener razón "
            "según las pistas (observación, climatología, consideraciones locales)?"
        ),
    },
    "safe_bracket": {
        "label": "🛡 Bracket más seguro",
        "question": (
            "¿Cuál bracket te parece MÁS SEGURO para apostar en esta estación? "
            "Prioriza bin límite (label 'X or below' o 'X or above') con our_p "
            "≤ 10% o ≥ 90% y |edge| ≥ 10pp. Da: bin exacto, side (YES/NO), edge en pp."
        ),
    },
    "profit_bracket": {
        "label": "💰 Bracket más profit",
        "question": (
            "¿Cuál bracket tiene MÁS UPSIDE de profit en esta estación, aún con "
            "más riesgo? Top edge por |diff|, cualquier dirección, incluyendo "
            "middle-bin si aplica. Da: bin exacto, side, edge en pp, y advertencia "
            "de riesgo (varianza, régimen)."
        ),
    },
    "streak_status": {
        "label": "🔥 Racha",
        "question": (
            "¿Esta estación está en racha de precisión? Mira el bloque RACHA "
            "(|err|≤1.5°F por ventana local 06/09/12/15/17). Si hay racha ≥3 "
            "días en alguna ventana, dilo. Si ninguna ventana llega a 3, "
            "señala la mejor disponible o di 'sin racha activa'. No recomiendes "
            "bet basado solo en racha — solo describe el estado."
        ),
    },
}


STATION_SYSTEM_PROMPT = """Eres un analista de mercados Kalshi de temperatura máxima diaria.
El usuario te pregunta sobre UNA estación específica. Responde en ESPAÑOL, breve y directo:
- Máximo 3-4 frases (~80 palabras)
- Sin markdown, sin JSON, sin fences
- Si recomiendas un bet: di bin exacto + side (YES/NO) + edge en pp
- Si NO recomiendas (régimen roto, day settled, spread alto): dilo claro

CONTEXTO QUE RECIBES:
- Snapshot ensemble (ens_med, p10, p90, spread, peak_status)
- today_max_obs (si ya hay observación parcial)
- Bins Kalshi con our_p (calibrado) y yes_mid
- Brief geo-climático de la estación (consideraciones locales)
- Bloque RACHA: días seguidos con |err|≤1.5°F por ventana local 06/09/12/15/17
- Asserts activos del usuario

REGLAS:
- Spread p90-p10 >5°F = modelo incierto, sé prudente
- today_max_obs ≈ ens_med con spread=0 → día settled, no recomendar
- KLGA settle es Central Park (KNYC) — LGA suele ser 1-3°F más caliente
- Si el brief menciona patrón (capa marina, lake breeze, monsoon) y aplica al
  día actual (mes, viento implícito por spread), inclúyelo en la razón.
"""


def _gather_station_ctx(station_id: str) -> dict:
    """Solo esa estación. Reusa _gather_context() y filtra."""
    full = _gather_context()
    sts = [s for s in full["stations"] if s["station"] == station_id]
    return {"timestamp_utc": full["timestamp_utc"],
            "station_data": sts[0] if sts else None,
            "assertions": [a for a in full["assertions"]
                           if a["station"] == station_id]}


def _build_station_prompt(ctx: dict, station_id: str, question: str,
                          brief: tuple[str, str] | None) -> str:
    lines = [f"ESTACIÓN: {station_id}",
             f"TIMESTAMP UTC: {ctx['timestamp_utc']}", ""]
    if brief:
        lines.append(f"=== CONTEXTO LOCAL FIJO ===")
        lines.append(f"{brief[0]}")
        lines.append(brief[1])
        lines.append("")
    sd = ctx.get("station_data")
    if sd:
        lines.append("=== SNAPSHOT ===")
        lines.append(
            f"current={sd['current_f']}°F · today_max_obs={sd['today_max_obs']} · "
            f"ens_med={sd['ens_med']}°F · p10-p90={sd['ens_p10']}-{sd['ens_p90']}°F "
            f"(spread {sd['ens_spread']}°F) · peak_status={sd['peak_status']}"
        )
        lines.append("")
        if sd["bins"]:
            lines.append("=== BINS KALSHI (con our_p calibrado) ===")
            for b in sd["bins"]:
                if b["kalshi_yes"] is None:
                    continue
                lines.append(
                    f"  {b['label']} | Kalshi YES {b['kalshi_yes']:.2f} | "
                    f"our_p {b['model_p_raw']:.2f} | edge {b['edge_raw_pp']:+.0f}pp"
                )
            lines.append("")
        # RACHA de precisión por ventana local — mismas reglas que /api/streak
        try:
            import sys as _sys
            _sys.path.insert(0, str(PROJECT_DIR / "weather-predictor"))
            import streaks as _streaks
            _cal_db = PROJECT_DIR / "weather-predictor" / "calibration.db"
            _st = _streaks.compute_streaks(str(_cal_db), stations=[station_id])
            lines.append(f"=== RACHA |err|≤{_streaks.THRESH_F}°F ===")
            for w in _streaks.WINDOWS_LOCAL:
                rows = _st.get(w, [])
                if rows:
                    r = rows[0]
                    sample = ", ".join(
                        f"{dd.date.strftime('%m-%d')} Δ{dd.err_f:+.1f}"
                        for dd in r.details[:3])
                    lines.append(f"  {w:02d}:00 local → {r.streak_days}d ({sample})")
                else:
                    lines.append(f"  {w:02d}:00 local → 0d")
            lines.append("")
        except Exception:
            pass
    else:
        lines.append("(no hay snapshot reciente — analysis_poller puede estar atrasado)")
        lines.append("")
    if ctx["assertions"]:
        lines.append("=== ASERTOS DEL USUARIO PARA ESTA ESTACIÓN ===")
        for a in ctx["assertions"]:
            rng = f"{a['lo']}-{a['hi']}" if a["lo"] != -1 else f"≤{a['hi']}"
            lines.append(f"Slot {a['slot']}: {a['side']} {rng}°F @ user {a['user_prob']}%")
        lines.append("")
    lines.append(f"=== PREGUNTA ===")
    lines.append(question)
    return "\n".join(lines)


def ask_station(kind: str, station_id: str) -> dict:
    """Respuesta conversacional sobre UNA estación. Texto plano, sin JSON.

    Guarda el resultado en agent_state con key 'last_station_ask:{STN}' para
    que /comparison lo renderice. Respeta paused + budget como ask().
    """
    if kind not in STATION_PROMPTS:
        return {"ok": False, "error": f"prompt '{kind}' no existe"}
    station_id = station_id.upper()
    c = _conn()
    if _is_paused(c):
        c.close()
        return {"ok": False, "error": "agente pausado"}
    cap = _budget_cap(c)
    spent = _total_spend(c)
    soft = min(SOFT_CAP, cap - 0.50)
    if spent >= soft:
        c.close()
        return {"ok": False,
                "error": f"budget agotado (${spent:.2f}/${cap:.2f})"}
    ctx = _gather_station_ctx(station_id)
    if ctx["station_data"] is None:
        c.close()
        return {"ok": False,
                "error": f"sin snapshot reciente para {station_id}"}
    try:
        api_key = _load_api_key()
    except SystemExit:
        c.close()
        return {"ok": False, "error": "ANTHROPIC_API_KEY no configurada"}
    brief = None
    try:
        import station_brief as _sb
        brief = _sb.get(station_id)
    except Exception:
        pass
    user_prompt = _build_station_prompt(
        ctx, station_id, STATION_PROMPTS[kind]["question"], brief)
    try:
        resp = _call_haiku(api_key, STATION_SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        c.close()
        return {"ok": False, "error": f"Haiku error: {e}"}
    # Extract text (no JSON parsing for conversational mode)
    text = ""
    tin = resp.get("usage", {}).get("input_tokens", 0)
    tout = resp.get("usage", {}).get("output_tokens", 0)
    for blk in resp.get("content", []):
        if blk.get("type") == "text":
            text += blk.get("text", "")
    text = text.strip()
    cost = tin * PRICE_IN + tout * PRICE_OUT
    # Persist for /comparison render
    payload = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "label": STATION_PROMPTS[kind]["label"],
        "text": text,
        "cost": cost,
    })
    c.execute(
        "INSERT OR REPLACE INTO agent_state (key, value) VALUES (?, ?)",
        (f"last_station_ask:{station_id}", payload))
    # Also log in agent_decisions for budget/audit trail
    c.execute("""INSERT INTO agent_decisions
        (ts, model, tokens_in, tokens_out, cost_usd,
         n_opportunities, summary, opportunities_json, raw_response, ask_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), MODEL, tin, tout, cost,
         0, f"[{station_id}] {STATION_PROMPTS[kind]['label']}: {text[:140]}",
         "[]", json.dumps(resp.get("content", []))[:2000],
         f"station:{station_id}:{kind}"))
    c.commit()
    c.close()
    return {"ok": True, "kind": kind, "station": station_id,
            "label": STATION_PROMPTS[kind]["label"],
            "text": text, "cost": cost}


def get_last_station_ask(station_id: str) -> dict | None:
    """Lee el último ask_station para esa estación, o None."""
    c = _conn()
    row = c.execute("SELECT value FROM agent_state WHERE key=?",
                    (f"last_station_ask:{station_id.upper()}",)).fetchone()
    c.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def clear_last_station_ask(station_id: str) -> None:
    c = _conn()
    c.execute("DELETE FROM agent_state WHERE key=?",
              (f"last_station_ask:{station_id.upper()}",))
    c.commit()
    c.close()


def ask(kind: str) -> dict:
    """One-off call para botones 'Preguntar AI ahora' del dashboard.

    Salta el throttle (es petición explícita del usuario) pero respeta
    paused + budget soft cap. Guarda la respuesta en agent_decisions con
    ask_kind=<kind> para distinguir de los sweeps de cron.

    Returns {ok: bool, error?: str, summary, opportunities, cost, n_opps, kind}.
    """
    if kind not in PROMPTS:
        return {"ok": False, "error": f"prompt kind '{kind}' no existe"}
    c = _conn()
    if _is_paused(c):
        c.close()
        return {"ok": False, "error": "agente pausado — reanúdalo primero"}
    cap = _budget_cap(c)
    spent = _total_spend(c)
    soft = min(SOFT_CAP, cap - 0.50)
    if spent >= soft:
        c.close()
        return {"ok": False,
                "error": f"budget agotado (${spent:.2f}/${cap:.2f})"}
    ctx = _gather_context()
    if not ctx["stations"]:
        c.close()
        return {"ok": False, "error": "sin datos de stations (analysis.db vacío)"}
    try:
        api_key = _load_api_key()
    except SystemExit:
        c.close()
        return {"ok": False, "error": "ANTHROPIC_API_KEY no configurada"}
    base = _build_user_prompt(ctx)
    extra = ("\n\n=== INSTRUCCIÓN ESPECIAL — pregunta on-demand ===\n"
             + PROMPTS[kind]["instructions"])
    user_prompt = base + extra
    try:
        resp = _call_haiku(api_key, SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        c.close()
        return {"ok": False, "error": f"Haiku error: {e}"}
    parsed, tin, tout = _parse_response(resp)
    cost = tin * PRICE_IN + tout * PRICE_OUT
    opps = parsed.get("opportunities", []) or []
    summary = parsed.get("summary", "") or ""
    if kind in PICK_KINDS:
        opps, dropped = _filter_actionable_opps(opps, ctx)
        if dropped:
            tail = "; ".join(
                f"{d['station']}/{d['bin']} ({', '.join(d['reasons'][:2])})"
                for d in dropped)
            summary = (summary + f" · [GUARDRAIL filtró {len(dropped)} "
                       f"no-accionables: {tail}]").strip()
    c.execute("""INSERT INTO agent_decisions
        (ts, model, tokens_in, tokens_out, cost_usd,
         n_opportunities, summary, opportunities_json, raw_response, ask_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), MODEL, tin, tout, cost,
         len(opps), summary, json.dumps(opps),
         json.dumps(resp.get("content", []))[:2000], kind))
    c.commit()
    c.close()
    return {"ok": True, "kind": kind, "label": PROMPTS[kind]["label"],
            "summary": summary, "opportunities": opps,
            "cost": cost, "n_opps": len(opps)}


def main() -> None:
    c = _conn()
    if _is_paused(c):
        print(f"{datetime.now(timezone.utc).isoformat()} pausado, skip")
        return
    skip, reason = _should_skip(c)
    if skip:
        return
    print(f"{datetime.now(timezone.utc).isoformat()} {reason}")
    cap = _budget_cap(c)
    spent = _total_spend(c)
    soft = min(SOFT_CAP, cap - 0.50)
    if spent >= soft:
        print(f"BUDGET soft cap alcanzado: ${spent:.2f} / ${cap:.2f}. Pausando.")
        c.execute("UPDATE agent_state SET value='1' WHERE key='paused'")
        c.commit()
        return

    ctx = _gather_context()
    if not ctx["stations"]:
        print("sin datos de stations, skip")
        return

    api_key = _load_api_key()
    user_prompt = _build_user_prompt(ctx)
    try:
        resp = _call_haiku(api_key, SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        print(f"call_haiku fail: {e}", file=sys.stderr)
        return
    parsed, tin, tout = _parse_response(resp)
    cost = tin * PRICE_IN + tout * PRICE_OUT
    opps = parsed.get("opportunities", [])
    summary = parsed.get("summary", "")
    # Cron sweep es pick-generating por naturaleza — aplica guardrail siempre.
    opps, dropped = _filter_actionable_opps(opps, ctx)
    if dropped:
        tail = "; ".join(
            f"{d['station']}/{d['bin']} ({', '.join(d['reasons'][:2])})"
            for d in dropped)
        summary = (summary + f" · [GUARDRAIL filtró {len(dropped)} "
                   f"no-accionables: {tail}]").strip()
        print(f"GUARDRAIL dropped {len(dropped)}: {tail}")
    c.execute("""INSERT INTO agent_decisions
        (ts, model, tokens_in, tokens_out, cost_usd,
         n_opportunities, summary, opportunities_json, raw_response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), MODEL, tin, tout, cost,
         len(opps), summary, json.dumps(opps),
         json.dumps(resp.get("content", []))[:2000]))
    c.commit()
    print(f"OK · in={tin} out={tout} cost=${cost:.4f} opps={len(opps)} · {summary[:80]}")
    for o in opps:
        print(f"  → {o.get('station')} {o.get('side')} {o.get('bin')} "
              f"({o.get('conviction')}): {o.get('reasoning','')[:100]}")


if __name__ == "__main__":
    main()
