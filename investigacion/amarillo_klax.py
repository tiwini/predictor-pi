#!/usr/bin/env python3
"""Diagnóstico del 🟡 de KLAX: ¿hay que recortar la corrección, o es ruido?

El criterio de vigilancia (`seguimiento_corrector.py`, escrito el 2026-08-17 con
N=1) dice que ante un amarillo «la salida probablemente sea recortar la mediana,
no apagarla». Esto mide si eso es cierto, y con qué parámetro.

=========================== CRITERIO PRE-REGISTRADO ==========================
Escrito el 2026-08-28 ANTES de correr nada. Se conocen los signos del informe
diario (8 de 10 negativos en KLAX) pero NINGÚN contrafactual.

Se cambia el corrector sólo si un MISMO parámetro —factor de recorte k, o
ventana W de la mediana— cumple las tres:

  (1) mejora el |err| medio de KLAX en >= 0.20°F sobre sus N días activos,
  (2) no empeora KSFO ni KNYC en > 0.10°F cada una (comparten mecanismo: es un
      solo corrector, no tres),
  (3) el efecto se sostiene en la vecindad del óptimo (k +-0.1, W +-5 días).
      Un óptimo de pico estrecho se declara SOBREAJUSTE y no se toca.

Si nada lo cumple: el amarillo se declara ruido de N pequeño, NO se cambia
nada, y se sigue vigilando con el watchdog diario.

Nota de validez: el recorte es seguro por construcción respecto al piso. La
corrección se RESTA, así que un k menor sube la predicción y la aleja del piso;
`cap_by_floor` no puede reactivarse por recortar.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/amarillo_klax.py [hora_local]
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from math import comb
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stations import STATION_TZ                      # noqa: E402
import level_corrector as lc                         # noqa: E402
import seguimiento_corrector as sg                   # noqa: E402

UTC = ZoneInfo("UTC")
HORA = int(sys.argv[1]) if len(sys.argv) > 1 else 12
ESTACIONES = sorted(lc.ENABLED_STATIONS)


def p_signos(neg: int, n: int) -> float:
    """p de dos colas del test de signos con p0=0.5."""
    k = max(neg, n - neg)
    cola = sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n
    return min(1.0, 2 * cola)


def sesgo_crudo_por_dia(an, cal, st: str) -> dict[str, float]:
    """{día: sesgo del ensemble CRUDO a las HORA local}, toda la historia.

    Réplica de `level_corrector.median_level_bias` (misma ventana, mismo
    `ORDER BY ts LIMIT 1`, mismo deshacer del bias aplicado). Si esto no
    reprodujera la corrección observada, el barrido de ventana estaría midiendo
    otra cantidad distinta de la desplegada.
    """
    tz = ZoneInfo(STATION_TZ[st])
    out: dict[str, float] = {}
    for dia, settle in cal.execute(
            "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
            "AND max_obs_f IS NOT NULL ORDER BY date", (st,)).fetchall():
        try:
            d = datetime.strptime(dia, "%Y-%m-%d").date()
        except ValueError:
            continue
        ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=HORA)
        lo = (ref - timedelta(minutes=30)).astimezone(timezone.utc)
        hi = (ref + timedelta(minutes=30)).astimezone(timezone.utc)
        r = an.execute(
            """SELECT ens_med, bias_f, bias_applied FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
               ORDER BY ts LIMIT 1""",
            (st, lo.strftime("%Y-%m-%dT%H:%M:%S"),
             hi.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
        if r is None:
            continue
        b = r[1] if (r[2] and r[1] is not None) else 0.0
        out[dia] = (r[0] + b) - settle
    return out


def mediana_causal(sesgos: dict[str, float], dia: str,
                   ventana: int | None) -> float | None:
    """Mediana de los sesgos de días ANTERIORES a `dia`; `ventana` = últimos W
    días con dato (None = toda la historia, que es lo desplegado hoy)."""
    prev = [sesgos[d] for d in sorted(sesgos) if d < dia]
    if ventana is not None:
        prev = prev[-ventana:]
    if len(prev) < lc.MIN_PREV_DAYS:
        return None
    prev = sorted(prev)
    return prev[len(prev) // 2]          # mediana superior, como el corrector


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    est = {st: sg.estado_de(an, cal, st, hora=HORA) for st in ESTACIONES}
    print(f"Diagnóstico del amarillo — hora de decisión {HORA}h local\n")

    # ---------- A. Descriptivo: ¿la corrección excede al sesgo real? ----------
    print("A. ¿Cuánto se pasa la corrección?")
    print(f"   {'est':6s} {'N':>3s} {'sesgo crudo':>12s} {'corrección':>11s} "
          f"{'exceso':>8s} {'Δpub medio':>11s} {'neg/10':>7s} {'p signos':>9s}")
    for st in ESTACIONES:
        e = est[st]
        if e["estado"] in ("sin_datos", "sin_settle"):
            continue
        f = e["filas"]
        crudo = statistics.mean(x["sin"] - x["settle"] for x in f)
        corr = statistics.mean(x["corr"] for x in f)
        dpub = statistics.mean(x["pub"] - x["settle"] for x in f)
        n10 = min(10, len(f))
        neg = sum(1 for x in f[-n10:] if x["pub"] - x["settle"] < 0)
        print(f"   {st:6s} {len(f):3d} {crudo:+12.2f} {corr:+11.2f} "
              f"{corr - crudo:+8.2f} {dpub:+11.2f} {neg:4d}/{n10:<2d} "
              f"{p_signos(neg, n10):9.3f}")
    print("   exceso>0 = corrige de más · Δpub<0 = sub-predice\n")

    # ---------- B. ¿Se está enfriando el sesgo crudo? ----------
    print("B. Sesgo crudo por semana (¿la mediana de TODA la historia va tarde?)")
    sesgos = {st: sesgo_crudo_por_dia(an, cal, st) for st in ESTACIONES}
    for st in ESTACIONES:
        s = sesgos[st]
        if not s:
            continue
        sem: dict[str, list[float]] = {}
        for d, v in sorted(s.items()):
            wk = datetime.strptime(d, "%Y-%m-%d").strftime("%Y-W%V")
            sem.setdefault(wk, []).append(v)
        linea = "  ".join(f"{w[-3:]}:{statistics.median(v):+5.2f}(n{len(v)})"
                          for w, v in sorted(sem.items()))
        med_all = statistics.median(list(s.values()))
        ult15 = [s[d] for d in sorted(s)[-15:]]
        print(f"   {st}: {linea}")
        print(f"        mediana toda la historia {med_all:+.2f}  ·  "
              f"últimos 15 días {statistics.median(ult15):+.2f}  ·  n={len(s)}")
    print()

    # ---------- C. Fidelidad de la réplica ----------
    print("C. ¿Reproduzco la corrección desplegada? (si no, lo demás no vale)")
    for st in ESTACIONES:
        e = est[st]
        if e["estado"] in ("sin_datos", "sin_settle"):
            continue
        difs = []
        for x in e["filas"]:
            m = mediana_causal(sesgos[st], x["dia"], None)
            if m is not None:
                difs.append(m - x["corr"])
        if difs:
            print(f"   {st}: |dif| media {statistics.mean(abs(d) for d in difs):.2f}°F"
                  f"  ·  máx {max(abs(d) for d in difs):.2f}°F  ·  n={len(difs)}")
    print()

    # ---------- D. Barrido del recorte k ----------
    print("D. Recorte k  —  pred_k = sin − k·corr   (k=1 es lo desplegado)")
    ks = [round(0.1 * i, 1) for i in range(0, 13)]
    print(f"   {'k':>5s} " + " ".join(f"{st:>7s}" for st in ESTACIONES))
    for k in ks:
        fila = []
        for st in ESTACIONES:
            f = est[st].get("filas") or []
            if not f:
                fila.append("      —")
                continue
            m = statistics.mean(abs(x["sin"] - x["settle"] - k * x["corr"])
                                for x in f)
            fila.append(f"{m:7.2f}")
        print(f"   {k:5.1f} " + " ".join(fila))
    print("   (|err| medio en °F; menos es mejor)\n")

    # ---------- E. Barrido de la ventana W ----------
    print("E. Ventana W de la mediana  —  W=todo es lo desplegado")
    ventanas = [7, 10, 15, 20, 30, 45, None]
    print(f"   {'W':>5s} " + " ".join(f"{st:>7s}" for st in ESTACIONES))
    for w in ventanas:
        fila = []
        for st in ESTACIONES:
            f = est[st].get("filas") or []
            errs = []
            for x in f:
                m = mediana_causal(sesgos[st], x["dia"], w)
                if m is None:
                    continue
                errs.append(abs(x["sin"] - x["settle"] - m))
            fila.append(f"{statistics.mean(errs):7.2f}" if errs else "      —")
        etiqueta = "todo" if w is None else str(w)
        print(f"   {etiqueta:>5s} " + " ".join(fila))
    print("   (|err| medio en °F sobre los mismos días activos)\n")

    print("Recordatorio del criterio: KLAX ≥0.20 mejor, KSFO y KNYC no peores "
          "de 0.10, y sostenido en la vecindad. Si no, no se toca.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
