#!/usr/bin/env python3
"""CONTROL de backtest_sky.py — ¿sw_manana sobrevive dentro de estación?

NO es una hipótesis nueva: el pre-registro cerró la lista de variables y las
cuatro fallaron. Esto es el control de robustez del único resultado con algo de
señal (sw_manana, rho=-0.191), y va sobre los MISMOS datos.

Por qué hace falta
------------------
En un pool de 20 estaciones, una correlación puede venir enteramente de
diferencias ENTRE estaciones: las desérticas tienen mucha radiación matinal
siempre y además sesgos propios, así que ambas cosas covarían sin que la
radiación explique nada del día concreto. Es el error de agregación que ya me
ha mordido varias veces en este proyecto (KPHX, KMIA, KATL).

Si el efecto es real, debe aparecer también DENTRO de cada estación, donde la
identidad de la estación está fijada por construcción.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from backtest_sky import recoger_filas, spearman   # noqa: E402


def main() -> int:
    filas = [f for f in recoger_filas() if f["sw_manana"] is not None]
    por_st: dict[str, list] = {}
    for f in filas:
        por_st.setdefault(f["st"], []).append(f)

    print("sw_manana vs error, DENTRO de cada estación\n")
    print(f"{'st':7s} {'N':>4s} {'rho':>7s}   {'sw mediana':>10s}  {'err mediano':>11s}")
    rhos = []
    for st in sorted(por_st):
        sub = por_st[st]
        if len(sub) < 15:
            continue
        rho, _ = spearman([f["sw_manana"] for f in sub], [f["err"] for f in sub])
        if rho == rho:
            rhos.append(rho)
        print(f"{st:7s} {len(sub):4d} {rho:+7.3f}   "
              f"{statistics.median(f['sw_manana'] for f in sub):10.2f}  "
              f"{statistics.median(f['err'] for f in sub):+11.2f}")

    if not rhos:
        print("\nninguna estación con N suficiente")
        return 1

    neg = sum(1 for r in rhos if r < 0)
    print(f"\n  estaciones: {len(rhos)}   rho mediano dentro de estación: "
          f"{statistics.median(rhos):+.3f}")
    print(f"  signo negativo en {neg}/{len(rhos)}  "
          f"(si fuera ruido puro se esperan ~{len(rhos) / 2:.0f})")
    print("  rho del pool cruzado era -0.191")

    # ¿cuánto del pool viene de diferencias ENTRE estaciones?
    medias = [(statistics.median(f["sw_manana"] for f in por_st[st]),
               statistics.median(f["err"] for f in por_st[st]))
              for st in por_st if len(por_st[st]) >= 15]
    rho_entre, p_entre = spearman([a for a, _ in medias], [b for _, b in medias])
    print(f"\n  correlación ENTRE estaciones (una fila por estación, N="
          f"{len(medias)}): rho={rho_entre:+.3f} p={p_entre:.3f}")
    print("  si ésta es fuerte y la de dentro ~0, el pool era efecto de estación")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
