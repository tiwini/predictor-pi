#!/usr/bin/env python3
"""¿Se habilita el corrector de nivel en KNYC?

CONTEXTO
--------
`level_corrector.ENABLED_STATIONS` cubre hoy sólo KLAX y KSFO. Su docstring deja
escrito el motivo de no haber generalizado: el backtest de pool (N=460) apoyaba
extender a 14 de 20 estaciones, pero «el pre-registro pedía confirmar con días
frescos antes de generalizar, así que se amplía con datos y no de golpe».

KNYC llega ahora con caso propio, por tres vías independientes:

  1. Sobre-predijimos 3 de 3 días esta semana, siempre en el mismo sentido:
     +3.4 (08-13), +4.1 (08-14), +2.2 (08-15, CLI parcial).
  2. Su `bias_median_causal_f` instrumentado vale **+3.97°F sobre 29 días**, el
     mayor del roster fuera de las dos ya habilitadas.
  3. El caso KNYC 2026-08-12 (`caso_knyc_2026_08_12.md`) y la guarda de techo
     físico salieron los dos del mismo modo de fallo: predicción alta que la
     tarde no respalda.

Ninguna de las tres es un backtest. Esto lo es.

⚠ KNYC NO tiene historia larga: el rename KLGA→KNYC es del 2026-07-22 y los
snapshots de `analysis.db` arrancan el 2026-07-15. Los settles llegan hasta
abril por el backfill CF6, pero sin snapshot no hay sesgo que medir. El N es
pequeño y el criterio de abajo está escrito sabiéndolo.

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de la primera corrida.

QUÉ SE COMPARA, mismo día y mismo snapshot
  pred_pub    = ens_med                       lo que el sistema publicó
  pred_crudo  = ens_med + bias_f aplicado     deshace el bias del sistema
  pred_causal = pred_crudo − mediana(sesgos de días ANTERIORES)   <- primaria
  pred_loo    = pred_crudo − mediana(sesgos de todos menos el día)  cota

  En KNYC se espera pred_pub == pred_crudo: no está en ENABLED_STATIONS y el
  EWMA quedó jubilado el 2026-08-14. Si difieren, el backtest lo dice y hay que
  entender por qué antes de leer el resto.

MUESTRAS  (dos, y la decisión cuelga de la primera)
  FRESCA    días >= 2026-07-29, posteriores al backtest de pool que ya usó los
            días previos de KNYC. Es la muestra de decisión.
  COMPLETA  todos los días evaluables, desde el 6º con snapshot. Se reporta
            como contexto; NO decide, porque solapa con la base ya usada.

HORAS  (la primaria reproduce la metodología del backtest de pool)
  11h local = PEAK_HOURS[KNYC][0] − 2 = 13 − 2.  Primaria.
  14h local, dentro de la ventana de pico. Secundaria, porque el sesgo decae
  durante el día (medido 2026-08-05) y es la hora a la que se opera de verdad.
  Si ayuda a las 11 y estorba a las 14, se habilita con `local_hour`, no plano.

MIN_PREV = 5 días anteriores con sesgo medible, igual que en producción.

CRITERIO DE DECISIÓN — sobre la muestra FRESCA a las 11h
  ADOPTAR   si las TRES se cumplen:
              (a) |error| medio causal <= publicado − 0.75°F
              (b) el corrector acerca en >= 12 de N días (signos)
              (c) el acierto de bin no baja
  RECHAZAR  si la causal es peor en media, o acerca en < la mitad de los días.
  ESPERAR   en cualquier otro caso: se revisa con N >= 30 frescos (~2026-09-01).

  El umbral (a) es 0.75°F y no los 0.30 del backtest de pool a propósito: con
  N≈17 días de una sola estación, una diferencia de décimas es ruido. Se pide
  que el efecto sea del tamaño del sesgo que dice corregir (~+4°F) o no se
  aplica. (b) es el test de signos, que no depende del tamaño de los outliers;
  12 de 17 es p≈0.07 unilateral — no llega a 0.05, y por eso hace falta (a)
  también, no en lugar de.

  ADOPTAR aquí significa **añadir KNYC a ENABLED_STATIONS**, nada más. No
  autoriza a extender a ninguna otra estación: cada una necesita su corrida.

LO QUE ESTE BACKTEST NO PUEDE RESPONDER
  - Si el sesgo de KNYC es estable más allá de un mes. Toda su historia cabe en
    julio-agosto, o sea una sola estación del año. Un sesgo de verano no dice
    nada del de octubre, y el corrector es causal, así que se irá adaptando.
  - Si el corrector interactúa mal con el piso de observación (`cap_by_floor`).
    A las 11h el piso está lejos; a las 14h puede morder. La guarda existe en
    producción y aquí no se simula.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_corrector_knyc.py
"""
from __future__ import annotations

import math
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
ST = "KNYC"
TOL_MIN = 30
MIN_PREV = 5
FRESCO_DESDE = "2026-07-29"
HORAS = [(PEAK_HOURS[ST][0] - 2, "primaria"), (14, "secundaria")]


def binom_cola(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) con X~Binomial(n, p). Unilateral, exacta."""
    return sum(math.comb(n, i) * p**i * (1 - p)**(n - i) for i in range(k, n + 1))


def bin_de(bins, v):
    """El bin de Kalshi que contiene v, o None. Medio grado de tolerancia en el
    corte, igual que `fiabilidad_por_estacion_hora.dentro`."""
    for b in bins:
        lo, hi = b["bin_lo"], b["bin_hi"]
        if (((lo - 0.5) <= v if abs(lo) < 1e8 else True)
                and (v <= (hi + 0.5) if abs(hi) < 1e8 else True)):
            return (lo, hi)
    return None


def recoger(an, cal, hora: int) -> list[dict]:
    """Un dict por día evaluable de KNYC, ordenados por fecha."""
    tz = ZoneInfo(STATION_TZ[ST])
    settles = dict(cal.execute(
        "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=?",
        (ST,)).fetchall())

    filas = []
    for day, settle in sorted(settles.items()):
        if settle is None:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=hora)
        lo = (ref - timedelta(minutes=TOL_MIN)).astimezone(UTC)
        hi = (ref + timedelta(minutes=TOL_MIN)).astimezone(UTC)
        r = an.execute(
            """SELECT ts, ens_med, bias_f, bias_applied FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (ST, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             ref.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is None:
            continue
        b = r["bias_f"] if (r["bias_applied"] and r["bias_f"] is not None) else 0.0
        bins = an.execute(
            """SELECT bin_lo, bin_hi FROM kalshi_snapshots
               WHERE station=? AND ts=(SELECT ts FROM kalshi_snapshots
                                       WHERE station=? AND ts<=?
                                       ORDER BY ts DESC LIMIT 1)""",
            (ST, ST, r["ts"])).fetchall()
        filas.append({"day": day, "settle": settle, "pub": r["ens_med"],
                      "crudo": r["ens_med"] + b, "bins": bins})
    return filas


def evaluar(filas: list[dict]) -> list[dict]:
    """Añade pred_causal y pred_loo. Descarta los primeros MIN_PREV días."""
    sesgos = [f["crudo"] - f["settle"] for f in filas]
    out = []
    for i, f in enumerate(filas):
        if i < MIN_PREV:
            continue
        prev = sesgos[:i]
        otros = sesgos[:i] + sesgos[i + 1:]
        f = dict(f)
        f["causal"] = f["crudo"] - statistics.median(prev)
        f["loo"] = f["crudo"] - statistics.median(otros)
        f["bias_usado"] = statistics.median(prev)
        out.append(f)
    return out


def informe(filas: list[dict], titulo: str, decide: bool) -> dict:
    if not filas:
        print(f"\n{titulo}: sin días evaluables")
        return {}
    e_pub = [abs(f["pub"] - f["settle"]) for f in filas]
    e_cru = [abs(f["crudo"] - f["settle"]) for f in filas]
    e_cau = [abs(f["causal"] - f["settle"]) for f in filas]
    e_loo = [abs(f["loo"] - f["settle"]) for f in filas]
    acerca = sum(1 for a, b in zip(e_cau, e_pub) if a < b - 0.05)
    aleja = sum(1 for a, b in zip(e_cau, e_pub) if a > b + 0.05)
    n = len(filas)

    bin_pub = bin_cau = bin_n = 0
    for f in filas:
        if not f["bins"]:
            continue
        bin_n += 1
        real = bin_de(f["bins"], f["settle"])
        if real is None:
            continue
        bin_pub += bin_de(f["bins"], f["pub"]) == real
        bin_cau += bin_de(f["bins"], f["causal"]) == real

    mean, med = statistics.mean, statistics.median
    print(f"\n{titulo}   N={n} días  ({filas[0]['day']} → {filas[-1]['day']})")
    print(f"  {'':22s} {'medio':>7s} {'mediano':>8s}")
    for nom, e in (("publicado", e_pub), ("crudo (sin bias)", e_cru),
                   ("CAUSAL  <- primaria", e_cau), ("LOO (cota optimista)", e_loo)):
        print(f"  |error| {nom:22s} {mean(e):6.2f} {med(e):8.2f}")
    print(f"  causal − publicado:  {mean(e_cau) - mean(e_pub):+.2f}°F medio, "
          f"{med(e_cau) - med(e_pub):+.2f}°F mediano")
    print(f"  signos: acerca {acerca}/{n}, aleja {aleja}/{n}, "
          f"neutro {n - acerca - aleja}   p={binom_cola(acerca, n):.3f}")
    if bin_n:
        print(f"  acierto de bin: publicado {bin_pub}/{bin_n} → "
              f"causal {bin_cau}/{bin_n}")
    print(f"  corrección mediana aplicada: "
          f"{med([f['bias_usado'] for f in filas]):+.2f}°F")

    if not decide:
        return {}
    print("\n  ── CRITERIO PRE-REGISTRADO ──")
    a = mean(e_cau) <= mean(e_pub) - 0.75
    b = acerca >= 12
    c = bin_cau >= bin_pub
    print(f"  (a) medio causal <= publicado − 0.75    "
          f"{mean(e_cau):.2f} vs {mean(e_pub) - 0.75:.2f}   {'SÍ' if a else 'NO'}")
    print(f"  (b) acerca en >= 12 días                "
          f"{acerca}                {'SÍ' if b else 'NO'}")
    print(f"  (c) acierto de bin no baja              "
          f"{bin_cau} vs {bin_pub}            {'SÍ' if c else 'NO'}")
    if a and b and c:
        v = "ADOPTAR — añadir KNYC a ENABLED_STATIONS"
    elif mean(e_cau) > mean(e_pub) or acerca < n / 2:
        v = "RECHAZAR — el corrector no ayuda en KNYC"
    else:
        v = "ESPERAR — revisar con N>=30 frescos (~2026-09-01)"
    print(f"\n  VEREDICTO: {v}")
    return {"veredicto": v}


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    for hora, rol in HORAS:
        print(f"\n{'=' * 70}\n{ST} a las {hora}h local  ({rol})\n{'=' * 70}")
        filas = evaluar(recoger(an, cal, hora))
        if not filas:
            print("  sin días evaluables")
            continue
        difs = [f for f in filas if abs(f["pub"] - f["crudo"]) > 0.01]
        if difs:
            print(f"  ⚠ {len(difs)} días con bias aplicado: publicado != crudo")
        frescos = [f for f in filas if f["day"] >= FRESCO_DESDE]
        informe(frescos, f"MUESTRA FRESCA (>= {FRESCO_DESDE})",
                decide=(rol == "primaria"))
        informe(filas, "MUESTRA COMPLETA (contexto, no decide)", decide=False)

    print("\n  detalle día a día de la muestra fresca a las "
          f"{HORAS[0][0]}h:")
    filas = [f for f in evaluar(recoger(an, cal, HORAS[0][0]))
             if f["day"] >= FRESCO_DESDE]
    print(f"  {'día':12s} {'settle':>7s} {'pub':>7s} {'causal':>7s} "
          f"{'corr':>6s} {'Δpub':>6s} {'Δcau':>6s}")
    for f in filas:
        print(f"  {f['day']:12s} {f['settle']:7.1f} {f['pub']:7.1f} "
              f"{f['causal']:7.1f} {f['bias_usado']:+6.2f} "
              f"{f['pub'] - f['settle']:+6.1f} {f['causal'] - f['settle']:+6.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
