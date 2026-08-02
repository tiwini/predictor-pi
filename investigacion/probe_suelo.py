#!/usr/bin/env python3
"""Probe: ¿la humedad del suelo se mueve lo suficiente para explicar algo?

Antes de diseñar (doctrina de la casa), verificar:
  1. que archive sirve soil_moisture y precipitación
  2. cuánto VARÍA dentro de cada estación — si en julio KPHX está clavada en
     0.05 m³/m³ todos los días, ahí no puede explicar nada y hay que excluirla
     por varianza, no descubrirlo luego como ruido
  3. si un evento de lluvia se ve claramente en la serie (sanity check físico)

KMSY tiene convección de verano casi diaria; KPHX es desierto. Deberían salir
en extremos opuestos. Si NO se distinguen, la variable está mal leída.

Read-only.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

import requests

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from predictor import fetch_station          # noqa: E402
from stations import STATION_TZ              # noqa: E402

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
UA = "weather-predictor/0.1 (educational)"
VARS = ("soil_moisture_0_to_7cm,soil_moisture_7_to_28cm,"
        "precipitation,et0_fao_evapotranspiration")


def probe(st: str, ini: str, fin: str) -> None:
    s = fetch_station(st)
    r = requests.get(ARCHIVE, params={
        "latitude": s.lat, "longitude": s.lon,
        "start_date": ini, "end_date": fin,
        "hourly": VARS, "timezone": STATION_TZ[st],
    }, headers={"User-Agent": UA}, timeout=40)
    if r.status_code != 200:
        print(f"{st}: HTTP {r.status_code} — {r.text[:200]}")
        return
    h = r.json().get("hourly") or {}
    if not h:
        print(f"{st}: sin datos horarios")
        return

    faltan = [v for v in VARS.split(",") if v not in h]
    if faltan:
        print(f"{st}: ⚠ el archive NO sirve {faltan}")

    times = h["time"]
    # una muestra diaria a las 06h local (antes del calentamiento)
    sm0, sm7, lluvia = {}, {}, {}
    for i, t in enumerate(times):
        day, hh = t[:10], int(t[11:13])
        p = (h.get("precipitation") or [None] * len(times))[i]
        if p is not None:
            lluvia[day] = lluvia.get(day, 0.0) + p
        if hh == 6:
            a = (h.get("soil_moisture_0_to_7cm") or [None] * len(times))[i]
            b = (h.get("soil_moisture_7_to_28cm") or [None] * len(times))[i]
            if a is not None:
                sm0[day] = a
            if b is not None:
                sm7[day] = b

    v0 = list(sm0.values())
    if not v0:
        print(f"{st}: sin soil_moisture")
        return
    q = statistics.quantiles(v0, n=4) if len(v0) >= 4 else [0, 0, 0]
    print(f"\n=== {st}   {ini} .. {fin}   N={len(v0)} días")
    print(f"  soil_moisture_0_to_7cm  min {min(v0):.3f}  mediana "
          f"{statistics.median(v0):.3f}  max {max(v0):.3f}  IQR "
          f"{q[2] - q[0]:.4f} m³/m³")
    if sm7:
        v7 = list(sm7.values())
        print(f"  soil_moisture_7_to_28cm min {min(v7):.3f}  mediana "
              f"{statistics.median(v7):.3f}  max {max(v7):.3f}")
    tot = sum(lluvia.values())
    dias_lluvia = sum(1 for v in lluvia.values() if v >= 1.0)
    print(f"  precipitación total {tot:.1f} mm en {len(lluvia)} días; "
          f"{dias_lluvia} días con >= 1 mm")

    # sanity físico: ¿el mayor evento de lluvia sube la humedad al día siguiente?
    if lluvia:
        dmax = max(lluvia, key=lambda d: lluvia[d])
        ds = sorted(sm0)
        if dmax in ds and ds.index(dmax) + 1 < len(ds):
            sig = ds[ds.index(dmax) + 1]
            print(f"  mayor evento: {dmax} con {lluvia[dmax]:.1f} mm  ->  "
                  f"sm pasó de {sm0[dmax]:.3f} a {sm0[sig]:.3f} "
                  f"({sm0[sig] - sm0[dmax]:+.3f})")


if __name__ == "__main__":
    ini = sys.argv[1] if len(sys.argv) > 1 else "2026-07-01"
    fin = sys.argv[2] if len(sys.argv) > 2 else "2026-07-30"
    for st in ("KMSY", "KPHX", "KATL", "KSFO"):
        probe(st, ini, fin)
