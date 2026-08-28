#!/usr/bin/env python3
"""¿En qué horas ayuda el corrector en KLAS, y dónde está la frontera?

La corrida de entrada (`backtest_corrector_kmia_klas.py`, 2026-08-28) midió dos
horas: a las 12h el corrector ayuda pero sin llegar al umbral, y a las 14h
**estorba**. El usuario decide habilitarlo sólo en la hora primaria; esto mide
dónde está la frontera en vez de suponerla, que es lo que el corrector prohíbe
en su propio docstring.

Mecánica **importada** de `backtest_corrector_knyc.py`: misma recogida, mismo
`evaluar`, mismos días. Lo único que cambia es que se barre la hora.

=========================== CRITERIO PRE-REGISTRADO ==========================
Escrito el 2026-08-28 ANTES de correr nada. Se conoce el resultado de 12h y 14h;
las demás horas no se han mirado nunca.

Se habilita el tramo **contiguo que contiene la hora primaria (12h)** formado
por las horas que cumplen LAS DOS:
    (a) |error| medio causal <= publicado   (cualquier mejora, no 0.75: la
        decisión de entrar ya está tomada; esto sólo fija la frontera)
    (b) el corrector acerca en >= 60% de los días

Si la hora primaria misma no cumple las dos, NO se habilita nada y se reporta.

Se reporta además el **escalón**: cuántos grados salta la predicción al entrar y
al salir de la ventana. Un corrector que se enciende y se apaga mueve la serie
publicada, y eso hay que verlo antes de desplegarlo, no después.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/ventana_horaria_klas.py
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stations import PEAK_HOURS               # noqa: E402
import backtest_corrector_knyc as bk          # noqa: E402

ST = "KLAS"
DESDE = "2026-07-28"
HORAS = list(range(9, 19))
MIN_ACERTAR = 0.60


def fila(an, cal, hora: int) -> dict | None:
    bk.ST = ST
    filas = [f for f in bk.evaluar(bk.recoger(an, cal, hora))
             if f["day"] >= DESDE]
    if len(filas) < 10:
        return None
    e_pub = [abs(f["pub"] - f["settle"]) for f in filas]
    e_cau = [abs(f["causal"] - f["settle"]) for f in filas]
    n = len(filas)
    acerca = sum(1 for a, b in zip(e_cau, e_pub) if a < b - 0.05)
    corr = [f["bias_usado"] for f in filas]
    return {"hora": hora, "n": n,
            "m_pub": statistics.mean(e_pub), "m_cau": statistics.mean(e_cau),
            "acerca": acerca, "prop": acerca / n,
            "corr": statistics.median(corr),
            "p": bk.binom_cola(acerca, n)}


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    primaria = PEAK_HOURS[ST][0] - 2
    print(f"{ST} — barrido horario del corrector (hora primaria {primaria}h)\n")
    print(f"  {'hora':>5s} {'N':>4s} {'pub':>7s} {'causal':>7s} {'Δ':>7s} "
          f"{'acerca':>8s} {'p':>7s} {'corr':>7s}  veredicto")

    res = {}
    for h in HORAS:
        f = fila(an, cal, h)
        if f is None:
            print(f"  {h:5d}    — sin muestra")
            continue
        ok = (f["m_cau"] <= f["m_pub"]) and (f["prop"] >= MIN_ACERTAR)
        res[h] = ok
        print(f"  {h:5d} {f['n']:4d} {f['m_pub']:7.2f} {f['m_cau']:7.2f} "
              f"{f['m_cau'] - f['m_pub']:+7.2f} "
              f"{f['acerca']:3d}/{f['n']:<3d} {100*f['prop']:3.0f}%"
              f" {f['p']:6.3f} {f['corr']:+7.2f}  {'✅' if ok else '❌'}")

    print()
    if not res.get(primaria):
        print(f"  ⇒ 🔴 la hora primaria ({primaria}h) NO cumple: no se habilita "
              "nada.")
        return 0

    lo = hi = primaria
    while lo - 1 in res and res[lo - 1]:
        lo -= 1
    while hi + 1 in res and res[hi + 1]:
        hi += 1
    print(f"  ⇒ ✅ ventana contigua alrededor de la primaria: **{lo}h a {hi}h**")

    # El escalón: cuánto salta la predicción al encender y apagar el corrector.
    borde_in = fila(an, cal, lo)
    borde_out = fila(an, cal, hi)
    fuera_antes = fila(an, cal, lo - 1) if (lo - 1) in range(0, 24) else None
    fuera_desp = fila(an, cal, hi + 1) if (hi + 1) < 24 else None
    print(f"\n  ESCALÓN al entrar/salir de la ventana (mediana de la corrección):")
    print(f"    {lo-1}h fuera: 0.00   →   {lo}h dentro: {borde_in['corr']:+.2f}°F")
    print(f"    {hi}h dentro: {borde_out['corr']:+.2f}   →   "
          f"{hi+1}h fuera: 0.00°F")
    if fuera_antes:
        print(f"    (a las {lo-1}h el corrector habría valido "
              f"{fuera_antes['corr']:+.2f}°F, y no se aplica)")
    if fuera_desp:
        print(f"    (a las {hi+1}h habría valido {fuera_desp['corr']:+.2f}°F)")
    print("\n  Un corrector que se enciende y se apaga mueve la serie publicada "
          "en\n  ese escalón. Es el precio de no aplicarlo donde estorba.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
