"""Landing page dashboard en :8080 — links a BTC predictor (:8001) y weather (:8000).

Sin estado, sin polling. Sólo HTML estático servido por Flask para tener un
único punto de entrada cuando entras desde otra PC / cel (Tailscale o LAN).

/analysis : tab de análisis con 6 aseveraciones (estación, rango, YES/NO,
tu prob) comparadas contra el modelo y Kalshi. Lee de analysis.db poblado
por analysis_poller.py.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, request, render_template_string, redirect, url_for

app = Flask(__name__)

WEATHER_STATIONS = [
    ("KPHX", "Phoenix"),
    ("KLAX", "Los Angeles"),
    ("KLAS", "Las Vegas"),
    ("KLGA", "New York (LGA→CP)"),
    ("KBOS", "Boston"),
]

CRYPTO_SYMBOLS = [
    ("BTCUSDT", "Bitcoin"),
    ("ETHUSDT", "Ethereum"),
    ("XRPUSDT", "XRP"),
    ("DOGEUSDT", "Dogecoin"),
    ("SOLUSDT", "Solana"),
]

ANALYSIS_DB = Path.home() / "weather-predictor" / "analysis.db"
ASSERTIONS_JSON = Path.home() / "dashboard_assertions.json"
N_SLOTS = 6
PR_TZ = timezone(timedelta(hours=-4), name="AST")


def _load_assertions() -> dict:
    if not ASSERTIONS_JSON.exists():
        return {}
    try:
        return json.loads(ASSERTIONS_JSON.read_text())
    except Exception:
        return {}


def _save_assertions(d: dict) -> None:
    ASSERTIONS_JSON.write_text(json.dumps(d, indent=2))


def _conn() -> sqlite3.Connection | None:
    if not ANALYSIS_DB.exists():
        return None
    return sqlite3.connect(ANALYSIS_DB)


def _latest_station_snapshot(c: sqlite3.Connection, station: str) -> dict | None:
    cur = c.execute("""SELECT ts, current_f, today_max_obs, ens_med, ens_p10,
                              ens_p90, ens_maxes_json, peak_status
                       FROM station_snapshots WHERE station = ?
                       ORDER BY ts DESC LIMIT 1""", (station,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "ts": row[0], "current_f": row[1], "today_max_obs": row[2],
        "ens_med": row[3], "ens_p10": row[4], "ens_p90": row[5],
        "ens_maxes": json.loads(row[6]) if row[6] else [],
        "peak_status": row[7],
    }


def _bin_prob(maxes: list[float], lo: float, hi: float) -> float:
    """Misma convención que kalshi.our_p_for_bin: ±0.5 redondeo NWS."""
    if not maxes:
        return 0.0
    edge_lo = lo - 0.5
    edge_hi = hi + 0.5
    cnt = sum(1 for v in maxes if edge_lo <= v < edge_hi)
    return cnt / len(maxes)


def _kalshi_mid_for_bin(c: sqlite3.Connection, station: str,
                        lo: float, hi: float) -> tuple[float | None, str]:
    """Devuelve (yes_mid, ts) del bin Kalshi más cercano al rango, o (None, "")."""
    cur = c.execute("""SELECT yes_mid, ts FROM kalshi_snapshots
                       WHERE station = ? AND bin_lo = ? AND bin_hi = ?
                       ORDER BY ts DESC LIMIT 1""", (station, lo, hi))
    row = cur.fetchone()
    if not row:
        return None, ""
    return row[0], row[1]


def _eval_assertion(c: sqlite3.Connection, a: dict) -> dict:
    """Calcula prob modelo y Kalshi para una aseveración."""
    snap = _latest_station_snapshot(c, a["station"])
    if snap is None:
        return {"model_p": None, "kalshi_p": None, "snap_ts": None,
                "diff_model": None, "diff_kalshi": None,
                "current_f": None, "ens_med": None}
    lo, hi = float(a["lo"]), float(a["hi"])
    p_yes = _bin_prob(snap["ens_maxes"], lo, hi)
    # Para NO: prob complementaria.
    model_p = p_yes if a["side"] == "YES" else (1.0 - p_yes)

    kalshi_yes, _k_ts = _kalshi_mid_for_bin(c, a["station"], lo, hi)
    kalshi_p = None
    if kalshi_yes is not None:
        kalshi_p = kalshi_yes if a["side"] == "YES" else (1.0 - kalshi_yes)

    user_p = float(a["prob"]) / 100.0
    return {
        "model_p": model_p,
        "kalshi_p": kalshi_p,
        "snap_ts": snap["ts"],
        "current_f": snap["current_f"],
        "ens_med": snap["ens_med"],
        "diff_model": (user_p - model_p) * 100,
        "diff_kalshi": ((user_p - kalshi_p) * 100) if kalshi_p is not None else None,
    }


# ───────────────────── HTML templates ─────────────────────

TMPL = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Predictores · dashboard</title>
<style>
  :root {
    --bg: #0e1117; --panel: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff; --warn: #d29922;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 18px; font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); }
  h1 { margin: 0 0 4px 0; font-size: 22px; }
  .sub { color: var(--muted); margin-bottom: 18px; font-size: 13px; }
  .tabs { display: flex; gap: 4px; margin-bottom: 14px; border-bottom: 1px solid var(--border); }
  .tabs a { padding: 8px 14px; color: var(--muted); text-decoration: none;
            border-bottom: 2px solid transparent; font-size: 13px; }
  .tabs a.active { color: var(--accent); border-bottom-color: var(--accent); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
  .card h2 { margin: 0 0 8px 0; font-size: 16px; display: flex; align-items: baseline; gap: 8px; }
  .card h2 a { color: var(--accent); text-decoration: none; }
  .card h2 a:hover { text-decoration: underline; }
  .card h2 .tag { font-size: 11px; color: var(--muted); font-weight: normal; }
  .links { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .links a { display: inline-block; padding: 4px 8px; background: #21262d; border: 1px solid var(--border);
             border-radius: 4px; color: var(--text); text-decoration: none; font-size: 12px; }
  .links a:hover { border-color: var(--accent); color: var(--accent); }
  .row { display: flex; justify-content: space-between; gap: 8px; align-items: center;
         padding: 4px 0; border-bottom: 1px solid #21262d; font-size: 13px; }
  .row:last-child { border-bottom: none; }
  .row a { color: var(--accent); text-decoration: none; }
  .row a:hover { text-decoration: underline; }
  .row .muted { color: var(--muted); font-size: 11px; }
  .footer { margin-top: 18px; color: var(--muted); font-size: 11px; }
  code { background: #21262d; padding: 2px 5px; border-radius: 3px; font-size: 12px; }
</style>
</head>
<body>
<h1>Predictores</h1>
<div class="sub">Entrada única · {{ host }} · {{ now }}</div>
<div class="tabs">
  <a href="/" class="active">inicio</a>
  <a href="/analysis">análisis</a>
</div>

<div class="grid">

  <div class="card">
    <h2><a href="{{ btc_base }}/">Cripto</a> <span class="tag">:8001 · AST</span></h2>
    {% for sym, name in cryptos %}
    <div class="row">
      <a href="{{ btc_base }}/?symbol={{ sym }}">{{ name }}</a>
      <span class="muted">{{ sym }}</span>
    </div>
    {% endfor %}
    <div class="links">
      <a href="{{ btc_base }}/hourly-call">hourly-call</a>
      <a href="{{ btc_base }}/calibration">calibration</a>
      <a href="{{ btc_base }}/history">history</a>
      <a href="{{ btc_base }}/tutorial">tutorial</a>
    </div>
  </div>

  <div class="card">
    <h2><a href="{{ wx_base }}/">Weather</a> <span class="tag">:8000</span></h2>
    {% for sid, name in stations %}
    <div class="row">
      <a href="{{ wx_base }}/?station={{ sid }}">{{ name }}</a>
      <span class="muted">{{ sid }}</span>
    </div>
    {% endfor %}
    <div class="links">
      <a href="{{ wx_base }}/ladder">ladder</a>
      <a href="{{ wx_base }}/comparison">comparison</a>
      <a href="{{ wx_base }}/calibration">calibration</a>
      <a href="{{ wx_base }}/edge">edge</a>
      <a href="{{ wx_base }}/timing">timing</a>
      <a href="{{ wx_base }}/cross">cross</a>
      <a href="{{ wx_base }}/reweight">reweight</a>
      <a href="{{ wx_base }}/movement">movement</a>
      <a href="{{ wx_base }}/precip">precip</a>
      <a href="{{ wx_base }}/bets">bets</a>
      <a href="{{ wx_base }}/history">history</a>
      <a href="{{ wx_base }}/status">status</a>
      <a href="{{ wx_base }}/about">about</a>
    </div>
  </div>

</div>

<div class="footer">
  Nota: weather sólo tiene una estación activa a la vez (single global state).
  Hacer click en otra estación cambia la activa para todos los clientes —
  multi-tab real requiere refactor de state.
  <br>Accesible vía Tailscale <code>100.122.62.70:8080</code> o LAN <code>10.0.0.23:8080</code>.
</div>

</body>
</html>
"""


ANALYSIS_TMPL = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>Análisis · aseveraciones</title>
<style>
  :root {
    --bg:#0e1117; --panel:#161b22; --border:#30363d;
    --text:#c9d1d9; --muted:#8b949e; --accent:#58a6ff;
    --good:#3fb950; --bad:#f85149; --warn:#d29922;
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:18px; font:14px/1.4 system-ui,-apple-system,sans-serif;
         background:var(--bg); color:var(--text); }
  h1 { margin:0 0 4px 0; font-size:22px; }
  .sub { color:var(--muted); margin-bottom:18px; font-size:13px; }
  .tabs { display:flex; gap:4px; margin-bottom:14px; border-bottom:1px solid var(--border); }
  .tabs a { padding:8px 14px; color:var(--muted); text-decoration:none;
            border-bottom:2px solid transparent; font-size:13px; }
  .tabs a.active { color:var(--accent); border-bottom-color:var(--accent); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(360px, 1fr)); gap:12px; }
  .slot { background:var(--panel); border:1px solid var(--border);
          border-radius:8px; padding:12px; }
  .slot h3 { margin:0 0 8px 0; font-size:14px; color:var(--muted); }
  .empty { color:var(--muted); font-style:italic; font-size:13px; }
  form.add { display:grid; gap:6px; grid-template-columns:1fr 1fr;
             margin-top:8px; }
  form.add select, form.add input { background:#0d1117; color:var(--text);
    border:1px solid var(--border); padding:5px 7px; border-radius:4px; font-size:13px; }
  form.add button { grid-column:1/-1; padding:6px 10px; background:var(--accent); color:#0d1117;
    border:none; border-radius:4px; cursor:pointer; font-weight:600; font-size:13px; }
  .label { font-size:15px; font-weight:600; margin-bottom:6px; }
  .metrics { display:grid; grid-template-columns:1fr 1fr 1fr; gap:6px; margin-top:8px;
             padding-top:8px; border-top:1px solid #21262d; font-size:12px; }
  .metric { background:#0d1117; padding:6px 8px; border-radius:4px; }
  .metric .k { color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.5px; }
  .metric .v { font-size:14px; font-weight:600; }
  .diff-pos { color:var(--bad); }
  .diff-neg { color:var(--good); }
  .diff-flat { color:var(--muted); }
  .ts { color:var(--muted); font-size:11px; margin-top:6px; }
  .actions { display:flex; gap:6px; margin-top:8px; }
  .actions form { display:inline; }
  .actions button { padding:4px 10px; background:#21262d; color:var(--muted);
    border:1px solid var(--border); border-radius:4px; cursor:pointer; font-size:12px; }
  .footer { margin-top:18px; color:var(--muted); font-size:11px; }
  .stale { color:var(--warn); }
</style>
</head>
<body>
<h1>Análisis</h1>
<div class="sub">
  Aseveraciones vs modelo y Kalshi · {{ now }} ·
  poller {{ poller_status }} ·
  <a href="/analysis" style="color:var(--accent)">refresh</a>
</div>
<div class="tabs">
  <a href="/">inicio</a>
  <a href="/analysis" class="active">análisis</a>
</div>

<div class="grid">
{% for slot in slots %}
  <div class="slot">
    <h3>Aseveración #{{ slot.idx }}</h3>
    {% if slot.a %}
      <div class="label">
        {{ slot.a.station }} {{ slot.a.lo|int }}–{{ slot.a.hi|int }}°F
        <span style="color:var(--accent)">{{ slot.a.side }}</span>
        @ {{ slot.a.prob }}%
      </div>
      <div class="metrics">
        <div class="metric">
          <div class="k">tu prob</div>
          <div class="v">{{ slot.a.prob }}%</div>
        </div>
        <div class="metric">
          <div class="k">modelo</div>
          <div class="v">
            {% if slot.e.model_p is not none %}{{ "%.0f"|format(slot.e.model_p*100) }}%{% else %}—{% endif %}
          </div>
        </div>
        <div class="metric">
          <div class="k">kalshi</div>
          <div class="v">
            {% if slot.e.kalshi_p is not none %}{{ "%.0f"|format(slot.e.kalshi_p*100) }}%{% else %}—{% endif %}
          </div>
        </div>
        <div class="metric">
          <div class="k">diff vs modelo</div>
          <div class="v {{ slot.diff_class_m }}">
            {% if slot.e.diff_model is not none %}{{ "%+.0f"|format(slot.e.diff_model) }}pp{% else %}—{% endif %}
          </div>
        </div>
        <div class="metric">
          <div class="k">diff vs kalshi</div>
          <div class="v {{ slot.diff_class_k }}">
            {% if slot.e.diff_kalshi is not none %}{{ "%+.0f"|format(slot.e.diff_kalshi) }}pp{% else %}—{% endif %}
          </div>
        </div>
        <div class="metric">
          <div class="k">obs / med</div>
          <div class="v" style="font-size:12px">
            {% if slot.e.current_f is not none %}{{ "%.0f"|format(slot.e.current_f) }}°{% else %}—{% endif %}
            /
            {% if slot.e.ens_med is not none %}{{ "%.0f"|format(slot.e.ens_med) }}°{% else %}—{% endif %}
          </div>
        </div>
      </div>
      <div class="ts {{ slot.stale_class }}">snapshot: {{ slot.snap_age }}</div>
      <div class="actions">
        <form method="POST" action="/analysis/clear">
          <input type="hidden" name="slot" value="{{ slot.idx }}">
          <button type="submit">borrar</button>
        </form>
      </div>
    {% else %}
      <div class="empty">vacío</div>
      <form method="POST" action="/analysis/set" class="add">
        <input type="hidden" name="slot" value="{{ slot.idx }}">
        <select name="station" required>
          {% for sid, name in stations %}
          <option value="{{ sid }}">{{ sid }} — {{ name }}</option>
          {% endfor %}
        </select>
        <select name="side" required>
          <option value="YES">YES</option>
          <option value="NO">NO</option>
        </select>
        <input type="number" name="lo" min="0" max="130" step="1"
               placeholder="lo °F" required>
        <input type="number" name="hi" min="0" max="130" step="1"
               placeholder="hi °F" required>
        <input type="number" name="prob" min="1" max="99" step="1"
               placeholder="tu %" required style="grid-column:1/-1">
        <button type="submit">guardar</button>
      </form>
    {% endif %}
  </div>
{% endfor %}
</div>

<div class="footer">
  Refresh auto cada 10 min · diff &gt; 0 = sobrestimas (rojo) · diff &lt; 0 = subestimas (verde)<br>
  Poller corre en background; si "stale" es viejo, revisar logs de analysis_poller.
</div>
</body>
</html>
"""


@app.route("/")
def home():
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M AST")
    host = request.host.split(":")[0]
    btc_base = f"http://{host}:8001"
    wx_base = f"http://{host}:8000"
    return render_template_string(
        TMPL, host=host, now=now, btc_base=btc_base, wx_base=wx_base,
        cryptos=CRYPTO_SYMBOLS, stations=WEATHER_STATIONS,
    )


@app.route("/analysis")
def analysis():
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M AST")
    assertions = _load_assertions()
    c = _conn()

    poller_status = "sin datos"
    if c is not None:
        cur = c.execute("SELECT MAX(ts) FROM station_snapshots")
        latest = cur.fetchone()[0]
        if latest:
            try:
                t = datetime.fromisoformat(latest)
                age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60
                poller_status = f"último snap hace {age_min:.0f} min"
            except Exception:
                poller_status = "último snap: " + latest

    slots = []
    for i in range(1, N_SLOTS + 1):
        a = assertions.get(str(i))
        slot = {"idx": i, "a": a, "diff_class_m": "diff-flat",
                "diff_class_k": "diff-flat"}
        if a and c is not None:
            e = _eval_assertion(c, a)
            dm = e["diff_model"]
            if dm is not None and abs(dm) >= 3:
                slot["diff_class_m"] = "diff-pos" if dm > 0 else "diff-neg"
            dk = e["diff_kalshi"]
            if dk is not None and abs(dk) >= 3:
                slot["diff_class_k"] = "diff-pos" if dk > 0 else "diff-neg"
            slot["e"] = e
            if e["snap_ts"]:
                try:
                    t = datetime.fromisoformat(e["snap_ts"])
                    age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60
                    slot["snap_age"] = f"hace {age_min:.0f} min"
                    slot["stale_class"] = "stale" if age_min > 30 else ""
                except Exception:
                    slot["snap_age"] = e["snap_ts"]
                    slot["stale_class"] = ""
            else:
                slot["snap_age"] = "sin snapshot aún"
                slot["stale_class"] = "stale"
        else:
            slot["e"] = {}
            slot["snap_age"] = ""
            slot["stale_class"] = ""
        slots.append(slot)

    if c:
        c.close()

    return render_template_string(
        ANALYSIS_TMPL, now=now, slots=slots, stations=WEATHER_STATIONS,
        poller_status=poller_status,
    )


@app.route("/analysis/set", methods=["POST"])
def analysis_set():
    slot = request.form.get("slot", "").strip()
    if slot not in {str(i) for i in range(1, N_SLOTS + 1)}:
        return "slot inválido", 400
    try:
        lo = int(request.form["lo"])
        hi = int(request.form["hi"])
        prob = int(request.form["prob"])
    except (KeyError, ValueError):
        return "valores inválidos", 400
    if lo > hi:
        lo, hi = hi, lo
    if not (1 <= prob <= 99):
        return "prob fuera de rango", 400
    station = request.form.get("station", "").strip().upper()
    side = request.form.get("side", "").strip().upper()
    if station not in {s for s, _ in WEATHER_STATIONS}:
        return "estación inválida", 400
    if side not in {"YES", "NO"}:
        return "side inválido", 400

    d = _load_assertions()
    d[slot] = {
        "station": station, "side": side,
        "lo": lo, "hi": hi, "prob": prob,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_assertions(d)
    return redirect(url_for("analysis"))


@app.route("/analysis/clear", methods=["POST"])
def analysis_clear():
    slot = request.form.get("slot", "").strip()
    d = _load_assertions()
    if slot in d:
        del d[slot]
        _save_assertions(d)
    return redirect(url_for("analysis"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
