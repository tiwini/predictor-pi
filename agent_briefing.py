"""Briefing matutino diario con Claude Sonnet.

Corre 1 vez/día a las 12:00 UTC (8 AM AST) vía cron. Lee snapshots actuales +
outcomes de ayer + última decisión del monitor, llama a Sonnet con prompt
narrativo (no JSON), guarda en agent_decisions con is_briefing=1 y opcionalmente
empuja push vía ntfy.

Costo: ~5K in + 1.5K out con Sonnet 4.6 = ~$0.04/call. 1/día → ~$1.20/mes.
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

PROJECT_DIR = Path(__file__).resolve().parent
ANALYSIS_DB = PROJECT_DIR / "weather-predictor" / "analysis.db"
CALIBRATION_DB = PROJECT_DIR / "weather-predictor" / "calibration.db"
ASSERTIONS_JSON = Path.home() / "dashboard_assertions.json"
AGENT_DB = PROJECT_DIR / "agent.db"
ENV_FILE = Path.home() / ".config" / "anthropic.env"
NTFY_ENV = Path.home() / ".config" / "ntfy.env"

MODEL = "claude-sonnet-4-6"
PRICE_IN = 3.0 / 1_000_000
PRICE_OUT = 15.0 / 1_000_000
MAX_TOKENS = 1500

STATION_CITY = {
    "KPHX": "Phoenix", "KLAX": "Los Angeles", "KLAS": "Las Vegas",
    "KNYC": "New York (Central Park)", "KBOS": "Boston", "KMIA": "Miami",
    "KMDW": "Chicago", "KIAH": "Houston", "KSFO": "San Francisco",
    "KAUS": "Austin", "KDEN": "Denver", "KSAT": "San Antonio",
    "KDCA": "Washington DC", "KDFW": "Dallas", "KPHL": "Philadelphia",
    "KSEA": "Seattle", "KATL": "Atlanta", "KMSY": "New Orleans",
    "KOKC": "Oklahoma City", "KMSP": "Minneapolis",
}


def _load_api_key() -> str:
    if not ENV_FILE.exists():
        print(f"ERROR: {ENV_FILE} no existe", file=sys.stderr)
        sys.exit(1)
    for ln in ENV_FILE.read_text().splitlines():
        if ln.startswith("ANTHROPIC_API_KEY="):
            return ln.split("=", 1)[1].strip()
    sys.exit(1)


def _load_ntfy_topic() -> str:
    if not NTFY_ENV.exists():
        return ""
    for ln in NTFY_ENV.read_text().splitlines():
        if ln.startswith("NTFY_TOPIC="):
            return ln.split("=", 1)[1].strip()
    return ""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(AGENT_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS agent_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, model TEXT, tokens_in INTEGER, tokens_out INTEGER,
        cost_usd REAL, n_opportunities INTEGER, summary TEXT,
        opportunities_json TEXT, raw_response TEXT
    )""")
    cur = c.execute("PRAGMA table_info(agent_decisions)")
    cols = [r[1] for r in cur.fetchall()]
    if "is_briefing" not in cols:
        c.execute("ALTER TABLE agent_decisions ADD COLUMN is_briefing INTEGER DEFAULT 0")
    if "briefing_text" not in cols:
        c.execute("ALTER TABLE agent_decisions ADD COLUMN briefing_text TEXT")
    c.commit()
    return c


def _gather_snapshots() -> list:
    if not ANALYSIS_DB.exists():
        return []
    db = sqlite3.connect(ANALYSIS_DB)
    db.row_factory = sqlite3.Row
    cur = db.execute("""
        WITH latest AS (
            SELECT station, MAX(ts) AS ts FROM station_snapshots GROUP BY station
        )
        SELECT s.station, s.ts, s.current_f, s.today_max_obs,
               s.ens_med, s.ens_p10, s.ens_p90, s.peak_status
        FROM station_snapshots s
        JOIN latest l ON s.station=l.station AND s.ts=l.ts
        ORDER BY s.station
    """)
    stations = {r["station"]: dict(r) for r in cur}
    cur = db.execute("""
        WITH latest AS (
            SELECT station, bin_lo, bin_hi, MAX(ts) AS ts
            FROM kalshi_snapshots GROUP BY station, bin_lo, bin_hi
        )
        SELECT k.station, k.label, k.yes_mid, k.our_p
        FROM kalshi_snapshots k
        JOIN latest l ON k.station=l.station AND k.bin_lo=l.bin_lo
                     AND k.bin_hi=l.bin_hi AND k.ts=l.ts
        ORDER BY k.station, k.bin_lo
    """)
    bins_by_stn: dict = {}
    for r in cur:
        bins_by_stn.setdefault(r["station"], []).append(dict(r))
    for stn, s in stations.items():
        s["bins"] = bins_by_stn.get(stn, [])
    db.close()
    return list(stations.values())


def _gather_yesterday_outcomes() -> list:
    if not CALIBRATION_DB.exists():
        return []
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    db = sqlite3.connect(CALIBRATION_DB)
    db.row_factory = sqlite3.Row
    try:
        cur = db.execute("""
            SELECT station, observed_max, source FROM day_outcomes
            WHERE date = ? ORDER BY station
        """, (yday,))
        out = [dict(r) for r in cur]
    except Exception:
        out = []
    db.close()
    return out


def _last_monitor_decision(c: sqlite3.Connection) -> dict:
    cur = c.execute("""SELECT ts, summary, opportunities_json FROM agent_decisions
        WHERE COALESCE(is_briefing,0)=0 ORDER BY ts DESC LIMIT 1""")
    r = cur.fetchone()
    if not r:
        return {}
    try:
        opps = json.loads(r[2] or "[]")
    except Exception:
        opps = []
    return {"ts": r[0], "summary": r[1], "opportunities": opps}


SYSTEM = """Eres analista jefe del sistema weather-predictor. Cada mañana das un briefing
breve al usuario para que sepa cómo arrancar el día sin tener que revisar todos los dashboards.

Tu briefing debe ser narrativo (no JSON), en español, máximo 6-8 líneas, estructura:
1. UNA frase de contexto general (cuántas estaciones live, régimen normal/extremo).
2. 1-3 SETUPS DEL DÍA — solo high conviction. Por cada uno: estación, side, bin, kalshi, model_p, razón en 1 frase.
3. ESTACIONES A EVITAR — las que tienen difficulty alto, régimen roto, o ya settled.
4. UNA línea de lección de ayer si hay outcomes (acierto/error vs predicción).

REGLAS DURAS:
- Conviction "high" requiere: edge≥30pp + ens_spread≤5°F + ext_med disponible. Si no, no flag.
- Mercado settled = ens_spread=0 + today_max_obs ≈ ens_med. Skip.
- KNYC = Central Park (settle Kalshi NY). NO usar LaGuardia como referencia — el sistema no lo trackea.
- Si NO hay setups buenos, dilo claramente — "hoy no tomar entries, esperar".

Tono: directo, accionable, sin disclaimers. El usuario sabe que esto es educativo.
NO uses markdown headers (###). Sí puedes usar bullets con "•" si ayuda.

FORMATO ESTACIÓN: siempre que menciones una estación, escribe "KXXX (Ciudad)". Ejemplos: "KBOS (Boston)", "KMIA (Miami)", "KPHX (Phoenix)".
"""


def _build_user_prompt(stations: list, yday: list, last_dec: dict) -> str:
    lines = [f"BRIEFING para {datetime.now(timezone.utc).date().isoformat()} UTC", ""]
    lines.append("=== SNAPSHOTS ACTUALES ===")
    for s in stations:
        spread = round((s["ens_p90"] or 0) - (s["ens_p10"] or 0), 1)
        city = STATION_CITY.get(s["station"], "")
        label = f"{s['station']} ({city})" if city else s["station"]
        lines.append(
            f"{label}: current={s['current_f']}°F, today_max_obs={s['today_max_obs']}, "
            f"ens_med={s['ens_med']}, p10-p90={s['ens_p10']}-{s['ens_p90']} "
            f"(spread {spread}°F), peak={s['peak_status']}"
        )
        for b in s["bins"]:
            if b["yes_mid"] is None or b["our_p"] is None:
                continue
            edge = round((b["our_p"] - b["yes_mid"]) * 100, 1)
            if abs(edge) >= 15:
                lines.append(
                    f"  · {b['label']} | Kalshi YES {b['yes_mid']:.2f} | "
                    f"model_raw {b['our_p']:.2f} | edge {edge:+.0f}pp"
                )
    lines.append("")
    if yday:
        lines.append("=== OUTCOMES DE AYER ===")
        for o in yday:
            city = STATION_CITY.get(o["station"], "")
            label = f"{o['station']} ({city})" if city else o["station"]
            lines.append(f"{label}: max real {o['observed_max']}°F ({o['source']})")
        lines.append("")
    if last_dec:
        lines.append(f"=== ÚLTIMO CICLO MONITOR ({last_dec.get('ts','?')}) ===")
        lines.append(f"Summary: {last_dec.get('summary','')}")
        for o in last_dec.get("opportunities", [])[:5]:
            lines.append(f"  → {o.get('station')} {o.get('side')} {o.get('bin')} ({o.get('conviction')})")
        lines.append("")
    lines.append("Genera el briefing narrativo.")
    return "\n".join(lines)


def _call_sonnet(api_key: str, system: str, user: str) -> dict:
    payload = json.dumps({
        "model": MODEL, "max_tokens": MAX_TOKENS, "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _push_ntfy(topic: str, title: str, body: str) -> None:
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": "default",
                     "Tags": "sunrise,chart_with_upwards_trend"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy push fail: {e}", file=sys.stderr)


def main() -> None:
    c = _conn()
    today = datetime.now().date().isoformat()
    cur = c.execute("""SELECT id FROM agent_decisions
        WHERE is_briefing=1 AND DATE(ts, '-4 hours')=? LIMIT 1""", (today,))
    if cur.fetchone():
        print(f"briefing ya existe para {today} (AST), skip")
        return

    stations = _gather_snapshots()
    if not stations:
        print("sin snapshots, skip")
        return
    yday = _gather_yesterday_outcomes()
    last_dec = _last_monitor_decision(c)

    api_key = _load_api_key()
    user_prompt = _build_user_prompt(stations, yday, last_dec)
    try:
        resp = _call_sonnet(api_key, SYSTEM, user_prompt)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return
    except Exception as e:
        print(f"sonnet fail: {e}", file=sys.stderr)
        return

    usage = resp.get("usage", {})
    tin = usage.get("input_tokens", 0)
    tout = usage.get("output_tokens", 0)
    text = (resp.get("content") or [{}])[0].get("text", "").strip()
    cost = tin * PRICE_IN + tout * PRICE_OUT

    c.execute("""INSERT INTO agent_decisions
        (ts, model, tokens_in, tokens_out, cost_usd, n_opportunities,
         summary, opportunities_json, raw_response, is_briefing, briefing_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (datetime.now(timezone.utc).isoformat(), MODEL, tin, tout, cost,
         0, text[:200], "[]", json.dumps(resp.get("content", []))[:2000], text))
    c.commit()
    print(f"BRIEFING OK · in={tin} out={tout} cost=${cost:.4f}")
    print(text)

    topic = _load_ntfy_topic()
    if topic:
        _push_ntfy(topic, f"Briefing {today}", text[:1500])


if __name__ == "__main__":
    main()
