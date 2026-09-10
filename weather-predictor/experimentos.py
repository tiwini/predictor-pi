"""Estado de los experimentos abiertos, para la home.

Tres cosas corren en sombra desde el 2026-09-10 y ninguna toca la predicción.
Todas vencen por muestra, no por fecha, y todas tienen su criterio escrito
ANTES de tener datos. El problema de los criterios que vencen por muestra es
que nadie sabe cuándo toca mirar — la nota «revisar Sep-Oct» de
SEASONAL_OFFSET_F estuvo dos meses caducada. Por eso el contador vive donde se
mira todos los días.

  reweight  las dos ramas en sombra (deff puro y sólo-banda) · N≥10 días
  congelado KLAX y KSFO, criterio de reactivación · N≥10 días
  KLAS      entró fallando su criterio · N≥40 días

Cacheado: los contadores se mueven una vez al día y la home se recarga sola.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
TTL_S = 900

_cache: dict = {"ts": 0.0, "datos": None}


def _estado_corrector():
    """(entradas) para el congelado y para KLAS, o [] si no se puede leer."""
    sys.path.insert(0, str(BASE.parent / "investigacion"))
    import seguimiento_corrector as sg
    import level_corrector as lc

    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    an.execute("PRAGMA busy_timeout=10000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)
    try:
        out = []
        congeladas = sorted(lc.FROZEN_STATIONS)
        ns, listos = [], []
        for st in congeladas:
            e = sg.estado_de(an, cal, st)
            ns.append(e.get("n", 0))
            listos.append(e.get("estado") not in ("congelado_n_bajo",
                                                  "sin_datos", "sin_settle"))
        if congeladas:
            out.append({
                "titulo": "congelado " + "+".join(congeladas),
                "n": min(ns) if ns else 0, "objetivo": sg.N_DECISION,
                "listo": all(listos) and bool(listos),
                "detalle": "criterio de reactivación",
            })
        for st, (obj, _motivo) in sg.REVISIONES_PENDIENTES.items():
            e = sg.estado_de(an, cal, st)
            out.append({
                "titulo": f"{st} a revisión",
                "n": e.get("n", 0), "objetivo": obj,
                "listo": bool(e.get("revision_debida")),
                "detalle": "entró fallando su criterio",
            })
        return out
    finally:
        an.close()
        cal.close()


def _estado_reweight():
    sys.path.insert(0, str(BASE.parent / "investigacion"))
    import reweight_veredicto as rw
    n = rw.dias_con_sombra()
    return {"titulo": "reweight (2 ramas)", "n": n, "objetivo": rw.MIN_DIAS,
            "listo": n >= rw.MIN_DIAS, "detalle": "banda contra nivel"}


def resumen(force: bool = False) -> list:
    """Lista de experimentos abiertos. Nunca lanza: la home no se cae por esto."""
    ahora = time.time()
    if (not force and _cache["datos"] is not None
            and ahora - _cache["ts"] < TTL_S):
        return _cache["datos"]
    datos = []
    try:
        datos.append(_estado_reweight())
    except Exception:
        pass
    try:
        datos.extend(_estado_corrector())
    except Exception:
        pass
    _cache["ts"] = ahora
    _cache["datos"] = datos
    return datos
