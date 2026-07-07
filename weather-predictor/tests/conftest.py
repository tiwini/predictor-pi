"""Test fixtures shared por todo tests/.

Autouse fixtures para desactivar features globales que interferirían con
casos base:
  - Safe mode D2 (bets.py 2026-07-07): tests pre-existentes usan edge 5pp
    y fechas dentro de la ventana safe (≤2026-07-20). Push safe cutoff a
    fecha pasada para que los tests base no lo vean; tests dedicados a
    safe mode pueden re-activar via monkeypatch.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_safe_mode(monkeypatch):
    try:
        import bets
        monkeypatch.setattr(bets, "SAFE_MODE_ACTIVE_UNTIL", "2020-01-01",
                            raising=False)
    except ImportError:
        pass
