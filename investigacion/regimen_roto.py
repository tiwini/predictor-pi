#!/usr/bin/env python3
"""Detectar el régimen roto y recortar la corrección SÓLO ahí.

Tercer intento contra el giro de septiembre. Los dos anteriores
([[giro_regimen_septiembre]]) fracasaron con la misma forma: el remedio ayuda en
la semana del giro y se diluye al promediarlo sobre 24 días. La conclusión de
aquello no era «el mecanismo no sirve» sino «este criterio no puede aprobar una
cura para algo raro». Esto lo mide como toca.

DOS TRAMPAS QUE ESTE DISEÑO EVITA A PROPÓSITO
---------------------------------------------
1. **Circularidad.** Si los periodos de régimen roto se marcaran mirando dónde
   el corrector falla, se estarían seleccionando justo los días en que falla y
   cualquier remedio saldría bien. Aquí el detector es **causal y ciego al
   resultado**: sólo mira sesgos de días ANTERIORES, nunca el settle del día.

2. **Muestra ya vista.** El bloque de «últimos 7 días» de la corrida anterior ya
   se miró en las 6 estaciones habilitadas, así que ésas están contaminadas: no
   pueden decidir. **Deciden las 14 que NO llevan corrector**, cuyo
   comportamiento en septiembre no ha mirado nadie. Las 6 se reportan como
   contexto, separadas, y no cuentan para el veredicto.

EL MECANISMO
    detector   |med_corto(k) − med_largo| ≥ τ   (τ = 1.5°F, k ∈ {5,7})
    acción     si dispara → bias = med_largo · clip(med_corto/med_largo, 0, 1)
               si no      → bias = med_largo    (lo de hoy, intacto)

=========================== CRITERIO PRE-REGISTRADO ==========================
Escrito y commiteado el 2026-09-04 ANTES de correr nada.

  ADOPTAR si las CUATRO, medidas SOBRE LAS 14 NO HABILITADAS:
    (a) en los días en que el detector dispara, el |err| medio mejora ≥0.30°F,
    (b) en los días en que NO dispara, el |err| cambia ≤0.05°F — el mecanismo
        tiene que ser inerte cuando no le toca (debería salir por construcción;
        si no sale, hay un error de implementación),
    (c) se sostiene con k=5 y con k=7,
    (d) el detector dispara en <25% de los días. Si dispara la mitad del tiempo
        no está detectando un suceso: está describiendo el estado normal, y
        entonces esto es otra cosa y hay que medirla como otra cosa.

  ⚠ Aunque pase, ADOPTAR aquí significa **implementarlo y vigilarlo**, no darlo
  por bueno: la muestra contiene UN solo cambio de régimen. La confirmación
  exige el segundo, con su propia fila. Queda escrito para que nadie lo lea
  después como validado.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/regimen_roto.py
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stations import PEAK_HOURS, STATION_TZ      # noqa: E402
import backtest_corrector_knyc as bk             # noqa: E402
import level_corrector as lc                     # noqa: E402

KS = [5, 7]
TAU = 1.5
MIN_PREV = lc.MIN_PREV_DAYS
MEJORA_MIN = 0.30
INERCIA_MAX = 0.05
DISPARO_MAX = 0.25


def evaluar(filas: list[dict], k: int) -> tuple[list, list]:
    """(días con detector disparado, días sin). Cada uno: (err_actual, err_nuevo)."""
    dentro, fuera, sesgos = [], [], []
    for f in filas:
        if len(sesgos) >= max(MIN_PREV, k):
            largo = statistics.median(sesgos)
            corto = statistics.median(sesgos[-k:])
            e_act = abs((f["crudo"] - largo) - f["settle"])
            if abs(corto - largo) >= TAU:
                fac = 1.0 if abs(largo) < 1e-9 else max(0.0, min(1.0, corto / largo))
                e_new = abs((f["crudo"] - largo * fac) - f["settle"])
                dentro.append((e_act, e_new))
            else:
                fuera.append((e_act, e_act))
        sesgos.append(f["crudo"] - f["settle"])
    return dentro, fuera


def bloque(nombre, ests, datos, k):
    d_tot, f_tot = [], []
    for st in ests:
        d, f = evaluar(datos[st], k)
        d_tot += d
        f_tot += f
    n = len(d_tot) + len(f_tot)
    if n == 0:
        print(f"  {nombre}: sin datos")
        return None
    disparo = len(d_tot) / n
    m_act = statistics.mean([a for a, _ in d_tot]) if d_tot else float("nan")
    m_new = statistics.mean([b for _, b in d_tot]) if d_tot else float("nan")
    inercia = (statistics.mean([b - a for a, b in f_tot]) if f_tot else 0.0)
    print(f"  {nombre:22s} dispara {100*disparo:4.1f}% ({len(d_tot)}/{n})   "
          f"|err| en disparo {m_act:.2f} → {m_new:.2f} ({m_act - m_new:+.2f})   "
          f"inercia fuera {inercia:+.3f}")
    return {"disparo": disparo, "mejora": m_act - m_new, "inercia": abs(inercia),
            "n_dentro": len(d_tot)}


def main() -> int:
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    todas = sorted(STATION_TZ)
    ciegas = [s for s in todas if s not in lc.ENABLED_STATIONS]
    vistas = sorted(lc.ENABLED_STATIONS)

    datos = {}
    for st in todas:
        bk.ST = st
        filas = bk.recoger(an, cal, PEAK_HOURS[st][0] - 2)
        if len(filas) >= MIN_PREV + 8:
            datos[st] = filas
    ciegas = [s for s in ciegas if s in datos]
    vistas = [s for s in vistas if s in datos]

    print(f"Régimen roto — detector causal (τ={TAU}°F)\n")
    print(f"  deciden las {len(ciegas)} ciegas: {' '.join(ciegas)}")
    print(f"  contexto, NO decide: {' '.join(vistas)}\n")

    res = {}
    for k in KS:
        print(f"  ── k={k} ──")
        res[k] = bloque("14 CIEGAS (deciden)", ciegas, datos, k)
        bloque("6 vistas (contexto)", vistas, datos, k)
        print()

    print("  ── CRITERIO (sólo con las ciegas) ──")
    ok_todo = True
    for k in KS:
        r = res[k]
        a = r["mejora"] >= MEJORA_MIN
        b = r["inercia"] <= INERCIA_MAX
        d = r["disparo"] < DISPARO_MAX
        ok_todo &= a and b and d
        print(f"  k={k}: (a) mejora {r['mejora']:+.2f} {'✓' if a else '✗'}   "
              f"(b) inercia {r['inercia']:.3f} {'✓' if b else '✗'}   "
              f"(d) dispara {100*r['disparo']:.1f}% {'✓' if d else '✗'}")
    print(f"  (c) se sostiene con los dos k: {'SÍ' if ok_todo else 'NO'}")
    print("\n  ⇒ " + ("✅ ADOPTAR — implementar y VIGILAR; la confirmación exige "
                      "un segundo cambio de régimen"
                      if ok_todo else
                      "🔴 NO ADOPTAR"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
