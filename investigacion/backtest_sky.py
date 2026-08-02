#!/usr/bin/env python3
"""¿La nubosidad y la radiación explican el error del modelo?

CONTEXTO
--------
Los tres días que medí con detalle fallaron en la subida de la tarde: KDFW subió
+1.0 cuando esperaba +2.2, KATL +0.1 cuando esperaba +1.4, y KATL otro día +5
cuando esperaba +1.6. Ninguna variable del sistema captura lo que gobierna esa
subida — cuánta energía entra y cuánto la bloquean las nubes.

En vez de instrumentar y esperar semanas, Open-Meteo sirve **archive** de esas
variables, así que se puede medir sobre los días que ya tienen settle.

=============================== PRE-REGISTRO ================================
Escrito ANTES de mirar resultados. Commiteado antes de la primera corrida.

H0: ni la radiación acumulada ni la nubosidad explican el error del modelo.

NOTA SOBRE REUTILIZACIÓN DE DATOS
  Julio ya lleva doce backtests, pero **estas variables son nuevas**: no han
  participado en ninguno. El riesgo de encontrar algo por azar viene de probar
  muchas hipótesis sobre las mismas variables, no de reusar el calendario. Aun
  así, se prueban SÓLO las cuatro listadas abajo y no se añaden más si fallan.

VARIABLES (las cuatro, decididas de antemano)
  sw_manana   = radiación acumulada 06:00-12:00 local (kWh/m²)
                -> cuánta energía ha entrado ANTES del momento de predecir
  sw_pico     = radiación acumulada durante la ventana de pico
                -> cuánta entrará DESPUÉS
  nubes_pico  = cloud_cover medio en la ventana de pico (%)
  nubes_bajas = cloud_cover_low medio en la ventana de pico (%)
                -> las bajas bloquean más sol; separadas a propósito porque
                   KSFO/KLAX tienen marine layer y el total no lo distingue

OBJETIVO
  error = ens_med(mediodía local) - settle
  Se usa el error FIRMADO, no el absoluto: la pregunta es si estas variables
  explican la DIRECCIÓN (sobre/sub-predecir), que es lo accionable.

CRITERIO DE DECISIÓN
  INSTRUMENTAR una variable si |Spearman| > 0.30 con N >= 100 y p < 0.01
  ZONA GRIS      0.15 < |rho| <= 0.30 -> no instrumentar, anotar y volver
  DESCARTAR      |rho| <= 0.15

  Si ninguna pasa, la conclusión es que el error de la subida de la tarde NO se
  explica por energía entrante, y hay que buscar en otro sitio (advección,
  mezcla vertical, humedad del suelo). Eso también es información útil.

COSTE
  1 llamada de archive por estación (20 en total). Cuota actual 1703/10000.
=============================================================================
"""
from __future__ import annotations

import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from predictor import fetch_station           # noqa: E402
from stations import STATION_TZ, PEAK_HOURS   # noqa: E402

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
UA = "weather-predictor/0.1 (educational)"
UTC = ZoneInfo("UTC")


def spearman(xs, ys):
    n = len(xs)
    if n < 10:
        return float("nan"), float("nan")

    def rk(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rk(xs), rk(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if not dx or not dy:
        return float("nan"), float("nan")
    rho = num / (dx * dy)
    z = 0.5 * math.log((1 + rho) / (1 - rho)) * math.sqrt(n - 3) if abs(rho) < 1 else 9
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return rho, p


def recoger_filas() -> list[dict]:
    """Extraída de main() sin cambios de lógica, para que el control de
    robustez (backtest_sky_control.py) reuse exactamente los mismos datos."""
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    settles = {}
    for st, d, m in cal.execute(
            "SELECT station_id, date, max_obs_f FROM day_outcomes"):
        if m is not None:
            settles.setdefault(st, {})[d] = m

    filas = []
    for st in sorted(settles):
        if st not in STATION_TZ:
            continue
        days = sorted(settles[st])
        if len(days) < 5:
            continue
        s = fetch_station(st)
        tz = ZoneInfo(STATION_TZ[st])
        try:
            r = requests.get(ARCHIVE, params={
                "latitude": s.lat, "longitude": s.lon,
                "start_date": days[0], "end_date": days[-1],
                "hourly": ("cloud_cover,cloud_cover_low,shortwave_radiation"),
                "timezone": STATION_TZ[st],
            }, headers={"User-Agent": UA}, timeout=40)
            h = r.json().get("hourly") or {}
        except Exception as e:
            print(f"{st}: archive falló ({e})")
            continue
        times = h.get("time") or []
        idx = {}
        for i, t in enumerate(times):
            dt = datetime.fromisoformat(t)
            idx.setdefault(dt.date().isoformat(), []).append((dt.hour, i))

        lo_p, hi_p = PEAK_HOURS[st]
        for day in days:
            horas = idx.get(day)
            if not horas:
                continue
            noon = datetime.combine(datetime.strptime(day, "%Y-%m-%d").date(),
                                    datetime.min.time(), tz) + timedelta(hours=12)
            snap = an.execute(
                """SELECT ens_med FROM station_snapshots
                   WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
                   ORDER BY ts LIMIT 1""",
                (st,
                 (noon - timedelta(minutes=90)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                 (noon + timedelta(minutes=90)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))
            ).fetchone()
            if snap is None:
                continue

            def agg(campo, h0, h1, media=False):
                arr = h.get(campo) or []
                vals = [arr[i] for hh, i in horas
                        if h0 <= hh < h1 and i < len(arr) and arr[i] is not None]
                if not vals:
                    return None
                return statistics.mean(vals) if media else sum(vals) / 1000.0

            filas.append({
                "st": st, "day": day,
                "err": snap["ens_med"] - settles[st][day],
                "sw_manana": agg("shortwave_radiation", 6, 12),
                "sw_pico": agg("shortwave_radiation", lo_p, hi_p),
                "nubes_pico": agg("cloud_cover", lo_p, hi_p, True),
                "nubes_bajas": agg("cloud_cover_low", lo_p, hi_p, True),
            })

    return filas


def main() -> int:
    filas = recoger_filas()
    if not filas:
        print("sin datos")
        return 1
    print(f"station-days: {len(filas)}   estaciones: "
          f"{len({f['st'] for f in filas})}\n")
    print(f"{'variable':14s} {'N':>5s} {'rho':>7s} {'p':>10s}   veredicto")
    for var in ("sw_manana", "sw_pico", "nubes_pico", "nubes_bajas"):
        par = [(f[var], f["err"]) for f in filas if f[var] is not None]
        if len(par) < 10:
            print(f"{var:14s} {len(par):5d}   (N insuficiente)")
            continue
        rho, p = spearman([a for a, _ in par], [b for _, b in par])
        if abs(rho) > 0.30 and len(par) >= 100 and p < 0.01:
            v = "INSTRUMENTAR"
        elif abs(rho) > 0.15:
            v = "zona gris"
        else:
            v = "descartar"
        print(f"{var:14s} {len(par):5d} {rho:+7.3f} {p:10.2e}   {v}")

    print("\nerror mediano por cuartil de nubosidad en la ventana de pico:")
    con_n = [f for f in filas if f["nubes_pico"] is not None]
    con_n.sort(key=lambda f: f["nubes_pico"])
    q = max(1, len(con_n) // 4)
    for i, lab in enumerate(("Q1 (más despejado)", "Q2", "Q3", "Q4 (más nublado)")):
        sub = con_n[i * q:(i + 1) * q] if i < 3 else con_n[3 * q:]
        if sub:
            print(f"  {lab:20s} N={len(sub):4d}  nubes "
                  f"{statistics.median(f['nubes_pico'] for f in sub):5.1f}%  "
                  f"error {statistics.median(f['err'] for f in sub):+.2f}°F")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
