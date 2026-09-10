"""Corrector de nivel por mediana causal.

Lo que más importa aquí es que sea CAUSAL: si llegara a mirar el día que está
prediciendo, el backtest que lo justifica (1.94 → 1.31°F) sería una ilusión de
look-ahead. El test de causalidad siembra un día futuro con un sesgo enorme y
comprueba que no lo toca.
"""
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import level_corrector as lc  # noqa: E402
from stations import STATION_TZ, PEAK_HOURS  # noqa: E402

ST = "KLAX"
# KLAX está CONGELADA desde el 2026-09-07: se le calcula la corrección pero no
# se le aplica. Los tests que comprueban que la corrección SALE necesitan una
# estación activa, y se toma del propio roster para que congelar otra mañana no
# los rompa ni, peor, los deje pasando por el motivo equivocado.
ST_ACTIVA = sorted(lc.ENABLED_STATIONS - lc.FROZEN_STATIONS)[0]


def _mk_dbs(tmp_path, monkeypatch, dias, hora=None, st=None):
    """dias = [(date_str, ens_med, settle)] -> escribe ambas DBs.

    `hora` = hora local del snapshot sembrado; por defecto la de referencia
    del backtest (peak_lo - HOURS_BEFORE_PEAK).
    `st` = estación sembrada; por defecto KLAX.
    """
    st = st or ST
    an_p = tmp_path / "analysis.db"
    cal_p = tmp_path / "calibration.db"
    an = sqlite3.connect(an_p)
    an.execute("""CREATE TABLE station_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, station TEXT,
        ens_med REAL, bias_f REAL, bias_applied INTEGER)""")
    cal = sqlite3.connect(cal_p)
    cal.execute("""CREATE TABLE day_outcomes (
        station_id TEXT, date TEXT, max_obs_f REAL)""")

    tz = ZoneInfo(STATION_TZ[st])
    ref_h = (PEAK_HOURS[st][0] - lc.HOURS_BEFORE_PEAK) if hora is None else hora
    for ds, ens, settle in dias:
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        ts = (datetime.combine(d, datetime.min.time(), tz)
              + timedelta(hours=ref_h)).astimezone(timezone.utc)
        an.execute("INSERT INTO station_snapshots (ts, station, ens_med, "
                   "bias_f, bias_applied) VALUES (?,?,?,?,?)",
                   (ts.strftime("%Y-%m-%dT%H:%M:%S"), st, ens, 0.0, 0))
        cal.execute("INSERT INTO day_outcomes VALUES (?,?,?)", (st, ds, settle))
    an.commit(); cal.commit(); an.close(); cal.close()

    monkeypatch.setattr(lc, "DB_PATH", an_p)
    monkeypatch.setattr(lc, "CAL_DB_PATH", cal_p)
    lc.clear_cache()
    return an_p, cal_p


@pytest.fixture(autouse=True)
def _limpia_cache():
    lc.clear_cache()
    yield
    lc.clear_cache()


def test_mediana_de_sesgos_pasados(tmp_path, monkeypatch):
    """Sesgo constante de +3 en 6 días previos -> corrección +3."""
    dias = [(f"2026-07-{d:02d}", 83.0, 80.0) for d in range(1, 7)]
    _mk_dbs(tmp_path, monkeypatch, dias)
    med, n = lc.median_level_bias(ST, date(2026, 7, 10))
    assert n == 6
    assert med == pytest.approx(3.0, abs=0.01)


def test_es_causal_ignora_el_futuro(tmp_path, monkeypatch):
    """Un día POSTERIOR con sesgo gigante no puede influir.

    Si este test falla, el backtest que justifica el corrector está viciado por
    look-ahead y el corrector no vale nada.
    """
    dias = [(f"2026-07-{d:02d}", 83.0, 80.0) for d in range(1, 7)]
    dias.append(("2026-07-20", 120.0, 80.0))    # sesgo +40 en el futuro
    _mk_dbs(tmp_path, monkeypatch, dias)
    med, n = lc.median_level_bias(ST, date(2026, 7, 10))
    assert n == 6, "no debe contar días posteriores a `today`"
    assert med == pytest.approx(3.0, abs=0.01)


def test_historia_insuficiente_devuelve_none(tmp_path, monkeypatch):
    dias = [(f"2026-07-{d:02d}", 83.0, 80.0) for d in range(1, 4)]   # 3 < 5
    _mk_dbs(tmp_path, monkeypatch, dias)
    med, n = lc.median_level_bias(ST, date(2026, 7, 10))
    assert med is None and n == 3


def test_corrección_absurda_se_ignora(tmp_path, monkeypatch):
    """Por encima de MAX_ABS_CORRECTION_F cae al EWMA en vez de aplicarla."""
    dias = [(f"2026-07-{d:02d}", 130.0, 80.0) for d in range(1, 7)]  # +50
    _mk_dbs(tmp_path, monkeypatch, dias)
    med, _ = lc.median_level_bias(ST, date(2026, 7, 10))
    assert med is None


def test_solo_estaciones_habilitadas():
    """KDCA en particular NO debe entrar: es donde el backtest empeora.

    El roster se asserta entero a propósito: cada estación entra con su propio
    backtest pre-registrado, así que añadir una sin tocar este test —y sin la
    corrida que lo justifique— debe romper.
    """
    assert lc.bias_info_for("KPHX", date(2026, 7, 10)) is None
    assert lc.bias_info_for("KDCA", date(2026, 7, 10)) is None
    # KNYC añadida 2026-08-15: backtest_corrector_knyc.py, N=16 frescos.
    # KMIA añadida 2026-08-28: backtest_corrector_kmia_klas.py, N=24, y es la
    # primera con corrección NEGATIVA.
    # KLAS añadida 2026-08-28 pero SÓLO 9-13h: ventana_horaria_klas.py.
    # KHOU añadida 2026-09-01: backtest_corrector_nivel8.py, 1 de 8 candidatas.
    assert lc.ENABLED_STATIONS == {"KLAX", "KSFO", "KNYC", "KMIA", "KLAS", "KHOU"}
    # Las que el mismo backtest rechazó NO deben colarse.
    for st in ("KBOS", "KDEN", "KOKC", "KPHL", "KMSP", "KMSY"):
        assert st not in lc.ENABLED_STATIONS
    assert lc.ENABLED_HOURS == {"KLAS": (9, 13)}
    # Congeladas 2026-09-07 tras el giro de régimen de septiembre. Siguen en
    # ENABLED —el corrector se les calcula y se registra— pero no corrigen.
    assert lc.FROZEN_STATIONS == {"KLAX", "KSFO"}
    assert lc.FROZEN_STATIONS <= lc.ENABLED_STATIONS, \
        "una congelada fuera del roster no se calcularía, y la vigilancia " \
        "se quedaría sin nada que medir"


def test_congelada_calcula_pero_no_aplica(tmp_path, monkeypatch):
    """El congelado tiene que dejar el número a la vista y la predicción quieta.

    Si `bias_info_for` devolviera None, `predictor` caería al bias_tracker y
    nadie registraría lo que el corrector habría hecho: al descongelar no
    habría con qué decidir. Y si devolviera applied=True, seguiría corrigiendo.
    """
    dias = [(f"2026-07-{d:02d}", 83.0, 80.0) for d in range(1, 7)]
    _mk_dbs(tmp_path, monkeypatch, dias, st="KLAX")
    info = lc.bias_info_for("KLAX", date(2026, 7, 10))
    assert info is not None, "una congelada no puede caer al bias_tracker"
    assert info["applied"] is False
    assert info["frozen"] is True
    assert info["bias"] == pytest.approx(3.0, abs=0.01), \
        "el valor se sigue calculando: es lo que se mide en sombra"
    assert info["bias_path"] == "median_causal", \
        "primer_dia_activo lee de aquí la fecha de alta histórica"
    assert "CONGELADA" in info["reason"]


def test_activa_no_se_marca_congelada(tmp_path, monkeypatch):
    """El flag no puede contagiarse al resto del roster."""
    dias = [(f"2026-07-{d:02d}", 83.0, 80.0) for d in range(1, 7)]
    _mk_dbs(tmp_path, monkeypatch, dias, st=ST_ACTIVA)
    info = lc.bias_info_for(ST_ACTIVA, date(2026, 7, 10))
    assert info["applied"] is True
    assert info["frozen"] is False


def test_ventana_horaria_de_klas():
    """KLAS lleva el corrector a ratos: fuera de 9-13h no aplica.

    Las horas 14-17 están medidas y el corrector EMPEORA allí, así que el gate
    no es cosmético: es la mitad de la decisión.
    """
    for h in (9, 10, 12, 13):
        assert lc.hora_habilitada("KLAS", h), f"{h}h debería estar dentro"
    for h in (0, 8, 14, 17, 23):
        assert not lc.hora_habilitada("KLAS", h), f"{h}h debería estar fuera"


def test_sin_ventana_todas_las_horas_valen():
    for h in (0, 9, 14, 23):
        assert lc.hora_habilitada("KLAX", h)
        assert lc.hora_habilitada("KMIA", h)


def test_hora_none_es_la_de_referencia_del_backtest():
    """`local_hour=None` significa peak_lo−2, que en KLAS son las 12h: dentro.

    Si el gate tratara None como 'fuera', el backtest y cualquier herramienta
    que no pase hora dejarían de ver el corrector sin decir por qué.
    """
    assert lc.hora_habilitada("KLAS", None)


def test_fuera_de_ventana_no_corrige_pero_si_calcula(monkeypatch):
    """Fuera de su ventana horaria KLAS no aplica, pero **sí** consulta.

    Hasta el 2026-09-10 este test exigía lo contrario: que el gate cortara
    antes de tocar las DBs. Se invirtió a propósito. Sin calcular el valor no
    queda rastro de lo que el corrector habría hecho en esas horas, y la
    revisión de KLAS con N≥40 tendría que reconstruirlo con una réplica — el
    mismo callejón que dejó sin decidir el barrido de ventanas del 08-28, donde
    la réplica erraba 0.46°F sobre un efecto de 0.24. El valor se registra en
    `bias_frozen_f` y no toca la predicción.

    Lo que el test sigue protegiendo: que fuera de ventana **no se corrija**, y
    que un fallo al leer la DB devuelva None en vez de reventar.
    """
    llamadas = []
    monkeypatch.setattr(lc, "median_level_bias",
                        lambda *a, **k: llamadas.append(a) or (-2.0, 25))
    info = lc.bias_info_for("KLAS", date(2026, 7, 10), local_hour=14)
    assert len(llamadas) == 1, "hace falta el valor para poder registrarlo"
    assert info is not None
    assert info["applied"] is False, "fuera de ventana NO se corrige"
    assert info["frozen"] is True


def test_sin_historia_sigue_devolviendo_none(monkeypatch):
    """Si la consulta no da valor, None — no un cero disfrazado de corrección."""
    monkeypatch.setattr(lc, "median_level_bias", lambda *a, **k: (None, 0))
    assert lc.bias_info_for("KLAS", date(2026, 7, 10), local_hour=14) is None
    assert lc.bias_info_for("KLAS", date(2026, 7, 10), local_hour=11) is None

def test_deshace_el_bias_aplicado_ese_dia(tmp_path, monkeypatch):
    """El sesgo se mide sobre el ensemble CRUDO, no sobre el ya corregido.

    Si el sistema restó 2°F aquel día, el ens_med guardado es 2 más bajo de lo
    que el modelo dijo; sin sumarlo de vuelta se subestima el sesgo real.
    """
    an_p = tmp_path / "analysis.db"
    cal_p = tmp_path / "calibration.db"
    an = sqlite3.connect(an_p)
    an.execute("""CREATE TABLE station_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, station TEXT,
        ens_med REAL, bias_f REAL, bias_applied INTEGER)""")
    cal = sqlite3.connect(cal_p)
    cal.execute("""CREATE TABLE day_outcomes (
        station_id TEXT, date TEXT, max_obs_f REAL)""")
    tz = ZoneInfo(STATION_TZ[ST])
    ref_h = PEAK_HOURS[ST][0] - lc.HOURS_BEFORE_PEAK
    for d in range(1, 7):
        ds = f"2026-07-{d:02d}"
        dd = datetime.strptime(ds, "%Y-%m-%d").date()
        ts = (datetime.combine(dd, datetime.min.time(), tz)
              + timedelta(hours=ref_h)).astimezone(timezone.utc)
        # publicado 81 tras restar 2 => crudo 83; settle 80 => sesgo real +3
        an.execute("INSERT INTO station_snapshots (ts, station, ens_med, "
                   "bias_f, bias_applied) VALUES (?,?,?,?,?)",
                   (ts.strftime("%Y-%m-%dT%H:%M:%S"), ST, 81.0, 2.0, 1))
        cal.execute("INSERT INTO day_outcomes VALUES (?,?,?)", (ST, ds, 80.0))
    an.commit(); cal.commit(); an.close(); cal.close()
    monkeypatch.setattr(lc, "DB_PATH", an_p)
    monkeypatch.setattr(lc, "CAL_DB_PATH", cal_p)
    lc.clear_cache()
    med, n = lc.median_level_bias(ST, date(2026, 7, 10))
    assert n == 6
    assert med == pytest.approx(3.0, abs=0.01), \
        "debe medir el sesgo del ensemble crudo, no el residuo post-corrección"


def test_bias_info_usa_la_clave_que_lee_el_poller(tmp_path, monkeypatch):
    """`analysis_poller` hace bi.get("bias_path"). Con otra clave la telemetría
    queda a NULL y dentro de una semana no se puede medir si el corrector
    ayudó — que es justo para lo que se encendió."""
    dias = [(f"2026-07-{d:02d}", 83.0, 80.0) for d in range(1, 7)]
    _mk_dbs(tmp_path, monkeypatch, dias, st=ST_ACTIVA)
    info = lc.bias_info_for(ST_ACTIVA, date(2026, 7, 10))
    assert info is not None
    assert info["bias_path"] == "median_causal"
    assert info["applied"] is True
    assert info["bias"] == pytest.approx(3.0, abs=0.01)


def test_condiciona_por_hora_local(tmp_path, monkeypatch):
    """El sesgo decae durante el día: pedir otra hora debe dar otro valor.

    Se siembran dos horas con sesgos distintos (+6 por la mañana, +1 por la
    tarde). Pedir la hora de tarde no puede devolver el sesgo matinal — aplicar
    el de la mañana por la tarde sobre-corregía 2.3°F en KLAX.
    """
    an_p = tmp_path / "analysis.db"
    cal_p = tmp_path / "calibration.db"
    an = sqlite3.connect(an_p)
    an.execute("""CREATE TABLE station_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, station TEXT,
        ens_med REAL, bias_f REAL, bias_applied INTEGER)""")
    cal = sqlite3.connect(cal_p)
    cal.execute("""CREATE TABLE day_outcomes (
        station_id TEXT, date TEXT, max_obs_f REAL)""")
    tz = ZoneInfo(STATION_TZ[ST])
    for d in range(1, 7):
        ds = f"2026-07-{d:02d}"
        dd = datetime.strptime(ds, "%Y-%m-%d").date()
        for h, ens in ((9, 86.0), (17, 81.0)):        # settle 80 -> +6 y +1
            ts = (datetime.combine(dd, datetime.min.time(), tz)
                  + timedelta(hours=h)).astimezone(timezone.utc)
            an.execute("INSERT INTO station_snapshots (ts, station, ens_med, "
                       "bias_f, bias_applied) VALUES (?,?,?,?,?)",
                       (ts.strftime("%Y-%m-%dT%H:%M:%S"), ST, ens, 0.0, 0))
        cal.execute("INSERT INTO day_outcomes VALUES (?,?,?)", (ST, ds, 80.0))
    an.commit(); cal.commit(); an.close(); cal.close()
    monkeypatch.setattr(lc, "DB_PATH", an_p)
    monkeypatch.setattr(lc, "CAL_DB_PATH", cal_p)
    lc.clear_cache()

    manana, _ = lc.median_level_bias(ST, date(2026, 7, 10), local_hour=9)
    tarde, _ = lc.median_level_bias(ST, date(2026, 7, 10), local_hour=17)
    assert manana == pytest.approx(6.0, abs=0.01)
    assert tarde == pytest.approx(1.0, abs=0.01), \
        "la tarde no puede heredar el sesgo de la mañana"


# ─── guard: no hundir la mediana bajo el piso observado ──────────────────

def _ahora(st, hora, minuto=0):
    from datetime import datetime as _dt
    return _dt(2026, 8, 14, hora, minuto, tzinfo=ZoneInfo(STATION_TZ[st]))


def test_recorta_si_hunde_la_mediana_bajo_el_piso():
    """KLAX 08-14: mediana 76.9, piso 75.2, corrección +2.90 -> quedaría 74.0.

    Se recorta a 1.7 para dejar la mediana justo en el piso, en vez de aplicar
    la corrección entera y que el clamp la rescate.
    """
    maxes = [76.0, 76.5, 76.9, 77.5, 78.0]
    b, why = lc.cap_by_floor(2.90, maxes, 75.2, "KLAX", _ahora("KLAX", 13))
    assert b == pytest.approx(76.9 - 75.2, abs=0.01)
    assert why is not None and "piso" in why


def test_no_toca_nada_pasado_el_pico():
    """Tras el pico, clavar en el piso da error 0.00° medido. No se toca."""
    maxes = [76.0, 76.5, 76.9, 77.5, 78.0]
    # ventana de KLAX es 12-15h; a las 16h ya cerró
    b, why = lc.cap_by_floor(2.90, maxes, 75.2, "KLAX", _ahora("KLAX", 16))
    assert b == pytest.approx(2.90)
    assert why is None


def test_no_recorta_si_no_hunde():
    maxes = [80.0, 81.0, 82.0, 83.0, 84.0]
    b, why = lc.cap_by_floor(2.0, maxes, 75.0, "KLAX", _ahora("KLAX", 13))
    assert b == pytest.approx(2.0) and why is None


def test_correccion_fria_no_se_toca():
    """Un bias negativo SUMA a la predicción; nunca la hunde bajo el piso."""
    maxes = [76.0, 76.9, 78.0]
    b, why = lc.cap_by_floor(-2.0, maxes, 75.2, "KLAX", _ahora("KLAX", 13))
    assert b == pytest.approx(-2.0) and why is None


def test_sin_piso_no_se_toca():
    maxes = [76.0, 76.9, 78.0]
    b, why = lc.cap_by_floor(2.9, maxes, None, "KLAX", _ahora("KLAX", 13))
    assert b == pytest.approx(2.9) and why is None


def test_nunca_devuelve_correccion_negativa():
    """Si la mediana ya está bajo el piso, el recorte es 0, no negativo:
    el guard limita el corrector, no lo convierte en su contrario."""
    maxes = [70.0, 71.0, 72.0]
    b, why = lc.cap_by_floor(2.9, maxes, 75.2, "KLAX", _ahora("KLAX", 13))
    assert b == 0.0 and why is not None


# ─── EWMA jubilado, corrector vivo ───────────────────────────────────────

def test_ewma_jubilado_pero_el_calculo_se_conserva():
    """El flag no apaga `compute_bias` — la lógica sigue cubierta por sus
    propios tests y el valor sirve de telemetría. Lo que se corta es que
    `predictor` lo aplique."""
    import bias_tracker as bt
    assert bt.EWMA_RETIRED is True
    assert hasattr(bt, "compute_bias")


def test_el_corrector_de_nivel_no_queda_afectado(tmp_path, monkeypatch):
    """La jubilación del EWMA no puede llevarse por delante el corrector, que
    llega por otra vía y sí está validado."""
    dias = [(f"2026-07-{d:02d}", 83.0, 80.0) for d in range(1, 7)]
    _mk_dbs(tmp_path, monkeypatch, dias, st=ST_ACTIVA)
    info = lc.bias_info_for(ST_ACTIVA, date(2026, 7, 10))
    assert info is not None
    assert info["applied"] is True
    assert info["bias_path"] == "median_causal"
