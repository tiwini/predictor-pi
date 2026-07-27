#!/usr/bin/env python3
"""Lectura accionable de UNA estación, con todos los gates de la doctrina.

Read-only sobre analysis.db del Pi. Sin LLM, sin side effects (a diferencia de
lectura.py, que cambia la estación activa del server). Los gates salen de
agent_signals para no divergir de lo que el poller y bets evalúan.

Uso:  ./venv/bin/python3 investigacion/lectura_estacion.py KMIA
      ... --cli     además pega al NWS por el CLI parcial en vivo
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
import agent_signals as A                                    # noqa: E402
from stations import STATION_TZ, CLI_LATE_HOUR, PEAK_HOURS   # noqa: E402

ANALYSIS_DB = BASE / "analysis.db"
CALIB_DB = BASE / "calibration.db"

# Hora local desde la que el modelo sostiene >=70% (convergencia_horaria 07-26).
CONV_H = {"KPHX": 16, "KMIA": 13, "KLAS": 15, "KNYC": 20,
          "KATL": 15, "KPHL": 17, "KOKC": 17}
NUNCA_CONVERGE = {"KBOS", "KLAX", "KDCA", "KSEA", "KMDW", "KDEN"}
# Hora local en que el pico ya está puesto el 100% de los días (tabla B).
PEAK_DONE_H = {"KLAX": 13, "KMIA": 14, "KNYC": 14, "KBOS": 15, "KDEN": 15,
               "KDCA": 16, "KMDW": 16, "KATL": 16, "KPHL": 16,
               "KPHX": 17, "KSEA": 17, "KLAS": 17, "KOKC": 17}
DIFFICULTY_DOCTRINE = 70.0   # feedback_triple_convergence_fails_regime_roto
# Los trades anteriores a esta fecha viven sobre el ledger roto (auditoría
# Fable 2026-07-07): su ROI global era +53.2% sobre 548 trades y se sabe
# artefacto. Cualquier ROI que los mezcle es ese número, no el del sistema.
LEDGER_FIX_DATE = "2026-07-07"


def f(v, w=6, d=1):
    return f"{v:{w}.{d}f}" if v is not None else " " * (w - 1) + "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("station")
    ap.add_argument("--cli", action="store_true",
                    help="pega al NWS por el CLI parcial en vivo")
    args = ap.parse_args()
    st = args.station.upper()
    if st not in STATION_TZ:
        print(f"estación desconocida: {st}")
        return 1

    tz = ZoneInfo(STATION_TZ[st])
    now_local = datetime.now(tz)
    hour_f = now_local.hour + now_local.minute / 60

    con = sqlite3.connect(f"file:{ANALYSIS_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM station_snapshots WHERE station=? "
                    "ORDER BY ts DESC LIMIT 1", (st,)).fetchone()
    if r is None:
        print(f"sin snapshots de {st}")
        return 1

    age_min = (datetime.now(timezone.utc)
               - datetime.fromisoformat(r["ts"])).total_seconds() / 60

    print(f"\n=== {st} — lectura {now_local:%H:%M} local ({now_local:%Z}) ===")
    # feedback_verify_snapshot_age: hasta 10 min es normal, más es sospechoso.
    flag = "✓ fresco" if age_min <= 11 else "⚠ STALE — forzar build_snapshot"
    print(f"snapshot {r['ts'][11:16]}Z · age {age_min:.0f} min  {flag}")

    print("\nPREDICCIÓN")
    print(f"  ens_med (crudo)     {f(r['ens_med'])}°F")
    print(f"  our_pred_f          {f(r['our_pred_f'])}°F   <- el que clasifica dirección")
    print(f"  pred_iso_med_f      {f(r['pred_iso_med_f'])}°F   <- post isotónica + blend")
    print(f"  banda p10-p90       {f(r['ens_p10'])} .. {f(r['ens_p90'])}")
    ext_d = r["ext_diff_f"]
    band = ""
    if ext_d is not None:
        # ext_diff_matinal_predice_error_2026_07_27 (N=483)
        if ext_d >= 3:
            band = "  ⚠ banda >+3: sobre-predecimos el 92% de los días (+3.76°F mediano)"
        elif ext_d >= 1.5:
            band = "  ⚠ banda +1.5/+3: sobre-predecimos el 79% (+1.43°F mediano)"
        elif ext_d < 0:
            band = "  banda <0: sub-predecimos (error -1.71°F mediano)"
    print(f"  externos (mediana)  {f(r['ext_med_f'])}°F   ext_diff {f(ext_d, 5, 1)}{band}")

    print("\nOBSERVACIÓN")
    print(f"  max obs hoy         {f(r['today_max_obs'])}°F   (feed 5-min)")
    cli_h = CLI_LATE_HOUR[st]
    if r["today_max_cli"] is not None:
        print(f"  CLI parcial         {f(r['today_max_cli'])}°F   ← piso duro, "
              f"iguala el settle final el 91% de los días")
    else:
        print(f"  CLI parcial         —      sale ~{int(cli_h):02d}:{int(cli_h % 1 * 60):02d} local; "
              f"hasta entonces el feed subestima ~1.0°F")

    print("\nESTADO FÍSICO")
    lo_h, hi_h = PEAK_HOURS[st]
    win = "ABIERTA" if lo_h <= now_local.hour < hi_h else "cerrada"
    print(f"  ventana de pico     {lo_h}-{hi_h}h local · {win}")
    done_h = PEAK_DONE_H.get(st)
    if done_h:
        print(f"  pico puesto 100%    desde las {done_h}h local"
              f"{'  ← ya pasó' if hour_f >= done_h else '  ← todavía no'}")
    conv = CONV_H.get(st)
    if st in NUNCA_CONVERGE:
        print("  convergencia        NUNCA sostiene 70% — no operar por convergencia")
    elif conv:
        print(f"  convergencia        ≥70% desde las {conv}h local"
              f"{'  ← ya legible' if hour_f >= conv else '  ← aún no legible'}")
    print(f"  peak_status         {r['peak_status']}")
    print(f"  regime              {r['regime_tag']} · {r['regime_reason']}")
    if r["convective_ambient"]:
        print("  convectivo          ⚠ SÍ — no confirma pico ni rompe meseta (KMIA 07-19)")
    if r["narrative_line"]:
        print(f"  narrativa           {r['narrative_line']}")

    print("\nGATES")
    diff = r["difficulty_score"]
    if diff is not None and diff > DIFFICULTY_DOCTRINE:
        print(f"  difficulty          {diff:.0f}  🔴 BLOQUEA (doctrina: no operar >{DIFFICULTY_DOCTRINE:.0f})")
    else:
        print(f"  difficulty          {f(diff, 4, 0)}  ok")
    if r["difficulty_reasons_json"]:
        print(f"    por qué:          {r['difficulty_reasons_json'][:150]}")
    print(f"  bias                {f(r['bias_f'], 5, 2)}  aplicado={r['bias_applied']} path={r['bias_path']}")
    print(f"  streak hot/cold     {r['streak_block_hot']}/{r['streak_block_cold']}"
          f"   cold_bias_block={r['cold_bias_block']}")
    # `roi_hist_pct` sale de A.historical_roi sobre TODA la historia, y el
    # grueso de esa historia es anterior al arreglo del ledger del 2026-07-07
    # — el periodo cuyo ROI +53% global ya se sabe artefacto. Mostrarlo pelado
    # hace que KLAS parezca +87% rentable cuando esos 72 trades son todos
    # pre-fix y no ha ejecutado ninguno desde entonces. Se parte en dos.
    cal = sqlite3.connect(f"file:{CALIB_DB}?mode=ro", uri=True)
    print("  ROI (ejecutados, sin shadow)")
    for lab, cond in (("pre  07-07 ⚠ledger roto", "date < ?"),
                      ("post 07-07 ✓fiable", "date >= ?")):
        q = cal.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(SUM(stake),0),
                       COALESCE(SUM(won),0)
                FROM simulated_bets
                WHERE station_id=? AND won IS NOT NULL AND blocked_by IS NULL
                  AND {cond}""", (st, LEDGER_FIX_DATE)).fetchone()
        n, pnl, stake, wins = q
        roi = f"{100 * pnl / stake:+.1f}%" if stake else "   —"
        print(f"    {lab:24s} N={n:3d}  ROI {roi:>8s}  wins {wins}/{n}")
    sh = cal.execute(
        """SELECT COUNT(*) FROM simulated_bets WHERE station_id=? AND
           won IS NOT NULL AND blocked_by IS NOT NULL AND date >= ?""",
        (st, LEDGER_FIX_DATE)).fetchone()[0]
    if sh:
        print(f"    {'shadow post-fix':24s} N={sh:3d}  (bloqueados por guardas, "
              f"no ejecutados)")
    cal.close()

    if args.cli:
        try:
            import nws_cli
            mx, iss = nws_cli.fetch_intraday_max(st, now_local.date())
            print(f"\nNWS EN VIVO   CLI parcial: {mx}  (emitido {iss})")
        except Exception as e:
            print(f"\nNWS EN VIVO   error: {e}")

    # ---- mercado ----
    print("\nMERCADO KALSHI (último ciclo)")
    bins = con.execute(
        """SELECT * FROM kalshi_snapshots WHERE station=? AND ts=(
               SELECT MAX(ts) FROM kalshi_snapshots WHERE station=?)
           ORDER BY bin_lo""", (st, st)).fetchall()
    if not bins:
        print("  (sin bins — mercado cerrado o sin datos)")
    else:
        print(f"  {'bin':22s} {'mercado':>8s} {'our_p':>7s} {'our_cal':>8s} "
              f"{'edge':>7s} {'side':>5s} {'dir':>5s}  veredicto")
        for b in bins:
            ev = A.evaluate_bin(
                station_id=st, bin_lo=b["bin_lo"], bin_hi=b["bin_hi"],
                bin_label=b["label"] or "", kalshi_yes_price=b["yes_mid"],
                model_p_calibrated=b["our_p_calibrated"], model_p_raw=b["our_p"],
                our_pred_f=r["our_pred_f"], ext_diff_f=ext_d,
                difficulty_score=diff,
                streak_hot_n=r["streak_block_hot"] or 0,
                streak_cold_n=r["streak_block_cold"] or 0,
                cold_bias_block=bool(r["cold_bias_block"]))
            edge = ev["edge_pp"]
            # A.DIFFICULTY_BLOCK_THRESHOLD está en 999 desde el 2026-07-06, así
            # que evaluate_bin NO bloquea por difficulty y marcaría ACTIONABLE
            # con difficulty 94. La doctrina de no operar >70 sigue vigente, así
            # que se aplica acá — si no, esta tabla se contradice con sus GATES.
            extra = []
            if diff is not None and diff > DIFFICULTY_DOCTRINE:
                extra.append(f"difficulty {diff:.0f}>{DIFFICULTY_DOCTRINE:.0f}")
            # Cola barata: con el mercado a 1-2¢ el "edge" sale de que la
            # isotónica levanta p hasta su piso (MIN_P=0.03) y el blend lo sube
            # más. No es señal, es el suelo del calibrador.
            if (b["yes_mid"] is not None and b["yes_mid"] <= 0.02
                    and ev["recommended_side"] == "YES"):
                extra.append("cola 1¢: edge = piso del calibrador, no señal")
            # `bias_blocks_direction` devuelve False para 'mid' por diseño, así
            # que el bin que contiene our_pred_f no recibe el gate de ext_diff.
            # Aviso genérico; no se ha observado un caso real todavía — el
            # candidato de KBOS 2026-07-27 resultó ser 'cold', porque
            # our_pred_f era 81.045 y el borde del bin 81 queda por debajo.
            if (ev["direction"] == "mid" and ext_d is not None
                    and abs(ext_d) >= A.EXT_DIFF_BLOCK_THRESHOLD):
                extra.append(f"⚠ bin mid: el gate de ext_diff ({ext_d:+.1f}) NO cubre mid")
            reasons = extra + list(ev["blocked_reasons"])
            verdict = "✅ ACTIONABLE" if (ev["actionable"] and not extra) else (
                " · ".join(reasons)[:70] if reasons else "—")
            print(f"  {(b['label'] or '')[:22]:22s} {f(b['yes_mid'], 8, 2)} "
                  f"{f(b['our_p'], 7, 3)} {f(b['our_p_calibrated'], 8, 3)} "
                  f"{f(edge, 6, 1)}pp {str(ev['recommended_side'] or '-'):>4s}"
                  f" {str(ev['direction'] or '-'):>5s}  {verdict}")

    print("\nCHECKLIST antes de cualquier llamada (doctrina de memoria)")
    print("  [ ] snapshot age ≤10 min (arriba)")
    print("  [ ] difficulty ≤70 y régimen no roto")
    print("  [ ] cruzar la hora local con 'pico puesto': si no está puesto, max_obs no informa")
    print("  [ ] ext_diff: si ≥+1.5 en oeste/sur, anclar a ext_med y NO comprar bins altos")
    print("  [ ] prob_rising=0 pesa MÁS que un EV alto — verificar lo físico antes de vender el setup")
    print("  [ ] si hay CLI parcial, es piso duro: ningún bin por debajo puede ganar")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
