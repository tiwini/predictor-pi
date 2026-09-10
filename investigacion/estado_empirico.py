#!/usr/bin/env python3
"""Estado empírico del sistema, para la auditoría externa. Sólo lee."""
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

WP = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(WP))
from stations import STATION_TZ, PEAK_HOURS  # noqa

UTC = ZoneInfo("UTC")
an = sqlite3.connect(f"file:{WP/'analysis.db'}?mode=ro", uri=True)
an.row_factory = sqlite3.Row
cal = sqlite3.connect(f"file:{WP/'calibration.db'}?mode=ro", uri=True)
cal.row_factory = sqlite3.Row

print("## 1. Muestra por estación\n")
print("`n_snap` = snapshots en `analysis.db`; `n_settle` = días liquidados con")
print("CLI del NWS en `calibration.db.day_outcomes`. La hora de referencia es")
print("`PEAK_HOURS[st][0] - 2` local, la misma con la que se han hecho los")
print("backtests del corrector.\n")
print("| est | n_snap | desde | n_settle | 1er settle | hora ref |")
print("|---|---|---|---|---|---|")
sts = [r[0] for r in an.execute(
    "SELECT DISTINCT station FROM station_snapshots ORDER BY station")]
for st in sts:
    r = an.execute("SELECT COUNT(*) n, MIN(date(ts)) d FROM station_snapshots "
                   "WHERE station=?", (st,)).fetchone()
    s = cal.execute("SELECT COUNT(*) n, MIN(date) d FROM day_outcomes "
                    "WHERE station_id=? AND max_obs_f IS NOT NULL",
                    (st,)).fetchone()
    href = PEAK_HOURS[st][0] - 2
    print(f"| {st} | {r['n']} | {r['d']} | {s['n']} | {s['d']} | {href}h |")

# ── error y cobertura de la banda a la hora de referencia ────────────────────
print("\n## 2. Error y cobertura de la banda, a la hora de referencia\n")
print("Para cada día liquidado se toma el snapshot más cercano a la hora de")
print("referencia (±45 min) y se compara `ens_med` contra el settle.")
print("`cobertura` = % de días con el settle dentro de [ens_p10, ens_p90],")
print("una banda que se publica como 80%.\n")
print("| est | N | \\|err\\| medio | mediana | p90 | sesgo medio | cobertura p10-p90 | ancho medio |")
print("|---|---|---|---|---|---|---|---|")

resumen = {}
for st in sts:
    tz = ZoneInfo(STATION_TZ[st])
    href = PEAK_HOURS[st][0] - 2
    errs, dentro, anchos = [], 0, []
    dias = cal.execute("SELECT date, max_obs_f FROM day_outcomes "
                       "WHERE station_id=? AND max_obs_f IS NOT NULL "
                       "ORDER BY date", (st,)).fetchall()
    for d in dias:
        day = datetime.strptime(d["date"], "%Y-%m-%d").date()
        ref = datetime.combine(day, datetime.min.time(), tz) + timedelta(hours=href)
        lo = (ref - timedelta(minutes=45)).astimezone(UTC)
        hi = (ref + timedelta(minutes=45)).astimezone(UTC)
        r = an.execute(
            """SELECT ens_med, ens_p10, ens_p90 FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts)-JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             ref.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is None:
            continue
        errs.append(r["ens_med"] - d["max_obs_f"])
        if r["ens_p10"] is not None and r["ens_p90"] is not None:
            anchos.append(r["ens_p90"] - r["ens_p10"])
            if r["ens_p10"] - 1e-9 <= d["max_obs_f"] <= r["ens_p90"] + 1e-9:
                dentro += 1
    if not errs:
        continue
    ab = sorted(abs(e) for e in errs)
    cob = 100.0 * dentro / len(anchos) if anchos else float("nan")
    resumen[st] = (len(errs), statistics.mean(ab), cob)
    print(f"| {st} | {len(errs)} | {statistics.mean(ab):.2f} | "
          f"{statistics.median(ab):.2f} | {ab[int(0.9*len(ab))-1]:.2f} | "
          f"{statistics.mean(errs):+.2f} | {cob:.1f}% | "
          f"{statistics.mean(anchos):.2f} |" if anchos else "")

tot_n = sum(v[0] for v in resumen.values())
tot_err = statistics.mean([v[1] for v in resumen.values()])
tot_cob = statistics.mean([v[2] for v in resumen.values()])
print(f"\n**Conjunto: N={tot_n} station-days, |err| medio de las medias "
      f"{tot_err:.2f}°F, cobertura media {tot_cob:.1f}% para una banda "
      f"nominal del 80%.**")

# ── Brier semanal ────────────────────────────────────────────────────────────
print("\n## 3. Brier nuestro contra el de Kalshi (tabla `brier_weekly`)\n")
try:
    rows = cal.execute(
        "SELECT * FROM brier_weekly ORDER BY week_iso DESC, station_id LIMIT 45"
    ).fetchall()
    if rows:
        cols = rows[0].keys()
        print("| " + " | ".join(cols) + " |")
        print("|" + "---|" * len(cols))
        for r in rows:
            print("| " + " | ".join(
                f"{r[c]:.4f}" if isinstance(r[c], float) else str(r[c])
                for c in cols) + " |")
except sqlite3.Error as e:
    print(f"(sin tabla brier_weekly: {e})")

# ── settles pendientes ───────────────────────────────────────────────────────
print("\n## 4. Integridad del settle\n")
r = cal.execute("SELECT COUNT(*) n FROM day_outcomes "
                "WHERE max_obs_f IS NULL").fetchone()
print(f"- Días registrados sin settle (esperando CLI): **{r['n']}**")
try:
    r = cal.execute("SELECT source, COUNT(*) n FROM day_outcomes "
                    "GROUP BY source").fetchall()
    print("- Origen de los settles: "
          + ", ".join(f"`{x['source']}`={x['n']}" for x in r))
except sqlite3.Error:
    pass
print("\n(generado por `investigacion/estado_empirico.py`, sólo lectura)")
