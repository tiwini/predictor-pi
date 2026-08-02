#!/usr/bin/env python3
"""¿La humedad del suelo explica el error del modelo?

CONTEXTO
--------
Tercer candidato físico. Descartadas nubosidad/radiación (el GFS ya las
incorpora) y advección (no explica qué día, sólo sugiere offset por estación).

Este tiene MEJOR razón a priori que los dos anteriores: la física es el reparto
Bowen —suelo húmedo manda energía a evaporación en vez de calentar el aire— y
la inicialización de humedad del suelo es una debilidad conocida de los modelos
NWP. A diferencia de las nubes, aquí hay motivo para esperar que el GFS no lo
tenga bien resuelto.

El probe confirmó que la variable se mueve: KMSY IQR 0.134 m³/m³, KPHX de 0.010
a 0.194. Y que KSFO está clavada (IQR 0.012, 4.2 mm de lluvia en el mes).

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: la humedad del suelo no explica el error del modelo.

VARIABLES (las tres, decididas de antemano) — todas a las 06:00 local, antes
del calentamiento, así que son causales respecto a la predicción del mediodía
  sm_superficial  soil_moisture_0_to_7cm   (m³/m³)
  sm_profundo     soil_moisture_7_to_28cm  (m³/m³) — reserva que se agota lento
  lluvia_72h      precipitación acumulada en las 72 h previas (mm)

OBJETIVO
  error = ens_med(mediodía local) - settle   [firmado]

  Hipótesis direccional: suelo húmedo => más calor latente => sube menos => si
  el modelo no lo cuenta, sobre-predecimos => error positivo con humedad alta
  => se espera rho POSITIVO. Se reporta el signo observado sin reinterpretarlo.

FILTRO DE APLICABILIDAD — declarado antes, no sesga
  Sólo entran estaciones con IQR(sm_superficial) > 0.02 m³/m³. Donde la humedad
  no varía (KSFO: 0.012) no puede explicar nada y sólo aportaría ruido. El
  filtro mira SÓLO el predictor, nunca el error, así que no selecciona por
  resultado. Se reporta qué estaciones quedan fuera.

CRITERIO DE DECISIÓN — primario DENTRO de estación
  INSTRUMENTAR  |rho mediano dentro de estación| > 0.20  Y  test de signos
                binomial two-sided p < 0.05
  ZONA GRIS     uno de los dos
  DESCARTAR     ninguno
  El pool cruzado se reporta como referencia y NO decide (lección de
  backtest_sky y backtest_adveccion: allí el pool marcaba señal inexistente).

AVISO SOBRE AUTOCORRELACIÓN
  La humedad del suelo se seca despacio, así que días consecutivos NO son
  independientes y el p del Spearman dentro de estación sale optimista. Por eso
  el criterio primario incluye el test de signos ENTRE estaciones, donde cada
  estación es una unidad independiente y la autocorrelación intra-estación no
  contamina. Los p intra-estación no se usan para decidir.

COSTE
  20 llamadas de archive.
=============================================================================

RESULTADO (2026-08-01, N=428 días-estación, 15 estaciones tras el filtro)

  Excluidas por IQR <= 0.02: KLAX (0.0033), KSEA (0.0113), KSFO (0.0147),
  KDEN (0.0168).

    sm_superficial  rho mediano -0.003   8/15 neg  p=1.00   [pool +0.088]
    sm_profundo     rho mediano +0.042   9/15 pos  p=0.61   [pool +0.031]
    lluvia_72h      rho mediano -0.124  10/15 neg  p=0.30   [pool -0.197]

  Por terciles dentro de cada estación, húmedo-menos-seco mediano -0.33°F, con
  signo positivo en sólo 5/15. La hipótesis predecía POSITIVO (suelo húmedo =>
  sobre-predecir). Sale al revés y sin significancia: el GFS aparentemente sí
  incorpora la humedad del suelo.

  ANOTACIÓN POST-HOC — no pre-registrada, NO actuar sobre ella: en lluvia_72h
  las cuatro estaciones con |rho| mayor son del mismo cluster (KSAT -0.48,
  KDFW -0.46, KAUS -0.37, KMSY -0.30: Texas y Golfo) y todas del mismo signo,
  o sea que tras lluvia nos quedamos CORTOS. Patrón coherente pero elegido
  después de mirar y con 4 estaciones. Queda escrito como hipótesis para un
  pre-registro propio, nunca como hallazgo.

  DECISIÓN: no instrumentar. H0 no se rechaza.
=============================================================================
"""
from __future__ import annotations

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
from predictor import fetch_station                  # noqa: E402
from stations import STATION_TZ                      # noqa: E402
from backtest_sky import spearman                    # noqa: E402
from backtest_adveccion import binom_ge              # noqa: E402

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
UA = "weather-predictor/0.1 (educational)"
UTC = ZoneInfo("UTC")
VARS = "soil_moisture_0_to_7cm,soil_moisture_7_to_28cm,precipitation"
IQR_MIN = 0.02
VARIABLES = ("sm_superficial", "sm_profundo", "lluvia_72h")


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
        ini = (datetime.strptime(days[0], "%Y-%m-%d")
               - timedelta(days=4)).strftime("%Y-%m-%d")
        try:
            r = requests.get(ARCHIVE, params={
                "latitude": s.lat, "longitude": s.lon,
                "start_date": ini, "end_date": days[-1],
                "hourly": VARS, "timezone": STATION_TZ[st],
            }, headers={"User-Agent": UA}, timeout=60)
            h = r.json().get("hourly") or {}
        except Exception as e:
            print(f"{st}: archive falló ({e})")
            continue
        times = h.get("time") or []
        if not times:
            continue
        sm0, sm7 = {}, {}
        lluvia_h: list[tuple[str, int, float]] = []
        for i, t in enumerate(times):
            day, hh = t[:10], int(t[11:13])
            p = (h.get("precipitation") or [None] * len(times))[i]
            lluvia_h.append((day, hh, p if p is not None else 0.0))
            if hh == 6:
                a = (h.get("soil_moisture_0_to_7cm") or [None] * len(times))[i]
                b = (h.get("soil_moisture_7_to_28cm") or [None] * len(times))[i]
                if a is not None:
                    sm0[day] = a
                if b is not None:
                    sm7[day] = b

        # lluvia acumulada en las 72 h previas a las 06h local de cada día
        acc: dict[str, float] = {}
        for day in sm0:
            d0 = datetime.strptime(day, "%Y-%m-%d")
            lim_ini = d0 - timedelta(days=3)
            tot = 0.0
            for dd, hh, p in lluvia_h:
                ts = datetime.strptime(dd, "%Y-%m-%d") + timedelta(hours=hh)
                if lim_ini <= ts < d0 + timedelta(hours=6):
                    tot += p
            acc[day] = tot

        tz = ZoneInfo(STATION_TZ[st])
        for day in days:
            if day not in sm0:
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
            filas.append({
                "st": st, "day": day,
                "err": snap["ens_med"] - settles[st][day],
                "sm_superficial": sm0[day],
                "sm_profundo": sm7.get(day),
                "lluvia_72h": acc.get(day),
            })
    return filas


def main() -> int:
    filas = recoger()
    if not filas:
        print("sin datos")
        return 1

    por_st: dict[str, list] = {}
    for f in filas:
        por_st.setdefault(f["st"], []).append(f)

    # filtro de aplicabilidad, declarado antes: sólo mira el predictor
    dentro, fuera = {}, []
    for st, sub in por_st.items():
        v = [f["sm_superficial"] for f in sub if f["sm_superficial"] is not None]
        if len(sub) < 15 or len(v) < 4:
            continue
        q = statistics.quantiles(v, n=4)
        iqr = q[2] - q[0]
        (dentro.setdefault(st, sub) if iqr > IQR_MIN
         else fuera.append((st, iqr)))

    print(f"días-estación {len(filas)}   estaciones con N>=15: {len(por_st)}")
    if fuera:
        print(f"  excluidas por IQR <= {IQR_MIN} m³/m³ (la humedad no varía, "
              f"no puede explicar nada):")
        for st, iqr in sorted(fuera, key=lambda x: x[1]):
            print(f"    {st}  IQR {iqr:.4f}")
    print(f"  entran {len(dentro)} estaciones, "
          f"{sum(len(v) for v in dentro.values())} días-estación")

    for var in VARIABLES:
        rhos = []
        for st, sub in dentro.items():
            par = [(f[var], f["err"]) for f in sub if f[var] is not None]
            if len(par) < 15:
                continue
            rho, _ = spearman([a for a, _ in par], [b for _, b in par])
            if rho == rho:
                rhos.append((st, rho))
        if not rhos:
            print(f"\n  {var}: sin estaciones evaluables")
            continue
        vals = [r for _, r in rhos]
        med = statistics.median(vals)
        pos = sum(1 for r in vals if r > 0)
        mayoria = max(pos, len(vals) - pos)
        p = binom_ge(mayoria, len(vals))
        ok_r, ok_s = abs(med) > 0.20, p < 0.05
        v = ("INSTRUMENTAR" if ok_r and ok_s
             else "zona gris" if ok_r or ok_s else "descartar")
        pool = [(f[var], f["err"]) for f in filas if f[var] is not None]
        rp, _ = spearman([a for a, _ in pool], [b for _, b in pool])
        print(f"\n  {var}")
        print(f"    rho mediano dentro de estación : {med:+.3f}   (umbral 0.20)")
        print(f"    signo mayoritario              : {mayoria}/{len(vals)} "
              f"{'positivo' if pos > len(vals) / 2 else 'negativo'}  "
              f"binomial p={p:.4f}   (umbral 0.05)")
        print(f"    rango por estación             : "
              f"{min(vals):+.3f} .. {max(vals):+.3f}")
        print(f"    [referencia, NO decide] pool   : {rp:+.3f}")
        print(f"    -> {v}")
        peor = sorted(rhos, key=lambda x: -abs(x[1]))[:4]
        print("    estaciones con |rho| mayor: " +
              "  ".join(f"{s}{r:+.2f}" for s, r in peor))

    print(f"\n{'=' * 70}\nerror mediano por terciles de sm_superficial "
          f"DENTRO de cada estación\n{'=' * 70}")
    print(f"  {'st':7s} {'N':>4s} {'seco':>8s} {'medio':>8s} {'húmedo':>8s} "
          f"{'húm-seco':>9s}")
    difs = []
    for st in sorted(dentro):
        sub = sorted((f for f in dentro[st] if f["sm_superficial"] is not None),
                     key=lambda f: f["sm_superficial"])
        t = max(1, len(sub) // 3)
        g = [sub[:t], sub[t:2 * t], sub[2 * t:]]
        m = [statistics.median(f["err"] for f in x) if x else float("nan")
             for x in g]
        difs.append(m[2] - m[0])
        print(f"  {st:7s} {len(sub):4d} {m[0]:+8.2f} {m[1]:+8.2f} {m[2]:+8.2f} "
              f"{m[2] - m[0]:+9.2f}")
    if difs:
        pos = sum(1 for d in difs if d > 0)
        print(f"\n  húmedo-menos-seco mediano: {statistics.median(difs):+.2f}°F"
              f"   positivo en {pos}/{len(difs)} estaciones")
        print("  (positivo = sobre-predecimos más con suelo húmedo, "
              "que es la hipótesis)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
