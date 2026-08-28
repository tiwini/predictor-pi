#!/usr/bin/env python3
"""¿Entran KMIA y KLAS en el corrector de nivel?

Salen de la medición de dispersión del 2026-08-28
([[dispersion_banda]]): son las dos estaciones que peor se escapan **por
arriba** —el settle queda sobre el p90 el 93% de los días en KMIA (con banda de
0.90°F) y el 77% en KLAS— y ninguna de las dos lleva corrector.

⚠ Serían las **primeras con corrección NEGATIVA**: KLAX, KSFO y KNYC
sobre-predicen y el corrector les resta; aquí sub-predecimos y habría que
sumar. Eso toca código que hasta hoy sólo ha visto biases positivos, así que el
pre-registro incluye una comprobación de consumidores, no sólo el número.

MISMA MECÁNICA QUE KNYC, NO UNA COPIA
-------------------------------------
`recoger`, `evaluar`, `bin_de` y `binom_cola` se **importan** de
`backtest_corrector_knyc.py`. Reimplementarlas sería medir otra cosa con el
mismo nombre. Lo único que cambia aquí es la estación, y el umbral de signos,
que se ajusta al N disponible (ver abajo).

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de la primera corrida.

MUESTRA   días >= 2026-07-28 (arranque de `station_snapshots`), que además son
          posteriores al backtest de pool del 07-27: ninguno de estos días se ha
          usado para decidir nada sobre el corrector. N esperado ~25-30 por
          estación tras descartar los 5 primeros de calentamiento.

HORAS     primaria  = PEAK_HOURS[st][0] − 2, la misma que usó KNYC.
          secundaria = 14h local, la hora a la que se opera.

CRITERIO — sobre la muestra a la hora primaria
  ADOPTAR si las TRES:
    (a) |error| medio causal <= publicado − 0.75°F   (mismo tamaño de efecto
        que se le exigió a KNYC: que valga lo que dice corregir, no décimas)
    (b) el corrector acerca en >= 65% de los días Y el test de signos
        unilateral da p < 0.05
    (c) el acierto de bin no baja
  RECHAZAR si la causal es peor en media, o acerca en < 50% de los días.
  ESPERAR   en cualquier otro caso.

  ⚠ El umbral (b) NO es el «>= 12 días» de KNYC: aquél se escribió para N≈17 y
  con N≈30 se cumpliría con 12 de 30, que es menos de la mitad. Se traduce a
  proporción + significancia, que es lo que aquel número quería decir.

  ADOPTAR significa añadir ESA estación a ENABLED_STATIONS. Cada una decide por
  separado: que entre una no arrastra a la otra.

COMPROBACIÓN DE CONSUMIDORES (bloqueante, aunque el número salga bien)
  Antes de habilitar hay que verificar qué hace cada consumidor de `bias_f` con
  un valor NEGATIVO: `cap_by_floor` (sólo actúa con bias>0), la guarda de
  cold-bias sobre YES, y el watchdog diario. Si algún consumidor asume el signo,
  se arregla ANTES de habilitar o no se habilita.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_corrector_kmia_klas.py
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stations import PEAK_HOURS                 # noqa: E402
import backtest_corrector_knyc as bk            # noqa: E402

ESTACIONES = ["KMIA", "KLAS"]
DESDE = "2026-07-28"
UMBRAL_MEDIA = 0.75
UMBRAL_PROP = 0.65


def decidir(filas: list[dict], st: str) -> str:
    e_pub = [abs(f["pub"] - f["settle"]) for f in filas]
    e_cau = [abs(f["causal"] - f["settle"]) for f in filas]
    n = len(filas)
    acerca = sum(1 for a, b in zip(e_cau, e_pub) if a < b - 0.05)
    aleja = sum(1 for a, b in zip(e_cau, e_pub) if a > b + 0.05)
    p = bk.binom_cola(acerca, n)

    bin_pub = bin_cau = bin_n = 0
    for f in filas:
        if not f["bins"]:
            continue
        real = bk.bin_de(f["bins"], f["settle"])
        if real is None:
            continue
        bin_n += 1
        bin_pub += bk.bin_de(f["bins"], f["pub"]) == real
        bin_cau += bk.bin_de(f["bins"], f["causal"]) == real

    m_pub, m_cau = statistics.mean(e_pub), statistics.mean(e_cau)
    print(f"\n  |error| medio   publicado {m_pub:.2f}   causal {m_cau:.2f}   "
          f"({m_cau - m_pub:+.2f})")
    print(f"  |error| mediano publicado {statistics.median(e_pub):.2f}   "
          f"causal {statistics.median(e_cau):.2f}")
    print(f"  signos: acerca {acerca}/{n} ({100*acerca/n:.0f}%), aleja {aleja}, "
          f"p={p:.4f}")
    print(f"  acierto de bin: publicado {bin_pub}/{bin_n} → causal {bin_cau}/{bin_n}")
    print(f"  corrección mediana: "
          f"{statistics.median([f['bias_usado'] for f in filas]):+.2f}°F")

    a = m_cau <= m_pub - UMBRAL_MEDIA
    b = (acerca >= UMBRAL_PROP * n) and p < 0.05
    c = bin_cau >= bin_pub
    print("\n  ── CRITERIO PRE-REGISTRADO ──")
    print(f"  (a) medio causal <= publicado − 0.75   "
          f"{m_cau:.2f} vs {m_pub - UMBRAL_MEDIA:.2f}      {'SÍ' if a else 'NO'}")
    print(f"  (b) acerca >= 65% y p < 0.05           "
          f"{100*acerca/n:.0f}% · p={p:.4f}   {'SÍ' if b else 'NO'}")
    print(f"  (c) acierto de bin no baja             "
          f"{bin_cau} vs {bin_pub}            {'SÍ' if c else 'NO'}")
    if a and b and c:
        v = f"ADOPTAR — añadir {st} a ENABLED_STATIONS"
    elif m_cau > m_pub or acerca < n / 2:
        v = f"RECHAZAR — el corrector no ayuda en {st}"
    else:
        v = "ESPERAR — no se cumplen las tres"
    print(f"\n  VEREDICTO {st}: {v}")
    return v


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    for st in ESTACIONES:
        bk.ST = st          # `recoger` lee el global del módulo importado
        primaria = PEAK_HOURS[st][0] - 2
        for hora, rol in ((primaria, "primaria"), (14, "secundaria")):
            print(f"\n{'=' * 70}\n{st} a las {hora}h local  ({rol})\n{'=' * 70}")
            filas = [f for f in bk.evaluar(bk.recoger(an, cal, hora))
                     if f["day"] >= DESDE]
            if len(filas) < 10:
                print(f"  sólo {len(filas)} días evaluables — no decide")
                continue
            print(f"  N={len(filas)} días ({filas[0]['day']} → {filas[-1]['day']})")
            difs = [f for f in filas if abs(f["pub"] - f["crudo"]) > 0.01]
            if difs:
                print(f"  ⚠ {len(difs)} días con bias aplicado: publicado != crudo")
            print(f"  {'día':12s} {'settle':>7s} {'pub':>7s} {'causal':>7s} "
                  f"{'corr':>6s} {'Δpub':>6s} {'Δcau':>6s}")
            for f in filas:
                print(f"  {f['day']:12s} {f['settle']:7.1f} {f['pub']:7.1f} "
                      f"{f['causal']:7.1f} {f['bias_usado']:+6.2f} "
                      f"{f['pub'] - f['settle']:+6.1f} "
                      f"{f['causal'] - f['settle']:+6.1f}")
            if rol == "primaria":
                decidir(filas, st)
            else:
                decidir(filas, st + " (secundaria, no decide)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
