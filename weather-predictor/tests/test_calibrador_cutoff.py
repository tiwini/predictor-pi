"""El calibrador isotónico se apaga pasadas las 17h locales.

Medido el 2026-09-10 sobre 445.710 bins: ayuda de 00h a 16h (Brier −0.0115 a
−0.0152) y estorba de 17h a 23h (+0.0015 a +0.0151). Pasado el pico la certeza
extrema está justificada y el calibrador la deshace.

La auditoría externa había señalado esto por el otro lado —«aprende del
atardecer, así que no sirve al mediodía»—. Lo primero es cierto (los pares son
de las 16-23h, mediana 19h) y lo segundo no: donde se aplica, mejora el Brier
de 0.1390 a 0.1247. Lo que estaba mal era dónde se aplica, no dónde aprende.
"""
from datetime import datetime
import sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import predictor as P  # noqa: E402

TZ = ZoneInfo("America/Phoenix")


def _snap(hora, maxes=None):
    return SimpleNamespace(
        station_local=datetime(2026, 9, 10, hora, 30, tzinfo=TZ),
        ensemble_daily_maxes=maxes or [100.0] * 20,
        ext_shift_info=None, today_max_obs=None, today_max_cli=None,
        today_max_asos_6h=None, today_max_asos_6h_ts=None,
        current_temp_f=None, today_max_5min=None)


def test_la_hora_de_corte_es_deliberada():
    assert P.CALIBRATOR_CUTOFF_HOUR == 17


@pytest.mark.parametrize("hora,apagado", [
    (0, False), (9, False), (12, False), (16, False),   # ayuda
    (17, True), (18, True), (23, True),                  # estorba
])
def test_la_frontera_cae_en_las_17h(hora, apagado):
    assert P.calibrador_apagado(_snap(hora)) is apagado


def test_sin_hora_no_se_apaga():
    """Ante la duda, seguir calibrando: es lo que lleva haciendo desde julio
    y lo que ayuda 16 de cada 24 horas."""
    assert P.calibrador_apagado(SimpleNamespace()) is False


class _Bin:
    def __init__(self, lo, hi):
        self.bin_lo, self.bin_hi = lo, hi


def _con_calibrador_falso(monkeypatch):
    """Un calibrador que se nota: empuja toda p a 0.42."""
    import isotonic as iso
    monkeypatch.setattr(iso, "get", lambda _s: SimpleNamespace(
        n_fit=10 ** 6, n_days=10 ** 6))
    monkeypatch.setattr(iso, "apply", lambda _c, _p: 0.42)


def test_antes_del_corte_se_calibra(monkeypatch):
    _con_calibrador_falso(monkeypatch)
    ps = P._compute_final_our_p_per_bin("KPHX", _snap(12), [_Bin(99.0, 100.0)])
    assert ps[0] == pytest.approx(0.42)


def test_pasado_el_corte_sale_el_crudo(monkeypatch):
    """EL test: a las 18h la probabilidad publicada tiene que ser la cruda."""
    _con_calibrador_falso(monkeypatch)
    b = _Bin(99.0, 100.0)
    crudo = P._compute_final_our_p_per_bin("KPHX", _snap(18), [b])[0]
    assert crudo != pytest.approx(0.42), "el calibrador siguió aplicándose"
