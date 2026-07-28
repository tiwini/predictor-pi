#!/usr/bin/env python3
"""¿El bias tracker mejora o empeora el error de la predicción?

CONTEXTO
--------
KPHX el 2026-07-27 cerró en 113.0. El ensemble post-reweight decía 113.35
(error +0.35°F) y el bias lo bajó a 110.06 (error −2.94). La winsorización al
p95 aplicada esa tarde redujo el daño a −1.83, pero seguía llevando la
predicción al bin equivocado. El bias óptimo del día era ~+0.35, o sea ninguno.

Un caso no decide. Esto lo mide sobre todo el histórico.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: aplicar el bias no cambia el |error| de la predicción.

RECONSTRUCCIÓN Y SU LIMITACIÓN — leer antes de interpretar
  El pipeline es:  final = max(P − b, F)
    P = predicción post-reweight (pre-bias),  b = bias,  F = piso de observación
  Se reconstruye  pred_sin_bias = ens_med + bias_f, que es EXACTO sólo cuando
  el clamp no fue vinculante (P − b >= F). Cuando sí lo fue, el clamp ya rescató
  parte del daño del bias, así que la reconstrucción **subestima** cuánto
  perjudica el bias. El sesgo va CONTRA encontrar daño: si aun así sale
  dañino, el resultado es conservador.

  Por eso se reporta por separado el subconjunto con `obs_floor_n = 0`, donde la
  reconstrucción es exacta. Ese subconjunto es pequeño (la instrumentación del
  clamp es del 2026-07-26) y va como CONTROL, no como primario.

UNIDAD Y VENTANA
  Un station-day = el snapshot más cercano a las 12:00 local (±90 min), igual
  que el resto de backtests. Sólo días con `bias_applied = 1` — en los demás no
  hay nada que comparar.

MÉTRICAS
  |error| mediano de pred_con_bias y de pred_sin_bias frente al settle NWS.
  % de días en que el bias ACERCA la predicción al settle.
  Desglose por `bias_path` (ewma / regime / nudge) y por estación.

CRITERIO DE DECISIÓN
  El bias AYUDA        si baja el |error| mediano >= 0.20°F  Y  acerca en >= 55%
  El bias PERJUDICA    si sube el |error| mediano >= 0.20°F  O  acerca en <= 45%
  NEUTRO               en cualquier otro caso

  Si sale PERJUDICA, la acción propuesta no es "ajustar el cap" sino desactivar
  el bias (APPLY_THRESHOLD alto) hasta tener N por estación que lo justifique.
  Si sale NEUTRO, también conviene desactivarlo: un mecanismo que no mejora nada
  pero añade una vía de fallo (el caso KPHX) no se sostiene por sí solo.
  Si sale AYUDA, se queda y el problema de KPHX se trata como caso de outlier.

NOTA SOBRE LOS DATOS
  El histórico refleja el bias SIN winsorizar: el cap se aplicó el 2026-07-27
  por la tarde. Eso es lo correcto para juzgar el mecanismo tal como ha operado.

Octavo backtest sobre esta base. Positivo o negativo, no se toca nada sin
decirlo antes.
=============================================================================
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ    # noqa: E402

UTC = ZoneInfo("UTC")
TOL_MIN = 90


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    rows = []
    for (st, day), settle in settles.items():
        if st not in STATION_TZ or settle is None:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        noon = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=12)
        lo = (noon - timedelta(minutes=TOL_MIN)).astimezone(UTC)
        hi = (noon + timedelta(minutes=TOL_MIN)).astimezone(UTC)
        r = an.execute(
            """SELECT ens_med, bias_f, bias_path, obs_floor_n
               FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND bias_applied=1
                 AND ens_med IS NOT NULL AND bias_f IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             noon.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is None or abs(r["bias_f"]) < 1e-9:
            continue
        con = r["ens_med"]
        sin = r["ens_med"] + r["bias_f"]
        rows.append({
            "st": st, "day": day, "settle": settle,
            "e_con": abs(con - settle), "e_sin": abs(sin - settle),
            "path": r["bias_path"], "floor_n": r["obs_floor_n"],
            "bias": r["bias_f"],
        })

    if not rows:
        print("sin datos")
        return 1

    def report(sub, label):
        if len(sub) < 5:
            print(f"{label}: N={len(sub)} (insuficiente)")
            return None
        mc = statistics.median(x["e_con"] for x in sub)
        ms = statistics.median(x["e_sin"] for x in sub)
        mejora = sum(1 for x in sub if x["e_con"] < x["e_sin"] - 1e-9)
        empata = sum(1 for x in sub if abs(x["e_con"] - x["e_sin"]) <= 1e-9)
        pct = 100 * mejora / len(sub)
        print(f"{label:28s} N={len(sub):4d}  |err| con {mc:5.2f}  sin {ms:5.2f}  "
              f"delta {mc - ms:+5.2f}  acerca {pct:4.1f}%  (empates {empata})")
        return mc - ms, pct, len(sub)

    print(f"station-days con bias aplicado: {len(rows)}\n")
    print("PRIMARIO")
    res = report(rows, "todos")

    print("\npor bias_path")
    for p in sorted({x["path"] for x in rows if x["path"]}):
        report([x for x in rows if x["path"] == p], f"  {p}")

    print("\nCONTROL — reconstrucción exacta (clamp no vinculante)")
    report([x for x in rows if x["floor_n"] == 0], "  obs_floor_n = 0")

    print("\npor estación")
    for st in sorted({x["st"] for x in rows}):
        report([x for x in rows if x["st"] == st], f"  {st}")

    if res:
        delta, pct, n = res
        if delta <= -0.20 and pct >= 55:
            v = "el bias AYUDA — se queda"
        elif delta >= 0.20 or pct <= 45:
            v = "el bias PERJUDICA — proponer desactivarlo"
        else:
            v = "NEUTRO — no mejora nada y añade una vía de fallo"
        print(f"\nVEREDICTO: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
