#!/usr/bin/env python3
"""¿Puede el ASOS de 1 minuto tapar la ventana ciega de KDEN (12h-18h)?

╔════════════════════════════════════════════════════════════════════════════╗
║ RESULTADO (2026-08-22): 🔴 NO. Predicción estructural CONFIRMADA.           ║
║                                                                            ║
║   latencia del archivo   29.6 h   (idéntica pidiendo 48h o 96h, o sea que  ║
║                                    es el archivo y no la consulta)          ║
║   última obs disponible  2026-08-20 21:04Z, con el reloj en 08-22 02:42Z    ║
║   tres sondeos en vivo   cero filas, las tres veces                         ║
║                                                                            ║
║ Y AQUÍ ESTÁ LO AMARGO: el dato es EXACTAMENTE el que hace falta.            ║
║   hora   hueco actual   con 1-min   cerrado   n                             ║
║    14h       +4.82        +0.40       92%    14                             ║
║    15h       +2.90        −0.10      103%    14                             ║
║    16h       +2.40        −0.10      104%    14                             ║
║ Cierra la ventana ciega entera. Y llega día y medio tarde.                  ║
║                                                                            ║
║ Hallazgo lateral que importaría si algún día hay feed en vivo:              ║
║   max(1-min) − settle = +1.00°F de mediana (n=15, cero violaciones tras     ║
║   el margen de 0.9). El 1-min captura transitorios que el promedio de 5     ║
║   min con el que liquida el NWS suaviza. El margen los absorbe, pero justo: ║
║   +1.00 − 0.9 = +0.10 contra una tolerancia de +0.5.                        ║
║                                                                            ║
║ CONSECUENCIA: la ventana ciega de KDEN se queda ABIERTA. No hay vía         ║
║ identificada. Lo honesto para el instrumento es MOSTRARLA, no taparla.      ║
╚════════════════════════════════════════════════════════════════════════════╝


CONTEXTO
--------
Cadena de tres medidas que llevan hasta aquí:

1. `gap_feed5min_vs_cli.py` (N=520): nuestro feed de 5 min lee −0.607°F por
   debajo del CLI de media. Seguro para un piso. Pero KDEN se despega:
   mediana −1.06°F, **media −1.60°F**.
2. `asos6h_kden_knyc.py`: el grupo ASOS de 6h tapa ese hueco en KNYC (82-92%
   desde las 14h) y **no en KDEN** (0% entre 14h y 16h). Mecanismo verificado:
   los grupos salen a las mismas horas UTC y el huso decide dónde caen.
   KDEN (UTC−6) recibe el suyo a las 17:53 local, cubriendo 11:53→17:53.
3. Resultado: **Denver queda ciego de 12h a 18h**, que es exactamente su pico.

El ASOS de 1 min de Iowa Mesonet es la fuente que resolvió el caso KPHL del
2026-07-23, cuando el mercado tenía razón y nosotros no. Es también la fuente
upstream de la que sale el CLI con el que liquida Kalshi.

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de descargar una sola observación.

🔮 PREDICCIÓN ESTRUCTURAL, escrita antes de mirar:

   El 1-min de Iowa Mesonet es un **archivo de investigación**, no un feed en
   tiempo real. Si se publica con retraso de horas o de un día, NO PUEDE ayudar
   intradía por muy exacto que sea — exactamente el mismo modo de fallo que
   acaba de descartar al grupo de 6h en esta misma estación.

   Ya hay un indicio: el 2026-08-21 se probó el endpoint `asos1min.py` para MDW
   y devolvió **sólo la cabecera**, sin filas. Puede ser el parámetro, puede ser
   que el archivo vaya con retraso. Esta corrida lo resuelve.

⚠ LECCIÓN APLICADA DE LA CORRIDA ANTERIOR
   En `asos6h_kden_knyc.py` el criterio dio un ✅ falso porque su métrica de
   aporte medía el levante del piso a mediodía y lo comparaba contra el gap del
   máximo del día. Aquí la métrica de aporte se define **desde el principio
   sobre la ventana ciega concreta (14h, 15h, 16h local)** y contra el settle
   del CLI, que es la pregunta real. Nada de medias del día entero.

MÉTRICAS
  1. DISPONIBILIDAD  ¿devuelve filas el endpoint para KDEN? Si no, se acabó:
                     sin datos no hay feature, y el veredicto es 🔴.
  2. LATENCIA        ⭐ LA QUE DECIDE. Consultando AHORA, ¿de cuándo es la
                     observación más reciente? Tres sondeos separados ≥5 min
                     para no juzgar por una lectura suelta.
  3. APORTE          En días históricos, dentro de la ventana ciega (14/15/16h
                     local): `max(1-min hasta h) − piso_actual(h)`, y qué
                     fracción del hueco contra el settle cierra a esa hora.
  4. FIDELIDAD       `max(1-min del día)` contra el settle del CLI. Deberían
                     coincidir —el CLI sale de ahí—; si no coinciden, la fuente
                     no es la que creemos y todo lo demás sobra.

UMBRALES DE LATENCIA, razonados antes de medir (no ajustados al dato)
  ≤ 30 min   comparable a la frescura que ya tenemos (mediana medida hoy en el
             roster: 38 min). Sería una mejora usable.
  ≤ 120 min  peor que nuestro feed, pero aún así partiría en dos una ventana
             ciega de SEIS horas. Merecería decisión propia, no adopción
             automática.
  > 120 min  no resuelve el problema que motivó la corrida.

CRITERIO DE DECISIÓN — fijado antes de correr
  ✅ ADOPTAR (sólo KDEN)  latencia ≤30 min  Y  cierra ≥50% del hueco a las 15h
                          Y  cero violaciones (`max_1min − margen > settle+0.5`)
  ⚠ DECISIÓN APARTE       latencia 30-120 min con aporte ≥50%: la ventana ciega
                          se parte pero no es tiempo real. Se documenta y se
                          decide con su propio pre-registro, NO aquí.
  🔴 RECHAZAR             latencia >120 min, o sin datos, o aporte <50%, o
                          cualquier violación.

  ⚠ NO SE TOCA EL PISO EN ESTA CORRIDA. Si sale ✅, la implementación va en un
  paso aparte con su propia guarda de ventana — como se hizo con el ASOS de 6h,
  donde la guarda resultó ser lo que lo hacía seguro.

LO QUE NO RESPONDE
  - Las otras 19 estaciones. KDEN es la única con ventana ciega medida.
  - Si conviene además para el `today_max_obs` histórico: no, y no se pregunta.
  - El coste de pegarle a Mesonet cada 3 min en producción. Si sale ✅, eso es
    parte del paso de implementación, y la ventana histórica de esta corrida se
    limita a 14 días a propósito para no abusar del servicio.
=============================================================================
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "weather-predictor"))

from stations import PEAK_HOURS, STATION_TZ                   # noqa: E402

CALIB_DB = AQUI.parent / "weather-predictor" / "calibration.db"
ANALYSIS_DB = AQUI.parent / "weather-predictor" / "analysis.db"

SID = "KDEN"
DIAS_HISTORICO = 14
HORAS_CIEGAS = (14, 15, 16)
MARGEN = 0.9          # mismo que CURRENT_FLOOR_MARGIN_F
SONDEOS = 3
ESPERA_SONDEO = 300   # 5 min entre sondeos


def _pedir(params: dict) -> str | None:
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
    try:
        r = requests.get(url, params=params, timeout=120)
    except Exception as e:
        print(f"    error de red: {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        print(f"    HTTP {r.status_code}")
        return None
    return r.text


def descargar_1min(ini: datetime, fin: datetime) -> list[tuple[datetime, float]]:
    """(ts_utc, temp_f) del ASOS de 1 minuto."""
    txt = _pedir({
        "station": SID[1:], "tz": "UTC", "vars": "tmpf",
        "sts": ini.strftime("%Y-%m-%dT%H:%MZ"),
        "ets": fin.strftime("%Y-%m-%dT%H:%MZ"),
        "sample": "1min", "what": "download", "delim": "comma", "gis": "no",
    })
    if not txt:
        return []
    filas = []
    for ln in txt.strip().splitlines()[1:]:
        p = ln.split(",")
        if len(p) < 4:
            continue
        try:
            ts = datetime.strptime(p[2].strip(), "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc)
            filas.append((ts, float(p[3])))
        except (ValueError, IndexError):
            continue
    return filas


def medir_latencia() -> float | None:
    """Minutos entre ahora y la observación más reciente. Mediana de N sondeos."""
    print(f"\n=== 2. LATENCIA — {SONDEOS} sondeos separados {ESPERA_SONDEO//60} min ===")
    lat = []
    for i in range(SONDEOS):
        ahora = datetime.now(timezone.utc)
        filas = descargar_1min(ahora - timedelta(hours=3), ahora)
        if filas:
            ultimo = max(t for t, _ in filas)
            m = (ahora - ultimo).total_seconds() / 60.0
            lat.append(m)
            print(f"  sondeo {i+1}: última obs {ultimo:%H:%M}Z  →  {m:.1f} min de retraso")
        else:
            print(f"  sondeo {i+1}: sin filas")
        if i < SONDEOS - 1:
            time.sleep(ESPERA_SONDEO)
    if not lat:
        return None
    return statistics.median(lat)


def main() -> int:
    tz = ZoneInfo(STATION_TZ[SID])
    hoy = date.today()
    ini = hoy - timedelta(days=DIAS_HISTORICO)

    print(f"=== 1. DISPONIBILIDAD — {SID}, {DIAS_HISTORICO} días ===")
    hist = descargar_1min(
        datetime.combine(ini, datetime.min.time(), timezone.utc),
        datetime.combine(hoy, datetime.max.time(), timezone.utc))
    if not hist:
        print("  🔴 el endpoint no devuelve filas → sin datos, sin feature")
        return 1
    print(f"  {len(hist)} observaciones, de {min(t for t,_ in hist):%Y-%m-%d %H:%M}Z "
          f"a {max(t for t,_ in hist):%Y-%m-%d %H:%M}Z")

    lat = medir_latencia()
    print(f"\n  latencia mediana: {lat:.1f} min" if lat is not None
          else "\n  latencia: NO MEDIBLE")

    # --- 3 y 4: aporte en la ventana ciega, y fidelidad contra el settle ---
    por_dia = defaultdict(list)
    for ts, f in hist:
        por_dia[ts.astimezone(tz).date()].append((ts.astimezone(tz), f))

    cal = sqlite3.connect(CALIB_DB)
    settles = {d: m for d, m in cal.execute(
        "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? AND source='cli'",
        (SID,))}
    cal.close()

    ana = sqlite3.connect(ANALYSIS_DB)
    print(f"\n=== 3. APORTE en la ventana ciega ===")
    print(f"{'hora':>6} {'hueco actual':>13} {'con 1-min':>11} {'cerrado':>9} {'n':>4}")
    resultados = {}
    for h in HORAS_CIEGAS:
        sin_l, con_l = [], []
        for d, obs in sorted(por_dia.items()):
            k = d.isoformat()
            if k not in settles:
                continue
            # piso actual a esa hora = max(current_f) de nuestros snapshots
            fin = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=h)
            row = ana.execute(
                "SELECT MAX(current_f) FROM station_snapshots "
                "WHERE station=? AND ts >= ? AND ts <= ?",
                (SID, datetime.combine(d, datetime.min.time(), tz).astimezone(
                    timezone.utc).isoformat(), fin.astimezone(timezone.utc).isoformat()
                 )).fetchone()
            if not row or row[0] is None:
                continue
            piso_act = row[0] - MARGEN
            m1 = max((f for t, f in obs if t <= fin), default=None)
            if m1 is None:
                continue
            sin_l.append(settles[k] - piso_act)
            con_l.append(settles[k] - max(piso_act, m1 - MARGEN))
        if not sin_l:
            continue
        a, b = statistics.median(sin_l), statistics.median(con_l)
        pct = 100 * (a - b) / a if a > 0 else 0.0
        resultados[h] = pct
        print(f"{h:>5}h {a:>+13.2f} {b:>+11.2f} {pct:>8.0f}% {len(sin_l):>4}")
    ana.close()

    print(f"\n=== 4. FIDELIDAD contra el settle del CLI ===")
    difs, viol = [], 0
    for d, obs in sorted(por_dia.items()):
        k = d.isoformat()
        if k not in settles:
            continue
        mx = max(f for _, f in obs)
        difs.append(mx - settles[k])
        if mx - MARGEN > settles[k] + 0.5:
            viol += 1
    if difs:
        difs.sort()
        print(f"  max(1-min) − settle: mediana {statistics.median(difs):+.2f}°F  "
              f"·  min {difs[0]:+.2f}  ·  max {difs[-1]:+.2f}  (n={len(difs)})")
        print(f"  violaciones (max−0.9 > settle+0.5): {viol}")

    print(f"\n=== VEREDICTO ===")
    pct15 = resultados.get(15)
    if lat is None:
        print("  🔴 RECHAZAR — latencia no medible")
    elif lat > 120:
        print(f"  🔴 RECHAZAR — latencia {lat:.0f} min > 120")
        print("     (predicción estructural CONFIRMADA: es un archivo, no un feed)")
    elif viol:
        print(f"  🔴 RECHAZAR — {viol} violaciones del settle")
    elif pct15 is None or pct15 < 50:
        print(f"  🔴 RECHAZAR — cierra {pct15 if pct15 is not None else 0:.0f}% "
              "del hueco a las 15h, bajo el 50%")
    elif lat <= 30:
        print(f"  ✅ ADOPTAR en KDEN — latencia {lat:.0f} min, cierra {pct15:.0f}% a las 15h")
        print("     (la implementación va APARTE, con su propia guarda de ventana)")
    else:
        print(f"  ⚠ DECISIÓN APARTE — latencia {lat:.0f} min, cierra {pct15:.0f}%")
        print("     parte la ventana ciega pero no es tiempo real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
