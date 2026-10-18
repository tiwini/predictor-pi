"""El watchdog de procesos (2026-09-27, tras el apagón silencioso del 09-22).

El apagón no lo causó una guarda mal calibrada: no había guarda. `analysis_poller`
murió en seco y las 20 estaciones perdieron 26h27m sin que nada lo notara. Lo que
estos tests fijan es que la guarda nueva distingue los tres estados que importan,
porque confundirlos es lo que la haría inútil o peligrosa:

1. Sano       -> no toca nada. Un watchdog que reinicia de más es peor que ninguno.
2. Muerto     -> lo ve por `pgrep`.
3. Colgado    -> el proceso VIVE y no escribe. `pgrep` lo da por bueno; sólo la
                 frescura del dato lo delata, y es el caso que justifica mirar
                 la DB en vez de la tabla de procesos.

Y el umbral se comprueba contra el dato real que lo fijó: p999 = 17.4 min sobre
3638 gaps de 30 días, con un único gap benigno de 29.0 min (2026-09-07) que NO
debe disparar, y el apagón de 1603.4 que SÍ.

Todo se siembra relativo a `now()`: un test con fecha literal caduca solo y
empieza a mentir sin que nadie lo toque.
"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import proceso_watchdog as W  # noqa: E402


@pytest.fixture(autouse=True)
def _log_aislado(tmp_path, monkeypatch):
    """Ningún test escribe en el log ni en el estado de producción.

    La primera versión de esta suite dejó ocho líneas de test en
    `proceso_watchdog.log` del Pi: un archivo de diagnóstico en el que no se
    puede confiar deja de ser diagnóstico.
    """
    monkeypatch.setattr(W, "LOG", tmp_path / "watchdog.log")
    monkeypatch.setattr(W, "ESTADO", tmp_path / "estado.json")
    # Ni la red. `causa_externa` sale a por DNS y a por api.weather.gov; sin
    # esto la suite daría verde o rojo según cómo ande la línea, que es la
    # definición de test inútil. Los tests de la guarda lo sobrescriben.
    monkeypatch.setattr(W, "causa_externa", lambda con_api=True: None)
    # Ni ntfy. Apuntando a un fichero que no existe, `push` es un no-op y
    # ningún test manda una notificación de verdad al móvil. Sin esto la suite
    # leía ~/.config/ntfy.env y empujaba al topic real: se vio al encender
    # ntfy, cuando la suite pasó de 0.46s a 6.43s porque estaba saliendo a la
    # red en cada test.
    monkeypatch.setattr(W, "NTFY_ENV", tmp_path / "ntfy.env")


def _db_con_ultimo_snapshot(tmp_path, hace_minutos):
    """analysis.db mínima cuyo último snapshot es de hace `hace_minutos`."""
    ruta = tmp_path / "analysis.db"
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE station_snapshots (id INTEGER PRIMARY KEY, ts TEXT, station TEXT)")
    ahora = datetime.now(timezone.utc)
    # Una serie normal cada ~12 min que termina en el instante pedido.
    for i in range(12):
        ts = ahora - timedelta(minutes=hace_minutos + 12 * i)
        con.execute("INSERT INTO station_snapshots (ts, station) VALUES (?, ?)",
                    (ts.isoformat(), "KHOU"))
    con.commit()
    con.close()
    return ruta


# --- la frescura mide lo que dice -------------------------------------------

@pytest.mark.parametrize("hace_minutos", [0, 11, 29, 41, 1603])
def test_edad_dato_devuelve_la_edad_sembrada(tmp_path, monkeypatch, hace_minutos):
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, hace_minutos))
    edad = W.edad_dato_min()
    assert edad == pytest.approx(hace_minutos, abs=1.0)


def test_sin_db_no_decide_nada(tmp_path, monkeypatch):
    """Si no se puede leer la fuente, el watchdog se calla en vez de reiniciar
    a ciegas: un error de lectura no es prueba de que el poller esté muerto."""
    monkeypatch.setattr(W, "ANALYSIS_DB", tmp_path / "no_existe.db")
    assert W.edad_dato_min() is None
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_analysis_poller({}) is False
    assert lanzados == []


# --- el umbral, contra los gaps reales que lo fijaron ------------------------

def test_el_gap_benigno_de_29_min_no_dispara(tmp_path, monkeypatch):
    """2026-09-07 04:48 -> 05:17 = 29.0 min, el mayor gap sano en 30 días."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 29))
    monkeypatch.setattr(W, "pids_de", lambda s: [4242])
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_analysis_poller({}) is False
    assert lanzados == []


def test_el_apagon_del_09_22_si_dispara(tmp_path, monkeypatch):
    """1603.4 min. El caso que motivó todo esto."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1603))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_analysis_poller({}) is True
    assert lanzados == ["analysis_poller.py"]


def test_umbral_coherente_con_el_p999_medido(tmp_path, monkeypatch):
    """El umbral tiene que dejar holgura sobre el p999 real (17.4 min) y sobre
    el máximo benigno (29.0). Si alguien lo baja a 20, esto lo para."""
    assert W.FRESCURA_MAX_MIN > 29.0


# --- el caso que pgrep no ve -------------------------------------------------

def test_vivo_pero_sin_escribir_se_reinicia(tmp_path, monkeypatch):
    """Proceso VIVO y dato viejo: `pgrep` lo daría por sano. Hay que pararlo
    antes de relanzar, o quedan dos pollers escribiendo la misma tabla."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 90))
    monkeypatch.setattr(W, "pids_de", lambda s: [4242])
    parados, lanzados = [], []
    monkeypatch.setattr(W, "parar", lambda pids, s: parados.extend(pids))
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_analysis_poller({}) is True
    assert parados == [4242], "un colgado hay que terminarlo, no duplicarlo"
    assert lanzados == ["analysis_poller.py"]


def test_sano_no_se_toca(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 3))
    monkeypatch.setattr(W, "pids_de", lambda s: [4242])
    tocado = []
    monkeypatch.setattr(W, "parar", lambda pids, s: tocado.append("parar"))
    monkeypatch.setattr(W, "lanzar", lambda s: tocado.append("lanzar") or True)
    assert W.revisar_analysis_poller({}) is False
    assert tocado == []


# --- el cooldown evita el bucle ---------------------------------------------

def test_cooldown_frena_el_segundo_reinicio(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1603))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    estado = {}
    assert W.revisar_analysis_poller(estado) is True
    assert W.revisar_analysis_poller(estado) is False, "no debe reiniciar en bucle"
    assert lanzados == ["analysis_poller.py"]


def test_cooldown_caduca(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1603))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    monkeypatch.setattr(W, "lanzar", lambda s: True)
    viejo = datetime.now(timezone.utc) - timedelta(minutes=W.COOLDOWN_MIN + 1)
    estado = {"ultimo_relanzamiento:analysis_poller.py": viejo.isoformat()}
    assert W.revisar_analysis_poller(estado) is True


# --- la trampa del roster: nunca matar por nombre ----------------------------

def test_pids_de_no_confunde_por_subcadena():
    """weather y crypto corren los DOS `predictor_web.py`; el proyecto ya se
    quemó matando por nombre. `pids_de` exige el componente completo, así que
    un sufijo compartido no puede arrastrar al proceso que no era."""
    guion = Path(__file__).resolve().parent / "_wd_predictor_web.py"
    guion.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(guion)], stdin=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if proc.pid in W.pids_de("_wd_predictor_web.py"):
                break
            time.sleep(0.1)
        else:
            pytest.fail("pids_de no encontró un proceso que sí corre")
        assert proc.pid not in W.pids_de("web.py"), "una subcadena no debe casar"
        assert proc.pid not in W.pids_de("predictor_web.py"), "ni un sufijo"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        guion.unlink(missing_ok=True)


def test_kalshi_fast_no_usa_frescura(tmp_path, monkeypatch):
    """Su tabla no crece de 02h a 14h UTC (sin mercados). Si se le aplicara el
    criterio de frescura, lo declararía muerto cada madrugada."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1603))
    monkeypatch.setattr(W, "pids_de", lambda s: [777])
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_kalshi_fast({}) is False
    assert lanzados == [], "vivo basta: no se mira el dato"


# --- un pico de latencia no es una caída ------------------------------------

def _curl_que(monkeypatch, resultados):
    """Encadena respuestas de curl: True = responde, False = timeout (28).

    Un entero en vez del booleano fija el código exacto, que desde el
    2026-10-18 cambia el diagnóstico: 7 es «no hay nadie escuchando» y 28 es
    «escucha y no contesta».

    Devuelve `CompletedProcess` con su returncode en lugar de lanzar, que es
    lo que curl hace de verdad: la versión que lanzaba `CalledProcessError`
    venía de cuando la llamada usaba `check=True`, y simulaba el envoltorio en
    vez de la herramienta.
    """
    pendientes = list(resultados)

    def falso_run(cmd, **kw):
        r = pendientes.pop(0)
        rc = 0 if r is True else (28 if r is False else int(r))
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(W.subprocess, "run", falso_run)


def test_un_pico_suelto_no_avisa(monkeypatch):
    """El caso real: 3 avisos falsos en 17h con el proceso vivo 4 días y
    respondiendo en 0.17s. Un timeout aislado no puede escribir en el log."""
    _curl_que(monkeypatch, [False, True])  # weather falla, crypto responde
    estado = {}
    assert W.revisar_servicios_web(estado) is False
    assert estado["fallos_http:8000"] == 1, "cuenta, pero no avisa"


def test_avisa_al_tercer_fallo_consecutivo(monkeypatch):
    estado = {}
    for esperado in (False, False, True):
        _curl_que(monkeypatch, [False, True])
        assert W.revisar_servicios_web(estado) is esperado
    assert estado["fallos_http:8000"] == 3


def test_no_repite_el_aviso_cada_cinco_minutos(monkeypatch):
    """Una caída larga deja una línea por hora, no una cada ejecución."""
    estado = {}
    avisos = 0
    for _ in range(15):
        _curl_que(monkeypatch, [False, True])
        avisos += bool(W.revisar_servicios_web(estado))
    assert avisos == 2, f"esperaba el aviso inicial y un recordatorio, hubo {avisos}"


def test_la_racha_se_rompe_al_responder(monkeypatch):
    estado = {}
    for _ in range(2):
        _curl_que(monkeypatch, [False, True])
        W.revisar_servicios_web(estado)
    _curl_que(monkeypatch, [True, True])
    assert W.revisar_servicios_web(estado) is False
    assert estado["fallos_http:8000"] == 0
    # y al volver a fallar arranca de cero, sin heredar la racha vieja
    _curl_que(monkeypatch, [False, True])
    assert W.revisar_servicios_web(estado) is False


def test_la_recuperacion_solo_se_anota_si_hubo_aviso(monkeypatch):
    """Si el pico nunca llegó a avisarse, su recuperación tampoco se anota:
    dos líneas en el log por algo que no pasó son peor que ninguna."""
    estado = {}
    _curl_que(monkeypatch, [False, True])
    W.revisar_servicios_web(estado)
    _curl_que(monkeypatch, [True, True])
    assert W.revisar_servicios_web(estado) is False


def test_el_contador_se_persiste_aunque_no_haya_aviso(tmp_path, monkeypatch):
    """El bug de la primera versión, al revés: `main` guardaba `{}` cuando no
    hacía falta y no habría guardado el contador, que sin persistir nunca
    llegaría a tres."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 3))
    monkeypatch.setattr(W, "pids_de", lambda s: [4242])
    _curl_que(monkeypatch, [False, True])
    W.main()
    assert json.loads(W.ESTADO.read_text())["fallos_http:8000"] == 1


def test_timeout_con_holgura_sobre_la_cola_medida():
    """p50 0.169s, p90 0.289, máximo 4.27 en 60 medidas. Si alguien lo baja
    a 8s vuelven los falsos positivos que motivaron esto."""
    assert W.TIMEOUT_HTTP_S >= 15


# --- la guarda de causa externa (2026-10-18, tras el apagón del 10-10) -------
#
# Aquel día el DNS dejó de resolver api.weather.gov durante 25 horas (1115
# fallos de resolución en analysis_poller.log). El poller no estaba colgado:
# estaba sano y sin fuente. El watchdog lo mató y lo relanzó ~15 veces, y lo
# único que consiguió fue llenar el log. Lo que estos tests fijan es que la
# pregunta «¿es mía la avería?» se hace ANTES de tocar nada.


def test_con_el_DNS_caido_no_mata_ni_relanza(tmp_path, monkeypatch):
    """El caso del 10-10, exactamente: dato de hace 1502 min y DNS muerto."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1502))
    monkeypatch.setattr(W, "pids_de", lambda s: [4242])
    monkeypatch.setattr(W, "causa_externa",
                        lambda con_api=True: "el DNS no resuelve api.weather.gov")
    tocado = []
    monkeypatch.setattr(W, "parar", lambda pids, s: tocado.append("parar"))
    monkeypatch.setattr(W, "lanzar", lambda s: tocado.append("lanzar") or True)
    assert W.revisar_analysis_poller({}) is False
    assert tocado == [], "matar un proceso sano no devuelve el DNS"


def test_con_la_API_caida_tampoco(tmp_path, monkeypatch):
    """Resolver no es alcanzar: una caché de DNS contesta por un host muerto.
    Es la distinción que start_all.sh ya hacía y que aquí faltaba."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1502))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    monkeypatch.setattr(W, "causa_externa",
                        lambda con_api=True: "https://api.weather.gov/ no responde")
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_analysis_poller({}) is False
    assert lanzados == []


def test_la_guarda_va_antes_del_cooldown(tmp_path, monkeypatch):
    """No es «espera para reintentar», es «no reintentes». Si fuera al revés,
    al caducar el cooldown volvería el bucle de reinicios."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1502))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    monkeypatch.setattr(W, "causa_externa", lambda con_api=True: "sin red")
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    viejo = datetime.now(timezone.utc) - timedelta(minutes=W.COOLDOWN_MIN + 1)
    estado = {"ultimo_relanzamiento:analysis_poller.py": viejo.isoformat()}
    assert W.revisar_analysis_poller(estado) is False
    assert lanzados == []


def test_cuando_vuelve_la_red_se_relanza(tmp_path, monkeypatch):
    """La guarda aplaza, no indulta."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1502))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    estado = {}
    monkeypatch.setattr(W, "causa_externa", lambda con_api=True: "sin red")
    assert W.revisar_analysis_poller(estado) is False
    monkeypatch.setattr(W, "causa_externa", lambda con_api=True: None)
    assert W.revisar_analysis_poller(estado) is True
    assert lanzados == ["analysis_poller.py"]


def test_no_escribe_una_linea_cada_cinco_minutos(tmp_path, monkeypatch):
    """25 horas a una línea cada 5 minutos son 300 líneas que nadie lee."""
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1502))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    monkeypatch.setattr(W, "causa_externa", lambda con_api=True: "sin red")
    monkeypatch.setattr(W, "lanzar", lambda s: True)
    estado = {}
    for _ in range(W.RECORDATORIO_EXTERNO_CADA):
        W.revisar_analysis_poller(estado)
    lineas = W.LOG.read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 2, f"una al entrar y una por hora, no {len(lineas)}"


def test_kalshi_no_se_queda_sin_vigilancia_por_una_caida_ajena(tmp_path, monkeypatch):
    """kalshi_fast vive de Kalshi. Si lo que está caído es api.weather.gov, su
    watchdog tiene que seguir funcionando: `causa_externa(con_api=False)`."""
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    vistos = []
    monkeypatch.setattr(W, "causa_externa",
                        lambda con_api=True: vistos.append(con_api) or None)
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_kalshi_fast({}) is True
    assert vistos == [False], "a kalshi no se le pregunta por api.weather.gov"
    assert lanzados == ["kalshi_fast_poller.py"]


def test_kalshi_con_el_DNS_caido_tampoco_se_relanza(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    monkeypatch.setattr(W, "causa_externa", lambda con_api=True: "el DNS no resuelve")
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_kalshi_fast({}) is False
    assert lanzados == []


# --- la sonda HTTP y lo que significa fallar --------------------------------


def test_la_sonda_de_weather_es_la_barata():
    """`/` renderiza la home (176 ms); `/api/ping` lee memoria (3 ms). Lo que
    se quiere saber es si ATIENDE, no si la página sale."""
    assert W.SONDA_HTTP[8000] == "/api/ping"


def test_el_timeout_cubre_la_peticion_mas_lenta_medida():
    """Barrido del 2026-10-18: /api/peak-status/refresh tarda 7.50s y Flask
    sirve en un hilo, así que la sonda puede encolarse detrás. Si alguien baja
    el timeout por debajo de ese techo, esto lo para."""
    assert W.TIMEOUT_HTTP_S >= 2 * 7.50


def test_curl_distingue_no_escucha_de_no_contesta():
    """Dos averías distintas que piden dos respuestas distintas. El 10-10 el
    log dijo 131 veces «sin responder» sin decir cuál de las dos era."""
    sin_conexion = W.sintoma_curl(W.CURL_SIN_CONEXION)
    atascado = W.sintoma_curl(W.CURL_TIMEOUT)
    assert "nadie escuchando" in sin_conexion
    assert "atascado" in atascado
    assert sin_conexion != atascado


def test_el_aviso_dice_cual_de_las_dos_averias_es(monkeypatch):
    """De punta a punta: el código de curl llega hasta la línea del log."""
    estado = {}
    for _ in range(W.FALLOS_HTTP_PARA_AVISAR):
        _curl_que(monkeypatch, [W.CURL_SIN_CONEXION, True])
        W.revisar_servicios_web(estado)
    assert "nadie escuchando" in W.LOG.read_text(encoding="utf-8")


# --- ntfy (2026-10-18) ------------------------------------------------------
#
# El watchdog diagnosticó bien los dos apagones y no se enteró nadie. Lo que
# estos tests fijan no es que empuje, sino que empuje POCO —un push por
# ejecución serían 288 al día y el canal se volvería a apagar, esta vez de
# hartazgo— y que no se rompa al hacerlo.


def _topic_puesto(monkeypatch, tmp_path):
    f = tmp_path / "ntfy.env"
    f.write_text("NTFY_TOPIC=el-de-pruebas\n", encoding="utf-8")
    monkeypatch.setattr(W, "NTFY_ENV", f)
    enviados = []

    def falso_urlopen(req, timeout=None):
        # Pasa por la codificación de cabeceras de verdad: así un título con
        # emoji vuelve a fallar aquí y no en el móvil.
        for _k, v in req.headers.items():
            str(v).encode("latin-1")
        enviados.append(req.headers.get("Title", ""))

    monkeypatch.setattr(W.urllib.request, "urlopen", falso_urlopen)
    return enviados


def test_sin_fichero_de_topic_no_empuja(tmp_path, monkeypatch):
    """El estado en el que vivió hasta hoy: silencioso y sin romperse."""
    monkeypatch.setattr(W.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("no debía empujar"))
    assert W.push("t", "m") is False


def test_los_titulos_caben_en_una_cabecera_HTTP(tmp_path, monkeypatch):
    """El bug que salió al encender ntfy: las cabeceras son latin-1 y un emoji
    en el Title levanta UnicodeEncodeError dentro de http.client. El icono va
    en Tags, que sí lo admite, y el cuerpo es UTF-8 y aguanta lo que sea."""
    enviados = _topic_puesto(monkeypatch, tmp_path)
    assert W.push("🔴 Poller relanzado", "cuerpo con emoji 🌐 sin problema")
    assert enviados == ["Poller relanzado"]


def test_empuja_cuando_relanza_el_poller(tmp_path, monkeypatch):
    enviados = _topic_puesto(monkeypatch, tmp_path)
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1603))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    monkeypatch.setattr(W, "lanzar", lambda s: True)
    assert W.revisar_analysis_poller({}) is True
    assert len(enviados) == 1 and "Poller relanzado" in enviados[0]


def test_la_fuente_caida_no_empuja_cada_cinco_minutos(tmp_path, monkeypatch):
    """25h de apagón a un push cada 5 min son 300 notificaciones."""
    enviados = _topic_puesto(monkeypatch, tmp_path)
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1502))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    monkeypatch.setattr(W, "causa_externa", lambda con_api=True: "sin red")
    monkeypatch.setattr(W, "lanzar", lambda s: True)
    estado = {}
    for _ in range(W.RECORDATORIO_EXTERNO_CADA):
        W.revisar_analysis_poller(estado)
    assert len(enviados) == 2, f"uno al entrar y uno por hora, no {len(enviados)}"


def test_el_servicio_caido_avisa_al_tercero_y_no_antes(tmp_path, monkeypatch):
    enviados = _topic_puesto(monkeypatch, tmp_path)
    estado = {}
    for i in range(1, W.FALLOS_HTTP_PARA_AVISAR + 1):
        _curl_que(monkeypatch, [W.CURL_SIN_CONEXION, True])
        W.revisar_servicios_web(estado)
        esperados = 0 if i < W.FALLOS_HTTP_PARA_AVISAR else 1
        assert len(enviados) == esperados, f"en la comprobación {i}"


def test_un_push_que_falla_no_tumba_al_watchdog(tmp_path, monkeypatch):
    """Un vigilante que se cae al avisar deja de vigilar, que es peor. El
    `except` es ancho a propósito: el bug del emoji no era URLError ni OSError,
    salía de las tripas de http.client."""
    f = tmp_path / "ntfy.env"
    f.write_text("NTFY_TOPIC=el-de-pruebas\n", encoding="utf-8")
    monkeypatch.setattr(W, "NTFY_ENV", f)

    def revienta(*a, **k):
        raise UnicodeEncodeError("latin-1", "x", 0, 1, "lo que pasó de verdad")

    monkeypatch.setattr(W.urllib.request, "urlopen", revienta)
    monkeypatch.setattr(W, "ANALYSIS_DB", _db_con_ultimo_snapshot(tmp_path, 1603))
    monkeypatch.setattr(W, "pids_de", lambda s: [])
    lanzados = []
    monkeypatch.setattr(W, "lanzar", lambda s: lanzados.append(s) or True)
    assert W.revisar_analysis_poller({}) is True
    assert lanzados == ["analysis_poller.py"], "el relanzamiento es lo que importa"
    assert "el push a ntfy falló" in W.LOG.read_text(encoding="utf-8")
