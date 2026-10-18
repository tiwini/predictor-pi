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

crypto :8001 se reporta y no se toca: lo reinicia systemd con Restart=always, y
la memoria manda `systemctl`, nunca kill+nohup.

weather :8000 SÍ se relanza desde el 2026-10-18. Antes sólo se reportaba, con el
argumento de que `start_weather_with_retry` tiene espera de DNS y backoff «que
no se replica aquí a ciegas» — cierto, y por eso ahora **no se replica: se
llama**. start_all.sh pasó a poder cargarse sin arrancar la pila, así que el
criterio vive en un solo sitio. Lo que cambió de opinión fue el 10-10: el aviso
salió 131 veces durante 27 horas, :8000 sólo volvió con el reinicio del Pi, y
2h45m después de que la red ya estuviera bien. «Se nota al abrir la web» supone
que alguien la abre.

Y antes de intervenir se pregunta DE QUIÉN es la avería (añadido el 2026-10-18,
tras el apagón del 10-10): si el DNS no resuelve o api.weather.gov no responde,
matar el poller no arregla nada y relanzarlo devuelve un proceso que tampoco
puede escribir. Aquel día esto lo mató y lo relanzó ~15 veces durante 25 horas.
Detectar funcionaba —12 minutos—; el remedio no correspondía a la avería.

Notifica desde el 2026-10-18, y hasta entonces no: el canal estaba pausado y
esto sólo dejaba rastro en proceso_watchdog.log. Los dos apagones que cazó —el
del 09-22 y el del 10-10— los diagnosticó bien y no se enteró nadie, porque
nadie lee un log por gusto. Empuja los cuatro sucesos que piden una mano
—poller relanzado, fuente caída, servicio HTTP mudo, y la vuelta de los dos
últimos— con la cadencia que ya tenían los contadores, no una por ejecución.
Si no hay ~/.config/ntfy.env, sigue siendo un no-op silencioso.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
# ── Relanzar :8000 solo (2026-10-18) ────────────────────────────────────────
# Hasta hoy esto reportaba y te dejaba el trabajo: «relanzar con start_all.sh:
# start_weather_with_retry». El 10-10 lo dijo 131 veces durante 27 horas y
# :8000 sólo volvió con el reinicio del Pi — y 2h45m DESPUÉS de que la red ya
# estuviera bien, así que no era la red: el proceso estaba atascado.
#
# No se replica la lógica: se CARGA start_all.sh y se llama a su función, que
# ya sabe esperar al DNS y reintentar con backoff. Para eso start_all.sh pasó a
# poder sourcearse sin arrancar la pila. Dos copias del mismo criterio acaban
# divergiendo, y aquí ya hay precedente (`corrector_watchdog` importa el
# criterio en vez de reimplementarlo).
#
# crypto :8001 NO se toca: lo reinicia systemd con Restart=always, y la memoria
# manda `systemctl restart crypto-predictor`, nunca kill+nohup.
START_ALL = RAIZ / "start_all.sh"
PUERTO_WEATHER = 8000
# Más largo que el de los pollers: levantar el web puede tardar minutos entre
# la espera de API y los 5 intentos, y pisarle el arranque sería peor que
# esperar. 30 min = 6 comprobaciones del cron.
COOLDOWN_WEB_MIN = 30.0
# Segundos que se le dan a cada señal para que suelte el puerto. 5 bastan de
# sobra para un Flask sano; el atascado se lleva el SIGKILL.
ESPERA_PUERTO_S = 5

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


# ── ntfy (2026-10-18) ───────────────────────────────────────────────────────
# Hasta hoy esto no notificaba: el canal estaba pausado a propósito y el
# docstring lo decía. El resultado se vio dos veces — el apagón del 09-22 tardó
# 5 días en notarse y el del 10-10 nadie lo vio nunca— y la causa no fue la
# medida sino el empujón, igual que con KHOU y con KSFO en el corrector.
#
# Mismo mecanismo que `corrector_watchdog`: el topic vive en ~/.config/ntfy.env,
# fuera del repo y con permisos 600, y si el fichero no está esto es un no-op
# silencioso. Así los tests no empujan nada sin tener que simular la red.
#
# La cadencia NO la pone el push: la ponen los contadores que ya existían
# (3 fallos consecutivos para avisar, recordatorio cada 12). Un push por
# ejecución serían 288 al día.
NTFY_ENV = Path.home() / ".config" / "ntfy.env"


def _topic() -> str:
    try:
        for ln in NTFY_ENV.read_text(encoding="utf-8").splitlines():
            if ln.startswith("NTFY_TOPIC="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def push(titulo: str, mensaje: str, prioridad: str = "high",
         tags: str = "warning") -> bool:
    """Empuja a ntfy si hay topic. Devuelve si se envió.

    El TÍTULO viaja en una cabecera HTTP, y las cabeceras son latin-1: un
    emoji ahí levanta `UnicodeEncodeError` dentro de http.client y el push no
    sale. Se descubrió al escribir estos tests, con títulos tipo «🔴 Poller
    relanzado» — y es el mismo patrón que lleva en `corrector_watchdog` desde
    agosto, donde nunca se notó porque no había topic que probarlo. El emoji
    va en `Tags`, que ntfy convierte en icono, y en el cuerpo, que sí es UTF-8.

    Y no levanta pase lo que pase: un vigilante que se cae al avisar deja de
    vigilar, que es peor que no avisar. Por eso el `except` es ancho a
    propósito — el bug de arriba no era ni URLError ni OSError.
    """
    topic = _topic()
    if not topic:
        return False
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=mensaje.encode("utf-8"),
                headers={"Title": titulo.encode("ascii", "ignore").decode().strip(),
                         "Priority": prioridad, "Tags": tags},
            ), timeout=10)
        return True
    except Exception as e:                                  # noqa: BLE001
        log(f"el push a ntfy falló: {type(e).__name__}: {e}")
        return False


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
        push(f"Sin fuente: {script}",
             f"{edad_txt} y {motivo}.\n\nNo es el Pi: el poller está sano y "
             f"sin datos que leer. No se toca hasta que vuelva la red.\n"
             f"Van {veces} comprobaciones ({veces * 5} min).",
             prioridad="high", tags="globe_with_meridians")


def _limpiar_causa_externa(estado: dict, script: str) -> None:
    clave = f"externo:{script}"
    if estado.get(clave):
        veces = estado[clave]
        log(f"{script}: la causa externa se despejó tras {veces} "
            f"comprobaciones")
        if veces >= RECORDATORIO_EXTERNO_CADA:
            push(f"Vuelve la fuente: {script}",
                 f"La red volvió tras {veces} comprobaciones "
                 f"({veces * 5} min). El watchdog retoma lo suyo.",
                 prioridad="default", tags="white_check_mark")
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
        push("Poller relanzado",
             f"{script} llevaba {edad:.0f} min sin escribir con la red OK, "
             f"así que el problema era local. Terminado y relanzado.\n\n"
             f"Si se repite, no es un cuelgue suelto: mira "
             f"weather-predictor/analysis_poller.log.",
             prioridad="high", tags="rotating_light")
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


def pid_en_puerto(puerto: int) -> int | None:
    """PID del proceso que ESCUCHA en ese puerto, según `ss -ltnp`."""
    try:
        salida = subprocess.run(["ss", "-ltnp"], capture_output=True,
                                text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for linea in salida.splitlines():
        if f":{puerto} " not in linea:
            continue
        m = re.search(r"pid=(\d+)", linea)
        if m:
            return int(m.group(1))
    return None


def pid_del_weather() -> int | None:
    """El que escucha en :8000 **y además** corre bajo weather-predictor/.

    Las dos condiciones, no una. weather y crypto corren los dos
    `predictor_web.py` y matar por nombre se lleva el que no era — es la
    trampa que ya documentaba `pids_de`, y aquí da más miedo porque esto mata
    solo. El puerto dice QUIÉN es y el cmdline CONFIRMA que es el nuestro; si
    no coinciden, se devuelve None y no se toca nada.
    """
    pid = pid_en_puerto(PUERTO_WEATHER)
    if pid is None:
        return None
    proc = Path("/proc") / str(pid)
    try:
        cmdline = (proc / "cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    if "predictor_web.py" not in cmdline:
        log(f"en :{PUERTO_WEATHER} escucha el PID {pid} y no es predictor_web "
            f"— no se toca")
        return None
    # La RUTA, por cwd o por cmdline, cualquiera de las dos. No es paranoia
    # repetida: según quién lo arrancara, el mismo proceso se ve distinto.
    #   start_weather_with_retry ->  cwd=.../weather-predictor
    #                                cmdline="./venv/bin/python3 predictor_web.py"
    #   arrancado a mano desde ~  ->  cwd=/home/popeye
    #                                cmdline con las rutas absolutas
    # Mirando sólo el cmdline, el que relanza este mismo watchdog quedaba sin
    # reconocer, y la próxima vez que se atascara no habría a quién matar: se
    # habría lanzado un segundo que ni siquiera puede enlazar el puerto. Se vio
    # en la prueba en vivo del 2026-10-18, en el intento siguiente al primero
    # que funcionó.
    try:
        cwd = str((proc / "cwd").resolve())
    except OSError:
        cwd = ""
    if cwd != str(WEATHER) and str(WEATHER) not in cmdline:
        log(f"en :{PUERTO_WEATHER} escucha el PID {pid}, que no es de "
            f"{WEATHER} (cwd={cwd or '?'}) — no se toca")
        return None
    return pid


def _liberar_puerto(pid: int) -> bool:
    """Termina al atascado y **espera a que suelte el puerto**.

    No basta con mandar la señal y seguir: mientras el puerto siga enlazado,
    el proceso nuevo muere al arrancar («address already in use») y
    `start_weather_with_retry` quema sus cinco intentos contra un puerto que
    no está libre. Y un proceso de verdad atascado puede tardar en atender el
    SIGTERM, o no atenderlo: por eso hay SIGKILL detrás. Un watchdog que pide
    por favor no es un watchdog.
    """
    for señal, nombre in ((15, "SIGTERM"), (9, "SIGKILL")):
        try:
            os.kill(pid, señal)
        except ProcessLookupError:
            return True                      # ya no está: puerto libre
        except OSError as e:
            log(f"no se pudo mandar {nombre} al PID {pid}: {e}")
            return False
        log(f"weather :{PUERTO_WEATHER} PID {pid}: {nombre} (atascado)")
        for _ in range(ESPERA_PUERTO_S):
            time.sleep(1)
            if pid_en_puerto(PUERTO_WEATHER) != pid:
                log(f"weather :{PUERTO_WEATHER} libre tras {nombre}")
                return True
    log(f"weather :{PUERTO_WEATHER} sigue enlazado por el PID {pid} tras "
        f"SIGKILL — no se arranca otro encima")
    return False


# El PID del último intento, para no pisarlo en la ejecución siguiente. Es una
# lista y no un global suelto porque así el test lo puede leer sin importar el
# orden en que corran.
_ultimo_pid_recuperacion = [0]


def relanzar_weather() -> bool:
    """Mata el :8000 atascado y lanza la recuperación, en segundo plano.

    En segundo plano porque `start_weather_with_retry` puede tardar hasta
    media hora esperando a la API, y esto corre en un cron de 5 minutos:
    bloquear aquí encadenaría ejecuciones solapadas.

    Y hay que matarlo ANTES: la función empieza con `if is_up 8000; return`,
    así que con el proceso atascado seguiría adelante y lanzaría otro que
    moriría al no poder enlazar el puerto. Su kill interno sólo alcanza al
    que ella misma arrancó.
    """
    if not START_ALL.exists():
        log(f"ERROR: no existe {START_ALL}")
        return False
    pid = pid_del_weather()
    if pid is not None and not _liberar_puerto(pid):
        return False
    if pid is None:
        log(f"weather :{PUERTO_WEATHER} no tiene a nadie escuchando: se arranca")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", f'source "{START_ALL}" && start_weather_with_retry'],
            cwd=str(RAIZ), stdout=(RAIZ / "boot.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as e:
        log(f"ERROR al lanzar la recuperación de :{PUERTO_WEATHER}: {e}")
        return False
    _ultimo_pid_recuperacion[0] = proc.pid
    log(f"recuperación de :{PUERTO_WEATHER} lanzada (PID {proc.pid}, "
        f"start_weather_with_retry, ver boot.log)")
    return True


def recuperacion_en_curso(estado: dict) -> bool:
    """¿Hay ya un intento corriendo? Dura minutos; no se le pisa."""
    pid = estado.get("recuperacion_web_pid")
    if not pid:
        return False
    return (Path("/proc") / str(pid)).exists()


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
                         else "se relanza solo con start_weather_with_retry")
                log(f"ATENCIÓN: {nombre} :{puerto}{ruta} lleva {fallos} "
                    f"comprobaciones {sintoma_curl(rc)} — {quien}")
                push(f"{nombre} :{puerto} no responde",
                     f"{fallos} comprobaciones ({fallos * 5} min) "
                     f"{sintoma_curl(rc)}.\n\n{quien}.",
                     prioridad="high", tags="rotating_light")
                incidencia = True
            # El relanzamiento va por su cuenta, no colgado del aviso: el aviso
            # se repite cada hora y aquí manda el cooldown de 30 min. Sólo
            # :8000 — crypto es de systemd.
            if (puerto == PUERTO_WEATHER
                    and fallos >= FALLOS_HTTP_PARA_AVISAR
                    and not recuperacion_en_curso(estado)
                    and not en_cooldown(estado, "weather_web")):
                if relanzar_weather():
                    ahora = datetime.now(timezone.utc).isoformat()
                    estado["ultimo_relanzamiento:weather_web"] = ahora
                    estado["recuperacion_web_pid"] = _ultimo_pid_recuperacion[0]
                    push("weather :8000 relanzado solo",
                         f"Llevaba {fallos * 5} min {sintoma_curl(rc)}.\n\n"
                         f"Matado el proceso atascado y lanzado "
                         f"start_weather_with_retry, que espera a la API y "
                         f"reintenta con backoff. Siguiente intento posible en "
                         f"{COOLDOWN_WEB_MIN:.0f} min.",
                         prioridad="high", tags="rotating_light")
                    incidencia = True
        else:
            if previos:
                # Sólo se anota la recuperación de algo que llegó a avisarse;
                # si no, un pico suelto dejaría dos líneas en vez de cero.
                if previos >= FALLOS_HTTP_PARA_AVISAR:
                    log(f"{nombre} :{puerto} vuelve a responder tras {previos} fallos")
                    push(f"{nombre} :{puerto} responde",
                         f"Vuelve tras {previos} comprobaciones "
                         f"({previos * 5} min).",
                         prioridad="default", tags="white_check_mark")
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
