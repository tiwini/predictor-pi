"""Tests de agent_signals.implied_median_f — la mediana en °F de la
distribución YA calibrada (isotonic + blend), que es lo único que
`pred_calibrated_f` prometía y nunca entregó."""
import agent_signals as A


class B:
    """Stand-in de kalshi.MarketBin: sólo hacen falta los bordes."""
    def __init__(self, lo, hi):
        self.bin_lo = lo
        self.bin_hi = hi


def test_none_without_material():
    assert A.implied_median_f([], []) is None
    assert A.implied_median_f([B(90, 91)], []) is None
    assert A.implied_median_f([B(90, 91)], [None]) is None


def test_all_mass_in_one_bin_lands_mid_bin():
    """Toda la probabilidad en un bin: la CDF cruza 0.5 a mitad de camino."""
    bins = [B(90, 91), B(91, 92), B(92, 93)]
    got = A.implied_median_f(bins, [0.0, 1.0, 0.0])
    assert abs(got - 91.5) < 1e-9


def test_interpolates_inside_the_crossing_bin():
    """25% bajo el bin y 50% dentro: el cruce cae al 50% del ancho."""
    bins = [B(90, 91), B(91, 92), B(92, 93)]
    got = A.implied_median_f(bins, [0.25, 0.50, 0.25])
    assert abs(got - 91.5) < 1e-9
    # Con la masa corrida hacia abajo el cruce se adelanta dentro del bin.
    got2 = A.implied_median_f(bins, [0.40, 0.50, 0.10])
    assert abs(got2 - 91.2) < 1e-9


def test_renormalizes_unnormalized_input():
    """isotonic.apply y blend_with_external actúan bin a bin y NO conservan la
    suma; sin renormalizar, la CDF no llegaría a 0.5 y saldría cualquier cosa."""
    bins = [B(90, 91), B(91, 92), B(92, 93)]
    norm = A.implied_median_f(bins, [0.25, 0.50, 0.25])
    scaled = A.implied_median_f(bins, [0.05, 0.10, 0.05])   # misma forma, suma 0.2
    assert abs(norm - scaled) < 1e-9


def test_cold_tail_returns_finite_edge():
    """Sobre una cola infinita no hay ancho que interpolar: el borde finito es
    lo más honesto sin inventarle forma a la cola."""
    bins = [B(float("-inf"), 90), B(90, 91)]
    assert A.implied_median_f(bins, [0.9, 0.1]) == 90


def test_hot_tail_returns_finite_edge():
    bins = [B(90, 91), B(91, float("inf"))]
    assert A.implied_median_f(bins, [0.1, 0.9]) == 91


def test_ignores_bins_without_probability():
    bins = [B(90, 91), B(91, 92), B(92, 93)]
    got = A.implied_median_f(bins, [None, 1.0, None])
    assert abs(got - 91.5) < 1e-9


def test_unsorted_bins_are_handled():
    """fetch_bins no garantiza orden; la CDF sólo tiene sentido ordenada."""
    bins = [B(92, 93), B(90, 91), B(91, 92)]
    got = A.implied_median_f(bins, [0.25, 0.25, 0.50])
    assert abs(got - 91.5) < 1e-9


def test_zero_total_probability_is_none():
    assert A.implied_median_f([B(90, 91), B(91, 92)], [0.0, 0.0]) is None
