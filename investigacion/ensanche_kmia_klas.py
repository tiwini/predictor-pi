#!/usr/bin/env python3
"""¿Cuánto hay que ensanchar la banda de KMIA y KLAS, y hace falta?

Viene de [[dispersion_banda]] (2026-08-28): el settle quedaba por encima del p90
el 93% de los días en KMIA y el 77% en KLAS.

⚠ **Ese número es de ANTES del corrector.** KMIA se habilitó ese mismo día y su
distribución entera sube ~2.4°F, así que buena parte de aquel escape era NIVEL y
no dispersión. Ensanchar sobre la cuenta vieja sería corregir dos veces el mismo
sesgo: la primera pregunta de esta corrida es cuánta miscalibración queda **con
el corrector puesto**. KLAS no lo lleva (se midió y no entró), así que su cuenta
no cambia.

QUÉ SE SIMULA, exactamente lo que se desplegaría
------------------------------------------------
Sobre los 500 miembros del snapshot, por día:

    v_corr = max(v − sesgo_causal, max_obs)        # corrector + piso
    v_k    = max(mediana + k·(v_corr − mediana), max_obs)   # ensanche + piso

El ensanche es **alrededor de la mediana**, así que el pronóstico central y el
piso no se mueven: sólo cambia la anchura. El piso se re-aplica porque inflar
hacia abajo por debajo de lo ya observado no significa nada.

Como los percentiles son afines, `p10_k = med + k·(p10 − med)` — o sea que este
barrido y lo que haría el código son la misma cantidad, no una aproximación.

=========================== CRITERIO PRE-REGISTRADO ==========================
Escrito el 2026-08-28 ANTES de correr nada.

  PRIMERO: cobertura con el corrector puesto y k=1 (sin ensanchar).
     Si queda en 70-90% ⇒ **NO se ensancha esa estación**: lo que había era
     nivel, ya está corregido, y ensanchar encima sería contar dos veces.

  Si no llega, se adopta un `k` por estación sólo si las tres:
     (a) la cobertura con ese k cae en 70-90% sobre la muestra completa,
     (b) el k de la 1ª mitad y el de la 2ª difieren <=25% (si no, es
         sobreajuste a un mes),
     (c) el ancho mediano resultante <= 8.0°F. Una banda más ancha que eso no
         informa de nada aunque cubra: cubrir por rendición no es calibrar.

  Se elige el k MÁS PEQUEÑO que cumpla, no el que mejor cubra: el objetivo es
  dejar de mentir, no maximizar cobertura.

⚠ Lo que este cambio toca aguas abajo: la banda, las probabilidades por bin
(`our_p`) y por tanto los edges. Es deliberado — una banda honesta con bins
mentirosos sería el fallo de [[principio_todo_se_reajusta]] otra vez. La
mediana, el piso y el techo físico NO se mueven, y eso se comprueba con un test.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/ensanche_kmia_klas.py [hora_local]
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispersion_banda as db          # noqa: E402
import level_corrector as lc           # noqa: E402

ESTACIONES = ["KMIA", "KLAS"]
MIN_PREV = lc.MIN_PREV_DAYS
ANCHO_MAX = 8.0


def con_corrector(regs: list[dict], st: str) -> list[dict]:
    """Aplica el sesgo causal día a día, como en producción.

    Sólo para estaciones habilitadas. El sesgo del día i sale de la mediana de
    los sesgos crudos de los días ANTERIORES — nunca del propio día.
    """
    out = []
    sesgos: list[float] = []
    for r in regs:
        crudo = r["med"] - r["settle"]
        if st in lc.ENABLED_STATIONS and len(sesgos) >= MIN_PREV:
            b = statistics.median(sesgos)
        else:
            b = 0.0
        sesgos.append(crudo)
        if not r["miembros"]:
            continue
        piso = r["max_obs"] if r["max_obs"] is not None else -999.0
        miembros = [max(v - b, piso) for v in r["miembros"]]
        out.append({**r, "miembros": miembros, "bias": b,
                    "med": statistics.median(miembros)})
    return out


def cobertura_k(regs: list[dict], k: float) -> tuple[float, float, float, float]:
    """(cubierto, por encima, por debajo, ancho mediano) al inflar por k."""
    cub = arr = aba = 0
    anchos = []
    for r in regs:
        piso = r["max_obs"] if r["max_obs"] is not None else -999.0
        med = r["med"]
        v = sorted(max(med + k * (x - med), piso) for x in r["miembros"])
        p10, p90 = v[int(len(v) * 0.1)], v[int(len(v) * 0.9)]
        anchos.append(p90 - p10)
        if r["settle"] > p90:
            arr += 1
        elif r["settle"] < p10:
            aba += 1
        else:
            cub += 1
    n = len(regs)
    return cub / n, arr / n, aba / n, statistics.median(anchos)


def k_minimo(regs: list[dict]) -> float | None:
    for i in range(10, 81):
        if cobertura_k(regs, i / 10)[0] >= 0.80:
            return i / 10
    return None


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    todos = db.dias(an, cal)
    print(f"Ensanche de banda — hora {db.HORA}h local\n")

    for st in ESTACIONES:
        regs = con_corrector([r for r in todos if r["st"] == st], st)
        if len(regs) < 15:
            print(f"{st}: sólo {len(regs)} días — no decide\n")
            continue
        hab = "CON corrector" if st in lc.ENABLED_STATIONS else "sin corrector"
        print(f"{'=' * 66}\n{st}  ({hab}, N={len(regs)})\n{'=' * 66}")
        sesgo_med = statistics.median([r["bias"] for r in regs])
        print(f"  corrección causal mediana aplicada: {sesgo_med:+.2f}°F")

        c, a, b, w = cobertura_k(regs, 1.0)
        print(f"\n  SIN ensanchar (k=1):  cubre {100*c:.0f}%  ·  "
              f"por encima {100*a:.0f}%  ·  por debajo {100*b:.0f}%  ·  "
              f"ancho {w:.2f}°F")
        if 0.70 <= c <= 0.90:
            print("  ⇒ ✅ NO SE ENSANCHA: el escape era nivel, ya corregido.\n")
            continue

        print(f"\n  {'k':>5s} {'cubre':>7s} {'>p90':>7s} {'<p10':>7s} {'ancho':>7s}")
        for i in range(10, 46, 5):
            k = i / 10
            c2, a2, b2, w2 = cobertura_k(regs, k)
            print(f"  {k:5.1f} {100*c2:6.0f}% {100*a2:6.0f}% {100*b2:6.0f}% "
                  f"{w2:7.2f}")

        k = k_minimo(regs)
        if k is None:
            print("\n  ⇒ 🔴 ningún k<=8 llega al 80%: no es sólo anchura\n")
            continue
        c3, _, _, w3 = cobertura_k(regs, k)
        mitad = len(regs) // 2
        k1, k2 = k_minimo(regs[:mitad]), k_minimo(regs[mitad:])
        est = (abs(k1 - k2) / max(k1, k2)) if (k1 and k2) else None
        print(f"\n  ── CRITERIO ──")
        print(f"  k mínimo que llega al 80%: {k}   (cubre {100*c3:.0f}%, "
              f"ancho {w3:.2f}°F)")
        print(f"  (a) cobertura en 70-90%           {100*c3:.0f}%"
              f"        {'SÍ' if 0.70 <= c3 <= 0.90 else 'NO'}")
        print(f"  (b) k estable entre mitades       {k1} vs {k2}"
              + (f" ({100*est:.0f}%)" if est is not None else "")
              + f"   {'SÍ' if (est is not None and est <= 0.25) else 'NO'}")
        print(f"  (c) ancho resultante <= {ANCHO_MAX}°F     {w3:.2f}"
              f"          {'SÍ' if w3 <= ANCHO_MAX else 'NO'}")
        ok = (0.70 <= c3 <= 0.90 and est is not None and est <= 0.25
              and w3 <= ANCHO_MAX)
        print(f"\n  VEREDICTO {st}: "
              + (f"ADOPTAR k={k}" if ok else "NO ADOPTAR — no cumple las tres")
              + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
