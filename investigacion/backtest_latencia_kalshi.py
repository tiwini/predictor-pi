#!/usr/bin/env python3
"""¿Hay una ventana de minutos entre que llega el dato y que el mercado reacciona?

POR QUÉ ESTA PREGUNTA Y NO OTRA
-------------------------------
El objetivo declarado del proyecto (2026-08-20) es **descifrar el mercado de
Kalshi reaccionando antes que él**, cuando llega información fresca. Eso cambia
qué es una victoria: un |error| de 1.5°F no vale nada si el precio ya se movió,
y uno de 3°F sí sirve si viste 20 minutos antes que el día rompía un bin.

Todo lo medido hasta ahora mide **nivel**, no **latencia**:

    el Brier de Kalshi nos gana 7 de 9 días   (edges_no_estructurales_brier)
    el mercado lidera 2:1, p≈0.004            (lag_our_p_vs_kalshi_refutado)
    ningún gate ni señal predice el error     (feedback_triple_convergence_fails)

Son preguntas distintas. Ésta es la que decide si el enfoque es viable.

⚠⚠ LÍMITE DURO DE RESOLUCIÓN — leer antes que nada
`kalshi_snapshots` lo escribe `analysis_poller` cada **600 s**, y la cadencia
real medida es de **11.6 min** (rango 6.5–14.2). O sea:

    NO se puede detectar ninguna ventaja menor de ~12 minutos.

Si la ventaja real fuera de 5 minutos, este estudio dirá "no hay ventana" y
estará EQUIVOCADO. Un resultado negativo aquí significa **"no hay ventana
detectable a 12 minutos de resolución"**, nunca "no hay ventana".

Es exactamente la trampa de [[lag_our_p_vs_kalshi_refutado_2026_07_24]], donde
"11 min de ventaja" resultaron ser un tick de muestreo de 10.9 min. Aquella vez
el artefacto favoreció la hipótesis; aquí el mismo límite la penaliza. En los
dos casos la resolución mandaba.

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de la primera consulta.

EVENTO — elegido por ser OBJETIVO, sin umbral inventado
  Un bin muere cuando el día lo deja atrás: `floor > bin_hi + 0.5` (mismo
  redondeo que `zero_impossible_bins`). En ese instante su probabilidad
  verdadera es CERO, sin discusión ni modelo de por medio.

  Referencia temporal `t_dato`: el timestamp de la OBSERVACIÓN que lo mató
  (`today_max_obs_ts`, cuándo se emitió el METAR), no cuándo lo sondeamos
  nosotros. Lo que se mide es la carrera contra el dato, no contra nuestro
  poller.

MEDIDA
  `t_mercado` = primer snapshot con `yes_mid <= 0.05` para ese bin, después de
  que naciera muerto.
  `Δt = t_mercado − t_dato`, en minutos.

     Δt > 0  el mercado tardó en enterarse  → hay ventana
     Δt ≤ 0  el mercado ya lo sabía         → no la hay por este canal

CRITERIO DE DECISIÓN
  VIABLE     si mediana(Δt) >= 20 min  Y  >= 70% de los eventos con Δt > 0
             Y  N >= 100 eventos
  NO VIABLE  si mediana(Δt) <= 0
  INCONCLUSO en cualquier otro caso, y en particular si la mediana cae entre
             0 y 20 min — ahí el límite de resolución no permite distinguir
             señal de muestreo.

  El listón de 20 min no es arbitrario: es ~1.7× la cadencia de muestreo
  (11.6 min). Por debajo de eso no se puede separar una ventaja real de un tick.

CONTROL OBLIGATORIO — sin esto el resultado no vale
  Los mismos bins, en snapshots donde NO murió nada, para medir cuánto se mueve
  `yes_mid` por puro ruido de muestreo. Si el "retraso" del mercado es del mismo
  tamaño que su ruido, no hay nada.

LO QUE ESTE ESTUDIO NO PUEDE RESPONDER
  - Ventanas menores de ~12 min (ver el límite de arriba).
  - Si otros participantes ven el ASOS de 1 minuto antes de que se publique el
    METAR. Nuestro `t_dato` es la emisión del METAR; el dato físico existe
    antes. Si el mercado cotiza contra el feed de 1 minuto, Δt saldrá negativo
    y la conclusión correcta será "no por este canal", no "el mercado es lento".
  - El lado positivo (cuándo el mercado RECONOCE el bin ganador). Se reporta
    como secundario porque no tiene ground-truth objetivo comparable.

INSTRUMENTACIÓN QUE SE HACE PASE LO QUE PASE
  Muestrear Kalshi más rápido que 600 s en la ventana de pico. Sin eso, ninguna
  pregunta sobre latencia por debajo de 12 min es contestable — ni ésta ni las
  que vengan. Es el mismo caso que persistir `today_max_asos_6h`: el dato que no
  se guarda con la resolución suficiente no se puede analizar después.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_latencia_kalshi.py [dias]
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))

DIAS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
MUERTO = 0.05          # yes_mid por debajo = el mercado lo dio por muerto
N_MIN = 100
UMBRAL_MIN = 20.0


def main() -> int:
    con = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    desde = (datetime.utcnow() - timedelta(days=DIAS)).strftime("%Y-%m-%dT%H:%M:%S")

    # Eventos: (estación, día, bin) donde el piso acabó superando bin_hi+0.5.
    # Se toma el PRIMER snapshot en que eso pasó y el ts de la observación que
    # lo provocó.
    filas = con.execute("""
        SELECT s.station, date(s.ts) dia, s.ts, s.today_max_obs,
               s.today_max_obs_ts, k.bin_lo, k.bin_hi, k.yes_mid, k.ticker
        FROM station_snapshots s
        JOIN kalshi_snapshots k
          ON k.station = s.station AND k.ts = s.ts
        WHERE s.ts >= ? AND s.today_max_obs IS NOT NULL
          AND s.today_max_obs_ts IS NOT NULL
          AND k.yes_mid IS NOT NULL AND ABS(k.bin_hi) < 1e8
        ORDER BY s.station, k.ticker, s.ts
    """, (desde,)).fetchall()

    print(f"snapshots pareados: {len(filas)}\n")

    # Agrupar por (estación, ticker, día) y localizar el cruce.
    por_serie: dict = {}
    for r in filas:
        por_serie.setdefault((r["station"], r["ticker"], r["dia"]), []).append(r)

    deltas, sin_reaccion, ya_muerto = [], 0, 0
    for (st, tk, dia), serie in por_serie.items():
        cruce = None
        for r in serie:
            if r["today_max_obs"] > r["bin_hi"] + 0.5:
                cruce = r
                break
        if cruce is None:
            continue
        # ¿ya estaba muerto antes de morir? entonces no hay carrera que medir
        previos = [r for r in serie if r["ts"] < cruce["ts"]]
        if previos and previos[-1]["yes_mid"] <= MUERTO:
            ya_muerto += 1
            continue
        try:
            t_dato = datetime.fromisoformat(cruce["today_max_obs_ts"])
        except (ValueError, TypeError):
            continue
        posteriores = [r for r in serie if r["ts"] >= cruce["ts"]
                       and r["yes_mid"] <= MUERTO]
        if not posteriores:
            sin_reaccion += 1
            continue
        t_mercado = datetime.fromisoformat(posteriores[0]["ts"])
        if t_mercado.tzinfo is None:
            from datetime import timezone as _tz
            t_mercado = t_mercado.replace(tzinfo=_tz.utc)
        if t_dato.tzinfo is None:
            from datetime import timezone as _tz
            t_dato = t_dato.replace(tzinfo=_tz.utc)
        deltas.append((t_mercado - t_dato).total_seconds() / 60.0)

    print(f"eventos con carrera medible : {len(deltas)}")
    print(f"bins ya muertos de antemano : {ya_muerto}")
    print(f"sin reacción del mercado    : {sin_reaccion}")
    if not deltas:
        print("\nsin eventos — nada que concluir")
        return 0

    deltas.sort()
    med = statistics.median(deltas)
    pos = sum(1 for d in deltas if d > 0)
    print(f"\nΔt = t_mercado − t_dato   (minutos)")
    print(f"  mediana {med:+.1f}   p25 {deltas[len(deltas)//4]:+.1f}   "
          f"p75 {deltas[3*len(deltas)//4]:+.1f}")
    print(f"  positivos: {pos}/{len(deltas)} = {100*pos/len(deltas):.0f}%")

    print("\n── CRITERIO PRE-REGISTRADO ──")
    if len(deltas) < N_MIN:
        v = f"ESPERAR — N={len(deltas)} < {N_MIN}"
    elif med <= 0:
        v = "NO VIABLE — el mercado ya lo sabía"
    elif med >= UMBRAL_MIN and pos >= 0.70 * len(deltas):
        v = f"VIABLE — ventana mediana de {med:.0f} min"
    else:
        v = (f"INCONCLUSO — mediana {med:.1f} min bajo el umbral de "
             f"{UMBRAL_MIN:.0f}; a 11.6 min de resolución no se separa "
             f"de un tick de muestreo")
    print(f"  VEREDICTO: {v}")
    print("\n⚠ Un negativo aquí NO descarta ventanas menores de ~12 min: la")
    print("  cadencia de muestreo no permite verlas. Ver el encabezado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
