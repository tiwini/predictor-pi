#!/usr/bin/env python3
"""Vigila que los pollers sigan VIVOS y ESCRIBIENDO, y los relanza si no.

Nace del apagón silencioso del 2026-09-22: `analysis_poller` murió en seco a las
16:42 AST (sin traceback, a mitad de `polling KHOU`) y nadie se enteró en 26h27m.
Las 20 estaciones perdieron el día; el 09-23 quedó con 80 filas en vez de ~2470.
No falló ninguna guarda: es que no había ninguna. `start_all.sh` sólo actúa en
`@reboot`, `crypto-predictor` tiene `Restart=always` de systemd, y el cron de
cada minuto (`agent_monitor.py`) lleva `interval_min=off` desde junio y además
es el agente de análisis, no un vigilante.

DOS fallos distintos, y el segundo es el que justifica mirar el dato:
  1. el proceso muere  -> lo ve `pgrep`
  2. el proceso vive pero no escribe -> `pgrep` dice que todo va bien

Por eso el criterio de `analysis_poller` es la FRESCURA DE LO ESCRITO, que cubre
los dos. El umbral sale del dato, no de la intuición: sobre 3638 gaps de 30 días
el p50 es 11.5 min, el p999 17.4, y sólo hay dos por encima de 25 (uno benigno
de 29.0 el 09-07 y el apagón, 1603.4). 40 min no habría dado un falso positivo
en 30 días y habría cazado el apagón en 40 minutos.

`kalshi_fast_poller` se vigila SÓLO por proceso, a propósito: no escribe 24/7
—de 02h a 14h UTC no hay mercados y la tabla no crece—, así que un criterio de
frescura lo declararía muerto cada madrugada.

weather :8000 y crypto :8001 se REPORTAN pero no se tocan. crypto ya lo reinicia
systemd (y la memoria manda `systemctl`, nunca kill+nohup); weather se relanza
con `start_weather_with_retry`, que tiene lógica de reintento y de espera de DNS
que no se replica aquí a ciegas. Un caído de :8000 se nota al abrir la web; el
poller caído es invisible, y ese es justo el agujero que esto cierra.

Y antes de intervenir se pregunta DE QUIÉN es la avería (añadido el 2026-10-18,
tras el apagón del 10-10): si el DNS no resuelve o api.weather.gov no responde,
matar el poller no arregla nada y relanzarlo devuelve un proceso que tampoco
puede escribir. Aquel día esto lo mató y lo relanzó ~15 veces durante 25 horas.
Detectar funcionaba —12 minutos—; el remedio no correspondía a la avería.

NO notifica: las alertas están pausadas a propósito (NTFY_TOPIC=""). Deja rastro
en proceso_watchdog.log y nada más.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
WEATHER = RAIZ / "weather-predictor"
ANALYSIS_DB = WEATHER / "analysis.db"
LOG = RAIZ / "proceso_watchdog.log"
ESTADO = RAIZ / "proceso_watchdog_estado.json"

# Umbral derivado de la distribución real de gaps (ver docstring).
FRESCURA_MAX_MIN = 40.0
# No relanzar lo mismo más de una vez por ventana: si algo está roto de fondo,
# un bucle de reinicios lo empeora y llena el log de ruido.
COOLDOWN_MIN = 15.0

# Qué se sondea en cada servicio (2026-10-18). Antes era `/` en los dos, que en
# weather renderiza la home entera; ahora se pide la sonda más barata que cada
# uno tenga, porque lo que se quiere saber es si el proceso ATIENDE, no si la
# página sale. Medido el 10-18 con 40 muestras por endpoint:
#
#   weather /           p50 0.176s   p90 0.181   max 0.229
#   weather /api/ping   p50 0.003s   p90 0.005   max 0.008   ← 22x más barata
#   crypto  /           p50 0.209s   p90 0.293   max 0.332
#
# crypto sigue con `/` porque no tiene endpoint de salud, y añadírselo obligaría
# a reiniciarlo para una mejora de 0.2s. Queda anotado, no hecho.
SONDA_HTTP = {8000: "/api/ping", 8001: "/"}

# El timeout ya NO es a ojo, que es como estaba y por lo que dio 3 de 3 falsos
# en sus primeras 17 horas. Lo que puede retrasar legítimamente a la sonda no es
# su propio coste —3 ms— sino quedarse encolada detrás de una petición pesada,
# porque Flask las sirve en un solo hilo. Así que el techo lo fija la petición
# MÁS LENTA que el servidor puede estar atendiendo cuando llega la sonda, barrida
# entera el 10-18:
#
#   /calibration                4.61s   ← la página más lenta
#   /api/peak-status/refresh    7.50s   ← el endpoint más lento, y el techo
#   el resto de las 10 rutas    <0.22s
#
# 20s son 2.7x ese techo, así que el número se queda — pero ahora se queda
# porque está medido, no porque nadie lo mirara.
TIMEOUT_HTTP_S = 20
# Y sobre todo: el veredicto ya no cuelga sólo del número. curl distingue «no
# hay nadie escuchando» (7) de «escucha y no contesta» (28), que son dos averías
# distintas y sólo la segunda depende del timeout.
CURL_SIN_CONEXION = 7
CURL_TIMEOUT = 28

# ── Guarda de causa externa (2026-10-18) ────────────────────────────────────
# El apagón del 10-10: el poller pasó 25h sin escribir porque el DNS no resolvía
# api.weather.gov (1115 fallos de resolución en su log), y esto lo mató y lo
# relanzó ~15 veces seguidas sin arreglar nada, porque el proceso nunca estuvo
# colgado. Detectar funcionó —12 minutos—; el remedio no correspondía a la
# avería.
#
# `start_all.sh` ya tenía la barrera (`wait_dns` + `wait_api_reachable`) desde el
# apagón del 08-08, pero `lanzar()` replica a mano el arranque de start_all.sh
# —lo dice su propio docstring— y se dejó la comprobación fuera. Esto la trae.
#
# Aquí no se ESPERA, se mira una vez: el cron pasa cada 5 minutos y la siguiente
# pasada vuelve a preguntar. Y sólo se mira cuando ya hay algo que arreglar, así
# que el camino sano no paga ni una petición de más.
DNS_HOST = "api.weather.gov"
API_URL = "https://api.weather.gov/"
TIMEOUT_UPSTREAM_S = 10          # el mismo -m 10 de wait_api_reachable
RECORDATORIO_EXTERNO_CADA = 12   # una línea por hora mientras dure, no doce
# Y aun así el umbral solo no basta: se exigen fallos CONSECUTIVOS, porque un
# timeout aislado no es una caída.
FALLOS_HTTP_PARA_AVISAR = 3      # ~15 min con el cron cada 5
RECORDATORIO_CADA = 12           # si sigue caído, una línea por hora, no 12


def log(msg: str) -> None:
    linea = f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} [watchdog] {msg}"
    print(linea)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(linea + "\n")
    except OSError:
        pass


def _estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar(estado: dict) -> None:
    try:
        ESTADO.write_text(json.dumps(estado, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"no se pudo guardar el estado: {e}")


def en_cooldown(estado: dict, clave: str) -> bool:
    ts = estado.get(f"ultimo_relanzamiento:{clave}")
    if not ts:
        return False
    try:
        transcurrido = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 60
    except ValueError:
        return False
    return transcurrido < COOLDOWN_MIN


def pids_de(script: str) -> list[int]:
    """PIDs cuyo cmdline invoca ESE script.

    No se usa `pgrep -f <nombre>` a secas por la trampa conocida del roster:
    weather y crypto corren los dos `predictor_web.py` y matar por nombre se
    lleva el que no era. Aquí se lee /proc/<pid>/cmdline y se exige que el
    script aparezca como componente propio, no como subcadena.
    """
    encontrados = []
    for entrada in Path("/proc").iterdir():
        if not entrada.name.isdigit():
            continue
        try:
            partes = (entrada / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        except OSError:
            continue  # el proceso murió mientras lo mirábamos
        if any(re.fullmatch(rf"(.*/)?{re.escape(script)}", p) for p in partes if p):
            encontrados.append(int(entrada.name))
    return encontrados


def edad_dato_min() -> float | None:
    """Minutos desde el último snapshot escrito, o None si no se puede leer."""
    if not ANALYSIS_DB.exists():
        log(f"ATENCIÓN: no existe {ANALYSIS_DB}")
        return None
    try:
        con = sqlite3.connect(f"file:{ANALYSIS_DB}?mode=ro", uri=True, timeout=10)
        try:
            fila = con.execute("SELECT MAX(ts) FROM station_snapshots").fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log(f"ATENCIÓN: no se pudo leer analysis.db: {e}")
        return None
    if not fila or not fila[0]:
        return None
    try:
        ultimo = datetime.fromisoformat(fila[0])
    except ValueError:
        return None
    if ultimo.tzinfo is None:
        ultimo = ultimo.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ultimo).total_seconds() / 60


def dns_ok(host: str = DNS_HOST) -> bool:
    """`getent hosts`, igual que `wait_dns` en start_all.sh."""
    try:
        return subprocess.run(["getent", "hosts", host],
                              capture_output=True, timeout=TIMEOUT_UPSTREAM_S
                              ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def api_ok(url: str = API_URL) -> bool:
    """Alcance REAL, no sólo resolución: una caché de DNS puede contestar por un
    host que no responde. Es la distinción que ya hacía start_all.sh."""
    try:
        return subprocess.run(
            ["curl", "-sf", "-o", "/dev/null", "-m", str(TIMEOUT_UPSTREAM_S), url],
            capture_output=True, timeout=TIMEOUT_UPSTREAM_S + 5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def causa_externa(con_api: bool = True) -> str | None:
    """Motivo por el que relanzar NO puede arreglar nada, o None.

    `con_api=False` para los pollers que no viven de api.weather.gov: ahí sólo
    vale el DNS como señal de «no hay red», y cargarle la caída de un tercero
    sería dejarlo sin vigilancia por una avería ajena.
    """
    if not dns_ok():
        return f"el DNS no resuelve {DNS_HOST}"
    if con_api and not api_ok():
        return f"{API_URL} no responde"
    return None


def _anotar_causa_externa(estado: dict, script: str, edad_txt: str,
                          motivo: str) -> None:
    """Una línea al entrar y una por hora mientras dure. Un apagón de 25h con
    una línea cada 5 minutos son 300 líneas que nadie lee."""
    clave = f"externo:{script}"
    veces = int(estado.get(clave, 0)) + 1
    estado[clave] = veces
    if veces == 1 or veces % RECORDATORIO_EXTERNO_CADA == 0:
        log(f"{script}: {edad_txt} pero {motivo} — la causa NO es local, "
            f"no se mata ni se relanza (van {veces} comprobaciones)")


def _limpiar_causa_externa(estado: dict, script: str) -> None:
    clave = f"externo:{script}"
    if estado.get(clave):
        log(f"{script}: la causa externa se despejó tras {estado[clave]} "
            f"comprobaciones")
        estado[clave] = 0


def lanzar(script: str) -> bool:
    """Relanza un poller igual que lo hace start_all.sh: mismo cwd, mismo venv,
    mismo log. Se replica en vez de hacer `source` porque start_all.sh arranca
    la pila entera al cargarse."""
    venv = WEATHER / "venv/bin/python3"
    if not venv.exists():
        log(f"ERROR: no existe el venv {venv}")
        return False
    destino = WEATHER / script
    if not destino.exists():
        log(f"ERROR: no existe {destino}")
        return False
    try:
        with (WEATHER / f"{Path(script).stem}.log").open("a", encoding="utf-8") as salida:
            proc = subprocess.Popen(
                [str(venv), script],
                cwd=str(WEATHER),
                stdout=salida,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # sobrevive a la muerte del cron
            )
    except OSError as e:
        log(f"ERROR al lanzar {script}: {e}")
        return False
    log(f"{script} relanzado (PID {proc.pid})")
    return True


def parar(pids: list[int], script: str) -> None:
    """Termina un proceso colgado. Sólo se llama con PIDs que ya se han
    confirmado como ESE script leyendo su cmdline."""
    for pid in pids:
        try:
            os.kill(pid, 15)
            log(f"{script} PID {pid} terminado (colgado, no escribía)")
        except OSError as e:
            log(f"no se pudo terminar {script} PID {pid}: {e}")


def revisar_analysis_poller(estado: dict) -> bool:
    """Vivo Y escribiendo. Devuelve True si tuvo que intervenir."""
    script = "analysis_poller.py"
    pids = pids_de(script)
    edad = edad_dato_min()
    if edad is None:
        log(f"{script}: sin lectura de frescura, no se decide nada")
        return False

    fresco = edad <= FRESCURA_MAX_MIN
    if pids and fresco:
        _limpiar_causa_externa(estado, script)
        return False  # el caso normal: silencio

    # Antes de tocar nada: ¿es nuestro el problema? Si la fuente no está, matar
    # el poller no lo arregla y relanzarlo devuelve un proceso que tampoco puede
    # escribir. Es la lección del 10-10, y por eso va ANTES del cooldown: no es
    # que haya que esperar para reintentar, es que no hay que reintentar.
    motivo = causa_externa()
    if motivo:
        _anotar_causa_externa(estado, script, f"dato de hace {edad:.1f} min",
                              motivo)
        return False
    _limpiar_causa_externa(estado, script)

    if en_cooldown(estado, script):
        log(f"{script}: degradado (pids={pids or 'ninguno'}, dato de hace "
            f"{edad:.1f} min) pero en cooldown, no se toca")
        return False

    if not pids:
        log(f"{script} NO CORRE — último dato hace {edad:.1f} min")
    else:
        log(f"{script} vivo (PID {pids[0]}) pero el último dato es de hace "
            f"{edad:.1f} min (umbral {FRESCURA_MAX_MIN:.0f}): colgado")
        parar(pids, script)

    if lanzar(script):
        estado[f"ultimo_relanzamiento:{script}"] = datetime.now(timezone.utc).isoformat()
    return True


def revisar_kalshi_fast(estado: dict) -> bool:
    """Sólo existencia: su tabla no crece fuera de la ventana de mercado."""
    script = "kalshi_fast_poller.py"
    if pids_de(script):
        _limpiar_causa_externa(estado, script)
        return False
    # Sin api.weather.gov: éste vive de Kalshi, así que sólo el DNS dice «no hay
    # red». Achacarle la caída de un tercero lo dejaría sin vigilancia.
    motivo = causa_externa(con_api=False)
    if motivo:
        _anotar_causa_externa(estado, script, "no corre", motivo)
        return False
    _limpiar_causa_externa(estado, script)
    if en_cooldown(estado, script):
        log(f"{script}: caído pero en cooldown, no se toca")
        return False
    log(f"{script} NO CORRE")
    if lanzar(script):
        estado[f"ultimo_relanzamiento:{script}"] = datetime.now(timezone.utc).isoformat()
    return True


def sintoma_curl(rc: int) -> str:
    """Qué avería es, no sólo que falló.

    Son dos cosas distintas y piden respuestas distintas: sin conexión el
    proceso no está escuchando y hay que arrancarlo; con timeout está
    escuchando y atascado, y hay que mirar en qué. El 10-10 fueron 131
    comprobaciones seguidas de «sin responder» sin decir cuál de las dos era,
    y resultó ser la segunda: :8000 siguió sin contestar 2h45m DESPUÉS de que
    el DNS volviera, y sólo lo arregló el reinicio.
    """
    if rc == CURL_SIN_CONEXION:
        return "sin aceptar conexión (no hay nadie escuchando en el puerto)"
    if rc == CURL_TIMEOUT:
        return (f"aceptando la conexión y sin contestar en {TIMEOUT_HTTP_S}s "
                f"(vivo pero atascado)")
    return f"fallando (curl {rc})"


def revisar_servicios_web(estado: dict) -> bool:
    """Reporta, no actúa. Ver el docstring del módulo.

    Exige fallos CONSECUTIVOS a propósito. La primera versión avisaba al primer
    timeout con `-m 8`, y en sus primeras 17 horas escribió tres avisos de
    «:8000 NO responde» con el proceso vivo desde hacía cuatro días y
    respondiendo en 0.17s: eran picos de latencia, no caídas. Un log de
    diagnóstico en el que todas las líneas son falsas deja de ser diagnóstico
    —el mismo defecto que los tests que escribían en el log de producción—, y
    el umbral se había puesto a ojo mientras el del poller se derivaba del dato.
    """
    incidencia = False
    for nombre, puerto in (("weather", 8000), ("crypto", 8001)):
        clave = f"fallos_http:{puerto}"
        previos = int(estado.get(clave, 0))
        ruta = SONDA_HTTP.get(puerto, "/")
        try:
            rc = subprocess.run(
                ["curl", "-sf", "-o", "/dev/null", "-m", str(TIMEOUT_HTTP_S),
                 f"http://127.0.0.1:{puerto}{ruta}"],
                capture_output=True).returncode
        except (OSError, subprocess.SubprocessError):
            rc = -1
        if rc != 0:
            fallos = previos + 1
            estado[clave] = fallos
            sostenido = fallos - FALLOS_HTTP_PARA_AVISAR
            if sostenido == 0 or (sostenido > 0 and sostenido % RECORDATORIO_CADA == 0):
                quien = ("lo reinicia systemd (Restart=always)" if puerto == 8001
                         else "relanzar con start_all.sh: start_weather_with_retry")
                log(f"ATENCIÓN: {nombre} :{puerto}{ruta} lleva {fallos} "
                    f"comprobaciones {sintoma_curl(rc)} — {quien}")
                incidencia = True
        else:
            if previos:
                # Sólo se anota la recuperación de algo que llegó a avisarse;
                # si no, un pico suelto dejaría dos líneas en vez de cero.
                if previos >= FALLOS_HTTP_PARA_AVISAR:
                    log(f"{nombre} :{puerto} vuelve a responder tras {previos} fallos")
                    incidencia = True
                estado[clave] = 0
    return incidencia


def main() -> int:
    estado = _estado()
    antes = json.dumps(estado, sort_keys=True)
    actuo = False
    actuo |= revisar_analysis_poller(estado)
    actuo |= revisar_kalshi_fast(estado)
    actuo |= revisar_servicios_web(estado)
    # Por el CONTENIDO y no por `actuo`: el contador de fallos consecutivos sube
    # sin que haya nada que reportar todavía, y si no se persistiera nunca
    # llegaría a tres. La versión anterior guardaba `{}` por el motivo opuesto.
    if json.dumps(estado, sort_keys=True) != antes:
        _guardar(estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())
