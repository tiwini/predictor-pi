"""Multi-day forecasts: distribuciones de max diario para D0, D+1, D+2.

Para D0 reusa build_snapshot (reweight Bayesiano con obs matutinas).
Para D+1 y D+2 calcula max por miembro del ensemble raw (sin reweight,
porque no hay observaciones todavía).
"""
from datetime import datetime, date, timedelta

from predictor import fetch_ensemble, build_snapshot, Station


def _day_distribution(station: Station, day_offset: int) -> dict:
    """Build dict with daily max distribution for today+day_offset.

    Uses raw ensemble (no reweight) — appropriate for D+1 and D+2 where
    there are no observations yet. For D0 prefer build_snapshot.
    """
    target = datetime.now(station.tz).date() + timedelta(days=day_offset)
    times, members = fetch_ensemble(station)

    today_idx = []
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        if ts.date() == target:
            today_idx.append(i)

    raw_maxes = []
    for k, temps in members.items():
        vals = [temps[i] for i in today_idx if temps[i] is not None]
        if vals:
            raw_maxes.append(max(vals))

    if not raw_maxes:
        return {"target": target, "day_offset": day_offset,
                "daily_maxes": [], "p10": None, "p50": None, "p90": None,
                "n_members": 0}

    sm = sorted(raw_maxes)
    n = len(sm)
    return {
        "target": target,
        "day_offset": day_offset,
        "daily_maxes": raw_maxes,
        "p10": sm[int(n * 0.1)],
        "p50": sm[n // 2],
        "p90": sm[int(n * 0.9)],
        "n_members": n,
    }


def day_forecast(station: Station, day_offset: int) -> dict:
    """Distribution for a single day. day_offset=0 uses build_snapshot
    (reweight Bayesiano); 1/2 use raw ensemble."""
    if day_offset == 0:
        snap = build_snapshot(station)
        dist = sorted(snap.ensemble_daily_maxes)
        n = len(dist)
        return {
            "target": datetime.now(station.tz).date(),
            "day_offset": 0,
            "daily_maxes": snap.ensemble_daily_maxes,
            "p10": dist[int(n * 0.1)] if n else None,
            "p50": dist[n // 2] if n else None,
            "p90": dist[int(n * 0.9)] if n else None,
            "n_members": len(snap.ensemble_raw_maxes) or 31,
            "eff_n": snap.ensemble_eff_n,
            "max_obs": snap.today_max_obs,
            "current_temp": snap.current_temp_f,
            "regime_breaks": len(snap.regime_break_hours),
        }
    return _day_distribution(station, day_offset)
