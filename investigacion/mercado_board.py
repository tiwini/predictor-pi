#!/usr/bin/env python3
"""Foto del mercado ahora: dónde coincidimos con Kalshi y dónde no.

Por estación: nuestro bin favorito vs el del mercado, la divergencia, y si el
bin del mercado contiene la mediana externa (o sea si el mercado está anclado a
los modelos externos o a otra cosa). Read-only.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
import agent_signals as A                      # noqa: E402
from stations import STATION_TZ, PEAK_HOURS    # noqa: E402

an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
an.row_factory = sqlite3.Row


def contains(b, v) -> bool:
    return v is not None and b["bin_lo"] <= v <= b["bin_hi"]


rows = an.execute("""SELECT * FROM station_snapshots s
                     WHERE ts = (SELECT MAX(ts) FROM station_snapshots
                                 WHERE station = s.station)""").fetchall()
out = []
for r in rows:
    st = r["station"]
    if st not in STATION_TZ:
        continue
    bins = an.execute(
        """SELECT * FROM kalshi_snapshots WHERE station=? AND ts=(
               SELECT MAX(ts) FROM kalshi_snapshots WHERE station=?)
           ORDER BY bin_lo""", (st, st)).fetchall()
    bins = [b for b in bins if b["yes_mid"] is not None]
    if not bins:
        continue
    mk = max(bins, key=lambda b: b["yes_mid"])
    ours = max((b for b in bins if b["our_p_calibrated"] is not None),
               key=lambda b: b["our_p_calibrated"], default=None)
    if ours is None:
        continue
    local = datetime.now(ZoneInfo(STATION_TZ[st]))
    lo_h, hi_h = PEAK_HOURS[st]
    # mayor edge accionable (excluye colas <=2c, donde el edge es piso del calibrador)
    best_edge, best_lab, best_side = 0.0, "", ""
    for b in bins:
        if b["yes_mid"] <= 0.02:
            continue
        ev = A.evaluate_bin(
            station_id=st, bin_lo=b["bin_lo"], bin_hi=b["bin_hi"],
            bin_label=b["label"] or "", kalshi_yes_price=b["yes_mid"],
            model_p_calibrated=b["our_p_calibrated"], model_p_raw=b["our_p"],
            our_pred_f=r["our_pred_f"], ext_diff_f=r["ext_diff_f"],
            difficulty_score=r["difficulty_score"],
            streak_hot_n=r["streak_block_hot"] or 0,
            streak_cold_n=r["streak_block_cold"] or 0,
            cold_bias_block=bool(r["cold_bias_block"]))
        if ev["edge_pp"] and ev["edge_pp"] > best_edge:
            best_edge = ev["edge_pp"]
            best_lab = (b["label"] or "")[:11]
            best_side = ev["recommended_side"] or ""
            best_ok = ev["actionable"]
    out.append({
        "st": st, "local": local, "peak_open": lo_h <= local.hour < hi_h,
        "pred": r["our_pred_f"], "ext": r["ext_med_f"], "extd": r["ext_diff_f"],
        "mk": mk, "ours": ours, "same": mk["label"] == ours["label"],
        "mk_has_ext": contains(mk, r["ext_med_f"]),
        "edge": best_edge, "lab": best_lab, "side": best_side,
    })

out.sort(key=lambda x: -(x["local"].hour + x["local"].minute / 60))
print(f"{'st':6s} {'loc':>5s} {'pico':>5s} {'pred':>6s} {'ext':>6s} "
      f"{'bin del mercado':18s} {'nuestro bin':18s} {'mayor edge':>16s}")
for o in out:
    mk_s = f"{(o['mk']['label'] or '')[:11]:11s} {o['mk']['yes_mid']:.2f}"
    ou_s = f"{(o['ours']['label'] or '')[:11]:11s} {o['ours']['our_p_calibrated']:.2f}"
    mark = "=" if o["same"] else "≠"
    ed = f"{o['edge']:4.1f}pp {o['side']:3s} {o['lab'][:8]}" if o["edge"] else "        —"
    print(f"{o['st']:6s} {o['local']:%H:%M} {'ABRE' if o['peak_open'] else '  · ':>5s} "
          f"{o['pred'] or 0:6.1f} {o['ext'] or 0:6.1f} {mk_s:18s} {mark} {ou_s:18s} {ed}")

n_same = sum(1 for o in out if o["same"])
n_ext = sum(1 for o in out if o["mk_has_ext"])
n_pos = sum(1 for o in out if (o["extd"] or 0) > 0)
print(f"\ncoincidimos con el mercado en {n_same}/{len(out)} estaciones")
print(f"el bin favorito del mercado contiene la mediana EXTERNA en {n_ext}/{len(out)}")
print(f"ext_diff positivo (predecimos más caliente) en {n_pos}/{len(out)}")
