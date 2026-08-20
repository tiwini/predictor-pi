#!/usr/bin/env python3
"""¿Debe el grupo ASOS de 6h entrar en el piso de observación?

CONTEXTO
--------
KMIA el 2026-08-19 a las 14:22 EDT:

    grupo ASOS 6h    96.08°F   (METAR de 13:53, ventana 07:53→13:53)
    max_obs (feed)   95.00°F
    piso efectivo    95.00°F   ← ignora el ASOS
    ens_med          95.00°F   ← la predicción ES el piso

O sea: el ASOS de 1 minuto —**la misma fuente con la que el NWS liquida**— decía
que el día ya había tocado 96.08, y predecíamos 95.0. Error garantizado de 1.1°F
como mínimo.

`build_snapshot` construye el piso con `max(max_obs, CLI, actual − 0.9)`.
`today_max_asos_6h` se calcula y se guarda en el Snapshot, pero **nunca entra en
el piso**.

POR QUÉ LA EXCLUSIÓN ES DEFENDIBLE, Y POR QUÉ ES DEMASIADO GRUESA
-----------------------------------------------------------------
El grupo de 6h se acepta hoy si su ventana **intersecta** el día local. Un METAR
de las 05:53 UTC tiene ventana que cae en la TARDE DE AYER en husos americanos.
Confirmado en vivo el 2026-08-20: KAUS y KDFW traían lecturas de 05:53Z con
100.04 y 100.94°F — calor de ayer, no de hoy. Meterlo en el piso a ciegas
importaría el pico del día anterior.

Pero cuando la ventana cae **entera dentro del día local** (KMIA: 07:53→13:53),
es información dura y válida sobre el día en curso, y se está tirando.

=============================== PRE-REGISTRO ================================
Escrito y commiteado (561bf21) ANTES de descargar ni un METAR.

DOS VARIANTES, y comparar las dos es el punto
  A) INGENUA   el ASOS entra siempre que su ventana intersecte hoy
  B) PROPUESTA el ASOS entra sólo si su ventana cae ENTERA dentro del día local

  Si (A) viola y (B) no, queda demostrado que **la guarda es lo que lo hace
  seguro**, no el ASOS. Si las dos son seguras, la guarda sobra y hay que
  decirlo. Si (B) también viola, se rechaza entero.

MÉTRICA PRIMARIA — SEGURIDAD, manda sobre todo lo demás
  Violación = el piso afirma más de lo que el día dio: `floor > settle + 0.5`
  (el settle del NWS es entero; mismo redondeo que el resto del sistema).

  RECHAZAR si la variante añade violaciones sobre el piso actual. El listón es
  el que se le exigió a `current` cuando entró al piso: riesgo añadido cero.

MÉTRICA SECUNDARIA — BENEFICIO
  Cuántos station-days tienen `asos > max_obs` con ventana limpia y cuánto
  suben. El piso es una guarda de corrección, no una fuente de skill.

CRITERIO DE DECISIÓN sobre (B)
  ADOPTAR  si violaciones_B <= violaciones_actual  Y  N >= 200 station-days
  RECHAZAR si violaciones_B > violaciones_actual
  ESPERAR  si N < 200

LO QUE ESTE BACKTEST NO RESPONDE
  - Fiabilidad del grupo en estaciones que no sean ASOS automáticas. Si alguna
    diera violaciones sistemáticas se excluiría ESA, no la feature.
  - El efecto sobre bins y `zero_impossible_bins`: un piso más alto mata más
    bins. Correcto por construcción, pero pide su propia corrida.

NOTA DE MÉTODO — por qué el análisis es a nivel de DÍA
  El piso real es intradía y cambia con cada poll. Para SEGURIDAD lo que importa
  es el peor caso del día: si el piso llegó a afirmar más que el settle en algún
  momento, eso es una violación. Tomando el máximo de cada fuente en el día se
  captura ese peor caso sin depender de la cadencia del poller.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_piso_asos6h.py [dias]
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))

from predictor import parse_metar_6h_max_c, c_to_f          # noqa: E402
from stations import STATIONS, STATION_TZ                   # noqa: E402

MESONET = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
DIAS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
N_MIN = 200


def metars(sid: str, ini, fin) -> list[tuple[datetime, str]]:
    """(ts_utc, raw) de cada METAR con grupo de 6h. Una petición por estación."""
    r = requests.get(MESONET, params={
        "station": sid[1:], "data": "metar",
        "year1": ini.year, "month1": ini.month, "day1": ini.day,
        "year2": fin.year, "month2": fin.month, "day2": fin.day,
        "tz": "UTC", "format": "onlycomma", "latlon": "no",
        "missing": "M", "trace": "T", "direct": "no", "report_type": 3,
    }, timeout=120)
    if r.status_code != 200:
        return []
    out = []
    for ln in r.text.splitlines()[1:]:
        partes = ln.split(",", 2)
        if len(partes) < 3:
            continue
        try:
            ts = datetime.strptime(partes[1], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        if parse_metar_6h_max_c(partes[2]) is not None:
            out.append((ts, partes[2]))
    return out


def asos_por_dia(sid: str, lecturas) -> tuple[dict, dict]:
    """(variante_A, variante_B) → {fecha_local: max_f}."""
    tz = ZoneInfo(STATION_TZ[sid])
    a, b = defaultdict(list), defaultdict(list)
    for ts, raw in lecturas:
        c = parse_metar_6h_max_c(raw)
        if c is None:
            continue
        f = c_to_f(c)
        ini = ts - timedelta(hours=6)
        ini_loc, fin_loc = ini.astimezone(tz), ts.astimezone(tz)
        # A: la ventana intersecta el día del METAR (criterio actual)
        a[fin_loc.date()].append(f)
        # B: la ventana cae ENTERA dentro del mismo día local
        if ini_loc.date() == fin_loc.date():
            b[fin_loc.date()].append(f)
    return ({d: max(v) for d, v in a.items()},
            {d: max(v) for d, v in b.items()})


def main() -> int:
    hoy = datetime.now(timezone.utc).date()
    ini, fin = hoy - timedelta(days=DIAS), hoy - timedelta(days=1)
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    print(f"Piso con ASOS 6h · {ini} → {fin}\n")
    print(f"{'st':6s} {'N':>4s} {'asos>maxobs':>12s} {'viol_now':>9s} "
          f"{'viol_A':>7s} {'viol_B':>7s} {'subida_med':>11s}")

    tot = {"n": 0, "op": 0, "now": 0, "A": 0, "B": 0}
    subidas, casos_b = [], []
    for s in STATIONS:
        sid = s.id
        tz = ZoneInfo(STATION_TZ[sid])
        off = int(datetime.now(tz).utcoffset().total_seconds() // 3600)
        desp = f"{off} hours"
        # nuestro max del feed y el CLI, por día local
        piso_now = {}
        for d, mo, cli in an.execute(
                f"""SELECT date(datetime(ts, ?)) d, MAX(today_max_obs),
                           MAX(COALESCE(today_max_cli, -999))
                    FROM station_snapshots
                    WHERE station=? AND ts>=? GROUP BY d""",
                (desp, sid, ini.isoformat())):
            vals = [v for v in (mo, cli if cli > -900 else None) if v is not None]
            if vals:
                piso_now[d] = max(vals)
        settles = dict(cal.execute(
            "SELECT date, max_obs_f FROM day_outcomes "
            "WHERE station_id=? AND date>=? AND max_obs_f IS NOT NULL",
            (sid, ini.isoformat())).fetchall())
        try:
            A, B = asos_por_dia(sid, metars(sid, ini, fin))
        except Exception as e:
            print(f"{sid:6s} error mesonet: {str(e)[:40]}")
            continue

        n = op = vn = va = vb = 0
        for d, settle in settles.items():
            base = piso_now.get(d)
            if base is None:
                continue
            fecha = datetime.strptime(d, "%Y-%m-%d").date()
            n += 1
            fa = max(base, A.get(fecha, -999))
            fb = max(base, B.get(fecha, -999))
            if fb > base + 0.01:
                op += 1
                subidas.append(fb - base)
                casos_b.append((sid, d, base, fb, settle))
            vn += base > settle + 0.5
            va += fa > settle + 0.5
            vb += fb > settle + 0.5

        if n:
            print(f"{sid:6s} {n:4d} {op:12d} {vn:9d} {va:7d} {vb:7d}")
            for k, v in (("n", n), ("op", op), ("now", vn), ("A", va), ("B", vb)):
                tot[k] += v

    print(f"\n{'=' * 72}")
    print(f"N = {tot['n']} station-days · el ASOS sube el piso en {tot['op']} "
          f"({100 * tot['op'] / max(1, tot['n']):.1f}%)")
    if subidas:
        print(f"subida mediana cuando actúa: {statistics.median(subidas):.2f}°F "
              f"· p90 {sorted(subidas)[int(len(subidas) * .9)]:.2f}°F")
    print(f"\nVIOLACIONES (piso > settle + 0.5):")
    print(f"  piso actual        {tot['now']:4d}")
    print(f"  + ASOS ingenuo (A) {tot['A']:4d}   ({tot['A'] - tot['now']:+d})")
    print(f"  + ASOS con guarda  {tot['B']:4d}   ({tot['B'] - tot['now']:+d})")

    print("\n── CRITERIO PRE-REGISTRADO ──")
    if tot["n"] < N_MIN:
        v = f"ESPERAR — N={tot['n']} < {N_MIN}"
    elif tot["B"] > tot["now"]:
        v = f"RECHAZAR — la guarda añade {tot['B'] - tot['now']} violaciones"
    else:
        v = "ADOPTAR — el ASOS con guarda no añade riesgo"
    print(f"  VEREDICTO: {v}")
    if tot["A"] > tot["B"]:
        print(f"  La guarda evita {tot['A'] - tot['B']} violaciones que la "
              f"variante ingenua sí cometería.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
