#!/usr/bin/env python3
"""¿Debe la mediana causal mirar sólo los últimos W días en vez de toda la historia?

El corrector usa la mediana de TODOS los días anteriores. La primera semana de
septiembre cuatro de seis estaciones cambiaron de régimen y el corrector siguió
aplicando la mediana del verano entero:

    est    ago 18-24  ago 25-31   sep 1-4   corrección vigente
    KSFO     +6.70      +5.16      −1.09    resta 4.97   ← se pasa 6°F
    KLAX     +2.40      −0.70      −0.20    resta 2.51
    KHOU     +2.97      +2.40      −0.40    resta 1.30
    KMIA     −2.50      −1.50      −1.05    suma  2.47
    KNYC     +3.30      +4.00      +4.80    resta 3.60   ← estable
    KLAS     −2.30      −1.99      −2.85    suma  1.96   ← creciendo

⚠ ESTO REABRE una corrida que quedó SIN DECIDIR el 2026-08-28, no una
rechazada. Aquella no pudo concluir por una razón concreta y medida: el efecto
entre ventanas era de 0.04-0.24°F y la réplica difería 0.46°F de la corrección
desplegada, así que el ruido del instrumento se comía la señal. Hoy el efecto
sería de 2-3°F. La condición que faltaba se cumplió sola.

Y se arregla el defecto de aquella: **todas las variantes se evalúan a la misma
hora fija y con la misma serie**, así que la comparación es interna y no depende
de reproducir el valor que se desplegó.

=========================== CRITERIO PRE-REGISTRADO ==========================
Escrito el 2026-09-04 ANTES de correr nada.

Un solo parámetro GLOBAL `W` para el mecanismo entero — no uno por estación:
seis parámetros sobre ~35 días cada uno es sobreajuste con otro nombre.

  ADOPTAR el W que cumpla las TRES:
    (a) mejora el |err| medio del conjunto de las 6 habilitadas en ≥0.20°F
        frente a la ventana actual ("todo"),
    (b) no empeora NINGUNA estación individual en más de 0.30°F,
    (c) el efecto es una MESETA, no un pico: al menos 3 valores de W contiguos
        cumplen (a). Un óptimo aislado se declara sobreajuste.

  Si ningún W cumple las tres: no se toca nada y se anota que la ventana no es
  la respuesta, pese al cambio de régimen.

Se reporta además el |err| de los ÚLTIMOS 7 DÍAS por separado —donde vive el
cambio de régimen— pero **no decide**: elegir por la última semana es ajustar al
ruido más reciente.

LO QUE NO RESPONDE
  · Si el problema es la ventana o el propio mecanismo. Una mediana corta se
    adapta antes y también es más ruidosa.
  · KLAX se deja fuera de cualquier acción: su veredicto lo dicta el watchdog
    (decisión del usuario, 2026-09-04).
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/ventana_mediana_causal.py
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

VENTANAS = [7, 10, 14, 21, 30, None]      # None = toda la historia (lo actual)
MIN_PREV = lc.MIN_PREV_DAYS
UMBRAL_CONJUNTO = 0.20
UMBRAL_ESTACION = 0.30


def errores(filas: list[dict], W: int | None) -> list[float]:
    """|error| por día aplicando la mediana causal con ventana W."""
    out, sesgos = [], []
    for f in filas:
        crudo = f["crudo"]
        if len(sesgos) >= MIN_PREV:
            prev = sesgos[-W:] if W else sesgos
            out.append(abs((crudo - statistics.median(prev)) - f["settle"]))
        sesgos.append(crudo - f["settle"])
    return out


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    ests = sorted(lc.ENABLED_STATIONS)
    datos = {}
    for st in ests:
        bk.ST = st
        # `recoger` y no `evaluar`: se quiere la serie ENTERA, porque cada
        # ventana necesita alimentarse de un número distinto de días previos.
        datos[st] = bk.recoger(an, cal, PEAK_HOURS[st][0] - 2)

    print("Ventana de la mediana causal — |error| medio por estación\n")
    cab = "  ".join(f"{('todo' if w is None else str(w)):>7s}" for w in VENTANAS)
    print(f"  {'est':6s} {'N':>3s}  {cab}")
    tabla = {}
    for st in ests:
        filas = datos[st]
        fila, n = [], 0
        for W in VENTANAS:
            e = errores(filas, W)
            n = len(e)
            fila.append(statistics.mean(e) if e else float("nan"))
        tabla[st] = fila
        print(f"  {st:6s} {n:3d}  " + "  ".join(f"{v:7.2f}" for v in fila))

    print()
    conj = [statistics.mean([tabla[st][i] for st in ests])
            for i in range(len(VENTANAS))]
    print(f"  {'CONJUNTO':10s} " + "  ".join(f"{v:7.2f}" for v in conj))
    base = conj[VENTANAS.index(None)]
    print(f"  {'vs actual':10s} " + "  ".join(
        f"{base - v:+7.2f}" for v in conj) + "   (positivo = mejor)")

    # ── últimos 7 días, informativo ────────────────────────────────────────
    print("\n  Últimos 7 días (informativo, NO decide):")
    for st in ests:
        fila = []
        for W in VENTANAS:
            e = errores(datos[st], W)[-7:]
            fila.append(statistics.mean(e) if e else float("nan"))
        print(f"  {st:6s}      " + "  ".join(f"{v:7.2f}" for v in fila))

    # ── criterio ──────────────────────────────────────────────────────────
    print("\n  ── CRITERIO ──")
    candidatos = []
    for i, W in enumerate(VENTANAS):
        if W is None:
            continue
        mejora = base - conj[i]
        peor = max(tabla[st][VENTANAS.index(None)] - tabla[st][i] for st in ests)
        peor_est = min(ests, key=lambda s: tabla[s][VENTANAS.index(None)] - tabla[s][i])
        empeora = tabla[peor_est][i] - tabla[peor_est][VENTANAS.index(None)]
        ok_a = mejora >= UMBRAL_CONJUNTO
        ok_b = empeora <= UMBRAL_ESTACION
        print(f"  W={str(W):>4s}  conjunto {mejora:+.2f}  "
              f"{'✓' if ok_a else '✗'}(a)   peor estación {peor_est} "
              f"{empeora:+.2f}  {'✓' if ok_b else '✗'}(b)")
        if ok_a and ok_b:
            candidatos.append(W)

    # (c) meseta: 3 valores contiguos de la lista que cumplan
    idx = [VENTANAS.index(w) for w in candidatos]
    meseta = any(all((i + k) in idx for k in range(3)) for i in idx)
    print(f"\n  candidatos: {candidatos or 'ninguno'}")
    print(f"  (c) meseta de 3 ventanas contiguas: {'SÍ' if meseta else 'NO'}")
    if candidatos and meseta:
        print(f"\n  ⇒ ✅ ADOPTAR W={min(candidatos)} (el más largo que cumple "
              "sería el más conservador; se elige el menor del rango contiguo)")
    else:
        print("\n  ⇒ 🔴 NO ADOPTAR: la ventana no cumple el criterio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
