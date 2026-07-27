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

--------------------- SEGUNDO PRE-REGISTRO (mismo día) ----------------------
Escrito tras ver que B (fallback agresivo) sale RECHAZADO: cierra el 94% del
gap pero se pasa del settle el 29% de los días. B usa el feed SIEMPRE, y lo que
se propuso operativamente era un fallback CONDICIONAL: usar el feed sólo cuando
el METAR horario falta. Es una hipótesis distinta y va con su propio criterio,
escrito antes de medirla.

  C = como A, pero permitiendo que `current_f` levante el máximo únicamente
      en los tramos donde `today_max_obs` lleva >= GAP_MIN minutos sin cambiar
      Y `current_f` supera a `today_max_obs` en más de 0.5°F.

      El proxy de "falta el METAR" es ese estancamiento: no persistimos la
      edad del último METAR, pero un máximo que no se mueve mientras el actual
      está por encima es exactamente la firma del hueco (KMIA hoy: max clavado
      en 82.9 desde las 12:53Z con el feed marcando 87.8).

  MISMO criterio de decisión que para B, sin relajarlo:
    ADOPTAR  si cierra >= 50% del gap Y P(C>settle) <= 5% Y exceso <= 0.5°F
    RECHAZAR si P(C>settle) > 15% O exceso mediano > 1.0°F
    ZONA GRIS en otro caso.

  GAP_MIN = 90 min, fijado de antemano por ser el valor que se propuso
  operativamente. Se reporta 60 y 120 como sensibilidad, SIN poder de decisión:
  si el veredicto cambia entre ellos, la conclusión es que el resultado no es
  robusto y se queda en zona gris.
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
        row = {"station": st, "date": day, "settle": settle,
               "A": a, "B": b, "n": n}
        # Estimador C: sólo levanta el máximo en los tramos donde today_max_obs
        # lleva >= gap_min sin moverse mientras current_f está por encima.
        serie = an.execute(
            """SELECT ts, today_max_obs, current_f FROM station_snapshots
               WHERE station=? AND ts>=? AND ts< ? ORDER BY ts""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"),
             hi.strftime("%Y-%m-%dT%H:%M:%S"))).fetchall()
        for gap_min in (60, 90, 120):
            row[f"C{gap_min}"] = _conditional_max(serie, gap_min)
        out.append(row)
    return out


def _conditional_max(serie, gap_min: int) -> float | None:
    """Max del día permitiendo el feed de 5 min sólo durante huecos de METAR."""
    best = None
    stale_since = None      # ts desde el que today_max_obs no se mueve
    last_max = None
    for ts_s, mx, cur in serie:
        if mx is None or mx <= -900:
            continue
        try:
            ts = datetime.fromisoformat(ts_s)
        except ValueError:
            continue
        if last_max is None or mx != last_max:
            last_max = mx
            stale_since = ts
        best = mx if best is None else max(best, mx)
        if cur is None or stale_since is None:
            continue
        stalled_min = (ts - stale_since).total_seconds() / 60
        if stalled_min >= gap_min and cur > mx + 0.5:
            best = max(best, cur)
    return best


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

    # ---- estimador C: fallback condicional ----
    print("\n" + "=" * 74)
    print("C — FALLBACK CONDICIONAL (feed sólo durante huecos de METAR)")
    print(f"  {'variante':12s} {'se pasa':>16s} {'exceso':>8s} {'gap':>8s} "
          f"{'cerrado':>8s}  veredicto")
    for gap_min in (60, 90, 120):
        key = f"C{gap_min}"
        sub = [r for r in rows if r.get(key) is not None]
        if not sub:
            continue
        over = [r for r in sub if r[key] > r["settle"] + 0.05]
        p = 100 * len(over) / len(sub)
        exc = statistics.median([r[key] - r["settle"] for r in over]) if over else 0.0
        g = statistics.median([r["settle"] - r[key] for r in sub])
        ga_sub = statistics.median([r["settle"] - r["A"] for r in sub])
        cl = 100 * (ga_sub - g) / ga_sub if ga_sub else 0.0
        if cl >= 50 and p <= 5 and exc <= 0.5:
            v = "ADOPTAR"
        elif p > 15 or exc > 1.0:
            v = "RECHAZAR"
        else:
            v = "ZONA GRIS"
        tag = "  <- pre-registrada" if gap_min == 90 else "  (sensibilidad)"
        print(f"  {key:12s} {len(over):5d}/{len(sub):<5d} {p:4.1f}% "
              f"{exc:+8.2f} {g:+8.2f} {cl:7.0f}%  {v}{tag}")

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
