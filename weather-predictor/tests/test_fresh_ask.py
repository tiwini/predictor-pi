"""Las respuestas de la IA caducan al acabar el día local.

Caso real que lo motivó (2026-08-17): la home servía en KLAS una respuesta del
2026-06-26 que abría con "Hoy ya observado 105.98°F", y /comparison una del
2026-07-18 con "Recomendación de bet: 95° to 96° YES, edge +92pp, confianza muy
alta" — mientras el día iba por 111.9°F.

El `ts` estaba guardado desde siempre. El fallo era de presentación: la home
pintaba `ts[11:16]`, la hora sin la fecha, así que una respuesta de dos meses
atrás parecía de esa mañana.

Se prueba `_fresh_ask` sin levantar Flask: importar `predictor_web` arrastra el
servidor entero, así que se carga el módulo por ruta y se toma sólo la función.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

TZ = ZoneInfo("America/Phoenix")


@pytest.fixture(scope="module")
def fresh_ask():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pw_for_test", BASE / "predictor_web.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"predictor_web no importable en el entorno de test: {e}")
    return mod._fresh_ask


def _ask(dt):
    return {"label": "🌡 Max hoy", "text": "…", "cost": 0.0007,
            "ts": dt.isoformat()}


def test_respuesta_de_hoy_se_muestra_con_edad(fresh_ask):
    hace_25 = datetime.now(timezone.utc) - timedelta(minutes=25)
    out = fresh_ask(_ask(hace_25), TZ)
    assert out is not None
    assert out["age_min"] == 25
    assert "25" in out["age_str"]


def test_respuesta_de_ayer_se_descarta(fresh_ask):
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    assert fresh_ask(_ask(ayer), TZ) is None


def test_el_caso_real_de_julio_se_descarta(fresh_ask):
    """La que recomendaba comprar YES en 95-96° con el día en 111.9°F."""
    viejo = datetime(2026, 7, 18, 1, 9, 6, tzinfo=timezone.utc)
    assert fresh_ask(_ask(viejo), TZ) is None


def test_sin_ts_no_se_muestra(fresh_ask):
    """No se puede afirmar frescura de algo que no trae fecha."""
    assert fresh_ask({"label": "x", "text": "y", "cost": 0.0}, TZ) is None
    assert fresh_ask({"label": "x", "ts": "no-es-fecha"}, TZ) is None


def test_none_pasa_limpio(fresh_ask):
    assert fresh_ask(None, TZ) is None


def test_horas_se_formatean_en_h_y_min(fresh_ask):
    """Mismo día pero varias horas atrás: se muestra, con la edad clara."""
    ahora_local = datetime.now(TZ)
    if ahora_local.hour < 4:
        pytest.skip("de madrugada no cabe un desfase de 3h en el mismo día local")
    hace_3h = datetime.now(timezone.utc) - timedelta(hours=3, minutes=5)
    out = fresh_ask(_ask(hace_3h), TZ)
    assert out is not None and out["age_str"].startswith("hace 3h")
