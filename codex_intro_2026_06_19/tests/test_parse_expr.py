"""Tests for predictor.parse_expr.

Cubre los 3 operadores nuevos (=, rango N-M, "entre N y M") y los
comparadores clásicos para que sigan funcionando.
"""
import pytest

from predictor import parse_expr


def test_greater_than():
    assert parse_expr(">89F") == (">", 89.0, 0.5, ">89F")
    assert parse_expr(">= 75") == (">=", 75.0, 0.5, ">=75F")


def test_less_than():
    assert parse_expr("<75F") == ("<", 75.0, 0.5, "<75F")
    assert parse_expr("<= 60") == ("<=", 60.0, 0.5, "<=60F")


def test_equal_simple():
    op, thr, half, disp = parse_expr("=59")
    assert op == "~"
    assert thr == 59.0
    assert half == 0.5
    assert disp == "=59F"


def test_equal_with_f_suffix():
    assert parse_expr("=80F") == ("~", 80.0, 0.5, "=80F")


def test_range_dash():
    op, thr, half, disp = parse_expr("59-60")
    assert op == "~"
    assert thr == 59.5         # center
    assert half == 1.0         # (60-59)/2 + 0.5
    assert disp == "=59–60F"


def test_range_double_dot():
    assert parse_expr("70..72") == ("~", 71.0, 1.5, "=70–72F")


def test_range_spanish_entre():
    op, thr, half, disp = parse_expr("entre 59 y 60")
    assert op == "~"
    assert thr == 59.5
    assert half == 1.0
    assert disp == "=59–60F"


def test_range_reversed_normalizes():
    # "entre 80 y 78" should normalize to lo=78, hi=80
    op, thr, half, _ = parse_expr("entre 80 y 78")
    assert thr == 79.0
    assert half == 1.5


def test_range_with_F_units():
    assert parse_expr("59F-60F") == ("~", 59.5, 1.0, "=59–60F")


def test_invalid_format_raises():
    with pytest.raises(ValueError):
        parse_expr("xyz")
    with pytest.raises(ValueError):
        parse_expr("89")        # bare number sin operador
    with pytest.raises(ValueError):
        parse_expr(">>89")
