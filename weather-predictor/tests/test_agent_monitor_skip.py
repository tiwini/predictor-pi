"""El monitor deja constancia cuando NO hace trabajo.

`interval_min="off"` lo tuvo parado del 2026-06-24 al 2026-10-31 sin escribir
una sola línea, porque el `return` del skip caía antes del print del motivo —
y un log que no crece se lee igual que un proceso tranquilo. `paused=1` sí
escribía, así que había dos maneras de estar apagado y sólo una se veía.

Estos tests fijan que el motivo llegue al log, y que llegue una vez por hora y
no 1440 veces al día.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_monitor as am


def _state_db() -> sqlite3.Connection:
    """DB en memoria con la única tabla que mira `_log_skip`."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE agent_state (key TEXT PRIMARY KEY, value TEXT)")
    return c


def _sembrar_aviso(c: sqlite3.Connection, minutos_atras: float) -> None:
    """Siembra el último aviso relativo a now(), nunca con fecha literal: un
    test sembrado con fecha fija cambia de veredicto con el calendario."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutos_atras)
    c.execute("INSERT OR REPLACE INTO agent_state (key, value) "
              "VALUES ('last_skip_notice', ?)", (ts.isoformat(),))


def test_el_motivo_del_skip_llega_al_log(capsys):
    c = _state_db()
    am._log_skip(c, "interval=off")
    assert "interval=off" in capsys.readouterr().out


def test_no_repite_dentro_de_la_ventana(capsys):
    c = _state_db()
    am._log_skip(c, "interval=off")
    capsys.readouterr()
    am._log_skip(c, "interval=off")
    assert capsys.readouterr().out == ""


def test_vuelve_a_avisar_pasada_la_ventana(capsys):
    c = _state_db()
    _sembrar_aviso(c, am.SKIP_NOTICE_MIN + 1)
    am._log_skip(c, "throttle (3.0/60 min)")
    assert "throttle" in capsys.readouterr().out


def test_marca_ilegible_no_silencia(capsys):
    """Una marca corrupta deja pasar el aviso en vez de tragárselo: el modo
    seguro de un vigía es hablar, no callar."""
    c = _state_db()
    c.execute("INSERT INTO agent_state (key, value) "
              "VALUES ('last_skip_notice', 'basura')")
    am._log_skip(c, "pausado")
    assert "pausado" in capsys.readouterr().out


def test_main_escribe_cuando_el_interruptor_esta_en_off(monkeypatch, capsys):
    """La integración, que es donde estaba el defecto: no basta con que
    `_log_skip` funcione si `main()` vuelve a saltárselo. No se monkeypatchea
    `_log_skip` a propósito — un doble taparía justo lo que se quiere mirar."""
    c = _state_db()
    c.execute("INSERT INTO agent_state (key, value) VALUES ('interval_min', 'off')")
    monkeypatch.setattr(am, "_conn", lambda: c)
    am.main()
    assert "interval=off" in capsys.readouterr().out
