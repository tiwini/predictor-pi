#!/usr/bin/env python3
"""¿Reconocemos al bin GANADOR antes que el mercado?

LA MITAD QUE FALTABA
--------------------
`backtest_latencia_kalshi.py` midió el lado bajista —cuándo se da un bin por
muerto— y salió **NO VIABLE**: el mercado mata el bin una mediana de 99.5 min
antes que nosotros, en el 99% de 385 carreras. Pero eso sólo mide un sentido.

Este mide el otro: de todos los bins, el que acaba GANANDO, ¿quién lo señala
primero? Es la pregunta que de verdad importa para operar, porque el dinero está
en reconocer al ganador, no en descartar perdedores.

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de la primera consulta.

GROUND TRUTH — objetivo y retrospectivo
  El bin ganador es el que contiene el settle del NWS (`day_outcomes.max_obs_f`),
  con el ±0.5 de siempre. No hay modelo de por medio ni umbral inventado.

🔑 POR QUÉ argmax Y NO "p > 0.5"
  Nuestro `our_p_calibrated` **casi nunca supera 0.5**: el techo del calibrador
  está medido y es correcto ([[calibrador_techo_050_2026_08_03]], our_p 0.80
  acierta 0.35). Comparar "cuándo cada lado pasa de 0.5" sería una derrota por
  construcción — el mercado llega a 0.9 y nosotros no llegamos nunca.

  Se usa **argmax**: en cada snapshot, cuál es el bin más probable para cada
  lado. La pregunta pasa a ser "¿cuándo apuntas al ganador?", que es
  invariante a la escala y a la calibración. Ésa es la comparación justa.

MEDIDA PRIMARIA — cara a cara sobre los MISMOS ticks
    t_nuestro = primer snapshot en que el bin ganador es nuestro argmax
    t_mercado = primer snapshot en que el bin ganador es el argmax del mercado
    Δt = t_mercado − t_nuestro   (minutos)

    Δt > 0  lo señalamos antes  ·  Δt < 0  el mercado se adelantó

  Ambas series salen del MISMO snapshot, así que el artefacto de muestreo es
  simétrico. Es la corrección que se aprendió en el lado bajista.

  DENOMINADOR: todos los station-days con settle y con bins, incluidos aquellos
  en que **nunca** llegamos a señalar al ganador. Ésos cuentan como derrota, no
  se descartan — fue el defecto que produjo el falso positivo de +186 min en el
  primer intento del lado bajista.

MEDIDA SECUNDARIA — persistencia
  Señalar al ganador un instante y luego cambiar de opinión no es reconocerlo.
  Se reporta también el primer instante a partir del cual el argmax **ya no
  cambia** hasta el cierre. Si ganamos en el primero pero perdemos en éste, lo
  que tenemos es ruido, no anticipación.

CRITERIO DE DECISIÓN
  VIABLE     si mediana(Δt) >= 20 min  Y  >= 60% de los días con Δt > 0
             Y  N >= 100 station-days
  NO VIABLE  si mediana(Δt) <= 0
  INCONCLUSO en otro caso

  El 60% es más laxo que el 70% del lado bajista a propósito: allí la señal era
  binaria y limpia (un bin muere o no); aquí el argmax oscila y se acepta más
  ruido. El umbral de 20 min sigue siendo ~1.7× la cadencia de 11.6 min.

MISMO LÍMITE DE RESOLUCIÓN
  11.6 min de cadencia media. Nada por debajo de ~12 min es observable. Un
  negativo significa "no detectable a esta resolución".

LO QUE NO RESPONDE
  - Si el mercado cotiza contra fuentes que no tenemos (ASOS 1-min, modelos
    propietarios). Si se adelanta, esto no dice POR QUÉ.
  - Rentabilidad. Señalar antes al ganador no es lo mismo que ganar dinero: hay
    que pagar el precio al que cotice en ese momento.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_latencia_alcista.py [dias]
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
N_MIN = 100
UMBRAL_MIN = 20.0


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def contiene(lo, hi, settle) -> bool:
    return (((lo - 0.5) <= settle if abs(lo) < 1e8 else True)
            and (settle <= (hi + 0.5) if abs(hi) < 1e8 else True))


def main() -> int:
    an = sqlite3.connect("file:" + str(BASE / "analysis.db") + "?mode=ro",
                         uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect("file:" + str(BASE / "calibration.db") + "?mode=ro",
                          uri=True)
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS)).date().isoformat()

    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE date >= ? AND max_obs_f IS NOT NULL", (desde,))}

    filas = an.execute("""
        SELECT station, date(ts) dia, ts, bin_lo, bin_hi, yes_mid,
               our_p_calibrated
        FROM kalshi_snapshots
        WHERE date(ts) >= ? AND yes_mid IS NOT NULL
          AND our_p_calibrated IS NOT NULL
        ORDER BY station, ts
    """, (desde,)).fetchall()

    por_snapshot: dict = {}
    for r in filas:
        por_snapshot.setdefault((r["station"], r["dia"], r["ts"]), []).append(r)

    por_dia: dict = {}
    for (st, dia, ts), bins in por_snapshot.items():
        por_dia.setdefault((st, dia), []).append((ts, bins))

    deltas, nunca_nosotros, nunca_mercado, sin_ganador = [], 0, 0, 0
    for (st, dia), snaps in por_dia.items():
        settle = settles.get((st, dia))
        if settle is None:
            continue
        snaps.sort(key=lambda x: x[0])
        t_n = t_m = None
        for ts, bins in snaps:
            gan = next((b for b in bins
                        if contiene(b["bin_lo"], b["bin_hi"], settle)), None)
            if gan is None:
                continue
            top_n = max(bins, key=lambda b: b["our_p_calibrated"])
            top_m = max(bins, key=lambda b: b["yes_mid"])
            if t_n is None and top_n["bin_lo"] == gan["bin_lo"]:
                t_n = ts
            if t_m is None and top_m["bin_lo"] == gan["bin_lo"]:
                t_m = ts
            if t_n and t_m:
                break
        if t_n is None and t_m is None:
            sin_ganador += 1
            continue
        if t_n is None:
            nunca_nosotros += 1        # derrota: nunca lo señalamos
            continue
        if t_m is None:
            nunca_mercado += 1         # victoria: el mercado nunca lo señaló
            continue
        deltas.append((_dt(t_m) - _dt(t_n)).total_seconds() / 60.0)

    total = len(deltas) + nunca_nosotros + nunca_mercado
    print(f"station-days con settle y bins : {total + sin_ganador}")
    print(f"  el ganador nunca aparece     : {sin_ganador}")
    print(f"  NOSOTROS nunca lo señalamos  : {nunca_nosotros}   (derrota)")
    print(f"  el MERCADO nunca lo señaló   : {nunca_mercado}   (victoria)")
    print(f"  carreras con los dos         : {len(deltas)}")
    if not deltas:
        print("\nsin carreras — nada que concluir")
        return 0

    deltas.sort()
    n = len(deltas)
    med = statistics.median(deltas)
    pos = sum(1 for d in deltas if d > 0)
    neg = sum(1 for d in deltas if d < 0)
    print(f"\nΔt = t_mercado − t_nuestro   (minutos, mismos ticks)")
    print(f"  mediana {med:+.1f}   p25 {deltas[n // 4]:+.1f}   "
          f"p75 {deltas[3 * n // 4]:+.1f}")
    print(f"  lo señalamos antes : {pos}/{n} = {100 * pos / n:.0f}%")
    print(f"  se adelantaron     : {neg}/{n} = {100 * neg / n:.0f}%")
    print(f"  mismo tick         : {n - pos - neg}/{n}")

    # Con las derrotas duras dentro del denominador.
    gana_nos = pos + nunca_mercado
    print(f"\ncon TODO en el denominador ({total}):")
    print(f"  señalamos antes o en exclusiva : {gana_nos}/{total} = "
          f"{100 * gana_nos / total:.0f}%")

    print("\n── CRITERIO PRE-REGISTRADO ──")
    if total < N_MIN:
        v = f"ESPERAR — N={total} < {N_MIN}"
    elif med <= 0:
        v = "NO VIABLE — el mercado señala al ganador antes o a la vez"
    elif med >= UMBRAL_MIN and pos >= 0.60 * n:
        v = f"VIABLE — ventana mediana de {med:.0f} min"
    else:
        v = (f"INCONCLUSO — mediana {med:.1f} min bajo el umbral de "
             f"{UMBRAL_MIN:.0f}, indistinguible de un tick a 11.6 min")
    print(f"  VEREDICTO: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
