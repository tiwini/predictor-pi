"""El piso recuerda el máximo del feed de 5 min (2026-08-21).

Lo que se protege: un máximo NO PUEDE BAJAR. Antes el piso miraba
`current_temp_f` instantáneo, así que KMDW usó su pico de 80.6°F mientras duró y
volvió a 78.08 al enfriarse. Backtest pre-registrado en
`investigacion/backtest_piso_max5min.py`.
"""
import predictor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class _Snap:
    """Lo mínimo que `obs_floor_from_snapshot` mira."""
    def __init__(self, **kw):
        self.today_max_obs = kw.get("max_obs", -999.0)
        self.today_max_cli = kw.get("cli")
        self.today_max_asos_6h = kw.get("asos")
        self.today_max_asos_6h_ts = kw.get("asos_ts")
        self.current_temp_f = kw.get("current")
        self.today_max_5min = kw.get("max5")
        self.station_local = datetime.now(ZoneInfo("America/Chicago"))


def test_el_pico_no_se_evapora_al_enfriarse():
    """El caso KMDW: marcó 80.6, bajó a 78.8, y max_obs se quedó en 78.08."""
    piso = predictor.obs_floor_from_snapshot(
        _Snap(max_obs=78.08, current=78.8, max5=80.6))
    assert piso == 80.6 - predictor.CURRENT_FLOOR_MARGIN_F
    assert piso > 78.08, "el piso NO puede caer por debajo del pico ya visto"


def test_sin_max5_cae_al_instantaneo_de_siempre():
    """Snapshots viejos y estaciones excluidas no pierden la guarda que tenían."""
    piso = predictor.obs_floor_from_snapshot(
        _Snap(max_obs=70.0, current=78.8, max5=None))
    assert piso == 78.8 - predictor.CURRENT_FLOOR_MARGIN_F


def test_max_obs_sigue_ganando_cuando_es_mayor():
    piso = predictor.obs_floor_from_snapshot(
        _Snap(max_obs=90.0, current=78.8, max5=80.6))
    assert piso == 90.0


def test_kmsp_excluida_por_el_backtest():
    """El criterio pre-registrado mandó excluir la estación, no la feature."""
    assert "KMSP" in predictor.MAX5MIN_EXCLUDED
    assert len(predictor.MAX5MIN_EXCLUDED) == 1, (
        "añadir o quitar una estación aquí exige su propio backtest")


def test_el_margen_de_cuantizacion_se_aplica_igual():
    """0.9°F no es defensa contra picos: es que el feed llega en °C enteros."""
    assert predictor.CURRENT_FLOOR_MARGIN_F == 0.9
    piso = predictor.obs_floor_from_snapshot(_Snap(max_obs=-999.0, max5=80.6))
    assert abs(piso - 79.7) < 1e-9


def test_consulta_del_maximo_corrido_no_revienta_sin_db(tmp_path, monkeypatch):
    """Si analysis.db no está, devuelve None en vez de tumbar el snapshot."""
    monkeypatch.setattr(predictor, "_MAX5MIN_CACHE", {})
    monkeypatch.setattr(predictor, "Path", predictor.Path)
    ayer = datetime.now(timezone.utc) - timedelta(days=400)
    assert predictor.today_max_current_f("KNOPE", ayer) is None
