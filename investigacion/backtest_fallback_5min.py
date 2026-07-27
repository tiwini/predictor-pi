#!/usr/bin/env python3
"""¿Debe `fetch_today_obs` caer al feed de 5 min cuando falta el METAR horario?

CONTEXTO
--------
`is_metar_slot_minute` descarta las observaciones con `minute % 5 == 0` porque
son del feed de 5 minutos, de menor fidelidad (vienen en °C enteros: 28°C =
82.4°F, 31°C = 87.8°F, o sea escalones de 1.8°F) y sin `rawMessage`. El filtro
existe por razones medidas (Fable 2026-07-10 tras el incidente KIAH; ce35c94).

El 2026-07-27 api.weather.gov cortó los METAR horarios hacia las 12:53Z y 14 de
20 estaciones quedaron con `today_max_obs` congelado hasta 12.6°F por debajo de
lo que el feed de 5 min ya había visto (KOKC). Ver
investigacion/obs_staleness.py.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

PREGUNTA
  ¿Incorporar el feed de 5 min al max del día mejora la estimación sin romper
  el invariante de piso?

ASIMETRÍA QUE GOBIERNA LA DECISIÓN
  `today_max_obs` se usa como PISO (apply_obs_floor). Quedarse corto cuesta
  precisión; pasarse cuesta un error GARANTIZADO, porque fuerza la distribución
  por encima de un settle que ya no puede subir. Medido 2026-07-26: de 176
  celdas con la predicción bajo el max observado, el settle terminó en o sobre
  ese max en 174. Por eso el riesgo pesa más que el beneficio.

ESTIMADORES (por station-day, sobre la serie de snapshots del día local)
  A = max(today_max_obs)                  <- lo actual
  B = max(A, max(current_f))              <- fallback agresivo (siempre el feed)
  settle = day_outcomes.max_obs_f (NWS CLI)

MÉTRICAS
  Riesgo   : P(B > settle + 0.05) y mediana del exceso cuando ocurre.
             Control: P(A > settle) — el estimador actual tampoco es perfecto.
  Beneficio: mediana de (settle - A) frente a (settle - B); cuánto gap cierra.

CRITERIO DE DECISIÓN
  ADOPTAR   si cierra >= 50% del gap mediano  Y  P(B > settle) <= 5%
            Y  exceso mediano <= 0.5°F
  RECHAZAR  si P(B > settle) > 15%  O  exceso mediano > 1.0°F
  ZONA GRIS en cualquier otro caso: no actuar, volver a medir con más N.

  Si el estimador actual A ya se pasa con frecuencia parecida, el riesgo del
  fallback debe juzgarse contra ESA base, no contra cero: se reporta el delta.

EXCLUSIONES
  - station-days sin settle NWS.
  - station-days con menos de 10 snapshots (día incompleto de datos).
  - KIAH y cualquier id fuera del roster actual.
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
from stations import STATION_TZ   # noqa: E402

ANALYSIS_DB = BASE / "analysis.db"
CALIB_DB = BASE / "calibration.db"
MIN_SNAPSHOTS = 10
UTC = ZoneInfo("UTC")


def collect() -> list[dict]:
    an = sqlite3.connect(f"file:{ANALYSIS_DB}?mode=ro", uri=True)
    cal = sqlite3.connect(f"file:{CALIB_DB}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    out = []
    for (st, day), settle in settles.items():
        if st not in STATION_TZ or settle is None:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        lo = datetime.combine(d, datetime.min.time(), tz).astimezone(UTC)
        hi = (datetime.combine(d, datetime.min.time(), tz)
              + timedelta(days=1)).astimezone(UTC)
        r = an.execute(
            """SELECT COUNT(*), MAX(today_max_obs), MAX(current_f)
               FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<?""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"),
             hi.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        n, a, c = r
        if n < MIN_SNAPSHOTS or a is None or a <= -900:
            continue
        b = a if c is None else max(a, c)
        out.append({"station": st, "date": day, "settle": settle,
                    "A": a, "B": b, "n": n})
    return out


def main() -> int:
    rows = collect()
    print(f"station-days con settle y >= {MIN_SNAPSHOTS} snapshots: {len(rows)}")
    if not rows:
        return 1
    print(f"rango: {min(r['date'] for r in rows)} .. {max(r['date'] for r in rows)}\n")

    over_b = [r for r in rows if r["B"] > r["settle"] + 0.05]
    over_a = [r for r in rows if r["A"] > r["settle"] + 0.05]
    pb = 100 * len(over_b) / len(rows)
    pa = 100 * len(over_a) / len(rows)
    exc_b = statistics.median([r["B"] - r["settle"] for r in over_b]) if over_b else 0.0
    exc_a = statistics.median([r["A"] - r["settle"] for r in over_a]) if over_a else 0.0

    print("RIESGO — ¿el estimador se pasa del settle? (rompe el invariante de piso)")
    print(f"  A (actual)    se pasa en {len(over_a):4d}/{len(rows)} = {pa:5.1f}%"
          f"   exceso mediano {exc_a:+.2f}°F")
    print(f"  B (fallback)  se pasa en {len(over_b):4d}/{len(rows)} = {pb:5.1f}%"
          f"   exceso mediano {exc_b:+.2f}°F")
    print(f"  delta introducido por el fallback: {pb - pa:+.1f} puntos")

    gap_a = [r["settle"] - r["A"] for r in rows]
    gap_b = [r["settle"] - r["B"] for r in rows]
    ma, mb = statistics.median(gap_a), statistics.median(gap_b)
    closed = 100 * (ma - mb) / ma if ma else 0.0
    print("\nBENEFICIO — ¿cuánto se acerca al settle?")
    print(f"  gap (settle - A) mediano  {ma:+.2f}°F")
    print(f"  gap (settle - B) mediano  {mb:+.2f}°F")
    print(f"  gap cerrado: {closed:.0f}%")
    print(f"  |error| mediano   A {statistics.median([abs(g) for g in gap_a]):.2f}°F"
          f"   B {statistics.median([abs(g) for g in gap_b]):.2f}°F")

    print("\nVEREDICTO segun el criterio pre-registrado")
    if closed >= 50 and pb <= 5 and exc_b <= 0.5:
        v = "ADOPTAR el fallback"
    elif pb > 15 or exc_b > 1.0:
        v = "RECHAZAR el fallback"
    else:
        v = "ZONA GRIS — no actuar, volver a medir"
    print(f"  gap cerrado {closed:.0f}%  ·  P(B>settle) {pb:.1f}%  "
          f"·  exceso mediano {exc_b:+.2f}°F")
    print(f"  -> {v}")

    print("\nPor estación (gap cerrado / veces que B se pasa):")
    per: dict[str, list] = {}
    for r in rows:
        per.setdefault(r["station"], []).append(r)
    print(f"  {'st':6s} {'N':>4s} {'gapA':>7s} {'gapB':>7s} {'cerr':>6s} "
          f"{'B>settle':>9s}")
    for st, sub in sorted(per.items()):
        ga = statistics.median([x["settle"] - x["A"] for x in sub])
        gb = statistics.median([x["settle"] - x["B"] for x in sub])
        ov = sum(1 for x in sub if x["B"] > x["settle"] + 0.05)
        cl = 100 * (ga - gb) / ga if ga else 0.0
        print(f"  {st:6s} {len(sub):4d} {ga:+7.2f} {gb:+7.2f} {cl:5.0f}% "
              f"{ov:4d}/{len(sub):<4d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
