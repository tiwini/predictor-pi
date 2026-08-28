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
    habilitada = st in lc.ENABLED_STATIONS
    for r in regs:
        crudo = r["med"] - r["settle"]
        listo = len(sesgos) >= MIN_PREV
        b = statistics.median(sesgos) if (habilitada and listo) else 0.0
        sesgos.append(crudo)
        if not r["miembros"]:
            continue
        # Los días de calentamiento —sin historia suficiente— NO cuentan para
        # una estación habilitada: en producción ya no ocurren, y contarlos
        # mete días sin corregir en la cobertura del corrector. (Se coló en la
        # primera corrida y hundía KMIA 5 días de 29.)
        if habilitada and not listo:
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


def cobertura_m(regs: list[dict], m: float) -> tuple[float, float, float, float]:
    """Igual que `cobertura_k` pero con el operador ADITIVO.

    Cada miembro se replica con offsets (−m, 0, 0, +m): una mezcla que añade
    ±m de dispersión ocurra lo que ocurra con la distribución de base. Es lo
    que hace falta cuando la banda está degenerada —el 33% de los días de KMIA
    tienen ancho <0.3°F— porque ahí multiplicar por k deja el cero en cero.
    """
    cub = arr = aba = 0
    anchos = []
    for r in regs:
        piso = r["max_obs"] if r["max_obs"] is not None else -999.0
        v = sorted(max(x + o, piso)
                   for x in r["miembros"] for o in (-m, 0.0, 0.0, m))
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


def m_minimo(regs: list[dict]) -> float | None:
    for i in range(1, 25):
        if cobertura_m(regs, i / 4)[0] >= 0.80:
            return i / 4
    return None


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

        # ── Operador ADITIVO ─────────────────────────────────────────────
        # Pre-registrado el 2026-08-28 tras ver que el multiplicativo se satura
        # en KMIA. Criterio: el m más pequeño con (a) cobertura 70-90%,
        # (b) m estable entre mitades <=25%, (c) ancho resultante <=8.0°F y
        # (d) NUEVO: |mediana del residuo| <= 0.75°F. Si el residuo está
        # descentrado, el problema es de NIVEL y ensanchar sólo lo tapa.
        res = sorted(r["settle"] - r["med"] for r in regs)
        centro = statistics.median(res)
        print(f"\n  {'m':>5s} {'cubre':>7s} {'>p90':>7s} {'<p10':>7s} {'ancho':>7s}"
              "     (aditivo)")
        for i in range(2, 13, 2):
            m = i / 4
            c4, a4, b4, w4 = cobertura_m(regs, m)
            print(f"  {m:5.2f} {100*c4:6.0f}% {100*a4:6.0f}% {100*b4:6.0f}% "
                  f"{w4:7.2f}")
        m = m_minimo(regs)
        if m is not None:
            c5, _, _, w5 = cobertura_m(regs, m)
            mitad = len(regs) // 2
            m1, m2 = m_minimo(regs[:mitad]), m_minimo(regs[mitad:])
            est_m = (abs(m1 - m2) / max(m1, m2)) if (m1 and m2) else None
            print(f"\n  ── CRITERIO (aditivo) ──")
            print(f"  m mínimo que llega al 80%: ±{m}°F  (cubre {100*c5:.0f}%, "
                  f"ancho {w5:.2f}°F)")
            print(f"  (a) cobertura en 70-90%        {100*c5:.0f}%"
                  f"        {'SÍ' if 0.70 <= c5 <= 0.90 else 'NO'}")
            print(f"  (b) m estable entre mitades    {m1} vs {m2}"
                  + (f" ({100*est_m:.0f}%)" if est_m is not None else "")
                  + f"   {'SÍ' if (est_m is not None and est_m <= 0.25) else 'NO'}")
            print(f"  (c) ancho <= {ANCHO_MAX}°F             {w5:.2f}"
                  f"          {'SÍ' if w5 <= ANCHO_MAX else 'NO'}")
            print(f"  (d) residuo centrado (<=0.75)  {centro:+.2f}"
                  f"         {'SÍ' if abs(centro) <= 0.75 else 'NO — es NIVEL, no anchura'}")
            ok_m = (0.70 <= c5 <= 0.90 and est_m is not None and est_m <= 0.25
                    and w5 <= ANCHO_MAX and abs(centro) <= 0.75)
            print(f"\n  VEREDICTO ADITIVO {st}: "
                  + (f"ADOPTAR m=±{m}°F" if ok_m
                     else "NO ADOPTAR — no cumple las cuatro"))
        else:
            print("\n  ningún m<=6°F llega al 80%")

        k = k_minimo(regs)
        if k is None:
            print("\n  ⇒ multiplicativo: ningún k<=8 llega al 80%\n")
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
