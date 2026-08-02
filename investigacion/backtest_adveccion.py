#!/usr/bin/env python3
"""¿La advección de temperatura explica el error del modelo?

CONTEXTO
--------
Descartadas nubosidad y radiación (backtest_sky.py, N=584): el GFS ya las
incorpora. Queda el siguiente candidato físico para la subida de la tarde.

Razón para esperar algo DISTINTO que con las nubes: el GFS resuelve la
advección sinóptica, pero a ~25 km de resolución la de mesoescala —brisa
marina, downslope— se le escapa. Y las dos estaciones donde más nos pasamos son
justo de brisa marina (KSFO +3.72°F, KLAX +2.70°F).

El probe (probe_adveccion.py, KSFO 2026-07-20) confirmó que el cálculo da el
signo físicamente correcto: advección fría creciente toda la tarde según el
viento gira al oeste, con gradiente costa-interior de +14°C sobre 110 km.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: la advección no explica el error del modelo.

CÁLCULO
  A = -(u·∂T/∂x + v·∂T/∂y)   en °C/h, por hora, de ERA5 vía Open-Meteo archive
  Gradiente de 4 puntos a ±0.5° (~55 km) alrededor de la estación.
  A > 0 entra aire cálido; A < 0 entra aire frío.

VARIABLES (las tres, decididas de antemano)
  adv_manana    media 06:00-12:00 local  -> lo que ya pasó al predecir
  adv_pico      media en la ventana de pico -> lo que pasará después
  adv_pico_min  mínimo horario en la ventana -> el episodio frío más fuerte,
                para capturar brisa que entra de golpe (la media lo diluiría)

OBJETIVO
  error = ens_med(mediodía local) - settle   [firmado: importa la dirección]

  Hipótesis direccional: advección fría no contada => sobre-predecimos
  => rho POSITIVO (más advección fría = más negativa = error más negativo...
  no: si A es muy negativa y sobre-predecimos, error>0 con A<0 => rho NEGATIVO).
  Se espera rho NEGATIVO. Se reporta el signo observado sin reinterpretarlo.

CRITERIO DE DECISIÓN — primario DENTRO de estación
  Lección de backtest_sky (2026-08-01): el rho del pool cruzado de 20
  estaciones midió diferencias ENTRE estaciones y murió al controlarlo. Aquí el
  criterio primario es dentro de estación DESDE EL DISEÑO; el pool se reporta
  sólo como referencia y NO decide.

  INSTRUMENTAR  |rho mediano dentro de estación| > 0.20  Y  signo consistente
                en >= 15 de 19 estaciones  (binomial two-sided p ~ 0.019)
  ZONA GRIS     uno de los dos: |rho mediano| > 0.15  o  signos >= 14/19
  DESCARTAR     ninguno de los dos

SUBGRUPO COSTERO — declarado ANTES de mirar, no elegido después
  La hipótesis física es específica de brisa marina, así que se mira aparte el
  subgrupo definido por geografía: KSFO, KLAX, KSEA, KBOS, KMIA, KNYC.
  Criterio propio: |rho mediano| > 0.25 y signo consistente en >= 5 de 6.
  Con 6 estaciones esto es sugerente, NUNCA concluyente: si sólo pasa el
  subgrupo, el resultado es "volver con más N", no "instrumentar".

COSTE
  20 llamadas de archive (multi-punto, 5 puntos cada una).
=============================================================================

RESULTADO (2026-08-01, N=540 días-estación, 19 estaciones)

  TODAS — las tres descartadas, dentro de estación no queda nada:
    adv_manana    rho mediano -0.008   signos 10/19  p=1.000   [pool -0.227]
    adv_pico      rho mediano -0.069   signos 11/19  p=0.648   [pool -0.144]
    adv_pico_min  rho mediano -0.054   signos 10/19  p=1.000   [pool -0.111]

  COSTERO — adv_pico y adv_pico_min quedan en zona gris (5/6 con signo
  negativo, pero p=0.219 con 6 estaciones: no es evidencia).

  LO INTERESANTE ESTÁ EN LA TABLA POR ESTACIÓN, no en los tests:
    KLAX  adv_pico -1.878  error mediano +3.00
    KSFO  adv_pico -1.744  error mediano +3.80
    el resto entre -0.32 y +0.23 de advección.

  Las dos estaciones con advección fría fuerte —brisa marina, confirmada
  físicamente por el probe— son las dos con mayor sobre-predicción. Pero el
  pool costero (-0.397) está conducido ENTERAMENTE por esos dos puntos: quitando
  KSFO y KLAX no queda patrón (KOKC tiene +3.49 de error con advección nula,
  KMIA -2.14 también con advección nula). N=2 estaciones NO prueba nada.

  CONCLUSIÓN: la advección no explica QUÉ DÍA fallamos — que es lo que se
  preguntaba. A lo sumo sugiere por qué fallamos MÁS EN CIERTAS ESTACIONES, y
  eso es un offset constante que un corrector de nivel por estación ya absorbe
  sin necesidad de instrumentar nada. Encaja con backtest_subida_restante
  (2026-07-28): el sesgo es POR estación (±4°F) y se cancela en la mediana.

  DECISIÓN: no instrumentar. H0 no se rechaza.
=============================================================================
"""
from __future__ import annotations

import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).parent))
from predictor import fetch_station               # noqa: E402
from stations import STATION_TZ, PEAK_HOURS       # noqa: E402
from backtest_sky import spearman                 # noqa: E402
from probe_adveccion import advection_c_per_h, DELTA_DEG   # noqa: E402

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
UA = "weather-predictor/0.1 (educational)"
UTC = ZoneInfo("UTC")
VARS = "temperature_2m,wind_speed_10m,wind_direction_10m"
COSTERAS = {"KSFO", "KLAX", "KSEA", "KBOS", "KMIA", "KNYC"}


def recoger() -> list[dict]:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles: dict[str, dict[str, float]] = {}
    for st, d, m in cal.execute(
            "SELECT station_id, date, max_obs_f FROM day_outcomes"):
        if m is not None:
            settles.setdefault(st, {})[d] = m

    filas = []
    for st in sorted(settles):
        if st not in STATION_TZ:
            continue
        days = sorted(settles[st])
        if len(days) < 5:
            continue
        s = fetch_station(st)
        d = DELTA_DEG
        lats = [s.lat, s.lat + d, s.lat - d, s.lat, s.lat]
        lons = [s.lon, s.lon, s.lon, s.lon + d, s.lon - d]
        try:
            r = requests.get(ARCHIVE, params={
                "latitude": ",".join(f"{x:.4f}" for x in lats),
                "longitude": ",".join(f"{x:.4f}" for x in lons),
                "start_date": days[0], "end_date": days[-1],
                "hourly": VARS, "timezone": STATION_TZ[st],
            }, headers={"User-Agent": UA}, timeout=60)
            js = r.json()
        except Exception as e:
            print(f"{st}: archive falló ({e})")
            continue
        if not isinstance(js, list) or len(js) != 5:
            print(f"{st}: respuesta inesperada")
            continue
        h = [p["hourly"] for p in js]
        times = h[0]["time"]

        # advección por hora, indexada por (día, hora local)
        adv: dict[str, dict[int, float]] = {}
        for i, t in enumerate(times):
            dt = datetime.fromisoformat(t)
            try:
                a = advection_c_per_h(
                    h[0]["temperature_2m"][i], h[1]["temperature_2m"][i],
                    h[2]["temperature_2m"][i], h[3]["temperature_2m"][i],
                    h[4]["temperature_2m"][i], h[0]["wind_speed_10m"][i],
                    h[0]["wind_direction_10m"][i], s.lat)
            except (IndexError, TypeError):
                a = None
            if a is not None:
                adv.setdefault(dt.date().isoformat(), {})[dt.hour] = a

        tz = ZoneInfo(STATION_TZ[st])
        lo_p, hi_p = PEAK_HOURS[st]
        for day in days:
            por_hora = adv.get(day)
            if not por_hora:
                continue
            noon = datetime.combine(
                datetime.strptime(day, "%Y-%m-%d").date(),
                datetime.min.time(), tz) + timedelta(hours=12)
            snap = an.execute(
                """SELECT ens_med FROM station_snapshots
                   WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
                   ORDER BY ts LIMIT 1""",
                (st,
                 (noon - timedelta(minutes=90)).astimezone(UTC)
                 .strftime("%Y-%m-%dT%H:%M:%S"),
                 (noon + timedelta(minutes=90)).astimezone(UTC)
                 .strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
            if snap is None:
                continue
            man = [v for hh, v in por_hora.items() if 6 <= hh < 12]
            pico = [v for hh, v in por_hora.items() if lo_p <= hh < hi_p]
            filas.append({
                "st": st, "day": day,
                "err": snap["ens_med"] - settles[st][day],
                "adv_manana": statistics.mean(man) if man else None,
                "adv_pico": statistics.mean(pico) if pico else None,
                "adv_pico_min": min(pico) if pico else None,
            })
    return filas


def binom_ge(k: int, n: int) -> float:
    """P(X >= k) con p=0.5, two-sided."""
    tot = sum(math.comb(n, j) for j in range(k, n + 1))
    return min(1.0, 2 * tot / (2 ** n))


def evaluar(filas, vars_, etiqueta, min_st, umbral_rho, umbral_signos):
    por_st: dict[str, list] = {}
    for f in filas:
        por_st.setdefault(f["st"], []).append(f)
    usables = {s: v for s, v in por_st.items() if len(v) >= 15}
    print(f"\n{'=' * 74}\n{etiqueta}   estaciones={len(usables)}  "
          f"días-estación={sum(len(v) for v in usables.values())}\n{'=' * 74}")
    if len(usables) < min_st:
        print("  N insuficiente")
        return
    for var in vars_:
        rhos = []
        for st, sub in usables.items():
            par = [(f[var], f["err"]) for f in sub if f[var] is not None]
            if len(par) < 15:
                continue
            rho, _ = spearman([a for a, _ in par], [b for _, b in par])
            if rho == rho:
                rhos.append((st, rho))
        if not rhos:
            continue
        vals = [r for _, r in rhos]
        med = statistics.median(vals)
        neg = sum(1 for r in vals if r < 0)
        mayoria = max(neg, len(vals) - neg)
        p = binom_ge(mayoria, len(vals))
        ok_r = abs(med) > umbral_rho
        ok_s = mayoria >= umbral_signos
        if ok_r and ok_s:
            v = "INSTRUMENTAR" if etiqueta.startswith("TODAS") else "sugerente"
        elif ok_r or ok_s:
            v = "zona gris"
        else:
            v = "descartar"
        pool = [(f[var], f["err"]) for f in filas if f[var] is not None]
        rp, _ = spearman([a for a, _ in pool], [b for _, b in pool])
        print(f"\n  {var}")
        print(f"    rho mediano dentro de estación : {med:+.3f}   "
              f"(umbral {umbral_rho})")
        print(f"    signo mayoritario              : {mayoria}/{len(vals)} "
              f"{'negativo' if neg > len(vals) / 2 else 'positivo'}  "
              f"binomial p={p:.3f}   (umbral {umbral_signos})")
        print(f"    rango de rho por estación      : "
              f"{min(vals):+.3f} .. {max(vals):+.3f}")
        print(f"    [referencia, NO decide] pool   : {rp:+.3f}")
        print(f"    -> {v}")


def main() -> int:
    filas = recoger()
    if not filas:
        print("sin datos")
        return 1
    vars_ = ("adv_manana", "adv_pico", "adv_pico_min")
    evaluar(filas, vars_, "TODAS las estaciones", 15, 0.20, 15)
    evaluar([f for f in filas if f["st"] in COSTERAS], vars_,
            "SUBGRUPO COSTERO (pre-declarado; sugerente, nunca concluyente)",
            5, 0.25, 5)

    print(f"\n{'=' * 74}\nadv_pico por estación (contexto físico, no un test)")
    por_st: dict[str, list] = {}
    for f in filas:
        por_st.setdefault(f["st"], []).append(f)
    print(f"  {'st':7s} {'N':>4s} {'adv_pico med':>13s} {'err mediano':>12s}")
    for st in sorted(por_st, key=lambda s: statistics.median(
            [f["adv_pico"] for f in por_st[s] if f["adv_pico"] is not None]
            or [0])):
        sub = [f for f in por_st[st] if f["adv_pico"] is not None]
        if len(sub) < 15:
            continue
        mark = " ← costera" if st in COSTERAS else ""
        print(f"  {st:7s} {len(sub):4d} "
              f"{statistics.median(f['adv_pico'] for f in sub):+13.3f} "
              f"{statistics.median(f['err'] for f in sub):+12.2f}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
