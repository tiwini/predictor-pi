#!/usr/bin/env python3
"""Seguimiento en vivo del corrector de nivel: ¿sigue ayudando, o sobre-corrige?

Para cada estación con el corrector activo, compara día a día contra el settle:

    publicado    = ens_med tal como salió (con corrector, ya capeado por el piso)
    sin_corr     = ens_med + bias_f aplicado   (lo que habría salido sin él)
    mercado      = centro del bin más caro de Kalshi a esa hora

Sólo mira días **posteriores** a que la estación se habilitara, que se detecta
del primer `bias_path='median_causal'` en `station_snapshots` — no se hardcodea.

============================ CRITERIO DE VIGILANCIA ==========================
Escrito el 2026-08-17, cuando N=1 y no se sabe nada todavía.

El riesgo que se vigila NO es que el corrector no ayude —eso ya lo midió el
backtest— sino que **sobre-corrija**: que al centrar el error lo pase al otro
lado. La señal es el signo, no la magnitud.

  🔴 REVERTIR la estación si, con N>=10 días:
       (a) el error publicado es NEGATIVO en >=7 de los últimos 10, Y
       (b) |error| medio publicado >= |error| medio sin corrector
     Las dos: sub-predecir sistemáticamente no es problema si aun así queda
     más cerca que no corregir.

  🟡 REVISAR a mano si el signo se vuelca (>=7 de 10 negativos) pero el
     |error| sigue siendo mejor. Es corrección excesiva que todavía compensa;
     la salida probablemente sea recortar la mediana, no apagarla.

  🟢 SEGUIR si el |error| publicado es menor y los signos están repartidos.

N<10 no decide nada, se imprime y ya. La hora de referencia es la de decisión
(mediodía local), no la de la ventana de pico: dentro de la ventana el piso de
observación domina y ambas ramas quedan clavadas, así que ahí no se distingue.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/seguimiento_corrector.py [hora_local]
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
from stations import STATION_TZ   # noqa: E402
import level_corrector as lc      # noqa: E402

UTC = ZoneInfo("UTC")
HORA = int(sys.argv[1]) if len(sys.argv) > 1 else 12
VENTANA_MIN = 45


def primer_dia_activo(an, st: str) -> str | None:
    r = an.execute(
        """SELECT MIN(date(datetime(ts, 'localtime'))) FROM station_snapshots
           WHERE station=? AND bias_path='median_causal' AND bias_applied=1""",
        (st,)).fetchone()
    return r[0] if r and r[0] else None


def fila_del_dia(an, cal, st: str, dia: str) -> dict | None:
    tz = ZoneInfo(STATION_TZ[st])
    d = datetime.strptime(dia, "%Y-%m-%d").date()
    ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=HORA)
    lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
    hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
    r = an.execute(
        """SELECT ts, ens_med, bias_f, bias_applied FROM station_snapshots
           WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
           ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
        (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
         ref.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
    if r is None:
        return None
    settle = cal.execute(
        "SELECT max_obs_f FROM day_outcomes WHERE station_id=? AND date=?",
        (st, dia)).fetchone()
    if settle is None or settle[0] is None:
        return None

    b = r["bias_f"] if (r["bias_applied"] and r["bias_f"] is not None) else 0.0
    bins = an.execute(
        """SELECT bin_lo, bin_hi, yes_mid FROM kalshi_snapshots
           WHERE station=? AND ts=(SELECT ts FROM kalshi_snapshots
                                   WHERE station=? AND ts<=?
                                   ORDER BY ts DESC LIMIT 1)
             AND yes_mid IS NOT NULL""", (st, st, r["ts"])).fetchall()
    mk = None
    if bins:
        top = max(bins, key=lambda x: x["yes_mid"])
        blo, bhi = top["bin_lo"], top["bin_hi"]
        mk = ((blo + bhi) / 2 if abs(blo) < 1e8 and abs(bhi) < 1e8
              else (bhi if abs(blo) > 1e8 else blo))
    return {"dia": dia, "settle": settle[0], "pub": r["ens_med"],
            "sin": r["ens_med"] + b, "corr": b, "mk": mk}


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    print(f"Corrector de nivel — seguimiento a las {HORA}h local "
          f"(hora de decisión)\n")
    for st in sorted(lc.ENABLED_STATIONS):
        desde = primer_dia_activo(an, st)
        if desde is None:
            print(f"{st}: nunca aplicó el corrector todavía\n")
            continue
        dias = [r[0] for r in cal.execute(
            "SELECT date FROM day_outcomes WHERE station_id=? AND date>=? "
            "ORDER BY date", (st, desde)).fetchall()]
        filas = [f for f in (fila_del_dia(an, cal, st, d) for d in dias) if f]
        if not filas:
            print(f"{st}: activo desde {desde}, aún sin días con settle\n")
            continue

        e_pub = [f["pub"] - f["settle"] for f in filas]
        e_sin = [f["sin"] - f["settle"] for f in filas]
        neg = sum(1 for e in e_pub[-10:] if e < 0)
        n_rec = len(e_pub[-10:])
        m_pub = statistics.mean([abs(e) for e in e_pub])
        m_sin = statistics.mean([abs(e) for e in e_sin])

        print(f"── {st}  (corrector activo desde {desde}, N={len(filas)})")
        print(f"   {'día':12s} {'settle':>7s} {'pub':>7s} {'sin':>7s} "
              f"{'corr':>6s} {'Δpub':>6s} {'Δsin':>6s} {'Δmk':>6s}")
        for f in filas:
            dmk = f"{f['mk'] - f['settle']:+6.1f}" if f["mk"] is not None else "     —"
            print(f"   {f['dia']:12s} {f['settle']:7.1f} {f['pub']:7.1f} "
                  f"{f['sin']:7.1f} {f['corr']:+6.2f} "
                  f"{f['pub'] - f['settle']:+6.1f} {f['sin'] - f['settle']:+6.1f} {dmk}")
        print(f"   |error| medio   publicado {m_pub:.2f}   sin corrector {m_sin:.2f}"
              f"   ({m_pub - m_sin:+.2f})")
        print(f"   signo: {neg} de los últimos {n_rec} negativos")

        if len(filas) < 10:
            v = f"N={len(filas)} — no decide, faltan {10 - len(filas)} días"
        elif neg >= 7 and m_pub >= m_sin:
            v = "🔴 REVERTIR — sobre-corrige y ya no compensa"
        elif neg >= 7:
            v = "🟡 REVISAR — vuelca el signo pero aún queda más cerca"
        else:
            v = "🟢 SEGUIR"
        print(f"   {v}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
