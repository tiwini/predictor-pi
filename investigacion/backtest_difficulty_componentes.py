#!/usr/bin/env python3
"""¿Alguna componente de `difficulty` predice el error? — backtest por componente.

CONTEXTO
--------
`difficulty.compute` combina con `overall = max(componentes)`, así que una sola
saturada fija el score. El gate se desactivó el 2026-07-06 (`bets.
DIFFICULTY_BLOCK_THR = 999.0`) con esta justificación escrita en el código:

    Spearman(p90-p10, |err|) = +0.004 (p=0.97, N=134)
    Readmision solo si rho > 0.3 out-of-sample sobre >=100 station-days nuevos.

Ese test midió **p90-p10**, o sea SÓLO el componente de spread. La anomalía
climática y el eff_n nunca se pusieron a prueba, y son los que más bloquean:
en julio el 31% de los bloqueos son sólo-por-anomalía (KMIA 59.8% de sus
snapshots). Este script mide las cuatro por separado.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar ningún resultado. No editar después de la primera
corrida: si el análisis cambia, se anota como una decisión nueva y fechada.

H0: ninguna componente de difficulty correlaciona con |error| de la predicción.

MÉTRICA PRIMARIA
  Spearman(componente, |pred - settle|) por componente, sobre station-days.
  `pred` = ens_med del snapshot de referencia; `settle` = day_outcomes (NWS).

VENTANA Y UNIDAD
  Un station-day aporta UN punto: el snapshot más cercano a las 12:00 LOCAL.
  Se fija a mediodía porque es antes de toda ventana de pico del roster
  (la más temprana abre a las 12h) — así el error medido es de predicción y
  no de observación ya consumada.

EXCLUSIONES (decididas de antemano)
  - station-days sin settle NWS.
  - snapshots a más de 90 min del mediodía local.
  - KIAH (fuera del roster desde el 2026-07-25).

CRITERIO DE DECISIÓN
  Se adopta el listón que la propia casa escribió al desactivar el gate:
    READMITIR una componente como gate  <=>  rho > 0.30  Y  N >= 100 station-days
                                              Y  p < 0.01
    ZONA GRIS (no actuar, volver a medir): 0.15 < rho <= 0.30
    DESCARTAR como gate:                   rho <= 0.15

  Si ninguna componente pasa, la conclusión es que `difficulty` no sirve como
  gate en NINGUNA de sus partes y la doctrina de ">70 no operar" debe retirarse
  de la lectura, no sólo del código.

SECUNDARIO (orientativo, sin poder de decisión por N chico)
  ROI de las bets settleadas post-2026-07-07 partido por banda de anomalía.
  No decide nada: se reporta para ver si apunta en la misma dirección.
=============================================================================
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
import difficulty as D                        # noqa: E402
from stations import STATION_TZ               # noqa: E402

ANALYSIS_DB = BASE / "analysis.db"
CALIB_DB = BASE / "calibration.db"
NOON_TOLERANCE_MIN = 90
LEDGER_FIX = "2026-07-07"
TOTAL_MEMBERS = 31


def spearman(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """rho de Spearman + p aproximado (normal para N grande). Sin scipy."""
    n = len(xs)
    if n < 10:
        return (float("nan"), float("nan"))

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return (float("nan"), float("nan"))
    rho = num / (dx * dy)
    # z de Fisher → p bilateral con la CDF normal via erf
    import math
    if abs(rho) >= 1.0:
        return (rho, 0.0)
    z = 0.5 * math.log((1 + rho) / (1 - rho)) * math.sqrt(n - 3)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return (rho, p)


def collect() -> list[dict]:
    an = sqlite3.connect(f"file:{ANALYSIS_DB}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{CALIB_DB}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    out: list[dict] = []
    for (st, day), settle in settles.items():
        if st not in STATION_TZ:      # KIAH y legacy fuera
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        noon = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=12)
        lo = (noon - timedelta(minutes=NOON_TOLERANCE_MIN)).astimezone(ZoneInfo("UTC"))
        hi = (noon + timedelta(minutes=NOON_TOLERANCE_MIN)).astimezone(ZoneInfo("UTC"))
        r = an.execute(
            """SELECT ts, ens_med, ens_p10, ens_p90, difficulty_score,
                      difficulty_reasons_json
               FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?))
               LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             noon.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is None:
            continue
        # Las componentes no se persisten; se recomputan desde lo que sí está.
        s_spread = D._spread_score(r["ens_p10"], r["ens_p90"])
        reasons = []
        try:
            reasons = json.loads(r["difficulty_reasons_json"] or "[]")
        except Exception:
            pass
        anom_txt = next((x for x in reasons if "anomal" in x), None)
        effn_txt = next((x for x in reasons if "reweight" in x), None)
        s_anom = s_effn = None
        if anom_txt:      # "anomalía climática (p96)"
            try:
                pct = float(anom_txt.split("(p")[1].rstrip(")"))
                s_anom = D._anomaly_score(pct)
            except Exception:
                pass
        if effn_txt:      # "reweight colapsado (eff_N=7.6/31)"
            try:
                effn = float(effn_txt.split("eff_N=")[1].split("/")[0])
                s_effn = D._effn_score(effn, TOTAL_MEMBERS)
            except Exception:
                pass
        out.append({
            "station": st, "date": day,
            "err_abs": abs(r["ens_med"] - settle),
            "difficulty": r["difficulty_score"],
            "s_spread": s_spread, "s_anom": s_anom, "s_effn": s_effn,
        })
    return out


def main() -> int:
    rows = collect()
    print(f"station-days con settle y snapshot a mediodía local: {len(rows)}")
    if not rows:
        return 1
    print(f"rango: {min(r['date'] for r in rows)} .. {max(r['date'] for r in rows)}\n")

    print("PRIMARIO — Spearman(componente, |error|)")
    print(f"  {'componente':14s} {'N':>5s} {'rho':>7s} {'p':>9s}   veredicto")
    verdicts = {}
    for key, label in (("difficulty", "difficulty"), ("s_spread", "spread"),
                       ("s_anom", "anomalía"), ("s_effn", "eff_n")):
        pairs = [(r[key], r["err_abs"]) for r in rows if r.get(key) is not None]
        if len(pairs) < 10:
            print(f"  {label:14s} {len(pairs):5d}       —         — (N insuficiente)")
            continue
        rho, p = spearman([a for a, _ in pairs], [b for _, b in pairs])
        if rho > 0.30 and len(pairs) >= 100 and p < 0.01:
            v = "READMITIR como gate"
        elif rho > 0.15:
            v = "zona gris — volver a medir"
        else:
            v = "descartar como gate"
        verdicts[label] = (rho, len(pairs), v)
        print(f"  {label:14s} {len(pairs):5d} {rho:+7.3f} {p:9.2e}   {v}")

    # La anomalía se recomputa sólo cuando apareció en `reasons` (>=50), así que
    # arriba está condicionada a valores altos. Se repite sobre TODOS los
    # station-days tratando "sin razón de anomalía" como score bajo, para no
    # medir únicamente la cola.
    print("\n  (control) anomalía imputando 0 donde no figuraba en reasons:")
    pairs = [(r["s_anom"] if r["s_anom"] is not None else 0.0, r["err_abs"])
             for r in rows]
    rho, p = spearman([a for a, _ in pairs], [b for _, b in pairs])
    print(f"  {'anomalía*':14s} {len(pairs):5d} {rho:+7.3f} {p:9.2e}")

    print("\n  |error| mediano por banda de difficulty:")
    for lo_b, hi_b in ((0, 30), (30, 55), (55, 75), (75, 101)):
        sub = sorted(r["err_abs"] for r in rows
                     if r["difficulty"] is not None and lo_b <= r["difficulty"] < hi_b)
        if not sub:
            continue
        print(f"    {lo_b:3d}-{hi_b:3d}  N={len(sub):5d}  "
              f"|err| mediano {sub[len(sub)//2]:.2f}°F")

    # ---- secundario: ROI por banda de anomalía (orientativo) ----
    print("\nSECUNDARIO — ROI de bets post-fix por banda de anomalía (no decide)")
    cal = sqlite3.connect(f"file:{CALIB_DB}?mode=ro", uri=True)
    anom_by_day = {(r["station"], r["date"]): (r["s_anom"] or 0.0) for r in rows}
    bets = cal.execute(
        """SELECT station_id, date, pnl, stake, won FROM simulated_bets
           WHERE won IS NOT NULL AND blocked_by IS NULL AND date >= ?""",
        (LEDGER_FIX,)).fetchall()
    buckets: dict[str, list] = {"anom<70": [], "anom>=70": []}
    for st, day, pnl, stake, won in bets:
        a = anom_by_day.get((st, day))
        if a is None:
            continue
        buckets["anom>=70" if a >= 70 else "anom<70"].append((pnl or 0, stake or 0, won))
    for k, v in buckets.items():
        if not v:
            print(f"  {k:10s} N=  0")
            continue
        pnl = sum(x[0] for x in v)
        stake = sum(x[1] for x in v)
        wins = sum(1 for x in v if x[2])
        roi = f"{100 * pnl / stake:+.1f}%" if stake else "—"
        print(f"  {k:10s} N={len(v):3d}  ROI {roi:>8s}  wins {wins}/{len(v)}")
    print("\n(N chico esperado: sólo 18 bets ejecutadas en todo el roster post-fix)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
