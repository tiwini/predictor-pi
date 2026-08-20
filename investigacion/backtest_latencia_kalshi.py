#!/usr/bin/env python3
"""¿Marcamos un bin como muerto antes que el mercado, y por cuánto?

POR QUÉ ESTA PREGUNTA
---------------------
El objetivo declarado del proyecto (2026-08-20) es **descifrar el mercado de
Kalshi reaccionando antes que él** cuando llega información fresca. Eso cambia
qué es una victoria: un |error| de 1.5°F no vale nada si el precio ya se movió,
y uno de 3°F sí sirve si viste antes que el día rompía un bin.

Todo lo medido hasta ahora mide **nivel**, no **latencia**: el Brier de Kalshi
nos gana 7 de 9 ([[edges_no_estructurales_brier_2026_07_27]]) y el mercado
lidera 2:1 ([[lag_our_p_vs_kalshi_refutado_2026_07_24]]). Son preguntas
distintas.

🔴 ESTE PRE-REGISTRO SUSTITUYE A UNO ANTERIOR QUE DIO UN FALSO POSITIVO
La primera versión (commit 4d1e2a7) definía el evento como "el piso cruza
`bin_hi + 0.5`" y dio **mediana +186 min, 100% positivos, VIABLE**. Era un
artefacto, y así se desmontó:

    series con cruce                          2113
      el cruce es el PRIMER snapshot del día   1105  (52%)  ← nunca hubo carrera
      el bin estuvo VIVO (>0.15) antes           386  (18%) ← la muestra real
    mercado ya lo había matado antes           1005  (48%)

Dos defectos que se sumaban:
  1. En la mitad de los casos el bin ya cotizaba a 0.005 desde antes de que
     empezáramos a mirar. No era el mercado reaccionando tarde: es que ese bin
     jamás estuvo vivo. Y como no había snapshot previo, el filtro de "ya
     muerto" no lo veía y lo contaba como carrera GANADA.
  2. Los 1005 casos en que el mercado se adelantó se excluían por "no hay
     carrera que medir" — o sea se tiraban justo las derrotas.

Lección para cualquier medida de latencia: **el denominador tiene que incluir
las carreras perdidas**, y hay que exigir que la carrera existiera.

⚠⚠ LÍMITE DURO DE RESOLUCIÓN
`kalshi_snapshots` lo escribe `analysis_poller` cada 600 s; la cadencia real
medida es **11.6 min** (rango 6.5–14.2). No se puede detectar ninguna ventaja
menor de ~12 min. Un negativo significa "no hay ventana detectable a esta
resolución", nunca "no hay ventana".

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de volver a consultar nada.

EVENTO — con las dos correcciones
  Sólo cuentan los bins que ESTUVIERON VIVOS: `yes_mid > 0.15` en algún
  snapshot del día antes de morir. Un bin que cotizó a 0.005 toda la jornada no
  es una carrera, es un bin que nadie disputó.

  Ese bin muere objetivamente cuando el día lo deja atrás:
  `today_max_obs > bin_hi + 0.5` (mismo redondeo que `zero_impossible_bins`).
  En ese instante su probabilidad verdadera es CERO, sin modelo de por medio.

MEDIDA PRIMARIA — cara a cara, robusta al muestreo
  Sobre los MISMOS instantes de muestreo, para cada bin vivo que muere:

    t_nuestro  = primer snapshot con `today_max_obs > bin_hi + 0.5`
    t_mercado  = primer snapshot con `yes_mid <= 0.05`
    Δt = t_mercado − t_nuestro   (minutos)

  Δt > 0  lo vimos antes  ·  Δt < 0  el mercado se adelantó

  Ambas series se muestrean en los mismos ticks, así que el artefacto de
  muestreo es **simétrico** — no puede favorecer a un lado. Ésa es la razón de
  preferir esto a comparar contra la hora de emisión del METAR.

  **TODOS los bins vivos que mueren entran en el denominador**, incluidos
  aquellos en que el mercado ganó. Es la corrección del defecto 2.

MEDIDA SECUNDARIA — latencia absoluta
  `t_mercado − today_max_obs_ts` (emisión del METAR que lo mató). Se reporta
  con la advertencia de que el mercado puede estar cotizando contra el feed
  ASOS de 1 minuto, que existe antes de que se publique el METAR: si sale
  negativo, la conclusión correcta es "no por este canal", no "el mercado es
  lento".

CONTROL OBLIGATORIO
  Los mismos bins vivos en snapshots donde NO murió nada: cuánto se mueve
  `yes_mid` por puro ruido entre ticks consecutivos. Si el "retraso" es del
  tamaño del ruido, no hay nada.

CRITERIO DE DECISIÓN
  VIABLE     si mediana(Δt) >= 20 min  Y  >= 70% de los eventos con Δt > 0
             Y  N >= 100 bins vivos
  NO VIABLE  si mediana(Δt) <= 0
  INCONCLUSO en cualquier otro caso, y en particular con la mediana entre 0 y
             20 min: ahí no se separa señal de tick de muestreo.

  El umbral de 20 min es ~1.7× la cadencia (11.6 min), no un número redondo.

LO QUE NO PUEDE RESPONDER
  - Ventanas menores de ~12 min.
  - Si el mercado cotiza contra el ASOS de 1 minuto antes de la publicación del
    METAR.
  - El lado positivo (cuándo el mercado RECONOCE al ganador): no tiene
    ground-truth objetivo comparable y se deja fuera a propósito.

INSTRUMENTACIÓN QUE SE HACE PASE LO QUE PASE
  Muestrear Kalshi más rápido que 600 s dentro de la ventana de pico. Sin eso
  ninguna pregunta de latencia sub-12 min es contestable, ni ésta ni las que
  vengan.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_latencia_kalshi.py [dias]
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))

DIAS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
VIVO = 0.15            # cotizó por encima de esto = la carrera existió
MUERTO = 0.05          # por debajo = el mercado lo dio por muerto
N_MIN = 100
UMBRAL_MIN = 20.0


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def main() -> int:
    con = sqlite3.connect("file:" + str(BASE / "analysis.db") + "?mode=ro",
                          uri=True)
    con.row_factory = sqlite3.Row
    desde = (datetime.now(timezone.utc)
             - timedelta(days=DIAS)).strftime("%Y-%m-%dT%H:%M:%S")

    filas = con.execute("""
        SELECT s.station, date(s.ts) dia, s.ts, s.today_max_obs,
               s.today_max_obs_ts, k.bin_hi, k.yes_mid, k.ticker
        FROM station_snapshots s
        JOIN kalshi_snapshots k
          ON k.station = s.station AND k.ts = s.ts
        WHERE s.ts >= ? AND s.today_max_obs IS NOT NULL
          AND k.yes_mid IS NOT NULL AND ABS(k.bin_hi) < 1e8
        ORDER BY s.station, k.ticker, s.ts
    """, (desde,)).fetchall()

    series: dict = {}
    for r in filas:
        series.setdefault((r["station"], r["ticker"], r["dia"]), []).append(r)

    deltas, absolutos = [], []
    nunca_vivo = sin_morir = sin_reaccion = 0
    ruido = []

    for _, s in series.items():
        i_nuestro = next((i for i, r in enumerate(s)
                          if r["today_max_obs"] > r["bin_hi"] + 0.5), None)
        if i_nuestro is None:
            # nunca murió: sirve de control de ruido si estuvo vivo
            if any(r["yes_mid"] > VIVO for r in s):
                for a, b in zip(s, s[1:]):
                    ruido.append(abs(b["yes_mid"] - a["yes_mid"]))
                sin_morir += 1
            continue
        # ¿existió la carrera? el bin tuvo que cotizar vivo ANTES de morir
        if not any(r["yes_mid"] > VIVO for r in s[:max(1, i_nuestro)]):
            nunca_vivo += 1
            continue
        i_mercado = next((i for i, r in enumerate(s)
                          if r["yes_mid"] <= MUERTO), None)
        if i_mercado is None:
            sin_reaccion += 1
            continue
        t_n, t_m = _dt(s[i_nuestro]["ts"]), _dt(s[i_mercado]["ts"])
        deltas.append((t_m - t_n).total_seconds() / 60.0)
        try:
            absolutos.append(
                (t_m - _dt(s[i_nuestro]["today_max_obs_ts"])).total_seconds() / 60.0)
        except (ValueError, TypeError):
            pass

    print(f"series analizadas          : {len(series)}")
    print(f"  descartadas · nunca vivo : {nunca_vivo}")
    print(f"  descartadas · sin morir  : {sin_morir}  (usadas de control)")
    print(f"  sin reacción del mercado : {sin_reaccion}")
    print(f"  CARRERAS MEDIBLES        : {len(deltas)}")
    if not deltas:
        print("\nsin eventos — nada que concluir")
        return 0

    deltas.sort()
    n = len(deltas)
    med = statistics.median(deltas)
    pos = sum(1 for d in deltas if d > 0)
    neg = sum(1 for d in deltas if d < 0)
    print(f"\nΔt = t_mercado − t_nuestro   (minutos, mismos ticks)")
    print(f"  mediana {med:+.1f}   p25 {deltas[n // 4]:+.1f}   "
          f"p75 {deltas[3 * n // 4]:+.1f}")
    print(f"  lo vimos antes : {pos}/{n} = {100 * pos / n:.0f}%")
    print(f"  se adelantaron : {neg}/{n} = {100 * neg / n:.0f}%")
    print(f"  empate en tick : {n - pos - neg}/{n}")

    if absolutos:
        absolutos.sort()
        print(f"\nsecundaria · t_mercado − emisión del METAR: "
              f"mediana {statistics.median(absolutos):+.1f} min")
        print("  ⚠ el mercado puede cotizar contra el ASOS de 1 min, que existe")
        print("    antes de que se publique el METAR")

    if ruido:
        ruido.sort()
        print(f"\ncontrol · |Δyes_mid| entre ticks en bins vivos que NO murieron:")
        print(f"  mediana {statistics.median(ruido):.3f}   "
              f"p90 {ruido[int(len(ruido) * .9)]:.3f}   n={len(ruido)}")

    print("\n── CRITERIO PRE-REGISTRADO ──")
    if n < N_MIN:
        v = f"ESPERAR — N={n} < {N_MIN}"
    elif med <= 0:
        v = "NO VIABLE — el mercado llega antes o a la vez"
    elif med >= UMBRAL_MIN and pos >= 0.70 * n:
        v = f"VIABLE — ventana mediana de {med:.0f} min"
    else:
        v = (f"INCONCLUSO — mediana {med:.1f} min bajo el umbral de "
             f"{UMBRAL_MIN:.0f}; a 11.6 min de resolución no se separa de un "
             f"tick de muestreo")
    print(f"  VEREDICTO: {v}")
    print("\n⚠ Un negativo NO descarta ventanas menores de ~12 min: la cadencia")
    print("  de muestreo no permite verlas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
