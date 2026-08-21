#!/usr/bin/env python3
"""¿Debe el piso recordar el MÁXIMO del feed de 5 min, en vez de sólo el valor actual?

CONTEXTO — KMDW, 2026-08-21
---------------------------
El usuario reporta: «Chicago sigue en 79, pero ya la máxima es 80». Reconstruido
contra la API cruda del NWS:

    20:15Z  80.6°F   ← 27°C exactos
    20:20Z  80.6°F
    20:25Z  80.6°F
    20:30Z  80.6°F   ← cuatro lecturas, 20 minutos sostenidos
    20:35Z  78.8°F   ← baja

    today_max_obs    78.08°F  (METAR horario de 19:53Z — el pico cayó ENTRE dos)
    piso a las 20:30  79.70°F  (= 80.6 − 0.9, vía `current`)
    piso a las 20:40  78.08°F  ← el pico se EVAPORA

El sistema vio 80.6, lo usó mientras duraba, y lo olvidó al bajar la aguja.
`today_max_obs` no lo recogió porque sólo acepta METARs horarios (:53) y el pico
cayó en medio del ciclo — el mismo undersampling documentado en KNYC.

Kalshi tenía el bin 80-81 al 94% mientras nosotros predecíamos 79.

EL DEFECTO, EN UNA LÍNEA
------------------------
`build_snapshot` hace `floor = max(max_obs, CLI, ASOS_6h, current − 0.9)`, donde
`current` es el valor **instantáneo**. Un máximo no puede bajar; este sí baja.

POR QUÉ EL MARGEN DE 0.9 NO ES EL OBSTÁCULO
--------------------------------------------
Se podría pensar que `CURRENT_FLOOR_MARGIN_F` protege contra picos espurios y
que aplicarlo al máximo corrido convertiría un glitch en un piso permanente.
**No es eso lo que hace.** Su comentario en `predictor.py` lo dice: el feed llega
en °C enteros, "80.6°F" representa el intervalo [79.7, 81.5), y para un piso hay
que tomar el extremo inferior. Es cuantización de unidades, no defensa contra
outliers — y esa lógica vale idéntica para el máximo del día que para el valor
de ahora. Ese es justamente el punto que este backtest tiene que **verificar**,
no asumir: si el máximo corrido añade violaciones, la premisa era falsa.

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de leer una sola fila de la DB.

⚠ NOTA DE MÉTODO — ESTE BACKTEST TIENE QUE SER INTRADÍA, NO POR DÍA
   `backtest_piso_asos6h.py` analizó a nivel de DÍA y fue correcto allí. Aquí
   sería un sinsentido: a nivel de día, `MAX(current_f)` **es** el máximo
   corrido, así que las dos variantes serían idénticas por construcción y el
   backtest no podría fallar. La diferencia sólo existe dentro del día: en el
   instante t, la regla actual mira `current(t)` y la propuesta mira
   `max(current) sobre [inicio_del_día, t]`. Se evalúa snapshot a snapshot.

DOS VARIANTES, y comparar las dos es el punto
  A) INGENUA   máximo corrido agrupando por día UTC
  B) PROPUESTA máximo corrido agrupando por día LOCAL de la estación

  A las 00Z son las ~19h local en husos americanos, así que agrupar por UTC
  arrastra la tarde de AYER al máximo de HOY. Es la misma trampa que ya mordió
  al grupo ASOS de 6h el 2026-08-20 (KAUS y KDFW con 100°F de la víspera). Si
  (A) viola y (B) no, queda demostrado que **la guarda del día local es lo que
  lo hace seguro**. Si las dos son seguras, la guarda sobra y hay que decirlo.
  Si (B) también viola, se rechaza entero.

MÉTRICA PRIMARIA — SEGURIDAD, manda sobre todo lo demás
  Violación = el piso afirma más de lo que el día dio: `floor > settle + 0.5`
  (el settle del NWS es entero; mismo redondeo que el resto del sistema).

  ⚠ La variante propuesta sólo puede AÑADIR violaciones, nunca quitarlas: es un
  max() sobre un superconjunto, así que `floor_B >= floor_actual` siempre. Por
  eso el listón es el mismo que se le exigió a `current` cuando entró al piso y
  que cumplió: **riesgo añadido CERO**. No hay compensación posible por el lado
  del beneficio — un piso que miente es un error garantizado, no un error medio.

MÉTRICA SECUNDARIA — BENEFICIO
  1. Cuántos snapshots suben de piso y cuánto (mediana y p90 del incremento).
  2. Cuántos snapshots tienen `our_pred_f < floor_B − 0.5`, o sea predicciones
     ya refutadas por el termómetro que la regla actual no corrige.
  El piso es una guarda de corrección, no una fuente de skill: si el beneficio
  fuera cero pero el riesgo también, la decisión es NO tocar nada.

VENTANA DE DATOS
  Primaria: 2026-08-01 en adelante. Julio lleva doce pasadas y no se decide
  sobre él (doctrina). Julio se reporta aparte, sólo como robustez.

CRITERIO DE DECISIÓN sobre (B) — fijado antes de correr
  ADOPTAR   si violaciones_añadidas_B == 0  Y  N >= 200 station-days con settle
  RECHAZAR  si violaciones_añadidas_B > 0 de forma transversal (≥3 estaciones)
  EXCLUIR   la estación concreta si las violaciones se concentran en 1-2
            estaciones (mismo trato que se le dio al roster en otros casos):
            se excluye ESA, no la feature
  ESPERAR   si N < 200

LO QUE ESTE BACKTEST NO RESPONDE
  - El efecto sobre los bins. Un piso más alto mata más bins vía
    `zero_impossible_bins`; es correcto por construcción pero pide su corrida.
  - Si conviene además ALIMENTAR `today_max_obs` con el feed de 5 min. Eso es
    otra pregunta (cambia la serie histórica, no sólo el piso) y NO se toca aquí.
  - Qué pasa cuando el feed calla horas: el máximo corrido se congela, que es el
    comportamiento correcto, pero no se mide su frecuencia.
=============================================================================
"""
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "weather-predictor"))
import stations  # noqa: E402

AQUI = os.path.join(os.path.dirname(__file__), "..", "weather-predictor")
ANALYSIS_DB = os.path.join(AQUI, "analysis.db")
CALIB_DB = os.path.join(AQUI, "calibration.db")

MARGEN = 0.9          # CURRENT_FLOOR_MARGIN_F
TOLERANCIA = 0.5      # el settle del NWS es entero
DESDE = "2026-08-01"
N_MINIMO = 200


def _parse(ts):
    if not ts:
        return None
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(str(ts)[:26], f).replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            continue
    return None


def cargar_settles():
    con = sqlite3.connect(CALIB_DB)
    out = {}
    for sid, d, mx in con.execute(
            "SELECT station_id, day, actual_max_f FROM day_outcomes "
            "WHERE actual_max_f IS NOT NULL"):
        out[(sid, d)] = float(mx)
    con.close()
    return out


def cargar_snapshots(desde):
    """Snapshots ordenados por estación y tiempo, con los componentes del piso."""
    con = sqlite3.connect(ANALYSIS_DB)
    q = ("SELECT station, ts, current_f, today_max_obs, today_max_cli, "
         "       today_max_asos_6h, today_max_asos_6h_ts, our_pred_f "
         "FROM station_snapshots WHERE ts >= ? ORDER BY station, ts")
    filas = con.execute(q, (desde,)).fetchall()
    con.close()
    return filas


def piso_base(max_obs, cli, asos, asos_ts, t_utc, tz):
    """El piso SIN la componente de `current` — idéntico en las tres variantes.

    Reproduce la guarda del ASOS de 6h: sólo entra si su ventana de 6h cae
    entera dentro del día local (backtest_piso_asos6h.py).
    """
    piso = None
    for v in (max_obs, cli):
        if v is not None and v > -900:
            piso = v if piso is None else max(piso, v)
    if asos is not None and asos > -900 and asos_ts:
        fin = _parse(asos_ts)
        if fin is not None:
            ini = fin - timedelta(hours=6)
            dia = t_utc.astimezone(tz).date()
            if (ini.astimezone(tz).date() == dia
                    and fin.astimezone(tz).date() == dia):
                piso = asos if piso is None else max(piso, asos)
    return piso


def correr(desde, etiqueta):
    settles = cargar_settles()
    filas = cargar_snapshots(desde)
    tzs = {sid: ZoneInfo(stations.tz_for(sid)) if hasattr(stations, "tz_for")
           else ZoneInfo(stations.STATIONS[sid].tz) for sid in stations.STATION_IDS}

    # Máximo corrido por (estación, día) en las dos agrupaciones.
    run_utc = defaultdict(lambda: None)
    run_loc = defaultdict(lambda: None)

    viol = {"actual": 0, "A": 0, "B": 0}
    viol_por_est = {"A": defaultdict(int), "B": defaultdict(int)}
    subidas, refutadas, n_snap = [], 0, 0
    dias = set()

    for sid, ts, cur, max_obs, cli, asos, asos_ts, pred in filas:
        if sid not in tzs:
            continue
        t = _parse(ts)
        if t is None:
            continue
        tz = tzs[sid]
        dia_loc = t.astimezone(tz).date().isoformat()
        dia_utc = t.date().isoformat()

        if cur is not None and cur > -900:
            for clave, d in ((("u", sid, dia_utc), run_utc), (("l", sid, dia_loc), run_loc)):
                k = clave
                prev = d[k]
                d[k] = cur if prev is None else max(prev, cur)

        settle = settles.get((sid, dia_loc))
        if settle is None:
            continue
        dias.add((sid, dia_loc))
        n_snap += 1

        base = piso_base(max_obs, cli, asos, asos_ts, t, tz)

        def con_current(valor):
            if valor is None:
                return base
            cand = valor - MARGEN
            return cand if base is None else max(base, cand)

        p_act = con_current(cur if (cur is not None and cur > -900) else None)
        p_a = con_current(run_utc[("u", sid, dia_utc)])
        p_b = con_current(run_loc[("l", sid, dia_loc)])

        techo = settle + TOLERANCIA
        if p_act is not None and p_act > techo:
            viol["actual"] += 1
        if p_a is not None and p_a > techo:
            viol["A"] += 1
            viol_por_est["A"][sid] += 1
        if p_b is not None and p_b > techo:
            viol["B"] += 1
            viol_por_est["B"][sid] += 1

        if p_b is not None and p_act is not None and p_b > p_act + 0.001:
            subidas.append(p_b - p_act)
        if p_b is not None and pred is not None and pred < p_b - TOLERANCIA:
            refutadas += 1

    print(f"\n=========== {etiqueta} (desde {desde}) ===========")
    print(f"snapshots con settle : {n_snap}")
    print(f"station-days         : {len(dias)}")
    if not n_snap:
        print("sin datos")
        return None

    print("\n--- SEGURIDAD (violación = piso > settle + 0.5) ---")
    print(f"  piso actual                 {viol['actual']:6}  "
          f"({100*viol['actual']/n_snap:.3f}%)")
    for v in ("A", "B"):
        nom = "A) máx corrido, día UTC  " if v == "A" else "B) máx corrido, día LOCAL"
        add = viol[v] - viol["actual"]
        print(f"  {nom}   {viol[v]:6}  ({100*viol[v]/n_snap:.3f}%)   "
              f"añadidas: {add:+}")
        if viol_por_est[v]:
            peor = sorted(viol_por_est[v].items(), key=lambda x: -x[1])[:5]
            print(f"      por estación: {peor}")

    print("\n--- BENEFICIO de (B) ---")
    if subidas:
        subidas.sort()
        n = len(subidas)
        print(f"  snapshots con piso más alto : {n} ({100*n/n_snap:.1f}%)")
        print(f"  subida mediana              : {subidas[n//2]:.2f}°F")
        print(f"  subida p90                  : {subidas[int(0.9*(n-1))]:.2f}°F")
        print(f"  subida máxima               : {subidas[-1]:.2f}°F")
    else:
        print("  ninguna subida")
    print(f"  predicciones ya refutadas por el termómetro que (B) corrige: {refutadas}")

    print("\n--- VEREDICTO ---")
    add_b = viol["B"] - viol["actual"]
    est_con_viol = len([s for s, c in viol_por_est["B"].items()
                        if c > viol_por_est["A"].get(s, 0) * 0])
    if len(dias) < N_MINIMO:
        print(f"  ESPERAR — N={len(dias)} < {N_MINIMO} station-days")
    elif add_b == 0:
        print("  ✅ ADOPTAR — riesgo añadido CERO, mismo listón que `current`")
    elif est_con_viol >= 3:
        print(f"  🔴 RECHAZAR — {add_b} violaciones añadidas en {est_con_viol} estaciones")
    else:
        print(f"  ⚠ EXCLUIR estación(es) — {add_b} violaciones concentradas")
    return viol


if __name__ == "__main__":
    correr(DESDE, "PRIMARIA · agosto (base fresca)")
    correr("2026-07-01", "ROBUSTEZ · julio+agosto (NO decide — julio lleva 12 pasadas)")
