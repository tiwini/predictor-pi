#!/usr/bin/env python3
"""¿Desde qué hora local la predicción del día ya es fiable?

Dos tablas, por estación × hora local:

  A) PREDICCIÓN — % de días con |pred - settle| <= tol. Responde "¿desde
     qué hora le puedo creer al modelo?".
  B) OBSERVACIÓN — % de días en que a esa hora la obs acumulada ya
     igualaba el settle. Responde "¿desde qué hora la máxima ya está puesta?".

El settle sale SIEMPRE de `day_outcomes` (NWS CLI). Nunca de
`MAX(today_max_obs)` — ver memoria feedback_settle_proxy_vs_nws_cli.

⚠ La tabla B es un PISO, no una medición limpia: hasta el fix del
2026-07-25 (commit ce35c94) `today_max_obs` perdía observaciones en las
estaciones que publican a :52 y :56, y además no era monótono. El
histórico subestima cuándo la obs alcanzó el settle.

Uso:  python3 convergencia_horaria.py [--pred cal|ens] [--tol 1.5] [--min-n 8]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/popeye/predictor-pi/weather-predictor")
from streaks import STATION_TZ  # noqa: E402  (mapa id -> zona IANA)

ANALYSIS_DB = "/home/popeye/predictor-pi/weather-predictor/analysis.db"
CALIB_DB = "/home/popeye/predictor-pi/weather-predictor/calibration.db"
HOURS = list(range(6, 21))  # 06:00-20:00 local
THRESHOLD = 70  # % de aciertos que consideramos "fiable"


def load_settles(path: str) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    rows = con.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE max_obs_f IS NOT NULL").fetchall()
    con.close()
    return {(s, d): m for s, d, m in rows}


def load_snapshots(path: str, pred_col: str) -> list:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    rows = con.execute(
        f"SELECT station, ts, {pred_col}, today_max_obs FROM station_snapshots "
        f"WHERE {pred_col} IS NOT NULL").fetchall()
    con.close()
    return rows


def first_sustained(pcts: dict, hours: list, threshold: int) -> int | None:
    """Primera hora desde la cual TODAS las horas medidas siguientes están
    en o por encima del umbral. None si nunca se sostiene."""
    best = None
    for h in reversed(hours):
        v = pcts.get(h)
        if v is None:          # celda sin N suficiente: no rompe la racha
            continue
        if v >= threshold:
            best = h
        else:
            break
    return best


def render(title: str, pcts_by_st: dict, days: dict, stations: list,
           note: str) -> dict:
    print(f"\n{title}")
    head = "  ".join(f"{h:02d}" for h in HOURS)
    print(f"{'STA':5} {'días':>4}   {head}   conv")
    print(f"{'':5} {'':>4}   " + "  ".join("--" for _ in HOURS) + "   ----")
    conv = {}
    for st in stations:
        pcts = pcts_by_st.get(st, {})
        cells = []
        for h in HOURS:
            v = pcts.get(h)
            cells.append(" ·" if v is None else f"{v:2.0f}")
        c = first_sustained(pcts, HOURS, THRESHOLD)
        conv[st] = c
        flag = "" if days[st] >= 10 else " ⚠"
        print(f"{st:5} {days[st]:>4}{flag:2} " + "  ".join(cells)
              + f"   {(str(c) + 'h') if c is not None else ' —':>4}")
    print(f"\n{note}")
    return conv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", choices=("ens", "cal"), default="cal")
    ap.add_argument("--tol", type=float, default=1.5)
    ap.add_argument("--min-n", type=int, default=8)
    args = ap.parse_args()

    pred_col = "ens_med" if args.pred == "ens" else "pred_calibrated_f"
    settles = load_settles(CALIB_DB)
    rows = load_snapshots(ANALYSIS_DB, pred_col)

    latest: dict = {}
    for station, ts_s, pred, max_obs in rows:
        tzname = STATION_TZ.get(station)
        if tzname is None:
            continue
        try:
            ts = datetime.fromisoformat(ts_s)
        except ValueError:
            continue
        loc = ts.astimezone(ZoneInfo(tzname))
        if loc.hour not in HOURS:
            continue
        key = (station, loc.date().isoformat(), loc.hour)
        prev = latest.get(key)
        if prev is None or loc > prev[0]:
            latest[key] = (loc, pred, max_obs)

    cells: dict = defaultdict(list)
    days_seen: dict = defaultdict(set)
    dropped_days = set()
    day_peak: dict = {}
    for (station, day, hour), (_, _, max_obs) in latest.items():
        if max_obs is not None and max_obs > -900:
            k = (station, day)
            if max_obs > day_peak.get(k, -999.0):
                day_peak[k] = max_obs
    for (station, day, hour), (_, pred, max_obs) in latest.items():
        settle = settles.get((station, day))
        if settle is None:
            dropped_days.add((station, day))
            continue
        peak = day_peak.get((station, day))
        peak_done = (max_obs is not None and peak is not None
                     and max_obs >= peak - 0.05)
        cells[(station, hour)].append((pred - settle, peak_done, settle))
        days_seen[station].add(day)

    if not cells:
        print("Sin solape entre snapshots y settles.")
        return 1

    label = "ens_med (crudo)" if args.pred == "ens" else "pred_calibrated_f"
    print(f"Predicción: {label} · acierto = |pred − settle| ≤ {args.tol}°F")
    print(f"Settle: day_outcomes (NWS CLI). Días-estación descartados por "
          f"no tener settle: {len(dropped_days)}")
    print(f"Celdas con menos de {args.min_n} días se muestran como '·'. "
          f"⚠ = estación con menos de 10 días.")

    pred_pcts: dict = defaultdict(dict)
    obs_pcts: dict = defaultdict(dict)
    for (st, h), vals in cells.items():
        if len(vals) < args.min_n:
            continue
        pred_pcts[st][h] = round(
            sum(1 for e, _, _ in vals if abs(e) <= args.tol) / len(vals) * 100)
        obs_pcts[st][h] = round(
            sum(1 for _, o, _ in vals if o) / len(vals) * 100)

    stations = sorted(days_seen, key=lambda s: -len(days_seen[s]))
    days = {s: len(days_seen[s]) for s in stations}

    conv_pred = render(
        f"A) PREDICCIÓN — % de días con |{label} − settle| ≤ {args.tol}°F",
        pred_pcts, days, stations,
        f"conv = primera hora desde la cual el acierto se mantiene ≥{THRESHOLD}% "
        f"el resto del día.")

    render(
        "B) OBSERVACIÓN — % de días en que el pico observado YA había ocurrido",
        obs_pcts, days, stations,
        "Referencia interna (el max de obs del propio día), así que no arrastra\n"
        "el gap contra el CLI. Sí arrastra el bug de obs perdidas anterior a\n"
        "ce35c94: si faltaban obs tardías, el pico parece más temprano.")

    # Cuánto se queda corta nuestra obs contra el settle del CLI.
    print("\nGAP OBS→CLI — mediana de (max obs del día − settle CLI):")
    gaps = defaultdict(list)
    for (st, day), peak in day_peak.items():
        settle = settles.get((st, day))
        if settle is not None:
            gaps[st].append(peak - settle)
    for st in stations:
        g = gaps.get(st, [])
        if len(g) < args.min_n:
            continue
        alcanza = sum(1 for x in g if x >= -0.5) / len(g) * 100
        print(f"  {st:5} N={len(g):>3}  mediana {statistics.median(g):+5.1f}°F  "
              f"· alcanza el settle en {alcanza:3.0f}% de los días")
    print("  Negativo = nuestra obs se queda corta. Es el gap 5-min vs 1-min\n"
          "  ASOS del que sale el CLI — ce35c94 no lo cierra, sólo recupera\n"
          "  las obs que el filtro de minutos descartaba.")

    print(f"\n{'STA':5} {'días':>4} {'fiable':>7} {'|err|':>7} {'bias':>7}  "
          f"lectura")
    for st in stations:
        h = conv_pred.get(st)
        if h is None:
            print(f"{st:5} {days[st]:>4} {'—':>7} {'':>7} {'':>7}  "
                  f"nunca sostiene {THRESHOLD}%")
            continue
        errs = [e for e, _, _ in cells[(st, h)]]
        print(f"{st:5} {days[st]:>4} {str(h) + ':00':>7} "
              f"{statistics.median(abs(e) for e in errs):>6.1f}° "
              f"{statistics.median(errs):>+6.1f}°  "
              f"N={len(errs)} días en esa hora")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
