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


def _mk_dbs(tmp_path, monkeypatch, dias, hora=None):
    """dias = [(date_str, ens_med, settle)] -> escribe ambas DBs.

    `hora` = hora local del snapshot sembrado; por defecto la de referencia
    del backtest (peak_lo - HOURS_BEFORE_PEAK).
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
    ref_h = (PEAK_HOURS[ST][0] - lc.HOURS_BEFORE_PEAK) if hora is None else hora
    for ds, ens, settle in dias:
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        ts = (datetime.combine(d, datetime.min.time(), tz)
              + timedelta(hours=ref_h)).astimezone(timezone.utc)
        an.execute("INSERT INTO station_snapshots (ts, station, ens_med, "
                   "bias_f, bias_applied) VALUES (?,?,?,?,?)",
                   (ts.strftime("%Y-%m-%dT%H:%M:%S"), ST, ens, 0.0, 0))
        cal.execute("INSERT INTO day_outcomes VALUES (?,?,?)", (ST, ds, settle))
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
    """KDCA en particular NO debe entrar: es donde el backtest empeora."""
    assert lc.bias_info_for("KPHX", date(2026, 7, 10)) is None
    assert lc.bias_info_for("KDCA", date(2026, 7, 10)) is None
    assert lc.ENABLED_STATIONS == {"KLAX", "KSFO"}


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
    _mk_dbs(tmp_path, monkeypatch, dias)
    info = lc.bias_info_for(ST, date(2026, 7, 10))
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
