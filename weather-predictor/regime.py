"""Detector de regime climático por estación.

Clasifica el estado actual de cada estación en uno de:
    stable          — todo normal
    heatwave        — pred en p≥90 vs climatología local
    cold_snap       — pred en p≤10 (incluido por simetría)
    marine_bimodal  — KLAX en verano con spread alto (capa marina)
    transition      — spread de ensemble alto sin firma de heat/cold
    regime_break    — obs ya rompió p1-p99 ≥2 horas hoy (modelo blown)

El tag se computa cada poll y se guarda en station_snapshots. Sirve para:
  - Difficulty floor (sumar bump al score 0-100 en /comparison)
  - Warnings en bets gate (no auto-skip salvo regime_break)
  - Badge visible en home/dashboard para que el usuario vea el contexto

Sin nuevos fetches a Open-Meteo: deriva todo del Snapshot ya construido.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Estaciones con regimen bimodal por capa marina en verano.
MARINE_STATIONS = {"KLAX", "KSFO"}
MARINE_MONTHS = {6, 7, 8, 9}

# Umbrales (calibrables sin cambiar el código de detection).
CLIM_HOT_PCT = 90.0
CLIM_COLD_PCT = 10.0
SPREAD_TRANSITION_F = 4.5     # spread max-min para flag de transition
                              # bajado de 8→4.5 el 2026-06-23 (KDEN convección)
SPREAD_MARINE_F = 2.0          # KLAX verano: capa marina con ensemble estrecho (1-2°F)
                               # ya divergence con mercado → bajado de 5→2 el 2026-06-23
REGIME_BREAK_HOURS_MIN = 2     # ≥N hrs fuera p1-p99 → regime_break


@dataclass
class RegimeTag:
    tag: str
    reason: str
    difficulty_bump: int   # 0-30 a sumar al difficulty floor
    bet_action: str        # "ok" | "soft_warn" | "skip"

    def __str__(self) -> str:
        return f"{self.tag} ({self.reason})"


def _spread(maxes) -> float | None:
    if not maxes:
        return None
    return max(maxes) - min(maxes)


def classify(snap, station_id: str,
             station_local_dt: datetime | None = None) -> RegimeTag:
    """Clasifica el regimen actual a partir del Snapshot.

    Prioridad (return en orden):
        regime_break > marine_bimodal > heatwave/cold_snap > transition > stable
    """
    # 1. Regime break — modelo blown, no bettear nada
    if snap is not None and len(getattr(snap, "regime_break_hours", []) or []) >= REGIME_BREAK_HOURS_MIN:
        hrs = snap.regime_break_hours
        return RegimeTag(
            tag="regime_break",
            reason=f"obs fuera p1-p99 en {len(hrs)} hrs ({','.join(f'{h:02d}h' for h in hrs)})",
            difficulty_bump=30,
            bet_action="skip",
        )

    spread = _spread(getattr(snap, "ensemble_daily_maxes", None)) if snap else None

    # 2. Marine bimodal — caso específico de KLAX/SoCal en verano
    dt = station_local_dt
    if (station_id in MARINE_STATIONS and dt is not None
            and dt.month in MARINE_MONTHS
            and spread is not None and spread >= SPREAD_MARINE_F):
        return RegimeTag(
            tag="marine_bimodal",
            reason=f"KLAX verano + ensemble spread {spread:.1f}°F (capa marina)",
            difficulty_bump=20,
            bet_action="soft_warn",
        )

    # 3. Heatwave / cold_snap por climatología
    clim = getattr(snap, "climatology", None)
    pct = getattr(clim, "percentile", None) if clim is not None else None
    if pct is not None:
        if pct >= CLIM_HOT_PCT:
            return RegimeTag(
                tag="heatwave",
                reason=f"pred en p{pct:.0f} histórico (clim)",
                difficulty_bump=10,
                bet_action="soft_warn",
            )
        if pct <= CLIM_COLD_PCT:
            return RegimeTag(
                tag="cold_snap",
                reason=f"pred en p{pct:.0f} histórico (clim)",
                difficulty_bump=10,
                bet_action="soft_warn",
            )

    # 4. Transition — spread alto sin firma de heat
    if spread is not None and spread >= SPREAD_TRANSITION_F:
        return RegimeTag(
            tag="transition",
            reason=f"ensemble spread {spread:.1f}°F",
            difficulty_bump=10,
            bet_action="soft_warn",
        )

    return RegimeTag(tag="stable", reason="todo normal",
                     difficulty_bump=0, bet_action="ok")
