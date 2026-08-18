"""El P&L cuenta desde el fix del ledger, no desde el principio de los tiempos.

Medido el 2026-08-18 sobre la tabla real, con el mismo filtro que usa la página:

    PRE-fix  (< 2026-07-07)   n=548   WR 51.8%   ROI  +53.2%   ← el artefacto
    POST-fix (>= 2026-07-07)  n= 38   WR 39.5%   ROI  −10.4%   ← la realidad

`/bets` mostraba el agregado, **+49.0%**, con el 94% de la muestra procedente
del periodo que la auditoría del 2026-07-06/07 declaró inválido. El mecanismo se
arregló entonces; las filas históricas nunca se pusieron en cuarentena, así que
el titular seguía afirmando que el sistema gana dinero.

Las fechas se siembran relativas a `today()` a propósito: sembrarlas literales
es lo que dejó tres tests en rojo dos semanas
([[feedback_tests_fecha_caducidad]]).
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import bets  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "calibration.db"
    monkeypatch.setattr(bets, "DB_PATH", p)
    bets._conn().close()   # corre las migraciones: crea tabla + columnas
    return p


def _fix_date() -> date:
    return date.fromisoformat(bets.LEDGER_FIX_DATE)


def _insert(db, d: date, won: int, stake=10.0, payoff=0.0):
    """Fila settleada directa: el objetivo es el filtro, no `maybe_bet`."""
    import sqlite3
    c = sqlite3.connect(db)
    c.execute(
        """INSERT INTO simulated_bets
           (station_id, date, ticker, bin_lo, bin_hi, bin_label, side,
            our_p, kalshi_p, edge_pp, stake, entry_price, contracts,
            entered_at, outcome, won, payoff, pnl)
           VALUES ('KX', ?, ?, 90, 91, '90-91', 'yes',
                   0.6, 0.4, 20, ?, 0.4, 25, ?, 1, ?, ?, ?)""",
        (d.isoformat(), f"T{d}{won}", stake, d.isoformat() + "T12:00:00",
         won, payoff, payoff - stake))
    c.commit()
    c.close()


def test_las_bets_pre_fix_no_cuentan(db):
    _insert(db, _fix_date() - timedelta(days=5), won=1, payoff=25.0)
    _insert(db, _fix_date() + timedelta(days=5), won=0, payoff=0.0)
    s = bets.stats("KX")
    assert s.n_settled == 1, "sólo la posterior al fix"
    assert s.roi == pytest.approx(-1.0), "la que cuenta perdió el stake entero"


def test_el_dia_del_fix_SI_cuenta(db):
    """El corte es inclusivo: el fix ya estaba aplicado ese día."""
    _insert(db, _fix_date(), won=1, payoff=25.0)
    assert bets.stats("KX").n_settled == 1


def test_since_none_recupera_el_historico(db):
    _insert(db, _fix_date() - timedelta(days=5), won=1, payoff=25.0)
    _insert(db, _fix_date() + timedelta(days=5), won=0, payoff=0.0)
    todo = bets.stats("KX", since=None)
    assert todo.n_settled == 2
    assert todo.roi == pytest.approx(0.25), "+53% del artefacto en miniatura"


def test_el_titular_no_puede_volver_a_incluir_lo_invalidado(db):
    """Regresión directa del caso real: pre-fix ganador, post-fix perdedor.

    Si alguien quita el filtro, el ROI vuelve a salir positivo y este test lo
    dice.
    """
    for i in range(9):
        _insert(db, _fix_date() - timedelta(days=10 + i), won=1, payoff=25.0)
    _insert(db, _fix_date() + timedelta(days=1), won=0, payoff=0.0)
    assert bets.stats("KX").roi < 0, "el titular refleja el periodo válido"
    assert bets.stats("KX", since=None).roi > 0, "el histórico sigue accesible"


# ── El sweep no puede relajar el umbral ──────────────────────────────────
#
# `bets_sweep` sólo puede endurecer: la tabla contiene únicamente bets que ya
# pasaron el filtro vigente, así que un umbral más laxo devuelve el mismo
# conjunto. El grid llevaba un 3.0 que salía idéntico al 5.0 actual (209 bets,
# −35.2% las dos filas) y se leía como "bajar a 3pp no empeora", cuando el dato
# no puede responder eso: las bets de 3-5pp nunca se registraron.

def test_el_sweep_no_ofrece_umbrales_por_debajo_del_actual():
    import bets_sweep
    r = bets_sweep.sweep_edge_threshold(days=30, current_thr_pp=5.0)
    etiquetas = [row.label for row in r["rows"]]
    assert not any("≥3pp" in e for e in etiquetas), etiquetas
    assert any("actual" in e for e in etiquetas), "el vigente sigue estando"


def test_un_grid_explicito_tambien_se_filtra():
    import bets_sweep
    r = bets_sweep.sweep_edge_threshold(
        days=30, thresholds_pp=[1.0, 2.0, 5.0, 20.0], current_thr_pp=5.0)
    assert [row.label for row in r["rows"]] == ["≥5pp · actual", "≥20pp"]


# ── El baseline del Brier ────────────────────────────────────────────────
#
# `/calibration` anunciaba "0.25 = al azar", que es el baseline de un binario
# EQUILIBRADO. Aquí sólo gana un bin por día: medido el 2026-08-18 la tasa base
# es 0.2256 y el baseline trivial p(1-p) cae a 0.1747. Con el 0.25 nuestro
# Brier de 0.3439 parecía "algo peor que el azar"; contra el baseline real es
# el doble de error que no predecir nada.

def test_reliability_expone_el_baseline_de_la_tasa_base(tmp_path, monkeypatch):
    import calibration
    monkeypatch.setattr(calibration, "DB_PATH", tmp_path / "c.db")
    c = calibration._conn()
    hoy = date.today().isoformat()
    # 2 de 8 aciertan → tasa base 0.25, baseline 0.25*0.75 = 0.1875
    for i in range(8):
        c.execute(
            """INSERT INTO prediction_snapshots
               (station_id, date, snapshot_time, slot, is_auto, expr, op,
                threshold, predicted_p, outcome)
               VALUES ('KX', ?, ?, ?, 1, 'max>=90', '>=', 90, 0.5, ?)""",
            (hoy, f"{hoy}T{i:02d}:00:00", i, 1 if i < 2 else 0))
    c.commit()
    c.close()
    rep = calibration.reliability("KX")
    assert rep.base_rate == pytest.approx(0.25)
    assert rep.baseline_brier == pytest.approx(0.1875)
    assert rep.brier == pytest.approx(0.25), "predecir 0.5 siempre"
    assert rep.brier > rep.baseline_brier, "peor que el baseline trivial"


def test_sin_filas_resueltas_el_baseline_es_none(tmp_path, monkeypatch):
    import calibration
    monkeypatch.setattr(calibration, "DB_PATH", tmp_path / "c2.db")
    calibration._conn().close()
    rep = calibration.reliability("KX")
    assert rep.brier is None and rep.baseline_brier is None
    assert rep.base_rate is None
