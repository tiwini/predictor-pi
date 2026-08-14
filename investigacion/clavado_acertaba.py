#!/usr/bin/env python3
"""Cuando la predicción queda clavada en el piso, ¿acierta o pierde?

Contexto: el corrector de nivel disparó la clavada prematura de KLAX del 0.1% al
8.4% (`clavado_en_el_piso.py`). Antes de ponerle un guard hay que saber si eso
es un problema o simplemente el clamp haciendo su trabajo: el piso es
información dura, y quedarse en él puede ser lo correcto.

Se compara, SÓLO en los snapshots clavados:

  publicado    = `ens_med` tal cual salió (clavado en el piso)
  sin_corrector= `ens_med + bias_aplicado` (deshace el corrector)
  piso         = el propio piso, para ver cuánto subió el día después

La pregunta operativa: en esas horas, ¿habría sido mejor no corregir?
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ, PEAK_HOURS   # noqa: E402

TOL = 0.15
CON_CORRECTOR = {"KLAX", "KSFO"}


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {(s, d): m for s, d, m in cal.execute(
        "SELECT station_id, date, max_obs_f FROM day_outcomes "
        "WHERE max_obs_f IS NOT NULL")}

    grupos = defaultdict(list)
    q = """SELECT station, ts, ens_med, today_max_obs, today_max_cli, current_f,
                  bias_f, bias_applied, bias_path
           FROM station_snapshots WHERE ens_med IS NOT NULL AND ts >= ?"""
    for r in an.execute(q, ("2026-08-05",)):
        st = r["station"]
        if st not in STATION_TZ:
            continue
        cands = [x for x in (r["today_max_obs"], r["today_max_cli"])
                 if x is not None and x > -900]
        if r["current_f"] is not None and r["current_f"] > -900:
            cands.append(r["current_f"] - 0.9)
        if not cands:
            continue
        piso = max(cands)
        if abs(r["ens_med"] - piso) > TOL:
            continue                       # sólo los clavados
        local = datetime.fromisoformat(r["ts"]).astimezone(ZoneInfo(STATION_TZ[st]))
        s = settles.get((st, local.date().isoformat()))
        if s is None:
            continue
        bias = r["bias_f"] if (r["bias_applied"] and r["bias_f"] is not None) else 0.0
        prematuro = (local.hour + local.minute / 60) < PEAK_HOURS[st][1]
        grupos[(st, prematuro)].append({
            "pub": r["ens_med"], "sin": r["ens_med"] + bias,
            "piso": piso, "settle": s, "bias": bias})

    print("Snapshots con la predicción CLAVADA en el piso (desde 2026-08-05)\n")
    print(f"  {'st':6s} {'cuándo':>10s} {'N':>5s} {'|err| pub':>10s} "
          f"{'|err| sin corr':>15s} {'mejor':>10s} {'subió tras clavar':>18s}")
    for (st, prem) in sorted(grupos):
        g = grupos[(st, prem)]
        if len(g) < 15:
            continue
        e_pub = [abs(x["pub"] - x["settle"]) for x in g]
        e_sin = [abs(x["sin"] - x["settle"]) for x in g]
        subio = [x["settle"] - x["piso"] for x in g]
        mejor = ("publicado" if statistics.median(e_pub) < statistics.median(e_sin)
                 else "sin corr" if statistics.median(e_sin) < statistics.median(e_pub)
                 else "=")
        cuando = "en ventana" if prem else "tras pico"
        marca = " ←corr" if st in CON_CORRECTOR else ""
        print(f"  {st:6s} {cuando:>10s} {len(g):5d} {statistics.median(e_pub):9.2f}° "
              f"{statistics.median(e_sin):14.2f}° {mejor:>10s} "
              f"{statistics.median(subio):+17.2f}°{marca}")

    print("\n\nSólo las corregidas, clavadas DENTRO de la ventana de pico:")
    print("(es el caso que motivó la pregunta — afirmar que no sube más)")
    for st in sorted(CON_CORRECTOR):
        g = grupos.get((st, True), [])
        if not g:
            print(f"  {st}: sin casos")
            continue
        e_pub = [abs(x["pub"] - x["settle"]) for x in g]
        e_sin = [abs(x["sin"] - x["settle"]) for x in g]
        gana_pub = sum(1 for a, b in zip(e_pub, e_sin) if a < b - 0.05)
        gana_sin = sum(1 for a, b in zip(e_pub, e_sin) if b < a - 0.05)
        subio = [x["settle"] - x["piso"] for x in g]
        print(f"\n  {st}  N={len(g)}")
        print(f"    |err| publicado (clavado) : {statistics.median(e_pub):.2f}°")
        print(f"    |err| sin corrector       : {statistics.median(e_sin):.2f}°")
        print(f"    gana el publicado en {gana_pub}/{len(g)} · gana sin corrector en {gana_sin}/{len(g)}")
        print(f"    el día subió tras clavar  : mediana {statistics.median(subio):+.2f}°  "
              f"(p90 {sorted(subio)[int(.9*len(subio))]:+.2f})")
        print(f"    corrección media aplicada : {statistics.median(x['bias'] for x in g):+.2f}°")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
