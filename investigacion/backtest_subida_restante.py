#!/usr/bin/env python3
"""¿El ensemble subestima la subida restante antes de que abra la ventana de pico?

CONTEXTO
--------
KMIA el 2026-07-28 a las 11:57 local (ventana 14-17h, sin abrir): `ens_med`
90.8 con `ens_p50 +1.2 sobre current`, mientras el histórico de la estación a
esa hora sube una mediana de **+3.0°F** y los 8 días previos settlearon todos
entre 92 y 94, ninguno en 90-91. El mercado pagaba 0.64 por `92-93` y nuestro
modelo le daba 0.249.

No había culpable de los habituales: bias 0.00, `ext_diff` +0.7 (alineados con
los externos), feed sano, distribución no especialmente difusa. El modelo
simplemente predecía poca subida.

TENSIÓN CON UN HALLAZGO PREVIO — hay que decirla
  `ext_diff_matinal_predice_error` (N=483) mostró que cuando divergimos de los
  externos por la mañana **sobre**-predecimos. Esto pregunta lo contrario: si
  **sub**-predecimos antes de la ventana. No son incompatibles: aquel mide
  divergencia contra los externos, éste mide el error absoluto contra el settle,
  y en KMIA hoy nosotros Y los externos estábamos ambos por debajo del
  empírico. Si este backtest sale positivo, las dos cosas conviven; si sale al
  revés, refuerza el anterior.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: antes de que abra la ventana de pico, el ensemble no es peor que un
baseline trivial de persistencia + subida climatológica.

BASELINE (el rival, deliberadamente tonto)
  pred_baseline = current_f + mediana_LOO(subida restante de esa estación a esa
                              hora local)
  `mediana_LOO` se calcula **excluyendo el día que se está prediciendo**, para
  no usar información del futuro. Un baseline que usara todos los días,
  incluido el propio, ganaría por construcción.

UNIDAD Y VENTANA
  Un station-day = el snapshot más cercano a **2 horas antes** de que abra
  PEAK_HOURS[estación] (±30 min). Es el momento en que la pregunta importa: la
  ventana no ha abierto y hay que decidir con la subida por delante.
  Se exige `current_f` y `ens_med` no nulos y settle disponible.

MÉTRICA PRIMARIA
  |error| mediano del ensemble frente al del baseline.
  Secundaria: error FIRMADO mediano del ensemble (si es negativo, subestima) y
  % de días en que el ensemble se queda corto.

CRITERIO DE DECISIÓN
  El ensemble SUBESTIMA de forma explotable si, con N >= 100 station-days:
      |error| del baseline es >= 0.30°F MENOR que el del ensemble
      Y el error firmado del ensemble es <= -0.30°F
      Y se queda corto en >= 60% de los días
  NEUTRO           si las diferencias son < 0.15°F
  El ensemble GANA si su |error| es >= 0.30°F menor que el del baseline

  Si el ensemble pierde contra un baseline tan simple, la acción propuesta NO es
  sustituirlo, sino instrumentar la subida climatológica como señal aparte y
  medirla en vivo antes de tocar nada del pipeline.

EXCLUSIONES
  - station-days sin settle NWS o sin snapshot en la ventana.
  - estaciones con menos de 10 días (la mediana LOO no significaría nada).
  - KIAH y cualquier id fuera del roster.

Noveno backtest sobre esta base. Positivo = candidato, no cambio de pipeline.
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
from stations import STATION_TZ, PEAK_HOURS   # noqa: E402

UTC = ZoneInfo("UTC")
TOL_MIN = 30
HOURS_BEFORE_PEAK = 2
MIN_DAYS_PER_STATION = 10


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    per_station: dict[str, list] = {}
    for (st, day), settle in settles.items():
        if st not in STATION_TZ or settle is None:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        peak_lo = PEAK_HOURS[st][0]
        ref = (datetime.combine(d, datetime.min.time(), tz)
               + timedelta(hours=peak_lo - HOURS_BEFORE_PEAK))
        lo = (ref - timedelta(minutes=TOL_MIN)).astimezone(UTC)
        hi = (ref + timedelta(minutes=TOL_MIN)).astimezone(UTC)
        r = an.execute(
            """SELECT current_f, ens_med FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=?
                 AND current_f IS NOT NULL AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             ref.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is None:
            continue
        per_station.setdefault(st, []).append(
            {"day": day, "settle": settle, "cur": r["current_f"],
             "ens": r["ens_med"], "delta": settle - r["current_f"]})

    rows = []
    for st, days in per_station.items():
        if len(days) < MIN_DAYS_PER_STATION:
            continue
        for i, x in enumerate(days):
            otros = [y["delta"] for j, y in enumerate(days) if j != i]
            if not otros:
                continue
            base_pred = x["cur"] + statistics.median(otros)
            rows.append({
                "st": st, "day": x["day"], "settle": x["settle"],
                "e_ens": x["ens"] - x["settle"],
                "e_base": base_pred - x["settle"],
            })

    if not rows:
        print("sin datos")
        return 1

    ae_ens = statistics.median(abs(x["e_ens"]) for x in rows)
    ae_base = statistics.median(abs(x["e_base"]) for x in rows)
    se_ens = statistics.median(x["e_ens"] for x in rows)
    corto = 100 * sum(1 for x in rows if x["e_ens"] < 0) / len(rows)

    print(f"station-days: {len(rows)}  ({len(per_station)} estaciones, "
          f"snapshot {HOURS_BEFORE_PEAK}h antes de la ventana de pico)\n")
    print("PRIMARIO")
    print(f"  |error| mediano ensemble   {ae_ens:5.2f}°F")
    print(f"  |error| mediano baseline   {ae_base:5.2f}°F   "
          f"(current + subida mediana LOO)")
    print(f"  ventaja del baseline       {ae_ens - ae_base:+5.2f}°F")
    print("\nSECUNDARIO")
    print(f"  error FIRMADO del ensemble {se_ens:+5.2f}°F   "
          f"(negativo = se queda corto)")
    print(f"  se queda corto en          {corto:4.1f}% de los días")

    print("\npor estación (|err| ens vs baseline)")
    for st in sorted(per_station):
        sub = [x for x in rows if x["st"] == st]
        if not sub:
            continue
        a = statistics.median(abs(x["e_ens"]) for x in sub)
        b = statistics.median(abs(x["e_base"]) for x in sub)
        s = statistics.median(x["e_ens"] for x in sub)
        mark = "  <- baseline gana" if b < a - 0.3 else ""
        print(f"  {st:6s} N={len(sub):3d}  ens {a:5.2f}  base {b:5.2f}  "
              f"firmado {s:+5.2f}{mark}")

    if len(rows) < 100:
        v = f"N insuficiente ({len(rows)})"
    elif (ae_base <= ae_ens - 0.30 and se_ens <= -0.30 and corto >= 60):
        v = "el ensemble SUBESTIMA de forma explotable (candidato)"
    elif ae_ens <= ae_base - 0.30:
        v = "el ensemble GANA al baseline"
    elif abs(ae_ens - ae_base) < 0.15:
        v = "NEUTRO"
    else:
        v = "sin veredicto claro — no actuar"
    print(f"\nVEREDICTO: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
