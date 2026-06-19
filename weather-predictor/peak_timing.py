"""Peak timing: distribución de la hora del máximo diario.

Por cada miembro del ensemble, combinamos observaciones pasadas con
forecast futuro para encontrar la hora (0-23) del peak hoy. Aplicamos
los mismos pesos Bayesianos que build_snapshot y devolvemos estadísticas
ponderadas.
"""
from datetime import datetime
from math import exp

from predictor import fetch_ensemble, fetch_today_obs, Station

SIGMA = 2.0


def _compute_weights(member_keys, members, times, hour_obs, today, current_hour, tz):
    matched = [[] for _ in member_keys]
    residual_hours = 0
    hours_seen = set()
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=tz)
        if ts.date() != today or ts > current_hour:
            continue
        obs_v = hour_obs.get(ts.hour)
        if obs_v is None:
            continue
        if ts.hour not in hours_seen:
            hours_seen.add(ts.hour)
            residual_hours += 1
        for mi, k in enumerate(member_keys):
            f = members[k][i]
            if f is None:
                continue
            matched[mi].append((f, obs_v))

    n = len(member_keys)
    if residual_hours >= 2 and any(matched):
        sses = [sum((f - o) ** 2 for f, o in m) if m else 0.0 for m in matched]
        min_sse = min(sses)
        raw = [exp(-(s - min_sse) / (2 * SIGMA * SIGMA)) for s in sses]
        z = sum(raw) or 1.0
        weights = [w / z for w in raw]
    else:
        weights = [1.0 / n] * n if n else []
    eff_n = 1.0 / sum(w * w for w in weights) if weights else 0.0
    return weights, eff_n, residual_hours


def compute(station: Station) -> dict:
    """Return peak timing stats for `station` for today."""
    times, members = fetch_ensemble(station)
    obs_full = fetch_today_obs(station)

    now_local = datetime.now(station.tz)
    today = now_local.date()
    current_hour_dt = now_local.replace(minute=0, second=0, microsecond=0)
    current_hour_int = now_local.hour

    hour_obs = {}
    for o in obs_full:
        if o["temp_f"] is None:
            continue
        tl = o["time"].astimezone(station.tz)
        if tl.date() != today:
            continue
        prev = hour_obs.get(tl.hour)
        if prev is None or o["temp_f"] > prev:
            hour_obs[tl.hour] = o["temp_f"]
    max_obs = max(hour_obs.values()) if hour_obs else None
    max_obs_hour = (max(hour_obs, key=hour_obs.get)
                    if hour_obs else None)

    today_idx = []
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        if ts.date() == today:
            today_idx.append((i, ts.hour))

    member_keys = list(members.keys())
    weights, eff_n, residual_hours = _compute_weights(
        member_keys, members, times, hour_obs, today, current_hour_dt, station.tz
    )

    peak_hours = []
    peak_temps = []
    for mi, k in enumerate(member_keys):
        temps = members[k]
        best_h = max_obs_hour
        best_t = max_obs if max_obs is not None else -9999.0
        for i, hour in today_idx:
            ts = datetime.fromisoformat(times[i]).replace(tzinfo=station.tz)
            if ts <= current_hour_dt:
                continue
            v = temps[i]
            if v is None:
                continue
            if v > best_t:
                best_t = v
                best_h = hour
        if best_h is None:
            continue
        peak_hours.append(best_h)
        peak_temps.append(best_t)

    hour_hist = {}
    for h, w in zip(peak_hours, weights):
        hour_hist[h] = hour_hist.get(h, 0.0) + w
    total_w = sum(hour_hist.values()) or 1.0
    hour_hist = {h: w / total_w for h, w in hour_hist.items()}

    sorted_pairs = sorted(zip(peak_hours, weights))
    cum = 0.0
    p10 = p50 = p90 = None
    for h, w in sorted_pairs:
        cum += w / total_w
        if p10 is None and cum >= 0.10:
            p10 = h
        if p50 is None and cum >= 0.50:
            p50 = h
        if p90 is None and cum >= 0.90:
            p90 = h
            break
    modal_hour = max(hour_hist, key=hour_hist.get) if hour_hist else None

    prob_already = sum(w for h, w in zip(peak_hours, weights)
                       if h <= current_hour_int) / total_w
    next_n = {}
    for n in (1, 2, 3, 6):
        next_n[n] = sum(w for h, w in zip(peak_hours, weights)
                        if current_hour_int < h <= current_hour_int + n) / total_w

    return {
        "station_id": station.id,
        "today": today.isoformat(),
        "current_hour": current_hour_int,
        "hour_hist": hour_hist,
        "modal_hour": modal_hour,
        "p10": p10,
        "p50": p50,
        "p90": p90,
        "prob_already": prob_already,
        "prob_next_n": next_n,
        "eff_n": eff_n,
        "residual_hours": residual_hours,
        "n_members": len(member_keys),
        "max_obs": max_obs,
        "max_obs_hour": max_obs_hour,
    }
