"""Landing page dashboard en :8080 — links a BTC predictor (:8001) y weather (:8000).

Sin estado, sin polling. Sólo HTML estático servido por Flask para tener un
único punto de entrada cuando entras desde otra PC / cel (Tailscale o LAN).
"""
from __future__ import annotations

from flask import Flask, request, render_template_string

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


@app.route("/")
def home():
    from datetime import datetime, timezone, timedelta
    pr_tz = timezone(timedelta(hours=-4), name="AST")
    now = datetime.now(pr_tz).strftime("%Y-%m-%d %H:%M AST")
    host = request.host.split(":")[0]
    # Use same host (so clicks work from Tailscale/LAN/localhost).
    btc_base = f"http://{host}:8001"
    wx_base = f"http://{host}:8000"
    return render_template_string(
        TMPL, host=host, now=now, btc_base=btc_base, wx_base=wx_base,
        cryptos=CRYPTO_SYMBOLS, stations=WEATHER_STATIONS,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
