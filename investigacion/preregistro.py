#!/usr/bin/env python3
"""Pre-registro de una llamada: congela la llamada del sistema a una hora local
fija y la puntua contra el CLI de NWS. Read-only sobre las DBs.

    preregistro.py KPHL --hora 15                 # captura (o reconstruye) la llamada
    preregistro.py KPHL --hora 15 --cerrar        # puntua contra el settle

La captura NO tiene que correrse a la hora en punto: el snapshot de esa hora
queda en analysis.db y la regla de seleccion es fija (el ultimo snapshot con
ts <= hora objetivo y dentro de la hora previa), asi que reconstruirla despues
da exactamente lo mismo. Lo que tiene que estar escrito ANTES es el criterio,
que vive en el .md que acompana a este fichero.

Criterio, en una linea: acierta quien tenga el settle dentro de su bin favorito.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
DEST = Path("/home/popeye/predictor-pi/investigacion/registros")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ  # noqa: E402

try:                                  # el margen del piso de 5-min, sin duplicarlo
    from predictor import CURRENT_FLOOR_MARGIN_F
except Exception:                     # si el import cambia, se ve en la salida
    CURRENT_FLOOR_MARGIN_F = None

UTC = ZoneInfo("UTC")
FUENTES_OK = ("cli", "cf6")          # NWS. Nunca Open-Meteo, nunca MAX(today_max_obs)
EDAD_MAX_MIN = 10                    # snapshot mas viejo que esto -> prueba nula
BANDA_MIN_F = 0.05                   # banda mas estrecha que esto = colapsada
PISO_TOL_F = 0.06                    # pred a menos de esto del piso = la dice el piso
HOLGURA_AVISO_F = 0.30               # aviso, NO invalida: el borde de bin esta cerca
OBS_MAX_MIN = 90                     # sin observacion nueva en esto -> estacion callada
# ⚠ La edad se mide sobre current_obs_ts (ultima observacion), NUNCA sobre
# today_max_obs_ts: ese marca CUANDO se puso el maximo del dia, que en un dia
# con el maximo de madrugada tiene 9 horas sin que nada este roto.


def conectar(nombre: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{BASE / nombre}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def bin_de(bins, valor):
    return next((b for b in bins if b["bin_lo"] <= round(valor) <= b["bin_hi"]), None)


def etiqueta(b):
    return (b["label"] or f'{b["bin_lo"]}-{b["bin_hi"]}') if b else "(ninguno)"


def capturar(station: str, dia: str, hora: int) -> dict:
    tz = ZoneInfo(STATION_TZ[station])
    objetivo = datetime.strptime(dia, "%Y-%m-%d").replace(hour=hora, tzinfo=tz)
    if objetivo > datetime.now(tz):
        sys.exit(f"todavia no son las {hora}h en {station} (son las "
                 f"{datetime.now(tz).strftime('%H:%M')}). Nada que capturar.")
    lim = objetivo.astimezone(UTC)

    an = conectar("analysis.db")
    snap = an.execute(
        "SELECT * FROM station_snapshots WHERE station=? AND ts<=? AND ts>=? "
        "ORDER BY ts DESC LIMIT 1",
        (station, lim.isoformat(), (lim - timedelta(hours=1)).isoformat())).fetchone()
    if snap is None:
        sys.exit(f"NULA: sin snapshot de {station} en la hora previa a las {hora}h local")

    ts_snap = datetime.fromisoformat(snap["ts"])
    edad = (lim - ts_snap).total_seconds() / 60

    ciclo = an.execute(
        "SELECT MAX(ts) FROM kalshi_snapshots WHERE station=? AND ts<=? AND ts>=?",
        (station, lim.isoformat(), (lim - timedelta(hours=6)).isoformat())).fetchone()[0]
    bins = []
    if ciclo:
        bins = [b for b in an.execute(
            "SELECT * FROM kalshi_snapshots WHERE station=? AND ts=? ORDER BY bin_lo",
            (station, ciclo)).fetchall() if b["yes_mid"] is not None]

    # El piso que realmente manda: el METAR horario, o el pico del feed de 5 min
    # menos medio escalon de C. Si la prediccion es eso, no la dice el modelo.
    cands = [snap["today_max_obs"]]
    if snap["today_max_5min"] is not None and CURRENT_FLOOR_MARGIN_F is not None:
        cands.append(snap["today_max_5min"] - CURRENT_FLOOR_MARGIN_F)
    piso = max([c for c in cands if c is not None], default=None)
    banda = (snap["ens_p90"] - snap["ens_p10"]
             if snap["ens_p90"] is not None and snap["ens_p10"] is not None else None)

    nuestro = max((b for b in bins if b["our_p_calibrated"] is not None),
                  key=lambda b: b["our_p_calibrated"], default=None)
    mercado = max(bins, key=lambda b: b["yes_mid"], default=None)

    # Invalidaciones, evaluadas con los datos del momento capturado.
    nulo = []
    if edad > EDAD_MAX_MIN:
        nulo.append(f"snapshot {edad:.0f} min viejo (max {EDAD_MAX_MIN})")
    if snap["today_max_cli"] is not None:
        nulo.append(f"CLI parcial ya presente ({snap['today_max_cli']}): mediria el piso, no el modelo")
    obs_edad = None
    if snap["current_obs_ts"]:
        try:
            obs_edad = (lim - datetime.fromisoformat(snap["current_obs_ts"])).total_seconds() / 60
            if obs_edad > OBS_MAX_MIN:
                nulo.append(f"sin observacion nueva desde hace {obs_edad:.0f} min: estacion callada")
        except ValueError:
            obs_edad = None
    if not bins:
        nulo.append("sin bins de Kalshi en el ciclo")
    if piso is not None and abs(snap["our_pred_f"] - piso) < PISO_TOL_F:
        nulo.append(f"la prediccion ES el piso ({piso:.1f}): la dice el clamp, no el modelo")
    if banda is not None and banda < BANDA_MIN_F:
        nulo.append(f"banda p10-p90 colapsada ({banda:.2f}F): no hay distribucion que puntuar")
    if CURRENT_FLOOR_MARGIN_F is None:
        nulo.append("no pude leer CURRENT_FLOOR_MARGIN_F: el piso no se ha comprobado")

    holgura = None
    if nuestro is not None:
        holgura = min(snap["our_pred_f"] - (nuestro["bin_lo"] - 0.5),
                      (nuestro["bin_hi"] + 0.5) - snap["our_pred_f"])

    d = {
        "station": station, "dia": dia, "hora_local": hora,
        "tz": STATION_TZ[station],
        "snapshot_ts": snap["ts"], "edad_snapshot_min": round(edad, 1),
        "our_pred_f": snap["our_pred_f"], "ens_med": snap["ens_med"],
        "pred_iso_med_f": snap["pred_iso_med_f"],
        "p10": snap["ens_p10"], "p90": snap["ens_p90"],
        "ext_med_f": snap["ext_med_f"], "ext_diff_f": snap["ext_diff_f"],
        "today_max_obs": snap["today_max_obs"], "today_max_obs_ts": snap["today_max_obs_ts"],
        "current_f": snap["current_f"], "current_obs_ts": snap["current_obs_ts"],
        "edad_obs_min": round(obs_edad, 1) if obs_edad is not None else None,
        "today_max_cli": snap["today_max_cli"],
        "bias_aplicado_f": snap["bias_median_causal_f"],
        "kalshi_ts": ciclo,
        "nuestro_bin": etiqueta(nuestro),
        "nuestro_bin_lo": nuestro["bin_lo"] if nuestro else None,
        "nuestro_bin_hi": nuestro["bin_hi"] if nuestro else None,
        "nuestro_p": round(nuestro["our_p_calibrated"], 3) if nuestro else None,
        "mercado_bin": etiqueta(mercado),
        "mercado_bin_lo": mercado["bin_lo"] if mercado else None,
        "mercado_bin_hi": mercado["bin_hi"] if mercado else None,
        "mercado_p": round(mercado["yes_mid"], 3) if mercado else None,
        "bins": [{"label": b["label"], "lo": b["bin_lo"], "hi": b["bin_hi"],
                  "mercado": b["yes_mid"], "our_cal": b["our_p_calibrated"]} for b in bins],
        "piso_f": piso, "banda_f": banda, "holgura_borde_f": holgura,
        "floor_margin_f": CURRENT_FLOOR_MARGIN_F,
        "aviso_holgura": holgura is not None and holgura < HOLGURA_AVISO_F,
        "nulo": nulo,
        "capturado_en": datetime.now(UTC).isoformat(),
    }
    DEST.mkdir(exist_ok=True)
    f = DEST / f"preregistro_{station}_{dia}_{hora}h.json"
    f.write_text(json.dumps(d, indent=2))
    return d


def f1(v, suf="°F"):
    return "—" if v is None else f"{v:.1f}{suf}"


def imprimir(d: dict) -> None:
    print(f"=== {d['station']} · {d['dia']} {d['hora_local']}h local — llamada congelada ===")
    print(f"  snapshot        {d['snapshot_ts']}  ({d['edad_snapshot_min']:.0f} min antes de la hora)")
    print(f"  our_pred_f      {f1(d['our_pred_f']):9} banda {f1(d['p10'],'')} .. {f1(d['p90'],'')}")
    print(f"  externos        {f1(d['ext_med_f']):9} ext_diff {f1(d['ext_diff_f'],'')}")
    print(f"  max_obs         {f1(d['today_max_obs']):9} puesto a las {(d['today_max_obs_ts'] or '?')[11:16]}Z")
    print(f"  current         {f1(d.get('current_f')):9} obs de hace {d.get('edad_obs_min')} min")
    print(f"  CLI parcial     {f1(d['today_max_cli'])}")
    print(f"  piso que manda  {f1(d.get('piso_f')):9} (max_obs, o max_5min - {d.get('floor_margin_f')})")
    print(f"  banda           {f1(d.get('banda_f')):9} holgura al borde {f1(d.get('holgura_borde_f'))}")
    print(f"  NUESTRO BIN     {d['nuestro_bin']:14} p={d['nuestro_p']}")
    print(f"  BIN DEL MERCADO {d['mercado_bin']:14} p={d['mercado_p']}")
    if d["nulo"]:
        print("  🔴 NULA: " + " · ".join(d["nulo"]))
    else:
        print("  ✅ valida: ninguna condicion de invalidacion disparo")
        if d.get("aviso_holgura"):
            print(f"  ⚠ aviso: la prediccion esta a {d['holgura_borde_f']:.1f}°F del borde del bin — "
                  "un acierto ahi puede ser redondeo. No invalida; leelo con esto delante.")


def cerrar(station: str, dia: str, hora: int) -> None:
    f = DEST / f"preregistro_{station}_{dia}_{hora}h.json"
    if not f.exists():
        sys.exit(f"no hay captura en {f} — corre primero sin --cerrar")
    d = json.loads(f.read_text())
    cal = conectar("calibration.db")
    r = cal.execute("SELECT max_obs_f, source, settled_at FROM day_outcomes "
                    "WHERE station_id=? AND date=?", (station, dia)).fetchone()
    if r is None or r["max_obs_f"] is None:
        print(f"PENDIENTE: {station} {dia} todavia no tiene settle. "
              f"El CLI de KPHL suele entrar ~07:00 AST del dia siguiente.")
        return
    if r["source"] not in FUENTES_OK:
        print(f"PENDIENTE: settle presente pero source={r['source']!r}, que no es NWS. "
              "No se puntua con proxy.")
        return
    settle = r["max_obs_f"]
    bins = d["bins"]
    def contiene(lo, hi):
        return lo is not None and lo <= round(settle) <= hi
    ok_nuestro = contiene(d["nuestro_bin_lo"], d["nuestro_bin_hi"])
    ok_mercado = contiene(d["mercado_bin_lo"], d["mercado_bin_hi"])
    err = d["our_pred_f"] - settle
    dentro = (d["p10"] is not None and d["p10"] <= settle <= d["p90"])
    ganador = next((b for b in bins if b["lo"] <= round(settle) <= b["hi"]), None)

    imprimir(d)
    print(f"\n=== SETTLE {settle}°F  (source={r['source']}, {r['settled_at']}) ===")
    print(f"  bin ganador     {(ganador['label'] if ganador else '(fuera de la escalera)'):14}"
          f" mercado {ganador['mercado']:.2f}  our_cal {ganador['our_cal']:.2f}" if ganador else
          "  bin ganador     (fuera de la escalera)")
    print(f"  NOSOTROS        {d['nuestro_bin']:14} {'✅ ACIERTA' if ok_nuestro else '🔴 FALLA'}")
    print(f"  MERCADO         {d['mercado_bin']:14} {'✅ ACIERTA' if ok_mercado else '🔴 FALLA'}")
    print(f"  |err| en °F     {abs(err):.1f}   (bias {err:+.1f})")
    print(f"  banda p10-p90   {'✅ contiene el settle' if dentro else '🔴 el settle cae fuera'}")
    if d["nulo"]:
        print("  ⚠ la captura estaba marcada NULA: " + " · ".join(d["nulo"]) +
              " — el resultado no puntua.")
    d.update(settle=settle, settle_source=r["source"], acierta_nuestro=ok_nuestro,
             acierta_mercado=ok_mercado, err_f=round(err, 2), banda_contiene=dentro,
             cerrado_en=datetime.now(UTC).isoformat())
    f.write_text(json.dumps(d, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("station")
    p.add_argument("--dia", default=datetime.now(ZoneInfo("America/Puerto_Rico")).strftime("%Y-%m-%d"))
    p.add_argument("--hora", type=int, required=True, help="hora LOCAL de la estacion")
    p.add_argument("--cerrar", action="store_true")
    a = p.parse_args()
    if a.station not in STATION_TZ:
        sys.exit(f"{a.station} no esta en el roster")
    if a.cerrar:
        cerrar(a.station, a.dia, a.hora)
    else:
        imprimir(capturar(a.station, a.dia, a.hora))
