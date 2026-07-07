"""Sweep retrospectivo sobre simulated_bets.

Replica decisión sobre las bets que YA están en DB (settled), aplicando un
filtro alternativo (edge_threshold más restrictivo, models_spread más bajo,
ext_gate distinto). No simula bets que el filtro original descartó — solo
podemos endurecer thresholds, no relajarlos.

Para sweep multivariado (ext_gate, models_spread) las columnas se llenan
desde el 2026-06-22; bets viejas las tienen NULL y quedan fuera de esos
slices.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "calibration.db"

# Mínimo de bets para que un slice cuente como "señal" (no sample-noise).
MIN_N = 30


@dataclass
class SweepRow:
    label: str
    n_bets: int
    n_wins: int
    win_rate: float | None
    total_stake: float
    total_payoff: float
    pnl: float
    roi: float | None


def _settled_bets(days: int = 30, station_id: str | None = None) -> list[dict]:
    """Devuelve bets settled de los últimos `days` días con todos los
    campos relevantes para sweep."""
    since = (date.today() - timedelta(days=days)).isoformat()
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    sql = """SELECT station_id, date, side, edge_pp, stake, entry_price,
                    contracts, outcome, won, payoff, pnl,
                    ext_diff_at_entry, models_spread_at_entry,
                    our_pred_at_entry, bin_lo, bin_hi
             FROM simulated_bets
             WHERE outcome IS NOT NULL
               AND date >= ?"""
    params: list = [since]
    if station_id:
        sql += " AND station_id=?"
        params.append(station_id)
    rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    c.close()
    return rows


def _aggregate(bets: list[dict], label: str) -> SweepRow:
    n = len(bets)
    if n == 0:
        return SweepRow(label, 0, 0, None, 0.0, 0.0, 0.0, None)
    wins = sum(1 for b in bets if b["won"])
    stake = sum(b["stake"] for b in bets)
    payoff = sum(b["payoff"] or 0.0 for b in bets)
    pnl = payoff - stake
    return SweepRow(
        label=label,
        n_bets=n,
        n_wins=wins,
        win_rate=wins / n,
        total_stake=stake,
        total_payoff=payoff,
        pnl=pnl,
        roi=(pnl / stake) if stake > 0 else None,
    )


def sweep_edge_threshold(
    days: int = 30,
    thresholds_pp: list[float] | None = None,
    station_id: str | None = None,
    current_thr_pp: float = 5.0,
) -> dict:
    """Sweep sobre |edge_pp| ≥ thr. Solo restrictivo (subir el umbral).

    Devuelve {'rows': [SweepRow], 'window_days', 'current_thr_pp'}.
    Cada row es como si hubiéramos usado ese thr en lugar del actual.
    """
    if thresholds_pp is None:
        thresholds_pp = [3.0, 5.0, 7.0, 10.0, 15.0]
    bets = _settled_bets(days=days, station_id=station_id)
    rows: list[SweepRow] = []
    for thr in thresholds_pp:
        subset = [b for b in bets if abs(b["edge_pp"]) >= thr]
        is_current = abs(thr - current_thr_pp) < 0.01
        label = f"≥{thr:.0f}pp" + (" · actual" if is_current else "")
        rows.append(_aggregate(subset, label))
    return {
        "rows": rows,
        "window_days": days,
        "current_thr_pp": current_thr_pp,
        "n_total_in_window": len(bets),
    }


def sweep_models_spread(
    days: int = 30,
    spread_cuts_f: list[float] | None = None,
    station_id: str | None = None,
    current_cut_f: float = 5.4,
) -> dict:
    """Sweep sobre models_spread_at_entry ≤ cut. NULL = excluida del slice
    (data antes del schema upgrade del 2026-06-22)."""
    if spread_cuts_f is None:
        spread_cuts_f = [3.0, 4.0, 5.4, 7.0, 999.0]
    bets = _settled_bets(days=days, station_id=station_id)
    with_spread = [b for b in bets if b["models_spread_at_entry"] is not None]
    rows: list[SweepRow] = []
    for cut in spread_cuts_f:
        subset = [b for b in with_spread
                  if (b["models_spread_at_entry"] or 0) <= cut]
        is_current = abs(cut - current_cut_f) < 0.01
        label = (f"≤{cut:.1f}°F" if cut < 100 else "sin filtro") \
                + (" · actual" if is_current else "")
        rows.append(_aggregate(subset, label))
    return {
        "rows": rows,
        "window_days": days,
        "current_cut_f": current_cut_f,
        "n_with_data": len(with_spread),
        "n_total_in_window": len(bets),
    }


def sweep_ext_gate(
    days: int = 30,
    gate_cuts_f: list[float] | None = None,
    station_id: str | None = None,
    current_gate_f: float = 1.5,
) -> dict:
    """Sweep sobre |ext_diff_at_entry| ≤ gate. Solo aplica a bets de cola
    (direction=cold/hot), no a mid-bins."""
    if gate_cuts_f is None:
        gate_cuts_f = [1.0, 1.5, 2.0, 3.0, 999.0]
    bets = _settled_bets(days=days, station_id=station_id)
    with_ext = [b for b in bets if b["ext_diff_at_entry"] is not None]
    rows: list[SweepRow] = []
    for gate in gate_cuts_f:
        subset = []
        for b in with_ext:
            ed = abs(b["ext_diff_at_entry"] or 0)
            # bet pasa si ext_diff está bajo gate, O si es un mid-bin
            # (sin direccionalidad clara). Marcamos mid si bin_lo/hi finitos
            # y our_pred_at_entry dentro del bin.
            is_tail = b["bin_lo"] == float("-inf") or b["bin_hi"] == float("inf")
            if not is_tail and b["our_pred_at_entry"] is not None:
                lo, hi = b["bin_lo"], b["bin_hi"]
                pred = b["our_pred_at_entry"]
                if lo <= pred <= hi:
                    subset.append(b)
                    continue
            if ed <= gate:
                subset.append(b)
        is_current = abs(gate - current_gate_f) < 0.01
        label = (f"≤{gate:.1f}°F" if gate < 100 else "sin filtro") \
                + (" · actual" if is_current else "")
        rows.append(_aggregate(subset, label))
    return {
        "rows": rows,
        "window_days": days,
        "current_gate_f": current_gate_f,
        "n_with_data": len(with_ext),
        "n_total_in_window": len(bets),
    }


def best_row(result: dict) -> SweepRow | None:
    """Devuelve la mejor fila por PnL absoluto, descartando slices con n < MIN_N
    (filtro anti sample-noise)."""
    rows = [r for r in result.get("rows", []) if r.n_bets >= MIN_N]
    if not rows:
        return None
    return max(rows, key=lambda r: r.pnl)
