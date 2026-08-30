#!/usr/bin/env python3
"""Qué le falta a cada una de las 20 estaciones: ¿nivel o anchura?

Sale del hallazgo del 2026-08-28 ([[dispersion_banda]]): la banda p10-p90 cubre
el 54.6% en vez del 80%, y sólo 2 de 20 estaciones llegan al nominal. Al
arreglar KMIA y KLAS apareció el criterio que separa los dos casos:

    residuo centrado      → falta ANCHURA   → dispersión mínima (KMIA, ±1.0°F)
    residuo descentrado   → falta NIVEL     → corrector          (KLAS, 9-13h)

Esto pasa ese diagnóstico a las 20. Es **descriptivo**: no adopta nada, produce
la tabla con la que después se pre-registra estación por estación.

CÓMO SE MIDE — «tal y como está configurado hoy»
------------------------------------------------
Por estación-día, al snapshot de las 12h locales:

  1. se deshace el bias que se aplicó ese día  → distribución CRUDA
  2. se aplica la configuración de HOY:
       · corrector causal si la estación está en `ENABLED_STATIONS`
         (respetando su ventana horaria si la tiene)
       · dispersión mínima si está en `SPREAD_MIN_F`
       · el piso de observación, que es lo último de la cadena en producción
  3. sobre esa distribución se miden cobertura y residuo

Así la tabla dice **qué falta todavía**, no qué faltaba antes de lo desplegado.
Los días de calentamiento del corrector (sin los 5 previos que exige) se
descartan en las estaciones habilitadas: en producción ya no ocurren.

Uso:  ./venv/bin/python3 ../investigacion/diagnostico_20.py [hora_local]
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ                    # noqa: E402
import level_corrector as lc                       # noqa: E402
import predictor as P                              # noqa: E402

UTC = ZoneInfo("UTC")
HORA = int(sys.argv[1]) if len(sys.argv) > 1 else 12
VENTANA_MIN = 45
CENTRADO_MAX = 0.75      # |mediana del residuo| por debajo de esto = centrado
COBERTURA_OK = (0.70, 0.90)


def dias_de(an, cal, st: str) -> list[dict]:
    """Un registro por estación-día con la distribución CRUDA (sin bias)."""
    tz = ZoneInfo(STATION_TZ[st])
    out = []
    for dia, settle in cal.execute(
            "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
            "AND max_obs_f IS NOT NULL ORDER BY date", (st,)).fetchall():
        try:
            d = datetime.strptime(dia, "%Y-%m-%d").date()
        except ValueError:
            continue
        ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=HORA)
        lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
        hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
        r = an.execute(
            """SELECT ens_med, ens_maxes_json, today_max_obs, bias_f, bias_applied
               FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
                 AND ens_maxes_json IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
            (st, lo.isoformat(), hi.isoformat(),
             ref.astimezone(UTC).isoformat())).fetchone()
        if r is None:
            continue
        try:
            miembros = json.loads(r["ens_maxes_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not miembros:
            continue
        # Se deshace el bias que el sistema aplicó ESE día: la distribución
        # guardada ya lo lleva restado (predictor.py resta a daily_maxes).
        b = r["bias_f"] if (r["bias_applied"] and r["bias_f"] is not None) else 0.0
        out.append({"dia": dia, "settle": settle,
                    "crudos": [v + b for v in miembros],
                    "max_obs": r["today_max_obs"]})
    return out


def configuracion_de_hoy(regs: list[dict], st: str) -> list[dict]:
    """Aplica a cada día lo que el sistema haría HOY con esa estación."""
    habilitada = st in lc.ENABLED_STATIONS and lc.hora_habilitada(st, HORA)
    m = P.SPREAD_MIN_F.get(st)
    out, sesgos = [], []
    for r in regs:
        med_crudo = statistics.median(r["crudos"])
        listo = len(sesgos) >= lc.MIN_PREV_DAYS
        b = statistics.median(sesgos) if (habilitada and listo) else 0.0
        sesgos.append(med_crudo - r["settle"])
        if habilitada and not listo:
            continue                       # día de calentamiento: hoy no ocurre
        piso = r["max_obs"] if r["max_obs"] is not None else -999.0
        v = [max(x - b, piso) for x in r["crudos"]]
        if m:
            v = [max(x, piso) for x in P.widen_min_spread(v, m)]
        v.sort()
        out.append({**r, "v": v, "med": v[len(v) // 2],
                    "p10": v[int(len(v) * 0.1)], "p90": v[int(len(v) * 0.9)]})
    return out


def m_necesario(regs: list[dict]) -> float | None:
    """Dispersión mínima adicional que llevaría la cobertura al 80%."""
    for i in range(1, 21):
        m = i / 4
        cub = 0
        for r in regs:
            piso = r["max_obs"] if r["max_obs"] is not None else -999.0
            v = sorted(max(x + o, piso) for x in r["v"] for o in (-m, 0.0, 0.0, m))
            if v[int(len(v) * 0.1)] <= r["settle"] <= v[int(len(v) * 0.9)]:
                cub += 1
        if cub / len(regs) >= 0.80:
            return m
    return None


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    print(f"Diagnóstico por estación — {HORA}h local, con la configuración de hoy")
    print(f"corrector activo: {', '.join(sorted(lc.ENABLED_STATIONS))}"
          f"   ·   dispersión mínima: {P.SPREAD_MIN_F}\n")
    print(f"  {'est':6s} {'N':>3s} {'cubre':>6s} {'>p90':>5s} {'<p10':>5s} "
          f"{'ancho':>6s} {'residuo':>8s} {'|err|':>6s}  falta          receta")
    filas = []
    for st in sorted(STATION_TZ):
        regs = configuracion_de_hoy(dias_de(an, cal, st), st)
        if len(regs) < 10:
            continue
        n = len(regs)
        arr = sum(1 for r in regs if r["settle"] > r["p90"]) / n
        aba = sum(1 for r in regs if r["settle"] < r["p10"]) / n
        cub = 1 - arr - aba
        res = statistics.median(r["settle"] - r["med"] for r in regs)
        err = statistics.median(abs(r["med"] - r["settle"]) for r in regs)
        ancho = statistics.median(r["p90"] - r["p10"] for r in regs)
        if COBERTURA_OK[0] <= cub <= COBERTURA_OK[1]:
            falta, receta = "—", "nada"
        elif abs(res) > CENTRADO_MAX:
            falta = "NIVEL"
            receta = f"corrector {-res:+.1f}°F"
        else:
            falta = "ANCHURA"
            mm = m_necesario(regs)
            receta = f"dispersión ±{mm}°F" if mm else "ni ±5°F basta"
        filas.append((st, falta))
        print(f"  {st:6s} {n:3d} {100*cub:5.0f}% {100*arr:4.0f}% {100*aba:4.0f}% "
              f"{ancho:6.2f} {res:+8.2f} {err:6.2f}  {falta:8s}  {receta}")

    print()
    for etiqueta in ("—", "NIVEL", "ANCHURA"):
        v = [s for s, f in filas if f == etiqueta]
        nombre = {"—": "ya calibradas", "NIVEL": "les falta nivel",
                  "ANCHURA": "les falta anchura"}[etiqueta]
        print(f"  {nombre:20s} {len(v):2d}  {' '.join(v)}")
    print("\n  residuo = mediana de (settle − nuestra mediana). Positivo = nos "
          "quedamos cortos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
