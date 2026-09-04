#!/usr/bin/env python3
"""¿Encoger la corrección cuando los días recientes contradicen a la mediana larga?

Viene de [[giro_regimen_septiembre]]: el 2026-09-04, cuatro de seis estaciones
cambiaron de régimen y el corrector siguió aplicando el sesgo del verano. Y
acortar la ventana **no** lo arregla —medido, ninguna W entre 7 y 30 mejora ni
0.03°F—: lo que una mediana corta gana adaptándose lo pierde persiguiendo ruido.

La idea de aquí es distinta y más conservadora: **no cambiar de qué historia se
saca el número, sino corregir MENOS cuando no sabemos**. Si los últimos k días
contradicen a la mediana larga, la confianza en esa corrección baja, y bajarla
no es lo mismo que sustituirla por la de la última semana.

LA REGLA QUE SE MIDE — `f = recorte`, y `bias = med_largo · f`

    continua   f = clip(med_corto / med_largo, 0, 1)
               si lo reciente coincide y es igual de grande  → f=1  (nada cambia)
               si lo reciente es la mitad                    → f=0.5
               si lo reciente cambió de signo                → f=0  (no corrige)
               ⚠ nunca puede AUMENTAR la corrección: por construcción va de 0 a 1

    signo      f = 0 si signo(med_corto) ≠ signo(med_largo), si no f = 1
    mitad      f = 0.5 si los signos difieren, si no f = 1

`med_corto` = mediana de los últimos k días; `med_largo` = la de siempre.

=========================== CRITERIO PRE-REGISTRADO ==========================
Escrito y **commiteado** el 2026-09-04 ANTES de correr nada. (La corrida de la
ventana, ese mismo día, escribió el criterio antes pero lo commiteó después;
esto lo corrige.)

  ADOPTAR una variante si las TRES:
    (a) mejora el |err| medio del conjunto de las 6 habilitadas en ≥0.20°F
        frente a lo desplegado hoy,
    (b) no empeora NINGUNA estación individual en más de 0.30°F,
    (c) **el efecto se sostiene con los dos valores de k (5 y 7)**. Robustez al
        parámetro: si sólo funciona con uno, es ajuste al ruido.

  Si ninguna cumple: no se toca nada, y queda anotado que tampoco el recorte
  por desacuerdo arregla el cambio de régimen.

  Entre variantes que cumplan se elige la MÁS CONSERVADORA — la que menos
  cambia la corrección vigente — y no la de mejor número.

Se reporta el |err| de los últimos 7 días aparte, informativo, sin decidir.

LO QUE NO RESPONDE
  · Si el mecanismo ayuda fuera de un cambio de régimen: la muestra tiene uno
    solo, el de septiembre. Un resultado bueno aquí hay que revalidarlo cuando
    haya un segundo giro.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/encoger_por_desacuerdo.py
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stations import PEAK_HOURS                  # noqa: E402
import backtest_corrector_knyc as bk             # noqa: E402
import level_corrector as lc                     # noqa: E402

KS = [5, 7]
MIN_PREV = lc.MIN_PREV_DAYS
UMBRAL_CONJUNTO = 0.20
UMBRAL_ESTACION = 0.30


def recorte(regla: str, corto: float, largo: float) -> float:
    if abs(largo) < 1e-9:
        return 1.0
    if regla == "continua":
        return max(0.0, min(1.0, corto / largo))
    if regla == "signo":
        return 0.0 if corto * largo < 0 else 1.0
    if regla == "mitad":
        return 0.5 if corto * largo < 0 else 1.0
    raise ValueError(regla)


def errores(filas: list[dict], regla: str | None, k: int = 0) -> list[float]:
    """|error| por día. `regla=None` = lo desplegado hoy (sin recorte)."""
    out, sesgos = [], []
    for f in filas:
        if len(sesgos) >= MIN_PREV:
            largo = statistics.median(sesgos)
            bias = largo
            if regla is not None and len(sesgos) >= k:
                bias = largo * recorte(regla, statistics.median(sesgos[-k:]), largo)
            out.append(abs((f["crudo"] - bias) - f["settle"]))
        sesgos.append(f["crudo"] - f["settle"])
    return out


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    ests = sorted(lc.ENABLED_STATIONS)
    datos = {}
    for st in ests:
        bk.ST = st          # `recoger` lee el global del módulo importado
        datos[st] = bk.recoger(an, cal, PEAK_HOURS[st][0] - 2)

    variantes = [("actual", None, 0)] + [(f"{r} k={k}", r, k)
                                         for r in ("continua", "signo", "mitad")
                                         for k in KS]
    print("Recorte por desacuerdo — |error| medio por estación\n")
    print(f"  {'est':6s} " + "".join(f"{n:>13s}" for n, _, _ in variantes))
    tabla = {}
    for st in ests:
        fila = [statistics.mean(errores(datos[st], r, k)) for _, r, k in variantes]
        tabla[st] = fila
        print(f"  {st:6s} " + "".join(f"{v:13.2f}" for v in fila))

    conj = [statistics.mean([tabla[st][i] for st in ests])
            for i in range(len(variantes))]
    print(f"\n  {'CONJUNTO':6s} " + "".join(f"{v:13.2f}" for v in conj))
    print(f"  {'vs hoy':6s} " + "".join(f"{conj[0] - v:+13.2f}" for v in conj)
          + "    (positivo = mejor)")

    print("\n  Últimos 7 días (informativo, NO decide):")
    for st in ests:
        fila = [statistics.mean(errores(datos[st], r, k)[-7:])
                for _, r, k in variantes]
        print(f"  {st:6s} " + "".join(f"{v:13.2f}" for v in fila))

    print("\n  ── CRITERIO ──")
    pasa = {}
    for i, (nom, r, k) in enumerate(variantes):
        if r is None:
            continue
        mejora = conj[0] - conj[i]
        empeora = max(tabla[st][i] - tabla[st][0] for st in ests)
        peor = max(ests, key=lambda s: tabla[s][i] - tabla[s][0])
        ok = mejora >= UMBRAL_CONJUNTO and empeora <= UMBRAL_ESTACION
        pasa[nom] = ok
        print(f"  {nom:12s} conjunto {mejora:+.2f} {'✓' if mejora >= UMBRAL_CONJUNTO else '✗'}(a)"
              f"   peor {peor} {empeora:+.2f} {'✓' if empeora <= UMBRAL_ESTACION else '✗'}(b)")

    robustas = [r for r in ("continua", "signo", "mitad")
                if all(pasa.get(f"{r} k={k}") for k in KS)]
    print(f"\n  (c) sostenidas con k=5 y k=7: {robustas or 'ninguna'}")
    if robustas:
        print(f"\n  ⇒ ✅ ADOPTAR la más conservadora de {robustas}")
    else:
        print("\n  ⇒ 🔴 NO ADOPTAR: el recorte por desacuerdo tampoco lo arregla")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
