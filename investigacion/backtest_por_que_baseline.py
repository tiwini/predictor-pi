#!/usr/bin/env python3
"""¿Por qué un baseline de una línea le gana al ensemble?

CONTEXTO
--------
`backtest_subida_restante.py` (N=511, snapshot 2h antes de la ventana de pico)
midió |error| mediano 1.93°F para el ensemble contra 1.60°F para
`current + mediana_LOO(subida restante)`. El baseline gana en 10 de 19
estaciones, y los sesgos por estación van de +4.08 (KSFO) a −2.30 (KMDW), o sea
se cancelan en la mediana global (+0.74).

Dos explicaciones llevan a acciones OPUESTAS:
  (A) NIVEL   — el ensemble discrimina bien pero está descentrado por estación.
                El baseline gana porque la mediana LOO absorbe ese offset.
                Acción: corregir el nivel (que es lo que el bias tracker
                intenta hacer, y hace mal — su path EWMA acierta el 49.7%).
  (B) SEÑAL   — el ensemble discrimina peor, y centrarlo no lo arregla.
                Acción: el problema está aguas arriba (reweight, miembros), y
                corregir el nivel no serviría de nada.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

PARTE A — ¿nivel o señal?
  e_ens          = ens_med - settle
  e_ens_centrado = e_ens - mediana_LOO(e_ens de esa estación)
                   (leave-one-out: excluye el día que se evalúa, igual que el
                    baseline, para que la comparación sea justa)
  e_base         = (current + mediana_LOO(subida)) - settle

  Se comparan |e_ens|, |e_ens_centrado| y |e_base|, medianas globales.

  VEREDICTO A
    NIVEL  si |e_ens_centrado| <= |e_base| - 0.15   (centrado, el ensemble gana
                                                     o iguala -> el problema era
                                                     el offset)
    SEÑAL  si |e_ens_centrado| >= |e_base| + 0.15   (ni centrado alcanza)
    MIXTO  en otro caso

PARTE B — ¿los sesgos por estación son ESTABLES?
  Es la pregunta que decide si corregir el nivel puede funcionar. Se parte cada
  estación en dos mitades temporales y se compara el sesgo mediano de cada una.

  VEREDICTO B
    ESTABLES     si Spearman(sesgo_1a_mitad, sesgo_2a_mitad) > 0.5 sobre >= 15
                 estaciones Y el signo coincide en >= 70% de ellas
    INESTABLES   si rho <= 0.2 o el signo coincide en <= 50%
    INTERMEDIO   en otro caso

  Si salen ESTABLES y la parte A dice NIVEL, entonces existe una corrección por
  estación que funcionaría, y el bias tracker actual no la está capturando.
  Si salen INESTABLES, corregir el nivel es perseguir ruido — y eso explicaría
  por qué el path EWMA del bias está en 49.7%.

NOTA: el baseline LOO absorbe el sesgo por construcción, así que la comparación
justa exige centrar también al ensemble con LOO. Compararlo sin centrar sería
darle la ventaja al baseline gratis; ése era justamente el resultado anterior.

Décimo backtest sobre esta base. No se toca nada del pipeline con esto: es
diagnóstico, y cualquier acción iría en su propio pre-registro.
=============================================================================
"""
from __future__ import annotations

import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ, PEAK_HOURS   # noqa: E402

UTC = ZoneInfo("UTC")
TOL_MIN = 30
HOURS_BEFORE_PEAK = 2
MIN_DAYS = 10


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return float("nan")

    def rk(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for pos, i in enumerate(order):
            r[i] = pos + 1.0
        return r
    rx, ry = rk(xs), rk(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else float("nan")


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    per: dict[str, list] = {}
    for (st, day), settle in settles.items():
        if st not in STATION_TZ or settle is None:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        ref = (datetime.combine(d, datetime.min.time(), tz)
               + timedelta(hours=PEAK_HOURS[st][0] - HOURS_BEFORE_PEAK))
        lo = (ref - timedelta(minutes=TOL_MIN)).astimezone(UTC)
        hi = (ref + timedelta(minutes=TOL_MIN)).astimezone(UTC)
        r = an.execute(
            """SELECT current_f, ens_med FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=?
                 AND current_f IS NOT NULL AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             ref.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is None:
            continue
        per.setdefault(st, []).append(
            {"day": day, "settle": settle, "cur": r["current_f"],
             "ens": r["ens_med"]})

    ae, aec, ab = [], [], []
    for st, days in per.items():
        if len(days) < MIN_DAYS:
            continue
        days.sort(key=lambda x: x["day"])
        for i, x in enumerate(days):
            otros = [j for j in range(len(days)) if j != i]
            e_ens = x["ens"] - x["settle"]
            sesgo_loo = statistics.median(
                days[j]["ens"] - days[j]["settle"] for j in otros)
            subida_loo = statistics.median(
                days[j]["settle"] - days[j]["cur"] for j in otros)
            ae.append(abs(e_ens))
            aec.append(abs(e_ens - sesgo_loo))
            ab.append(abs(x["cur"] + subida_loo - x["settle"]))

    m_ae, m_aec, m_ab = (statistics.median(ae), statistics.median(aec),
                         statistics.median(ab))
    print(f"station-days: {len(ae)}\n")
    print("PARTE A — ¿nivel o señal?")
    print(f"  |error| ensemble             {m_ae:5.2f}°F")
    print(f"  |error| ensemble CENTRADO    {m_aec:5.2f}°F   (menos su sesgo LOO)")
    print(f"  |error| baseline             {m_ab:5.2f}°F")
    if m_aec <= m_ab - 0.15:
        va = "NIVEL — el ensemble discrimina mejor; le sobra offset"
    elif m_aec >= m_ab + 0.15:
        va = "SEÑAL — ni centrado alcanza al baseline"
    else:
        va = "MIXTO"
    print(f"  -> {va}")

    print("\nPARTE B — ¿son estables los sesgos por estación?")
    s1, s2, sts = [], [], []
    for st, days in sorted(per.items()):
        if len(days) < MIN_DAYS * 2:
            continue
        days.sort(key=lambda x: x["day"])
        h = len(days) // 2
        a = statistics.median(x["ens"] - x["settle"] for x in days[:h])
        b = statistics.median(x["ens"] - x["settle"] for x in days[h:])
        s1.append(a)
        s2.append(b)
        sts.append(st)
        print(f"  {st:6s} 1a mitad {a:+6.2f}   2a mitad {b:+6.2f}   "
              f"{'mismo signo' if a * b > 0 else 'CAMBIA de signo'}")
    if len(s1) >= 5:
        rho = spearman(s1, s2)
        same = sum(1 for a, b in zip(s1, s2) if a * b > 0)
        pct = 100 * same / len(s1)
        print(f"\n  Spearman(1a, 2a) = {rho:+.2f}   signo coincide en "
              f"{same}/{len(s1)} ({pct:.0f}%)")
        if rho > 0.5 and pct >= 70 and len(s1) >= 15:
            vb = "ESTABLES — existe una corrección por estación que funcionaría"
        elif rho <= 0.2 or pct <= 50:
            vb = "INESTABLES — corregir el nivel es perseguir ruido"
        else:
            vb = "INTERMEDIO"
        print(f"  -> {vb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
