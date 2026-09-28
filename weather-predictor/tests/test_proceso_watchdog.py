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
    """Encadena respuestas de curl: True = responde, False = timeout."""
    pendientes = list(resultados)

    def falso_run(cmd, **kw):
        if pendientes.pop(0):
            return subprocess.CompletedProcess(cmd, 0)
        raise subprocess.CalledProcessError(28, cmd)  # 28 = timeout en curl

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
