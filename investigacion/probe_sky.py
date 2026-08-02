#!/usr/bin/env python3
"""Probe: ¿qué da Open-Meteo de nubosidad y radiación, y sirve para el pico?

Antes de diseñar nada, verificar (doctrina de la casa):
  - qué variables responde y con qué granularidad
  - si la radiación acumulada hasta el mediodía discrimina días
  - coste en cuota: 1 llamada por estación, con TTL sería ~640/día sobre las
    1703 actuales (límite 10k)

Read-only. No toca el poller ni la DB.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import requests

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from predictor import fetch_station          # noqa: E402
from stations import PEAK_HOURS              # noqa: E402

OM = "https://api.open-meteo.com/v1/forecast"
UA = "weather-predictor/0.1 (educational)"
VARS = ("cloud_cover,cloud_cover_low,shortwave_radiation,"
        "direct_normal_irradiance")


def probe(st: str) -> None:
    s = fetch_station(st)
    r = requests.get(OM, params={
        "latitude": s.lat, "longitude": s.lon,
        "hourly": VARS,
        "past_hours": 6, "forecast_hours": 8,
        "timezone": s.tz.key,
    }, headers={"User-Agent": UA}, timeout=25)
    if r.status_code != 200:
        print(f"{st}: HTTP {r.status_code}")
        return
    h = r.json().get("hourly") or {}
    times = h.get("time") or []
    lo, hi = PEAK_HOURS[st]
    print(f"\n=== {st}   ventana de pico {lo}-{hi}h local")
    print(f"  {'hora':>6s} {'nubes%':>7s} {'bajas%':>7s} {'SW W/m2':>8s} {'DNI':>6s}")
    acc = 0.0
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t)
        cc = (h.get("cloud_cover") or [None] * len(times))[i]
        cl = (h.get("cloud_cover_low") or [None] * len(times))[i]
        sw = (h.get("shortwave_radiation") or [None] * len(times))[i]
        dni = (h.get("direct_normal_irradiance") or [None] * len(times))[i]
        if sw:
            acc += sw
        marca = " <- pico" if lo <= dt.hour < hi else ""
        print(f"  {dt:%H:%M} {str(cc):>7s} {str(cl):>7s} {str(sw):>8s} "
              f"{str(dni):>6s}{marca}")
    print(f"  radiación acumulada en la ventana mostrada: {acc / 1000:.2f} kWh/m²")


if __name__ == "__main__":
    for st in (sys.argv[1:] or ["KPHX", "KSFO", "KMIA"]):
        probe(st.upper())
