#!/usr/bin/env python3
"""¿Debe `direction_of` clasificar con `our_pred_f` o con `pred_iso_med_f`?

CONTEXTO
--------
`direction_of(side, lo, hi, ref)` etiqueta cold/hot/mid según dónde cae el bin
respecto a `ref`, y esa etiqueta alimenta los gates (bias_blocks_direction,
streak por dirección, ROI per-dir). Hoy `ref = our_pred_f`, que es la mediana
del ensemble CRUDO. Desde el 2026-07-27 existe `pred_iso_med_f`, la mediana de
la distribución ya calibrada (isotónica + blend).

KDCA 2026-07-27 enseñó la discrepancia: our_pred_f 90.6 caía FUERA del bin
89-90 y pred_iso_med_f 89.5 caía DENTRO, o sea las dos referencias clasifican
ese bin de forma distinta.

`pred_iso_med_f` sólo existe desde hoy, así que se RECONSTRUYE retroactivamente
con `agent_signals.implied_median_f` sobre `kalshi_snapshots.our_p_calibrated`,
que sí se persiste desde el 2026-06-29 (440676 filas, 581 station-days).

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: las dos referencias clasifican igual de bien.

VERDAD EX-POST
  `direction_true = direction_of(side, lo, hi, settle)`. Un bin enteramente por
  encima del settle real ERA, en efecto, una apuesta hot; por debajo, cold. Es
  la etiqueta que se habría querido tener en el momento de decidir.

MÉTRICA PRIMARIA
  accuracy = fracción de (bin, side) cuya etiqueta coincide con direction_true.
  Se compara accuracy(our_pred_f) contra accuracy(pred_iso reconstruida).

UNIDAD Y VENTANA
  Un station-day = snapshot más cercano a las 12:00 local (±90 min), igual que
  los otros backtests del día. N se cuenta en station-days, no en
  clasificaciones (un día aporta ~12 y no son independientes).

MÉTRICA SECUNDARIA (explicativa, no decide)
  |ref - settle| mediano de cada referencia. Si una clasifica mejor debería ser
  porque está más cerca del settle.

CRITERIO DE DECISIÓN
  ADOPTAR pred_iso  <=>  accuracy(iso) - accuracy(crudo) >= 5pp  Y  N >= 100
  ZONA GRIS          2pp <= delta < 5pp   -> no actuar
  DESCARTAR          delta < 2pp
  Si el delta es negativo, la respuesta es que `our_pred_f` ya era la correcta.

  Se reporta además en qué fracción de casos las dos referencias dan etiquetas
  distintas: si casi nunca difieren, el cambio es irrelevante aunque gane.

EXCLUSIONES
  - station-days sin settle NWS o sin bins con our_p_calibrated.
  - precios de mercado <=2c o >=98c (colas del calibrador).
  - KIAH y cualquier id fuera del roster.

NOTA: quinto backtest sobre esta base hoy. Resultado positivo = candidato, no
cambio de gate; exige repetirse sobre días frescos antes de tocar el path.

============================ RESULTADO 2026-07-27 ===========================
496 station-days · 3644 clasificaciones (bin × side)

  accuracy contra la etiqueta ex-post del settle
    our_pred_f     (ensemble crudo)   62.29%
    pred_iso_med_f (calibrada)        63.56%
    delta                             +1.26 pp   <- por debajo del corte de 2pp
    difieren en                       15.5% de los casos

  |ref - settle| mediano: crudo 1.80°F · iso 1.63°F

VEREDICTO: DESCARTAR. `pred_iso` queda algo más cerca del settle y clasifica
marginalmente mejor, pero el delta no llega al umbral y sólo cambiaría el 15.5%
de las clasificaciones. **`direction_of` se queda con `our_pred_f`.**

Dato de contexto que conviene recordar: ambas referencias aciertan la dirección
en ~62-63% de los casos. Clasificar un bin como hot/cold respecto a un settle
que aún no ha ocurrido es difícil, y ninguna de las dos lo resuelve.
=============================================================================
"""
from __future__ import annotations

import sqlite3
import statistics
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


class Bin:
    __slots__ = ("bin_lo", "bin_hi")

    def __init__(self, lo, hi):
        self.bin_lo, self.bin_hi = lo, hi


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(r[0], r[1]): r[2] for r in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes")}

    ok_crudo = ok_iso = tot = 0
    n_days = 0
    difieren = 0
    err_crudo: list[float] = []
    err_iso: list[float] = []

    desde = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("--desde="):
        desde = sys.argv[1].split("=", 1)[1]
        print(f"[réplica out-of-sample: sólo station-days >= {desde}]\n")
    for (st, day), settle in settles.items():
        if st not in STATION_TZ or settle is None:
            continue
        if desde and day < desde:
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
            """SELECT ts, our_pred_f FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND our_pred_f IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
             noon.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if snap is None:
            continue
        rows = an.execute(
            """SELECT bin_lo, bin_hi, yes_mid, our_p_calibrated
               FROM kalshi_snapshots
               WHERE station=? AND ts=(SELECT ts FROM kalshi_snapshots
                                       WHERE station=? AND ts<=?
                                       ORDER BY ts DESC LIMIT 1)""",
            (st, st, snap["ts"])).fetchall()
        rows = [r for r in rows if r["our_p_calibrated"] is not None]
        if not rows:
            continue
        bins = [Bin(r["bin_lo"], r["bin_hi"]) for r in rows]
        iso = A.implied_median_f(bins, [r["our_p_calibrated"] for r in rows])
        if iso is None:
            continue
        crudo = snap["our_pred_f"]
        n_days += 1
        err_crudo.append(abs(crudo - settle))
        err_iso.append(abs(iso - settle))
        for r, b in zip(rows, bins):
            p = r["yes_mid"]
            if p is None or p <= 0.02 or p >= 0.98:
                continue
            for side in ("YES", "NO"):
                truth = A.direction_of(side, b.bin_lo, b.bin_hi, settle)
                dc = A.direction_of(side, b.bin_lo, b.bin_hi, crudo)
                di = A.direction_of(side, b.bin_lo, b.bin_hi, iso)
                tot += 1
                ok_crudo += (dc == truth)
                ok_iso += (di == truth)
                difieren += (dc != di)

    if not tot:
        print("sin datos")
        return 1
    ac, ai = 100 * ok_crudo / tot, 100 * ok_iso / tot
    print(f"station-days: {n_days}   clasificaciones (bin x side): {tot}")
    print(f"rango: reconstruido desde kalshi_snapshots.our_p_calibrated\n")
    print("PRIMARIO — accuracy contra la etiqueta que da el settle real")
    print(f"  our_pred_f     (ensemble crudo)   {ac:6.2f}%")
    print(f"  pred_iso_med_f (calibrada)        {ai:6.2f}%")
    print(f"  delta                             {ai - ac:+6.2f} pp")
    print(f"  las dos referencias difieren en   {100*difieren/tot:5.1f}% de los casos")

    print("\nSECUNDARIO — distancia al settle (explica el primario)")
    print(f"  |our_pred_f - settle| mediano     {statistics.median(err_crudo):5.2f}°F")
    print(f"  |pred_iso   - settle| mediano     {statistics.median(err_iso):5.2f}°F")

    delta = ai - ac
    if n_days < 100:
        v = f"N insuficiente ({n_days} station-days)"
    elif delta >= 5:
        v = "ADOPTAR pred_iso_med_f (candidata: repetir con días frescos)"
    elif delta >= 2:
        v = "ZONA GRIS — no actuar"
    elif delta > -2:
        v = "DESCARTAR — las dos clasifican igual"
    else:
        v = "our_pred_f ES MEJOR — no cambiar"
    print(f"\nVEREDICTO: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
