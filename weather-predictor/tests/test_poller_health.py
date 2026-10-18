"""Vigilancia del `analysis_poller`, el colector de fondo.

Hasta el 2026-08-18 nadie lo vigilaba: `POLL_STATS` cubre sólo el poll loop del
web y su estación activa, así que si el poller moría, `/system?tab=health`
seguía diciendo OK y el hueco se descubría semanas después al correr un
backtest. Es el proceso que llena `analysis.db`, base de todos los backtests y
del corrector de nivel.

Se mide **el dato que llega a la tabla**, no si el proceso vive: uno colgado
sigue apareciendo en `ps` y deja de escribir.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


@pytest.fixture(scope="module")
def pw():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pw_health", BASE / "predictor_web.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"predictor_web no importable: {e}")
    return m


@pytest.fixture
def db(tmp_path, monkeypatch, pw):
    """analysis.db de mentira, en el sitio donde la función la busca."""
    p = tmp_path / "analysis.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE station_snapshots (ts TEXT, station TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(pw, "__file__", str(tmp_path / "predictor_web.py"))
    return p


def _sembrar(p, hace_seg: float, n_estaciones: int):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=hace_seg))
    con = sqlite3.connect(p)
    for i in range(n_estaciones):
        con.execute("INSERT INTO station_snapshots VALUES (?, ?)",
                    (ts.isoformat(), f"K{i:03d}"))
    con.commit()
    con.close()


def test_fresco_y_completo_es_ok(pw, db, monkeypatch):
    monkeypatch.setattr(pw, "SUPPORTED_STATIONS", [f"K{i:03d}" for i in range(20)])
    _sembrar(db, hace_seg=120, n_estaciones=20)
    h = pw._analysis_poller_health()
    assert h["label"] == "OK"
    assert h["cobertura"] == 20


def test_dato_viejo_degrada(pw, db, monkeypatch):
    monkeypatch.setattr(pw, "SUPPORTED_STATIONS", [f"K{i:03d}" for i in range(20)])
    _sembrar(db, hace_seg=3 * 600, n_estaciones=20)   # 3× intervalo
    assert pw._analysis_poller_health()["label"] == "WARN"


def test_poller_muerto_es_bad(pw, db, monkeypatch):
    monkeypatch.setattr(pw, "SUPPORTED_STATIONS", [f"K{i:03d}" for i in range(20)])
    _sembrar(db, hace_seg=6 * 600, n_estaciones=20)   # 6× intervalo
    assert pw._analysis_poller_health()["label"] == "BAD"


def test_cobertura_incompleta_degrada_aunque_el_dato_sea_fresco(pw, db, monkeypatch):
    """El modo de fallo del roster fantasma: vivo, fresco, y cubriendo 5 de 20."""
    monkeypatch.setattr(pw, "SUPPORTED_STATIONS", [f"K{i:03d}" for i in range(20)])
    _sembrar(db, hace_seg=60, n_estaciones=5)
    h = pw._analysis_poller_health()
    assert h["label"] == "WARN", "fresco pero incompleto no puede salir OK"
    assert h["cobertura"] == 5


def test_tabla_vacia_es_bad(pw, db):
    h = pw._analysis_poller_health()
    assert h["label"] == "BAD" and h["age"] == "nunca"


def test_sin_db_no_revienta_la_pagina(pw, tmp_path, monkeypatch):
    """Si no se puede leer, la salud se reporta como BAD con el motivo, pero
    /system tiene que seguir cargando."""
    monkeypatch.setattr(pw, "__file__", str(tmp_path / "no_existe" / "x.py"))
    h = pw._analysis_poller_health()
    assert h["label"] == "BAD"


def test_el_intervalo_no_puede_divergir_del_poller(pw):
    """Se importa en vez de copiarse; si alguien cambia uno, esto lo dice."""
    import analysis_poller
    assert pw._AP_INTERVAL_S == analysis_poller.INTERVAL_S
