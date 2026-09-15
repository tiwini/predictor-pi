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

  ⚠ REESCRITO el 2026-09-15. La versión anterior pedía «>=7 de los últimos
  10» y la evaluaba el cron CADA DÍA sobre una ventana MÓVIL. Eso es la misma
  parada opcional que la auditoría del 09-10 quitó de la rama congelada, y aquí
  se quedó: bajo H0 el umbral 7/10 marca el 17.2% de las miradas, así que
  mirando a diario la bandera acaba saliendo sola (cota superior 43% a los 10
  días, 81% a los 30). El 🟡 de KHOU del 09-12 nació de ahí.

  No se arregla copiando la rama congelada —«los N primeros»— porque las dos
  ramas responden a preguntas distintas: aquélla decide ADOPCIÓN sobre un
  conjunto que ya no cambia, y ésta es un DETECTOR DE CAMBIO DE RÉGIMEN, cuyo
  trabajo es precisamente mirar lo reciente. Fijar la ventana la dejaría ciega
  para lo único que vigila. La cura de un detector continuo es un test
  secuencial: subir el listón por mirada y exigir que la señal se SOSTENGA.

  Calibrado por simulación (200k series bajo H0, ventana 10, horizonte de 30
  miradas diarias = la retención de analysis.db). Las miradas comparten 9 de
  sus 10 días, así que la tasa se simula, no se deriva de una binomial. De la
  rejilla (umbral, k) que deja la falsa alarma <=0.05 se eligió, por regla
  ejecutable, la de MENOR latencia mediana entre las de potencia >=0.85:

       umbral contra>=9 de 10, sostenido k=3 miradas  (12 días de settle)
       falsa alarma en 30 miradas .... 0.043
       detecta un giro real p=0.8 .... 0.85  (mediana 8 miradas)
       detecta un giro real p=0.9 .... 0.99  (mediana 3 miradas)

  Se descartó (umbral 8, k=8): misma potencia (0.87) pero mediana 9 miradas a
  p=0.9 en vez de 3, y 17 días de settle en vez de 12. Con el umbral viejo de
  7 NINGÚN k llega a 0.05 (k=12 aún da 0.061): el listón por mirada era
  demasiado flojo para que la racha lo salvara sola.

  🔴 REVERTIR la estación si, con N>=12 días:
       (a) el error publicado cae EN CONTRA DE LA CORRECCIÓN en >=9 de los
           últimos 10, en las 3 miradas diarias consecutivas, Y
       (b) |error| medio publicado >= |error| medio sin corrector
     Las dos: pasarse sistemáticamente no es problema si aun así queda más
     cerca que no corregir.

  🟡 REVISAR a mano si la racha se sostiene igual pero el |error| sigue siendo
     mejor. Es corrección excesiva que todavía compensa; la salida
     probablemente sea recortar la mediana, no apagarla.

  🟢 SEGUIR si el |error| publicado es menor y los signos están repartidos.

  ⚠ «En contra de la corrección», no «negativo» (corregido el 2026-08-28 al
  entrar KMIA). Hasta ese día las tres estaciones habilitadas sobre-predecían y
  el corrector RESTABA, así que pasarse producía errores negativos y bastaba con
  contar ésos. KMIA es la primera a la que se SUMA: allí pasarse produce errores
  POSITIVOS, y la guarda escrita con el signo fijo no habría visto nunca una
  sobre-corrección. Contar el lado equivocado es no vigilar nada — el mismo
  agujero que dejó a KSFO sin aviso llevando el peor |error| de las tres.

N<10 no decide nada, se imprime y ya. La hora de referencia es la de decisión
(mediodía local), no la de la ventana de pico: dentro de la ventana el piso de
observación domina y ambas ramas quedan clavadas, así que ahí no se distingue.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/seguimiento_corrector.py [hora_local]
"""
from __future__ import annotations

import sqlite3
import math
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
# La hora se lee en main(), no aquí: al importar el módulo desde un test
# `sys.argv[1]` es el path que pytest recibió y el int() reventaba la colección.
HORA = 12
VENTANA_MIN = 45


def primer_dia_activo(an, st: str) -> str | None:
    r = an.execute(
        """SELECT MIN(date(datetime(ts, 'localtime'))) FROM station_snapshots
           WHERE station=? AND bias_path='median_causal' AND bias_applied=1""",
        (st,)).fetchone()
    return r[0] if r and r[0] else None


def fila_del_dia(an, cal, st: str, dia: str,
                 frozen: bool = False) -> dict | None:
    tz = ZoneInfo(STATION_TZ[st])
    d = datetime.strptime(dia, "%Y-%m-%d").date()
    ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=HORA)
    lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
    hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
    r = an.execute(
        """SELECT ts, ens_med, bias_f, bias_applied, bias_frozen_f
           FROM station_snapshots
           WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
           ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
        (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"),
         ref.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
    if r is None:
        return None
    # Sólo cuentan los días en que el corrector estaba aplicado A ESA HORA. El
    # día en que se habilita la estación tiene snapshots de ambos tipos: los de
    # antes del deploy salen con bias_applied=0 y compararlos contra sí mismos
    # mete un día de delta cero que diluye la métrica. Ojo: `bias_f` = 0.00 con
    # applied=1 sí es un día válido — es el corrector diciendo "no corrijo".
    if frozen:
        # Congelada: lo publicado NO lleva corrección, y el contrafactual es lo
        # que el corrector habría hecho (`bias_frozen_f`, registrado por el
        # poller ya capeado por el piso). Los papeles se invierten respecto al
        # modo activo, por eso la columna se llama `alt` y no `sin`.
        if r["bias_applied"] or r["bias_frozen_f"] is None:
            return None
    elif not r["bias_applied"]:
        return None
    settle = cal.execute(
        "SELECT max_obs_f FROM day_outcomes WHERE station_id=? AND date=?",
        (st, dia)).fetchone()
    if settle is None or settle[0] is None:
        return None

    if frozen:
        b = r["bias_frozen_f"]
    else:
        b = (r["bias_f"] if (r["bias_applied"] and r["bias_f"] is not None)
             else 0.0)
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
    fila = {"dia": dia, "settle": settle[0], "pub": r["ens_med"],
            "alt": r["ens_med"] - b if frozen else r["ens_med"] + b,
            "corr": b, "mk": mk, "frozen": frozen}
    if not frozen:
        # Nombre histórico de la columna alternativa; lo lee
        # `investigacion/amarillo_klax.py`. Sólo existe en modo activo, donde
        # significa "sin corrector".
        fila["sin"] = fila["alt"]
    return fila


def primer_dia_congelado(an, st: str) -> str | None:
    """Primer día con la estación congelada, del primer `bias_frozen_f`.

    El congelado abre una fase NUEVA: los días de antes ya los juzgó la guarda
    de sobre-corrección y su veredicto es lo que llevó aquí. Mezclarlos
    invertiría el significado de la columna a mitad de tabla.
    """
    r = an.execute(
        """SELECT MIN(date(datetime(ts, 'localtime'))) FROM station_snapshots
           WHERE station=? AND bias_frozen_f IS NOT NULL""",
        (st,)).fetchone()
    return r[0] if r and r[0] else None


# Revisiones que una estación se dejó pendientes al entrar: {est: (N, motivo)}.
# El watchdog avisa solo al alcanzarse el N — un comentario con fecha no avisa,
# y ya pasó: la nota «revisar Sep-Oct» de SEASONAL_OFFSET_F venció y estuvo dos
# meses sin que nadie la mirara, con el offset corrigiendo mientras tanto.
REVISIONES_PENDIENTES: dict[str, tuple[int, str]] = {
    "KLAS": (40,
             "entró FALLANDO su propio criterio (mejora 0.66°F contra los 0.75 "
             "pedidos, acierta 17/25 con p=0.054) y se habilitó restringiendo "
             "la ventana a 9-13h sobre la MISMA muestra que había fallado. Su "
             "fila del 2026-08-28 decía «se revisa con N≥40». Correr "
             "investigacion/klas_revision.py"),
}


VENTANA_SIGNOS = 10      # días de la ventana de signos
UMBRAL_CONTRA = 9        # días en contra dentro de la ventana que la marcan
K_SOSTENIDO = 3          # miradas diarias consecutivas marcadas para disparar
N_MIN_SIGNOS = VENTANA_SIGNOS + K_SOSTENIDO - 1     # 12 días de settle


def _contra_en(e_pub: list[float], fin: int, signo: float) -> int:
    """Días en contra de la corrección en la ventana que TERMINA en `fin`."""
    return sum(1 for e in e_pub[fin - VENTANA_SIGNOS:fin] if e * signo < 0)


def racha_en_contra(e_pub: list[float], corr_mediana: float) -> int:
    """Miradas diarias consecutivas, contando hacia atrás desde hoy, marcadas.

    Una «mirada» es lo que el cron de las 16h habría visto ese día: la ventana
    de 10 que termina ahí. La racha se recalcula del dato, no se persiste: si
    viviera en `corrector_watchdog/estado.json` podría divergir del criterio,
    que es justo lo que el watchdog dice evitar al importar este módulo en vez
    de reimplementarlo.
    """
    signo = 1.0 if corr_mediana >= 0 else -1.0
    racha = 0
    for fin in range(len(e_pub), VENTANA_SIGNOS - 1, -1):
        if _contra_en(e_pub, fin, signo) < UMBRAL_CONTRA:
            break
        racha += 1
    return racha


def veredicto_por_signos(e_pub: list[float], corr_mediana: float,
                         m_pub: float, m_sin: float) -> tuple[str, str, int]:
    """(estado, texto, n_en_contra) — la regla de vigilancia, aislada y pura.

    Vive aparte para poder testearla sin montar dos bases de datos, y para que
    el umbral tenga UN solo sitio donde vivir.

    Sobre-corregir empuja el error al lado CONTRARIO al de la corrección: si se
    resta (corr>0) aparece sub-predicción (errores negativos); si se suma
    (corr<0), sobre-predicción (errores positivos). Por eso el conteo se hace
    contra el signo de la corrección y no contra un signo fijo.

    `n_en_contra` es el de la ventana de HOY, que es lo que se imprime; el que
    decide es la racha (ver `racha_en_contra` y la doctrina de arriba).
    """
    signo = 1.0 if corr_mediana >= 0 else -1.0
    contra = (_contra_en(e_pub, len(e_pub), signo)
              if len(e_pub) >= VENTANA_SIGNOS
              else sum(1 for e in e_pub if e * signo < 0))
    if len(e_pub) < N_MIN_SIGNOS:
        return ("n_bajo", f"N={len(e_pub)} — no decide, "
                          f"faltan {N_MIN_SIGNOS - len(e_pub)} días", contra)
    if racha_en_contra(e_pub, corr_mediana) < K_SOSTENIDO:
        return "verde", "🟢 SEGUIR", contra
    if m_pub >= m_sin:
        return ("rojo",
                f"🔴 REVERTIR — sobre-corrige y ya no compensa "
                f"(>={UMBRAL_CONTRA}/{VENTANA_SIGNOS} en contra "
                f"{K_SOSTENIDO} días seguidos)", contra)
    return ("amarillo",
            f"🟡 REVISAR — vuelca el signo {K_SOSTENIDO} días seguidos pero "
            f"aún queda más cerca", contra)


N_DECISION = 10          # primera ventana de decisión
N_DECISION_2 = 20        # segunda y última
MEJORA_MIN_F = 0.50      # suelo de materialidad, en °F
CORR_MIN_RELEVANTE_F = 0.50
Z_UNILATERAL_05 = 1.645


def _p_signos(a_favor: int, n: int) -> float:
    """p unilateral del test de signos: P(X >= a_favor | p=0.5, n).

    Binomial exacta con `math.comb`; sin scipy, que no está en el venv del Pi.
    """
    if n <= 0:
        return 1.0
    cola = sum(math.comb(n, k) for k in range(a_favor, n + 1))
    return cola / (2 ** n)


def veredicto_congelado(e_pub: list[float], e_alt: list[float],
                        corr_mediana: float) -> tuple[str, str, int]:
    """(estado, texto, n_a_favor) — cuándo se descongela una estación.

    ======================= CRITERIO DE REACTIVACIÓN ========================
    Escrito el 2026-09-07 con N=0 y **rehecho el 2026-09-10** tras la auditoría
    externa, todavía con N=3, o sea antes de que ningún dato pudiera opinar.

    Lo que falló en la primera versión, y es la razón de ésta:

      · **El umbral no era un umbral.** Pedía «mejora ≥ 0.50°F de media con
        N=10». Medida sobre los 28 días de KLAX, la desviación de las
        diferencias diarias `|err_pub| − |err_alt|` es **1.96°F**, así que con
        N=10 el error estándar es 0.62°F: el listón valía **0.8 errores
        estándar**. Bajo la hipótesis de que el corrector no aporta nada, se
        cruza el **21% de las veces** en una sola mirada.
      · **Y se miraba todos los días.** El watchdog corre a diario, así que
        aquello no era una mirada sino treinta: parada opcional de manual.
      · **Asimétrico sin razón**: reactivaba con N≥10 y retiraba con N≥20.
      · **El contrafactual se mueve solo.** La corrección sale de la mediana de
        los días previos y la serie sólo guarda 30 días, así que según avanza
        septiembre los días de agosto —los del sesgo grande— se caen de la
        ventana y la corrección se encoge sola. Una «mejora» podía venir de que
        el corrector se apagó, no de que el régimen volviera.

    Lo que se pide ahora, y por qué:

      🟢 REACTIVAR — las TRES:
         (a) **test de signos** p<0.05 unilateral sobre en cuántos días el
             corrector habría dejado menor |err|. Es el contraste que la
             doctrina del proyecto ya usa, y se ajusta solo al N que haya:
             con N=10 hace falta 9 de 10; con N=20, 15 de 20.
         (b) **mejora media ≥ max(0.50°F, 1.645·EE)** — significativa *y*
             material. El primer término evita reactivar por una diferencia
             real pero irrelevante; el segundo, por ruido cuando la estación
             es volátil. El EE se calcula del propio dato, no se supone.
         (c) **|corrección mediana| ≥ 0.50°F** en la fase congelada. Si el
             corrector ya no corrige nada, «gana» sin hacer nada y no hay
             razón para encenderlo.

      🔴 RETIRAR — el espejo exacto de (a) y (b), con el mismo N. Sin asimetría.

      ⚪ IRRELEVANTE — falla (c): el corrector se apagó solo. No es que el
         régimen haya vuelto, es que ya no hay nada que corregir.

      🔵 SIGUE CONGELADA — todo lo demás.

    **Mirar a diario ya no suma oportunidades.** El veredicto se calcula sobre
    los **N primeros** días de la fase, no sobre los últimos ni sobre todos: en
    cuanto hay 10, ese conjunto ya no cambia, y el informe diario repite el
    mismo resultado en vez de tirar otra moneda. La segunda y última ventana es
    N=20, con los 20 primeros. Dos decisiones en total, no treinta.
    """
    n_total = len(e_pub)
    if n_total < N_DECISION:
        return ("congelado_n_bajo",
                f"🔵 CONGELADA — N={n_total}, faltan "
                f"{N_DECISION - n_total} días para decidir", 0)

    usar = N_DECISION_2 if n_total >= N_DECISION_2 else N_DECISION
    pub, alt = e_pub[:usar], e_alt[:usar]
    # d > 0 ⇒ el corrector habría quedado más cerca ese día.
    d = [abs(p) - abs(a) for p, a in zip(pub, alt)]
    no_nulos = [x for x in d if abs(x) > 1e-9]
    a_favor = sum(1 for x in no_nulos if x > 0)
    en_contra = len(no_nulos) - a_favor
    mejora = statistics.mean(d)
    ee = (statistics.stdev(d) / math.sqrt(len(d))) if len(d) > 1 else float("inf")
    listón = max(MEJORA_MIN_F, Z_UNILATERAL_05 * ee)
    p_favor = _p_signos(a_favor, len(no_nulos))
    p_contra = _p_signos(en_contra, len(no_nulos))
    cola = f"(N={usar} primeros; signos {a_favor}/{len(no_nulos)} p={p_favor:.3f}; " \
           f"mejora {mejora:+.2f}°F contra listón {listón:.2f})"

    if abs(corr_mediana) < CORR_MIN_RELEVANTE_F:
        return ("irrelevante",
                f"⚪ IRRELEVANTE — la corrección mediana de la fase es "
                f"{corr_mediana:+.2f}°F: el corrector ya no corrige nada, "
                f"así que ni reactivar ni retirar significan algo {cola}",
                a_favor)
    if p_favor < 0.05 and mejora >= listón:
        return ("reactivar",
                f"🟢 REACTIVAR — el corrector gana de forma significativa y "
                f"material {cola}", a_favor)
    if p_contra < 0.05 and -mejora >= listón:
        return ("retirar",
                f"🔴 RETIRAR del corrector — pierde de forma significativa y "
                f"material {cola}", a_favor)
    return ("congelado",
            f"🔵 SIGUE CONGELADA — no se cumplen las tres condiciones {cola}",
            a_favor)


def estado_de(an, cal, st: str, hora: int = None) -> dict:
    """Estado del corrector en una estación: filas, métricas y veredicto.

    Fuente ÚNICA del criterio de vigilancia — `corrector_watchdog.py` la importa
    en vez de reimplementarlo, para que el umbral no pueda divergir entre lo que
    se mira a mano y lo que alerta sola.
    """
    global HORA
    if hora is not None:
        HORA = hora
    frozen = st in getattr(lc, "FROZEN_STATIONS", set())
    desde = (primer_dia_congelado(an, st) if frozen
             else primer_dia_activo(an, st))
    if desde is None:
        return {"st": st, "estado": "sin_datos", "n": 0, "frozen": frozen,
                "veredicto": ("congelada, aún sin snapshots registrados"
                              if frozen else
                              "nunca aplicó el corrector todavía")}

    dias = [r[0] for r in cal.execute(
        "SELECT date FROM day_outcomes WHERE station_id=? AND date>=? "
        "ORDER BY date", (st, desde)).fetchall()]
    filas = [f for f in (fila_del_dia(an, cal, st, d, frozen) for d in dias)
             if f]
    if not filas:
        return {"st": st, "estado": "sin_settle", "n": 0, "desde": desde,
                "frozen": frozen,
                "veredicto": (f"{'congelada' if frozen else 'activo'} desde "
                              f"{desde}, aún sin días con settle")}

    e_pub = [f["pub"] - f["settle"] for f in filas]
    e_alt = [f["alt"] - f["settle"] for f in filas]
    m_pub = statistics.mean([abs(e) for e in e_pub])
    m_alt = statistics.mean([abs(e) for e in e_alt])
    corr_med = statistics.median([f["corr"] for f in filas])
    racha = None
    if frozen:
        estado, v, neg = veredicto_congelado(e_pub, e_alt, corr_med)
    else:
        estado, v, neg = veredicto_por_signos(e_pub, corr_med, m_pub, m_alt)
        racha = racha_en_contra(e_pub, corr_med)

    pend = REVISIONES_PENDIENTES.get(st)
    return {"st": st, "estado": estado, "veredicto": v, "desde": desde,
            "frozen": frozen,
            "n": len(filas), "filas": filas, "neg": neg, "corr_med": corr_med,
            "racha": racha,
            "n_reciente": len(e_pub[-10:]), "m_pub": m_pub, "m_sin": m_alt,
            "revision_debida": bool(pend and len(filas) >= pend[0]),
            "revision_n": pend[0] if pend else None,
            "revision_motivo": pend[1] if pend else None}


def render(e: dict) -> str:
    """Bloque de texto de una estación. Compartido por el script y el watchdog."""
    if e["estado"] in ("sin_datos", "sin_settle"):
        return f"── {e['st']}: {e['veredicto']}\n"
    fz = e.get("frozen")
    cab = "CONGELADA desde" if fz else "corrector activo desde"
    # En congelado `pub` es la predicción SIN corregir —lo que de verdad salió—
    # y `alt` es lo que el corrector habría dado. Al revés que en activo.
    col = "con" if fz else "sin"
    out = [f"── {e['st']}  ({cab} {e['desde']}, N={e['n']})",
           f"   {'día':12s} {'settle':>7s} {'pub':>7s} {col:>7s} "
           f"{'corr':>6s} {'Δpub':>6s} {'Δ' + col:>6s} {'Δmk':>6s}"]
    for f in e["filas"]:
        dmk = f"{f['mk'] - f['settle']:+6.1f}" if f["mk"] is not None else "     —"
        out.append(f"   {f['dia']:12s} {f['settle']:7.1f} {f['pub']:7.1f} "
                   f"{f['alt']:7.1f} {f['corr']:+6.2f} "
                   f"{f['pub'] - f['settle']:+6.1f} "
                   f"{f['alt'] - f['settle']:+6.1f} {dmk}")
    out.append(f"   |error| medio   publicado {e['m_pub']:.2f}   "
               f"{'con corrector' if fz else 'sin corrector'} {e['m_sin']:.2f}"
               f"   ({e['m_pub'] - e['m_sin']:+.2f})")
    lado = "negativos" if e.get("corr_med", 0.0) >= 0 else "positivos"
    if fz:
        out.append(f"   signo: {e['neg']} de los últimos {e['n_reciente']} con "
                   f"el sesgo del lado que la corrección arregla "
                   f"(no-{lado}; corrección mediana "
                   f"{e.get('corr_med', 0.0):+.2f}°F)")
    else:
        out.append(f"   signo: {e['neg']} de los últimos {e['n_reciente']} en contra "
                   f"de la corrección ({lado}; corrección mediana "
                   f"{e.get('corr_med', 0.0):+.2f}°F)")
        if e.get("racha") is not None:
            out.append(f"   racha: {e['racha']} de {K_SOSTENIDO} miradas "
                       f"seguidas con >={UMBRAL_CONTRA}/{VENTANA_SIGNOS} "
                       f"en contra (hace falta {K_SOSTENIDO} para disparar)")
    if e.get("revision_debida"):
        out.append(f"   🔔 REVISIÓN DEBIDA (N≥{e['revision_n']}): "
                   f"{e['revision_motivo']}")
    elif e.get("revision_n"):
        out.append(f"   ⏳ revisión pendiente al llegar a N={e['revision_n']} "
                   f"(faltan {e['revision_n'] - e['n']} días)")
    out.append(f"   {e['veredicto']}\n")
    return "\n".join(out)


def main() -> int:
    global HORA
    if len(sys.argv) > 1:
        HORA = int(sys.argv[1])
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    print(f"Corrector de nivel — seguimiento a las {HORA}h local "
          f"(hora de decisión)\n")
    for st in sorted(lc.ENABLED_STATIONS):
        print(render(estado_de(an, cal, st)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
