#!/usr/bin/env python3
"""Flask web UI for the weather predictor — browse from iPad/phone on same WiFi.

Run with the venv python:

    ./venv/bin/python3 predictor_web.py [STATION_ID] [PORT]
"""
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, redirect, render_template_string, request

from predictor import (
    POLL_SEC, PR_TZ, PEAK_HOURS,
    Assertion, State,
    fetch_station, build_snapshot, refresh_auto, eval_assertion,
    find_informative_bin, most_likely_max, movement_cents, parse_expr, log_snapshot,
    record_kalshi, invalidate_obs_cache,
)
try:
    import calibration as _calibration
except Exception:
    _calibration = None
try:
    import kalshi as _kalshi
except Exception:
    _kalshi = None
try:
    import peak_timing as _peak_timing
except Exception:
    _peak_timing = None
try:
    import difficulty as _difficulty
except Exception:
    _difficulty = None
try:
    import weather_alerts as _weather_alerts
except Exception:
    _weather_alerts = None
try:
    import external_models as _external_models
except Exception:
    _external_models = None


def build_day_chart_svg(day_chart, current_hour: int) -> str:
    """Inline SVG: observed line (verde, gruesa, con puntos) + ensemble
    envelope p10-p90 (banda azul) + median (línea fina punteada). Marcador
    'ahora' resaltado, eje Y con °F, eje X con horas clave."""
    all_temps = [v for h, obs, med, p10, p90 in day_chart
                 for v in (obs, med, p10, p90) if v is not None]
    if not all_temps:
        return "<p style='color:#a6adc8'>sin datos del día aún</p>"
    lo, hi = min(all_temps) - 1, max(all_temps) + 1
    if hi - lo < 5:
        mid = (hi + lo) / 2
        lo, hi = mid - 3, mid + 3

    W, H = 640, 240
    pad_l, pad_r, pad_t, pad_b = 40, 14, 28, 32
    iw, ih = W - pad_l - pad_r, H - pad_t - pad_b

    def xpos(h): return pad_l + h / 23 * iw
    def ypos(t): return pad_t + (hi - t) / (hi - lo) * ih

    # Bandas de noche (00-06, 19-24) y día (06-19) — pintan el contexto
    # temporal de un vistazo. Opacidad baja para no competir con la banda
    # p10-p90 del ensemble.
    night_color = "rgba(49, 50, 68, 0.55)"
    day_color = "rgba(249, 226, 175, 0.045)"
    bands = (
        f'<rect x="{xpos(0):.1f}" y="{pad_t}" '
        f'width="{xpos(6)-xpos(0):.1f}" height="{ih:.1f}" fill="{night_color}"/>'
        f'<rect x="{xpos(6):.1f}" y="{pad_t}" '
        f'width="{xpos(19)-xpos(6):.1f}" height="{ih:.1f}" fill="{day_color}"/>'
        f'<rect x="{xpos(19):.1f}" y="{pad_t}" '
        f'width="{xpos(23)-xpos(19):.1f}" height="{ih:.1f}" fill="{night_color}"/>'
    )

    # shaded envelope polygon (top: p90 forward, bottom: p10 reversed)
    top = [(xpos(h), ypos(p90)) for h, _, _, _, p90 in day_chart if p90 is not None]
    bot = [(xpos(h), ypos(p10)) for h, _, _, p10, _ in day_chart if p10 is not None]
    env = ""
    if top and bot:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in top)
        pts += " " + " ".join(f"{x:.1f},{y:.1f}" for x, y in reversed(bot))
        env = (f'<polygon points="{pts}" fill="rgba(137,180,250,0.18)" '
               f'stroke="rgba(137,180,250,0.35)" stroke-width="0.5"/>')

    med_pts = " ".join(f"{xpos(h):.1f},{ypos(med):.1f}"
                       for h, _, med, _, _ in day_chart if med is not None)

    # observed: line + dots so cada hora con dato es visible
    obs_data = [(h, obs) for h, obs, _, _, _ in day_chart if obs is not None]
    obs_pts = " ".join(f"{xpos(h):.1f},{ypos(obs):.1f}" for h, obs in obs_data)
    obs_dots = "".join(
        f'<circle cx="{xpos(h):.1f}" cy="{ypos(obs):.1f}" r="2.5" '
        f'fill="#a6e3a1" stroke="#0a0e14" stroke-width="0.8"/>'
        for h, obs in obs_data)

    # y-axis: rayitas cada 1°F (sin label) + label cada 5°F con grid line
    y_lines = []
    for t in range(int(lo), int(hi) + 1):
        if t < lo or t > hi:
            continue
        if t % 5 == 0:
            y_lines.append(f'<line x1="{pad_l}" y1="{ypos(t):.1f}" x2="{W-pad_r}" '
                           f'y2="{ypos(t):.1f}" stroke="#2a2e42" '
                           f'stroke-dasharray="2,3"/>')
            y_lines.append(f'<text x="{pad_l-6}" y="{ypos(t)+3:.0f}" font-size="10" '
                           f'fill="#a6adc8" text-anchor="end">{t}°F</text>')
        else:
            y_lines.append(f'<line x1="{pad_l-3}" y1="{ypos(t):.1f}" '
                           f'x2="{pad_l}" y2="{ypos(t):.1f}" '
                           f'stroke="#a6adc8" opacity="0.5"/>')

    # x-axis: tick + label en cada hora 00-23, énfasis en 06/12/18
    x_lines = []
    for h in range(0, 24):
        emphasized = h in (6, 12, 18)
        color = "#cdd6f4" if emphasized else "#a6adc8"
        weight = "600" if emphasized else "400"
        size = "10" if emphasized else "9"
        x_lines.append(f'<line x1="{xpos(h):.1f}" y1="{H-pad_b}" '
                       f'x2="{xpos(h):.1f}" y2="{H-pad_b+3}" '
                       f'stroke="{color}" opacity="0.7"/>')
        x_lines.append(f'<text x="{xpos(h):.1f}" y="{H-pad_b+15}" font-size="{size}" '
                       f'fill="{color}" font-weight="{weight}" '
                       f'text-anchor="middle">{h:02d}</text>')

    # "ahora" line + label más prominente
    nx = xpos(current_hour)
    now_line = (
        f'<line x1="{nx:.1f}" y1="{pad_t}" x2="{nx:.1f}" y2="{H-pad_b}" '
        f'stroke="#f9e2af" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.85"/>'
        f'<rect x="{nx-22:.1f}" y="{pad_t-18}" width="44" height="14" rx="3" '
        f'fill="#f9e2af"/>'
        f'<text x="{nx:.1f}" y="{pad_t-8}" font-size="10" font-weight="600" '
        f'fill="#0a0e14" text-anchor="middle">ahora</text>'
    )

    med_line = (f'<polyline points="{med_pts}" stroke="#89b4fa" stroke-width="1" '
                f'fill="none" stroke-dasharray="3,3" opacity="0.75"/>') if med_pts else ""
    obs_line = (f'<polyline points="{obs_pts}" stroke="#a6e3a1" stroke-width="2.8" '
                f'fill="none" stroke-linejoin="round"/>') if obs_pts else ""

    # legend en la parte superior izquierda, fuera del área del gráfico
    legend = (
        f'<g font-size="10" fill="#cdd6f4">'
        f'<line x1="{pad_l}" y1="{pad_t-12}" x2="{pad_l+14}" y2="{pad_t-12}" '
        f'stroke="#a6e3a1" stroke-width="2.8"/>'
        f'<circle cx="{pad_l+7}" cy="{pad_t-12}" r="2.5" fill="#a6e3a1" '
        f'stroke="#0a0e14" stroke-width="0.8"/>'
        f'<text x="{pad_l+18}" y="{pad_t-9}">observado</text>'
        f'<rect x="{pad_l+90}" y="{pad_t-16}" width="14" height="8" '
        f'fill="rgba(137,180,250,0.3)" stroke="rgba(137,180,250,0.5)" stroke-width="0.5"/>'
        f'<text x="{pad_l+108}" y="{pad_t-9}">p10-p90 (ensemble)</text>'
        f'<line x1="{pad_l+220}" y1="{pad_t-12}" x2="{pad_l+234}" y2="{pad_t-12}" '
        f'stroke="#89b4fa" stroke-width="1" stroke-dasharray="3,3"/>'
        f'<text x="{pad_l+238}" y="{pad_t-9}">mediana</text>'
        f'</g>')

    return (f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block">'
            + bands + legend + "".join(y_lines) + "".join(x_lines) + env
            + med_line + now_line + obs_line + obs_dots + '</svg>')


def build_top_max_bars(ensemble, top_n: int = 7):
    """Top-N temperaturas máximas más probables (redondeadas a entero) con su
    probabilidad. Devuelve lista ordenada de menor a mayor temperatura para
    facilitar lectura tipo histograma; el modal se distingue por bar_pct=100."""
    if ensemble is None or len(ensemble) == 0:
        return []
    from collections import Counter
    rounded = [int(round(float(v))) for v in ensemble]
    n = len(rounded)
    counts = Counter(rounded)
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]
    if not top:
        return []
    max_count = max(c for _, c in top)
    top.sort(key=lambda kv: kv[0])
    modal_deg = max(top, key=lambda kv: kv[1])[0]
    return [
        {
            "deg": deg,
            "p_pct": c / n * 100,
            "bar_pct": c / max_count * 100,
            "is_modal": deg == modal_deg,
        }
        for deg, c in top
    ]

app = Flask(__name__)
state_lock = threading.Lock()
state: State | None = None
_last_ts = [None]


HTML = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>{{ station.id }} — Weather</title>
<style>
  :root {
    --bg:#0a0e14; --surface:#1e2030; --surface2:#2a2e42;
    --text:#cdd6f4; --muted:#a6adc8; --accent:#89b4fa;
    --yellow:#f9e2af; --green:#a6e3a1; --red:#f38ba8; --cyan:#89dceb;
  }
  * { box-sizing: border-box; }
  body { font-family:-apple-system,system-ui,sans-serif; background:var(--bg);
         color:var(--text); margin:0; padding:max(1rem,env(safe-area-inset-top)) 1rem 1rem;
         -webkit-text-size-adjust:100%; }
  .container { max-width: 900px; margin: 0 auto; }
  .header { display:flex; justify-content:space-between; align-items:baseline;
            margin-bottom:1rem; flex-wrap:wrap; gap:0.5rem; }
  .station-name { font-size:1.2rem; color:var(--cyan); font-weight:600; }
  .clock, .age { color: var(--muted); font-size: 0.85rem; }
  .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem; }
  .card { background: var(--surface); padding: 1rem; border-radius: 14px;
          border: 1px solid var(--surface2); }
  .card h3 { margin: 0 0 0.5rem; font-size: 0.75rem; color: var(--muted);
             text-transform: uppercase; letter-spacing: 0.05em; }
  .temp-big { font-size: 2.8rem; font-weight: 200; color: var(--yellow); line-height: 1; }
  .desc { color: var(--muted); margin-top: 0.25rem; margin-bottom: 0.5rem; }
  .feels { color: var(--accent); font-size: 0.9rem; margin-left: 0.5rem; }
  .kv { display: flex; justify-content: space-between; padding: 0.35rem 0;
        border-bottom: 1px solid var(--surface2); }
  .kv:last-child { border-bottom: none; }
  .kv-k { color: var(--muted); }
  .assertion { display: grid; grid-template-columns: 24px 1fr 70px 70px 110px;
               gap: 0.4rem; align-items: center; padding: 0.6rem 0;
               border-bottom: 1px solid var(--surface2); font-size: 0.95rem; }
  .assertion:last-child { border-bottom: none; }
  .cents { text-align: right; font-variant-numeric: tabular-nums; font-weight: 700;
           font-size: 1.1rem; }
  .mv-up { color: var(--green); text-align: right; font-size: 0.85rem; }
  .mv-down { color: var(--red); text-align: right; font-size: 0.85rem; }
  .mv-flat { color: var(--muted); text-align: right; font-size: 0.85rem; }
  .stat { text-align: right; font-size: 0.85rem; }
  .live { color: var(--cyan); }
  .resuelta { color: var(--green); font-weight: 600; }
  .fallida { color: var(--red); }
  .peak-green { color: var(--green); }
  .peak-yellow { color: var(--yellow); }
  .peak-cyan { color: var(--cyan); }
  .diff-badge { padding: .65rem .8rem; border-radius: 10px; margin-bottom: .9rem;
                border: 1px solid var(--surface2); font-size: .92rem; }
  .diff-label { font-weight: 600; font-size: .85rem; }
  .diff-reasons { color: var(--muted); font-size: .8rem; margin-top: .25rem; }
  .diff-skip { color: var(--red); font-weight: 600; font-size: .82rem; margin-top: .35rem; }
  .diff-easy  { border-color: #2e4e3a; background: rgba(166,227,161,0.08); }
  .diff-easy strong, .diff-easy .diff-label { color: var(--green); }
  .diff-normal { border-color: #2e3e4e; background: rgba(137,180,250,0.08); }
  .diff-normal strong, .diff-normal .diff-label { color: var(--cyan); }
  .diff-hard  { border-color: #5e4a2a; background: rgba(250,179,135,0.10); }
  .diff-hard strong, .diff-hard .diff-label { color: var(--yellow); }
  .diff-veryhard { border-color: #5e2e3a; background: rgba(243,139,168,0.12); }
  .diff-veryhard strong, .diff-veryhard .diff-label { color: var(--red); }
  .hero { background: var(--surface); border: 1px solid var(--surface2);
          border-radius: 14px; padding: 1rem 1.1rem; margin-bottom: 1rem;
          display: flex; flex-direction: column; gap: .3rem; }
  .hero-label { font-size: .72rem; color: var(--muted); text-transform: uppercase;
                letter-spacing: .06em; }
  .hero-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: .7rem; }
  .hero-val { font-size: 3.2rem; font-weight: 300; line-height: 1;
              font-variant-numeric: tabular-nums; }
  .hero-unit { font-size: 1.4rem; color: var(--muted); margin-left: -0.35rem; }
  .hero-trend { font-size: .95rem; font-variant-numeric: tabular-nums;
                padding: .15rem .5rem; border-radius: 8px;
                background: var(--surface2); }
  .hero-trend-up { color: var(--red); }
  .hero-trend-down { color: var(--cyan); }
  .hero-trend-flat { color: var(--muted); }
  .hero-conf { font-size: .85rem; color: var(--muted); }
  .hero-conf-badge { display: inline-block; padding: .1rem .45rem;
                     border-radius: 6px; font-size: .72rem; font-weight: 600;
                     margin-left: .3rem; letter-spacing: .04em; text-transform: uppercase; }
  .conf-high { background: rgba(166,227,161,0.15); color: var(--green); }
  .conf-mid  { background: rgba(249,226,175,0.15); color: var(--yellow); }
  .conf-low  { background: rgba(243,139,168,0.15); color: var(--red); }
  .hero-hint { color: var(--muted); font-size: .78rem; margin-top: .15rem; }
  .hero .val-color-hot { color: var(--red); }
  .hero .val-color-warm { color: var(--yellow); }
  .hero .val-color-cool { color: var(--cyan); }
  form { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
  input, select, button { font-size: 1rem; padding: 0.65rem;
                          border-radius: 10px; border: 1px solid var(--surface2);
                          background: var(--surface); color: var(--text); }
  button { background: var(--accent); color: var(--bg); border: none;
           font-weight: 600; min-width: 70px; }
  button:active { opacity: 0.7; }
  button.danger { background: var(--red); }
  .forecast-hour { display: grid; grid-template-columns: 60px 60px 1fr;
                   padding: 0.3rem 0; font-size: 0.9rem; border-bottom: 1px solid var(--surface2); }
  .forecast-hour:last-child { border: none; }
  .spark { font-family: ui-monospace, monospace; color: var(--muted); letter-spacing: -1px; }
  .topmax-row { display: grid; grid-template-columns: 56px 1fr 56px;
                gap: .5rem; align-items: center; padding: .25rem 0;
                font-size: .9rem; }
  .topmax-deg { font-variant-numeric: tabular-nums; font-weight: 600;
                color: var(--text); text-align: right; }
  .topmax-deg.modal { color: var(--yellow); }
  .topmax-bar-wrap { background: var(--surface2); height: 14px;
                     border-radius: 7px; overflow: hidden; position: relative; }
  .topmax-bar { height: 100%; background: linear-gradient(90deg,
                rgba(137,180,250,0.8), rgba(137,180,250,0.5));
                border-radius: 7px; }
  .topmax-bar.modal { background: linear-gradient(90deg,
                      rgba(249,226,175,0.95), rgba(249,226,175,0.6)); }
  .topmax-pct { font-variant-numeric: tabular-nums; color: var(--muted);
                font-size: .85rem; text-align: right; }
  .topmax-hint { font-size: .75rem; color: var(--muted); margin-top: .5rem; }
  .ext-narr { color: var(--text); font-size: .92rem; line-height: 1.4;
              padding: .5rem .7rem; background: var(--surface2);
              border-radius: 8px; margin-bottom: .7rem; }
  .ext-models { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
                gap: .4rem; }
  .ext-model { background: var(--surface2); border-radius: 8px;
               padding: .45rem .55rem; display: flex; flex-direction: column;
               gap: .15rem; }
  .ext-model-label { font-size: .7rem; color: var(--muted);
                     text-transform: uppercase; letter-spacing: .05em; }
  .ext-model-val { font-size: 1.15rem; font-weight: 600;
                   font-variant-numeric: tabular-nums; }
  .ext-model-val.ours { color: var(--yellow); }
  .ext-model-val.muted { color: var(--muted); font-weight: 400; }
  .ext-summary { font-size: .8rem; color: var(--muted); margin-top: .55rem;
                 display: flex; justify-content: space-between; gap: .5rem;
                 flex-wrap: wrap; }
  .ext-delta-warn { color: var(--yellow); font-weight: 600; }
  .ext-delta-ok { color: var(--green); }
  @media (max-width: 600px) { .cards { grid-template-columns: 1fr; } }
  .signals { display: flex; flex-wrap: wrap; gap: .4rem; margin: -.5rem 0 1rem; }
  .signal-pill { display: inline-flex; align-items: center; gap: .35rem;
                 padding: .3rem .65rem; border-radius: 999px; font-size: .8rem;
                 background: var(--surface); border: 1px solid var(--surface2); }
  .signal-pill .k { color: var(--muted); font-size: .68rem; text-transform: uppercase;
                    letter-spacing: .05em; }
  .signal-pill .v { font-weight: 600; }
  .signal-pill.ok    { border-color: rgba(166,227,161,.45); }
  .signal-pill.ok    .v { color: var(--green); }
  .signal-pill.warn  { border-color: rgba(249,226,175,.45); }
  .signal-pill.warn  .v { color: var(--yellow); }
  .signal-pill.alert { border-color: rgba(243,139,168,.55); background: rgba(243,139,168,0.08); }
  .signal-pill.alert .v { color: var(--red); }
  details.tools { background: var(--surface); border: 1px solid var(--surface2);
                  border-radius: 12px; padding: .55rem .9rem; margin-top: 1rem; }
  details.tools > summary { cursor: pointer; font-weight: 600; color: var(--muted);
                            list-style: none; }
  details.tools[open] > summary { color: var(--text); margin-bottom: .4rem; }
  details.tools > summary::after { content: " ▸"; color: var(--muted); }
  details.tools[open] > summary::after { content: " ▾"; }
  .clock-wrap { background: #1e2030; border-radius: 12px; padding: .7rem .9rem;
                margin-top: 1rem; }
  .clock-title { font-weight: 600; color: var(--muted); font-size: 12px;
                 margin-bottom: .45rem; letter-spacing: .03em; text-transform: uppercase; }
  .clock-bar { position: relative; height: 24px; background: #313244;
               border-radius: 6px; overflow: visible; }
  .clock-bar > .zone { position: absolute; top: 0; bottom: 0; }
  .zone-pre  { background: rgba(108,112,134,.25); border-radius: 6px 0 0 6px; }
  .zone-conf { background: rgba(166,227,161,.28); }
  .zone-dec  { background: rgba(249,226,175,.32); }
  .zone-post { background: rgba(108,112,134,.25); border-radius: 0 6px 6px 0; }
  .modal-mark { position: absolute; top: -3px; bottom: -3px; width: 4px;
                background: #f38ba8; transform: translateX(-50%); border-radius: 2px;
                box-shadow: 0 0 6px rgba(243,139,168,.6); }
  .now-mark { position: absolute; top: -4px; bottom: -4px; width: 2px;
              background: #cdd6f4; transform: translateX(-50%);
              box-shadow: 0 0 0 1px #0a0e14; }
  .now-mark::after { content: "▼"; position: absolute; top: -11px; left: 50%;
                     transform: translateX(-50%); font-size: 10px; color: #cdd6f4; }
  .clock-axis { display: flex; justify-content: space-between; color: var(--muted);
                font-size: 10px; margin-top: .3rem; padding: 0 1px; }
  .clock-legend { display: flex; flex-wrap: wrap; gap: .7rem .9rem; margin-top: .5rem;
                  color: var(--muted); font-size: 11px; }
  .clock-legend i.dot { display: inline-block; width: 10px; height: 10px;
                        border-radius: 2px; vertical-align: middle; margin-right: .3rem; }
  .clock-legend i.pre  { background: rgba(108,112,134,.6); }
  .clock-legend i.conf { background: rgba(166,227,161,.6); }
  .clock-legend i.dec  { background: rgba(249,226,175,.7); }
  .clock-legend i.peak { background: #f38ba8; }
  .clock-now-text { margin-top: .5rem; color: var(--text); font-size: 13px; }
  .clock-now-text b { color: #fab387; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <div class="station-name">{{ station.id }} — {{ station.name }}</div>
      <div class="clock">local {{ local_time }} · PR {{ pr_time }}</div>
    </div>
    <div style="display:flex;align-items:center;gap:.8rem">
      <form method="post" action="/api/station" style="margin:0">
        <select name="id" onchange="this.form.submit()"
                style="background:#181825;color:#cdd6f4;border:1px solid #313244;
                       border-radius:4px;padding:.3rem .5rem;font-size:.9rem">
          {% for sid, sname in station_options %}
            <option value="{{sid}}" {% if sid == station.id %}selected{% endif %}>{{sid}} — {{sname}}</option>
          {% endfor %}
        </select>
      </form>
      <div class="age">actualizado hace <span id="age">0</span>s</div>
    </div>
  </div>

  <div class="hero">
    <div class="hero-label">Máxima esperada hoy · se ajusta cada poll</div>
    <div class="hero-row">
      <span class="hero-val {{ hero.val_color }}">{{ hero.value }}<span class="hero-unit">°F</span></span>
      <span class="hero-trend {{ hero.trend_class }}" title="cambio vs snapshot anterior">{{ hero.trend_str }}</span>
    </div>
    <div class="hero-conf">
      {{ hero.conf_str }}
      <span class="hero-conf-badge {{ hero.conf_class }}">{{ hero.conf_label }}</span>
    </div>
    {% if hero.hint %}
    <div class="hero-hint">{{ hero.hint }}</div>
    {% endif %}
  </div>

  {% if signals %}
  <div class="signals">
    {% for s in signals %}
    {% if s.href %}<a href="{{s.href}}" style="text-decoration:none">{% endif %}
    <span class="signal-pill {{s.kls}}">
      <span class="k">{{s.k}}</span>
      <span class="v">{{s.v}}</span>
    </span>
    {% if s.href %}</a>{% endif %}
    {% endfor %}
  </div>
  {% endif %}

  <div class="card" style="margin-bottom:1rem;padding:.6rem .9rem">
    <div style="display:flex;flex-wrap:wrap;gap:1rem;align-items:center;font-size:.9rem">
      <a href="/status" style="text-decoration:none">
        <span class="badge {{dash.health_class}}" style="display:inline-block;padding:.15rem .5rem;border-radius:4px;font-weight:600;font-size:12px;background:{% if dash.health_class=='ok' %}#2a4a32;color:#a6e3a1{% elif dash.health_class=='warn' %}#4a3a24;color:#f9e2af{% else %}#4a2a32;color:#f38ba8{% endif %}">{{dash.health_label}}</span>
        <span style="color:var(--muted);margin-left:.3rem">{{dash.health_age}}</span>
      </a>
      <span style="color:var(--muted)">·</span>
      <a href="/bets" style="color:inherit;text-decoration:none">
        <span style="color:var(--muted)">P&amp;L</span>
        <span style="color:{% if dash.pnl > 0 %}#a6e3a1{% elif dash.pnl < 0 %}#f38ba8{% else %}#cdd6f4{% endif %};font-weight:600">${{ '%.2f'|format(dash.pnl) }}</span>
        <span style="color:var(--muted);font-size:.8rem">({{dash.bets_settled}}/{{dash.bets_total}}{% if dash.roi is not none %} · ROI {{'%+.1f'|format(dash.roi*100)}}%{% endif %})</span>
      </a>
      {% if dash.brier_n %}
      <span style="color:var(--muted)">·</span>
      <a href="/history" style="color:inherit;text-decoration:none">
        <span style="color:var(--muted)">Brier {{dash.brier_n}}d</span>
        <span style="font-weight:600;color:{% if dash.brier_ours < dash.brier_kalshi %}#a6e3a1{% elif dash.brier_ours > dash.brier_kalshi %}#f38ba8{% else %}#cdd6f4{% endif %}">{{'%.4f'|format(dash.brier_ours)}}</span>
        <span style="color:var(--muted);font-size:.8rem">vs K {{'%.4f'|format(dash.brier_kalshi)}}</span>
      </a>
      {% endif %}
      {% if dash.iso_days %}
      <span style="color:var(--muted)">·</span>
      <span style="color:var(--muted);font-size:.8rem">iso {{dash.iso_days}}/7d</span>
      {% endif %}
    </div>
  </div>

  {% if market or timing or precip %}
  <div class="card" style="margin-bottom:1rem;border-left:4px solid {% if market and market.top_alert %}#f38ba8{% else %}#89b4fa{% endif %}">
    <div style="display:flex;flex-wrap:wrap;gap:1.5rem;align-items:flex-start">
      {% if market %}
      <div style="flex:1;min-width:220px">
        <h3 style="margin:0 0 .3rem">Mercado {{market_name}}</h3>
        <div class="kv"><span class="kv-k">Bin modal</span>
          <span>{{market.modal_label}} @ {{'%.0f'|format(market.modal_mid*100)}}%</span></div>
        <div class="kv"><span class="kv-k">Nuestro P (modal)</span>
          <span>{{'%.0f'|format(market.modal_ourp*100)}}%</span></div>
        <div class="kv"><span class="kv-k">Edge máx</span>
          <span style="color:{% if market.top_alert %}{% if market.top_edge > 0 %}#a6e3a1{% else %}#f38ba8{% endif %}{% else %}#cdd6f4{% endif %}">
            {{market.top_label}}: {{'%+.1f'|format(market.top_edge*100)}}pp
            {% if market.top_alert %}{% if market.top_edge > 0 %}· buy YES{% else %}· buy NO{% endif %}{% endif %}
          </span></div>
      </div>
      {% endif %}
      {% if timing %}
      <div style="flex:1;min-width:220px">
        <h3 style="margin:0 0 .3rem">Peak timing</h3>
        <div class="kv"><span class="kv-k">Hora modal</span>
          <span>{{'%02d'|format(timing.modal_hour)}}:00</span></div>
        <div class="kv"><span class="kv-k">p10 – p90</span>
          <span>{{'%02d'|format(timing.p10)}}:00 – {{'%02d'|format(timing.p90)}}:00</span></div>
        <div class="kv"><span class="kv-k">P(ya ocurrió)</span>
          <span>{{'%.0f'|format(timing.prob_already*100)}}%</span></div>
      </div>
      {% endif %}
      {% if precip %}
      <div style="flex:1;min-width:220px">
        <h3 style="margin:0 0 .3rem"><a href="/precip" style="color:#89dceb;text-decoration:none">Precipitación →</a></h3>
        <div class="kv"><span class="kv-k">P(any)</span>
          <span>{{'%.0f'|format(precip.p_any*100)}}%</span></div>
        <div class="kv"><span class="kv-k">P(&gt;0.1in)</span>
          <span>{{'%.0f'|format(precip.p_notable*100)}}%</span></div>
        <div class="kv"><span class="kv-k">Esperado hoy</span>
          <span>{{'%.2f'|format(precip.expected_mm)}} mm{% if precip.p_any_snow and precip.p_any_snow > 0.05 %} · nieve {{'%.0f'|format(precip.p_any_snow*100)}}%{% endif %}</span></div>
      </div>
      {% endif %}
    </div>
  </div>
  {% endif %}

  <div class="cards">
    <div class="card">
      <h3>Ahora</h3>
      <div>
        <span class="temp-big">{{ '%.1f' % snap.current_temp_f }}°F</span>
        {% if feels_line %}<span class="feels">{{ feels_line }}</span>{% endif %}
      </div>
      <div class="desc">{{ snap.current_desc }}</div>
      {% if snap.humidity_pct is not none %}
      <div class="kv"><span class="kv-k">Humedad</span>
        <span>{{ '%.0f' % snap.humidity_pct }}%{% if snap.dewpoint_f is not none %} · dp {{ '%.0f' % snap.dewpoint_f }}°F{% endif %}</span></div>
      {% endif %}
      {% if snap.wind_mph is not none %}
      <div class="kv"><span class="kv-k">Viento</span>
        <span>{{ '%.0f' % snap.wind_mph }} mph {{ snap.wind_dir_card or '' }}{% if snap.wind_gust_mph %} · gust {{ '%.0f' % snap.wind_gust_mph }}{% endif %}</span></div>
      {% endif %}
      {% if snap.pressure_inhg is not none %}
      <div class="kv"><span class="kv-k">Presión</span>
        <span>{{ '%.2f' % snap.pressure_inhg }} inHg {{ pressure_arrow }}</span></div>
      {% endif %}
      {% if snap.visibility_mi is not none %}
      <div class="kv"><span class="kv-k">Visibilidad</span><span>{{ '%.0f' % snap.visibility_mi }} mi</span></div>
      {% endif %}
    </div>

    <div class="card">
      {% if difficulty %}
      <div class="diff-badge diff-{{ difficulty.klass }}">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <strong>Dificultad del día</strong>
          <span class="diff-label">{{ difficulty.label }} · {{ '%.0f' % difficulty.score }}/100</span>
        </div>
        {% if difficulty.reasons %}
        <div class="diff-reasons">{{ difficulty.reasons|join(' · ') }}</div>
        {% endif %}
        {% if difficulty.recommend_skip %}
        <div class="diff-skip">⚠ considera saltar hoy — alta incertidumbre</div>
        {% endif %}
      </div>
      {% endif %}
      <h3>Hoy</h3>
      <div class="kv"><span class="kv-k">Max obs</span><span>{{ '%.1f' % snap.today_max_obs }}°F</span></div>
      <div class="kv"><span class="kv-k">Min obs</span><span>{{ '%.1f' % snap.today_min_obs }}°F</span></div>
      <div class="kv"><span class="kv-k">Pico</span>
        <span class="{{ peak_class }}">{{ snap.peak_status }}</span></div>
      <div class="kv"><span class="kv-k">P(sube más)</span>
        <span>{{ '%.0f' % (snap.prob_rising * 100) }}%</span></div>
      <h3 style="margin-top:1rem">Distribución Max (ensemble 31m)</h3>
      <div class="kv"><span class="kv-k">Mediana</span><span>{{ '%.1f' % dist_med }}°F</span></div>
      <div class="kv"><span class="kv-k">p10–p90</span><span>{{ '%.1f' % dist_p10 }}–{{ '%.1f' % dist_p90 }}°F</span></div>
      <div class="kv"><span class="kv-k">Más probable</span><span>{{ ml_display }}</span></div>
      {% if snap.ensemble_residual_hours > 0 and snap.ensemble_eff_n %}
      <div class="kv"><span class="kv-k">Reweight bayesiano</span>
        <span>eff N = {{ '%.1f' % snap.ensemble_eff_n }} / 31 · {{ snap.ensemble_residual_hours }}h obs</span></div>
      {% endif %}
      {% if climate %}
      <h3 style="margin-top:1rem">Climatología <span style="font-weight:400">{{ climate.year_span }}</span></h3>
      <div class="kv"><span class="kv-k">{{ '%.1f' % snap.climatology_target_f }}°F vs histórico</span>
        <span class="{{ climate_class }}">p{{ '%.0f' % climate.percentile }} · {{ climate_word }}</span></div>
      <div class="kv"><span class="kv-k">Normal (p50)</span><span>{{ '%.0f' % climate.p50 }}°F</span></div>
      <div class="kv"><span class="kv-k">p10 – p90</span><span>{{ '%.0f' % climate.p10 }} – {{ '%.0f' % climate.p90 }}°F</span></div>
      <div class="kv"><span class="kv-k">Record alto / bajo</span><span>{{ '%.0f' % climate.record }}°F / {{ '%.0f' % climate.record_low }}°F</span></div>
      {% endif %}
    </div>
  </div>

  <div class="card">
    <h3>Aserciones · auto modo: {{ auto_mode }}</h3>
    {% for slot in [1, 2, 3] %}
    {% set a = assertions[slot] %}
    <div class="assertion">
      <span>{{ slot }}</span>
      <span>{{ a.label }}</span>
      <span class="cents {{ a.class }}">{% if a.label != '—' %}{{ a.cents }}¢{% endif %}</span>
      <span class="{{ a.mv_class }}">{{ a.mv_str }}</span>
      <span class="stat {{ a.class }}">{{ a.status }}</span>
    </div>
    {% endfor %}
    <form method="POST" action="/api/set">
      <select name="slot"><option value="1">Slot 1</option><option value="2">Slot 2</option></select>
      <input name="expr" placeholder=">89F · =80F · 79-81" autocapitalize="off" autocomplete="off">
      <button type="submit">Set</button>
    </form>
    <form method="POST" action="/api/clear">
      <select name="slot"><option value="1">Slot 1</option><option value="2">Slot 2</option></select>
      <button type="submit" class="danger">Borrar</button>
    </form>
  </div>

  <div class="card" style="margin-top:1rem">
    <h3>Temperatura hoy · observado + ensemble</h3>
    {{ day_chart_svg|safe }}
  </div>

  {% if external %}
  <div class="card" style="margin-top:1rem">
    <h3>Qué dicen los demás</h3>
    {% if external.narrative %}
    <div class="ext-narr">{{ external.narrative }}</div>
    {% endif %}
    {% if external.models %}
    <div class="ext-models">
      <div class="ext-model">
        <div class="ext-model-label">nuestra</div>
        <div class="ext-model-val ours">{{ '%.1f' % external.ours }}°</div>
      </div>
      {% for label, val in external.models %}
      <div class="ext-model">
        <div class="ext-model-label">{{ label }}</div>
        <div class="ext-model-val {% if val is none %}muted{% endif %}">
          {% if val is not none %}{{ '%.1f' % val }}°{% else %}—{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    <div class="ext-summary">
      <span>mediana modelos: <strong>{{ '%.1f' % external.median }}°</strong>
            · spread: {{ '%.1f' % external.spread }}°</span>
      <span class="{{ external.delta_class }}">
        nuestra vs mediana: {{ external.delta_str }}
      </span>
    </div>
    {% endif %}
  </div>
  {% endif %}

  {% if top_max_bars %}
  <div class="card" style="margin-top:1rem">
    <h3>Máximas más probables (°F enteros)</h3>
    {% for b in top_max_bars %}
    <div class="topmax-row">
      <div class="topmax-deg {% if b.is_modal %}modal{% endif %}">{{ b.deg }}°</div>
      <div class="topmax-bar-wrap">
        <div class="topmax-bar {% if b.is_modal %}modal{% endif %}"
             style="width: {{ '%.1f' % b.bar_pct }}%"></div>
      </div>
      <div class="topmax-pct">{{ '%.0f' % b.p_pct }}%</div>
    </div>
    {% endfor %}
    <div class="topmax-hint">Probabilidad por entero (redondeo al más cercano) sobre el ensemble actual.</div>
  </div>
  {% endif %}

  <div class="card" style="margin-top:1rem">
    <h3>Pronóstico próximas horas</h3>
    {% for ts, med, p10, p90 in snap.forecast_next_hours %}
    <div class="forecast-hour">
      <span class="kv-k">{{ ts.strftime('%H:%M') }}</span>
      <span>{{ '%.0f' % med }}°F</span>
      <span class="kv-k" style="text-align:right">p10-p90 {{ '%.0f' % p10 }}–{{ '%.0f' % p90 }}°F</span>
    </div>
    {% endfor %}
  </div>

  <div class="card" style="margin-top:1rem">
    <h3>Cambiar estación</h3>
    <form method="POST" action="/api/station">
      <input name="id" placeholder="KJFK, TJSJ, KSFO..." autocapitalize="characters" autocomplete="off">
      <button type="submit">Cambiar</button>
    </form>
    <form method="POST" action="/api/refresh">
      <button type="submit" style="width:100%">Refrescar ahora</button>
    </form>
  </div>

  {% if clock %}
  <div class="clock-wrap">
    <div class="clock-title">Reloj del día · {{station.id}}</div>
    <div class="clock-bar">
      <div class="zone zone-pre"  style="left:0;width:{{'%.1f'|format(clock.confidence_start_pct)}}%"></div>
      <div class="zone zone-conf" style="left:{{'%.1f'|format(clock.confidence_start_pct)}}%;width:{{'%.1f'|format(clock.decisive_start_pct - clock.confidence_start_pct)}}%"></div>
      <div class="zone zone-dec"  style="left:{{'%.1f'|format(clock.decisive_start_pct)}}%;width:{{'%.1f'|format(clock.decisive_end_pct - clock.decisive_start_pct)}}%"></div>
      <div class="zone zone-post" style="left:{{'%.1f'|format(clock.decisive_end_pct)}}%;right:0"></div>
      <div class="modal-mark" style="left:{{'%.1f'|format(clock.modal_pct)}}%" title="Pico esperado {{'%02d'|format(clock.modal_h_int)}}:00 {{clock.tz_abbr}} · {{'%02d'|format(clock.modal_pr_h_int)}}:00 PR"></div>
      <div class="now-mark" style="left:{{'%.1f'|format(clock.now_pct)}}%" title="Ahora {{'%02d'|format(clock.now_h_int)}}:{{'%02d'|format(clock.now_min)}} {{clock.tz_abbr}} · {{'%02d'|format(clock.now_pr_h_int)}}:{{'%02d'|format(clock.now_pr_min)}} PR"></div>
    </div>
    <div class="clock-axis">
      <span>6h</span><span>9h</span><span>12h</span><span>15h</span><span>18h</span><span>21h</span><span>23h</span>
    </div>
    <div class="clock-legend">
      <span><i class="dot pre"></i>pre · obs poco diagnósticas</span>
      <span><i class="dot conf"></i>confianza creciente</span>
      <span><i class="dot dec"></i>decisiva {{'%02d'|format(clock.decisive_start_h_int)}}–{{'%02d'|format(clock.decisive_end_h_int)}}h {{clock.tz_abbr}} ({{'%02d'|format(clock.decisive_start_pr_h_int)}}–{{'%02d'|format(clock.decisive_end_pr_h_int)}}h PR) · si no llega aquí, no llega</span>
      <span><i class="dot peak"></i>pico esperado {{'%02d'|format(clock.modal_h_int)}}h {{clock.tz_abbr}} ({{'%02d'|format(clock.modal_pr_h_int)}}h PR)</span>
    </div>
    <div class="clock-now-text">
      Ahora {{'%02d'|format(clock.now_h_int)}}:{{'%02d'|format(clock.now_min)}} {{clock.tz_abbr}} · {{'%02d'|format(clock.now_pr_h_int)}}:{{'%02d'|format(clock.now_pr_min)}} PR → <b>{{clock.now_zone}}</b>
    </div>
  </div>
  {% endif %}

  <details class="tools">
    <summary>Más herramientas · diagnóstico</summary>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
                gap:.35rem;font-size:13px;margin-top:.3rem">
      <a href="/comparison" style="color:#f5c2e7">{{market_name}} vs nuestro modelo →</a>
      <a href="/ladder" style="color:#a6e3a1">Threshold ladder →</a>
      <a href="/calibration" style="color:#89b4fa">Reliability diagram →</a>
      <a href="/timing" style="color:#fab387">Peak timing →</a>
      <a href="/edge" style="color:#94e2d5">Edge tracking →</a>
      <a href="/cross" style="color:#cba6f7">Cross-station →</a>
      <a href="/movement" style="color:#f9e2af">Movement tracking →</a>
      <a href="/history" style="color:#b4befe">Historial diario →</a>
      <a href="/bets" style="color:#f5c2e7">Simulador P&amp;L →</a>
      <a href="/notify" style="color:#f38ba8">Push notifications →</a>
      <a href="/alerts" style="color:#f38ba8">Alertas NWS →</a>
      <a href="/export" style="color:#fab387">Export CSV →</a>
      <a href="/status" style="color:#94e2d5">Status / poll health →</a>
      <a href="/reweight" style="color:#94e2d5">Reweight diagnostics →</a>
      <a href="/about" style="color:#cba6f7">About / tutorial →</a>
    </div>
  </details>
</div>
<script>
  let lastUpdate = {{ (snap.fetched_at.timestamp() * 1000)|int }};
  function tick() {
    const age = Math.floor((Date.now() - lastUpdate) / 1000);
    const el = document.getElementById('age');
    if (el) el.textContent = age;
    if (age >= 12 && age % 10 === 2) {
      fetch('/api/ping').then(r => r.json()).then(d => {
        if (d.ts && d.ts_ms > lastUpdate) location.reload();
      }).catch(() => {});
    }
  }
  setInterval(tick, 1000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    if state is None or state.last_snapshot is None:
        return ("<html><body style='background:#0a0e14;color:#cdd6f4;font-family:sans-serif;"
                "padding:2rem'>Cargando primera observación… recarga en unos segundos.</body></html>")
    snap = state.last_snapshot
    station = state.station
    dist = sorted(snap.ensemble_daily_maxes)
    n = len(dist)
    dist_med = dist[n // 2]
    dist_p10, dist_p90 = dist[int(n * 0.1)], dist[int(n * 0.9)]
    spread = max(dist) - min(dist)
    if spread < 0.1:
        ml_display = f"final = {dist_med:.1f}°F (ensemble convergido)"
    else:
        val, w, p = find_informative_bin(snap.ensemble_daily_maxes)
        ml_display = f"{val:.1f}°F ±{w/2:.2f}  (P={p*100:.0f}%)"

    if snap.pressure_trend_3h is not None:
        d = snap.pressure_trend_3h
        arrow = "↑" if d > 0.02 else "↓" if d < -0.02 else "→"
        pressure_arrow = f"{arrow} {d:+.2f}/3h"
    else:
        pressure_arrow = ""

    feels_line = ""
    if snap.heat_index_f and snap.heat_index_f > snap.current_temp_f + 1:
        feels_line = f"sens {snap.heat_index_f:.0f}°F"
    elif snap.wind_chill_f and snap.wind_chill_f < snap.current_temp_f - 1:
        feels_line = f"sens {snap.wind_chill_f:.0f}°F"

    assertions = {}
    for slot in (1, 2, 3):
        a = state.assertions.get(slot)
        if a is None:
            assertions[slot] = {"label": "—", "prob": 0, "cents": 0, "status": "",
                                "class": "", "mv_str": "", "mv_class": ""}
            continue
        prob, status = eval_assertion(a, snap)
        cls = ("resuelta" if "RESUELTA" in status else
               "fallida" if "FALLIDA" in status else "live")
        label = a.expr + (" (auto)" if a.auto else "")
        mv = movement_cents(a)
        if mv is None:
            mv_str, mv_class = "—", "mv-flat"
        elif mv > 0:
            mv_str, mv_class = f"↑+{mv}¢", "mv-up"
        elif mv < 0:
            mv_str, mv_class = f"↓{mv}¢", "mv-down"
        else:
            mv_str, mv_class = "→0", "mv-flat"
        assertions[slot] = {"label": label, "prob": prob,
                            "cents": int(round(prob * 100)),
                            "status": status, "class": cls,
                            "mv_str": mv_str, "mv_class": mv_class}

    peak_class = ("peak-green" if "confirmado" in snap.peak_status or "probable" in snap.peak_status
                  else "peak-yellow" if "alza" in snap.peak_status
                  else "peak-cyan")

    pr_time = snap.station_local.astimezone(PR_TZ).strftime("%H:%M")
    local_time = snap.station_local.strftime("%H:%M %Z")
    day_chart_svg = build_day_chart_svg(snap.day_chart, snap.station_local.hour)

    climate = snap.climatology
    climate_class, climate_word = "", ""
    if climate is not None:
        pct = climate.percentile
        if pct >= 95:
            climate_class, climate_word = "fallida", "MUY CALIENTE"
        elif pct >= 80:
            climate_class, climate_word = "peak-yellow", "caliente"
        elif pct >= 20:
            climate_class, climate_word = "peak-green", "normal"
        elif pct >= 5:
            climate_class, climate_word = "peak-cyan", "fresco"
        else:
            climate_class, climate_word = "live", "MUY FRÍO"

    # Kalshi bin modal + top edge (if any market data)
    market = None
    if _kalshi is not None:
        today = snap.station_local.date()
        try:
            kalshi_bins = _kalshi.latest_snapshot(station.id, today)
        except Exception:
            kalshi_bins = []
        if kalshi_bins:
            valid = [b for b in kalshi_bins
                     if b.get("yes_mid") is not None
                     and b.get("our_p") is not None]
            if valid:
                modal = max(valid, key=lambda b: b["yes_mid"])
                top = max(valid, key=lambda b: abs(b["our_p"] - b["yes_mid"]))
                top_edge = top["our_p"] - top["yes_mid"]
                market = {
                    "modal_label": modal.get("label") or f"{modal['bin_lo']:.0f}-{modal['bin_hi']:.0f}",
                    "modal_mid": modal["yes_mid"],
                    "modal_ourp": modal["our_p"],
                    "top_label": top.get("label") or f"{top['bin_lo']:.0f}-{top['bin_hi']:.0f}",
                    "top_edge": top_edge,
                    "top_alert": abs(top_edge) >= 0.05,
                }

    # Peak timing (cacheado porque usa fetch_ensemble cacheado)
    timing = None
    if _peak_timing is not None:
        try:
            t = _peak_timing.compute(station)
            timing = {
                "modal_hour": t["modal_hour"],
                "p10": t["p10"], "p90": t["p90"],
                "prob_already": t["prob_already"],
            }
        except Exception:
            timing = None

    # Reloj del día: zonas (pre / confianza / decisiva / post) + marcador pico + cursor ahora
    clock = None
    if timing is not None and timing["p10"] is not None and timing["p90"] is not None:
        peak_lo_h, _peak_hi_h = PEAK_HOURS.get(station.id, (12, 16))
        now_dt = snap.station_local
        now_h_float = now_dt.hour + now_dt.minute / 60.0
        confidence_start = max(peak_lo_h - 3.0, 6.0)
        decisive_start = float(timing["p10"])
        decisive_end = float(timing["p90"])
        if decisive_end < decisive_start:
            decisive_end = decisive_start
        if confidence_start > decisive_start:
            confidence_start = max(decisive_start - 1.0, 6.0)
        modal_h = float(timing["modal_hour"]) if timing["modal_hour"] is not None \
                  else (decisive_start + decisive_end) / 2.0
        range_lo, range_hi = 6.0, 23.0
        def _pct(h):
            return max(0.0, min(100.0, (h - range_lo) / (range_hi - range_lo) * 100.0))
        if now_h_float < confidence_start:
            zone = "pre-confianza"
        elif now_h_float < decisive_start:
            zone = "confianza creciente"
        elif now_h_float <= decisive_end:
            zone = "ventana DECISIVA"
        else:
            zone = "post-pico"

        # Conversión a hora PR: el reloj está en hora de la estación, pero el
        # usuario opera desde PR — mostramos ambas para no confundir.
        def _to_pr_hour(h_float: float) -> tuple[int, int]:
            base = now_dt.replace(minute=0, second=0, microsecond=0)
            hh = int(h_float)
            mm = int(round((h_float - hh) * 60))
            local_at = base.replace(hour=max(0, min(23, hh)), minute=max(0, min(59, mm)))
            pr_at = local_at.astimezone(PR_TZ)
            return pr_at.hour, pr_at.minute
        ds_pr_h, _ = _to_pr_hour(decisive_start)
        de_pr_h, _ = _to_pr_hour(decisive_end)
        mp_pr_h, _ = _to_pr_hour(modal_h)
        now_pr = now_dt.astimezone(PR_TZ)
        tz_abbr = now_dt.strftime("%Z") or "local"

        clock = {
            "now_pct": _pct(now_h_float),
            "now_h_int": now_dt.hour, "now_min": now_dt.minute,
            "now_pr_h_int": now_pr.hour, "now_pr_min": now_pr.minute,
            "confidence_start_pct": _pct(confidence_start),
            "decisive_start_pct": _pct(decisive_start),
            "decisive_end_pct": _pct(decisive_end),
            "modal_pct": _pct(modal_h),
            "decisive_start_h_int": int(decisive_start),
            "decisive_end_h_int": int(decisive_end),
            "modal_h_int": int(modal_h),
            "decisive_start_pr_h_int": ds_pr_h,
            "decisive_end_pr_h_int": de_pr_h,
            "modal_pr_h_int": mp_pr_h,
            "tz_abbr": tz_abbr,
            "now_zone": zone,
        }

    # Precipitation summary for today (uses its own cached ensemble fetch)
    precip = None
    try:
        from predictor import build_precip_summary as _bps
        ps = _bps(station, 0)
        if ps["n_members"]:
            precip = {
                "p_any": ps["p_any_precip"] or 0.0,
                "p_notable": ps["p_notable_precip"] or 0.0,
                "expected_mm": ps["expected_mm"] or 0.0,
                "p_any_snow": ps["p_any_snow"] or 0.0,
            }
    except Exception:
        precip = None

    dash = _build_dashboard(station.id)
    hero = _build_hero(snap.ensemble_daily_maxes, state.prev_dist_med)
    top_max_bars = build_top_max_bars(snap.ensemble_daily_maxes)
    external = _build_external_view(station, dist_med)
    station_options = _supported_stations()

    difficulty = None
    if _difficulty is not None:
        d = _difficulty.compute(
            ens_p10=dist_p10, ens_p90=dist_p90,
            eff_n=snap.ensemble_eff_n,
            total_members=len(snap.ensemble_raw_maxes) or len(snap.ensemble_daily_maxes),
            clim_percentile=(climate.percentile if climate is not None else None),
            p_notable_precip=(precip["p_notable"] if precip else None),
            regime_breaks=len(snap.regime_break_hours),
        )
        klass = {"fácil": "easy", "normal": "normal",
                 "difícil": "hard", "muy difícil": "veryhard"}[d.label]
        difficulty = {
            "score": d.score, "label": d.label, "klass": klass,
            "reasons": d.reasons, "recommend_skip": d.recommend_skip,
        }

    signals = _build_signals(difficulty, market, external, dash, snap)

    return render_template_string(
        HTML, station=station, snap=snap, dash=dash, hero=hero,
        signals=signals,
        top_max_bars=top_max_bars, external=external,
        station_options=station_options,
        dist_med=dist_med, dist_p10=dist_p10, dist_p90=dist_p90,
        ml_display=ml_display, assertions=assertions,
        auto_mode=state.auto_mode, peak_class=peak_class,
        pressure_arrow=pressure_arrow, feels_line=feels_line,
        pr_time=pr_time, local_time=local_time,
        day_chart_svg=day_chart_svg,
        climate=climate, climate_class=climate_class, climate_word=climate_word,
        market=market, timing=timing, clock=clock, precip=precip,
        difficulty=difficulty,
        market_name=_market_name(station.id),
    )


@app.route("/api/ping")
def api_ping():
    if state is None or state.last_snapshot is None:
        return jsonify({"ts": None})
    ts_ms = int(state.last_snapshot.fetched_at.timestamp() * 1000)
    return jsonify({"ts": state.last_snapshot.fetched_at.isoformat(), "ts_ms": ts_ms})


@app.route("/api/set", methods=["POST"])
def api_set():
    try:
        slot = int(request.form["slot"])
        if slot not in (1, 2):
            return "slot 1 o 2", 400
        op, thr, half, expr = parse_expr(request.form["expr"])
        with state_lock:
            prev = state.assertions.get(slot)
            state.assertions[slot] = Assertion(
                expr=expr, op=op, threshold=thr, bin_half=half,
                history=prev.history if prev else [])
    except Exception as e:
        return f"error: {e}", 400
    return redirect("/")


@app.route("/api/clear", methods=["POST"])
def api_clear():
    slot = int(request.form["slot"])
    with state_lock:
        if slot in state.assertions and slot != 3:
            del state.assertions[slot]
    return redirect("/")


@app.route("/api/station", methods=["POST"])
def api_station():
    sid = request.form["id"].strip().upper()
    if not sid:
        return redirect("/")
    try:
        new = fetch_station(sid)
    except Exception as e:
        return f"estación no encontrada: {e}", 400
    with state_lock:
        state.set_station(new)
    threading.Thread(target=do_poll, daemon=True).start()
    return redirect("/")


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=do_poll, daemon=True).start()
    return redirect("/")


def _reliability_svg(rep, kalshi_rep=None, market_name: str = "Kalshi") -> str:
    """Render reliability diagram. Our buckets in blue; optional market
    buckets in pink for side-by-side comparison."""
    W, H = 420, 420
    m = 50
    plot_w, plot_h = W - 2 * m, H - 2 * m
    dots = []
    # our buckets (blue)
    for b in rep.buckets:
        if b.n == 0:
            continue
        x = m + b.mean_pred * plot_w
        y = H - m - b.hit_rate * plot_h
        r = 3 + min(10, b.n ** 0.5)
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" '
                    f'fill="#89b4fa" stroke="#1e66f5" stroke-width="1.5">'
                    f'<title>NOSOTROS {b.low:.1f}-{b.high:.1f}: n={b.n}, '
                    f'pred={b.mean_pred*100:.1f}%, hit={b.hit_rate*100:.1f}%'
                    f'</title></circle>')
    # Kalshi buckets (pink triangle-ish; use diamond via rotated square)
    if kalshi_rep:
        for b in kalshi_rep["buckets"]:
            if b["n"] == 0:
                continue
            x = m + b["mean_pred"] * plot_w
            y = H - m - b["hit_rate"] * plot_h
            r = 3 + min(10, b["n"] ** 0.5)
            dots.append(f'<rect x="{x-r:.1f}" y="{y-r:.1f}" width="{2*r:.1f}" '
                        f'height="{2*r:.1f}" transform="rotate(45 {x:.1f} {y:.1f})" '
                        f'fill="#f5c2e7" stroke="#d44d99" stroke-width="1.5">'
                        f'<title>{market_name.upper()} {b["low"]:.1f}-{b["high"]:.1f}: n={b["n"]}, '
                        f'pred={b["mean_pred"]*100:.1f}%, hit={b["hit_rate"]*100:.1f}%'
                        f'</title></rect>')
    # grid + ticks every 20%
    grid = []
    for i in range(0, 11, 2):
        t = i / 10
        x = m + t * plot_w
        y = H - m - t * plot_h
        grid.append(f'<line x1="{x:.1f}" y1="{m}" x2="{x:.1f}" '
                    f'y2="{H-m}" stroke="#313244" stroke-width="0.5"/>')
        grid.append(f'<line x1="{m}" y1="{y:.1f}" x2="{W-m}" '
                    f'y2="{y:.1f}" stroke="#313244" stroke-width="0.5"/>')
        grid.append(f'<text x="{x:.1f}" y="{H-m+15}" fill="#a6adc8" '
                    f'font-size="10" text-anchor="middle">{int(t*100)}%</text>')
        grid.append(f'<text x="{m-8}" y="{y+3:.1f}" fill="#a6adc8" '
                    f'font-size="10" text-anchor="end">{int(t*100)}%</text>')
    legend = (
        f'<g font-size="11" font-family="system-ui">'
        f'<circle cx="{m+10}" cy="{m-25}" r="5" fill="#89b4fa" stroke="#1e66f5"/>'
        f'<text x="{m+22}" y="{m-21}" fill="#cdd6f4">nosotros</text>'
        f'<rect x="{m+90}" y="{m-30}" width="10" height="10" transform="rotate(45 {m+95} {m-25})" fill="#f5c2e7" stroke="#d44d99"/>'
        f'<text x="{m+110}" y="{m-21}" fill="#cdd6f4">{market_name}</text>'
        f'</g>' if kalshi_rep else ""
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" style="max-width:100%;height:auto">
  <rect width="{W}" height="{H}" fill="#1e1e2e"/>
  {''.join(grid)}
  <line x1="{m}" y1="{H-m}" x2="{W-m}" y2="{m}" stroke="#f9e2af" stroke-width="1" stroke-dasharray="4,4"/>
  <rect x="{m}" y="{m}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#585b70"/>
  {''.join(dots)}
  {legend}
  <text x="{W/2}" y="{H-10}" fill="#cdd6f4" font-size="12" text-anchor="middle">Probabilidad predicha</text>
  <text x="15" y="{H/2}" fill="#cdd6f4" font-size="12" text-anchor="middle" transform="rotate(-90 15 {H/2})">Frecuencia observada</text>
</svg>"""


CALIB_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Reliability</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:900px;margin:0 auto}
  h1{color:#f5c2e7;margin:0 0 .4rem} a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:4px 8px;text-align:right;border-bottom:1px solid #313244}
  th{color:#a6adc8;text-align:center}
  .dim{color:#6c7086}
  .brier{font-size:18px;color:#a6e3a1}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Reliability — {{scope}}</h1>
<div class="card">
  <div>Snapshots totales: <b>{{total}}</b> · resueltos: <b>{{settled}}</b></div>
  {% if brier is not none %}
    <div class="brier">Brier nosotros: {{'%.4f'|format(brier)}} <span class="dim">(0=perfecto, 0.25=al azar)</span></div>
  {% else %}
    <div class="dim">Sin datos resueltos aún — vuelve mañana cuando el archive API tenga el max de hoy.</div>
  {% endif %}
  {% if kalshi_rep and kalshi_rep.brier is not none %}
    <div class="brier" style="color:#f5c2e7">Brier {{market_name}}: {{'%.4f'|format(kalshi_rep.brier)}}
      <span class="dim">({{kalshi_rep.settled_n}} filas resueltas de {{kalshi_rep.total_n}})</span></div>
  {% elif kalshi_rep %}
    <div class="dim">{{market_name}}: {{kalshi_rep.total_n}} filas capturadas, {{kalshi_rep.settled_n}} resueltas.</div>
  {% endif %}
</div>
{% if settled or (kalshi_rep and kalshi_rep.settled_n) %}
<div class="card">{{svg|safe}}
<p class="dim" style="font-size:12px">Círculos azules = nosotros · diamantes rosas = {{market_name}} · tamaño ∝ √n · diagonal amarilla = calibración perfecta.</p></div>
{% endif %}
{% if settled %}
<div class="card"><h3 style="margin:.2rem 0;color:#89b4fa">Nosotros</h3><table>
<tr><th>bucket</th><th>n</th><th>pred medio</th><th>hit rate</th><th>diff</th></tr>
{% for b in buckets %}{% if b.n > 0 %}
<tr><td>{{'%.1f'|format(b.low)}} – {{'%.1f'|format(b.high)}}</td>
    <td>{{b.n}}</td>
    <td>{{'%.1f'|format(b.mean_pred*100)}}%</td>
    <td>{{'%.1f'|format(b.hit_rate*100)}}%</td>
    <td style="color:{% if b.hit_rate >= b.mean_pred %}#a6e3a1{% else %}#f38ba8{% endif %}">
        {{'%+.1f'|format((b.hit_rate - b.mean_pred)*100)}}pp</td></tr>
{% endif %}{% endfor %}
</table></div>
{% endif %}
{% if kalshi_rep and kalshi_rep.settled_n %}
<div class="card"><h3 style="margin:.2rem 0;color:#f5c2e7">{{market_name}}</h3><table>
<tr><th>bucket</th><th>n</th><th>pred medio</th><th>hit rate</th><th>diff</th></tr>
{% for b in kalshi_rep.buckets %}{% if b.n > 0 %}
<tr><td>{{'%.1f'|format(b.low)}} – {{'%.1f'|format(b.high)}}</td>
    <td>{{b.n}}</td>
    <td>{{'%.1f'|format(b.mean_pred*100)}}%</td>
    <td>{{'%.1f'|format(b.hit_rate*100)}}%</td>
    <td style="color:{% if b.hit_rate >= b.mean_pred %}#a6e3a1{% else %}#f38ba8{% endif %}">
        {{'%+.1f'|format((b.hit_rate - b.mean_pred)*100)}}pp</td></tr>
{% endif %}{% endfor %}
</table></div>
{% endif %}
<div class="card">
  <h3 style="margin:.2rem 0;color:#a6e3a1">Auto-calibración isotónica</h3>
  <div class="dim" style="font-size:12px;margin-bottom:.4rem">
    Regresión PAV sobre pares (p, outcome) settleados. Si nuestro modelo
    sobre-estima, la curva verde queda debajo de la diagonal; si sub-estima,
    arriba. Aplicar este mapa a futuras p corregiría el sesgo sistemático.
  </div>
  <div>Muestras fit: <b>{{cal.n_fit}}</b> · días únicos: <b>{{cal.n_days}}</b>
    · bloques PAV: <b>{{cal.blocks}}</b>
    <span class="dim">(min: {{cal.min_n}} muestras, {{cal.min_days}} días)</span></div>
  {% if cal.raw_brier is not none and cal.cal_brier is not none %}
    <div>Brier <b>cruda</b>: {{'%.4f'|format(cal.raw_brier)}} ·
         Brier <b>calibrada</b>:
         <span style="color:{% if cal.cal_brier < cal.raw_brier %}#a6e3a1{% else %}#f38ba8{% endif %}">
           {{'%.4f'|format(cal.cal_brier)}}</span>
         (mejora: {{'%+.4f'|format(cal.raw_brier - cal.cal_brier)}})</div>
  {% endif %}
  {% if not cal.enough %}
    <div style="color:#f9e2af;font-size:12px;margin-top:.3rem">
      ⚠ Fit disponible pero poco confiable: N={{cal.n_fit}} (min {{cal.min_n}})
      · días={{cal.n_days}} (min {{cal.min_days}}).
      Acumula más días settleados antes de aplicar en vivo.
    </div>
  {% endif %}
  {% if cal.svg %}<div style="margin-top:.6rem">{{cal.svg|safe}}</div>{% endif %}
</div>
<p class="dim" style="font-size:12px">
  <a href="/calibration">esta estación</a> ·
  <a href="/calibration?all=1">todas las estaciones</a>
</p>
</body></html>"""


COMPARE_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>{{market_name}} vs nosotros</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:900px;margin:0 auto}
  h1{color:#f5c2e7;margin:0 0 .4rem} a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:14px}
  th,td{padding:6px 8px;border-bottom:1px solid #313244}
  th{color:#a6adc8;text-align:center;font-weight:normal}
  td.lbl{color:#cdd6f4}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  .bar{position:relative;height:10px;background:#313244;border-radius:2px;overflow:hidden}
  .bar-k{position:absolute;top:0;left:0;height:100%;background:#f5c2e7}
  .bar-o{position:absolute;top:0;left:0;height:100%;background:#a6e3a1;opacity:.65}
  .diff-pos{color:#a6e3a1}
  .diff-neg{color:#f38ba8}
  .dim{color:#6c7086;font-size:12px}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>{{market_name}} vs nuestro modelo — {{station}}  {{target_date}} ({{day_label}})</h1>
<div class="card">
  <div>
    <a href="?day=0" style="color:{% if day_offset==0 %}#f9e2af{% else %}#89b4fa{% endif %}">hoy</a> ·
    <a href="?day=1" style="color:{% if day_offset==1 %}#f9e2af{% else %}#89b4fa{% endif %}">mañana</a> ·
    <a href="?day=2" style="color:{% if day_offset==2 %}#f9e2af{% else %}#89b4fa{% endif %}">pasado</a>
  </div>
  <div class="dim" style="margin-top:.4rem">
    {% if day_offset == 0 %}Max observado hasta ahora: <b style="color:#f9e2af">{{max_obs}}</b> · {% endif %}
    último fetch {{market_name}}: <b>{{fetched_age}}</b>
    ·
    {% if cal_active %}
      <span style="background:#2a4a32;color:#a6e3a1;padding:.05rem .4rem;border-radius:3px;font-size:11px;font-weight:600">CALIBRADO</span>
      isotonic n={{cal_n_fit}}, {{cal_n_days}}d
    {% else %}
      <span style="background:#3a3a3a;color:#a6adc8;padding:.05rem .4rem;border-radius:3px;font-size:11px">RAW</span>
      gate {{cal_n_fit}}/{{cal_min_n}}n · {{cal_n_days}}/{{cal_min_days}}d
    {% endif %}
  </div>
</div>
{% if not bins %}
<div class="card">
  <p>No hay mercado activo para esta estación/fecha.</p>
  <p class="dim">Estaciones soportadas (Kalshi): KPHX, KLAX, KLAS, KLGA, KBOS.</p>
</div>
{% else %}
<div class="card"><table>
<tr><th style="text-align:left">rango</th>
    <th>{{market_name}} (mid)</th><th>nosotros</th>
    <th>diff</th><th style="width:35%">visual</th></tr>
{% for b in bins %}
<tr>
  <td class="lbl">{{b.label}}</td>
  <td class="num" style="color:#f5c2e7">{{'%.1f'|format((b.yes_mid or 0)*100)}}%</td>
  <td class="num" style="color:#a6e3a1" title="{% if b.our_p_raw is not none %}raw {{'%.1f'|format(b.our_p_raw*100)}}%{% endif %}">{{'%.1f'|format((b.our_p or 0)*100)}}%</td>
  <td class="num {% if (b.our_p or 0) >= (b.yes_mid or 0) %}diff-pos{% else %}diff-neg{% endif %}">
    {{'%+.1f'|format(((b.our_p or 0) - (b.yes_mid or 0))*100)}}pp
  </td>
  <td>
    <div class="bar">
      <div class="bar-k" style="width:{{(b.yes_mid or 0)*100}}%"></div>
      <div class="bar-o" style="width:{{(b.our_p or 0)*100}}%"></div>
    </div>
  </td>
</tr>
{% endfor %}
</table>
<p class="dim" style="margin-top:.8rem">
  <span style="color:#f5c2e7">■</span> {{market_name}} · <span style="color:#a6e3a1">■</span> nuestro modelo (ensemble GFS).
  Diff positivo = nosotros le damos más probabilidad que el mercado.
  Datos se graban cada poll (~10 min) en market_cache.db.
</p>
</div>
{% endif %}
<p class="dim">Refrescar: esta página se recarga manual; el fetch automático ocurre en cada poll del servidor.</p>
</body></html>"""


LADDER_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Decision ladder</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:1200px;margin:0 auto}
  h1{color:#f5c2e7;margin:0 0 .4rem} a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:5px 7px;border-bottom:1px solid #313244}
  th{color:#a6adc8;font-weight:normal;text-align:right}
  th:first-child,td:first-child{text-align:left}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  .diff-pos{color:#a6e3a1} .diff-neg{color:#f38ba8}
  .dim{color:#6c7086;font-size:12px}
  tr.hl{background:#252535}
  .pill{display:inline-block;padding:.05rem .4rem;border-radius:3px;font-size:11px;font-weight:600}
  .rec-yes{background:#2a4a32;color:#a6e3a1}
  .rec-no{background:#4a2a32;color:#f38ba8}
  .rec-none{background:#313244;color:#6c7086}
  .ev-pos{color:#a6e3a1;font-weight:600}
  .ev-neg{color:#f38ba8}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Decision ladder — {{station}} {{target_date}} ({{day_label}})</h1>

<div class="card">
  <div>
    <a href="?day=0&range={{window}}" style="color:{% if day_offset==0 %}#f9e2af{% else %}#89b4fa{% endif %}">hoy</a> ·
    <a href="?day=1&range={{window}}" style="color:{% if day_offset==1 %}#f9e2af{% else %}#89b4fa{% endif %}">mañana</a> ·
    <a href="?day=2&range={{window}}" style="color:{% if day_offset==2 %}#f9e2af{% else %}#89b4fa{% endif %}">pasado</a>
    &nbsp;·&nbsp;
    Ventana:
    {% for r in [2,3,4,6,10] %}
      <a href="?day={{day_offset}}&range={{r}}"
         style="color:{% if not show_all and window==r %}#f9e2af{% else %}#89b4fa{% endif %}">±{{r}}°F</a>{% if not loop.last %} ·{% endif %}
    {% endfor %}
    · <a href="?day={{day_offset}}&all=1" style="color:{% if show_all %}#f9e2af{% else %}#89b4fa{% endif %}">todo</a>
  </div>
  <div style="margin-top:.4rem">
    {% if day_offset == 0 %}Max observado: <b style="color:#f9e2af">{{max_obs}}</b> · {% endif %}
    mediana ensemble: <b>{{median_pred}}</b>
    {% if cal_active %}
      · <span style="background:#2a4a32;color:#a6e3a1;padding:.05rem .4rem;border-radius:3px;font-size:11px;font-weight:600">CALIBRADO</span>
      <span class="dim">isotonic n={{cal_n_fit}}, {{cal_n_days}}d</span>
    {% else %}
      · <span style="background:#3a3a3a;color:#a6adc8;padding:.05rem .4rem;border-radius:3px;font-size:11px">RAW</span>
      <span class="dim">isotonic gate {{cal_n_fit}}/{{cal_min_n}}n · {{cal_n_days}}/{{cal_min_days}}d</span>
    {% endif %}
  </div>
  <div class="dim">
    Mesa de decisión centrada en la mediana (±{{window}}°F).
    <b>yes = P(max &gt; X)</b>, <b>no = P(max ≤ X)</b>.
    EV = retorno esperado por $1 al precio {{market_name}} · Kelly = fracción óptima del bankroll.
    Columna <b>rec</b> marca el lado con EV positivo (si hay).
    {% if day_offset > 0 %}D+{{day_offset}} sin obs → ensemble raw.{% endif %}
  </div>
</div>

<div class="card">
<table>
<tr>
  <th>thr.</th>
  <th>yes (ours)</th><th>no (ours)</th>
  <th>yes (K)</th><th>no (K)</th>
  <th>edge</th>
  <th>EV yes</th><th>EV no</th>
  <th>Kelly yes</th><th>Kelly no</th>
  <th>rec</th>
</tr>
{% for r in rows %}
<tr class="{% if r.hl %}hl{% endif %}">
  <td><b>&gt;{{r.thr}}°F</b></td>
  <td class="num" style="color:#a6e3a1" title="raw {{'%.0f'|format(r.our_yes_raw*100)}}%">{{'%.0f'|format(r.our_yes*100)}}%</td>
  <td class="num" style="color:#a6e3a1;opacity:.6">{{'%.0f'|format(r.our_no*100)}}%</td>
  <td class="num" style="color:#f5c2e7">{% if r.k_yes is not none %}{{'%.0f'|format(r.k_yes*100)}}%{% else %}—{% endif %}</td>
  <td class="num" style="color:#f5c2e7;opacity:.6">{% if r.k_no is not none %}{{'%.0f'|format(r.k_no*100)}}%{% else %}—{% endif %}</td>
  <td class="num {% if r.edge is not none and r.edge >= 0 %}diff-pos{% elif r.edge is not none %}diff-neg{% endif %}">
    {% if r.edge is not none %}{{'%+.0f'|format(r.edge*100)}}pp{% else %}—{% endif %}
  </td>
  <td class="num {% if r.ev_yes is not none and r.ev_yes > 0 %}ev-pos{% elif r.ev_yes is not none %}ev-neg{% endif %}">
    {% if r.ev_yes is not none %}{{'%+.0f'|format(r.ev_yes*100)}}%{% else %}—{% endif %}
  </td>
  <td class="num {% if r.ev_no is not none and r.ev_no > 0 %}ev-pos{% elif r.ev_no is not none %}ev-neg{% endif %}">
    {% if r.ev_no is not none %}{{'%+.0f'|format(r.ev_no*100)}}%{% else %}—{% endif %}
  </td>
  <td class="num">{% if r.kelly_yes is not none %}{{'%.0f'|format(r.kelly_yes*100)}}%{% else %}—{% endif %}</td>
  <td class="num">{% if r.kelly_no is not none %}{{'%.0f'|format(r.kelly_no*100)}}%{% else %}—{% endif %}</td>
  <td style="text-align:center">
    {% if r.rec == 'yes' %}<span class="pill rec-yes">YES</span>
    {% elif r.rec == 'no' %}<span class="pill rec-no">NO</span>
    {% elif r.k_yes is not none %}<span class="pill rec-none">—</span>
    {% else %}<span class="dim">—</span>{% endif %}
    {% if r.rec_kelly %}<div class="dim" style="font-size:10px">~{{'%.0f'|format(r.rec_kelly*100)}}%</div>{% endif %}
  </td>
</tr>
{% endfor %}
</table>
<p class="dim" style="margin-top:.8rem">
  yes col opaca · no col semi-transparente · fila resaltada = threshold ≈ mediana.
  <br>EV positivo = apostar ese lado tiene expected value &gt; 0 bajo nuestro modelo.
  Kelly = fracción del bankroll a apostar (0% = skip, 10% = moderado, &gt;30% = modelo muy confiado).
</p>
</div>
</body></html>"""


def _bin_to_dict_for_impl(mb, our_p=None):
    return {
        "bin_lo": mb.bin_lo, "bin_hi": mb.bin_hi,
        "yes_mid": mb.yes_mid, "yes_bid": mb.yes_bid, "yes_ask": mb.yes_ask,
        "label": mb.label, "ticker": mb.ticker,
        "our_p": our_p,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _anchor_context(station, dist):
    """Devuelve dict con ext_med, ext_spread, ext_diff, lam o None si falta data.

    Lee del último snapshot (para ext_shift_info.lambda, evitando que el blend
    se sume al shift más allá de ANCHOR_WEIGHT_CAP) y de external_models
    (cache 30min). Usado por comparison_view y _run_auto_bets.
    """
    if _external_models is None or not dist:
        return None
    try:
        mm = _external_models.fetch_multi_model_max(station)
    except Exception:
        mm = None
    if mm is None or mm.median is None or mm.spread is None:
        return None
    pred_med = sorted(dist)[len(dist) // 2]
    ext_diff = pred_med - mm.median
    lam = 0.0
    snap = getattr(state, "last_snapshot", None) if state else None
    if snap is not None and getattr(snap, "ext_shift_info", None):
        lam = float(snap.ext_shift_info.get("lambda") or 0.0)
    return {"ext_med": mm.median, "ext_spread": mm.spread,
            "ext_diff": ext_diff, "lam": lam}


def _load_day_dist(station, day_offset: int):
    """Return (dist_sorted, target_date, max_obs_val). Uses cached snapshot
    for D+0 when available; multi_day otherwise."""
    if day_offset == 0 and state is not None and state.last_snapshot is not None:
        snap = state.last_snapshot
        dist = sorted(snap.ensemble_daily_maxes)
        return dist, snap.station_local.date(), snap.today_max_obs
    if _multi_day is None:
        return [], None, None
    d = _multi_day.day_forecast(station, day_offset)
    return sorted(d["daily_maxes"]), d["target"], d.get("max_obs")


def _ev_kelly(p_our: float, k_yes: float) -> dict:
    """EV y Kelly por $1 apostado en yes o no al precio Kalshi.

    EV_yes = (p - k)/k         · EV_no = (k - p)/(1-k)
    f*_yes = (p - k)/(1 - k)   · f*_no = (k - p)/k
    Retorna el lado recomendado (mayor EV, positivo). None si k inválido.
    """
    if k_yes is None or k_yes <= 0.01 or k_yes >= 0.99:
        return {"ev_yes": None, "ev_no": None,
                "kelly_yes": None, "kelly_no": None,
                "rec": None, "rec_ev": None, "rec_kelly": None}
    ev_yes = (p_our - k_yes) / k_yes
    ev_no = (k_yes - p_our) / (1 - k_yes)
    kel_yes = max(0.0, (p_our - k_yes) / (1 - k_yes))
    kel_no = max(0.0, (k_yes - p_our) / k_yes)
    if ev_yes > ev_no and ev_yes > 0:
        rec, rec_ev, rec_kelly = "yes", ev_yes, kel_yes
    elif ev_no > 0:
        rec, rec_ev, rec_kelly = "no", ev_no, kel_no
    else:
        rec, rec_ev, rec_kelly = None, None, None
    return {"ev_yes": ev_yes, "ev_no": ev_no,
            "kelly_yes": kel_yes, "kelly_no": kel_no,
            "rec": rec, "rec_ev": rec_ev, "rec_kelly": rec_kelly}


@app.route("/ladder")
def ladder_view():
    if state is None:
        return redirect("/")
    station = state.station
    try:
        day_offset = max(0, min(2, int(request.args.get("day", 0))))
    except ValueError:
        day_offset = 0
    try:
        window = max(1, min(15, int(request.args.get("range", 4))))
    except ValueError:
        window = 4
    show_all = request.args.get("all") == "1"

    dist, target, max_obs_val = _load_day_dist(station, day_offset)
    if not dist or target is None:
        return "ensemble vacío", 503
    n = len(dist)
    median = dist[n // 2]
    if show_all:
        thr_lo = int(dist[0]) - 1
        thr_hi = int(dist[-1]) + 1
    else:
        center = round(median)
        thr_lo = center - window
        thr_hi = center + window

    kalshi_bins_for_impl = []
    if _kalshi is not None:
        if day_offset == 0:
            try:
                kalshi_bins_for_impl = _kalshi.latest_snapshot(station.id, target)
            except Exception as e:
                print(f"ladder kalshi error: {e}", file=sys.stderr)
        if not kalshi_bins_for_impl:
            try:
                live = _kalshi.fetch_bins(station.id, target)
                kalshi_bins_for_impl = [_bin_to_dict_for_impl(b) for b in live]
            except Exception as e:
                print(f"ladder live-fetch error: {e}", file=sys.stderr)

    import isotonic as _iso
    cal = _iso.get(station.id)
    cal_active = (cal is not None
                  and cal.n_fit >= _iso.MIN_N
                  and cal.n_days >= _iso.MIN_DAYS)
    cal_for_apply = cal if cal_active else None

    rows = []
    for thr in range(thr_lo, thr_hi + 1):
        our_yes_raw = sum(1 for v in dist if v > thr) / n
        our_yes = _iso.apply(cal_for_apply, our_yes_raw)
        our_no = 1.0 - our_yes
        k_yes = None
        if kalshi_bins_for_impl:
            k_yes = _kalshi.implied_prob_above(kalshi_bins_for_impl, thr)
        k_no = (1.0 - k_yes) if k_yes is not None else None
        ek = _ev_kelly(our_yes, k_yes)
        edge = (our_yes - k_yes) if k_yes is not None else None
        rows.append({
            "thr": thr,
            "our_yes": our_yes, "our_yes_raw": our_yes_raw, "our_no": our_no,
            "k_yes": k_yes, "k_no": k_no,
            "edge": edge,
            **ek,
            "hl": abs(thr - median) < 0.5,
        })
    max_obs = (f"{max_obs_val:.1f}°F" if max_obs_val is not None
               and max_obs_val > -900 else "—")
    day_labels = {0: "hoy", 1: "mañana", 2: "pasado"}
    return render_template_string(
        LADDER_TMPL, station=station.id, target_date=target.isoformat(),
        rows=rows, max_obs=max_obs,
        median_pred=f"{median:.1f}°F",
        day_offset=day_offset, day_label=day_labels[day_offset],
        window=window, show_all=show_all,
        cal_active=cal_active,
        cal_n_fit=(cal.n_fit if cal else 0),
        cal_n_days=(cal.n_days if cal else 0),
        cal_min_n=_iso.MIN_N, cal_min_days=_iso.MIN_DAYS,
        market_name=_market_name(station.id))


@app.route("/comparison")
def comparison_view():
    if _kalshi is None:
        return "kalshi module unavailable", 500
    if state is None:
        return redirect("/")
    station = state.station
    try:
        day_offset = max(0, min(2, int(request.args.get("day", 0))))
    except ValueError:
        day_offset = 0

    dist, target, max_obs_val = _load_day_dist(station, day_offset)
    if target is None:
        target = datetime.now(station.tz).date()

    bins = []
    if day_offset == 0:
        bins = _kalshi.latest_snapshot(station.id, target)
        if not bins and dist:
            try:
                live = _kalshi.fetch_bins(station.id, target)
                if live:
                    _kalshi.record(station.id, target, live, dist, datetime.utcnow())
                    bins = _kalshi.latest_snapshot(station.id, target)
            except Exception as e:
                print(f"comparison live-fetch error: {e}", file=sys.stderr)
    else:
        try:
            live = _kalshi.fetch_bins(station.id, target)
            for mb in live:
                our_p = (_kalshi.our_p_for_bin(dist, mb.bin_lo, mb.bin_hi)
                         if dist else None)
                bins.append(_bin_to_dict_for_impl(mb, our_p))
        except Exception as e:
            print(f"comparison live-fetch (D+{day_offset}) error: {e}",
                  file=sys.stderr)

    import isotonic as _iso
    cal = _iso.get(station.id)
    cal_active = (cal is not None
                  and cal.n_fit >= _iso.MIN_N
                  and cal.n_days >= _iso.MIN_DAYS)
    cal_for_apply = cal if cal_active else None

    # External anchor context (solo para day_offset==0; con day>0 no hay snapshot/bias del día)
    anchor_ctx = _anchor_context(station, dist) if day_offset == 0 else None

    for b in bins:
        if b.get("label") is None:
            if b["bin_lo"] == float("-inf"):
                b["label"] = f"≤{b['bin_hi']:.0f}°F"
            elif b["bin_hi"] == float("inf"):
                b["label"] = f"≥{b['bin_lo']:.0f}°F"
            else:
                b["label"] = f"{b['bin_lo']:.0f}-{b['bin_hi']:.0f}°F"
        if b.get("our_p") is not None:
            b["our_p_raw"] = b["our_p"]
            iso_p = _iso.apply(cal_for_apply, b["our_p"])
            if anchor_ctx is not None:
                blended, w = _external_models.blend_with_external(
                    iso_p, anchor_ctx["ext_med"], anchor_ctx["ext_spread"],
                    b["bin_lo"], b["bin_hi"],
                    anchor_ctx["ext_diff"], anchor_ctx["lam"])
                b["our_p"] = blended
                b["our_p_iso"] = iso_p
                b["anchor_weight"] = w
            else:
                b["our_p"] = iso_p
                b["our_p_iso"] = iso_p
                b["anchor_weight"] = 0.0
        else:
            b["our_p_raw"] = None
            b["our_p_iso"] = None
            b["anchor_weight"] = 0.0

    max_obs = (f"{max_obs_val:.1f}°F" if max_obs_val is not None
               and max_obs_val > -900 else "—")
    if bins:
        latest_ts = max(b["fetched_at"] for b in bins)
        try:
            dt = datetime.fromisoformat(latest_ts)
            tz = dt.tzinfo or timezone.utc
            age = int((datetime.now(tz) - dt).total_seconds())
            fetched_age = f"hace {age//60}m {age%60}s" if age >= 60 else f"hace {age}s"
        except Exception:
            fetched_age = latest_ts
    else:
        fetched_age = "—"
    day_labels = {0: "hoy", 1: "mañana", 2: "pasado"}
    return render_template_string(
        COMPARE_TMPL,
        station=station.id,
        target_date=target.isoformat(),
        bins=bins,
        max_obs=max_obs,
        fetched_age=fetched_age,
        day_offset=day_offset,
        day_label=day_labels[day_offset],
        cal_active=cal_active,
        cal_n_fit=(cal.n_fit if cal else 0),
        cal_n_days=(cal.n_days if cal else 0),
        cal_min_n=_iso.MIN_N, cal_min_days=_iso.MIN_DAYS,
        market_name=_market_name(station.id),
    )


@app.route("/calibration")
def calibration_view():
    if _calibration is None:
        return "calibration module unavailable", 500
    want_all = request.args.get("all") == "1"
    station_id = None if want_all else (state.station.id if state else None)
    rep = _calibration.reliability(station_id)
    kalshi_rep = None
    if _kalshi is not None:
        try:
            kalshi_rep = _kalshi.reliability(station_id)
        except Exception:
            kalshi_rep = None
    scope = "todas las estaciones" if want_all else (station_id or "—")

    import isotonic as _iso
    cal = _iso.refit(station_id)  # always fresh on this page
    cal_info = {
        "n_fit": cal.n_fit if cal else 0,
        "n_days": cal.n_days if cal else 0,
        "min_n": _iso.MIN_N,
        "min_days": _iso.MIN_DAYS,
        "enough": (cal is not None
                   and cal.n_fit >= _iso.MIN_N
                   and cal.n_days >= _iso.MIN_DAYS),
        "blocks": len(cal.blocks) if cal else 0,
        "curve": _iso.reliability_curve(cal, 20) if cal else [],
    }
    # Compute Brier raw vs calibrated on the same settled samples.
    raw_samples = []
    if cal is not None:
        import sqlite3
        from calibration import DB_PATH as _CAL_DB
        cc = sqlite3.connect(_CAL_DB)
        if station_id:
            raw_samples = cc.execute(
                """SELECT predicted_p, outcome FROM prediction_snapshots
                   WHERE outcome IS NOT NULL AND station_id=?""",
                (station_id,)).fetchall()
        else:
            raw_samples = cc.execute(
                """SELECT predicted_p, outcome FROM prediction_snapshots
                   WHERE outcome IS NOT NULL""").fetchall()
        cc.close()
    raw_brier = _iso.brier(raw_samples, None) if raw_samples else None
    cal_brier = _iso.brier(raw_samples, cal) if raw_samples else None
    cal_info["raw_brier"] = raw_brier
    cal_info["cal_brier"] = cal_brier
    cal_info["svg"] = _isotonic_svg(cal_info["curve"]) if cal_info["curve"] else ""

    mkt = _market_name(station_id or (state.station.id if state else ""))
    return render_template_string(
        CALIB_TMPL,
        scope=scope,
        total=rep.total_n,
        settled=rep.settled_n,
        brier=rep.brier,
        buckets=rep.buckets,
        kalshi_rep=kalshi_rep,
        svg=_reliability_svg(rep, kalshi_rep, market_name=mkt),
        cal=cal_info,
        market_name=mkt,
    )


def _isotonic_svg(curve: list) -> str:
    W, H = 360, 360
    m = 40
    plot_w, plot_h = W - 2 * m, H - 2 * m
    pts = []
    for x, y in curve:
        px = m + x * plot_w
        py = H - m - y * plot_h
        pts.append(f"{px:.1f},{py:.1f}")
    path = " ".join(pts)
    grid = []
    for i in range(0, 11, 2):
        t = i / 10
        x = m + t * plot_w
        y = H - m - t * plot_h
        grid.append(f'<line x1="{x:.1f}" y1="{m}" x2="{x:.1f}" y2="{H-m}" '
                    f'stroke="#313244" stroke-width="0.5"/>')
        grid.append(f'<line x1="{m}" y1="{y:.1f}" x2="{W-m}" y2="{y:.1f}" '
                    f'stroke="#313244" stroke-width="0.5"/>')
        grid.append(f'<text x="{x:.1f}" y="{H-m+15}" fill="#a6adc8" '
                    f'font-size="10" text-anchor="middle">{int(t*100)}%</text>')
        grid.append(f'<text x="{m-6}" y="{y+3:.1f}" fill="#a6adc8" '
                    f'font-size="10" text-anchor="end">{int(t*100)}%</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" style="max-width:100%;height:auto">
  <rect width="{W}" height="{H}" fill="#1e1e2e"/>
  {''.join(grid)}
  <line x1="{m}" y1="{H-m}" x2="{W-m}" y2="{m}" stroke="#f9e2af" stroke-width="1" stroke-dasharray="4,4"/>
  <rect x="{m}" y="{m}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#585b70"/>
  <polyline points="{path}" fill="none" stroke="#a6e3a1" stroke-width="2"/>
  <text x="{W/2}" y="{H-8}" fill="#cdd6f4" font-size="11" text-anchor="middle">p cruda</text>
  <text x="14" y="{H/2}" fill="#cdd6f4" font-size="11" text-anchor="middle" transform="rotate(-90 14 {H/2})">p calibrada</text>
</svg>"""


def _timing_hist_svg(hour_hist: dict, current_hour: int,
                     modal: int | None, p10: int | None,
                     p50: int | None, p90: int | None) -> str:
    W, H = 560, 220
    pad_l, pad_r, pad_t, pad_b = 36, 10, 10, 30
    iw, ih = W - pad_l - pad_r, H - pad_t - pad_b
    max_p = max(hour_hist.values()) if hour_hist else 1.0
    bw = iw / 24
    bars = []
    for h in range(24):
        p = hour_hist.get(h, 0.0)
        bh = (p / max_p) * ih if max_p else 0
        x = pad_l + h * bw
        y = pad_t + ih - bh
        in_range = (p10 is not None and p90 is not None and p10 <= h <= p90)
        color = "#fab387" if h == modal else ("#f9e2af" if in_range else "#585b70")
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 1.5:.1f}" '
            f'height="{bh:.1f}" fill="{color}">'
            f'<title>{h:02d}:00 — {p * 100:.1f}%</title></rect>'
        )
    # hour axis labels every 3h
    labels = []
    for h in range(0, 24, 3):
        x = pad_l + h * bw + bw / 2
        labels.append(
            f'<text x="{x:.1f}" y="{H - 10}" fill="#a6adc8" '
            f'font-size="10" text-anchor="middle">{h:02d}</text>'
        )
    # current hour marker
    cx = pad_l + current_hour * bw + bw / 2
    marker = (
        f'<line x1="{cx:.1f}" y1="{pad_t}" x2="{cx:.1f}" y2="{pad_t + ih}" '
        f'stroke="#f38ba8" stroke-width="1.5" stroke-dasharray="3,3"/>'
        f'<text x="{cx:.1f}" y="{pad_t - 1}" fill="#f38ba8" font-size="10" '
        f'text-anchor="middle">ahora</text>'
    )
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px">'
            + "".join(bars) + marker + "".join(labels) + "</svg>")


TIMING_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Peak timing</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:720px;margin:0 auto}
  h1{color:#fab387;margin:0 0 .4rem} a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  .big{font-size:36px;color:#fab387;font-weight:600}
  .row{display:flex;gap:1rem;flex-wrap:wrap}
  .stat{flex:1;min-width:140px}
  .stat .lbl{color:#a6adc8;font-size:12px}
  .stat .val{font-size:22px;color:#f9e2af;font-family:monospace}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:14px}
  th,td{padding:4px 8px;text-align:right;border-bottom:1px solid #313244}
  th{color:#a6adc8;text-align:center}
  .dim{color:#6c7086;font-size:12px}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Peak timing — {{stats.station_id}} · {{stats.today}}</h1>
<div class="card">
  <div class="dim">eff_N = {{'%.1f'|format(stats.eff_n)}} / {{stats.n_members}}
    · {{stats.residual_hours}}h observadas hoy
    · ahora = {{'%02d'|format(stats.current_hour)}}:00</div>
  {% if stats.max_obs is not none %}
    <div class="dim">Max observado hasta ahora: <b style="color:#f9e2af">{{'%.1f'|format(stats.max_obs)}}°F</b>
      a las {{'%02d'|format(stats.max_obs_hour)}}:00</div>
  {% endif %}
</div>
<div class="card row">
  <div class="stat"><div class="lbl">hora modal</div>
    <div class="val">{{'%02d'|format(stats.modal_hour)}}:00</div></div>
  <div class="stat"><div class="lbl">p10 – p90</div>
    <div class="val">{{'%02d'|format(stats.p10)}}:00 – {{'%02d'|format(stats.p90)}}:00</div></div>
  <div class="stat"><div class="lbl">mediana</div>
    <div class="val">{{'%02d'|format(stats.p50)}}:00</div></div>
</div>
<div class="card">
  <div class="dim">Distribución horaria del peak (ponderada)</div>
  {{svg|safe}}
  <div class="dim">naranja = modal · amarillo = p10–p90 · gris = resto · línea rosa = hora actual</div>
</div>
<div class="card row">
  <div class="stat"><div class="lbl">P(peak ya ocurrió)</div>
    <div class="val">{{'%.0f'|format(stats.prob_already * 100)}}%</div></div>
  {% for n, p in stats.prob_next_n.items() %}
  <div class="stat"><div class="lbl">P(peak en próximas {{n}}h)</div>
    <div class="val">{{'%.0f'|format(p * 100)}}%</div></div>
  {% endfor %}
</div>
</body></html>"""


EDGE_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Edge tracking</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:820px;margin:0 auto}
  h1{color:#94e2d5;margin:0 0 .4rem} h2{color:#f5c2e7;margin:.6rem 0 .3rem;font-size:16px}
  a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:4px 8px;text-align:right;border-bottom:1px solid #313244}
  th{color:#a6adc8;text-align:center}
  td.lbl{text-align:left;color:#cdd6f4}
  .pos{color:#a6e3a1} .neg{color:#f38ba8} .dim{color:#6c7086;font-size:12px}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Edge tracking — {{station_id or '—'}}</h1>

<div class="card">
  <h2>Edges ahora (|edge| ≥ 5%)</h2>
  {% if current %}
    <table>
    <tr><th>bin</th><th>{{market_name}} mid</th><th>nosotros</th><th>edge</th><th>acción</th></tr>
    {% for r in current %}
    <tr><td class="lbl">{{r.label}}</td>
        <td>{{'%.1f'|format(r.yes_mid*100)}}%</td>
        <td>{{'%.1f'|format(r.our_p*100)}}%</td>
        <td class="{% if r.edge > 0 %}pos{% else %}neg{% endif %}">
          {{'%+.1f'|format(r.edge*100)}}pp</td>
        <td>{% if r.edge > 0 %}buy YES{% else %}buy NO{% endif %}</td></tr>
    {% endfor %}
    </table>
    <p class="dim">"buy YES/NO" es el lado con valor esperado positivo si confías en nuestro modelo. Educativo, no es consejo financiero.</p>
  {% else %}
    <p class="dim">Sin edges grandes ahora — nuestro modelo y {{market_name}} están ±5%.</p>
  {% endif %}
</div>

<div class="card">
  <h2>Performance histórica por bucket de edge</h2>
  {% if analysis.settled_n %}
    <div class="dim">{{analysis.settled_n}} filas resueltas (bin × snapshot × día).</div>
    <table>
    <tr><th>edge</th><th>n</th><th>mean edge</th><th>hit rate</th><th>ROI hipotético</th></tr>
    {% for b in analysis.buckets %}{% if b.n > 0 %}
    <tr><td class="lbl">{{'%+.0f'|format(b.low*100)}}pp → {{'%+.0f'|format(b.high*100)}}pp</td>
        <td>{{b.n}}</td>
        <td>{{'%+.1f'|format(b.mean_edge*100)}}pp</td>
        <td>{{'%.1f'|format(b.hit_rate*100)}}%</td>
        <td class="{% if b.roi >= 0 %}pos{% else %}neg{% endif %}">
          {{'%+.1f'|format(b.roi*100)}}%</td></tr>
    {% endif %}{% endfor %}
    </table>
    <p class="dim">ROI: compras YES a yes_mid cuando edge &gt; 0, NO a (1-yes_mid) cuando edge &lt; 0. Si nuestro modelo le gana a {{market_name}}, los buckets extremos deberían tener ROI positivo.</p>
  {% else %}
    <p class="dim">Aún no hay días resueltos. Vuelve mañana.</p>
  {% endif %}
</div>
</body></html>"""


@app.route("/edge")
def edge_view():
    if _kalshi is None:
        return "kalshi module unavailable", 500
    if state is None:
        return "no station loaded", 500
    station_id = state.station.id
    today = datetime.now(state.station.tz).date()
    try:
        current = _kalshi.current_edges(station_id, today, min_abs_edge=0.05)
    except Exception:
        current = []
    try:
        analysis = _kalshi.edge_analysis(station_id)
    except Exception:
        analysis = {"buckets": [], "settled_n": 0, "station_id": station_id}
    return render_template_string(
        EDGE_TMPL,
        station_id=station_id,
        current=current,
        analysis=analysis,
        market_name=_market_name(station_id),
    )


@app.route("/timing")
def timing_view():
    if _peak_timing is None:
        return "peak_timing module unavailable", 500
    if state is None:
        return "no station loaded", 500
    try:
        stats = _peak_timing.compute(state.station)
    except Exception as e:
        return f"error: {e}", 500
    svg = _timing_hist_svg(
        stats["hour_hist"], stats["current_hour"],
        stats["modal_hour"], stats["p10"], stats["p50"], stats["p90"],
    )
    return render_template_string(TIMING_TMPL, stats=stats, svg=svg)


def _movement_svg(points: list[dict], label: str, station_tz) -> str:
    """Dos líneas (yes_mid en rosa, our_p en verde) sobre el rango horario del día."""
    valid = [p for p in points
             if p["yes_mid"] is not None and p["our_p"] is not None]
    if len(valid) < 2:
        return "<p style='color:#a6adc8;font-size:12px'>pocos puntos para graficar</p>"
    W, H = 640, 240
    pad_l, pad_r, pad_t, pad_b = 44, 12, 20, 30
    iw, ih = W - pad_l - pad_r, H - pad_t - pad_b

    parsed = []
    for p in valid:
        try:
            dt = datetime.fromisoformat(p["t"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(station_tz)
            parsed.append((dt_local, p["yes_mid"], p["our_p"]))
        except Exception:
            continue
    if len(parsed) < 2:
        return "<p style='color:#a6adc8'>timestamps inválidos</p>"
    parsed.sort(key=lambda x: x[0])
    t0 = parsed[0][0].replace(hour=0, minute=0, second=0, microsecond=0)
    t1 = t0.replace(hour=23, minute=59, second=59)
    span = (t1 - t0).total_seconds() or 1

    def xpos(dt):
        return pad_l + (dt - t0).total_seconds() / span * iw

    def ypos(p):
        return pad_t + (1 - p) * ih

    k_pts = " ".join(f"{xpos(dt):.1f},{ypos(ym):.1f}"
                     for dt, ym, _ in parsed)
    o_pts = " ".join(f"{xpos(dt):.1f},{ypos(op):.1f}"
                     for dt, _, op in parsed)
    # Axis
    ticks = []
    for h in (0, 6, 12, 18, 24):
        x = pad_l + (h / 24) * iw
        ticks.append(
            f'<line x1="{x:.1f}" y1="{pad_t + ih}" x2="{x:.1f}" '
            f'y2="{pad_t + ih + 4}" stroke="#6c7086"/>'
            f'<text x="{x:.1f}" y="{H - 12}" fill="#a6adc8" font-size="10" '
            f'text-anchor="middle">{h:02d}</text>'
        )
    y_ticks = []
    for pct in (0, 25, 50, 75, 100):
        y = pad_t + (1 - pct / 100) * ih
        y_ticks.append(
            f'<line x1="{pad_l - 4}" y1="{y:.1f}" x2="{pad_l}" y2="{y:.1f}" '
            f'stroke="#6c7086"/>'
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" fill="#a6adc8" '
            f'font-size="10" text-anchor="end">{pct}%</text>'
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + iw}" y2="{y:.1f}" '
            f'stroke="#313244" stroke-dasharray="2,3"/>'
        )
    now_local = datetime.now(station_tz)
    now_x = xpos(now_local) if t0 <= now_local <= t1 else None
    now_marker = ""
    if now_x is not None:
        now_marker = (
            f'<line x1="{now_x:.1f}" y1="{pad_t}" x2="{now_x:.1f}" '
            f'y2="{pad_t + ih}" stroke="#f38ba8" stroke-width="1" '
            f'stroke-dasharray="3,3"/>'
        )
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px">'
            + "".join(y_ticks) + "".join(ticks) + now_marker
            + f'<polyline points="{k_pts}" fill="none" stroke="#f5c2e7" '
              'stroke-width="2"/>'
            + f'<polyline points="{o_pts}" fill="none" stroke="#a6e3a1" '
              'stroke-width="2"/>'
            + f'<text x="{pad_l + iw}" y="{pad_t - 4}" fill="#a6adc8" '
              f'font-size="11" text-anchor="end">{label}</text>'
            + "</svg>")


MOVEMENT_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Movement tracking</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:820px;margin:0 auto}
  h1{color:#f9e2af;margin:0 0 .4rem} h2{color:#cba6f7;margin:.8rem 0 .3rem;font-size:16px}
  a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:4px 8px;text-align:right;border-bottom:1px solid #313244}
  th{color:#a6adc8;text-align:center}
  td.lbl{text-align:left;color:#cdd6f4}
  .pos{color:#a6e3a1} .neg{color:#f38ba8} .dim{color:#6c7086;font-size:12px}
  .active{color:#f9e2af;font-weight:600}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Movement tracking — {{station_id}} · {{target_date}}</h1>
<div class="card">
  <div class="dim">Series temporales de yes_mid ({{market_name}}) y our_p (modelo) durante el día. Solo estaciones pollagas activamente tienen histórico.</div>
</div>

{% if not bins %}
<div class="card"><p>Sin datos persistidos para esta estación/fecha. Solo la estación default ({{station_id}}) tiene histórico.</p></div>
{% else %}

<div class="card">
  <h2>Bin seleccionado</h2>
  <div>
  {% for b in bins %}
    <a href="?ticker={{b.ticker}}" class="{% if b.ticker == selected.ticker %}active{% endif %}">{{b.label}}</a>{% if not loop.last %} · {% endif %}
  {% endfor %}
  </div>
  {% if selected %}
    <div style="margin-top:.8rem">{{svg|safe}}</div>
    <div class="dim"><span style="color:#f5c2e7">■</span> {{market_name}} yes_mid ·
      <span style="color:#a6e3a1">■</span> nuestro our_p ·
      línea rosa punteada = ahora. Eje X = hora local (0–24).</div>
  {% endif %}
</div>

<div class="card">
  <h2>Movimiento por bin</h2>
  <table>
  <tr><th>bin</th>
      <th>{{market_name}} inicio</th><th>{{market_name}} final</th><th>Δ {{market_name}}</th>
      <th>nosotros inicio</th><th>nosotros final</th><th>Δ nosotros</th>
      <th>n</th></tr>
  {% for r in summary %}
  <tr>
    <td class="lbl"><a href="?ticker={{r.ticker}}">{{r.label}}</a></td>
    <td>{{'%.1f'|format(r.k_first*100)}}%</td>
    <td>{{'%.1f'|format(r.k_last*100)}}%</td>
    <td class="{% if r.k_delta >= 0 %}pos{% else %}neg{% endif %}">
      {{'%+.1f'|format(r.k_delta*100)}}pp</td>
    <td>{{'%.1f'|format(r.o_first*100)}}%</td>
    <td>{{'%.1f'|format(r.o_last*100)}}%</td>
    <td class="{% if r.o_delta >= 0 %}pos{% else %}neg{% endif %}">
      {{'%+.1f'|format(r.o_delta*100)}}pp</td>
    <td>{{r.n}}</td>
  </tr>
  {% endfor %}
  </table>
  <p class="dim">Δ {{market_name}} grande con Δ nosotros chico = mercado reaccionando a info nueva que nuestro modelo ya tenía. Δ {{market_name}} ≈ Δ nosotros = ambos actualizando juntos con las obs.</p>
</div>

{% endif %}
</body></html>"""


@app.route("/movement")
def movement_view():
    if _kalshi is None:
        return "kalshi module unavailable", 500
    if state is None:
        return redirect("/")
    station = state.station
    station_id = request.args.get("station", station.id).upper()
    date_str = request.args.get("date")
    if date_str:
        try:
            target_date = datetime.fromisoformat(date_str).date()
        except ValueError:
            target_date = datetime.now(station.tz).date()
    else:
        target_date = datetime.now(station.tz).date()

    hist = _kalshi.movement_history(station_id, target_date)
    bins = hist["bins"]

    summary = []
    for b in bins:
        pts = [p for p in b["points"]
               if p["yes_mid"] is not None and p["our_p"] is not None]
        if len(pts) < 1:
            continue
        first, last = pts[0], pts[-1]
        summary.append({
            "ticker": b["ticker"], "label": b["label"],
            "k_first": first["yes_mid"], "k_last": last["yes_mid"],
            "k_delta": last["yes_mid"] - first["yes_mid"],
            "o_first": first["our_p"], "o_last": last["our_p"],
            "o_delta": last["our_p"] - first["our_p"],
            "n": len(pts),
        })
    summary.sort(key=lambda r: -abs(r["k_delta"]))

    selected_ticker = request.args.get("ticker")
    selected = None
    if bins:
        if selected_ticker:
            selected = next((b for b in bins if b["ticker"] == selected_ticker), None)
        if selected is None:
            selected = max(bins, key=lambda b: len(b["points"]))

    svg = ""
    if selected:
        svg = _movement_svg(selected["points"], selected["label"], station.tz)

    return render_template_string(
        MOVEMENT_TMPL,
        station_id=station_id,
        target_date=target_date.isoformat(),
        bins=bins, summary=summary,
        selected=selected, svg=svg,
        market_name=_market_name(station_id),
    )


DEFAULT_CROSS = ["KPHX", "KLAX", "KLAS", "KLGA", "KBOS"]

PEAK_POLL_SEC = 180


def _poll_interval_for(station) -> int:
    lo, hi = PEAK_HOURS.get(station.id, (12, 16))
    hour = datetime.now(station.tz).hour
    return PEAK_POLL_SEC if lo <= hour < hi else POLL_SEC

try:
    import multi_day as _multi_day
except Exception:
    _multi_day = None


def _cross_one(sid: str, day_offset: int = 0) -> dict:
    """Build one row for the cross-station dashboard at today+day_offset."""
    try:
        station = fetch_station(sid)
        if _multi_day is None:
            return {"station": sid, "error": "multi_day unavailable"}
        d = _multi_day.day_forecast(station, day_offset)
    except Exception as e:
        return {"station": sid, "error": f"forecast: {e}"}

    target = d["target"]

    modal_bin = None
    our_p = None
    edge = None
    if _kalshi is not None:
        try:
            bins = _kalshi.fetch_bins(sid, target)
            valid = [b for b in bins if b.yes_mid is not None]
            if valid:
                modal_bin = max(valid, key=lambda b: b.yes_mid)
                our_p = _kalshi.our_p_for_bin(
                    d["daily_maxes"], modal_bin.bin_lo, modal_bin.bin_hi
                )
                edge = our_p - modal_bin.yes_mid
        except Exception:
            pass

    diff = None
    if _difficulty is not None:
        dd = _difficulty.compute(
            ens_p10=d["p10"], ens_p90=d["p90"],
            eff_n=d.get("eff_n"), total_members=d["n_members"],
            clim_percentile=None, p_notable_precip=None,
            regime_breaks=d.get("regime_breaks", 0),
        )
        diff = {"score": dd.score, "label": dd.label, "skip": dd.recommend_skip}

    maxes = d.get("daily_maxes") or []
    if maxes:
        s = sorted(maxes)
        nm = len(s)
        p50_precise = s[nm // 2]
        ml_val, ml_p = most_likely_max(maxes)
        band = d["p90"] - d["p10"]
        if ml_p >= 0.35 and band <= 2.0:
            conf_class = "conf-high"
        elif ml_p < 0.20 or band > 5.0:
            conf_class = "conf-low"
        else:
            conf_class = "conf-mid"
    else:
        p50_precise, ml_val, ml_p, conf_class = d["p50"], None, None, "conf-mid"

    div_info = None
    try:
        import divergence as _dv
        _dv.record_band(sid, target, day_offset,
                        d["p10"], d["p50"], d["p90"], d["n_members"])
        div_info = _dv.detect(sid, target)
    except Exception:
        div_info = None

    return {
        "station": sid,
        "name": station.name,
        "current_temp": d.get("current_temp"),
        "max_obs": d.get("max_obs"),
        "p10": d["p10"], "p50": d["p50"], "p90": d["p90"],
        "p50_precise": p50_precise,
        "ml_val": ml_val, "ml_p": ml_p,
        "conf_class": conf_class,
        "eff_n": d.get("eff_n"),
        "n_members": d["n_members"],
        "modal_bin": modal_bin,
        "our_p": our_p,
        "edge": edge,
        "difficulty": diff,
        "divergence": div_info,
        "target": target,
        "day_offset": day_offset,
    }


CROSS_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Cross-station</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:960px;margin:0 auto}
  h1{color:#cba6f7;margin:0 0 .4rem} a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:6px 8px;border-bottom:1px solid #313244;text-align:right}
  th{color:#a6adc8;text-align:center}
  td.lbl{text-align:left;color:#cdd6f4}
  td.bin{text-align:left;color:#f5c2e7}
  .pos{color:#a6e3a1} .neg{color:#f38ba8}
  .dim{color:#6c7086;font-size:12px}
  .err{color:#f38ba8;font-style:italic}
  .rank{font-weight:700;text-align:center}
  .rank1{color:#a6e3a1}
  .rank2{color:#f9e2af}
  .rank3{color:#fab387}
  .diff-easy{color:#a6e3a1}
  .diff-normal{color:#89b4fa}
  .diff-hard{color:#f9e2af}
  .diff-veryhard{color:#f38ba8}
  .rec{padding:1.1rem 1.2rem;border-radius:10px;margin:.8rem 0;font-size:1.05rem}
  .rec-bet{background:rgba(166,227,161,0.10);border:1px solid #2e4e3a;color:#cdd6f4}
  .rec-bet .station{color:#a6e3a1;font-weight:700;font-size:1.4rem}
  .rec-bet .side{color:#f9e2af;font-weight:700;margin-left:.4rem}
  .rec-skip{background:rgba(243,139,168,0.10);border:1px solid #5e2e3a;color:#cdd6f4}
  .rec-skip .big{color:#f38ba8;font-weight:700;font-size:1.2rem}
  .rec-meta{font-size:.85rem;color:#a6adc8;margin-top:.3rem}
  .expected{font-weight:700;font-size:15px}
  .conf-high{color:#a6e3a1}
  .conf-mid{color:#f9e2af}
  .conf-low{color:#f38ba8}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Cross-station — {{day_label}} ({{target_date}})</h1>
{% if recommendation.action == 'bet' %}
<div class="rec rec-bet">
  {{day_label}} →
  <span class="station">{{recommendation.station}}</span>
  <span class="side">{{recommendation.side}}</span>
  · {{recommendation.bin_label}}
  · <span style="color:#a6e3a1">{% if recommendation.edge_pp >= 0 %}+{% endif %}{{'%.1f'|format(recommendation.edge_pp)}}pp</span>
  · <span class="diff-easy">{{recommendation.diff_label}}</span>
  <div class="rec-meta">#1 del ranking cumple edge ≥ {{'%.0f'|format(min_edge_pp)}}pp y dificultad &lt; {{'%.0f'|format(max_diff)}}.</div>
</div>
{% else %}
<div class="rec rec-skip">
  <span class="big">⚠ mejor saltar {{day_label}}</span>
  <div class="rec-meta">Ningún candidato tiene edge ≥ {{'%.0f'|format(min_edge_pp)}}pp en día suficientemente estable (dificultad &lt; {{'%.0f'|format(max_diff)}}).</div>
</div>
{% endif %}
<div class="card">
  <div>
    <a href="?day=0{{extra_qs}}" style="color:{% if day_offset==0 %}#f9e2af{% else %}#89b4fa{% endif %}">hoy</a> ·
    <a href="?day=1{{extra_qs}}" style="color:{% if day_offset==1 %}#f9e2af{% else %}#89b4fa{% endif %}">mañana</a> ·
    <a href="?day=2{{extra_qs}}" style="color:{% if day_offset==2 %}#f9e2af{% else %}#89b4fa{% endif %}">pasado</a>
  </div>
  <div class="dim" style="margin-top:.4rem">Fetch paralelo del ensemble + Kalshi. Rank combina |edge| × (1 − dificultad/100): el #1 es "mejor edge en día más estable". Dificultad usa spread del ensemble + eff_N del reweight.
  {% if day_offset > 0 %}<br>D+{{day_offset}} no tiene observaciones todavía → ensemble raw sin reweight.{% endif %}</div>
</div>
<div class="card">
<table>
<tr><th>#</th><th>station</th>
    {% if day_offset == 0 %}<th>actual</th><th>max obs</th>{% endif %}
    <th>esperado</th>
    <th>p10</th><th>p90</th>
    {% if day_offset == 0 %}<th>eff_N</th>{% endif %}
    <th>dificultad</th>
    <th>bin modal</th>
    <th>mercado</th><th>nuestro</th><th>edge</th></tr>
{% for r in results %}
{% if r.error %}
<tr><td></td><td class="lbl">{{r.station}}</td>
    <td colspan="11" class="err">{{r.error}}</td></tr>
{% else %}
<tr>
    <td class="rank {% if r.rank == 1 %}rank1{% elif r.rank == 2 %}rank2{% elif r.rank == 3 %}rank3{% endif %}">{% if r.rank %}{{r.rank}}{% else %}—{% endif %}</td>
    <td class="lbl">{{r.station}}<br><span class="dim">{{r.name[:24]}}</span>
      {% if r.divergence and r.divergence.diverging %}
        <br><span title="{{r.divergence.message}}" style="background:#5e2e3a;color:#f9e2af;padding:.05rem .35rem;border-radius:3px;font-size:10px;font-weight:600;letter-spacing:.5px">⚠ DIVERGE</span>
      {% endif %}
    </td>
    {% if day_offset == 0 %}
      <td>{% if r.current_temp is not none %}{{'%.1f'|format(r.current_temp)}}°{% endif %}</td>
      <td>{% if r.max_obs is not none %}{{'%.1f'|format(r.max_obs)}}°{% endif %}</td>
    {% endif %}
    <td><span class="expected {{r.conf_class}}">{{'%.2f'|format(r.p50_precise)}}°</span>
        {% if r.ml_val is not none %}<br><span class="dim">{{'%.0f'|format(r.ml_p*100)}}% @ {{'%.0f'|format(r.ml_val)}}°±0.5</span>{% endif %}</td>
    <td>{{'%.0f'|format(r.p10)}}°</td>
    <td>{{'%.0f'|format(r.p90)}}°</td>
    {% if day_offset == 0 %}
      <td>{% if r.eff_n %}{{'%.1f'|format(r.eff_n)}}/{{r.n_members}}{% else %}{{r.n_members}}{% endif %}</td>
    {% endif %}
    {% if r.difficulty %}
      {% set dclass = {'fácil':'diff-easy','normal':'diff-normal','difícil':'diff-hard','muy difícil':'diff-veryhard'}[r.difficulty.label] %}
      <td class="{{dclass}}">{{r.difficulty.label}}<br><span class="dim">{{'%.0f'|format(r.difficulty.score)}}/100</span></td>
    {% else %}
      <td class="dim">—</td>
    {% endif %}
    {% if r.modal_bin %}
      <td class="bin">{{r.modal_bin.label}}</td>
      <td>{{'%.1f'|format(r.modal_bin.yes_mid * 100)}}%</td>
      <td>{{'%.1f'|format(r.our_p * 100)}}%</td>
      <td class="{% if r.edge >= 0 %}pos{% else %}neg{% endif %}">
        {{'%+.1f'|format(r.edge * 100)}}pp</td>
    {% else %}
      <td colspan="4" class="dim">sin mercado</td>
    {% endif %}
</tr>
{% endif %}
{% endfor %}
</table>
</div>
<p class="dim" style="font-size:12px">
Default: KPHX, KLAX, KLAS, KLGA, KBOS (estaciones Kalshi curadas).
Custom: <code>/cross?stations=KPHX,KLAX</code>
</p>
</body></html>"""


@app.route("/cross")
def cross_view():
    raw = request.args.get("stations", ",".join(DEFAULT_CROSS))
    stations = [s.strip().upper() for s in raw.split(",") if s.strip()]
    try:
        day_offset = max(0, min(2, int(request.args.get("day", 0))))
    except ValueError:
        day_offset = 0
    with ThreadPoolExecutor(max_workers=max(1, len(stations))) as ex:
        results = list(ex.map(lambda s: _cross_one(s, day_offset), stations))

    # Ranking combinado: edge disponible × (1 - difficulty/100).
    # Un edge grande en día estable vence a edge grande en día volátil.
    def rank_score(r):
        if r.get("error"):
            return -1.0
        edge = r.get("edge")
        if edge is None:
            return 0.0
        diff = r.get("difficulty") or {}
        diff_score = diff.get("score") or 0.0
        return abs(edge) * (1.0 - diff_score / 100.0)

    for r in results:
        r["rank_score"] = rank_score(r)
    results.sort(key=lambda r: (-r["rank_score"], r.get("station", "")))
    rank = 1
    for r in results:
        if r.get("error") or r.get("edge") is None:
            r["rank"] = None
        else:
            r["rank"] = rank
            rank += 1

    # Recomendación explícita: el #1 debe tener |edge|≥5pp y dificultad<30.
    MIN_EDGE = 0.05
    MAX_DIFF = 30.0
    recommendation = {"action": "skip"}
    winner = next((r for r in results if r.get("rank") == 1), None)
    if winner:
        edge = winner.get("edge") or 0.0
        diff = winner.get("difficulty") or {}
        diff_score = diff.get("score") or 0.0
        if abs(edge) >= MIN_EDGE and diff_score < MAX_DIFF:
            recommendation = {
                "action": "bet",
                "station": winner["station"],
                "side": "YES" if edge > 0 else "NO",
                "edge_pp": edge * 100,
                "bin_label": (winner["modal_bin"].label
                              if winner.get("modal_bin") else ""),
                "diff_label": diff.get("label") or "",
                "diff_score": diff_score,
            }

    day_labels = {0: "hoy", 1: "mañana (D+1)", 2: "pasado (D+2)"}
    target = next((r["target"] for r in results if r.get("target")), None)
    extra_qs = ""
    if raw != ",".join(DEFAULT_CROSS):
        extra_qs = f"&stations={raw}"
    return render_template_string(
        CROSS_TMPL,
        results=results,
        day_offset=day_offset,
        day_label=day_labels[day_offset],
        target_date=target.isoformat() if target else "—",
        extra_qs=extra_qs,
        recommendation=recommendation,
        min_edge_pp=MIN_EDGE * 100,
        max_diff=MAX_DIFF,
    )


POLL_STATS = {
    "started_at": datetime.now(timezone.utc),
    "last_ok_at": None,
    "last_err_at": None,
    "last_err_msg": None,
    "ok_count": 0,
    "err_count": 0,
    "recent_errors": [],  # list of (datetime, str), keep last 10
}


def _health_badge() -> tuple[str, str]:
    """Return (css_class, label) describing poll health."""
    ps = POLL_STATS
    if ps["last_ok_at"] is None:
        return "bad", "BAD"
    age = (datetime.now(timezone.utc) - ps["last_ok_at"]).total_seconds()
    if age < 2 * POLL_SEC:
        return "ok", "OK"
    if age < 5 * POLL_SEC:
        return "warn", "WARN"
    return "bad", "BAD"


SUPPORTED_STATIONS = [
    "KPHX", "KLAX", "KLAS", "KLGA", "KBOS", "KMIA", "KMDW",
    "KIAH", "KSFO", "KAUS", "KDEN", "KSAT", "KDCA", "KDFW",
    "KPHL", "KSEA", "KATL", "KMSY", "KOKC", "KMSP",
]


def _supported_stations() -> list:
    """Return [(id, name), ...] for the curated Kalshi stations.
    Includes the active station even if it's not in the curated list,
    so the dropdown never hides where the user currently is."""
    out = []
    seen = set()
    for sid in SUPPORTED_STATIONS:
        try:
            s = fetch_station(sid)
            out.append((sid, s.name))
            seen.add(sid)
        except Exception:
            pass
    if state is not None and state.station.id not in seen:
        out.insert(0, (state.station.id, state.station.name))
    return out


def _market_name(station_id: str) -> str:
    return "Kalshi"


def _build_signals(difficulty, market, external, dash, snap) -> list[dict]:
    """Strip de pills: 4-5 señales clave para lectura rápida.

    Cada item: {k: label, v: valor, kls: 'ok'|'warn'|'alert', href: opcional}
    Sólo se incluyen señales con información útil (omitimos mid/ok-mudos
    cuando no aportan nada).
    """
    out: list[dict] = []
    if difficulty is not None:
        kls = {"easy": "ok", "normal": "ok",
               "hard": "warn", "veryhard": "alert"}.get(difficulty["klass"], "warn")
        out.append({"k": "dificultad",
                    "v": f"{difficulty['label']} · {difficulty['score']:.0f}",
                    "kls": kls})
    if market and market.get("top_alert"):
        edge_pp = market["top_edge"] * 100
        side = "YES" if edge_pp > 0 else "NO"
        out.append({"k": f"edge {market['top_label']}",
                    "v": f"{edge_pp:+.1f}pp · buy {side}",
                    "kls": "alert" if abs(edge_pp) >= 8 else "warn",
                    "href": "/comparison"})
    if external and external.get("median") is not None:
        d = external["ours"] - external["median"]
        if abs(d) >= 2.0:
            out.append({"k": "vs externos",
                        "v": external.get("delta_str") or f"{d:+.1f}°F",
                        "kls": "warn"})
    if snap.regime_break_hours:
        out.append({"k": "régimen roto",
                    "v": f"{len(snap.regime_break_hours)}h obs fuera p1-p99",
                    "kls": "alert"})
    bi = snap.bias_info
    if bi and bi.get("applied"):
        regime = bi.get("regime_break", False)
        mode = bi.get("mode", "global")
        suffix = " · régimen" if regime else (f" · {mode}" if mode == "conditional" else "")
        out.append({"k": "bias",
                    "v": f"{bi['bias']:+.2f}°F aplicado{suffix}",
                    "kls": "warn",
                    "href": "/reweight"})
    if dash.get("health_class") and dash["health_class"] != "ok":
        out.append({"k": "salud",
                    "v": dash.get("health_label", "?"),
                    "kls": "alert" if dash["health_class"] == "err" else "warn",
                    "href": "/status"})
    return out


def _build_hero(dist: list[float], prev_med: float | None) -> dict:
    """Hero number: ensemble median with 2 decimals, trend vs prev snapshot,
    and confidence badge from most-likely bin probability + p10-p90 band.

    Confidence tiers (combined):
      high  = P(bin ±0.5°F) ≥ 35% AND (p90-p10) ≤ 2.0°F
      low   = P(bin ±0.5°F) < 20% OR  (p90-p10) > 5.0°F
      mid   = otherwise
    """
    n = len(dist)
    s = sorted(dist)
    med = s[n // 2]
    p10, p90 = s[int(n * 0.1)], s[int(n * 0.9)]
    band = p90 - p10
    ml_val, ml_p = most_likely_max(dist)

    if prev_med is None:
        trend_str, trend_class = "—", "hero-trend-flat"
    else:
        d = med - prev_med
        if d > 0.05:
            trend_str, trend_class = f"↑ +{d:.2f}°F", "hero-trend-up"
        elif d < -0.05:
            trend_str, trend_class = f"↓ {d:.2f}°F", "hero-trend-down"
        else:
            trend_str, trend_class = f"→ {d:+.2f}°F", "hero-trend-flat"

    if ml_p >= 0.35 and band <= 2.0:
        conf_class, conf_label = "conf-high", "alta confianza"
    elif ml_p < 0.20 or band > 5.0:
        conf_class, conf_label = "conf-low", "baja confianza"
    else:
        conf_class, conf_label = "conf-mid", "confianza media"

    if med >= 90:
        val_color = "val-color-hot"
    elif med >= 70:
        val_color = "val-color-warm"
    else:
        val_color = "val-color-cool"

    hint = ""
    if conf_class == "conf-low":
        hint = "rango amplio o pico difuso — considera esperar más polls"

    return {
        "value": f"{med:.2f}",
        "val_color": val_color,
        "trend_str": trend_str,
        "trend_class": trend_class,
        "conf_str": (f"{ml_p*100:.0f}% de caer en {ml_val:.0f}°F ±0.5°F · "
                     f"banda p10-p90 {p10:.1f}–{p90:.1f}°F ({band:.1f}°F)"),
        "conf_class": conf_class,
        "conf_label": conf_label,
        "hint": hint,
    }


def _build_external_view(station, our_med: float):
    """Junta narrativa NWS + máximas multi-modelo (Open-Meteo) en un dict
    listo para template. Devuelve None si todo falla o módulo no cargado.
    Calcula el delta vs mediana de modelos para detectar si vamos solos."""
    if _external_models is None:
        return None
    try:
        narrative = _external_models.fetch_nws_narrative(station)
    except Exception:
        narrative = None
    try:
        mm = _external_models.fetch_multi_model_max(station)
    except Exception:
        mm = None

    if narrative is None and mm is None:
        return None

    out = {"narrative": narrative, "models": None, "ours": our_med}
    if mm is not None:
        delta = our_med - mm.median
        if abs(delta) >= 3.0:
            delta_class = "ext-delta-warn"
        elif abs(delta) >= 1.5:
            delta_class = ""
        else:
            delta_class = "ext-delta-ok"
        sign = "+" if delta >= 0 else ""
        out.update({
            "models": mm.by_model,
            "median": mm.median,
            "spread": mm.spread,
            "delta_str": f"{sign}{delta:.1f}°",
            "delta_class": delta_class,
        })
    return out


def _build_dashboard(station_id: str) -> dict:
    """Compact top-bar summary: health + P&L + recent Brier + isotonic coverage."""
    hc, hl = _health_badge()
    last_ok = POLL_STATS["last_ok_at"]
    health_age = _fmt_age(last_ok) if last_ok else "nunca"

    try:
        import bets as _bets
        bs = _bets.stats(station_id)
        pnl = bs.pnl
        bets_settled = bs.n_settled
        bets_total = bs.n_total
        roi = bs.roi
    except Exception:
        pnl, bets_settled, bets_total, roi = 0.0, 0, 0, None

    brier_n = 0
    brier_ours = 0.0
    brier_kalshi = 0.0
    try:
        import calibration as _cal
        rows = _cal.list_summaries(station_id, limit=7)
        paired = [(r["our_brier"], r["kalshi_brier"]) for r in rows
                  if r.get("our_brier") is not None and r.get("kalshi_brier") is not None]
        if paired:
            brier_n = len(paired)
            brier_ours = sum(a for a, _ in paired) / brier_n
            brier_kalshi = sum(b for _, b in paired) / brier_n
    except Exception:
        pass

    iso_days = 0
    try:
        import isotonic as _iso
        cal = _iso.get(station_id)
        if cal is not None:
            iso_days = cal.n_days
    except Exception:
        pass

    return {
        "health_class": hc, "health_label": hl, "health_age": health_age,
        "pnl": pnl, "bets_settled": bets_settled, "bets_total": bets_total, "roi": roi,
        "brier_n": brier_n, "brier_ours": brier_ours, "brier_kalshi": brier_kalshi,
        "iso_days": iso_days,
    }


def _record_poll_error(msg: str) -> None:
    now = datetime.now(timezone.utc)
    POLL_STATS["last_err_at"] = now
    POLL_STATS["last_err_msg"] = msg
    POLL_STATS["err_count"] += 1
    POLL_STATS["recent_errors"].append((now, msg))
    if len(POLL_STATS["recent_errors"]) > 10:
        POLL_STATS["recent_errors"].pop(0)


STATUS_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Status</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:720px;margin:0 auto}
  h1{color:#94e2d5;margin:0 0 .4rem} h2{color:#cba6f7;margin:.8rem 0 .3rem;font-size:16px}
  a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  .badge{display:inline-block;padding:.2rem .6rem;border-radius:4px;font-weight:600;font-size:13px}
  .ok{background:#2a4a32;color:#a6e3a1} .warn{background:#4a3a24;color:#f9e2af}
  .bad{background:#4a2a32;color:#f38ba8}
  .kv{display:flex;justify-content:space-between;padding:.2rem 0;border-bottom:1px solid #313244;font-family:monospace;font-size:13px}
  .kv:last-child{border:none}
  .kv-k{color:#a6adc8}
  .err{color:#f38ba8;font-family:monospace;font-size:12px;word-break:break-word}
  .dim{color:#6c7086;font-size:12px}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Status
  <span class="badge {{health_class}}">{{health_label}}</span>
</h1>
<div class="card">
  <div class="kv"><span class="kv-k">Uptime</span><span>{{uptime}}</span></div>
  <div class="kv"><span class="kv-k">Último poll OK</span><span>{{last_ok}}</span></div>
  <div class="kv"><span class="kv-k">Último error</span><span>{{last_err}}</span></div>
  <div class="kv"><span class="kv-k">Polls OK / error</span><span>{{ok_count}} / {{err_count}}</span></div>
  <div class="kv"><span class="kv-k">Error rate</span><span>{{err_rate}}</span></div>
  <div class="kv"><span class="kv-k">Intervalo poll</span><span>{{poll_sec}}s</span></div>
  <div class="kv"><span class="kv-k">Cache TTL</span><span>{{cache_ttl}}s</span></div>
  <div class="kv"><span class="kv-k">Estación activa</span><span>{{station}}</span></div>
</div>
{% if recent_errors %}
<div class="card">
  <h2>Errores recientes</h2>
  {% for ts, msg in recent_errors %}
    <div class="kv"><span class="kv-k">{{ts}}</span><span class="err">{{msg}}</span></div>
  {% endfor %}
</div>
{% else %}
<div class="card"><p class="dim">Sin errores recientes — todo limpio.</p></div>
{% endif %}
<p class="dim">
  <b>OK</b> = último poll hace &lt; 2× intervalo ·
  <b>WARN</b> = entre 2× y 5× ·
  <b>BAD</b> = &gt; 5× o nunca.
</p>
</body></html>"""


def _fmt_age(dt) -> str:
    if dt is None:
        return "nunca"
    now = datetime.now(timezone.utc)
    s = int((now - dt).total_seconds())
    if s < 60:
        return f"hace {s}s"
    if s < 3600:
        return f"hace {s // 60}m {s % 60}s"
    if s < 86400:
        return f"hace {s // 3600}h {(s % 3600) // 60}m"
    return f"hace {s // 86400}d {(s % 86400) // 3600}h"


@app.route("/status")
def status_view():
    ps = POLL_STATS
    now = datetime.now(timezone.utc)
    uptime_s = int((now - ps["started_at"]).total_seconds())
    uptime = _fmt_age(ps["started_at"]).replace("hace ", "")

    last_ok = _fmt_age(ps["last_ok_at"])
    last_err = "—"
    if ps["last_err_at"] is not None:
        last_err = f"{_fmt_age(ps['last_err_at'])} · {ps['last_err_msg']}"

    health_class, health_label = _health_badge()

    total = ps["ok_count"] + ps["err_count"]
    err_rate = f"{100 * ps['err_count'] / total:.1f}%" if total else "—"

    recent = []
    for ts, msg in reversed(ps["recent_errors"]):
        recent.append((ts.strftime("%Y-%m-%d %H:%M:%SZ"), msg))

    return render_template_string(
        STATUS_TMPL,
        health_class=health_class, health_label=health_label,
        uptime=uptime, last_ok=last_ok, last_err=last_err,
        ok_count=ps["ok_count"], err_count=ps["err_count"], err_rate=err_rate,
        poll_sec=_poll_interval_for(state.station) if state else POLL_SEC, cache_ttl=600,
        station=state.station.id if state else "—",
        recent_errors=recent,
    )


REWEIGHT_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Reweight · {{station}}</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:900px;margin:0 auto}
  h1{color:#94e2d5;margin:0 0 .4rem} h2{color:#cba6f7;margin:.8rem 0 .3rem;font-size:16px}
  a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  .kv{display:flex;justify-content:space-between;padding:.2rem 0;border-bottom:1px solid #313244;font-family:monospace;font-size:13px}
  .kv:last-child{border:none} .kv-k{color:#a6adc8}
  table{border-collapse:collapse;width:100%;font-family:monospace;font-size:13px}
  th,td{padding:.3rem .5rem;text-align:right;border-bottom:1px solid #313244}
  th{color:#a6adc8;text-align:right} td.h{text-align:left;color:#cba6f7}
  tr.broken td{background:#3a1e24;color:#f38ba8}
  tr.peak td.sig{color:#a6e3a1;font-weight:600}
  .dim{color:#6c7086;font-size:12px}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Reweight diagnostics · {{station}}</h1>
<div class="card">
  <div class="kv"><span class="kv-k">Residual hours</span><span>{{residual_hours}}</span></div>
  <div class="kv"><span class="kv-k">eff_N / total</span><span>{{eff_n}} / {{total_members}} ({{eff_ratio}})</span></div>
  <div class="kv"><span class="kv-k">Regime-break hours</span><span>{{regime_break_hours or '—'}}</span></div>
  <div class="kv"><span class="kv-k">Peak window (local)</span><span>{{peak_lo}}-{{peak_hi}}h</span></div>
</div>
<div class="card">
  <h2>Ajuste por sesgo histórico</h2>
  {% if bias_info %}
    {% if bias_info.applied %}
      <div class="kv"><span class="kv-k">Corrección aplicada</span>
        <span style="color:{% if bias_correction_f > 0 %}#f9e2af{% else %}#89dceb{% endif %};font-weight:600">
          {{ '%+.2f'|format(-bias_correction_f) }}°F al ensemble
        </span>
      </div>
      <div class="kv"><span class="kv-k">Bias detectado (early_pred − actual)</span>
        <span>{{ '%+.2f'|format(bias_correction_f) }}°F</span>
      </div>
    {% else %}
      <div class="kv"><span class="kv-k">Corrección</span><span class="dim">no aplicada · {{bias_info.reason}}</span></div>
    {% endif %}
    <div class="kv"><span class="kv-k">Modo</span>
      {% if bias_info.mode == 'conditional' %}
        <span style="color:#cba6f7;font-weight:600">condicional</span>
        <span class="dim">· régimen {{bias_info.regime}} · pct {{ '%.0f'|format(bias_info.today_percentile) }}</span>
      {% else %}
        <span class="dim">global (sin clim. o bucket fino)</span>
      {% endif %}
    </div>
    <div class="kv"><span class="kv-k">N días usados</span><span>{{bias_info.n}}</span></div>
    {% if bias_info.mode == 'conditional' and bias_info.global_bias is defined %}
    <div class="kv"><span class="kv-k">Bias global (referencia)</span>
      <span class="dim">{{ '%+.2f'|format(bias_info.global_bias) }}°F · n={{bias_info.global_n}}</span>
    </div>
    {% endif %}
    {% if bias_info.samples %}
    <table style="margin-top:.4rem">
      <tr><th style="text-align:left">fecha</th><th>error (pred − obs)</th></tr>
      {% for d, e in bias_info.samples %}
      <tr><td class="h">{{d}}</td><td style="color:{% if e > 0 %}#f9e2af{% elif e < 0 %}#89dceb{% else %}#a6adc8{% endif %}">{{ '%+.2f'|format(e) }}°F</td></tr>
      {% endfor %}
    </table>
    {% endif %}
  {% else %}
    <p class="dim">Tracker no disponible (sin datos o error de lectura).</p>
  {% endif %}
  <p class="dim" style="margin-top:.5rem">
    Promedio exponencial (α=0.4) de los últimos 7 días settleados con snapshot temprano.
    Se aplica solo si |bias| ≥ 0.7°F y hay ≥4 días.
  </p>
</div>
<div class="card">
  <h2>Per-hour breakdown</h2>
  {% if diagnostics %}
  <table>
    <tr><th>hora</th><th>obs</th><th>ens p10</th><th>p50</th><th>p90</th><th>σ</th><th>n</th><th>∈ p1-p99</th></tr>
    {% for d in diagnostics %}
    <tr class="{% if d.out_of_range %}broken{% endif %} {% if peak_lo <= d.hour < peak_hi %}peak{% endif %}">
      <td class="h">{{'%02d'|format(d.hour)}}h</td>
      <td>{{'%.1f'|format(d.obs)}}°F</td>
      <td>{{'%.1f'|format(d.p10) if d.p10 is not none else '—'}}</td>
      <td>{{'%.1f'|format(d.p50) if d.p50 is not none else '—'}}</td>
      <td>{{'%.1f'|format(d.p90) if d.p90 is not none else '—'}}</td>
      <td class="sig">{{'%.1f'|format(d.sigma)}}</td>
      <td>{{d.n_members}}</td>
      <td>{% if d.out_of_range %}✗ ROTO{% else %}✓{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="dim">Aún no hay obs matched hoy (demasiado temprano).</p>
  {% endif %}
</div>
<p class="dim">
  Las horas de pico usan σ=1.5°F (más peso por observación, color verde). Filas
  rojas = obs fuera del rango p1-p99 del ensemble en esa hora (ruptura de
  régimen). ≥2 filas rojas dispara push + fuerza "muy difícil" en /cross.
</p>
</body></html>"""


@app.route("/reweight")
def reweight_view():
    if state is None or state.last_snapshot is None:
        return ("No snapshot yet", 503)
    snap = state.last_snapshot
    total = len(snap.ensemble_raw_maxes) or len(snap.ensemble_daily_maxes)
    eff_ratio = f"{(snap.ensemble_eff_n / total * 100):.0f}%" if (
        snap.ensemble_eff_n and total) else "—"
    lo, hi = PEAK_HOURS.get(state.station.id, (12, 16))
    return render_template_string(
        REWEIGHT_TMPL,
        station=state.station.id,
        residual_hours=snap.ensemble_residual_hours,
        eff_n=f"{snap.ensemble_eff_n:.1f}" if snap.ensemble_eff_n else "—",
        total_members=total,
        eff_ratio=eff_ratio,
        regime_break_hours=", ".join(f"{h:02d}h" for h in snap.regime_break_hours),
        peak_lo=lo, peak_hi=hi,
        diagnostics=snap.reweight_diagnostics,
        bias_correction_f=getattr(snap, "bias_correction_f", 0.0),
        bias_info=getattr(snap, "bias_info", None),
    )


HISTORY_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Historial diario</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:1100px;margin:0 auto}
  h1{color:#94e2d5;margin:0 0 .4rem}
  a{color:#89b4fa}
  .dim{color:#6c7086;font-size:12px}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:.35rem .5rem;text-align:right;border-bottom:1px solid #313244}
  th{color:#cba6f7;font-weight:600;text-align:right}
  th:first-child,td:first-child{text-align:left}
  th:nth-child(2),td:nth-child(2){text-align:left}
  tr:hover{background:#181825}
  .good{color:#a6e3a1} .bad{color:#f38ba8} .neu{color:#a6adc8}
  .win{background:#2a4a32;color:#a6e3a1;padding:.1rem .4rem;border-radius:3px;font-size:11px}
  .lose{background:#4a2a32;color:#f38ba8;padding:.1rem .4rem;border-radius:3px;font-size:11px}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Historial diario · {{station_id}}</h1>
<p class="dim">
  Un row por día settleado. <b>B-nuestro/B-{{market_name}}</b> = Brier del día (&lt; mejor).
  <b>p-gana</b> = nuestra P para el bin que contenía el max real · idealmente alto.
  <b>Edge</b> = mayor |nuestra_p − {{market_name}}| encontrado ese día, y si nuestra dirección acertó.
</p>
{% if rows %}
<div class="card">
<table>
<thead><tr>
  <th>Fecha</th><th>Max</th>
  <th>n-ours</th><th>B-nuestro</th>
  <th>n-M</th><th>B-{{market_name}}</th>
  <th>Bin gana</th><th>p-ours</th><th>p-K</th>
  <th>Edge</th><th>Ok?</th>
</tr></thead>
<tbody>
{% for r in rows %}
<tr>
  <td>{{r.date}}</td>
  <td>{{ "%.1f"|format(r.actual_max_f) }}°F</td>
  <td>{{r.our_n}}</td>
  <td class="{{r.our_class}}">{{r.our_brier_s}}</td>
  <td>{{r.kalshi_n or "—"}}</td>
  <td class="{{r.kalshi_class}}">{{r.kalshi_brier_s}}</td>
  <td>{{r.winning_bin_label or "—"}}</td>
  <td>{{r.our_p_winning_s}}</td>
  <td>{{r.kalshi_p_winning_s}}</td>
  <td>{{r.best_edge_s}}{% if r.best_edge_bin_label %} <span class="dim">({{r.best_edge_bin_label}})</span>{% endif %}</td>
  <td>{% if r.best_edge_correct == 1 %}<span class="win">✓</span>{% elif r.best_edge_correct == 0 %}<span class="lose">✗</span>{% else %}—{% endif %}</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
<div class="card">
  <b>Agregado:</b>
  días = {{agg.n}} ·
  B-nuestro medio = {{agg.our_brier_mean}} ·
  B-{{market_name}} medio = {{agg.kalshi_brier_mean}} ·
  días que ganamos a {{market_name}} = {{agg.beats_kalshi}}/{{agg.n_with_kalshi}} ·
  edge-calls correctos = {{agg.edge_correct}}/{{agg.edge_total}}
</div>
{% else %}
<p class="dim">Aún no hay días settleados. Aparecerán aquí cuando Open-Meteo publique el max histórico (normalmente al día siguiente).</p>
{% endif %}
</body></html>"""


EXPORT_TABLES = {
    "snapshots": {
        "db": "calibration",
        "sql": """SELECT station_id, date, snapshot_time, slot, is_auto,
                         expr, op, threshold, bin_half, predicted_p, outcome
                  FROM prediction_snapshots
                  WHERE (? IS NULL OR station_id=?)
                    AND (? IS NULL OR date>=?)
                  ORDER BY snapshot_time""",
    },
    "market_prices": {
        "db": "kalshi",
        "sql": """SELECT fetched_at, station_id, date, ticker,
                         bin_lo, bin_hi, label,
                         yes_bid, yes_ask, yes_mid, our_p
                  FROM market_prices
                  WHERE (? IS NULL OR station_id=?)
                    AND (? IS NULL OR date>=?)
                  ORDER BY fetched_at""",
    },
    "day_summary": {
        "db": "calibration",
        "sql": """SELECT station_id, date, actual_max_f,
                         our_n, our_brier, kalshi_n, kalshi_brier,
                         winning_bin_label, our_p_winning, kalshi_p_winning,
                         best_edge_abs, best_edge_bin_label,
                         best_edge_our_p, best_edge_kalshi_p,
                         best_edge_correct, computed_at
                  FROM day_summary
                  WHERE (? IS NULL OR station_id=?)
                    AND (? IS NULL OR date>=?)
                  ORDER BY date""",
    },
    "day_outcomes": {
        "db": "calibration",
        "sql": """SELECT station_id, date, max_obs_f, settled_at
                  FROM day_outcomes
                  WHERE (? IS NULL OR station_id=?)
                    AND (? IS NULL OR date>=?)
                  ORDER BY date""",
    },
    "simulated_bets": {
        "db": "calibration",
        "sql": """SELECT id, station_id, date, ticker, bin_lo, bin_hi,
                         bin_label, side, our_p, kalshi_p, edge_pp,
                         stake, entry_price, contracts, entered_at,
                         outcome, won, payoff, pnl, settled_at
                  FROM simulated_bets
                  WHERE (? IS NULL OR station_id=?)
                    AND (? IS NULL OR date>=?)
                  ORDER BY entered_at""",
    },
}


def _export_rows(table: str, station_id: str | None, since: str | None):
    import sqlite3, csv, io
    spec = EXPORT_TABLES[table]
    if spec["db"] == "kalshi":
        from kalshi import DB_PATH as DBP
    else:
        from calibration import DB_PATH as DBP
    c = sqlite3.connect(DBP)
    cur = c.execute(spec["sql"],
                    (station_id, station_id, since, since))
    cols = [d[0] for d in cur.description]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(cur.fetchall())
    c.close()
    return buf.getvalue()


@app.route("/export/<table>.csv")
def export_csv(table: str):
    if table not in EXPORT_TABLES:
        return "unknown table", 404
    station_id = request.args.get("station") or None
    since = request.args.get("since") or None
    try:
        body = _export_rows(table, station_id, since)
    except Exception as e:
        return f"error: {e}", 500
    fname = f"{table}"
    if station_id:
        fname += f"_{station_id}"
    if since:
        fname += f"_from_{since}"
    fname += ".csv"
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{fname}"'})


EXPORT_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Export CSV</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:820px;margin:0 auto}
  h1{color:#94e2d5;margin:0 0 .4rem} h2{color:#cba6f7;margin:.8rem 0 .3rem;font-size:16px}
  a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  code{background:#181825;padding:.1rem .4rem;border-radius:3px;font-size:13px}
  .dim{color:#6c7086;font-size:12px}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-top:.4rem}
  th,td{padding:.35rem .5rem;text-align:left;border-bottom:1px solid #313244}
  th{color:#cba6f7}
  input,select{background:#181825;color:#cdd6f4;border:1px solid #313244;border-radius:4px;padding:.3rem .5rem;font-size:13px}
  button{background:#89b4fa;color:#11111b;border:none;padding:.4rem .8rem;border-radius:4px;font-weight:600;cursor:pointer}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Export CSV</h1>
<p class="dim">Descarga raw data para análisis offline (pandas, DuckDB, etc.).
Filtros opcionales: estación, fecha mínima (<code>YYYY-MM-DD</code>).</p>

<div class="card">
<form method="get" action="" onsubmit="event.preventDefault();go()">
<div>Tabla:
  <select id="table">
    {% for t in tables %}<option value="{{t}}">{{t}}</option>{% endfor %}
  </select>
</div>
<div style="margin-top:.4rem">Estación (opcional):
  <input id="station" placeholder="ej. KPHX" size="10">
</div>
<div style="margin-top:.4rem">Desde (opcional, YYYY-MM-DD):
  <input id="since" placeholder="2026-04-01" size="14">
</div>
<div style="margin-top:.8rem">
  <button type="submit">Descargar</button>
</div>
</form>
<script>
function go(){
  var t=document.getElementById('table').value;
  var s=document.getElementById('station').value.trim();
  var d=document.getElementById('since').value.trim();
  var q=[];
  if(s)q.push('station='+encodeURIComponent(s));
  if(d)q.push('since='+encodeURIComponent(d));
  var url='/export/'+t+'.csv'+(q.length?'?'+q.join('&'):'');
  window.location.href=url;
}
</script>
</div>

<div class="card">
<h2>Tablas disponibles</h2>
<table>
<tr><th>nombre</th><th>descripción</th></tr>
<tr><td>snapshots</td><td>Cada predicción (op, threshold, predicted_p, outcome) por poll · base para reliability</td></tr>
<tr><td>market_prices</td><td>yes_bid/ask/mid del mercado (Kalshi) + nuestra our_p por bin, por poll</td></tr>
<tr><td>day_summary</td><td>Un row por día settleado (max real, briers, best edge)</td></tr>
<tr><td>day_outcomes</td><td>Max observado (NWS CLI con fallback a Open-Meteo) por (station, date)</td></tr>
</table>
<p class="dim" style="margin-top:.6rem">
  Ejemplos URL directa:<br>
  <code>/export/market_prices.csv?station=KPHX</code><br>
  <code>/export/day_summary.csv?since=2026-04-01</code>
</p>
</div>
</body></html>"""


PRECIP_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Precipitación ensemble</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:900px;margin:0 auto}
  h1{color:#89dceb;margin:0 0 .4rem} a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  .kpi{display:inline-block;margin-right:1.2rem}
  .kpi b{color:#89dceb;font-size:20px;display:block}
  .dim{color:#6c7086;font-size:12px}
  table{width:100%;border-collapse:collapse;font-family:monospace;font-size:13px}
  th,td{padding:4px 8px;text-align:right;border-bottom:1px solid #313244}
  th{color:#a6adc8;font-weight:normal}
  th:first-child,td:first-child{text-align:left}
  .bar{position:relative;height:10px;background:#313244;border-radius:2px;overflow:hidden;width:100px}
  .bar-f{position:absolute;top:0;left:0;height:100%;background:#89dceb}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Precipitación ensemble — {{station}}</h1>
<p class="dim">
  Agregado por día sobre los 31 miembros del ensemble GFS. Umbrales:
  <b>any</b> = cualquier traza (>0.1mm) · <b>notable</b> ≈ 0.1in (2.5mm) ·
  <b>heavy</b> ≈ 0.4in (10mm). Snow: cm.
</p>
{% for d in days %}
<div class="card">
  <h2 style="margin:.2rem 0;color:#cba6f7;font-size:16px">
    {{d.label}} — {{d.target}}
  </h2>
  <div class="kpi"><span class="dim">n miembros</span><b>{{d.n_members}}</b></div>
  <div class="kpi"><span class="dim">P(any)</span><b>{{d.p_any}}</b></div>
  <div class="kpi"><span class="dim">P(notable)</span><b>{{d.p_notable}}</b></div>
  <div class="kpi"><span class="dim">P(heavy)</span><b>{{d.p_heavy}}</b></div>
  <div class="kpi"><span class="dim">Esperado</span><b>{{d.expected_mm}}</b></div>
  <div class="kpi"><span class="dim">p10 / p50 / p90</span><b>{{d.p_pct_mm}}</b></div>
  {% if d.p_any_snow and d.p_any_snow != '0%' %}
    <div style="margin-top:.6rem;color:#b4befe">
      Nieve: P(any)={{d.p_any_snow}} · P(notable)={{d.p_notable_snow}} ·
      esperado {{d.expected_snow_cm}} cm
    </div>
  {% endif %}
  <div style="margin-top:.6rem">
    {% for t, p in d.bar_items %}
      <div style="display:flex;align-items:center;gap:.5rem;font-size:12px">
        <span style="width:80px" class="dim">{{t}}</span>
        <div class="bar"><div class="bar-f" style="width:{{p*100}}%"></div></div>
        <span class="dim">{{'%.0f'|format(p*100)}}%</span>
      </div>
    {% endfor %}
  </div>
</div>
{% endfor %}
</body></html>"""


@app.route("/precip")
def precip_view():
    if state is None:
        return redirect("/")
    from predictor import build_precip_summary, PRECIP_ANY, PRECIP_NOTABLE, \
        PRECIP_HEAVY
    def _pct(x):
        return "—" if x is None else f"{100*x:.0f}%"
    days = []
    for i, lbl in enumerate(["hoy", "mañana", "pasado"]):
        try:
            s = build_precip_summary(state.station, i)
        except Exception as e:
            return f"error: {e}", 500
        days.append({
            "label": lbl,
            "target": s["target"].isoformat(),
            "n_members": s["n_members"],
            "p_any": _pct(s["p_any_precip"]),
            "p_notable": _pct(s["p_notable_precip"]),
            "p_heavy": _pct(s["p_heavy_precip"]),
            "expected_mm": f"{s['expected_mm']:.2f} mm",
            "p_pct_mm": f"{s['p10_mm']:.1f} / {s['p50_mm']:.1f} / {s['p90_mm']:.1f} mm",
            "p_any_snow": _pct(s["p_any_snow"]),
            "p_notable_snow": _pct(s["p_notable_snow"]),
            "expected_snow_cm": f"{s['expected_snow_cm']:.2f}",
            "bar_items": [
                (f">{PRECIP_ANY}mm (any)", s["p_any_precip"] or 0),
                (f">{PRECIP_NOTABLE}mm (0.1in)", s["p_notable_precip"] or 0),
                (f">{PRECIP_HEAVY}mm (0.4in)", s["p_heavy_precip"] or 0),
            ],
        })
    return render_template_string(PRECIP_TMPL, station=state.station.id, days=days)


@app.route("/export")
def export_view():
    return render_template_string(EXPORT_TMPL,
                                  tables=list(EXPORT_TABLES.keys()))


BETS_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Simulador de ganancias</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:1200px;margin:0 auto}
  h1{color:#94e2d5;margin:0 0 .4rem} h2{color:#cba6f7;margin:.8rem 0 .3rem;font-size:16px}
  a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  .kpi{display:inline-block;margin-right:1.2rem}
  .kpi b{color:#94e2d5;font-size:20px;display:block}
  .dim{color:#6c7086;font-size:12px}
  .good{color:#a6e3a1} .bad{color:#f38ba8} .neu{color:#a6adc8}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:.35rem .5rem;text-align:right;border-bottom:1px solid #313244}
  th{color:#cba6f7;font-weight:600;text-align:right}
  th:nth-child(-n+4),td:nth-child(-n+4){text-align:left}
  tr:hover{background:#181825}
  .pill{display:inline-block;padding:.1rem .5rem;border-radius:3px;font-size:11px;font-weight:600}
  .yes{background:#2a4a32;color:#a6e3a1} .no{background:#4a2a32;color:#f38ba8}
  .win{background:#2a4a32;color:#a6e3a1;padding:.1rem .4rem;border-radius:3px;font-size:11px}
  .lose{background:#4a2a32;color:#f38ba8;padding:.1rem .4rem;border-radius:3px;font-size:11px}
  .open{background:#313244;color:#a6adc8;padding:.1rem .4rem;border-radius:3px;font-size:11px}
  select{background:#181825;color:#cdd6f4;border:1px solid #313244;border-radius:4px;padding:.3rem}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Simulador de ganancias · {{station_id}}</h1>
<p class="dim">
  Apuesta hipotética de <b>${{stake}}</b> cada vez que encontramos |edge| ≥ {{thr}}pp
  contra el mercado (Kalshi). Lado <b>yes</b> si nuestro modelo dice más probable;
  <b>no</b> si menos. Payoff al settlear. <b>No es dinero real.</b>
</p>

<div class="card">
  <div class="kpi"><span class="dim">Total bets</span><b>{{s.n_total}}</b></div>
  <div class="kpi"><span class="dim">Settled</span><b>{{s.n_settled}}</b></div>
  <div class="kpi"><span class="dim">Wins</span><b>{{s.n_wins}}</b></div>
  <div class="kpi"><span class="dim">Win rate</span><b>{{win_rate_s}}</b></div>
  <div class="kpi"><span class="dim">Total stake</span><b>${{ "%.2f"|format(s.total_stake) }}</b></div>
  <div class="kpi"><span class="dim">Total payoff</span><b>${{ "%.2f"|format(s.total_payoff) }}</b></div>
  <div class="kpi"><span class="dim">P&amp;L</span><b class="{{pnl_class}}">${{ "%.2f"|format(s.pnl) }}</b></div>
  <div class="kpi"><span class="dim">ROI</span><b class="{{pnl_class}}">{{roi_s}}</b></div>
</div>

<form method="get" style="margin:.4rem 0">
  Filtro:
  <select name="only" onchange="this.form.submit()">
    <option value="all"    {{'selected' if only=='all' else ''}}>todos</option>
    <option value="open"   {{'selected' if only=='open' else ''}}>abiertos</option>
    <option value="settled"{{'selected' if only=='settled' else ''}}>settleados</option>
  </select>
  Estación:
  <select name="station" onchange="this.form.submit()">
    <option value="">— todas —</option>
    {% for sid in known_stations %}
      <option value="{{sid}}" {{'selected' if sid==station_id else ''}}>{{sid}}</option>
    {% endfor %}
  </select>
</form>

<div class="card">
<table>
<thead><tr>
  <th>Entrada</th><th>Est.</th><th>Fecha</th><th>Bin</th>
  <th>Lado</th><th>our p</th><th>K p</th><th>edge</th>
  <th>Stake</th><th>Entry $</th><th>Contracts</th>
  <th>Outc.</th><th>Payoff</th><th>P&amp;L</th>
</tr></thead>
<tbody>
{% for b in bets %}
<tr>
  <td class="dim">{{b.entered_at[:16]}}</td>
  <td>{{b.station_id}}</td>
  <td>{{b.date}}</td>
  <td>{{b.bin_label or '—'}}</td>
  <td><span class="pill {{b.side}}">{{b.side}}</span></td>
  <td>{{ "%.0f"|format(b.our_p*100) }}%</td>
  <td>{{ "%.0f"|format(b.kalshi_p*100) }}%</td>
  <td>{{ "%+.1f"|format(b.edge_pp) }}pp</td>
  <td>${{ "%.0f"|format(b.stake) }}</td>
  <td>{{ "%.2f"|format(b.entry_price) }}</td>
  <td>{{ "%.1f"|format(b.contracts) }}</td>
  <td>
    {% if b.outcome is none %}<span class="open">open</span>
    {% elif b.won %}<span class="win">WON</span>
    {% else %}<span class="lose">LOST</span>{% endif %}
  </td>
  <td>{% if b.payoff is not none %}${{ "%.2f"|format(b.payoff) }}{% else %}—{% endif %}</td>
  <td class="{{'good' if b.pnl and b.pnl>0 else ('bad' if b.pnl and b.pnl<0 else 'neu')}}">
    {% if b.pnl is not none %}${{ "%+.2f"|format(b.pnl) }}{% else %}—{% endif %}
  </td>
</tr>
{% endfor %}
</tbody>
</table>
{% if not bets %}<p class="dim">Sin bets registrados en este filtro.</p>{% endif %}
</div>
</body></html>"""


@app.route("/bets")
def bets_view():
    import bets as _bets
    station_id = request.args.get("station") or None
    only = request.args.get("only") or "all"
    rows = _bets.list_bets(station_id, only=only, limit=300)
    s = _bets.stats(station_id)
    pnl_class = "good" if s.pnl > 0 else ("bad" if s.pnl < 0 else "neu")
    roi_s = f"{100*s.roi:+.1f}%" if s.roi is not None else "—"
    win_rate_s = f"{100*s.win_rate:.0f}%" if s.win_rate is not None else "—"
    # Known stations = union of bets' stations + active one
    known = sorted({r["station_id"] for r in _bets.list_bets(limit=10000)})
    if state and state.station.id not in known:
        known = sorted(set(known) | {state.station.id})
    return render_template_string(
        BETS_TMPL,
        bets=rows, s=s, pnl_class=pnl_class, roi_s=roi_s,
        win_rate_s=win_rate_s, only=only,
        station_id=station_id or "todas",
        known_stations=known,
        thr=int(_bets.EDGE_THR * 100), stake=int(_bets.STAKE),
    )


NOTIFY_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Push notifications</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:720px;margin:0 auto}
  h1{color:#94e2d5;margin:0 0 .4rem} h2{color:#cba6f7;margin:.8rem 0 .3rem;font-size:16px}
  a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  code{background:#181825;padding:.1rem .4rem;border-radius:3px;font-size:13px}
  pre{background:#181825;padding:.6rem;border-radius:4px;overflow-x:auto;font-size:13px}
  .badge{display:inline-block;padding:.2rem .6rem;border-radius:4px;font-weight:600;font-size:13px}
  .on{background:#2a4a32;color:#a6e3a1} .off{background:#4a3a24;color:#f9e2af}
  .dim{color:#6c7086;font-size:12px}
  ul{line-height:1.7}
</style></head><body>
<p><a href="/">&larr; volver</a></p>
<h1>Push notifications
  <span class="badge {{status_class}}">{{status_label}}</span>
</h1>
<div class="card">
  <div><b>Estado:</b> {{status_msg}}</div>
  <div><b>Topic actual:</b> <code>{{topic}}</code></div>
  <div><b>Threshold edge:</b> |edge| ≥ {{thr}}pp</div>
  <div><b>Dedupe:</b> 1 push por bin/día · 1 push al settlear</div>
</div>
{% if not enabled %}
<div class="card">
  <h2>Cómo activar</h2>
  <ol>
    <li>Instala la app <b>ntfy</b> en iPad/iPhone (App Store, gratis).</li>
    <li>En la app → Add Subscription → topic único tuyo, por ejemplo
        <code>weather-predictor-{{suggestion}}</code> (usa algo no-adivinable,
        el topic es la auth).</li>
    <li>En la laptop (ThinkPad), edita el launch del server para setear
        la env var:
        <pre>export NTFY_TOPIC=weather-predictor-{{suggestion}}
./venv/bin/python3 predictor_web.py</pre></li>
    <li>Reinicia el server. Vuelve a esta página; el badge debería poner <b>ACTIVO</b>.</li>
    <li>Prueba: <a href="/notify/test">enviar notificación de prueba</a>.</li>
  </ol>
  <p class="dim">ntfy.sh es gratis, sin cuenta, pub/sub por topic. Quien
  adivine tu topic ve las notifs — por eso el sufijo random.</p>
</div>
{% else %}
<div class="card">
  <h2>Probar</h2>
  <p><a href="/notify/test">→ enviar push de prueba</a></p>
</div>
{% endif %}
</body></html>"""


@app.route("/notify")
def notify_view():
    import notify as _notify
    import uuid
    topic = _notify.TOPIC or "—"
    enabled = _notify.enabled()
    status_class = "on" if enabled else "off"
    status_label = "ACTIVO" if enabled else "INACTIVO"
    status_msg = ("Push habilitado, alerts de edge y settle activos."
                  if enabled
                  else "NTFY_TOPIC no seteada; no se envía nada.")
    return render_template_string(
        NOTIFY_TMPL,
        topic=topic, enabled=enabled, thr=int(EDGE_ALERT_THR * 100),
        status_class=status_class, status_label=status_label,
        status_msg=status_msg, suggestion=uuid.uuid4().hex[:10])


ALERTS_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Alertas NWS</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:1rem;max-width:960px;margin:0 auto}
  h1{color:#cba6f7;margin:0 0 .4rem} a{color:#89b4fa}
  .card{background:#1e1e2e;border-radius:8px;padding:1rem;margin:.8rem 0}
  .alert{border-left:4px solid #6c7086;padding:.6rem .8rem;margin:.5rem 0;border-radius:4px;background:#181825}
  .alert.sev-Extreme,.alert.sev-Severe{border-left-color:#f38ba8}
  .alert.sev-Moderate{border-left-color:#f9e2af}
  .alert.sev-Minor{border-left-color:#89b4fa}
  .evt{font-weight:700;color:#f9e2af}
  .meta{font-size:.8rem;color:#a6adc8;margin-top:.25rem}
  .head{margin-top:.35rem;color:#cdd6f4;font-size:.92rem}
  .area{margin-top:.25rem;color:#a6adc8;font-size:.82rem}
  .dim{color:#6c7086;font-size:12px}
  .ok{color:#a6e3a1}
  .empty{color:#6c7086;font-style:italic;padding:.5rem 0}
</style></head><body>
<p><a href="/">&larr; volver</a> · <a href="/notify">configurar push</a></p>
<h1>Alertas NWS activas</h1>
<p class="dim">Poll al endpoint público <code>api.weather.gov/alerts/active</code>
(rate-limit 15 min/estación). Filtra eventos irrelevantes para max diario
(Coastal Flood, Rip Current, Small Craft, Marine...). Severo o urgente
inmediato = push prioritario.</p>
{% for sid, info in per_station.items() %}
<div class="card">
  <h3 style="margin:0">{{sid}} <span class="dim">({{info.name}})</span></h3>
  {% if info.error %}
    <div class="empty">error: {{info.error}}</div>
  {% elif not info.alerts %}
    <div class="empty ok">✓ sin alerts activos</div>
  {% else %}
    {% for a in info.alerts %}
    <div class="alert sev-{{a.severity}}">
      <span class="evt">{{a.event}}</span>
      <span class="dim"> · {{a.severity}} · {{a.urgency}} · {{a.certainty}}</span>
      {% if a.headline %}<div class="head">{{a.headline}}</div>{% endif %}
      {% if a.area_desc %}<div class="area">Área: {{a.area_desc}}</div>{% endif %}
      <div class="meta">sender: {{a.sender_name}}
        {% if a.ends %}· termina: {{a.ends}}{% endif %}</div>
    </div>
    {% endfor %}
  {% endif %}
</div>
{% endfor %}
</body></html>"""


@app.route("/alerts")
def alerts_view():
    if _weather_alerts is None:
        return "weather_alerts module unavailable", 500
    per_station = {}
    for sid in DEFAULT_CROSS:
        try:
            st = fetch_station(sid)
            alerts = _weather_alerts.fetch_active(st)
            per_station[sid] = {"name": st.name, "alerts": alerts, "error": None}
        except Exception as e:
            per_station[sid] = {"name": "—", "alerts": [], "error": str(e)}
    return render_template_string(ALERTS_TMPL, per_station=per_station)


@app.route("/notify/test")
def notify_test():
    import notify as _notify
    if not _notify.enabled():
        return "NTFY_TOPIC no seteada. Ver /notify.", 400
    ok = _notify.send("Prueba", "weather-predictor dice hola.",
                      priority="default", tags=["wave"])
    return ("Enviado." if ok else "Falló el envío."), (200 if ok else 500)


def _fmt_brier(b):
    return f"{b:.3f}" if b is not None else "—"


@app.route("/history")
def history_view():
    import calibration as _calibration
    station_id = request.args.get("station", state.station.id if state else "KPHX")
    raw = _calibration.list_summaries(station_id, limit=90)
    rows = []
    n_kalshi = 0
    beats = 0
    edge_total = 0
    edge_ok = 0
    our_briers = []
    kalshi_briers = []
    for r in raw:
        our_b = r["our_brier"]
        k_b = r["kalshi_brier"]
        if our_b is not None:
            our_briers.append(our_b)
        if k_b is not None:
            kalshi_briers.append(k_b)
        our_class = "neu"
        kalshi_class = "neu"
        if our_b is not None and k_b is not None:
            n_kalshi += 1
            if our_b < k_b:
                beats += 1
                our_class = "good"
                kalshi_class = "bad"
            elif our_b > k_b:
                our_class = "bad"
                kalshi_class = "good"
        best_edge_s = (f"{100*r['best_edge_abs']:.1f}pp"
                       if r["best_edge_abs"] is not None else "—")
        if r["best_edge_correct"] is not None:
            edge_total += 1
            edge_ok += r["best_edge_correct"]
        rows.append({
            **r,
            "our_brier_s": _fmt_brier(our_b),
            "kalshi_brier_s": _fmt_brier(k_b),
            "our_p_winning_s": (f"{100*r['our_p_winning']:.0f}%"
                                if r["our_p_winning"] is not None else "—"),
            "kalshi_p_winning_s": (f"{100*r['kalshi_p_winning']:.0f}%"
                                   if r["kalshi_p_winning"] is not None else "—"),
            "best_edge_s": best_edge_s,
            "our_class": our_class,
            "kalshi_class": kalshi_class,
        })
    agg = {
        "n": len(rows),
        "our_brier_mean": (f"{sum(our_briers)/len(our_briers):.3f}"
                           if our_briers else "—"),
        "kalshi_brier_mean": (f"{sum(kalshi_briers)/len(kalshi_briers):.3f}"
                              if kalshi_briers else "—"),
        "beats_kalshi": beats,
        "n_with_kalshi": n_kalshi,
        "edge_correct": edge_ok,
        "edge_total": edge_total,
    }
    return render_template_string(HISTORY_TMPL,
                                  station_id=station_id, rows=rows, agg=agg,
                                  market_name=_market_name(station_id))


ABOUT_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>About — Weather Predictor</title>
<style>
  body{background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;
       padding:1rem;max-width:820px;margin:0 auto;line-height:1.55}
  h1{color:#cba6f7;margin:0 0 .4rem;border-bottom:2px solid #313244;padding-bottom:.2rem}
  h2{color:#89b4fa;margin-top:2rem;border-bottom:1px solid #313244;padding-bottom:.1rem}
  h3{color:#94e2d5;margin-top:1.2rem}
  h4{color:#f5c2e7;margin-top:1rem}
  a{color:#89b4fa}
  code{background:#181825;padding:1px 5px;border-radius:3px;color:#f5c2e7;
       font-family:"JetBrains Mono",Menlo,Consolas,monospace;font-size:12px}
  pre{background:#181825;padding:.8rem 1rem;border-radius:6px;overflow-x:auto;
      font-size:12px;border:1px solid #313244}
  pre code{background:transparent;padding:0;color:#cdd6f4}
  table{width:100%;border-collapse:collapse;margin:.6rem 0;font-size:13px}
  th,td{border:1px solid #313244;padding:6px 10px;text-align:left;vertical-align:top}
  th{background:#181825;color:#a6adc8}
  hr{border:none;border-top:1px solid #313244;margin:1.6rem 0}
  blockquote{border-left:3px solid #45475a;margin:.6rem 0;padding:.3rem 1rem;
             color:#a6adc8;background:#181825;border-radius:0 4px 4px 0}
  ul,ol{padding-left:1.4rem}
  li{margin:.15rem 0}
  strong{color:#f9e2af}
  .meta{color:#6c7086;font-size:12px;margin-top:.2rem}
</style></head><body>
<p><a href="/">&larr; volver</a> ·
   <a href="/tutorial.pdf">descargar PDF</a></p>
{{ body|safe }}
</body></html>"""


@app.route("/about")
def about_view():
    from pathlib import Path
    md_path = Path(__file__).parent / "tutorial.md"
    if not md_path.exists():
        return "tutorial.md no encontrado", 404
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return "markdown-it-py no instalado", 500
    md = MarkdownIt("commonmark", {"linkify": True, "typographer": True}).enable("table")
    body = md.render(md_path.read_text(encoding="utf-8"))
    return render_template_string(ABOUT_TMPL, body=body)


@app.route("/tutorial.pdf")
def tutorial_pdf():
    from pathlib import Path
    from flask import send_file
    pdf = Path(__file__).parent / "tutorial.pdf"
    if not pdf.exists():
        return "tutorial.pdf no generado", 404
    return send_file(str(pdf), mimetype="application/pdf",
                     as_attachment=False, download_name="weather-predictor-tutorial.pdf")


EDGE_ALERT_THR = 0.10  # |our_p - kalshi_p| ≥ 10pp triggers push


def _check_edge_alerts(snap, station) -> None:
    import notify as _notify
    import bets as _bets
    try:
        import kalshi as _k
    except Exception:
        return
    if _k.series_for(station.id) is None:
        return
    target = snap.station_local.date()
    rows = _k.latest_snapshot(station.id, target)
    models_spread = None
    if _external_models is not None:
        try:
            mm = _external_models.fetch_multi_model_max(station)
            if mm is not None:
                models_spread = mm.spread
        except Exception:
            pass
    # External anchor: blendea our_p contra Gaussiana centrada en ext_med
    # cuando el modelo discrepa fuerte. Evita auto-betear el lado cold/hot
    # que el modelo sistemáticamente exagera. Ver external_models.blend_with_external.
    anchor_ctx = _anchor_context(station, snap.ensemble_daily_maxes)
    sm = sorted(snap.ensemble_daily_maxes) if snap.ensemble_daily_maxes else []
    pred_med = sm[len(sm) // 2] if sm else None
    # ext_diff para el gate direccional: PRE-shift. La discrepancia original
    # es la señal de peligro; anchor_ctx["ext_diff"] viene atenuado tras el
    # anclaje del posterior (KLAS 06-10: -1.6 pre, ~-1.1 post → gate no disparaba).
    gate_ext_diff = None
    if getattr(snap, "ext_shift_info", None):
        gate_ext_diff = snap.ext_shift_info.get("ext_diff_pre")
    if gate_ext_diff is None and anchor_ctx is not None:
        gate_ext_diff = anchor_ctx["ext_diff"]
    import isotonic as _iso
    _cal = _iso.get(station.id)
    _cal_active = (_cal is not None and _cal.n_fit >= _iso.MIN_N
                   and _cal.n_days >= _iso.MIN_DAYS)
    _cal_for_apply = _cal if _cal_active else None
    for r in rows:
        op_ = r.get("our_p")
        ym = r.get("yes_mid")
        if op_ is None or ym is None:
            continue
        op_ = _iso.apply(_cal_for_apply, op_)
        if anchor_ctx is not None and _external_models is not None:
            op_, _ = _external_models.blend_with_external(
                op_, anchor_ctx["ext_med"], anchor_ctx["ext_spread"],
                r["bin_lo"], r["bin_hi"],
                anchor_ctx["ext_diff"], anchor_ctx["lam"])
        edge_abs = abs(op_ - ym)
        try:
            _bets.maybe_bet(station.id, target, r["ticker"],
                            r["bin_lo"], r["bin_hi"], r.get("label") or "",
                            op_, ym, models_spread_f=models_spread,
                            our_pred_f=pred_med,
                            ext_diff_f=gate_ext_diff)
        except Exception as e:
            print(f"bet error: {e}", file=sys.stderr)
        if _notify.enabled() and edge_abs >= EDGE_ALERT_THR:
            _notify.alert_edge(station.id, target, r["ticker"],
                               r.get("label") or "", op_, ym)


def _check_regime_alerts(snap, station) -> None:
    """Trigger logic (any of these fires one push per day per station):
      A. ≥2 horas con obs fuera de p1-p99 del ensemble                → reason p1-p99
      B. 1 hora rota + eff_n < 3/31 (reweight colapsado)              → reason combo
      C. eff_n < 2/31 sin ningún break (ensemble totalmente fuera)    → reason eff_n_low

    Thresholds elegidos tras el caso KBOS 2026-04-24 (eff_n=1.8, 1 break,
    prediccion +2°F high toda la tarde sin dispararse antes)."""
    import notify as _notify
    if not _notify.enabled():
        return
    n_breaks = len(snap.regime_break_hours)
    eff_n = snap.ensemble_eff_n
    target_date = snap.station_local.date()

    reason = None
    if n_breaks >= 2:
        reason = "p1-p99"
    elif n_breaks >= 1 and eff_n is not None and eff_n < 3.0:
        reason = "combo"
    elif eff_n is not None and eff_n < 2.0:
        reason = "eff_n_low"
    if reason is None:
        return

    _notify.alert_regime_break(station.id, target_date,
                               snap.regime_break_hours,
                               eff_n=eff_n, reason=reason)


_last_weather_alert_check: dict[str, datetime] = {}
_WEATHER_ALERT_INTERVAL_SEC = 900  # NWS refresh at most every 15 min/station


def _check_weather_alerts(station) -> int:
    """Poll NWS active alerts for this station and push via notify. Rate-limited
    to every 15 min per station to avoid hitting NWS every poll during peak."""
    if _weather_alerts is None:
        return 0
    import notify as _notify
    if not _notify.enabled():
        return 0
    now = datetime.now(timezone.utc)
    last = _last_weather_alert_check.get(station.id)
    if last is not None and (now - last).total_seconds() < _WEATHER_ALERT_INTERVAL_SEC:
        return 0
    _last_weather_alert_check[station.id] = now
    return _weather_alerts.check_and_push(station)


def _check_settle_alerts(station, settled: list) -> None:
    import notify as _notify
    if not _notify.enabled() or not settled:
        return
    import calibration as _cal
    c = _cal._conn()
    for d, max_f in settled:
        row = c.execute("""SELECT our_brier, kalshi_brier FROM day_summary
                           WHERE station_id=? AND date=?""",
                        (station.id, d.isoformat())).fetchone()
        ob, kb = (row if row else (None, None))
        _notify.alert_settled(station.id, d, max_f, ob, kb)
    c.close()


def do_poll():
    if state is None:
        return
    if _poll_interval_for(state.station) == PEAK_POLL_SEC:
        invalidate_obs_cache(state.station.id)
    try:
        snap = build_snapshot(state.station)
    except Exception as e:
        print(f"poll error: {e}", file=sys.stderr)
        _record_poll_error(f"snapshot: {e}")
        return
    POLL_STATS["last_ok_at"] = datetime.now(timezone.utc)
    POLL_STATS["ok_count"] += 1
    with state_lock:
        if state.last_snapshot is not None:
            prev_dist = sorted(state.last_snapshot.ensemble_daily_maxes)
            state.prev_dist_med = prev_dist[len(prev_dist) // 2]
        state.last_snapshot = snap
        refresh_auto(state, snap)
        for slot in (1, 2, 3):
            a = state.assertions.get(slot)
            if a is not None:
                p, _ = eval_assertion(a, snap)
                a.history.append((snap.fetched_at, p))
        try:
            log_snapshot(snap, state.station, state.assertions)
        except Exception as e:
            print(f"csv log error: {e}", file=sys.stderr)
            _record_poll_error(f"csv: {e}")
        # Persist external-model signal del día (primer write gana, INSERT OR
        # IGNORE) para backtest futuro de umbrales del posterior shift y gate.
        try:
            import calibration as _cal
            info = getattr(snap, "ext_shift_info", None)
            if info is not None:
                _sm = sorted(snap.ensemble_daily_maxes) if snap.ensemble_daily_maxes else []
                # pred POST-shift; el pred_pre_shift se reconstruye con shift_f
                _pred_post = _sm[len(_sm) // 2] if _sm else None
                _pred_pre = (_pred_post - info.get("shift_f", 0.0)
                             if _pred_post is not None else None)
                _cal.record_ext_signal(state.station.id,
                                       snap.station_local.date(),
                                       info, _pred_pre,
                                       bias_info=getattr(snap, "bias_info", None))
        except Exception as e:
            print(f"ext_signal log error: {e}", file=sys.stderr)
    try:
        record_kalshi(snap, state.station)
    except Exception as e:
        print(f"kalshi error: {e}", file=sys.stderr)
        _record_poll_error(f"kalshi: {e}")
    try:
        _check_edge_alerts(snap, state.station)
    except Exception as e:
        print(f"notify error: {e}", file=sys.stderr)
    try:
        _check_regime_alerts(snap, state.station)
    except Exception as e:
        print(f"regime notify error: {e}", file=sys.stderr)
    try:
        _check_weather_alerts(state.station)
    except Exception as e:
        print(f"weather alert error: {e}", file=sys.stderr)
    t = f"{snap.current_temp_f:.1f}°F" if snap.current_temp_f is not None else "—"
    mx = f"{snap.today_max_obs:.1f}°F" if snap.today_max_obs is not None and snap.today_max_obs > -900 else "—"
    print(f"[{snap.station_local.strftime('%H:%M:%S')}] {state.station.id} "
          f"{t}  max={mx}  {snap.peak_status}")


def _warm_cross_cache():
    """Pre-fetch ensemble + market for DEFAULT_CROSS so /cross hits warm.
    Runs in a thread; failures are silent (cache miss just means slow page)."""
    try:
        with ThreadPoolExecutor(max_workers=len(DEFAULT_CROSS)) as ex:
            list(ex.map(lambda s: _cross_one(s, 0), DEFAULT_CROSS))
    except Exception as e:
        print(f"warm_cross_cache error: {e}", file=sys.stderr)


def poll_loop():
    last_settle_day = None
    while state is not None and not state.stop.is_set():
        do_poll()
        threading.Thread(target=_warm_cross_cache, daemon=True).start()
        if _calibration is not None and state is not None:
            today = datetime.now(state.station.tz).date()
            if last_settle_day != today:
                try:
                    settled = _calibration.settle_pending(state.station)
                    last_settle_day = today
                    try:
                        _check_settle_alerts(state.station, settled)
                    except Exception as e:
                        print(f"settle notify error: {e}", file=sys.stderr)
                except Exception as e:
                    print(f"settle_pending error: {e}", file=sys.stderr)
        state.stop.wait(_poll_interval_for(state.station))


def overnight_loop():
    """Wake at SWEEP_HOUR_AST each day, sweep divergence for SUPPORTED_STATIONS."""
    import overnight as _ov
    last_run = None
    while state is not None and not state.stop.is_set():
        now = datetime.now(_ov.AST)
        today = now.date()
        target_hour = _ov.SWEEP_HOUR_AST
        # Compute next fire time
        if now.hour < target_hour:
            fire = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        else:
            fire = (now + timedelta(days=1)).replace(
                hour=target_hour, minute=0, second=0, microsecond=0)
        sleep_s = max(60.0, (fire - now).total_seconds())
        if state.stop.wait(sleep_s):
            return
        if last_run == today:
            continue
        try:
            _ov.run_sweep(SUPPORTED_STATIONS)
            last_run = datetime.now(_ov.AST).date()
        except Exception as e:
            print(f"overnight sweep error: {e}", file=sys.stderr)


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global state
    sid = sys.argv[1] if len(sys.argv) > 1 else "KPHX"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    print(f"Cargando estación {sid}...")
    try:
        station = fetch_station(sid)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    state = State(station)
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=overnight_loop, daemon=True).start()
    ip = get_lan_ip()
    print(f"\n🌦️  Weather Predictor Web — {station.id} {station.name}")
    print(f"   Laptop:  http://localhost:{port}")
    print(f"   iPad:    http://{ip}:{port}    (misma WiFi)")
    print(f"\n   Ctrl+C para detener\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
