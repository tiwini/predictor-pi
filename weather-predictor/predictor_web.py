#!/usr/bin/env python3
"""Flask web UI for the weather predictor — browse from iPad/phone on same WiFi.

Run with the venv python:

    ./venv/bin/python3 predictor_web.py [STATION_ID] [PORT]
"""
import socket
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request

from predictor import (
    POLL_SEC, PR_TZ, PEAK_HOURS,
    Assertion, State, PeakState, peak_state_display,
    fetch_station, build_snapshot, refresh_auto, eval_assertion,
    find_informative_bin, most_likely_max, movement_cents, parse_expr, log_snapshot,
    record_kalshi, invalidate_obs_cache, fetch_precip_context_12h,
    obs_floor_from_snapshot, zero_impossible_bins,
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
try:
    import station_brief as _station_brief
except Exception:
    _station_brief = None
try:
    import sys as _sys
    _sys.path.insert(0, "/home/popeye/predictor-pi")
    from agent_monitor import (ask_station as _ask_station,
                                get_last_station_ask as _get_last_station_ask,
                                clear_last_station_ask as _clear_last_station_ask,
                                STATION_PROMPTS as _STATION_PROMPTS,
                                ask as _ask_global,
                                get_last_home_ask as _get_last_home_ask,
                                clear_last_home_ask as _clear_last_home_ask,
                                PROMPTS as _HOME_PROMPTS)
except Exception as _e:
    _ask_station = None
    _get_last_station_ask = lambda s: None
    _clear_last_station_ask = lambda s: None
    _STATION_PROMPTS = {}
    _ask_global = None
    _get_last_home_ask = lambda: None
    _clear_last_home_ask = lambda: None
    _HOME_PROMPTS = {}


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


def _build_station_strip(active_sid: str):
    """F2b.4 — 8 station cards horizontal (max, band, difficulty, edge).
    Reads _stations_cache only (populated por _warm_cross_cache cada poll).
    Cold cache → devuelve [] (no bloqueamos home load con fetch).

    F4 — También enriquece cada card con peak_badge desde _peak_status_cache
    (5 curadas solamente). Si el cache está vacío/expirado, badge queda None.
    """
    now = datetime.now(timezone.utc)
    cached = _stations_cache.get("results")
    cached_at = _stations_cache.get("computed_at")
    if not cached or not cached_at:
        return []
    if (now - cached_at).total_seconds() > _STATIONS_TTL_SEC * 2:
        return []
    peak_cached_at = _peak_status_cache.get("computed_at")
    peak_fresh = (peak_cached_at is not None and
                  (now - peak_cached_at).total_seconds() < _PEAK_STATUS_TTL_SEC)
    peak_data = _peak_status_cache.get("data") or {} if peak_fresh else {}
    # N8 Fable veredicto R4: strip del home ordena por longitud DESC pura
    # (este→oeste). Sort estable, sin fila "central". Dashboard :8080 hará el
    # compose activas-primero; aquí es longitud a secas.
    from stations import STATION_TO_LON as _LON
    cached = sorted(cached,
                    key=lambda r: _LON.get(r.get("station", ""), 0.0),
                    reverse=True)
    cards = []
    for r in cached:
        if r.get("error"):
            continue
        p50 = r.get("p50_precise") or r.get("p50")
        p10 = r.get("p10")
        p90 = r.get("p90")
        band = (p90 - p10) / 2.0 if p10 is not None and p90 is not None else None
        diff = r.get("difficulty") or {}
        edge_pp = r["edge"] * 100.0 if r.get("edge") is not None else None
        modal_lbl = None
        if r.get("modal_bin") is not None:
            mb = r["modal_bin"]
            modal_lbl = getattr(mb, "label", None) or f"{mb.bin_lo:.0f}-{mb.bin_hi:.0f}"
        pb = peak_data.get(r["station"])
        cards.append({
            "sid": r["station"],
            "name": r.get("name") or r["station"],
            "p50": p50,
            "band": band,
            "diff_label": diff.get("label") or "—",
            "diff_klass": {"fácil": "easy", "normal": "normal",
                            "difícil": "hard", "muy difícil": "veryhard"}.get(
                                diff.get("label") or "", "normal"),
            "diff_skip": diff.get("skip", False),
            "edge_pp": edge_pp,
            "modal_lbl": modal_lbl,
            "is_active": r["station"] == active_sid,
            "peak_badge": pb.get("badge_text") if pb else None,
            "peak_kind": pb.get("badge_kind") if pb else None,
        })
    return cards


def _build_streak_top3():
    """F2b.2 — RACHA ACTIVA card data. Flatten /api/streak windows into top-3
    by streak_days (desc) across all stations × windows.
    """
    try:
        import streaks as _streaks
        from calibration import DB_PATH as _CAL_DB
        rows = _streaks.compute_streaks(str(_CAL_DB))
    except Exception:
        return []
    flat = []
    for w, entries in rows.items():
        for r in entries:
            if r.streak_days >= 1:
                flat.append({"window": w, "station_id": r.station_id,
                             "streak_days": r.streak_days})
    flat.sort(key=lambda x: (-x["streak_days"], x["window"]))
    return flat[:3]


def _build_brier_watchdog():
    """E.1 — Latest weekly Brier snapshot from brier_weekly table.
    Cron writes weekly; home reads. Table missing / empty → return None.
    """
    try:
        import sqlite3
        from calibration import DB_PATH as _CAL_DB
        conn = sqlite3.connect(str(_CAL_DB))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='brier_weekly'")
            if not cur.fetchone():
                return None
            latest = conn.execute(
                "SELECT MAX(week_iso) FROM brier_weekly").fetchone()[0]
            if not latest:
                return None
            rows = conn.execute(
                """SELECT station_id, n, our_brier, kalshi_brier,
                          ratio, alerted, generated_at, lookback_days
                   FROM brier_weekly WHERE week_iso=?
                   ORDER BY (ratio IS NULL), ratio DESC""",
                (latest,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    if not rows:
        return None
    stations = [{"sid": r[0], "n": r[1], "our": r[2], "kalshi": r[3],
                  "ratio": r[4], "alerted": bool(r[5])} for r in rows]
    return {
        "week_iso": latest,
        "generated_at": rows[0][6],
        "lookback_days": rows[0][7],
        "stations": stations,
        "n_alert": sum(1 for s in stations if s["alerted"]),
    }


def _build_decision_pill(station, market, difficulty,
                          dist_med, dist_p10, dist_p90):
    """F2b.1 — DECISIÓN HOY pill (active station).
    Uses only data already computed in index() — zero extra fetch.
    """
    band = (dist_p90 - dist_p10) / 2.0 if dist_p10 is not None and dist_p90 is not None else 0.0
    band_str = f"{dist_med:.0f}°F ±{band:.1f}"

    if market is None:
        return {"action": "wait",
                "text": "SIN MERCADO",
                "detail": f"{band_str} · sin bins de Kalshi hoy"}

    edge_pp = market["top_edge"] * 100.0
    bin_lbl = market["top_label"]

    if difficulty and difficulty.get("recommend_skip"):
        return {"action": "skip",
                "text": f"SKIP · {difficulty.get('label', 'difícil')}",
                "detail": f"{band_str} · bin {bin_lbl} · edge {edge_pp:+.1f}pp"}

    if abs(edge_pp) >= 5.0:
        side = "YES" if edge_pp > 0 else "NO"
        return {"action": "bet",
                "text": f"APUESTA · {side} bin {bin_lbl}",
                "detail": f"{band_str} · edge {edge_pp:+.1f}pp"}

    return {"action": "wait",
            "text": "MIRAR · edge chico",
            "detail": f"{band_str} · bin {bin_lbl} · edge {edge_pp:+.1f}pp"}


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

    peak_class = {
        PeakState.CONFIRMED: "peak-green",
        PeakState.PLATEAU: "peak-yellow",
        PeakState.PRE_WINDOW: "peak-dim",
        PeakState.RISING: "peak-cyan",
    }.get(snap.peak_state, "peak-cyan")

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

    # Empírico de últimos 7 días via Open-Meteo archive (cache 24h).
    # Si está disponible, prefiere las zonas empíricas sobre las del ensemble
    # de hoy — son más estables y específicas de la estación.
    empirical_window = None
    try:
        import peak_window as _pw
        empirical_window = _pw.get(station)
    except Exception:
        empirical_window = None

    # Reloj del día: zonas (pre / confianza / decisiva / post) + marcador pico + cursor ahora
    clock = None
    have_timing = (timing is not None
                   and timing["p10"] is not None
                   and timing["p90"] is not None)
    if have_timing or empirical_window is not None:
        peak_lo_h, _peak_hi_h = PEAK_HOURS.get(station.id, (12, 16))
        now_dt = snap.station_local
        now_h_float = now_dt.hour + now_dt.minute / 60.0
        if empirical_window is not None:
            decisive_start = float(empirical_window["p10"])
            decisive_end = float(empirical_window["p90"])
            modal_h = float(empirical_window["modal_hour"])
            source_label = f"empírico 7d · n={empirical_window['n']}"
        else:
            decisive_start = float(timing["p10"])
            decisive_end = float(timing["p90"])
            modal_h = (float(timing["modal_hour"])
                       if timing["modal_hour"] is not None
                       else (decisive_start + decisive_end) / 2.0)
            source_label = "ensemble hoy"
        confidence_start = max(peak_lo_h - 3.0, 6.0)
        if decisive_end < decisive_start:
            decisive_end = decisive_start
        if confidence_start > decisive_start:
            confidence_start = max(decisive_start - 1.0, 6.0)
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
            "source_label": source_label,
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

    # Fable #E (2026-07-15): assert coherencia ens_med vs bins visibles.
    # Bug KLAS 2026-06-26: card mostró ens_med=106.9°F pero la distribución
    # de bins concentraba 83% en ≤100°F (0% en ≥103°F). Sólo puede pasar
    # si dos consumidores leyeron distintas copias del `dist` (pre vs post
    # bias/shift). Este check catchea el patrón: comparamos el bin modal
    # del histograma (mismo `dist` que dist_med) vs dist_med. Divergencia
    # ≥3°F → log + tag UI para diagnóstico.
    dist_divergence_note = None
    if top_max_bars:
        modal_deg = next((b["deg"] for b in top_max_bars if b["is_modal"]), None)
        if modal_deg is not None and abs(modal_deg - dist_med) >= 3.0:
            _delta = modal_deg - dist_med
            print(f"[WARN dist-divergence] station={station.id} "
                  f"ens_med={dist_med:.2f}°F modal_deg={modal_deg}°F "
                  f"delta={_delta:+.1f}°F p10={dist_p10:.1f} p90={dist_p90:.1f} "
                  f"n={len(dist)}", flush=True)
            dist_divergence_note = (
                f"⚠ ens_med {dist_med:.1f}°F vs modal {modal_deg}°F "
                f"({_delta:+.1f}°F) — revisar dist pre/post-shift")

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

    try:
        import regime as _regime
        regime_tag = _regime.classify(snap, station.id, snap.station_local)
    except Exception:
        regime_tag = None

    decision = _build_decision_pill(station, market, difficulty,
                                    dist_med, dist_p10, dist_p90)
    streak_top3 = _build_streak_top3()
    station_strip = _build_station_strip(station.id)
    peak_status_age = None
    _pca = _peak_status_cache.get("computed_at")
    if _pca is not None:
        peak_status_age = int(
            (datetime.now(timezone.utc) - _pca).total_seconds())
    brier_watchdog = _build_brier_watchdog()
    station_ask_last = _fresh_ask(
        _get_last_station_ask(station.id) if _ask_station else None,
        station.tz)
    station_ask_prompts = [{"kind": k, "label": v["label"]}
                           for k, v in _STATION_PROMPTS.items()]
    station_ask_error = request.args.get("ask_err")
    max_obs_ts_local = None
    if snap.today_max_obs_ts is not None:
        max_obs_ts_local = snap.today_max_obs_ts.astimezone(PR_TZ).strftime("%H:%M AST")

    # Fable #4 (2026-07-15): context-clamp — cuando current > max_obs, el gap
    # suele ser ruido de redondeo del feed 5-min (34°C rounded = 93.2°F vs
    # METAR real 33.9°C = 93.0°F). NO tocamos fetch_current (gated a 07-24);
    # solo anotamos el contexto para que el usuario no lea "actual > max obs"
    # como un nuevo peak. Delta >1°F sugiere un METAR :51 pendiente (gap
    # legítimo, no redondeo).
    current_over_max_note = None
    if (snap.current_temp_f is not None
            and snap.today_max_obs is not None
            and snap.today_max_obs > -900):
        _delta = snap.current_temp_f - snap.today_max_obs
        if 0.05 < _delta <= 1.0:
            current_over_max_note = (
                f"vs max obs {snap.today_max_obs:.1f}°F · "
                f"+{_delta:.1f}°F (redondeo feed 5-min)")
        elif _delta > 1.0:
            current_over_max_note = (
                f"⚠ vs max obs {snap.today_max_obs:.1f}°F · "
                f"+{_delta:.1f}°F (METAR :51 pendiente?)")

    # ASOS 6h-max override: cuando el grupo `1sTTT` del METAR excede el max_obs
    # del feed 5-min por >0.5°F, exponemos ese valor + timestamp para que el
    # usuario vea el gap (Kalshi settle contra CLI usa la misma fuente).
    asos_6h_display = None
    settle_hint_f = snap.today_max_obs
    if (snap.today_max_asos_6h is not None
            and snap.today_max_obs is not None
            and snap.today_max_asos_6h > snap.today_max_obs + 0.5):
        settle_hint_f = snap.today_max_asos_6h
        # Fable #2 2026-07-15: declarar ventana 6h explícita en display.
        # El grupo mide max en [ts-6h, ts]; sin el rango, "12:51 AST" se
        # confunde con "peak fue a las 12:51" cuando es realmente el cierre
        # del bucket.
        window_str = ""
        if snap.today_max_asos_6h_ts is not None:
            win_end = snap.today_max_asos_6h_ts.astimezone(PR_TZ)
            win_start = win_end - timedelta(hours=6)
            window_str = (f" · ventana {win_start.strftime('%H:%M')}"
                          f"–{win_end.strftime('%H:%M AST')}")
        asos_6h_display = f"{snap.today_max_asos_6h:.1f}°F{window_str}"

    # F2 pieza 1 (2026-07-26): el CLI parcial de la tarde manda sobre las dos
    # anteriores como pista de settle — es literalmente el producto con el que
    # Kalshi liquida, leído antes del cierre. Va después del bloque ASOS a
    # propósito: si ambos hablan, gana el CLI.
    cli_display = None
    if (snap.today_max_cli is not None
            and settle_hint_f is not None
            and snap.today_max_cli > settle_hint_f):
        settle_hint_f = snap.today_max_cli
        issued_str = ""
        if snap.today_max_cli_ts is not None:
            issued_str = (" · emitido "
                          + snap.today_max_cli_ts.astimezone(PR_TZ)
                          .strftime("%H:%M AST"))
        cli_display = f"{snap.today_max_cli:.1f}°F{issued_str}"

    return render_template(
        "home.html", station=station, snap=snap, dash=dash, hero=hero,
        max_obs_ts_local=max_obs_ts_local,
        asos_6h_display=asos_6h_display,
        cli_display=cli_display,
        settle_hint_f=settle_hint_f,
        current_over_max_note=current_over_max_note,
        dist_divergence_note=dist_divergence_note,
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
        difficulty=difficulty, regime_tag=regime_tag,
        decision=decision, streak_top3=streak_top3,
        station_strip=station_strip,
        peak_status_age=peak_status_age,
        brier_watchdog=brier_watchdog,
        station_ask_last=station_ask_last,
        station_ask_prompts=station_ask_prompts,
        station_ask_error=station_ask_error,
        station_ask_enabled=(_ask_station is not None),
        market_name=_market_name(station.id),
    )


@app.route("/api/ping")
def api_ping():
    if state is None or state.last_snapshot is None:
        return jsonify({"ts": None})
    ts_ms = int(state.last_snapshot.fetched_at.timestamp() * 1000)
    return jsonify({"ts": state.last_snapshot.fetched_at.isoformat(), "ts_ms": ts_ms})


@app.route("/api/quota")
def api_quota():
    """Open-Meteo daily quota counter. Reset implícito a UTC midnight."""
    try:
        import om_quota
        return jsonify({
            **om_quota.today_count(),
            "limit": om_quota.DAILY_LIMIT,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/streak")
def api_streak():
    """Top estaciones en racha de precisión por ventana horaria local."""
    try:
        import streaks as _streaks
        from calibration import DB_PATH as _CAL_DB
        out = _streaks.compute_streaks(str(_CAL_DB))
        return jsonify(_streaks.to_json(out, top_n=3))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/peak-status")
def api_peak_status():
    """F4 — Peak status por estación curada (KPHX/KLAX/KLAS/KNYC/KBOS).
    On-demand + TTL 20 min. ?refresh=1 fuerza recompute.
    Costo: 2 hits Open-Meteo (ensemble+obs) × 5 estaciones por refresh."""
    now = datetime.now(timezone.utc)
    force = request.args.get("refresh") == "1"
    cached_at = _peak_status_cache.get("computed_at")
    fresh = (cached_at is not None and
             (now - cached_at).total_seconds() < _PEAK_STATUS_TTL_SEC)
    if not fresh or force:
        _refresh_peak_status_cache()
        cached_at = _peak_status_cache["computed_at"]
    age = int((now - cached_at).total_seconds()) if cached_at else -1
    return jsonify({
        "computed_at": cached_at.isoformat() if cached_at else None,
        "age_sec": age,
        "ttl_sec": _PEAK_STATUS_TTL_SEC,
        "stations": _peak_status_cache["data"],
    })


@app.route("/api/peak-status/refresh", methods=["POST"])
def api_peak_status_refresh():
    """Botón manual — recompute y volvés a home."""
    try:
        _refresh_peak_status_cache()
    except Exception:
        pass
    return redirect(request.form.get("next", "/"))


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


_refresh_started_ts: datetime | None = None
_refresh_lock = threading.Lock()


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    global _refresh_started_ts
    with _refresh_lock:
        _refresh_started_ts = datetime.now(timezone.utc)
    threading.Thread(target=do_poll, daemon=True).start()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"started_at": _refresh_started_ts.isoformat()})
    return redirect("/")


@app.route("/api/refresh-status")
def api_refresh_status():
    """Busy si un refresh fue disparado y el snapshot aún no se reconstruyó
    después de ese ts. UI hace polling para saber cuándo recargar."""
    global _refresh_started_ts
    started = _refresh_started_ts
    snap_ts = None
    if state is not None and state.last_snapshot is not None:
        snap_ts = state.last_snapshot.fetched_at
    busy = bool(started and (snap_ts is None or snap_ts < started))
    return jsonify({
        "busy": busy,
        "started_at": started.isoformat() if started else None,
        "snapshot_ts": snap_ts.isoformat() if snap_ts else None,
    })


@app.route("/api/home-ask", methods=["POST"])
def api_home_ask():
    """F2b.3 — canned prompt global desde home. Bloqueante (~2-4s Haiku call)."""
    if _ask_global is None:
        return redirect("/")
    kind = (request.form.get("kind") or "").strip()
    if kind not in _HOME_PROMPTS:
        return redirect("/")
    _ask_global(kind)
    return redirect("/")


@app.route("/api/home-ask/clear", methods=["POST"])
def api_home_ask_clear():
    _clear_last_home_ask()
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
    nudge_ext_used = 0.0
    snap = getattr(state, "last_snapshot", None) if state else None
    if snap is not None and getattr(snap, "ext_shift_info", None):
        lam = float(snap.ext_shift_info.get("lambda") or 0.0)
        nudge_ext_used = float(snap.ext_shift_info.get("nudge_ext_used") or 0.0)
    return {"ext_med": mm.median, "ext_spread": mm.spread,
            "ext_diff": ext_diff, "lam": lam,
            "nudge_ext_used": nudge_ext_used}


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


# Kelly se muestra FRACCIONADO. Kelly completo es óptimo sólo si la
# probabilidad que le das es la verdadera, y la nuestra está medida y no lo es:
# `our_p` 0.80 acierta 0.35 (calibrador_techo_050_2026_08_03) y el Brier de
# Kalshi nos gana 7 de 9 (edges_no_estructurales_brier_2026_07_27). Con
# probabilidades sobreconfiadas, Kelly completo crece justo cuando más
# equivocado está el modelo: el 2026-08-17 la mesa de KMIA recomendaba "NO ~87%
# del bankroll" sobre un desacuerdo de 20pp con el mercado.
#
# 1/4 es la convención habitual bajo incertidumbre de parámetros; NO sale de
# nuestros datos, y por eso se muestra etiquetado como fracción y con el Kelly
# completo al lado, en vez de esconder el cálculo.
KELLY_FRACTION = 0.25


def _default_move_bin(move_bins: list, move_summary: list) -> dict | None:
    """Bin que abre la gráfica de /intraday: el que encabeza la tabla.

    Antes era `max(move_bins, key=len(points))`, o sea el que tuviera más filas.
    A media mañana todos los bins llevan el mismo número de muestras, así que
    `max` devolvía el primero de la lista y la gráfica principal abría con
    "92° or below" — mercado 0.5%, nuestro 3%, movimiento 0.0pp: dos líneas
    planas en el suelo. Mientras tanto el bin que se había movido 8pp estaba a
    dos clics (KMIA, 2026-08-18).

    `move_summary` ya viene ordenado por |Δ Kalshi|, que es justo el criterio de
    la tabla; se reutiliza para que la gráfica y la tabla no se contradigan. Se
    exigen ≥2 puntos porque con uno solo no hay línea que dibujar, y si ninguno
    llega se cae al criterio viejo en vez de no mostrar nada.
    """
    if not move_bins:
        return None
    dibujables = {b["ticker"]: b for b in move_bins if len(b["points"]) >= 2}
    for r in move_summary:
        b = dibujables.get(r["ticker"])
        if b is not None:
            return b
    return max(move_bins, key=lambda b: len(b["points"]))


def _ev_kelly(p_our: float, k_yes: float) -> dict:
    """EV y Kelly por $1 apostado en yes o no al precio Kalshi.

    EV_yes = (p - k)/k         · EV_no = (k - p)/(1-k)
    f*_yes = (p - k)/(1 - k)   · f*_no = (k - p)/k

    `kelly_*` son los completos (el cálculo, intacto) y `kelly_*_frac` los
    que se muestran y se recomiendan. Retorna el lado con mayor EV positivo.
    None si k inválido.
    """
    if k_yes is None or k_yes <= 0.01 or k_yes >= 0.99:
        return {"ev_yes": None, "ev_no": None,
                "kelly_yes": None, "kelly_no": None,
                "kelly_yes_frac": None, "kelly_no_frac": None,
                "rec": None, "rec_ev": None, "rec_kelly": None,
                "rec_kelly_full": None}
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
            "kelly_yes_frac": kel_yes * KELLY_FRACTION,
            "kelly_no_frac": kel_no * KELLY_FRACTION,
            "rec": rec, "rec_ev": rec_ev,
            # Lo que se recomienda es el fraccionado; el completo se conserva
            # para poder enseñar de dónde sale.
            "rec_kelly": (rec_kelly * KELLY_FRACTION
                          if rec_kelly is not None else None),
            "rec_kelly_full": rec_kelly}


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
    # Global, no por estacion — por estacion daba skill -12.4% vs -0.1%
    # global, medido out-of-sample 2026-07-24 sobre 83724 eventos. Ver
    # nota extensa en predictor._compute_final_our_p_per_bin.
    cal = _iso.get(None)
    cal_active = (cal is not None
                  and cal.n_fit >= _iso.MIN_N
                  and cal.n_days >= _iso.MIN_DAYS)
    cal_for_apply = cal if cal_active else None

    # Mismo piso que /comparison, en su forma de umbral: si el día ya pasó de
    # `thr`, P(max > thr) es CERTEZA y no una estimación que la isotónica deba
    # tocar (su techo efectivo es 0.50, así que degradaba un 1.00 seguro a ~0.5
    # y con ello inflaba our_no sobre algo ya perdido). Mismo criterio de
    # redondeo que `zero_impossible_bins`: seguro sólo si floor > thr + 0.5.
    ladder_floor = None
    if day_offset == 0 and state is not None and state.last_snapshot is not None:
        ladder_floor = obs_floor_from_snapshot(state.last_snapshot)

    rows = []
    for thr in range(thr_lo, thr_hi + 1):
        our_yes_raw = sum(1 for v in dist if v > thr) / n
        if ladder_floor is not None and ladder_floor > thr + 0.5:
            our_yes_raw = 1.0
            our_yes = 1.0
        else:
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
    return render_template(
        "ladder.html", station=station.id, target_date=target.isoformat(),
        rows=rows, max_obs=max_obs,
        median_pred=f"{median:.1f}°F",
        day_offset=day_offset, day_label=day_labels[day_offset],
        window=window, show_all=show_all,
        cal_active=cal_active,
        cal_n_fit=(cal.n_fit if cal else 0),
        cal_n_days=(cal.n_days if cal else 0),
        cal_min_n=_iso.MIN_N, cal_min_days=_iso.MIN_DAYS,
        market_name=_market_name(station.id))


def _fresh_ask(ask: dict | None, tz) -> dict | None:
    """Respuesta de la IA sólo si es del día local en curso, con su edad anotada.

    Por qué caduca: el 2026-08-17 la home servía en KLAS una respuesta del
    2026-06-26 que decía "Hoy ya observado 105.98°F", y /comparison una del
    2026-07-18 con "Recomendación de bet: 95° to 96° YES, edge +92pp, confianza
    muy alta" — con el día ya en 111.9°F. El `ts` estaba guardado desde el
    principio; la home sólo pintaba `ts[11:16]`, o sea la hora sin la fecha, y
    una respuesta de hace dos meses parecía de esta mañana.

    Una respuesta a "¿ya empezó a bajar?" o "max hoy" no vale nada al día
    siguiente, así que se descarta en vez de envejecerse en pantalla. Sin `ts`
    tampoco se muestra: no se puede afirmar frescura de algo sin fecha.
    """
    if not ask or not ask.get("ts"):
        return None
    try:
        dt = datetime.fromisoformat(str(ask["ts"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.astimezone(tz).date() != datetime.now(tz).date():
        return None
    mins = max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 60))
    out = dict(ask)
    out["age_min"] = mins
    out["age_str"] = (f"hace {mins} min" if mins < 60
                      else f"hace {mins // 60}h {mins % 60:02d}m")
    return out


def _split_recs(bins, min_edge_pp=5.0, tail_lo=0.08, tail_hi=0.92, top_k=3,
                low_edge_min_yes=0.05, low_edge_max_yes=0.95):
    """Divide en 'safe' (tail-negation), 'edge' (top |diff|), 'low' (consenso).

    safe = our_p extremo + side opuesto al mercado + edge ≥ min_edge_pp.
    edge = top_k por |our_p - yes_mid|, cualquier dirección.
    low  = top_k con MENOR |diff| dentro de la zona activa del mercado
           (yes_mid entre low_edge_min_yes y low_edge_max_yes). Sanity check:
           dónde modelo y Kalshi coinciden.
    """
    # tail-negation category RETIRADA 2026-07-07 (Fable audit response P0):
    # los 89 penny-YES longshots con 6 winners contaminaron la validación de
    # esta categoría bajo el ledger roto pre-2026-07-06. Se preserva la lógica
    # comentada para re-audit post-N≥100 bets post-fix. Readmite solo si
    # win_rate >55% con p<0.05 sobre N≥100.
    safe: list = []  # <-- forzado vacío hasta re-audit
    edge, low = [], []
    for b in bins:
        op = b.get("our_p"); km = b.get("yes_mid")
        if op is None or km is None:
            continue
        if km <= 0.01 or km >= 0.99:
            continue
        diff = op - km
        side = "YES" if diff > 0 else "NO"
        edge_pp = abs(diff) * 100
        rec = {
            "label": b["label"], "side": side,
            "our_p": op, "yes_mid": km, "edge_pp": edge_pp,
        }
        if edge_pp >= min_edge_pp:
            edge.append(rec)
            # PRE-RETIRO (2026-07-07):
            # if (op <= tail_lo and side == "NO") or (op >= tail_hi and side == "YES"):
            #     safe.append(rec)
        elif low_edge_min_yes <= km <= low_edge_max_yes:
            low.append(rec)
    edge.sort(key=lambda r: r["edge_pp"], reverse=True)
    low.sort(key=lambda r: r["edge_pp"])
    return safe[:top_k], edge[:top_k], low[:top_k]


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
    sort_mode = "edge" if request.args.get("sort") == "edge" else "bin"

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
    cal = _iso.get(None)  # global, ver nota en /ladder
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
                    anchor_ctx["ext_diff"], anchor_ctx["lam"],
                    ext_used=anchor_ctx.get("nudge_ext_used", 0.0))
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

    # Piso de observación — el mismo paso final que `_compute_final_our_p_per_bin`
    # aplica en el poller. Esta ruta reimplementa el pipeline (dist → isotónica →
    # blend externo) y se había quedado sin él: el 2026-08-17, con KPHX ya en
    # 111.9°F, los bins ≤107 / 108-109 / 110-111 se mostraban al 7.7-9.7% cuando
    # el poller los tenía en 0.0 para ese mismo instante. La isotónica sube el
    # raw de 0.030 a ~0.09, así que sin este paso la masa se queda en bins
    # muertos y produce "edge" a favor de algo que ya no puede pasar.
    # Sólo D+0: un día futuro no tiene observación que fije piso.
    dead_bins, dead_mass = 0, 0.0
    if day_offset == 0 and state is not None and state.last_snapshot is not None:
        _floor = obs_floor_from_snapshot(state.last_snapshot)
        _ps = [b.get("our_p") for b in bins]
        _ps, dead_bins, dead_mass = zero_impossible_bins(bins, _ps, _floor)
        for b, p in zip(bins, _ps):
            b["our_p"] = p

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

    # `recs_safe` es la tail-negation, retirada el 2026-07-07: `_split_recs` la
    # fuerza vacía y desde el 2026-08-17 el template ya no la pinta. Se recoge
    # con `_` para que quede claro que se descarta a propósito.
    _recs_safe_retirada, recs_edge, recs_low = _split_recs(bins)

    # F3.2a — sort=edge: reorder bins by |diff| desc + surface /edge data.
    edge_current, edge_analysis = [], None
    if sort_mode == "edge":
        def _abs_diff(b):
            op = b.get("our_p") or 0.0
            ym = b.get("yes_mid") or 0.0
            return abs(op - ym)
        bins = sorted(bins, key=_abs_diff, reverse=True)
        try:
            edge_current = _kalshi.current_edges(
                station.id, target, min_abs_edge=0.05)
        except Exception:
            edge_current = []
        try:
            edge_analysis = _kalshi.edge_analysis(station.id)
        except Exception:
            edge_analysis = None

    brief = _station_brief.get(station.id) if _station_brief else None
    last_ask = _fresh_ask(
        _get_last_station_ask(station.id) if _ask_station else None,
        station.tz)
    ask_error = None
    # Racha de precisión por ventana local para esta estación
    try:
        import streaks as _streaks
        from calibration import DB_PATH as _CAL_DB
        _st_rows = _streaks.compute_streaks(
            str(_CAL_DB), stations=[station.id])
        station_streak = [
            {"window": w,
             "days": (_st_rows[w][0].streak_days if _st_rows.get(w) else 0),
             "details": ([{"date": dd.date.isoformat(),
                           "pred_f": round(dd.pred_f, 1),
                           "obs_f": round(dd.obs_f, 1),
                           "err_f": round(dd.err_f, 1)}
                          for dd in _st_rows[w][0].details[:3]]
                         if _st_rows.get(w) else [])}
            for w in _streaks.WINDOWS_LOCAL
        ]
        streak_thresh_f = _streaks.THRESH_F
    except Exception as _e:
        station_streak = []
        streak_thresh_f = 1.5
    # Render-once error param coming from /ai/station-ask redirect
    if request.args.get("ask_err"):
        ask_error = request.args.get("ask_err")

    day_labels = {0: "hoy", 1: "mañana", 2: "pasado"}
    return render_template(
        "comparison.html",
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
        recs_edge=recs_edge,
        recs_low=recs_low,
        brief=brief,
        station_prompts=_STATION_PROMPTS,
        last_ask=last_ask,
        ask_error=ask_error,
        ask_enabled=(_ask_station is not None),
        station_streak=station_streak,
        streak_thresh_f=streak_thresh_f,
        sort_mode=sort_mode,
        edge_current=edge_current,
        edge_analysis=edge_analysis,
    )


@app.route("/ai/station-ask", methods=["POST"])
def ai_station_ask():
    if _ask_station is None:
        return "agent_monitor no disponible", 500
    kind = request.form.get("kind", "").strip()
    sid = request.form.get("station", "").strip().upper()
    return_to = request.form.get("return_to", "/comparison").strip() or "/comparison"
    if return_to not in ("/", "/comparison"):
        return_to = "/comparison"
    if not kind or not sid:
        from urllib.parse import quote
        sep = "&" if "?" in return_to else "?"
        return redirect(f"{return_to}{sep}ask_err={quote('parámetros faltantes')}")
    res = _ask_station(kind, sid)
    if not res.get("ok"):
        from urllib.parse import quote
        sep = "&" if "?" in return_to else "?"
        return redirect(f"{return_to}{sep}ask_err={quote(res.get('error', 'error'))}")
    return redirect(return_to)


@app.route("/ai/station-ask/clear", methods=["POST"])
def ai_station_ask_clear():
    sid = request.form.get("station", "").strip().upper()
    return_to = request.form.get("return_to", "/comparison").strip() or "/comparison"
    if return_to not in ("/", "/comparison"):
        return_to = "/comparison"
    if sid:
        _clear_last_station_ask(sid)
    return redirect(return_to)


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
    return render_template(
        "calibration.html",
        scope=scope,
        total=rep.total_n,
        settled=rep.settled_n,
        brier=rep.brier,
        base_rate=rep.base_rate,
        baseline_brier=rep.baseline_brier,
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


@app.route("/edge")
def edge_view():
    # F3.2a — /edge absorbed into /comparison?sort=edge (audit R1 §I1).
    return redirect("/comparison?sort=edge", code=301)


@app.route("/timing")
def timing_view():
    # F3.2b — /timing folded into /intraday (audit R1 §D2 trap #3).
    return redirect("/intraday", code=301)


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


@app.route("/intraday")
def intraday_view():
    # F3.2b — fusión /timing + /movement (audit R1 §D2 trap #3).
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

    timing, timing_svg = None, ""
    if _peak_timing is not None:
        try:
            timing = _peak_timing.compute(station)
            timing_svg = _timing_hist_svg(
                timing["hour_hist"], timing["current_hour"],
                timing["modal_hour"], timing["p10"], timing["p50"], timing["p90"],
            )
        except Exception as e:
            print(f"intraday timing error: {e}", file=sys.stderr)
            timing = None

    hist = _kalshi.movement_history(station_id, target_date)
    move_bins = hist["bins"]
    move_summary = []
    for b in move_bins:
        pts = [p for p in b["points"]
               if p["yes_mid"] is not None and p["our_p"] is not None]
        if len(pts) < 1:
            continue
        first, last = pts[0], pts[-1]
        move_summary.append({
            "ticker": b["ticker"], "label": b["label"],
            "k_first": first["yes_mid"], "k_last": last["yes_mid"],
            "k_delta": last["yes_mid"] - first["yes_mid"],
            "o_first": first["our_p"], "o_last": last["our_p"],
            "o_delta": last["our_p"] - first["our_p"],
            "n": len(pts),
        })
    move_summary.sort(key=lambda r: -abs(r["k_delta"]))

    selected_ticker = request.args.get("ticker")
    selected = None
    if move_bins:
        if selected_ticker:
            selected = next((b for b in move_bins if b["ticker"] == selected_ticker), None)
        if selected is None:
            selected = _default_move_bin(move_bins, move_summary)

    move_svg = ""
    if selected:
        move_svg = _movement_svg(selected["points"], selected["label"], station.tz)

    return render_template(
        "intraday.html",
        station_id=station_id,
        target_date=target_date.isoformat(),
        timing=timing, timing_svg=timing_svg,
        move_bins=move_bins, move_summary=move_summary,
        selected=selected, move_svg=move_svg,
        market_name=_market_name(station_id),
    )


@app.route("/movement")
def movement_view():
    # F3.2b — /movement folded into /intraday (audit R1 §D2 trap #3).
    qs = request.query_string.decode("utf-8")
    target = "/intraday" + (f"?{qs}" if qs else "")
    return redirect(target, code=301)


# DEFAULT_CROSS = ["KPHX","KLAX","KLAS","KNYC","KBOS"] vivía aquí: el roster de
# cuando eran 5 estaciones. Sobrevivió al paso a 20 y se quedó gobernando tres
# cosas que parecían completas y cubrían un cuarto del sistema, sin fallar ni
# avisar en ninguna de las tres:
#
#   _refresh_peak_status_cache      marcadores de pico en las tarjetas de la home
#   _record_min_snapshots_curated   38 días de datos de mínima, sólo de 5
#   _render_alerts_page             alertas NWS — ocultaba 4 avisos de calor
#                                   extremo activos el 2026-08-18
#
# Las tres usan `SUPPORTED_STATIONS` desde el 2026-08-18. La constante se borra
# para que no pueda reaparecer: el roster es `stations.py` y nada más.

PEAK_POLL_SEC = 180
LOST_POLL_SEC = 2700  # 45 min para estaciones settled / sin info útil

import sqlite3 as _sqlite3_modes
from pathlib import Path as _Path_modes

_AGENT_DB_FOR_MODES = _Path_modes(__file__).resolve().parent.parent / "agent.db"
_ANALYSIS_DB_FOR_MODES = _Path_modes(__file__).resolve().parent / "analysis.db"


def _station_mode(station_id: str) -> str:
    """Devuelve 'observation' | 'lost' | 'normal'.

    - observation: toggle manual en agent.db.station_modes (sube polling a 3 min)
    - lost: auto si último snapshot indica mercado settled (baja polling a 45 min)
    - normal: lógica peak/off-peak existente
    """
    try:
        if _AGENT_DB_FOR_MODES.exists():
            c = _sqlite3_modes.connect(_AGENT_DB_FOR_MODES)
            row = c.execute(
                "SELECT observation FROM station_modes WHERE station=?",
                (station_id,),
            ).fetchone()
            c.close()
            if row and row[0] == 1:
                return "observation"
    except Exception:
        pass
    try:
        if _ANALYSIS_DB_FOR_MODES.exists():
            c = _sqlite3_modes.connect(_ANALYSIS_DB_FOR_MODES)
            row = c.execute(
                """SELECT today_max_obs, ens_med, ens_p10, ens_p90
                   FROM station_snapshots
                   WHERE station=? ORDER BY ts DESC LIMIT 1""",
                (station_id,),
            ).fetchone()
            c.close()
            if row:
                obs, ens_med, p10, p90 = row
                if (obs is not None and ens_med is not None
                        and p10 is not None and p90 is not None):
                    spread = (p90 or 0) - (p10 or 0)
                    if spread <= 0.5 and abs(obs - ens_med) <= 1.0:
                        return "lost"
    except Exception:
        pass
    return "normal"


def _poll_interval_for(station) -> int:
    mode = _station_mode(station.id)
    if mode == "observation":
        return PEAK_POLL_SEC
    if mode == "lost":
        return LOST_POLL_SEC
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


_STATIONS_TTL_SEC = 180
_stations_cache: dict = {"computed_at": None, "results": None}

# F4 — Peak status badges, scoped a las 5 curadas, on-demand + TTL 20 min.
_PEAK_STATUS_TTL_SEC = 1200
_peak_status_cache: dict = {"computed_at": None, "data": {}}


def _peak_badge_from_timing(t: dict) -> dict:
    """Convert peak_timing.compute() output to a compact badge descriptor.
    Umbral post = 0.9 · umbral ventana = 0.2. Devuelve dict con badge_text
    (string listo para render) + badge_kind (pre/window/post/unknown)."""
    if not t:
        return {"badge_text": "—", "badge_kind": "unknown", "prob_already": None}
    p = t.get("prob_already") or 0.0
    cur = t.get("current_hour")
    if p >= 0.9:
        moh = t.get("max_obs_hour")
        max_obs = t.get("max_obs")
        if moh is not None and cur is not None:
            diff = max(0, int(cur) - int(moh))
            text = f"✓ pico -{diff}h"
        else:
            text = "✓ pico"
        return {"badge_text": text, "badge_kind": "post",
                "prob_already": p, "max_obs": max_obs, "max_obs_hour": moh}
    if p >= 0.2:
        return {"badge_text": "● ventana", "badge_kind": "window",
                "prob_already": p}
    modal = t.get("modal_hour")
    if modal is not None and cur is not None:
        diff = max(0, int(modal) - int(cur))
        text = f"↑ ~{diff}h" if diff > 0 else "↑ pronto"
    else:
        text = "↑ pre"
    return {"badge_text": text, "badge_kind": "pre",
            "prob_already": p, "modal_hour": modal}


def _refresh_peak_status_cache() -> dict:
    """Recompute peak_status de las 20 y guardar en cache.

    Iteraba `DEFAULT_CROSS`, o sea 5. Por eso las tarjetas de la home mostraban
    el marcador de pico (`↑ ~3h`, `✓ pico -2h`) sólo en KPHX/KLAX/KLAS/KNYC/KBOS
    y las otras quince salían en blanco: no era irregularidad visual, es que
    nunca se computaban.

    En paralelo porque el caso frío son 20 fetches; con `fetch_ensemble` ya
    caliente (TTL 60 min, lo warmea `_warm_cross_cache`) es casi gratis.
    """
    import peak_timing as _pt
    from predictor import fetch_station as _fs

    def _una(sid):
        try:
            return sid, _peak_badge_from_timing(_pt.compute(_fs(sid)))
        except Exception as e:
            return sid, {"badge_text": "err", "badge_kind": "err",
                         "error": str(e)[:80]}

    sids = list(SUPPORTED_STATIONS)
    with ThreadPoolExecutor(max_workers=min(20, len(sids))) as ex:
        data = dict(ex.map(_una, sids))
    _peak_status_cache["computed_at"] = datetime.now(timezone.utc)
    _peak_status_cache["data"] = data
    return data


# ─── Tabla filtrable 5 estaciones (2026-07-16) ───


_MIN_SNAPSHOT_INTERVAL_SEC = 1200  # 20 min por estación
_min_snapshot_last_ts: dict = {}   # sid → ts float


def _record_min_snapshots_curated() -> None:
    """F8 fase 0: captura p10/p50/p90 del min diario del ensemble, las 20.

    Iteraba `DEFAULT_CROSS`: por eso `prediction_min_snapshots` tenía 38 días de
    historia pero sólo de KPHX/KLAX/KLAS/KNYC/KBOS. El ensemble ya está cacheado
    (TTL 60 min), así que ampliar a 20 **no cuesta fetches**; sólo filas.

    ⚠ Este colector vive en el web, que es lo que ya mató al EWMA del bias
    (`bias_ewma_muerto_2026_08_14`): lo que escribe el web depende de que el web
    corra y de su cadencia. Migrarlo a `analysis_poller` sigue pendiente para la
    v2 — la regla es que la recolección va en el poller y el web sólo sirve.

    Rate-limit por estación a 20 min para no inflar la tabla.
    """
    from predictor import fetch_station as _fs, compute_min_forecast as _cmf
    import calibration as _cal
    import time as _time
    now = _time.time()
    for sid in SUPPORTED_STATIONS:
        last = _min_snapshot_last_ts.get(sid, 0.0)
        if now - last < _MIN_SNAPSHOT_INTERVAL_SEC:
            continue
        try:
            stn = _fs(sid)
            target = datetime.now(stn.tz).date()
            fc = _cmf(stn, target)
            if fc is None:
                continue
            _cal.record_min_snapshot(
                sid, target,
                fc.get("p10"), fc.get("p50"), fc.get("p90"),
                fc.get("n_members"))
            _min_snapshot_last_ts[sid] = now
        except Exception as e:
            print(f"min snapshot {sid}: {e}", file=sys.stderr)


def _compute_stations_results() -> list:
    """Fetch _cross_one para todas las SUPPORTED_STATIONS en paralelo y devuelve
    la lista ordenada por dificultad. Usado tanto por la ruta /stations como por
    el pre-warm — comparten el mismo cache."""
    stations = list(SUPPORTED_STATIONS)
    with ThreadPoolExecutor(max_workers=max(1, len(stations))) as ex:
        results = list(ex.map(lambda s: _cross_one(s, 0), stations))

    def sort_key(r):
        if r.get("error"):
            return (2, r.get("station", ""))
        d = r.get("difficulty") or {}
        score = d.get("score")
        if score is None:
            return (1, r.get("station", ""))
        return (0, score, r.get("station", ""))

    results.sort(key=sort_key)
    return results


@app.route("/station/<sid>")
def station_drilldown(sid):
    """F3.3 — read-only per-station snapshot from _stations_cache.
    Reads only cached data; does not mutate global state. Cache miss → data=None.
    """
    sid = sid.upper()
    if sid not in SUPPORTED_STATIONS:
        return f"estación desconocida: {sid}", 404
    now = datetime.now(timezone.utc)
    cached = _stations_cache.get("results") or []
    cached_at = _stations_cache.get("computed_at")
    cache_age = int((now - cached_at).total_seconds()) if cached_at else -1
    row = next((r for r in cached if r.get("station") == sid), None)

    data, error, target_date, name = None, None, "—", sid
    if row:
        name = row.get("name") or sid
        error = row.get("error")
        target = row.get("target")
        if target is not None:
            target_date = target.isoformat() if hasattr(target, "isoformat") else str(target)
        if not error:
            p50 = row.get("p50_precise") or row.get("p50")
            p10, p90 = row.get("p10"), row.get("p90")
            band = (p90 - p10) / 2.0 if p10 is not None and p90 is not None else None
            diff = row.get("difficulty") or {}
            edge_pp = row["edge"] * 100.0 if row.get("edge") is not None else None
            modal_lbl = None
            if row.get("modal_bin") is not None:
                mb = row["modal_bin"]
                modal_lbl = getattr(mb, "label", None) or f"{mb.bin_lo:.0f}-{mb.bin_hi:.0f}"
            data = {
                "p50": p50, "band": band,
                "diff_label": diff.get("label") or "—",
                "diff_klass": {"fácil": "easy", "normal": "normal",
                                "difícil": "hard", "muy difícil": "veryhard"}.get(
                                    diff.get("label") or "", "normal"),
                "diff_skip": diff.get("skip", False),
                "edge_pp": edge_pp, "modal_lbl": modal_lbl,
            }
    is_active = state is not None and state.station.id == sid
    brief = _station_brief.get(sid) if _station_brief else None
    return render_template(
        "station_drilldown.html",
        sid=sid, name=name, data=data, error=error,
        cache_age=cache_age, target_date=target_date, is_active=is_active,
        brief=brief,
    )


# Cadencia del colector de fondo. Se importa del propio módulo en vez de
# copiarla, para que no puedan divergir; si `analysis_poller` no es importable
# se cae al valor conocido y la vigilancia sigue funcionando.
try:
    from analysis_poller import INTERVAL_S as _AP_INTERVAL_S
except Exception:      # pragma: no cover
    _AP_INTERVAL_S = 600

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


from stations import STATION_IDS as SUPPORTED_STATIONS  # noqa: E402


# Cache de Station objects por id. fetch_station hace GET a NWS API — no
# queremos pagarlo cada tick del poll_loop cuando iteramos SUPPORTED_STATIONS
# para el settle multi-estación. TTL infinito: metadata (lat/lon/tz/name) no
# cambia. Reset via restart si NWS renombra algo.
_station_obj_cache: dict = {}


def _get_cached_station(sid: str):
    hit = _station_obj_cache.get(sid)
    if hit is not None:
        return hit
    try:
        st = fetch_station(sid)
    except Exception:
        return None
    _station_obj_cache[sid] = st
    return st


def _supported_stations() -> list:
    """Return [(id, name), ...] for the curated Kalshi stations.
    Includes the active station even if it's not in the curated list,
    so the dropdown never hides where the user currently is."""
    out = []
    seen = set()
    for sid in SUPPORTED_STATIONS:
        s = _get_cached_station(sid)
        if s is None:
            continue
        out.append((sid, s.name))
        seen.add(sid)
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
                    "kls": kls,
                    "hide_on_skip": True})
    if market and market.get("top_alert"):
        edge_pp = market["top_edge"] * 100
        side = "YES" if edge_pp > 0 else "NO"
        out.append({"k": f"edge {market['top_label']}",
                    "v": f"{edge_pp:+.1f}pp · buy {side}",
                    "kls": "alert" if abs(edge_pp) >= 8 else "warn",
                    "href": "/comparison",
                    "hide_on_skip": True})
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
        cal = _iso.get(None)  # global: el calibrador en uso
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


SYSTEM_TABS = (
    ("health", "salud",    "/system?tab=health"),
    ("notify", "push",     "/system?tab=notify"),
    ("alerts", "alertas",  "/system?tab=alerts"),
    ("about",  "tutorial", "/system?tab=about"),
)


def _render_system_tabs_html(active: str) -> str:
    parts = ['<nav class="system-tabs" style="'
             'display:flex;gap:.4rem;flex-wrap:wrap;'
             'padding:.4rem 0;margin:0 0 .8rem;'
             'border-bottom:1px solid #313244;font-size:13px">']
    parts.append('<a href="/" style="color:#a6adc8;margin-right:.6rem">&larr; inicio</a>')
    for key, label, href in SYSTEM_TABS:
        style = ("padding:.25rem .7rem;border-radius:4px;"
                 "text-decoration:none;")
        if key == active:
            style += "background:#313244;color:#cdd6f4;font-weight:600"
        else:
            style += "color:#89b4fa"
        parts.append(f'<a href="{href}" style="{style}">{label}</a>')
    parts.append('</nav>')
    return "".join(parts)


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


def _analysis_poller_health() -> dict:
    """Salud del `analysis_poller`, el colector de fondo.

    Existe porque hasta el 2026-08-18 **nadie lo vigilaba**: `POLL_STATS` cubre
    sólo el poll loop del web y su estación activa, así que si el poller moría,
    esta página seguía diciendo OK y el hueco se descubría semanas después al
    correr un backtest. Es el proceso que llena `analysis.db`, o sea la base de
    todos los backtests y del corrector de nivel.

    Se mide **lo que llega a la tabla**, no si el proceso está vivo: un poller
    colgado sigue apareciendo en `ps` pero deja de escribir, y eso es lo que
    importa. Mismo criterio de umbrales que `_health_badge` (OK < 2× intervalo,
    WARN < 5×, BAD más allá) para no tener dos escalas distintas en la misma
    página.

    Añade la COBERTURA del último ciclo: un poller vivo que sólo escribe 5 de
    20 estaciones es el modo de fallo del roster fantasma, y sin esto también
    pasaría desapercibido.
    """
    out = {"age_s": None, "age": "nunca", "clase": "bad", "label": "BAD",
           "cobertura": None, "n_total": len(SUPPORTED_STATIONS),
           "intervalo_s": _AP_INTERVAL_S, "error": None}
    try:
        con = sqlite3.connect(
            f"file:{Path(__file__).parent / 'analysis.db'}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA busy_timeout=3000")
            fila = con.execute(
                "SELECT MAX(ts) FROM station_snapshots").fetchone()
            ultimo = fila[0] if fila else None
            if not ultimo:
                return out
            dt = datetime.fromisoformat(ultimo)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            edad = (datetime.now(timezone.utc) - dt).total_seconds()
            # Ventana de un ciclo y medio: cuántas estaciones distintas
            # escribieron de verdad en la última vuelta.
            desde = (dt - timedelta(seconds=_AP_INTERVAL_S * 1.5)).isoformat()
            n = con.execute(
                "SELECT COUNT(DISTINCT station) FROM station_snapshots "
                "WHERE ts >= ?", (desde,)).fetchone()[0]
        finally:
            con.close()
    except Exception as e:
        out["error"] = str(e)[:120]
        return out

    out["age_s"] = int(edad)
    out["age"] = _fmt_age(dt)
    out["cobertura"] = n
    if edad < 2 * _AP_INTERVAL_S:
        out["clase"], out["label"] = "ok", "OK"
    elif edad < 5 * _AP_INTERVAL_S:
        out["clase"], out["label"] = "warn", "WARN"
    else:
        out["clase"], out["label"] = "bad", "BAD"
    # Cobertura incompleta degrada aunque el dato sea fresco.
    if n is not None and n < len(SUPPORTED_STATIONS) and out["clase"] == "ok":
        out["clase"], out["label"] = "warn", "WARN"
    return out


def _render_health_page():
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

    return render_template(
        "status.html",
        health_class=health_class, health_label=health_label,
        uptime=uptime, last_ok=last_ok, last_err=last_err,
        ok_count=ps["ok_count"], err_count=ps["err_count"], err_rate=err_rate,
        poll_sec=_poll_interval_for(state.station) if state else POLL_SEC, cache_ttl=600,
        station=state.station.id if state else "—",
        recent_errors=recent,
        poller=_analysis_poller_health(),
        system_tabs_html=_render_system_tabs_html("health"),
    )


@app.route("/reweight")
def reweight_view():
    if state is None or state.last_snapshot is None:
        return ("No snapshot yet", 503)
    snap = state.last_snapshot
    total = len(snap.ensemble_raw_maxes) or len(snap.ensemble_daily_maxes)
    eff_ratio = f"{(snap.ensemble_eff_n / total * 100):.0f}%" if (
        snap.ensemble_eff_n and total) else "—"
    lo, hi = PEAK_HOURS.get(state.station.id, (12, 16))
    return render_template(
        "reweight.html",
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


@app.route("/precip")
def precip_view():
    if state is None:
        return redirect("/")
    from predictor import build_precip_summary, PRECIP_ANY, PRECIP_NOTABLE, \
        PRECIP_HEAVY, fetch_past_precip, precip_windows_from_past
    def _pct(x):
        return "—" if x is None else f"{100*x:.0f}%"
    past = None
    try:
        past_raw = fetch_past_precip(state.station, hours=8)
        past = precip_windows_from_past(
            past_raw, datetime.now(state.station.tz))
    except Exception:
        past = None
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
    return render_template("precip.html", station=state.station.id, days=days, past=past)


@app.route("/export")
def export_view():
    return render_template("export.html",
                                  tables=list(EXPORT_TABLES.keys()))


@app.route("/bets")
def bets_view():
    # F3.2c — ?view=history sirve historial Brier diario (audit R1 §D2 trap #5).
    if request.args.get("view") == "history":
        return _render_history_view()
    import bets as _bets
    import bets_sweep as _sweep
    station_id = request.args.get("station") or None
    only = request.args.get("only") or "all"
    try:
        sweep_days = max(7, min(90, int(request.args.get("window") or 30)))
    except ValueError:
        sweep_days = 30
    rows = _bets.list_bets(station_id, only=only, limit=300)
    s = _bets.stats(station_id)
    # El histórico completo se calcula sólo para poder decir cuánto se está
    # dejando fuera y por qué. No alimenta ningún titular.
    s_todo = _bets.stats(station_id, since=None)
    pnl_class = "good" if s.pnl > 0 else ("bad" if s.pnl < 0 else "neu")
    roi_s = f"{100*s.roi:+.1f}%" if s.roi is not None else "—"
    win_rate_s = f"{100*s.win_rate:.0f}%" if s.win_rate is not None else "—"
    # Known stations = union of bets' stations + active one
    known = sorted({r["station_id"] for r in _bets.list_bets(limit=10000)})
    if state and state.station.id not in known:
        known = sorted(set(known) | {state.station.id})

    edge_sw = _sweep.sweep_edge_threshold(
        days=sweep_days, station_id=station_id,
        current_thr_pp=_bets.EDGE_THR * 100.0)
    spread_sw = _sweep.sweep_models_spread(
        days=sweep_days, station_id=station_id,
        current_cut_f=_bets.MAX_MODELS_SPREAD_F)
    ext_sw = _sweep.sweep_ext_gate(
        days=sweep_days, station_id=station_id,
        current_gate_f=_bets.EXT_GATE_F)
    sweeps = [
        {"title": "Edge threshold (|edge_pp| ≥ X)",
         "data": edge_sw, "best": _sweep.best_row(edge_sw)},
        {"title": "Models spread cap (descartar día si max-min externos > X)",
         "data": spread_sw, "best": _sweep.best_row(spread_sw)},
        {"title": "External gate (|pred − ext_med| ≤ X para bets de cola)",
         "data": ext_sw, "best": _sweep.best_row(ext_sw)},
    ]

    return render_template(
        "bets.html",
        bets=rows, s=s, pnl_class=pnl_class, roi_s=roi_s,
        win_rate_s=win_rate_s, only=only,
        station_id=station_id or "todas",
        known_stations=known,
        thr=int(_bets.EDGE_THR * 100), stake=int(_bets.STAKE),
        sweeps=sweeps, sweep_days=sweep_days,
        sweep_min_n=_sweep.MIN_N,
        ledger_fix_date=_bets.LEDGER_FIX_DATE,
        n_prefix=(s_todo.n_settled - s.n_settled),
        roi_prefix=(f"{100 * s_todo.roi:+.1f}%" if s_todo.roi is not None else "—"),
    )


def _render_notify_page():
    import notify as _notify
    import uuid
    topic = _notify.TOPIC or "—"
    enabled = _notify.enabled()
    status_class = "on" if enabled else "off"
    status_label = "ACTIVO" if enabled else "INACTIVO"
    status_msg = ("Push habilitado, alerts de edge y settle activos."
                  if enabled
                  else "NTFY_TOPIC no seteada; no se envía nada.")
    return render_template(
        "notify.html",
        topic=topic, enabled=enabled, thr=int(EDGE_ALERT_THR * 100),
        status_class=status_class, status_label=status_label,
        status_msg=status_msg, suggestion=uuid.uuid4().hex[:10],
        system_tabs_html=_render_system_tabs_html("notify"))


def _render_alerts_page():
    """Alertas NWS de las 20 estaciones.

    Iteraba `DEFAULT_CROSS`, o sea 5 — el roster viejo. Con KDFW, KOKC y KSAT
    en calor extremo el 2026-08-18, la página decía lo que pasaba en KPHX y
    KLAS y callaba el resto, sin fallar ni avisar de que faltaban 15.

    `fetch_active` no cachea: cada carga hace una llamada por estación, así que
    en serie 20 estaciones podían encadenar hasta 20 timeouts de 10s. Va en
    paralelo, igual que `_compute_stations_results`.
    """
    if _weather_alerts is None:
        return "weather_alerts module unavailable", 500

    def _una(sid):
        try:
            st = fetch_station(sid)
            return sid, {"name": st.name,
                         "alerts": _weather_alerts.fetch_active(st),
                         "error": None}
        except Exception as e:
            return sid, {"name": "—", "alerts": [], "error": str(e)}

    sids = list(SUPPORTED_STATIONS)
    with ThreadPoolExecutor(max_workers=min(20, len(sids))) as ex:
        resultados = dict(ex.map(_una, sids))

    # Con 20 estaciones la mayoría dice "sin alerts" y entierra las 3 que
    # importan; las que tienen algo suben. Dentro de cada grupo se conserva el
    # orden del roster, que es geográfico E→O.
    per_station = {s: resultados[s] for s in sids if resultados[s]["alerts"]}
    per_station.update({s: resultados[s] for s in sids
                        if not resultados[s]["alerts"]})
    return render_template(
        "alerts.html", per_station=per_station,
        n_con_alertas=sum(1 for v in resultados.values() if v["alerts"]),
        n_total=len(sids),
        system_tabs_html=_render_system_tabs_html("alerts"))


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
    # F3.2c — /history folded into /bets?view=history (audit R1 §D2 trap #5).
    qs = request.query_string.decode("utf-8")
    sep = "&" if qs else ""
    return redirect(f"/bets?view=history{sep}{qs}", code=301)


def _render_history_view():
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
    return render_template("history.html",
                                  station_id=station_id, rows=rows, agg=agg,
                                  market_name=_market_name(station_id))


def _render_about_page():
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
    return render_template(
        "about.html", body=body,
        system_tabs_html=_render_system_tabs_html("about"))


@app.route("/system")
def system_view():
    tab = request.args.get("tab", "health")
    dispatch = {
        "health": _render_health_page,
        "notify": _render_notify_page,
        "alerts": _render_alerts_page,
        "about":  _render_about_page,
    }
    if tab not in dispatch:
        return redirect("/system?tab=health", code=302)
    return dispatch[tab]()


@app.route("/status")
def status_view():
    return redirect("/system?tab=health", code=301)


@app.route("/notify")
def notify_view():
    return redirect("/system?tab=notify", code=301)


@app.route("/alerts")
def alerts_view():
    return redirect("/system?tab=alerts", code=301)


@app.route("/about")
def about_view():
    return redirect("/system?tab=about", code=301)


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
    try:
        import regime as _regime
        rt = _regime.classify(snap, station.id, snap.station_local)
        if rt.bet_action == "skip":
            print(f"bets skip: {station.id} regime={rt.tag} ({rt.reason})",
                  file=sys.stderr)
            return
    except Exception:
        pass
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
    _cal = _iso.get(None)  # global, ver nota en /ladder
    _cal_active = (_cal is not None and _cal.n_fit >= _iso.MIN_N
                   and _cal.n_days >= _iso.MIN_DAYS)
    _cal_for_apply = _cal if _cal_active else None
    _diff_score = None
    if _difficulty is not None:
        try:
            maxes = sorted(snap.ensemble_daily_maxes or [])
            n_mem = len(snap.ensemble_raw_maxes) or len(maxes) or 31
            clim_pct = (getattr(snap.climatology, "percentile", None)
                        if snap.climatology is not None else None)
            _dd = _difficulty.compute(
                ens_p10=maxes[int(len(maxes) * 0.1)] if maxes else None,
                ens_p90=maxes[int(len(maxes) * 0.9)] if maxes else None,
                eff_n=snap.ensemble_eff_n, total_members=n_mem,
                clim_percentile=clim_pct, p_notable_precip=None,
                regime_breaks=len(snap.regime_break_hours or []))
            _diff_score = _dd.score
        except Exception:
            pass
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
                anchor_ctx["ext_diff"], anchor_ctx["lam"],
                ext_used=anchor_ctx.get("nudge_ext_used", 0.0))
        edge_abs = abs(op_ - ym)
        try:
            _bets.maybe_bet(station.id, target, r["ticker"],
                            r["bin_lo"], r["bin_hi"], r.get("label") or "",
                            op_, ym, models_spread_f=models_spread,
                            our_pred_f=pred_med,
                            ext_diff_f=gate_ext_diff,
                            bias_info=getattr(snap, "bias_info", None),
                            difficulty_score=_diff_score,
                            yes_bid=r.get("yes_bid"),
                            yes_ask=r.get("yes_ask"),
                            station_local_hour=snap.station_local.hour)
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
            # Fable #5 histéresis 2-ciclos: sólo cambiar peak_state si el nuevo
            # candidate coincide con el del poll anterior. Evita parpadeo por
            # una lectura ruidosa (5-min feed rounding). El candidate ya viene
            # calculado en build_snapshot; peak_state es el confirmado.
            # Bug #2 Fable 2026-07-21: histéresis SOLO para transiciones hacia
            # PLATEAU/CONFIRMED. PRE_WINDOW/RISING son deterministas
            # (reloj + distribución ensemble fresh); aplicar histéresis ahí es
            # lo que atrapaba pre-ventana en "confirmado" viejo.
            prev_cand = getattr(state.last_snapshot, "peak_state_candidate", None)
            prev_state = getattr(state.last_snapshot, "peak_state", None)
            new_cand = snap.peak_state_candidate
            hysteresis_targets = (PeakState.PLATEAU, PeakState.CONFIRMED)
            if (new_cand in hysteresis_targets
                    and new_cand != prev_cand
                    and prev_state is not None):
                snap.peak_state = prev_state
                snap.peak_status = peak_state_display(prev_state)
                # B7: la línea narrable arranca su parte de estado con el
                # display del candidate. Si histéresis revierte, sustituir
                # sólo la cabeza. narrative_line siempre empieza con display.
                cand_display = peak_state_display(new_cand)
                if snap.narrative_line.startswith(cand_display):
                    snap.narrative_line = (
                        snap.peak_status
                        + snap.narrative_line[len(cand_display):]
                    )
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
    """Pre-fetch ensemble + market + peak_window para SUPPORTED_STATIONS
    así /cross, /stations y la primera navegación por estación sirven
    calientes. La primera vuelta tras boot toma ~40s (cold fetch paralelo
    de 20 estaciones); las siguientes son ~instantáneas porque el resultado
    de /stations se guarda en _stations_cache (TTL 3min) y peak_window
    TTL 24h.

    Runs in a thread; failures are silent (cache miss just means slow page)."""
    try:
        results = _compute_stations_results()
        _stations_cache["computed_at"] = datetime.now(timezone.utc)
        _stations_cache["results"] = results
    except Exception as e:
        print(f"warm_cross_cache error: {e}", file=sys.stderr)

    # Pre-warm peak_window una vez al día (cache 24h) para que la primera
    # navegación a cada estación renderice el reloj sin esperar al archive.
    try:
        import peak_window as _pw
        from predictor import fetch_station as _fs

        def _warm_pw(sid: str):
            try:
                _pw.get(_fs(sid))
            except Exception:
                pass

        stations = list(SUPPORTED_STATIONS)
        with ThreadPoolExecutor(max_workers=min(20, len(stations))) as ex:
            list(ex.map(_warm_pw, stations))
    except Exception as e:
        print(f"warm peak_window error: {e}", file=sys.stderr)

    # F8 fase 0: snapshot forecast de min diario para las 5 curadas.
    # Ensemble ya está cacheado (TTL 60 min), así que costo neto es 0.
    # Rate-limit a 1 snapshot/estación cada 20 min para no inflar la tabla.
    try:
        _record_min_snapshots_curated()
    except Exception as e:
        print(f"min snapshot capture error: {e}", file=sys.stderr)

    # Pre-warm peak_window una vez al día (cache 24h). 20 fetches al archive,
    # cero costo si ya cacheados.
    try:
        import peak_window as _pw
        from predictor import fetch_station

        def _warm_pw(sid: str):
            try:
                _pw.get(fetch_station(sid))
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=min(20, len(stations))) as ex:
            list(ex.map(_warm_pw, stations))
    except Exception as e:
        print(f"warm peak_window error: {e}", file=sys.stderr)


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
                    try:
                        _cov = _calibration.settle_coverage(state.station)
                        if _cov:
                            print(f"settle_coverage {state.station.id}: "
                                  f"{len(_cov)} days", file=sys.stderr)
                    except Exception as e:
                        print(f"settle_coverage primary error: {e}",
                              file=sys.stderr)
                    last_settle_day = today
                    try:
                        _check_settle_alerts(state.station, settled)
                    except Exception as e:
                        print(f"settle notify error: {e}", file=sys.stderr)
                except Exception as e:
                    print(f"settle_pending error: {e}", file=sys.stderr)
                # Fable/Codex retro 2026-07-06: settle_pending histórico solo
                # corría para state.station → KATL/KDEN/otros nunca settleaban
                # (KMDW/KMIA/KLAX 2-4 semanas stale). Barrido diario del resto
                # de estaciones curadas después del primary. Errores por
                # estación se logean pero no rompen el loop.
                for _sid in SUPPORTED_STATIONS:
                    if _sid == state.station.id:
                        continue
                    try:
                        _st = _get_cached_station(_sid)
                        if _st is None:
                            continue
                        _settled = _calibration.settle_pending(_st)
                        if _settled:
                            print(f"settle_pending {_sid}: {len(_settled)} days",
                                  file=sys.stderr)
                        # Cobertura: settle_pending solo mira
                        # prediction_snapshots, que no existen para estaciones
                        # que nunca fueron activas -> 16 de 20 sin settle real.
                        _cov = _calibration.settle_coverage(_st)
                        if _cov:
                            print(f"settle_coverage {_sid}: {len(_cov)} days",
                                  file=sys.stderr)
                    except Exception as e:
                        print(f"settle_pending {_sid} error: {e}",
                              file=sys.stderr)
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


# ============================================================
# /grid — vista heatmap de las 20 estaciones (mobile-first)
# Lee analysis.db (poller cada 10 min) para no quemar Open-Meteo.
# Sin live ensemble fetch — los snapshots ya están frescos.
# ============================================================

_GRID_STATION_CITY = {
    "KPHX": "Phoenix", "KLAX": "Los Angeles", "KLAS": "Las Vegas",
    "KNYC": "New York (Central Park)", "KBOS": "Boston", "KMIA": "Miami",
    "KMDW": "Chicago", "KHOU": "Houston", "KSFO": "San Francisco",
    "KAUS": "Austin", "KDEN": "Denver", "KSAT": "San Antonio",
    "KDCA": "Washington", "KDFW": "Dallas", "KPHL": "Philadelphia",
    "KSEA": "Seattle", "KATL": "Atlanta", "KMSY": "New Orleans",
    "KOKC": "Oklahoma City", "KMSP": "Minneapolis",
}
_GRID_ANALYSIS_DB = _Path_modes(__file__).resolve().parent / "analysis.db"


if __name__ == "__main__":
    main()
