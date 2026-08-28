#!/usr/bin/env python3
"""¿Un reweight colapsado deja de APRENDER de las observaciones nuevas?

Nace del registro predictivo de KDEN del 2026-08-25 (settle 85.0, la nota
acertó): entre dos snapshots `today_max_obs` saltó de 78.1 a 82.0 y `ens_med`
no se movió ni una décima, con `eff_N = 1.5 de 31`.

⚠ NO reabre el backtest del 2026-07-27, que midió `eff_N` contra el NIVEL del
error y lo descartó (N=164, rho=+0.070, p=0.373). Aquí la cantidad medida es la
RESPUESTA a un dato nuevo, que nunca se ha medido.

=========================== CRITERIO PRE-REGISTRADO ==========================
Escrito el 2026-08-28 ANTES de correr nada.

Unidad: par de snapshots consecutivos (<=20 min) de la misma estación y el mismo
día local, donde `today_max_obs` sube >= 1.0°F —o sea, entró información nueva.

  · El piso NO puede atar en ninguno de los dos extremos:
    `ens_med > max(today_max_obs, current_f - 0.9) + 0.5`.
    Sin esto se mide el clamp, que sube la predicción por definición, y no el
    reweight.
  · Respuesta  r = Δ(ens_med sin bias) / Δtoday_max_obs.  El bias se deshace
    porque cambia por hora y contaminaría la diferencia.
  · Grupos por eff_N: colapsado (<3), medio (3-10), alto (10-18.6), sano
    (>18.6 — censurado: `difficulty` sólo escribe el número cuando el ratio
    baja de 0.6).

  CONFIRMADO si las tres:
    (1) N>=100 pares en colapsado y N>=100 en sano,
    (2) mediana de r del colapsado <= 0.5 x la del sano,
    (3) el signo se repite en >=2/3 de las estaciones con >=10 pares en ambos
        grupos (test de signos por estación, no pool cruzado).
  GRIS   si la razón cae entre 0.5 y 0.8.
  REFUTADO si la razón > 0.8, o si el signo no se sostiene por estación.

Control obligatorio: mismo cálculo por franja horaria local. eff_N y el margen
de error caen los dos según avanza la tarde, así que el pool sin partir puede
fabricar el efecto solo.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/reweight_colapsado.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import comb
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ            # noqa: E402

SALTO_MIN_F = 1.0        # cuánto tiene que subir max_obs para contar como dato nuevo
GAP_MAX_MIN = 20         # el poller va a 10 min; 20 tolera un ciclo perdido
MARGEN_PISO_F = 0.5      # holgura exigida sobre el piso en ambos extremos
CENSURA = 18.6           # 0.6 * 31: por encima de aquí difficulty no escribe el número

RE_EFFN = re.compile(r"eff_N=([\d.]+)/(\d+)")


def p_signos(k: int, n: int) -> float:
    """p de dos colas del test de signos con p0=0.5."""
    if n == 0:
        return 1.0
    k = max(k, n - k)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n)


def eff_n_de(reasons_json: str | None) -> float | None:
    """eff_N del snapshot. None = difficulty no corrió (desconocido).

    Devuelve `CENSURA + 1` cuando difficulty SÍ corrió y no escribió la razón:
    eso significa ratio > 0.6, o sea sano. Es censura por la derecha, no dato
    ausente, y distinguirlas importa — mezclarlas mete los snapshots sin
    ensemble en el grupo sano.
    """
    if reasons_json is None:
        return None
    try:
        razones = json.loads(reasons_json)
    except (json.JSONDecodeError, TypeError):
        return None
    for r in razones:
        m = RE_EFFN.search(r)
        if m:
            return float(m.group(1))
    return CENSURA + 1.0


def grupo_de(eff: float | None) -> str | None:
    if eff is None:
        return None
    if eff < 3:
        return "colapsado"
    if eff < 10:
        return "medio"
    if eff <= CENSURA:
        return "alto"
    return "sano"


def franja(hora: float) -> str:
    if hora < 10:
        return "madrugada-10h"
    if hora < 13:
        return "10-13h"
    if hora < 16:
        return "13-16h"
    return "16h-noche"


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row

    filas = an.execute(
        """SELECT ts, station, current_f, today_max_obs, ens_med, ens_p10, bias_f,
                  bias_applied, difficulty_reasons_json
           FROM station_snapshots
           WHERE ens_med IS NOT NULL AND today_max_obs IS NOT NULL
           ORDER BY station, ts""").fetchall()

    por_est: dict[str, list] = defaultdict(list)
    for r in filas:
        por_est[r["station"]].append(r)

    # ---------- A. ¿Cada cuánto colapsa? ----------
    print("A. Frecuencia del colapso (todos los snapshots con difficulty)\n")
    cuenta = defaultdict(lambda: [0, 0, 0])       # est -> [total, <3, <2]
    por_franja = defaultdict(lambda: [0, 0])      # franja -> [total, <3]
    for st, rs in por_est.items():
        tz = ZoneInfo(STATION_TZ[st]) if st in STATION_TZ else timezone.utc
        for r in rs:
            eff = eff_n_de(r["difficulty_reasons_json"])
            if eff is None:
                continue
            cuenta[st][0] += 1
            if eff < 3:
                cuenta[st][1] += 1
            if eff < 2:
                cuenta[st][2] += 1
            loc = datetime.fromisoformat(r["ts"]).astimezone(tz)
            f = franja(loc.hour + loc.minute / 60)
            por_franja[f][0] += 1
            por_franja[f][1] += 1 if eff < 3 else 0

    tot = sum(c[0] for c in cuenta.values())
    c3 = sum(c[1] for c in cuenta.values())
    c2 = sum(c[2] for c in cuenta.values())
    print(f"   global: {tot} snapshots · eff_N<3 en {c3} ({100*c3/tot:.1f}%) · "
          f"eff_N<2 en {c2} ({100*c2/tot:.1f}%)")
    peor = sorted(cuenta.items(), key=lambda kv: -kv[1][1] / max(1, kv[1][0]))
    print("   por estación (5 con más colapso y 3 con menos):")
    for st, c in peor[:5] + peor[-3:]:
        print(f"      {st}  {100*c[1]/max(1,c[0]):5.1f}% <3   "
              f"{100*c[2]/max(1,c[0]):5.1f}% <2   (n={c[0]})")
    print("   por franja local:")
    for f in ("madrugada-10h", "10-13h", "13-16h", "16h-noche"):
        t, k = por_franja[f]
        if t:
            print(f"      {f:14s} {100*k/t:5.1f}% con eff_N<3   (n={t})")
    print()

    # ---------- Construcción de los pares ----------
    pares = []     # (station, franja, grupo, r, d_raw, d_obs)
    descartes = defaultdict(int)
    for st, rs in por_est.items():
        tz = ZoneInfo(STATION_TZ[st]) if st in STATION_TZ else timezone.utc
        for a, b in zip(rs, rs[1:]):
            ta = datetime.fromisoformat(a["ts"])
            tb = datetime.fromisoformat(b["ts"])
            if not (timedelta(0) < tb - ta <= timedelta(minutes=GAP_MAX_MIN)):
                descartes["gap"] += 1
                continue
            la, lb = ta.astimezone(tz), tb.astimezone(tz)
            if la.date() != lb.date():
                descartes["cambio de día"] += 1
                continue
            d_obs = b["today_max_obs"] - a["today_max_obs"]
            if d_obs < SALTO_MIN_F:
                descartes["sin salto"] += 1
                continue
            # El piso no puede atar en ninguno de los dos extremos
            ata = False
            for r in (a, b):
                piso = r["today_max_obs"]
                if r["current_f"] is not None:
                    piso = max(piso, r["current_f"] - 0.9)
                if r["ens_med"] <= piso + MARGEN_PISO_F:
                    ata = True
            if ata:
                descartes["piso ata"] += 1
                continue
            g = grupo_de(eff_n_de(b["difficulty_reasons_json"]))
            if g is None:
                descartes["sin difficulty"] += 1
                continue
            raw_a = a["ens_med"] + ((a["bias_f"] or 0.0) if a["bias_applied"] else 0.0)
            raw_b = b["ens_med"] + ((b["bias_f"] or 0.0) if b["bias_applied"] else 0.0)
            # Distancia del dato nuevo al borde bajo de la distribución. Cada
            # miembro vale max(max_obs, su pronóstico), así que `ens_p10` NUNCA
            # puede caer por debajo de max_obs: la pregunta útil no es si lo
            # supera —imposible— sino cuánto le falta para tocarlo.
            hueco = (b["ens_p10"] - b["today_max_obs"]
                     if b["ens_p10"] is not None else None)
            pares.append((st, franja(lb.hour + lb.minute / 60), g,
                          (raw_b - raw_a) / d_obs, raw_b - raw_a, d_obs,
                          hueco, abs(raw_b - raw_a) > 1e-9))

    print(f"B. Pares construidos: {len(pares)}")
    print("   descartados: " + " · ".join(f"{k} {v}" for k, v in
                                          sorted(descartes.items())) + "\n")

    def resumen(sel, etiqueta):
        por_g = defaultdict(list)
        for pr in sel:
            por_g[pr[2]].append((pr[3], pr[4]))
        print(f"   {etiqueta}")
        print(f"      {'grupo':10s} {'N':>6s} {'r mediana':>10s} "
              f"{'Δens med':>10s} {'r>0':>7s}")
        for g in ("colapsado", "medio", "alto", "sano"):
            v = por_g.get(g)
            if not v:
                continue
            rs_ = [x[0] for x in v]
            ds = [x[1] for x in v]
            pos = 100 * sum(1 for x in rs_ if x > 0) / len(rs_)
            print(f"      {g:10s} {len(v):6d} {statistics.median(rs_):10.3f} "
                  f"{statistics.median(ds):10.2f} {pos:6.0f}%")
        return por_g

    print("C. Respuesta al dato nuevo  —  r = Δens_med / Δmax_obs\n")
    glob = resumen(pares, "pool completo (sin control horario)")
    print()
    for f in ("10-13h", "13-16h", "16h-noche", "madrugada-10h"):
        sel = [p for p in pares if p[1] == f]
        if len(sel) >= 30:
            resumen(sel, f"franja {f}")
            print()

    # ---------- D. Test de signos por estación ----------
    print("D. Por estación (>=10 pares en colapsado y en sano)")
    a_favor = en_contra = 0
    for st in sorted(por_est):
        col = [p[3] for p in pares if p[0] == st and p[2] == "colapsado"]
        san = [p[3] for p in pares if p[0] == st and p[2] == "sano"]
        if len(col) < 10 or len(san) < 10:
            continue
        mc, ms = statistics.median(col), statistics.median(san)
        marca = "menos" if mc < ms else "MÁS"
        a_favor += mc < ms
        en_contra += mc >= ms
        print(f"   {st}  colapsado {mc:+.3f} (n={len(col):3d})  "
              f"sano {ms:+.3f} (n={len(san):3d})   responde {marca}")
    n_est = a_favor + en_contra
    print(f"   → {a_favor} de {n_est} estaciones responden MENOS colapsadas "
          f"(p={p_signos(a_favor, n_est):.3f})\n")

    # ---------- E. Veredicto automático contra el criterio ----------
    col = [x[0] for x in glob.get("colapsado", [])]
    san = [x[0] for x in glob.get("sano", [])]
    print("E. Veredicto contra el criterio pre-registrado")
    if len(col) < 100 or len(san) < 100:
        print(f"   ⏸ SIN MUESTRA — colapsado {len(col)}, sano {len(san)}, "
              "hacen falta 100 en cada uno")
        return 0
    mc, ms = statistics.median(col), statistics.median(san)
    razon = mc / ms if ms else float("inf")
    print(f"   (1) N: colapsado {len(col)} · sano {len(san)}            ✅")
    print(f"   (2) razón de medianas {mc:.3f}/{ms:.3f} = {razon:.2f}")
    print(f"   (3) signo por estación: {a_favor}/{n_est}, "
          f"p={p_signos(a_favor, n_est):.3f}")
    sostiene = n_est > 0 and a_favor >= (2 * n_est) / 3
    if razon <= 0.5 and sostiene:
        print("   ⇒ ✅ CONFIRMADO: el reweight colapsado deja de aprender")
    elif razon <= 0.8 and sostiene:
        print("   ⇒ 🟡 GRIS: efecto real pero por debajo del listón; se anota "
              "y no se actúa")
    else:
        print("   ⇒ 🔴 REFUTADO por el criterio")
    # ---------- F. Post-hoc: ¿por qué no se mueve? NO decide nada ----------
    print("\nF. Post-hoc (descriptivo, NO parte del criterio): ¿dónde cae el "
          "dato nuevo?")
    print("   Cada miembro vale max(max_obs, pronóstico restante). Un max_obs "
          "por\n   debajo del miembro no puede moverlo, por construcción.\n")
    print(f"      {'p10 − max_obs':22s} {'N':>6s} {'mueve ens_med':>14s} "
          f"{'Δens med mediana':>17s}")
    for etiqueta, lo, hi in (("toca (≤0.1°F)", -99.0, 0.1),
                             ("0.1 – 1°F", 0.1, 1.0),
                             ("1 – 3°F", 1.0, 3.0),
                             ("más de 3°F", 3.0, 999.0)):
        sel = [pr for pr in pares
               if pr[6] is not None and lo < pr[6] <= hi]
        if not sel:
            continue
        mueve = 100 * sum(1 for pr in sel if pr[7]) / len(sel)
        print(f"      {etiqueta:22s} {len(sel):6d} {mueve:13.0f}% "
              f"{statistics.median([pr[4] for pr in sel]):17.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
