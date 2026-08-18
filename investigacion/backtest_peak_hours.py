#!/usr/bin/env python3
"""¿Están descalibradas las ventanas PEAK_HOURS, y cuáles hay que mover?

CONTEXTO
--------
El 2026-08-17 el usuario dijo "el reloj del día está mal calibrado" mirando
KPHL. No era el reloj. Una medición rápida sobre `station_snapshots` sugirió que
**12 de 20 estaciones** tienen ≥3 días de 12 con el pico fuera de su ventana, y
siempre en la misma dirección: la ventana **cierra antes** de que ocurra el pico.

Generaliza [[bug_kbos_peak_hours_2026_07_20]], que se trató como caso aislado.

No es cosmético. `PEAK_HOURS` alimenta cuatro consumidores, y el error va en
todos hacia el mismo lado — dar el día por terminado antes de tiempo:

    physical_gate vía 1   fija techo max_obs+1.0 mientras el día aún sube
    polling adaptativo    pasa de 3 a 10 min justo cuando ocurre el pico
    cap_by_floor          deja de proteger contra la clavada prematura
    level_corrector       su hora de referencia es peak_lo − 2

⚠ LA MEDICIÓN RÁPIDA NO VALE PARA DECIDIR. Medía "primera vez que el sistema
registró el máximo", que es un **límite superior** de la hora real: con huecos
del poller la hora sale más tarde de lo que fue, exagerando justo el efecto que
se quiere demostrar. Los valores 0h/18h/20h de aquella tabla eran basura de días
con gaps.

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de la primera corrida.

FUENTE — dos independientes, y la decisión exige que coincidan
  A) ARCHIVO Open-Meteo (`peak_window._fetch_peak_hours`): temperatura horaria
     de reanálisis. No depende de nuestro poller, así que no tiene el sesgo de
     huecos. Es el método que el proyecto YA usa para la ventana empírica.
  B) NUESTROS METAR (`station_snapshots.today_max_obs`): la hora en que el
     high-water mark alcanza su valor final. Es la observación real de la
     estación, con el sesgo de huecos.

  (A) es reanálisis en un punto de rejilla y puede desviarse en timing; (B) es
  la estación real pero con posible retraso. **Se exige que las dos coincidan**
  en dirección antes de mover nada: si sólo una dice que hay descalibre, se
  reporta y NO se toca.

  Para (B) se descartan los días con cobertura insuficiente: <80% de los
  snapshots esperados entre las 06h y las 20h locales. El apagón del 08-08 y
  días similares no deben votar.

VENTANA PROPUESTA
  lo_nuevo = floor(p10 de las horas del pico)
  hi_nuevo = ceil(p90) + 1     (hi es EXCLUSIVO en PEAK_HOURS)
  Acotado a [6, 21] y con ancho mínimo de 3 h, que es el de las ventanas
  actuales más estrechas.

CRITERIO DE DECISIÓN, por estación
  MOVER si las TRES:
    (a) la ventana actual deja fuera el pico en ≥30% de los días, en la MISMA
        dirección (cierra pronto o abre tarde), en las DOS fuentes
    (b) N ≥ 20 días con dato en ambas
    (c) la ventana propuesta contiene el pico en ≥80% de los días
  NO MOVER en cualquier otro caso, aunque una fuente sola lo pida.

  El 30% no es arbitrario: con 3 de 12 días fuera ya se pierde el pico una vez
  por semana, que es la frecuencia con la que el techo físico y el polling
  toman decisiones erróneas. El 80% de cobertura es el listón que la ventana
  nueva tiene que superar para valer la pena.

LO QUE ESTE BACKTEST NO RESPONDE
  - Si mover la ventana MEJORA el error de predicción. Mide calibración de la
    ventana contra el pico observado, no P&L. El efecto sobre `physical_gate` y
    sobre el corrector pide su propia medición después.
  - Estacionalidad: los datos son de julio-agosto. Una ventana de verano puede
    no valer en octubre. Se anota y se revisa en otoño.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_peak_hours.py [dias]
"""
from __future__ import annotations

import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))

from stations import STATIONS, PEAK_HOURS, STATION_TZ   # noqa: E402
import peak_window as _pw                               # noqa: E402
from predictor import fetch_station                     # noqa: E402

DIAS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
COBERTURA_MIN = 0.80
FUERA_MAX = 0.30
N_MIN = 20
COBERTURA_NUEVA_MIN = 0.80
HORA_INI, HORA_FIN = 6, 20


def horas_pico_metar(con, sid: str, dias: int) -> list[int]:
    """Hora local en que hoy_max_obs alcanza su valor final, por día.

    Se descartan los días con <80% de los snapshots esperados entre 06h y 20h:
    con huecos, el high-water mark sube tarde y la hora sale sesgada.
    """
    tz = ZoneInfo(STATION_TZ[sid])
    off = int(datetime.now(tz).utcoffset().total_seconds() // 3600)
    desp = f"{off} hours"
    desde = (datetime.now(tz).date() - timedelta(days=dias)).isoformat()
    filas = con.execute(
        f"""SELECT date(datetime(ts, ?)) d,
                   CAST(strftime('%H', datetime(ts, ?)) AS INT) h,
                   today_max_obs
            FROM station_snapshots
            WHERE station = ? AND ts >= ? AND today_max_obs IS NOT NULL
            ORDER BY ts""",
        (desp, desp, sid, desde)).fetchall()

    por_dia: dict[str, list] = {}
    for d, h, mx in filas:
        por_dia.setdefault(d, []).append((h, mx))

    out = []
    hoy = datetime.now(tz).date().isoformat()
    for d, vals in por_dia.items():
        if d == hoy:
            continue                      # día incompleto
        en_ventana = [v for v in vals if HORA_INI <= v[0] <= HORA_FIN]
        # Cadencia mínima esperada: un snapshot cada 10 min en 14 h = 84.
        if len(en_ventana) < 84 * COBERTURA_MIN:
            continue
        mx_final = max(m for _, m in vals)
        # primera hora en que se alcanza el máximo del día
        h_pico = next(h for h, m in vals if m >= mx_final - 1e-9)
        out.append(h_pico)
    return out


def ventana_propuesta(horas: list[int]) -> tuple[int, int]:
    st = _pw._stats(horas)
    lo = int(math.floor(st["p10"]))
    hi = int(math.ceil(st["p90"])) + 1
    lo = max(HORA_INI, min(21, lo))
    hi = max(lo + 3, min(21, hi))
    return lo, hi


def fuera(horas: list[int], lo: int, hi: int) -> tuple[float, int, int]:
    tarde = sum(1 for h in horas if h >= hi)
    pronto = sum(1 for h in horas if h < lo)
    n = len(horas) or 1
    return (tarde + pronto) / n, tarde, pronto


def main() -> int:
    con = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    print(f"Ventanas PEAK_HOURS · {DIAS} días · dos fuentes independientes\n")
    print(f"{'st':6s} {'actual':>7s} │ {'ARCHIVO':>18s} │ {'METAR':>18s} │ "
          f"{'propuesta':>9s}  veredicto")

    a_mover = {}
    for s in STATIONS:
        sid = s.id
        lo, hi = PEAK_HOURS[sid]
        h_met = horas_pico_metar(con, sid, DIAS)
        try:
            h_arc = _pw._fetch_peak_hours(fetch_station(sid), days=DIAS) or []
        except Exception as e:
            h_arc = []
            print(f"{sid:6s} archivo falló: {str(e)[:40]}")

        if len(h_arc) < 3 or len(h_met) < 3:
            print(f"{sid:6s} {f'{lo}-{hi}':>7s} │ datos insuficientes "
                  f"(arc={len(h_arc)} met={len(h_met)})")
            continue

        f_arc, t_arc, _ = fuera(h_arc, lo, hi)
        f_met, t_met, _ = fuera(h_met, lo, hi)
        n_ok = min(len(h_arc), len(h_met)) >= N_MIN
        # Misma dirección: las dos fuentes deben señalar que cierra pronto.
        misma_dir = (t_arc / max(1, len(h_arc)) > 0.15
                     and t_met / max(1, len(h_met)) > 0.15)

        nlo, nhi = ventana_propuesta(h_arc + h_met)
        cob_nueva = 1 - fuera(h_arc + h_met, nlo, nhi)[0]

        cumple = (f_arc >= FUERA_MAX and f_met >= FUERA_MAX and misma_dir
                  and n_ok and cob_nueva >= COBERTURA_NUEVA_MIN)
        if cumple:
            a_mover[sid] = (nlo, nhi)
            v = f"MOVER → ({nlo},{nhi})  cubre {cob_nueva*100:.0f}%"
        elif f_arc >= FUERA_MAX or f_met >= FUERA_MAX:
            razon = ("una sola fuente" if not misma_dir
                     else f"N={min(len(h_arc), len(h_met))}<{N_MIN}"
                     if not n_ok else f"la nueva sólo cubre {cob_nueva*100:.0f}%")
            v = f"no mover · {razon}"
        else:
            v = "ok"

        print(f"{sid:6s} {f'{lo}-{hi}':>7s} │ "
              f"n={len(h_arc):2d} fuera={f_arc*100:3.0f}% tarde={t_arc:2d} │ "
              f"n={len(h_met):2d} fuera={f_met*100:3.0f}% tarde={t_met:2d} │ "
              f"{f'({nlo},{nhi})':>9s}  {v}")

    print(f"\n{'='*70}")
    if a_mover:
        print(f"CUMPLEN EL CRITERIO: {len(a_mover)} estaciones\n")
        for sid, (nlo, nhi) in a_mover.items():
            print(f"    StationConfig(\"{sid}\", ..., {nlo}, {nhi}, ...)")
    else:
        print("Ninguna estación cumple las tres condiciones.")
    print("\n⚠ Datos de julio-agosto: ventana de verano. Revisar en otoño.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
