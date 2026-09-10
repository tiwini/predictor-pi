"""El Brier watchdog mide las 20 estaciones, no la que el web tuviera abierta.

Hasta el 2026-09-10 leía `day_summary`, que se llena desde `market_prices`, y a
ésa sólo la escribe `record_kalshi` — el web, la TUI y el CLI, nunca el poller.
Resultado: 14 días de KPHX y cero de doce estaciones. El test de integración
siembra dos estaciones y comprueba que salen las dos.
"""
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brier_watchdog as bw  # noqa: E402
from stations import STATION_TZ, PEAK_HOURS  # noqa: E402


# ─── el outcome usa el redondeo del NWS ──────────────────────────────────────

def test_bin_contiene_con_el_medio_grado():
    """El NWS liquida entero y our_p_for_bin cubre [lo-0.5, hi+0.5]. Sin ese
    ±0.5 el outcome mide una pregunta distinta de la que se predijo."""
    assert bw._bin_contains(80.0, 80.0, 81.0) == 1
    assert bw._bin_contains(81.0, 80.0, 81.0) == 1
    assert bw._bin_contains(81.4, 80.0, 81.0) == 1     # dentro por el redondeo
    assert bw._bin_contains(82.0, 80.0, 81.0) == 0
    assert bw._bin_contains(79.0, 80.0, 81.0) == 0


def test_bins_de_cola():
    """Las colas llegan como ±1e9 desde la DB. Tratadas como número, el bin
    inferior no contendría nada y el Brier saldría inflado en el bin que más
    masa suele llevar."""
    assert bw._bin_contains(-40.0, -1e9, 60.0) == 1
    assert bw._bin_contains(130.0, 110.0, 1e9) == 1
    assert bw._bin_contains(61.0, -1e9, 60.0) == 0


# ─── el criterio de alerta, en un solo sitio ─────────────────────────────────

def _s(**kw):
    base = {"station_id": "KX", "n": 7, "our_brier": 0.14,
            "kalshi_brier": 0.10, "ratio": 1.4, "diff": 0.04}
    base.update(kw)
    return base


def test_alerta_con_las_tres_condiciones():
    assert bw.es_roja(_s()) is True


def test_muestra_corta_no_alerta():
    assert bw.es_roja(_s(n=bw.ALERT_MIN_N - 1)) is False


def test_diferencia_trivial_no_alerta():
    """1.3× sobre un Brier de 0.02 es ruido: 0.026 vs 0.020."""
    assert bw.es_roja(_s(our_brier=0.026, kalshi_brier=0.020,
                         ratio=1.30001, diff=0.006)) is False


def test_ratio_sin_denominador_decide_la_diferencia():
    """El 883× de la W37 era un bin a 0.99 en un día ya resuelto. Con el
    denominador por los suelos el ratio no se calcula y manda la diferencia."""
    assert bw.es_roja(_s(kalshi_brier=0.0001, ratio=None, diff=0.14)) is True
    assert bw.es_roja(_s(kalshi_brier=0.0001, ratio=None, diff=0.001)) is False


def test_mejor_que_el_mercado_no_alerta():
    assert bw.es_roja(_s(our_brier=0.08, kalshi_brier=0.12,
                         ratio=0.67, diff=-0.04)) is False


# ─── integración: dos estaciones, una por día, a la hora de referencia ───────

ST_A, ST_B = "KPHX", "KDEN"


def _siembra(tmp_path, monkeypatch, dias=6):
    """Dos estaciones con settle y bins. Fechas RELATIVAS a hoy: con fechas
    literales el test caduca en cuanto el lookback de 7 días las deja fuera."""
    an_p, cal_p = tmp_path / "analysis.db", tmp_path / "calibration.db"
    an = sqlite3.connect(an_p)
    an.execute("""CREATE TABLE kalshi_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, station TEXT,
        bin_lo REAL, bin_hi REAL, yes_mid REAL, our_p REAL,
        our_p_calibrated REAL)""")
    cal = sqlite3.connect(cal_p)
    cal.execute("""CREATE TABLE day_outcomes (
        station_id TEXT, date TEXT, max_obs_f REAL)""")

    for st in (ST_A, ST_B):
        tz = ZoneInfo(STATION_TZ[st])
        ref_h = PEAK_HOURS[st][0] - bw.REF_HOURS_BEFORE_PEAK
        for k in range(1, dias + 1):
            d = date.today() - timedelta(days=k)
            settle = 80.0
            cal.execute("INSERT INTO day_outcomes VALUES (?,?,?)",
                        (st, d.isoformat(), settle))
            base = datetime.combine(d, datetime.min.time(), tz)
            # A la hora de referencia acertamos poco; tres horas después, con
            # el día resuelto, acertamos del todo. Si el watchdog promediara
            # el día, el Brier saldría mucho mejor de lo que es a la hora útil.
            for hora, p_ganador in ((ref_h, 0.30), (ref_h + 3, 1.00)):
                ts = (base + timedelta(hours=hora)).astimezone(timezone.utc)
                for lo, hi in ((79.0, 79.0), (80.0, 80.0), (81.0, 81.0)):
                    gana = (lo == settle)
                    p = p_ganador if gana else (1 - p_ganador) / 2
                    an.execute(
                        "INSERT INTO kalshi_snapshots (ts, station, bin_lo, "
                        "bin_hi, yes_mid, our_p, our_p_calibrated) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (ts.strftime("%Y-%m-%dT%H:%M:%S"), st, lo, hi,
                         0.5 if gana else 0.25, p, p))
    an.commit(); cal.commit(); an.close(); cal.close()
    monkeypatch.setattr(bw, "ANALYSIS_DB", an_p)
    monkeypatch.setattr(bw, "CALIBRATION_DB", cal_p)


def test_cubre_todas_las_estaciones_sembradas(tmp_path, monkeypatch):
    _siembra(tmp_path, monkeypatch)
    stats = bw.compute_brier_by_station()
    assert {s["station_id"] for s in stats} == {ST_A, ST_B}, \
        "la fuente vieja devolvía sólo la estación abierta en el web"
    for s in stats:
        assert s["n"] == 6


def test_toma_la_hora_de_referencia_y_no_promedia_el_dia(tmp_path, monkeypatch):
    """Con p=0.30 en el ganador y 0.35 en cada perdedor, el Brier a la hora de
    referencia es (0.70² + 2·0.35²)/3 = 0.245. Si colara la hora tardía
    (p=1.0 en el ganador, Brier 0) la media caería a 0.1225 y el watchdog
    diría que estamos el doble de bien de lo que estamos cuando importa."""
    _siembra(tmp_path, monkeypatch)
    s = next(x for x in bw.compute_brier_by_station()
             if x["station_id"] == ST_A)
    assert s["our_brier"] == pytest.approx(0.245, abs=0.002)


def test_diff_y_ratio_se_calculan(tmp_path, monkeypatch):
    _siembra(tmp_path, monkeypatch)
    s = next(x for x in bw.compute_brier_by_station()
             if x["station_id"] == ST_B)
    assert s["diff"] == pytest.approx(s["our_brier"] - s["kalshi_brier"])
    assert s["ratio"] == pytest.approx(s["our_brier"] / s["kalshi_brier"])
