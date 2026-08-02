#!/usr/bin/env python3
"""Probe: ¿se puede calcular advección de temperatura con Open-Meteo?

Antes de diseñar nada (doctrina de la casa), verificar:
  1. que el archive acepta MULTI-PUNTO en una llamada (si no, 5x el coste)
  2. que el signo de la advección sale físicamente correcto

Sanity check del punto 2: KSFO por la tarde tiene brisa marina — viento del
oeste trayendo aire frío del Pacífico. La advección DEBE salir negativa (fría).
Si sale positiva, el cálculo está mal y cualquier correlación posterior sería
basura. KPHX en verano no tiene mar cerca y debería dar valores mucho menores.

Advección de temperatura:  A = -(u·∂T/∂x + v·∂T/∂y)
  u = -ws·sin(θ)   v = -ws·cos(θ)     θ = dirección METEOROLÓGICA (de dónde
                                          viene), por eso los signos negativos
  A > 0  entra aire más cálido        A < 0  entra aire más frío

Read-only. No toca el poller ni la DB.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path

import requests

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from predictor import fetch_station          # noqa: E402
from stations import PEAK_HOURS, STATION_TZ  # noqa: E402

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
UA = "weather-predictor/0.1 (educational)"
DELTA_DEG = 0.5          # ~55 km; compromiso entre mesoescala y ruido de celda
VARS = "temperature_2m,wind_speed_10m,wind_direction_10m"


def advection_c_per_h(t_c, t_n, t_s, t_e, t_w, ws_kmh, wd_deg, lat):
    """°C/h. Devuelve None si falta algo."""
    if None in (t_n, t_s, t_e, t_w, ws_kmh, wd_deg):
        return None
    dy_m = DELTA_DEG * 111_320.0 * 2
    dx_m = DELTA_DEG * 111_320.0 * math.cos(math.radians(lat)) * 2
    if dx_m <= 0:
        return None
    dTdy = (t_n - t_s) / dy_m
    dTdx = (t_e - t_w) / dx_m
    ws = ws_kmh / 3.6
    th = math.radians(wd_deg)
    u, v = -ws * math.sin(th), -ws * math.cos(th)
    return -(u * dTdx + v * dTdy) * 3600.0


def probe(st: str, day: str) -> None:
    s = fetch_station(st)
    d = DELTA_DEG
    lats = [s.lat, s.lat + d, s.lat - d, s.lat, s.lat]
    lons = [s.lon, s.lon, s.lon, s.lon + d, s.lon - d]
    r = requests.get(ARCHIVE, params={
        "latitude": ",".join(f"{x:.4f}" for x in lats),
        "longitude": ",".join(f"{x:.4f}" for x in lons),
        "start_date": day, "end_date": day,
        "hourly": VARS, "timezone": STATION_TZ[st],
    }, headers={"User-Agent": UA}, timeout=40)
    if r.status_code != 200:
        print(f"{st}: HTTP {r.status_code} — {r.text[:200]}")
        return
    js = r.json()
    if not isinstance(js, list):
        print(f"{st}: ⚠ multi-punto NO soportado, devolvió un solo objeto")
        return
    if len(js) != 5:
        print(f"{st}: ⚠ esperaba 5 puntos, llegaron {len(js)}")
        return

    h = [p["hourly"] for p in js]
    times = h[0]["time"]
    lo, hi = PEAK_HOURS[st]
    print(f"\n=== {st}  {day}   ventana de pico {lo}-{hi}h local")
    print(f"  {'hora':>5s} {'T':>6s} {'viento':>13s} {'grad N-S':>9s} "
          f"{'grad E-W':>9s} {'ADVEC':>8s}")
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t)
        if not (6 <= dt.hour <= 20):
            continue
        tc, tn, ts, te, tw = (h[k]["temperature_2m"][i] for k in range(5))
        ws = h[0]["wind_speed_10m"][i]
        wd = h[0]["wind_direction_10m"][i]
        a = advection_c_per_h(tc, tn, ts, te, tw, ws, wd, s.lat)
        marca = " <- pico" if lo <= dt.hour < hi else ""
        av = f"{a:+7.2f}" if a is not None else "      -"
        print(f"  {dt:%H:%M} {tc:6.1f} {ws:7.1f}km/h {wd:3.0f}° "
              f"{tn - ts:+9.1f} {te - tw:+9.1f} {av}{marca}")
    print("  (grad N-S y E-W en °C sobre ~110 km; ADVEC en °C/h)")


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-07-20"
    for st in ("KSFO", "KPHX"):
        probe(st, day)
