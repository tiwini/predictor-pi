#!/usr/bin/env python3
"""¿Los bins 'mid' con el modelo caliente son una trampa? — y si lo son, ¿sólo en YES?

CONTEXTO
--------
`direction_of` clasifica 'mid' al bin que CONTIENE our_pred_f, y
`bias_blocks_direction` devuelve False para 'mid' por diseño: el gate de
ext_diff protege hot y cold, y deja sin cubrir justo el bin de nuestra
predicción. El 2026-07-27 aparecieron tres casos simultáneos de mid-YES con el
modelo caliente (KOKC +3.9, KATL +1.8, KNYC +1.8).

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: el ROI de los bins mid no depende de ext_diff, ni en YES ni en NO.

HIPÓTESIS DIRECCIONAL (falsable, y es el punto del ejercicio)
  Si el hueco de 'mid' es realmente asimétrico como se argumentó:
    mid-YES con ext_diff alto  -> PEOR  (apuesta a acertar donde el sesgo dice
                                        que te pasas)
    mid-NO  con ext_diff alto  -> MEJOR (apuesta contra tu propia predicción
                                        inflada; es defensivo)
  Si AMBOS salen mal, la lectura de asimetría era incorrecta y el problema no
  es la dirección sino el bin mid en sí.
  Si NINGUNO se mueve con ext_diff, no hay nada que arreglar.

UNIVERSO
  Contrafactual sobre kalshi_snapshots, no sobre bets reales: las bets
  ejecutadas post-fix son 18 en todo el roster y no dan N. Cada oportunidad =
  (estación, día, bin) tomada UNA vez, en el snapshot más cercano a las 12:00
  local (antes de la ventana de pico más temprana del roster, así la decisión
  es de predicción y no de observación consumada). Tolerancia ±90 min.

  direction = agent_signals.direction_of(side, bin_lo, bin_hi, our_pred_f)
  gana YES  <=> settle dentro de [bin_lo, bin_hi]
  gana NO   <=> settle fuera

MÉTRICA PRIMARIA
  ROI simulado por unidad arriesgada. Comprar YES a precio p paga 1 si acierta:
    pnl = (1 - p) si gana, -p si pierde        -> ROI = sum(pnl) / N
  Para NO el precio es (1 - p) y la condición se invierte.
  El ROI así definido ya descuenta el precio: mide si HAY edge, no si acertamos.

CRITERIO DE DECISIÓN
  Se considera evidencia de que el gate debe cubrir mid-YES si:
    ROI(mid-YES, ext_diff >= +1.5)  es al menos 20 puntos PEOR que
    ROI(mid-YES, ext_diff <  +1.5)   Y ambos grupos tienen N >= 100
  Y ADEMÁS el control no contradice: ROI(mid-NO, ext_diff >= +1.5) no debe ser
  también 20 puntos peor que su propio control (si lo fuera, el problema es el
  bin mid entero y no la dirección, y la conclusión cambia).

  Diferencias por debajo de 10 puntos se tratan como ruido: no actuar.

EXCLUSIONES
  - bins sin yes_mid o sin our_p_calibrated.
  - station-days sin settle NWS.
  - precios <= 0.02 o >= 0.98 (colas donde el edge es el piso del calibrador).
  - KIAH y cualquier id fuera del roster actual.

NOTA DE HONESTIDAD: es el cuarto backtest sobre esta base de datos hoy. Si el
resultado sale positivo, es candidato — no cambio de gate — y exige repetirse
sobre días frescos antes de tocar bets.py.
=============================================================================
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
import agent_signals as A          # noqa: E402
from stations import STATION_TZ    # noqa: E402

UTC = ZoneInfo("UTC")
TOL_MIN = 90
EXT_HI = 1.5


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    # (direction, side, banda) -> [pnl...]
    buckets: dict[tuple, list] = {}
    n_days = 0
    for (st, day), settle in settles.items():
        if st not in STATION_TZ or settle is None:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        noon = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=12)
        lo = (noon - timedelta(minutes=TOL_MIN)).astimezone(UTC)
        hi = (noon + timedelta(minutes=TOL_MIN)).astimezone(UTC)
        snap = an.execute(
            """SELECT ts, our_pred_f, ext_diff_f FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND our_pred_f IS NOT NULL
                 AND ext_diff_f IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             noon.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if snap is None:
            continue
        bins = an.execute(
            """SELECT bin_lo, bin_hi, yes_mid FROM kalshi_snapshots
               WHERE station=? AND ts=(SELECT ts FROM kalshi_snapshots
                                       WHERE station=? AND ts<=?
                                       ORDER BY ts DESC LIMIT 1)""",
            (st, st, snap["ts"])).fetchall()
        if not bins:
            continue
        n_days += 1
        band = "ext>=1.5" if snap["ext_diff_f"] >= EXT_HI else "ext<1.5"
        for b in bins:
            p = b["yes_mid"]
            if p is None or p <= 0.02 or p >= 0.98:
                continue
            inside = b["bin_lo"] <= settle <= b["bin_hi"]
            for side in ("YES", "NO"):
                dirn = A.direction_of(side, b["bin_lo"], b["bin_hi"],
                                      snap["our_pred_f"])
                price = p if side == "YES" else 1.0 - p
                if price <= 0.02 or price >= 0.98:
                    continue
                won = inside if side == "YES" else not inside
                pnl = (1.0 - price) if won else -price
                buckets.setdefault((dirn, side, band), []).append(pnl)

    print(f"station-days usados: {n_days}\n")
    print(f"{'dirección':10s} {'side':5s} {'banda':10s} {'N':>6s} "
          f"{'ROI':>8s} {'win%':>7s}")
    res: dict[tuple, float] = {}
    for key in sorted(buckets):
        v = buckets[key]
        roi = 100 * sum(v) / len(v)
        wins = 100 * sum(1 for x in v if x > 0) / len(v)
        res[key] = roi
        print(f"{key[0]:10s} {key[1]:5s} {key[2]:10s} {len(v):6d} "
              f"{roi:+7.1f}% {wins:6.1f}%")

    print("\nVEREDICTO segun el criterio pre-registrado")
    for side in ("YES", "NO"):
        hi_k, lo_k = ("mid", side, "ext>=1.5"), ("mid", side, "ext<1.5")
        if hi_k not in res or lo_k not in res:
            print(f"  mid-{side}: faltan datos")
            continue
        n_hi, n_lo = len(buckets[hi_k]), len(buckets[lo_k])
        delta = res[hi_k] - res[lo_k]
        if n_hi < 100 or n_lo < 100:
            v = f"N insuficiente ({n_hi}/{n_lo})"
        elif delta <= -20:
            v = "PEOR de forma material"
        elif delta >= 20:
            v = "MEJOR de forma material"
        elif abs(delta) < 10:
            v = "ruido — no actuar"
        else:
            v = "zona intermedia — no actuar"
        print(f"  mid-{side}: ROI {res[lo_k]:+.1f}% -> {res[hi_k]:+.1f}% "
              f"(delta {delta:+.1f}pp, N={n_hi}/{n_lo})  {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
