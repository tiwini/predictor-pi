"""El piso de observación aplicado a la distribución por bin.

Contexto: `apply_obs_floor` (2026-07-26) clampeó la predicción puntual pero no
la distribución. Medido el 2026-08-02: la isotónica levanta el piso MIN_P de
0.030 a ~0.090 en bins que el día ya dejó atrás, y en KAUS llegó a 0.346 siendo
el bin favorito calibrado.

El caso de redondeo (test_umbral_respeta_medio_grado) es el que importa: el
primer análisis usó `bin_hi < floor` y contó 28 bins imposibles, pero el settle
del NWS es entero y `our_p_for_bin` cubre [lo-0.5, hi+0.5]. Con max_obs 98.06 el
bin "98 or below" sigue vivo. El criterio correcto es `floor > hi + 0.5`.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from predictor import zero_impossible_bins, obs_floor_from_snapshot  # noqa: E402


def B(lo, hi):
    return SimpleNamespace(bin_lo=lo, bin_hi=hi)


def test_anula_bin_ya_superado_y_conserva_la_masa():
    bins = [B(float("-inf"), 90), B(91, 92), B(93, float("inf"))]
    ps = [0.09, 0.50, 0.10]
    out, n, liberada = zero_impossible_bins(bins, ps, floor=92.0)
    assert n == 1 and out[0] == 0.0
    assert abs(liberada - 0.09) < 1e-9
    # la masa liberada se reparte entre los vivos, sin cambiar el total
    assert abs(sum(out) - sum(ps)) < 1e-9
    # y proporcionalmente: el que tenía 5x del otro sigue teniendo 5x
    assert abs(out[1] / out[2] - ps[1] / ps[2]) < 1e-9


def test_umbral_respeta_medio_grado():
    """El settle es entero: con max_obs 98.06 el bin '98 or below' VIVE.

    Éste es el caso donde el criterio ingenuo `hi < floor` se equivoca — mataría
    un bin que gana si el día no sube más.
    """
    bins = [B(float("-inf"), 98), B(99, 100)]
    ps = [0.35, 0.30]
    out, n, _ = zero_impossible_bins(bins, ps, floor=98.06)
    assert n == 0 and out == ps

    # a partir de floor > 98.5 sí queda fuera de alcance
    out2, n2, _ = zero_impossible_bins(bins, ps, floor=98.6)
    assert n2 == 1 and out2[0] == 0.0


def test_sin_piso_o_sin_imposibles_es_identidad():
    bins = [B(float("-inf"), 90), B(91, 92)]
    ps = [0.2, 0.6]
    assert zero_impossible_bins(bins, ps, None) == (ps, 0, 0.0)
    assert zero_impossible_bins(bins, ps, 80.0) == (ps, 0, 0.0)


def test_tail_superior_nunca_es_imposible():
    bins = [B(120, float("inf"))]
    out, n, _ = zero_impossible_bins(bins, [0.05], floor=200.0)
    assert n == 0 and out == [0.05]


def test_no_revienta_si_todos_son_imposibles():
    """Caso degenerado: sin bins vivos no hay dónde redistribuir."""
    bins = [B(float("-inf"), 70), B(71, 72)]
    out, n, liberada = zero_impossible_bins(bins, [0.4, 0.4], floor=95.0)
    assert n == 2 and out == [0.0, 0.0]
    assert abs(liberada - 0.8) < 1e-9


def test_none_en_ps_no_rompe():
    bins = [B(float("-inf"), 80), B(81, 82)]
    out, n, _ = zero_impossible_bins(bins, [None, 0.5], floor=95.0)
    assert out[1] == 0.5   # sin vivos con masa, no se escala nada
    assert n == 1


def test_piso_desde_snapshot_toma_el_mayor_de_los_tres():
    s = SimpleNamespace(today_max_obs=88.0, today_max_cli=91.0,
                        current_temp_f=85.0)
    assert obs_floor_from_snapshot(s) == 91.0
    # current manda cuando va por delante, con su margen de medio escalón °C
    s2 = SimpleNamespace(today_max_obs=88.0, today_max_cli=None,
                         current_temp_f=95.0)
    assert abs(obs_floor_from_snapshot(s2) - 94.1) < 1e-9
    # sentinel de "sin observación" no debe colarse como piso
    s3 = SimpleNamespace(today_max_obs=-999.0, today_max_cli=None,
                         current_temp_f=None)
    assert obs_floor_from_snapshot(s3) is None
