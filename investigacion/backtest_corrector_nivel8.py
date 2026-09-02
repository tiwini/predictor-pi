#!/usr/bin/env python3
"""¿Entran en el corrector de nivel las ocho estaciones que el mapa marcó?

Sale de `diagnostico_20.py` (2026-08-30): nueve estaciones tienen el residuo
descentrado. KSFO se excluye porque **ya lleva corrector** — que se le quede
corto es otra pregunta, con otro criterio, y mezclarlas sería medir dos cosas
distintas con el mismo listón.

Quedan ocho:  KBOS KDEN KHOU KMSP KMSY KOKC KPHL KPHX

MISMA MECÁNICA Y MISMO CRITERIO QUE KMIA/KLAS
---------------------------------------------
`recoger`, `evaluar`, `bin_de` y `binom_cola` se importan de
`backtest_corrector_knyc.py`; la función de decisión, de
`backtest_corrector_kmia_klas.py`. Nada se reimplementa: si el criterio cambia,
cambia para todas a la vez.

=========================== PRE-REGISTRO ====================================
Escrito el 2026-08-31 ANTES de correr nada. Es el criterio del 2026-08-28,
palabra por palabra, aplicado a ocho estaciones **por separado**:

  ADOPTAR esa estación si las TRES:
    (a) |error| medio causal <= publicado − 0.75°F
    (b) el corrector acerca en >= 65% de los días Y p < 0.05 unilateral
    (c) el acierto de bin no baja
  RECHAZAR si la causal es peor en media, o acerca en < 50% de los días.
  ESPERAR en cualquier otro caso.

  Cada veredicto es independiente: que entre una no arrastra a ninguna otra.
  Se decide a la hora primaria (PEAK_HOURS[st][0] − 2) y se reporta la
  secundaria (14h) como contexto, igual que en las corridas anteriores.

LO QUE ESTA CORRIDA NO PUEDE RESPONDER
  - La muestra es un mes de VERANO (desde 2026-07-28). Un sesgo de agosto no
    dice nada del de octubre; el corrector es causal y se irá adaptando, pero
    la decisión hay que revisarla en otoño.
  - KDEN arrastra su fuente horaria ([[estaciones_sin_feed_5min]]): su sesgo
    puede estar contaminado por muestrear el pico 4× menos. Si sale ADOPTAR,
    anotarlo como caso a vigilar.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_corrector_nivel8.py
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stations import PEAK_HOURS                      # noqa: E402
import backtest_corrector_knyc as bk                 # noqa: E402
import backtest_corrector_kmia_klas as bkk           # noqa: E402
import level_corrector as lc                         # noqa: E402

ESTACIONES = ["KBOS", "KDEN", "KHOU", "KMSP", "KMSY", "KOKC", "KPHL", "KPHX"]
DESDE = "2026-07-28"


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    ya = sorted(lc.ENABLED_STATIONS)
    print(f"Entrada al corrector — 8 candidatas del mapa del 2026-08-30")
    print(f"ya dentro: {', '.join(ya)}\n")

    veredictos = {}
    for st in ESTACIONES:
        if st in lc.ENABLED_STATIONS:
            print(f"{st}: ya está habilitada, se salta"); continue
        bk.ST = st
        primaria = PEAK_HOURS[st][0] - 2
        print(f"\n{'=' * 68}\n{st}   hora primaria {primaria}h local\n{'=' * 68}")
        for hora, rol in ((primaria, "primaria"), (14, "secundaria")):
            filas = [f for f in bk.evaluar(bk.recoger(an, cal, hora))
                     if f["day"] >= DESDE]
            if len(filas) < 10:
                print(f"  {rol}: sólo {len(filas)} días — no decide")
                continue
            print(f"\n  ── {rol} ({hora}h) · N={len(filas)} "
                  f"({filas[0]['day']} → {filas[-1]['day']})")
            v = bkk.decidir(filas, f"{st} {hora}h")
            if rol == "primaria":
                veredictos[st] = v

    print(f"\n{'=' * 68}\nRESUMEN\n{'=' * 68}")
    for st, v in veredictos.items():
        marca = ("✅" if v.startswith("ADOPTAR") else
                 "🔴" if v.startswith("RECHAZAR") else "⏸")
        print(f"  {marca} {st}: {v}")
    adoptar = [s for s, v in veredictos.items() if v.startswith("ADOPTAR")]
    print(f"\n  ENABLED_STATIONS quedaría: "
          f"{sorted(set(lc.ENABLED_STATIONS) | set(adoptar))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
