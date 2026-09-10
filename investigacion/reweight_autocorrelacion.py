#!/usr/bin/env python3
"""¿Cuánta información NUEVA trae cada hora en el reweight bayesiano?

El reweight pesa cada miembro del ensemble por `w ∝ exp(−SSE/2)` con
`SSE = Σ_h ((f_h − o_h)/σ_h)²` sumado sobre TODAS las horas casadas del día
(`predictor.py:1370`). Esa suma es la log-verosimilitud gaussiana de
observaciones **independientes**.

No lo son. Si el miembro que va caliente a las 9h es el mismo que va caliente a
las 12h, la segunda hora no aporta evidencia nueva sobre qué miembro es mejor:
repite la primera. Cada repetición multiplica el exponente, los pesos colapsan
sobre uno o dos miembros y la distribución remuestreada acaba teniendo dos o
tres valores distintos — que es de donde sale la banda que se estrecha más
rápido que el error.

Lo que se mide aquí, **sin cambiar nada de lo publicado**:

  ρ̄   correlación media entre pares de HORAS del vector de residuales a través
      de los miembros. ρ̄→1 significa "todas las horas ordenan igual".
  deff  efecto de diseño, 1 + (n_horas − 1)·ρ̄. Cuántas veces está inflado el
        exponente.
  n_eff_horas = n_horas / deff. Cuántas horas independientes hay de verdad.

Y qué pasaría con el arreglo candidato: dividir el SSE por `deff`, que no
introduce ningún parámetro libre — sale del propio dato de esa estación y ese
momento.

Sólo lee: no escribe en ninguna DB ni toca la predicción.
"""
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))

from datetime import datetime  # noqa: E402
import predictor as P  # noqa: E402
from stations import STATIONS  # noqa: E402


def corr(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    if sx < 1e-9 or sy < 1e-9:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n * sx * sy)


def kish(ws):
    s = sum(ws)
    if s <= 0:
        return float("nan")
    w = [x / s for x in ws]
    return 1.0 / sum(x * x for x in w)


def percentiles(vals, ws, ps=(0.10, 0.90)):
    pares = sorted(zip(vals, ws))
    tot = sum(ws)
    out, acc, i = [], 0.0, 0
    for p in ps:
        objetivo = p * tot
        while i < len(pares) and acc + pares[i][1] < objetivo:
            acc += pares[i][1]
            i += 1
        out.append(pares[min(i, len(pares) - 1)][0])
    return out


def analizar(sid):
    station = P.fetch_station(sid)
    times, members = P.fetch_ensemble(station)
    obs_full = P.fetch_today_obs(station)
    now_local = datetime.now(station.tz)
    today = now_local.date()
    current_hour = now_local.replace(minute=0, second=0, microsecond=0)

    hour_obs = {}
    for o in obs_full:
        if o["temp_f"] is None:
            continue
        tl = o["time"].astimezone(station.tz)
        if tl.date() == today:
            hour_obs[tl.hour] = o["temp_f"]

    member_keys = list(members.keys())
    # residuales[mi] = [(hora, residual)], y el máximo del día por miembro
    residuales = {mi: [] for mi in range(len(member_keys))}
    horas = []
    maxes = [None] * len(member_keys)
    for i, ts_str in enumerate(times):
        ts = datetime.fromisoformat(ts_str).replace(tzinfo=station.tz)
        if ts.date() != today:
            continue
        for mi, k in enumerate(member_keys):
            f = members[k][i]
            if f is None:
                continue
            maxes[mi] = f if maxes[mi] is None else max(maxes[mi], f)
        if ts > current_hour:
            continue
        obs_v = hour_obs.get(ts.hour)
        if obs_v is None:
            continue
        horas.append(ts.hour)
        for mi, k in enumerate(member_keys):
            f = members[k][i]
            if f is not None:
                residuales[mi].append((ts.hour, f - obs_v))

    n_h = len(horas)
    if n_h < 3 or any(m is None for m in maxes):
        return None

    # matriz miembros × horas
    R = [[r for _, r in sorted(residuales[mi])] for mi in range(len(member_keys))]
    R = [fila for fila in R if len(fila) == n_h]
    if len(R) < 5:
        return None

    cols = list(zip(*R))
    cs = [corr(cols[a], cols[b])
          for a in range(n_h) for b in range(a + 1, n_h)]
    cs = [c for c in cs if c is not None]
    if not cs:
        return None
    rho = statistics.mean(cs)
    deff = max(1.0, 1.0 + (n_h - 1) * rho)

    sses = []
    for fila, (mi) in zip(R, range(len(R))):
        sse = sum((r / P.sigma_for_hour(h, sid)) ** 2
                  for h, r in zip(sorted(horas), fila))
        sses.append(sse)
    mn = min(sses)
    w_act = [pow(2.718281828, -(s - mn) / 2.0) for s in sses]
    w_new = [pow(2.718281828, -(s - mn) / (2.0 * deff)) for s in sses]

    mx = [maxes[i] for i in range(len(R))]
    p10a, p90a = percentiles(mx, w_act)
    p10n, p90n = percentiles(mx, w_new)
    return {
        "st": sid, "n_h": n_h, "rho": rho, "deff": deff,
        "n_eff_h": n_h / deff,
        "effN_act": kish(w_act), "effN_new": kish(w_new),
        "ancho_act": p90a - p10a, "ancho_new": p90n - p10n,
    }


def main():
    print("| est | horas | ρ̄ entre horas | deff | horas efectivas | "
          "eff_N ahora | eff_N con deff | ancho p10-p90 ahora | con deff |")
    print("|---|---|---|---|---|---|---|---|---|")
    filas = []
    for s in STATIONS:
        sid = s.id if hasattr(s, "id") else s
        try:
            r = analizar(sid)
        except Exception as e:
            print(f"| {sid} | error: {str(e)[:40]} |")
            continue
        if r is None:
            print(f"| {sid} | sin horas casadas suficientes |")
            continue
        filas.append(r)
        print(f"| {r['st']} | {r['n_h']} | {r['rho']:.3f} | {r['deff']:.1f} | "
              f"{r['n_eff_h']:.2f} | {r['effN_act']:.2f} | {r['effN_new']:.2f} | "
              f"{r['ancho_act']:.2f}°F | {r['ancho_new']:.2f}°F |")
    if filas:
        print()
        print(f"**N={len(filas)} estaciones · ρ̄ mediana "
              f"{statistics.median([f['rho'] for f in filas]):.3f} · "
              f"deff mediano {statistics.median([f['deff'] for f in filas]):.1f} · "
              f"eff_N {statistics.median([f['effN_act'] for f in filas]):.2f} → "
              f"{statistics.median([f['effN_new'] for f in filas]):.2f} · "
              f"ancho {statistics.median([f['ancho_act'] for f in filas]):.2f} → "
              f"{statistics.median([f['ancho_new'] for f in filas]):.2f}°F**")


if __name__ == "__main__":
    sys.exit(main())
