#!/usr/bin/env python3
"""audit_ext_thresholds.py — valida/recalibra los umbrales del anclaje externo.

Uso:
    python3 audit_ext_thresholds.py --signal daily_ext_signal.csv \
        --outcomes day_outcomes_last60.csv [--bets simulated_bets.csv]

Espera en --signal una fila por (station_id, date) matinal con columnas:
    station_id, date, pred_med_pre, ext_med, ext_spread, ext_diff_pre,
    clim_pct, lam, shift_f
(ext_diff_pre = pred_med_pre - ext_med; si tu log usa otros nombres,
ajusta COLMAP abajo.)

Produce, en orden de importancia:
  1. beta — regresión (real - pred) ~ (ext_med - pred): la pendiente ES el
     lambda óptimo empírico. Global, por estación, y por bucket de |ext_diff|
     con CI bootstrap. Valida cap=0.5 y los umbrales 1.5/1.0.
  2. MAE comparado pred vs ext vs pred+shift, por estación y bucket de clim.
     Valida el corte de percentil (p80/p85).
  3. Guardia de spread: MAE(ext) por cuartil de ext_spread. Valida 5.4°F.
  4. Gate (si pasas --bets): EV de bets direccionales por bucket de
     ext_diff_pre x dirección. El umbral del gate es donde el EV de apostar
     contra externos cruza cero.
  5. Persistencia de signo del error por estación (CI binomial) — para el
     sign-nudge y los k_s per-station del tracker.

Regla de potencia: celdas con n<25 se imprimen con '!' y no deben usarse
para recalibrar; usa pooled + offset por estación hasta tener n.
"""
import argparse
import sys

import numpy as np
import pandas as pd

COLMAP = {  # nombre esperado -> nombre en tu log (edita si difiere)
    "station_id": "station_id", "date": "date", "pred": "pred_med_pre",
    "ext_med": "ext_med", "ext_spread": "ext_spread",
    "ext_diff_pre": "ext_diff_pre", "clim_pct": "clim_pct",
    "lam": "lam", "shift_f": "shift_f",
}

EXT_DIFF_BUCKETS = [0.0, 0.5, 1.0, 1.5, 2.5, np.inf]
CLIM_BUCKETS = [0, 50, 80, 90, 100.01]
MIN_CELL_N = 25
N_BOOT = 2000


def load(args):
    sig = pd.read_csv(args.signal)
    sig = sig.rename(columns={v: k for k, v in COLMAP.items()})
    out = pd.read_csv(args.outcomes)
    om = out.set_index(["station_id", "date"]).max_obs_f.to_dict()
    sig["actual"] = [om.get((r.station_id, r.date)) for r in sig.itertuples()]
    sig = sig.dropna(subset=["actual", "pred", "ext_med"]).copy()
    sig["gap"] = sig.ext_med - sig.pred          # hacia dónde apuntan externos
    sig["resid"] = sig.actual - sig.pred         # hacia dónde estaba la verdad
    sig["abs_diff"] = sig.gap.abs()
    return sig


def boot_slope(x, y, n_boot=N_BOOT, seed=0):
    """Pendiente OLS sin intercepto libre raro: y = a + b*x; devuelve b y CI."""
    rng = np.random.default_rng(seed)
    n = len(x)
    if n < 5 or np.allclose(x.var(), 0):
        return np.nan, (np.nan, np.nan)
    b = np.polyfit(x, y, 1)[0]
    bs = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        if np.allclose(x[idx].var(), 0):
            continue
        bs.append(np.polyfit(x[idx], y[idx], 1)[0])
    lo, hi = np.percentile(bs, [2.5, 97.5]) if bs else (np.nan, np.nan)
    return b, (lo, hi)


def flag(n):
    return "" if n >= MIN_CELL_N else " !"


def sec1_beta(sig):
    print("=" * 72)
    print("1. BETA: (real - pred) ~ (ext_med - pred). beta ~ lambda óptimo")
    print("=" * 72)
    b, ci = boot_slope(sig.gap.values, sig.resid.values)
    print(f"GLOBAL  n={len(sig):4d}  beta={b:+.2f}  CI95=[{ci[0]:+.2f},{ci[1]:+.2f}]")
    print("\nPor bucket de |ext_diff| (valida umbrales 1.5/1.0: el umbral")
    print("correcto es el bucket donde el CI de beta deja de excluir 0):")
    sig["db"] = pd.cut(sig.abs_diff, EXT_DIFF_BUCKETS, include_lowest=True)
    for bkt, g in sig.groupby("db", observed=True):
        b, ci = boot_slope(g.gap.values, g.resid.values)
        print(f"  |diff| {str(bkt):14s} n={len(g):4d}{flag(len(g))}  "
              f"beta={b:+.2f}  CI=[{ci[0]:+.2f},{ci[1]:+.2f}]")
    print("\nPor estación:")
    for sid, g in sig.groupby("station_id"):
        b, ci = boot_slope(g.gap.values, g.resid.values)
        print(f"  {sid}  n={len(g):3d}{flag(len(g))}  beta={b:+.2f}  "
              f"CI=[{ci[0]:+.2f},{ci[1]:+.2f}]")
    print("\nPor régimen (clim>=85 y gap>0, i.e. calor+vamos fríos) vs resto:")
    heat = (sig.clim_pct >= 85) & (sig.gap > 0)
    for name, g in [("HEAT_UNDER", sig[heat]), ("RESTO", sig[~heat])]:
        b, ci = boot_slope(g.gap.values, g.resid.values)
        print(f"  {name:10s} n={len(g):4d}{flag(len(g))}  beta={b:+.2f}  "
              f"CI=[{ci[0]:+.2f},{ci[1]:+.2f}]")


def sec2_mae(sig):
    print("\n" + "=" * 72)
    print("2. MAE: pred | ext_med | pred+shift (valida corte de percentil)")
    print("=" * 72)
    sig["mae_pred"] = (sig.actual - sig.pred).abs()
    sig["mae_ext"] = (sig.actual - sig.ext_med).abs()
    sig["mae_shift"] = (sig.actual - (sig.pred + sig.shift_f.fillna(0))).abs()
    def row(name, g):
        print(f"  {name:16s} n={len(g):4d}{flag(len(g))}  "
              f"pred {g.mae_pred.mean():.2f} | ext {g.mae_ext.mean():.2f} | "
              f"pred+shift {g.mae_shift.mean():.2f}")
    row("GLOBAL", sig)
    print("  -- por bucket de clim_pct (busca dónde ext cruza bajo pred):")
    sig["cb"] = pd.cut(sig.clim_pct, CLIM_BUCKETS, include_lowest=True)
    for bkt, g in sig.groupby("cb", observed=True):
        row(f"clim {bkt}", g)
    print("  -- por estación:")
    for sid, g in sig.groupby("station_id"):
        row(sid, g)


def sec3_spread(sig):
    print("\n" + "=" * 72)
    print("3. GUARDIA DE SPREAD: MAE(ext) por cuartil de ext_spread (valida 5.4)")
    print("=" * 72)
    g = sig.dropna(subset=["ext_spread"]).copy()
    if len(g) < 20:
        print("  n insuficiente"); return
    g["sq"] = pd.qcut(g.ext_spread, 4, duplicates="drop")
    for bkt, gg in g.groupby("sq", observed=True):
        print(f"  spread {str(bkt):18s} n={len(gg):3d}{flag(len(gg))}  "
              f"MAE(ext)={ (gg.actual-gg.ext_med).abs().mean():.2f}  "
              f"MAE(pred)={(gg.actual-gg.pred).abs().mean():.2f}")


def sec4_gate(sig, bets_path):
    print("\n" + "=" * 72)
    print("4. GATE: EV de bets direccionales por ext_diff_pre x dirección")
    print("=" * 72)
    bets = pd.read_csv(bets_path)
    bets = bets[bets.won.notna()].copy()
    key = sig.set_index(["station_id", "date"])[["pred", "gap"]].to_dict("index")
    rows = []
    for b in bets.itertuples():
        info = key.get((b.station_id, b.date))
        if info is None:
            continue
        lo, hi, pred = b.bin_lo, b.bin_hi, info["pred"]
        yes = b.side == "yes"
        if not np.isfinite(hi):
            d = "hot" if yes else "cold"
        elif not np.isfinite(lo):
            d = "cold" if yes else "hot"
        elif lo > pred:
            d = "hot" if yes else "cold"
        elif hi < pred:
            d = "cold" if yes else "hot"
        else:
            d = "mid"
        rows.append((d, -info["gap"], b.won, b.pnl))  # -gap = ext_diff_pre
    df = pd.DataFrame(rows, columns=["dir", "ext_diff_pre", "won", "pnl"])
    df["db"] = pd.cut(df.ext_diff_pre, [-np.inf, -2.5, -1.5, -1.0, 1.0, 1.5, 2.5, np.inf])
    piv = df.groupby(["dir", "db"], observed=True).agg(
        n=("won", "size"), wr=("won", "mean"), ev=("pnl", "mean")).round(2)
    print(piv.to_string())
    print("\n  Umbral del gate: para dir=cold, el bucket de ext_diff_pre más")
    print("  cercano a 0 donde ev<0 con n>=%d. Simétrico para hot." % MIN_CELL_N)


def sec5_sign(sig):
    print("\n" + "=" * 72)
    print("5. PERSISTENCIA DE SIGNO del error (para sign-nudge y k_s)")
    print("=" * 72)
    from scipy.stats import binomtest
    for sid, g in sig.groupby("station_id"):
        g = g.sort_values("date")
        d = pd.to_datetime(g.date)
        e = (g.pred - g.actual).values
        pairs = [(e[i-1], e[i]) for i in range(1, len(g))
                 if (d.iloc[i] - d.iloc[i-1]).days == 1 and e[i-1] != 0 and e[i] != 0]
        if len(pairs) < 5:
            print(f"  {sid}: n={len(pairs)} — insuficiente"); continue
        a = np.array(pairs)
        same = int((np.sign(a[:, 0]) == np.sign(a[:, 1])).sum())
        ci = binomtest(same, len(a)).proportion_ci(0.95)
        r = np.corrcoef(a[:, 0], a[:, 1])[0, 1]
        print(f"  {sid}: n={len(a):3d}{flag(len(a))}  P(mismo signo)={same/len(a):.2f} "
              f"CI=[{ci.low:.2f},{ci.high:.2f}]  r_mag={r:+.2f}  "
              f"k_s sugerido={max(0.0, min(1.0, r)):.2f} (shrink con pooled antes de usar)")


def sec6_nudge(sig):
    """Requiere columnas extra en daily_ext_signal:
    sign_nudge_applied (0/1), nudge_f (con signo), pred_pre_bias,
    bias_path ('regime'|'ewma'|'nudge'|'none')."""
    need = {"sign_nudge_applied", "nudge_f", "pred_pre_bias"}
    if not need.issubset(sig.columns):
        return
    print("\n" + "=" * 72)
    print("6. SIGN-NUDGE: ¿reduce MAE o genera flips falsos?")
    print("=" * 72)
    nd = sig[sig.sign_nudge_applied == 1].copy()
    if len(nd) < 10:
        print(f"  n={len(nd)} días con nudge — insuficiente todavía"); return
    nd["mae_sin"] = (nd.actual - nd.pred_pre_bias).abs()
    nd["mae_con"] = (nd.actual - (nd.pred_pre_bias + nd.nudge_f)).abs()
    # flip falso: el nudge alejó la pred del settle
    nd["falso"] = (np.sign(nd.nudge_f) !=
                   np.sign(nd.actual - nd.pred_pre_bias)).astype(int)
    # flip de bin modal: el nudge cambió el entero redondeado
    nd["bin_mov"] = (np.round(nd.pred_pre_bias + nd.nudge_f)
                     != np.round(nd.pred_pre_bias)).astype(int)
    nd["bin_mejora"] = nd.bin_mov & (nd.mae_con < nd.mae_sin)
    print(f"  GLOBAL n={len(nd)}{flag(len(nd))}  MAE sin {nd.mae_sin.mean():.2f} "
          f"-> con {nd.mae_con.mean():.2f}  | falsos {nd.falso.mean():.0%}  "
          f"| bin movido {nd.bin_mov.sum()} (mejora {int(nd.bin_mejora.sum())})")
    for sid, g in nd.groupby("station_id"):
        print(f"  {sid}  n={len(g):3d}{flag(len(g))}  "
              f"MAE {g.mae_sin.mean():.2f} -> {g.mae_con.mean():.2f}  "
              f"falsos {g.falso.mean():.0%}")
    if "ext_diff_pre" in nd.columns or "gap" in nd.columns:
        # nudge concordante vs contradicho por externos (pregunta KPHX 06-11)
        contra = nd[np.sign(nd.nudge_f) == np.sign(nd.gap * -1)]  # aleja de ext
        acorde = nd.drop(contra.index)
        for name, g in [("acorde con ext", acorde), ("contra ext", contra)]:
            if len(g):
                print(f"  nudge {name:15s} n={len(g):3d}{flag(len(g))}  "
                      f"MAE {g.mae_sin.mean():.2f} -> {g.mae_con.mean():.2f}  "
                      f"falsos {g.falso.mean():.0%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", required=True)
    ap.add_argument("--outcomes", required=True)
    ap.add_argument("--bets", default=None)
    args = ap.parse_args()
    sig = load(args)
    if len(sig) < 30:
        print(f"AVISO: solo {len(sig)} filas con settle — los CI serán anchos.",
              file=sys.stderr)
    sec1_beta(sig)
    sec2_mae(sig)
    sec3_spread(sig)
    if args.bets:
        sec4_gate(sig, args.bets)
    sec5_sign(sig)
    sec6_nudge(sig)


if __name__ == "__main__":
    main()
