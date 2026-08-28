#!/usr/bin/env python3
"""¿La banda p10-p90 del ensemble dice la verdad sobre lo que sabemos?

Nace del registro de KDEN del 2026-08-25 (settle 85.0): la banda era de 0.5°F,
el ensemble crudo daba CERO probabilidad a 85-86, y el día cerró 3.0°F por
encima del miembro más caliente de 500. La nota lo atribuyó a dispersión, no a
sesgo ni a calibración. Esto lo mide.

Distinto de lo ya medido: [[backtest_difficulty_componentes]] preguntó si el
ANCHO predice el error (rho=+0.047, descartado como gate). Aquí la pregunta es
si el ancho es **honesto**: una banda p10-p90 tiene que contener el resultado el
80% de las veces, y eso nunca se ha comprobado para el máximo.

=========================== CRITERIO PRE-REGISTRADO ==========================
Escrito el 2026-08-28 ANTES de correr nada.

Unidad: station-day. Snapshot más cercano a las 12:00 locales (±45 min), la
misma hora de decisión que usa el seguimiento del corrector. Settle = `max_obs_f`
de `day_outcomes` (NWS CLI, nunca proxy).

  MAL CALIBRADA si: cobertura de p10-p90 < 65% (nominal 80%) con N>=200,
  Y el déficit se repite en >=2/3 de las estaciones con N>=10 días.

  Se propone un factor de ensanche `k` concreto SÓLO si:
    (a) el mismo k deja la cobertura entre 70% y 90% en >=2/3 de las
        estaciones individualmente, y
    (b) el k de la primera mitad del periodo y el de la segunda difieren
        <=25%. Si no se cumplen las dos, se reporta la miscalibración SIN
        parámetro: un k inestable es sobreajuste a un mes de verano.

  Sharpness (decide la FORMA del arreglo, no si se hace):
    rho de Spearman entre ancho y |error|, DENTRO de cada estación y con test
    de signos entre estaciones — el pool cruzado exagera.
      mediana de rho <= 0.15  ⇒ la banda no informa del error: ensanche uniforme
      mediana de rho >  0.30  ⇒ la banda sí discrimina: ensanche proporcional

⚠ El ensanche hacia ABAJO es en buena parte inútil por construcción: cada
miembro vale max(max_obs, pronóstico), así que el borde bajo no puede caer bajo
lo ya observado. Por eso las dos colas se reportan por separado y el `k` que
importa es el de arriba.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/dispersion_banda.py [hora_local]
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from math import comb
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ            # noqa: E402

UTC = ZoneInfo("UTC")
HORA = int(sys.argv[1]) if len(sys.argv) > 1 else 12
VENTANA_MIN = 45
NOMINAL = 0.80


def p_signos(k: int, n: int) -> float:
    if n == 0:
        return 1.0
    k = max(k, n - k)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 5:
        return None

    def rangos(v):
        orden = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[orden[j + 1]] == v[orden[i]]:
                j += 1
            medio = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[orden[k]] = medio
            i = j + 1
        return r

    rx, ry = rangos(xs), rangos(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def dias(an, cal) -> list[dict]:
    """Un registro por station-day, al snapshot de la hora de decisión."""
    out = []
    for st in sorted({r[0] for r in an.execute(
            "SELECT DISTINCT station FROM station_snapshots").fetchall()}):
        if st not in STATION_TZ:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        for dia, settle in cal.execute(
                "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
                "AND max_obs_f IS NOT NULL ORDER BY date", (st,)).fetchall():
            try:
                d = datetime.strptime(dia, "%Y-%m-%d").date()
            except ValueError:
                continue
            ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=HORA)
            lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
            hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
            r = an.execute(
                """SELECT ts, ens_med, ens_p10, ens_p90, ens_maxes_json,
                          today_max_obs
                   FROM station_snapshots
                   WHERE station=? AND ts>=? AND ts<=? AND ens_p10 IS NOT NULL
                     AND ens_p90 IS NOT NULL AND ens_med IS NOT NULL
                   ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
                (st, lo.isoformat(), hi.isoformat(), ref.astimezone(UTC).isoformat()
                 )).fetchone()
            if r is None:
                continue
            miembros = []
            if r["ens_maxes_json"]:
                try:
                    miembros = sorted(json.loads(r["ens_maxes_json"]))
                except (json.JSONDecodeError, TypeError):
                    miembros = []
            out.append({
                "st": st, "dia": dia, "settle": settle,
                "med": r["ens_med"], "p10": r["ens_p10"], "p90": r["ens_p90"],
                "ancho": r["ens_p90"] - r["ens_p10"],
                "err": abs(r["ens_med"] - settle),
                "miembros": miembros,
                "max_obs": r["today_max_obs"],
            })
    return out


def cobertura(regs, k: float = 1.0) -> tuple[float, float, float]:
    """(cubierto, por encima, por debajo) escalando la banda por k."""
    cub = arr = aba = 0
    for x in regs:
        lo = x["med"] - k * (x["med"] - x["p10"])
        hi = x["med"] + k * (x["p90"] - x["med"])
        if x["settle"] > hi:
            arr += 1
        elif x["settle"] < lo:
            aba += 1
        else:
            cub += 1
    n = len(regs)
    return cub / n, arr / n, aba / n


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    regs = dias(an, cal)
    print(f"Dispersión de la banda p10-p90 — hora de decisión {HORA}h local")
    print(f"N = {len(regs)} station-days · "
          f"{min(x['dia'] for x in regs)} a {max(x['dia'] for x in regs)}\n")

    # ---------- A. Cobertura ----------
    cub, arr, aba = cobertura(regs)
    print("A. Cobertura de la banda (nominal 80%)")
    print(f"   cubierto {100*cub:.1f}%   ·   por ENCIMA del p90 {100*arr:.1f}%"
          f"   ·   por debajo del p10 {100*aba:.1f}%")
    anchos = [x["ancho"] for x in regs]
    errs = [x["err"] for x in regs]
    print(f"   ancho mediano {statistics.median(anchos):.2f}°F  "
          f"(p10 {sorted(anchos)[len(anchos)//10]:.2f} · "
          f"p90 {sorted(anchos)[9*len(anchos)//10]:.2f})")
    print(f"   |error| mediano {statistics.median(errs):.2f}°F  ·  "
          f"p90 del |error| {sorted(errs)[9*len(errs)//10]:.2f}°F")
    fuera_max = [x for x in regs if x["miembros"] and x["settle"] > x["miembros"][-1]]
    if regs:
        exc = [x["settle"] - x["miembros"][-1] for x in fuera_max]
        print(f"   el settle supera al miembro MÁS CALIENTE de 500 en "
              f"{len(fuera_max)} de {len(regs)} días ({100*len(fuera_max)/len(regs):.1f}%)"
              + (f", mediana +{statistics.median(exc):.2f}°F" if exc else ""))
    print()

    # ---------- B. Por estación ----------
    print("B. Por estación (cobertura y de qué lado se escapa)")
    por_st = defaultdict(list)
    for x in regs:
        por_st[x["st"]].append(x)
    deficit = 0
    con_n = 0
    print(f"   {'est':6s} {'N':>4s} {'cubre':>7s} {'>p90':>7s} {'<p10':>7s} "
          f"{'ancho':>7s} {'|err|':>7s}")
    for st in sorted(por_st):
        v = por_st[st]
        if len(v) < 10:
            continue
        con_n += 1
        c, a, b = cobertura(v)
        deficit += c < 0.65
        print(f"   {st:6s} {len(v):4d} {100*c:6.0f}% {100*a:6.0f}% {100*b:6.0f}% "
              f"{statistics.median([x['ancho'] for x in v]):7.2f} "
              f"{statistics.median([x['err'] for x in v]):7.2f}")
    print(f"   → {deficit} de {con_n} estaciones por debajo del 65% de cobertura\n")

    # ---------- C. PIT: dónde cae el settle dentro de los 500 ----------
    print("C. PIT — en qué decil de los 500 miembros cae el settle")
    pits = []
    for x in regs:
        m = x["miembros"]
        if not m:
            continue
        pits.append(sum(1 for v in m if v < x["settle"]) / len(m))
    if pits:
        hist = [0] * 10
        for p in pits:
            hist[min(9, int(p * 10))] += 1
        print("   " + "  ".join(f"{i*10}-{i*10+10}" for i in range(10)))
        print("   " + "  ".join(f"{100*h/len(pits):5.1f}%" for h in hist))
        print(f"   (uniforme = 10% en cada decil · n={len(pits)})")
        print(f"   settle bajo el miembro más frío: "
              f"{100*sum(1 for p in pits if p == 0)/len(pits):.1f}%  ·  "
              f"sobre el más caliente: "
              f"{100*sum(1 for p in pits if p == 1)/len(pits):.1f}%")
    print()

    # ---------- D. Sharpness: ¿el ancho informa del error? ----------
    print("D. ¿El ancho predice el error? (rho DENTRO de estación)")
    rhos = []
    for st in sorted(por_st):
        v = por_st[st]
        if len(v) < 10:
            continue
        r = spearman([x["ancho"] for x in v], [x["err"] for x in v])
        if r is not None:
            rhos.append((st, r))
    pos = sum(1 for _, r in rhos if r > 0)
    print("   " + " · ".join(f"{st} {r:+.2f}" for st, r in rhos))
    print(f"   mediana de rho {statistics.median([r for _, r in rhos]):+.3f}  ·  "
          f"{pos} de {len(rhos)} positivas (p={p_signos(pos, len(rhos)):.3f})\n")

    # ---------- E. Factor de ensanche ----------
    print("E. Factor de ensanche k para llegar al 80%")
    k_glob = None
    for i in range(10, 101):
        k = i / 10
        if cobertura(regs, k)[0] >= NOMINAL:
            k_glob = k
            break
    print(f"   k global = {k_glob}   (cobertura {100*cobertura(regs, k_glob)[0]:.1f}%"
          f" con banda mediana {statistics.median(anchos)*k_glob:.2f}°F)"
          if k_glob else "   ningún k <=10 llega al 80%")
    # (a) mismo k por estación
    if k_glob:
        ok = 0
        det = []
        for st in sorted(por_st):
            v = por_st[st]
            if len(v) < 10:
                continue
            c = cobertura(v, k_glob)[0]
            ok += 0.70 <= c <= 0.90
            det.append(f"{st} {100*c:.0f}%")
        print("   con ese k por estación: " + " · ".join(det))
        print(f"   → {ok} de {con_n} caen en 70-90%")
        # (b) estabilidad temporal
        fechas = sorted({x["dia"] for x in regs})
        corte = fechas[len(fechas) // 2]
        mitades = {}
        for etiqueta, sel in (("1ª mitad", [x for x in regs if x["dia"] < corte]),
                              ("2ª mitad", [x for x in regs if x["dia"] >= corte])):
            kk = next((i / 10 for i in range(10, 101)
                       if cobertura(sel, i / 10)[0] >= NOMINAL), None)
            mitades[etiqueta] = kk
            print(f"   {etiqueta} (n={len(sel)}, corte {corte}): k = {kk}")
        a, b = mitades.get("1ª mitad"), mitades.get("2ª mitad")
        if a and b:
            dif = abs(a - b) / max(a, b)
            print(f"   → diferencia entre mitades {100*dif:.0f}% "
                  f"({'<=25%, estable' if dif <= 0.25 else '>25%, INESTABLE'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
