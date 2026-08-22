#!/usr/bin/env python3
"""¿Cuánto del gap de KDEN y KNYC recupera el grupo ASOS de 6h, y CUÁNDO llega?

CONTEXTO
--------
Medido el 2026-08-21 (`gap_feed5min_vs_cli.py`, N=520 station-days): el máximo de
nuestro feed de 5 min queda por debajo del CLI en casi todo el roster —media
−0.607°F— pero dos estaciones se despegan del resto:

    KDEN   mediana −1.06°F   media −1.60°F
    KNYC   mediana −0.94°F   media −1.04°F
    (la mejor del roster, KLAX, está en 0.00)

Ahí el piso se queda corto y deja información sobre la mesa. No es falta de
muestreo: las 20 estaciones se sondean igual, ~110 polls/día.

El grupo ASOS de 6h (`1sTTT` en los remarks) es **la misma fuente con la que
liquida el NWS** y entró al piso el 2026-08-20 (`backtest_piso_asos6h.py`). La
pregunta es cuánto de ese hueco tapa **en estas dos**, que nunca se midió por
separado.

⚠ POR QUÉ NO SE PUEDE USAR LA COLUMNA PERSISTIDA
   `today_max_asos_6h` sólo se guarda desde el 2026-08-20: 3 días. Se
   reconstruye desde METARs crudos de Iowa Mesonet con `parse_metar_6h_max_c`,
   el mismo parser que usó el backtest original — no uno nuevo.

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de descargar un solo METAR.

🔮 PREDICCIÓN ESTRUCTURAL, escrita antes de mirar — esto es lo que hace la
   corrida falsable en vez de un paseo por los datos:

   El grupo de 6h se publica al CERRAR su ventana. Los grupos salen a 00/06/12/18Z,
   así que en Denver (UTC−6) las ventanas locales son 00-06, 06-12, 12-18, 18-24.
   El pico de KDEN cae hacia las 15-16h local → lo cubre la ventana 12-18, cuyo
   grupo **no existe hasta las 18h local**.

   Si eso es así, el ASOS de 6h NO PUEDE cerrar el hueco intradía en estas
   estaciones por muy bueno que sea el dato: llega cuando el día ya se decidió.
   Sería una limitación ESTRUCTURAL, no un parámetro que se ajusta.

   Si los datos lo contradicen —el grupo llega antes del cierre del pico en la
   mayoría de los días— la predicción queda refutada y el ASOS sí es la vía.

MÉTRICAS
  1. COBERTURA   % de días con grupo LIMPIO (ventana entera dentro del día
                 local, la guarda ya vigente) disponible en algún momento.
  2. MOMENTO     hora local a la que el primer grupo limpio SUPERA al max_obs
                 corrido de los METARs horarios. Comparada contra el cierre de
                 `PEAK_HOURS` de la estación. Ésta es la métrica que decide.
  3. APORTE      en los días que aporta, cuánto sube sobre max_obs, y qué
                 fracción de la mediana del gap (−1.06 KDEN / −0.94 KNYC) cierra.
  4. SEGURIDAD   ¿algún grupo limpio se pasa del settle? En estas dos en
                 concreto. Si pasara, es problema de datos de ESAS estaciones.

CRITERIO DE DECISIÓN — fijado antes de correr
  ✅ EL ASOS ES LA VÍA     si el grupo limpio llega en o antes del cierre de
                           PEAK_HOURS en ≥50% de los días Y cierra ≥50% de la
                           mediana del gap.
  🔴 GAP ESTRUCTURAL       si llega DESPUÉS del cierre de PEAK_HOURS en ≥50% de
                           los días. Se documenta y se para: el ASOS no puede
                           ayudar intradía aquí, y cualquier alternativa lleva
                           su PROPIO pre-registro.
  ⚠ AMBIGUO                cualquier mezcla de las dos. No se toca nada.

  ⚠ ESTA CORRIDA NO CAMBIA NINGÚN THRESHOLD. N será ~25-50 días por estación,
  que es diagnóstico, no base para tunear (doctrina: N<10 no cambia un
  threshold, y 30 tampoco lo cambia si el efecto no es enorme). El resultado
  legítimo es saber por dónde va la cosa, no ajustar un número.

LO QUE NO RESPONDE
  - Si conviene alguna otra fuente (ASOS 1-min de Mesonet, CLI más temprano).
    Eso es otra pregunta y va aparte.
  - Nada sobre las otras 18 estaciones: el gap grande es de estas dos.
  - El grupo de 3h (`2sTTT`), que existe en algunas estaciones y no se mira aquí.
=============================================================================
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "weather-predictor"))

from predictor import parse_metar_6h_max_c, c_to_f            # noqa: E402
from stations import PEAK_HOURS, STATION_TZ                   # noqa: E402

MESONET = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
CALIB_DB = AQUI.parent / "weather-predictor" / "calibration.db"

ESTACIONES = ("KDEN", "KNYC")
GAP_CONOCIDO = {"KDEN": -1.06, "KNYC": -0.94}   # mediana medida el 08-21
DESDE = date(2026, 7, 1)


def descargar(sid: str, ini: date, fin: date) -> list[tuple[datetime, str]]:
    """(ts_utc, raw) de todos los METARs. Una petición por estación."""
    r = requests.get(MESONET, params={
        "station": sid[1:], "data": "metar",
        "year1": ini.year, "month1": ini.month, "day1": ini.day,
        "year2": fin.year, "month2": fin.month, "day2": fin.day,
        "tz": "UTC", "format": "onlycomma", "latlon": "no",
        "missing": "M", "trace": "T", "direct": "no", "report_type": 3,
    }, timeout=180)
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
        out.append((ts, partes[2]))
    return out


def temp_f_del_metar(raw: str) -> float | None:
    """Temperatura del grupo TT/DD del METAR, en °F. None si no está."""
    for tok in raw.split():
        if "/" not in tok or len(tok) < 3:
            continue
        t = tok.split("/")[0]
        neg = t.startswith("M")
        t2 = t[1:] if neg else t
        if t2.isdigit() and len(t2) == 2:
            c = -int(t2) if neg else int(t2)
            return c_to_f(c)
    return None


def settles(sid: str) -> dict:
    con = sqlite3.connect(CALIB_DB)
    out = {d: m for d, m in con.execute(
        "SELECT date, max_obs_f FROM day_outcomes "
        "WHERE station_id=? AND source='cli'", (sid,))}
    con.close()
    return out


def analizar(sid: str) -> None:
    tz = ZoneInfo(STATION_TZ[sid])
    cierre_pico = PEAK_HOURS[sid][1]
    hoy = date.today()
    lecturas = descargar(sid, DESDE, hoy)
    st = settles(sid)

    # Serie por día local: METARs horarios (temp) y grupos de 6h limpios.
    temps = defaultdict(list)      # dia -> [(hora_local, temp_f)]
    grupos = defaultdict(list)     # dia -> [(hora_local, max_f)]
    for ts, raw in lecturas:
        loc = ts.astimezone(tz)
        t = temp_f_del_metar(raw)
        if t is not None:
            temps[loc.date()].append((loc.hour + loc.minute / 60.0, t))
        c6 = parse_metar_6h_max_c(raw)
        if c6 is not None:
            ini_loc = (ts - timedelta(hours=6)).astimezone(tz)
            if ini_loc.date() == loc.date():        # la guarda ya vigente
                grupos[loc.date()].append((loc.hour + loc.minute / 60.0, c_to_f(c6)))

    dias = sorted(d for d in temps if d.isoformat() in st)
    if not dias:
        print(f"\n{sid}: sin días con settle")
        return

    con_grupo, momentos, aportes, violaciones = 0, [], [], 0
    for d in dias:
        gs = sorted(grupos.get(d, []))
        if not gs:
            continue
        con_grupo += 1
        serie = sorted(temps[d])
        primera = None
        for h, v in gs:
            corrido = max((t for hh, t in serie if hh <= h), default=None)
            if corrido is not None and v > corrido + 0.05:
                primera = (h, v - corrido)
                break
        if primera:
            momentos.append(primera[0])
            aportes.append(primera[1])
        if max(v for _, v in gs) > st[d.isoformat()] + 0.5:
            violaciones += 1

    n = len(dias)
    print(f"\n=================== {sid} ===================")
    print(f"  días con settle del CLI        : {n}")
    print(f"  días con grupo 6h LIMPIO       : {con_grupo} ({100*con_grupo/n:.0f}%)")
    print(f"  cierre de PEAK_HOURS           : {cierre_pico}h local")
    if not momentos:
        print("  el grupo limpio NUNCA supera al max_obs corrido → aporte nulo")
        return
    momentos.sort()
    a_tiempo = [h for h in momentos if h <= cierre_pico]
    print(f"\n  --- MOMENTO (la métrica que decide) ---")
    print(f"  hora local en que el grupo supera al max_obs:")
    print(f"     mediana {statistics.median(momentos):.1f}h  ·  "
          f"min {momentos[0]:.1f}h  ·  max {momentos[-1]:.1f}h")
    print(f"  llega EN O ANTES del cierre del pico: {len(a_tiempo)}/{len(momentos)} "
          f"({100*len(a_tiempo)/len(momentos):.0f}%)")
    print(f"\n  --- APORTE ---")
    aportes.sort()
    med_ap = statistics.median(aportes)
    gap = abs(GAP_CONOCIDO[sid])
    print(f"  sube sobre max_obs: mediana +{med_ap:.2f}°F  ·  p90 "
          f"+{aportes[int(0.9*(len(aportes)-1))]:.2f}°F")
    print(f"  cierra el {100*med_ap/gap:.0f}% de la mediana del gap ({gap:.2f}°F)")
    print(f"\n  --- SEGURIDAD ---")
    print(f"  días con grupo limpio por encima del settle: {violaciones}")

    pct_tiempo = 100 * len(a_tiempo) / len(momentos)
    pct_gap = 100 * med_ap / gap
    print(f"\n  --- VEREDICTO ---")
    if violaciones:
        print(f"  ⚠ {violaciones} violaciones — problema de datos de ESTA estación")
    if pct_tiempo >= 50 and pct_gap >= 50:
        print("  ✅ EL ASOS ES LA VÍA — llega a tiempo y cierra la mitad del gap")
    elif pct_tiempo < 50:
        print("  🔴 GAP ESTRUCTURAL — el grupo llega después del cierre del pico")
        print("     (predicción pre-registrada CONFIRMADA)")
    else:
        print("  ⚠ AMBIGUO — no se toca nada")


if __name__ == "__main__":
    for s in ESTACIONES:
        try:
            analizar(s)
        except Exception as e:
            print(f"{s}: error {type(e).__name__}: {e}")


# =========================================================================
# SEGUIMIENTO (2026-08-21, escrito DESPUÉS de ver el resultado y etiquetado
# como tal). La corrida pre-registrada da ✅ por su propio criterio, pero su
# métrica de APORTE compara dos cosas distintas y hay que decirlo:
#
#   · "sube +1.17°F sobre max_obs a las 11.9h" mide un LEVANTE DEL PISO A
#     MEDIODÍA, producido por el grupo que cubre la ventana de la MAÑANA.
#   · el gap de −1.06°F que motivó todo es del MÁXIMO DEL DÍA, que lo pone el
#     pico de la TARDE.
#
# El grupo de la mañana no puede saber nada del pico de la tarde. Que cierre
# "el 110% del gap" es una coincidencia numérica entre magnitudes que no se
# corresponden, no una respuesta.
#
# Esto mide lo alineado: cuánto del hueco contra el SETTLE queda tapado en el
# momento que importa. Sin criterio de decisión — es descriptivo, y cualquier
# cambio que salga de aquí lleva su propio pre-registro.
# =========================================================================
def seguimiento(sid: str) -> None:
    tz = ZoneInfo(STATION_TZ[sid])
    cierre = PEAK_HOURS[sid][1]
    lecturas = descargar(sid, DESDE, date.today())
    st = settles(sid)

    temps, grupos = defaultdict(list), defaultdict(list)
    for ts, raw in lecturas:
        loc = ts.astimezone(tz)
        t = temp_f_del_metar(raw)
        if t is not None:
            temps[loc.date()].append((loc.hour + loc.minute / 60.0, t))
        c6 = parse_metar_6h_max_c(raw)
        if c6 is not None:
            ini = (ts - timedelta(hours=6)).astimezone(tz)
            if ini.date() == loc.date():
                grupos[loc.date()].append((loc.hour + loc.minute / 60.0, c_to_f(c6)))

    print(f"\n--- {sid}: hueco contra el settle, por hora local ---")
    print(f"{'hora':>6} {'sin ASOS':>10} {'con ASOS':>10} {'cerrado':>9} {'n':>4}")
    for h in (12, 14, 15, 16, cierre):
        sin_l, con_l = [], []
        for d in sorted(temps):
            k = d.isoformat()
            if k not in st:
                continue
            mo = max((t for hh, t in temps[d] if hh <= h), default=None)
            if mo is None:
                continue
            g = max((v for hh, v in grupos.get(d, []) if hh <= h), default=None)
            sin_l.append(st[k] - mo)
            con_l.append(st[k] - (mo if g is None else max(mo, g)))
        if not sin_l:
            continue
        a, b = statistics.median(sin_l), statistics.median(con_l)
        pct = 100 * (a - b) / a if a > 0 else 0.0
        print(f"{h:>5}h {a:>+10.2f} {b:>+10.2f} {pct:>8.0f}% {len(sin_l):>4}")
    print("  (hueco = settle − piso; más cerca de 0 es mejor)")


if __name__ == "__main__" and "--seguimiento" in sys.argv:
    for s in ESTACIONES:
        try:
            seguimiento(s)
        except Exception as e:
            print(f"{s}: error {type(e).__name__}: {e}")
