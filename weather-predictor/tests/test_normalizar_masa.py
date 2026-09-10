"""La masa por bin suma 1.

Los bins de Kalshi son excluyentes y exhaustivos. La suma publicada valía
1.0499 de mediana (p10 0.95, p90 1.19) porque la isotónica se aplica bin a bin.
Medido sobre 445.710 bins con settle: normalizar mejora el Brier (0.0975 →
0.0966) **y** la fiabilidad (0.0539 → 0.0476).
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import predictor as P  # noqa: E402


def test_la_masa_queda_en_uno():
    out = P.normalizar_masa([0.6, 0.3, 0.2])   # suma 1.1
    assert sum(out) == pytest.approx(1.0)


def test_conserva_las_proporciones():
    """Normalizar reescala, no reordena: el bin favorito sigue siéndolo y las
    razones entre bins no se tocan."""
    ps = [0.6, 0.3, 0.2]
    out = P.normalizar_masa(ps)
    assert out[0] / out[1] == pytest.approx(ps[0] / ps[1])
    assert out.index(max(out)) == ps.index(max(ps))


def test_una_suma_que_ya_vale_uno_no_se_toca():
    ps = [0.5, 0.3, 0.2]
    assert P.normalizar_masa(ps) is ps


def test_con_masa_cero_no_inventa():
    """Sin masa no hay nada que repartir: dividir sería inventarse una
    distribución uniforme que nadie ha medido."""
    ps = [0.0, 0.0]
    assert P.normalizar_masa(ps) is ps


def test_con_algun_bin_a_none_se_deja_igual():
    ps = [0.5, None, 0.2]
    assert P.normalizar_masa(ps) is ps


def test_lista_vacia():
    assert P.normalizar_masa([]) == []


class _Bin:
    def __init__(self, lo, hi):
        self.bin_lo, self.bin_hi = lo, hi


def test_el_pipeline_entrega_masa_normalizada(monkeypatch):
    """De punta a punta: lo que sale de `_compute_final_our_p_per_bin` suma 1.

    Es el test que habría fallado antes del 2026-09-10, con la salida en 1.05.
    """
    import isotonic as iso
    monkeypatch.setattr(iso, "get", lambda _s: SimpleNamespace(
        n_fit=10 ** 6, n_days=10 ** 6))
    # un calibrador que rompe la masa a propósito
    monkeypatch.setattr(iso, "apply", lambda _c, p: min(1.0, p * 1.4 + 0.05))
    snap = SimpleNamespace(
        station_local=datetime(2026, 9, 10, 11, 0,
                               tzinfo=ZoneInfo("America/Phoenix")),
        ensemble_daily_maxes=[100.0] * 10 + [102.0] * 10,
        ext_shift_info=None, today_max_obs=None, today_max_cli=None,
        today_max_asos_6h=None, today_max_asos_6h_ts=None,
        current_temp_f=None, today_max_5min=None)
    bins = [_Bin(float("-inf"), 99.0), _Bin(100.0, 101.0),
            _Bin(102.0, 103.0), _Bin(104.0, float("inf"))]
    ps = P._compute_final_our_p_per_bin("KPHX", snap, bins)
    assert sum(ps) == pytest.approx(1.0)
