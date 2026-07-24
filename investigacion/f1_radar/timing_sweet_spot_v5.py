#!/usr/bin/env python3
"""6º corte v5 — bracket de piso de precio x variante de our_p, con settle real.

Tres cambios sobre v3:

1. SETTLE REAL. v3 usaba MAX(today_max_obs) de station_snapshots como settle.
   Comparado con day_outcomes (NWS CLI, lo que Kalshi realmente usa) el entero
   difiere en 70% de los dias, con errores de hasta -11F. v5 usa day_outcomes
   y descarta los dias sin settle CLI en vez de inventarlo.

2. PISO DE PRECIO. v3 tenia KAL_PRICE_MAX=0.20 sin piso, asi que bins a 0.5-1c
   pasaban el filtro de ratio trivialmente (our_p 0.97 / 0.005 = 194x). El
   barrido separa "edge en bins vivos" de "loteria de cola".

3. DEDUP HONESTO. v3 emite un trigger por slot de 15 min, asi que el mismo
   (dia, bin) cuenta decenas de veces. v5 toma el PRIMER trigger de cada
   (estacion, dia, bin) — una entrada por oportunidad, como en la vida real.

Ademas reporta las dos variantes de our_p: raw (conteo del ensemble, 33
miembros con Laplace) y calibrated (post-isotonica).

Nota: v3 documentaba criterios "cur >= max_obs" y "ventana OPEN" pero nunca
los implemento — solo ratio + precio + cutoff de hora. v5 mantiene eso
igual para que la comparacion sea limpia.
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path.home() / "predictor-pi"
sys.path.insert(0, str(BASE / "weather-predictor"))
from stations import PEAK_HOURS

AN_DB = BASE / "weather-predictor" / "analysis.db"
CAL_DB = BASE / "weather-predictor" / "calibration.db"

STATIONS = ["KMIA", "KIAH", "KAUS", "KATL", "KMSY",
            "KNYC", "KBOS", "KDCA", "KPHL", "KPHX"]

TZ_MAP = {
    "KMIA": "America/New_York", "KATL": "America/New_York",
    "KIAH": "America/Chicago", "KAUS": "America/Chicago", "KMSY": "America/Chicago",
    "KNYC": "America/New_York", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KPHL": "America/New_York",
    "KPHX": "America/Phoenix",
}

RATIO_MIN = 2.0
KAL_PRICE_MAX = 0.20
FLOORS = [0.00, 0.02, 0.03, 0.05, 0.08, 0.10]
POST_WIN_CUTOFF_H = 1
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def parse_ticker_date(ticker):
    try:
        dp = ticker.split("-")[1]
        return date(int(dp[:2]) + 2000, MONTHS[dp[2:5]], int(dp[5:]))
    except Exception:
        return None


def load_settles_cli():
    c = sqlite3.connect(CAL_DB); c.row_factory = sqlite3.Row
    out = {}
    for r in c.execute("""SELECT station_id, date, max_obs_f FROM day_outcomes
                          WHERE max_obs_f IS NOT NULL"""):
        out[(r["station_id"], r["date"])] = int(round(r["max_obs_f"]))
    c.close()
    return out


def load_settles_proxy():
    """El settle que usaba v3, solo para cuantificar el sesgo."""
    c = sqlite3.connect(AN_DB); c.row_factory = sqlite3.Row
    out = {}
    for r in c.execute("""SELECT station, date(ts) d, MAX(today_max_obs) m
                          FROM station_snapshots
                          WHERE today_max_obs IS NOT NULL AND today_max_obs > -900
                          GROUP BY station, date(ts)"""):
        out[(r["station"], r["d"])] = int(round(r["m"]))
    c.close()
    return out


def candidates(sid):
    """Todos los (dia, bin_encima_del_modo) con su primera cotizacion por slot."""
    tz = ZoneInfo(TZ_MAP[sid])
    peak_hi = PEAK_HOURS[sid][1]
    c = sqlite3.connect(AN_DB); c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT ts, ticker, label, bin_lo, bin_hi, yes_mid, our_p, our_p_calibrated
        FROM kalshi_snapshots
        WHERE station = ? AND yes_mid IS NOT NULL
        ORDER BY ts""", (sid,)).fetchall()
    c.close()

    slots = defaultdict(list)
    for r in rows:
        td = parse_ticker_date(r["ticker"])
        if td is None:
            continue
        loc = datetime.fromisoformat(r["ts"]).astimezone(tz)
        if td != loc.date() or loc.hour >= peak_hi + POST_WIN_CUTOFF_H:
            continue
        slots[(loc.date().isoformat(), loc.hour, loc.minute // 15)].append(dict(r))

    out = []
    for slot, bins in sorted(slots.items()):
        latest = {}
        for k in bins:
            key = (k["bin_lo"], k["bin_hi"])
            if key not in latest or k["ts"] > latest[key]["ts"]:
                latest[key] = k
        finite = [k for k in latest.values() if k["bin_hi"] < 200 and k["bin_lo"] > -50]
        if len(finite) < 2:
            continue
        finite.sort(key=lambda x: x["bin_lo"])
        modo = max(range(len(finite)), key=lambda i: finite[i]["yes_mid"])
        if modo + 1 >= len(finite):
            continue
        t = finite[modo + 1]
        if t["yes_mid"] is None or t["yes_mid"] <= 0:
            continue
        out.append({"day": slot[0], "hour": slot[1], "label": t["label"],
                    "bin_lo": t["bin_lo"], "bin_hi": t["bin_hi"],
                    "kal": t["yes_mid"], "raw": t["our_p"],
                    "cal": t["our_p_calibrated"]})
    return out


def run(cands, settles, pkey, floor):
    """Dedup por (sid, dia, bin): primer trigger que cumple. Devuelve stats."""
    seen = set()
    per_sta = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    skipped_no_settle = 0
    for sid, cl in cands.items():
        for c in cl:
            p = c[pkey]
            if p is None:
                continue
            kal = c["kal"]
            if not (floor <= kal <= KAL_PRICE_MAX):
                continue
            if p / kal < RATIO_MIN:
                continue
            key = (sid, c["day"], c["bin_lo"], c["bin_hi"])
            if key in seen:
                continue
            st = settles.get((sid, c["day"]))
            if st is None:
                skipped_no_settle += 1
                continue
            seen.add(key)
            d = per_sta[sid]
            d["n"] += 1
            if c["bin_lo"] <= st <= c["bin_hi"]:
                d["w"] += 1
                d["pnl"] += 1 - kal
            else:
                d["pnl"] -= kal
    return per_sta, skipped_no_settle


def agg(per_sta):
    n = sum(d["n"] for d in per_sta.values())
    w = sum(d["w"] for d in per_sta.values())
    pnl = sum(d["pnl"] for d in per_sta.values())
    return n, w, pnl


def main():
    cands = {s: candidates(s) for s in STATIONS}
    print("candidatos (bin encima del modo, pre-filtros) por estacion:")
    print("  " + "  ".join(f"{s}={len(cands[s])}" for s in STATIONS))

    cli, proxy = load_settles_cli(), load_settles_proxy()

    for label, settles in (("SETTLE REAL (NWS CLI, day_outcomes)", cli),
                           ("SETTLE PROXY (el de v3, MAX today_max_obs)", proxy)):
        print(f"\n{'='*78}\n{label}\n{'='*78}")
        print(f"{'piso':>6} | {'our_p RAW':^30} | {'our_p CALIBRADO':^30}")
        print(f"{'':>6} | {'N':>4} {'wins':>5} {'win%':>6} {'ROI%':>8} | {'N':>4} {'wins':>5} {'win%':>6} {'ROI%':>8}")
        print("-" * 78)
        for fl in FLOORS:
            cells = []
            for pkey in ("raw", "cal"):
                ps, _ = run(cands, settles, pkey, fl)
                n, w, pnl = agg(ps)
                roi = (pnl / n * 100) if n else 0.0
                wr = (w / n * 100) if n else 0.0
                cells.append(f"{n:>4} {w:>5} {wr:>5.1f}% {roi:>+7.1f}%")
            print(f"{fl:>6.2f} | {cells[0]} | {cells[1]}")

    # desglose por estacion con settle real, sin piso y con piso 0.05
    print(f"\n{'='*78}\nDESGLOSE POR ESTACION — settle real, our_p RAW\n{'='*78}")
    print(f"{'STA':6}{'piso 0.00':>28}{'piso 0.05':>28}")
    print(f"{'':6}{'N':>6}{'w':>4}{'win%':>7}{'ROI%':>9}{'N':>7}{'w':>4}{'win%':>7}{'ROI%':>9}")
    a, _ = run(cands, cli, "raw", 0.00)
    b, _ = run(cands, cli, "raw", 0.05)
    for s in STATIONS:
        da, db = a.get(s, {"n":0,"w":0,"pnl":0}), b.get(s, {"n":0,"w":0,"pnl":0})
        ra = (da["pnl"]/da["n"]*100) if da["n"] else 0
        rb = (db["pnl"]/db["n"]*100) if db["n"] else 0
        wa = (da["w"]/da["n"]*100) if da["n"] else 0
        wb = (db["w"]/db["n"]*100) if db["n"] else 0
        print(f"{s:6}{da['n']:>6}{da['w']:>4}{wa:>6.0f}%{ra:>+8.1f}%{db['n']:>7}{db['w']:>4}{wb:>6.0f}%{rb:>+8.1f}%")

    _, miss = run(cands, cli, "raw", 0.00)
    print(f"\ntriggers descartados por falta de settle CLI: {miss}")


if __name__ == "__main__":
    main()
