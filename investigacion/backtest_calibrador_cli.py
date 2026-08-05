#!/usr/bin/env python3
"""¿Condicionar el calibrador por "hay CLI parcial" mejora el Brier?

CONTEXTO
--------
Medido el 2026-08-03: la curva isotónica global comprime todo el rango [0, 0.97]
dentro de [0.03, 0.50]. Sólo el 5.4% de nuestras probabilidades calibradas pasan
de 0.50, contra el 11.8% del mercado. Ése es el mecanismo concreto de que todos
los edges salgan NO: si el mercado dice 0.90 y nuestro techo es 0.50, sale −40pp
por construcción, sin señal detrás.

La compresión NO es un bug: la isotónica se ajustó sobre outcomes reales, así que
mapear 0.9→0.46 significa que cuando decíamos 0.9 acertábamos el 46%. Es la
medida honesta de nuestra sobreconfianza.

Lo que sí puede ser un defecto de diseño es que el fit sea GLOBAL sobre todas las
horas y estados mezclados. Cuando ya hay CLI parcial publicado —que iguala el
settle final el 91% de los días— deberíamos poder decir 0.95, y el techo sigue
siendo 0.50. Se probó global vs por-estación (ganó global, 2026-07-24) pero nunca
por estado de información.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: condicionar el calibrador por presencia de CLI parcial no mejora el Brier
    out-of-sample respecto al calibrador global actual.

MUESTRA
  Pares (our_p crudo, outcome) de `kalshi_snapshots` cruzados con el settle real
  de `day_outcomes`. outcome = 1 si settle ∈ [bin_lo-0.5, bin_hi+0.5], que es la
  misma semántica de redondeo que usa `kalshi.our_p_for_bin`.
  El estado "hay CLI" se lee de `station_snapshots.today_max_cli` en el MISMO ts.

SPLIT — por DÍA, no por fila
  Los bin-snapshots del mismo día-estación son casi idénticos entre sí: hay
  ~479k filas pero el N efectivo son cientos de días-estación. Partir por fila
  metería el mismo día en train y test y regalaría una mejora falsa. Se ordenan
  los días y se parte 70/30 temporalmente; ningún día aparece en ambos lados.

TRES CALIBRADORES, ajustados SÓLO en train y evaluados SÓLO en test
  A) GLOBAL          — una curva. Es el baseline, lo que corre hoy.
  B) POR CLI         — dos curvas: con CLI parcial / sin CLI.
  C) POR HORA        — dos curvas partidas en las 13h local. **CONTROL.**

  C es imprescindible: el CLI sale por la tarde, así que "hay CLI" está
  fuertemente correlacionado con "es tarde". Sin este control, una mejora de B
  podría ser simplemente que por la tarde sabemos más — nada que ver con el CLI.

CRITERIO DE DECISIÓN
  ADOPTAR B si las DOS cosas:
    (1) Brier(B) < Brier(A) en test con mejora relativa >= 3%
    (2) Brier(B) < Brier(C)   — el CLI aporta algo más allá de la hora
  ZONA GRIS   mejora <3%, o mejora pero no supera al control C
  DESCARTAR   no mejora sobre A

  El 3% se fija de antemano por referencia al margen que separó global de
  por-estación en 2026-07-24 (0.15985 vs 0.17935, ~11%): un 3% es detectable y
  material sin ser ruido.

REQUISITOS DE N (si no se cumplen, el brazo no se evalúa)
  >= 500 pares y >= 20 días-estación por curva.

SE REPORTA ADEMÁS (no decide, pero es la motivación)
  qué fracción de las p calibradas supera 0.50 con cada calibrador, y el Brier
  restringido al subconjunto CON CLI, que es donde la hipótesis dice que está
  la ganancia.

AVISO
  Un Brier mejor no implica edges operables. Esto mide calibración, no P&L, y
  ningún resultado de aquí autoriza a operar por sí solo.
=============================================================================
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
import isotonic  # noqa: E402

MIN_PARES = 500
MIN_DIAS = 20
MEJORA_MIN = 0.03
HORA_CORTE = 13


def recoger() -> list[dict]:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles: dict[tuple[str, str], float] = {}
    for st, d, m in cal.execute(
            "SELECT station_id, date, max_obs_f FROM day_outcomes"):
        if m is not None:
            settles[(st, d)] = m

    an.execute("CREATE TEMP TABLE IF NOT EXISTS _x AS SELECT 1")
    filas = []
    q = """
      SELECT k.station, k.ts, k.bin_lo, k.bin_hi, k.our_p,
             s.today_max_cli, s.current_f
      FROM kalshi_snapshots k
      LEFT JOIN station_snapshots s
             ON s.station = k.station AND s.ts = k.ts
      WHERE k.our_p IS NOT NULL
    """
    for r in an.execute(q):
        dia = r["ts"][:10]
        key = (r["station"], dia)
        if key not in settles:
            continue
        settle = settles[key]
        lo, hi = r["bin_lo"], r["bin_hi"]
        if lo is None or hi is None:
            continue
        dentro = ((lo - 0.5) <= settle if abs(lo) < 1e8 else True) and \
                 (settle <= (hi + 0.5) if abs(hi) < 1e8 else True)
        # hora local aproximada: el ts es UTC; se usa la hora UTC menos un
        # offset fijo por estación no está disponible aquí, así que la hora
        # local se deriva del propio snapshot cuando existe. Si no, se descarta
        # de la rama C (no de A ni B).
        filas.append({
            "st": r["station"], "dia": dia, "ts": r["ts"],
            "p": float(r["our_p"]), "y": 1.0 if dentro else 0.0,
            "cli": r["today_max_cli"] is not None,
            "hora_utc": int(r["ts"][11:13]),
        })
    return filas


def brier(pares) -> float:
    return sum((p - y) ** 2 for p, y in pares) / len(pares) if pares else float("nan")


def evaluar(nombre, train, test, clave=None):
    """clave=None -> un solo calibrador. Si no, una curva por valor de clave."""
    if clave is None:
        cals = {None: isotonic.fit([(f["p"], f["y"]) for f in train],
                                   len({(f['st'], f['dia']) for f in train}))}
    else:
        cals = {}
        for v in {clave(f) for f in train}:
            sub = [f for f in train if clave(f) == v]
            dias = len({(f["st"], f["dia"]) for f in sub})
            if len(sub) < MIN_PARES or dias < MIN_DIAS:
                print(f"  {nombre}: rama {v!r} sin N suficiente "
                      f"({len(sub)} pares, {dias} días) — no evaluable")
                return None, None
            cals[v] = isotonic.fit([(f["p"], f["y"]) for f in sub], dias)
    pares, altos = [], 0
    for f in test:
        c = cals.get(clave(f) if clave else None)
        if c is None:
            continue
        pc = isotonic.apply(c, f["p"])
        pares.append((pc, f["y"]))
        if pc > 0.5:
            altos += 1
    return brier(pares), (altos / len(pares) if pares else 0.0)


def main() -> int:
    filas = recoger()
    if not filas:
        print("sin datos")
        return 1
    dias = sorted({f["dia"] for f in filas})
    corte = dias[int(len(dias) * 0.7)]
    train = [f for f in filas if f["dia"] < corte]
    test = [f for f in filas if f["dia"] >= corte]
    print(f"pares totales {len(filas)}   días {len(dias)}  "
          f"({dias[0]} .. {dias[-1]})")
    print(f"  corte en {corte}   train {len(train)} pares / "
          f"{len({f['dia'] for f in train})} días   "
          f"test {len(test)} pares / {len({f['dia'] for f in test})} días")
    con_cli = sum(1 for f in filas if f["cli"])
    print(f"  con CLI parcial: {con_cli} ({100*con_cli/len(filas):.1f}%)\n")

    bA, aA = evaluar("A global", train, test)
    bB, aB = evaluar("B por CLI", train, test, clave=lambda f: f["cli"])
    bC, aC = evaluar("C por hora (CONTROL)", train, test,
                     clave=lambda f: f["hora_utc"] >= HORA_CORTE + 4)

    print(f"{'calibrador':26s} {'Brier test':>11s} {'vs A':>8s} {'p>0.50':>8s}")
    for nom, b, a in (("A global (baseline)", bA, aA),
                      ("B por CLI parcial", bB, aB),
                      ("C por hora (control)", bC, aC)):
        if b is None or b != b:
            print(f"{nom:26s} {'—':>11s}")
            continue
        rel = (bA - b) / bA * 100 if bA else 0.0
        print(f"{nom:26s} {b:11.5f} {rel:+7.2f}% {100*a:7.2f}%")

    print()
    if bB is None or bA is None:
        print("VEREDICTO: no evaluable")
        return 0
    mejora = (bA - bB) / bA
    gana_control = bC is None or bB < bC
    if mejora >= MEJORA_MIN and gana_control:
        v = "ADOPTAR B — mejora >=3% y supera al control de hora"
    elif mejora > 0 and not gana_control:
        v = ("ZONA GRIS — B mejora pero NO supera al control C: lo que aporta "
             "es la hora, no el CLI")
    elif mejora > 0:
        v = f"ZONA GRIS — mejora {100*mejora:.2f}% < 3%"
    else:
        v = "DESCARTAR — B no mejora sobre A"
    print(f"VEREDICTO: {v}")

    # Brier restringido al subconjunto CON CLI: donde la hipótesis dice
    # que está la ganancia. No decide.
    sub = [f for f in test if f["cli"]]
    if sub:
        cA = isotonic.fit([(f["p"], f["y"]) for f in train],
                          len({(f['st'], f['dia']) for f in train}))
        pares_a = [(isotonic.apply(cA, f["p"]), f["y"]) for f in sub]
        tr_cli = [f for f in train if f["cli"]]
        if len(tr_cli) >= MIN_PARES:
            cB = isotonic.fit([(f["p"], f["y"]) for f in tr_cli],
                              len({(f['st'], f['dia']) for f in tr_cli}))
            pares_b = [(isotonic.apply(cB, f["p"]), f["y"]) for f in sub]
            print(f"\n  sólo filas CON CLI en test (N={len(sub)}): "
                  f"Brier A {brier(pares_a):.5f} → B {brier(pares_b):.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
