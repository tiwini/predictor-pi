#!/usr/bin/env python3
"""¿El colapso del reweight degrada las PROBABILIDADES, aunque no la mediana?

CONTEXTO
--------
El backtest de componentes de difficulty (2026-07-27, N=505) midió
Spearman(eff_n, |ens_med - settle|) = +0.070 y concluyó que `eff_n` no sirve
como gate. Pero midió el error de la **predicción puntual**, y eso no es lo que
se apuesta: se apuesta por bins, o sea por PROBABILIDADES.

KMDW 2026-07-27 enseñó la diferencia: eff_N = 1.0/31, distribución colapsada a
un punto exacto (p10 = p90 = 88.0), y sin embargo la mediana era correcta
(coincidía con max_obs). Lo que estaba roto no era el centro, era la forma —
y de ahí salió un `our_p` de 0.970 para "89 or below" que la isotónica tuvo que
aplastar a 0.433, generando un edge falso de 50pp contra un mercado que
coincidía con nuestro modelo crudo.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: el Brier de nuestras probabilidades por bin no depende de eff_N.

MEDIDA DE eff_N
  Reconstruida de `ens_maxes_json` como diversidad efectiva de la distribución:
    eff_N_dist = (sum n_v)^2 / sum(n_v^2)   sobre los valores únicos v
  Verificada contra el eff_N que difficulty escribe en reasons: coincide exacto
  en KMDW (1.0) y subestima algo en KPHX (6.8 vs 9.0), porque dos miembros con
  el mismo dmax cuentan como uno. Es la medida CORRECTA para esta pregunta:
  lo que genera `our_p_for_bin` es la distribución de valores, no los pesos por
  miembro. Se usa la misma definición en todas las bandas, así que el orden se
  mantiene.

UNIVERSO
  Una oportunidad por (estación, día, bin), en el snapshot más cercano a las
  12:00 local (±90 min), igual que el resto de backtests del día.
  outcome = 1 si el settle NWS cae dentro del bin.

MÉTRICAS
  brier_us     = (our_p_calibrated - outcome)^2
  brier_kalshi = (yes_mid - outcome)^2          <- CONTROL INDEPENDIENTE
  Se reportan por banda de eff_N_dist: [1,3) [3,6) [6,12) [12,inf)

  El control es lo que da sentido al test: si Kalshi TAMBIÉN empeora en las
  bandas bajas, esos días son difíciles para todos y eff_N no es culpable. Si
  sólo empeoramos nosotros, el colapso es nuestro problema.
  (Nota: a diferencia del "control" mid-NO del backtest anterior, que era el
  complemento aritmético de la misma apuesta y no controlaba nada, Kalshi es
  un estimador genuinamente independiente.)

CRITERIO DE DECISIÓN
  eff_N DEGRADA las probabilidades si, con N >= 500 bins por banda:
    brier_us empeora de forma monótona al bajar eff_N
    Y brier_us[1,3) es >= 30% peor que brier_us[12,inf)
    Y ese deterioro NO se explica por el control (el de Kalshi crece menos de
      la mitad en términos relativos)
  Si Kalshi empeora en proporción parecida -> son días difíciles, no eff_N.
  Si no hay monotonía -> no actuar.

  Un resultado positivo NO reabre `difficulty` como gate de la predicción: la
  conclusión del backtest de componentes (eff_n no predice |error| puntual)
  sigue en pie. Sería una señal distinta, sobre la calidad probabilística, y
  aplicable sólo al path de bets.

NOTA: `our_p_calibrated` no está normalizada (suma ~1.15). Se mide tal cual la
produce el sistema, porque es tal cual como se usa para calcular edges.

Séptimo backtest sobre esta base hoy. Positivo = candidato, no cambio de gate.
=============================================================================
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ    # noqa: E402

UTC = ZoneInfo("UTC")
TOL_MIN = 90
BANDS = [(1, 3), (3, 6), (6, 12), (12, 10**9)]


def eff_n_dist(js: str | None) -> float | None:
    if not js:
        return None
    try:
        vals = json.loads(js)
    except Exception:
        return None
    if not vals:
        return None
    cnt = Counter(vals)
    ss = sum(n * n for n in cnt.values())
    return (len(vals) ** 2) / ss if ss else None


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

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
            """SELECT ts, ens_maxes_json FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_maxes_json IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             noon.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if snap is None:
            continue
        eff = eff_n_dist(snap["ens_maxes_json"])
        if eff is None:
            continue
        band = next((b for b in BANDS if b[0] <= eff < b[1]), None)
        if band is None:
            continue
        bins = an.execute(
            """SELECT bin_lo, bin_hi, yes_mid, our_p_calibrated
               FROM kalshi_snapshots
               WHERE station=? AND ts=(SELECT ts FROM kalshi_snapshots
                                       WHERE station=? AND ts<=?
                                       ORDER BY ts DESC LIMIT 1)""",
            (st, st, snap["ts"])).fetchall()
        used = False
        for b in bins:
            p, m = b["our_p_calibrated"], b["yes_mid"]
            if p is None or m is None:
                continue
            outcome = 1.0 if b["bin_lo"] <= settle <= b["bin_hi"] else 0.0
            buckets.setdefault(band, []).append(
                ((p - outcome) ** 2, (m - outcome) ** 2))
            used = True
        n_days += used

    print(f"station-days: {n_days}\n")
    print(f"{'eff_N_dist':12s} {'N bins':>7s} {'brier_us':>10s} "
          f"{'brier_kalshi':>13s} {'ratio us/kalshi':>16s}")
    res = {}
    for band in BANDS:
        v = buckets.get(band)
        if not v:
            continue
        bu = statistics.mean(x[0] for x in v)
        bk = statistics.mean(x[1] for x in v)
        res[band] = (bu, bk, len(v))
        lab = f"[{band[0]}, {band[1]})" if band[1] < 10**8 else f"[{band[0]}, inf)"
        print(f"{lab:12s} {len(v):7d} {bu:10.4f} {bk:13.4f} {bu / bk:15.2f}x")

    print("\nVEREDICTO segun el criterio pre-registrado")
    lo_b, hi_b = BANDS[0], BANDS[-1]
    if lo_b not in res or hi_b not in res:
        print("  faltan bandas")
        return 0
    bu_lo, bk_lo, n_lo = res[lo_b]
    bu_hi, bk_hi, n_hi = res[hi_b]
    us_worse = 100 * (bu_lo - bu_hi) / bu_hi
    ka_worse = 100 * (bk_lo - bk_hi) / bk_hi
    ordered = [res[b][0] for b in BANDS if b in res]
    monot = all(ordered[i] >= ordered[i + 1] for i in range(len(ordered) - 1))
    print(f"  brier_us   banda baja {bu_lo:.4f} vs alta {bu_hi:.4f}  "
          f"-> {us_worse:+.0f}% peor")
    print(f"  brier_kalshi mismo corte                      -> {ka_worse:+.0f}% peor")
    print(f"  monotonía al bajar eff_N: {'sí' if monot else 'NO'}")
    if min(n_lo, n_hi) < 500:
        v = f"N insuficiente ({n_lo}/{n_hi})"
    elif not monot:
        v = "sin monotonía — no actuar"
    elif us_worse >= 30 and ka_worse < us_worse / 2:
        v = "eff_N DEGRADA las probabilidades (candidato, sólo path de bets)"
    elif ka_worse >= us_worse / 2:
        v = "son días difíciles para todos — eff_N no es la causa"
    else:
        v = "deterioro por debajo del umbral — no actuar"
    print(f"  -> {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
