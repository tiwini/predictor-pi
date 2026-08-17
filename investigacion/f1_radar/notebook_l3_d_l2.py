#!/usr/bin/env python3
"""Notebook L3+D+L2 single-pass — descriptivo error residual del ensemble.

Fable spec (memoria fable_session_2026_07_20_post_r5_decisions):
"Un pass 'error residual del ensemble', tres cortes:
  1. Viento (D)
  2. Aplanamiento last-mile condicionado a slope ≥+1°F/h a 15h local (L3)
  3. Flag convectivo retroactivo por METAR text (L2)"

Corre como script (imprime resultados) o adapta a notebook Jupyter.

Datos: ~100 días-estación en 3 semanas × 20 stations = 420 días-estación.
Del backfill radar: 5 estaciones convectivas KMIA/KIAH/KAUS/KATL/KMSY.

Doctrina "el número lo firma la data" — NO pre-fijar thresholds.
"""
import sys
import sqlite3
from pathlib import Path
from collections import defaultdict, Counter
from statistics import mean, median, stdev

sys.path.insert(0, str(Path(__file__).resolve().parent))
from join_radar_obs import join_radar_obs, summary_stats

DB_PATH = Path(__file__).resolve().parent.parent.parent / "weather-predictor" / "analysis.db"

CONVECTIVE = ["KMIA", "KIAH", "KAUS", "KATL", "KMSY"]
ALL_STATIONS = ["KPHX", "KLAX", "KLAS", "KNYC", "KBOS",
                "KMIA", "KMDW", "KIAH", "KSFO", "KAUS",
                "KDEN", "KSAT", "KDCA", "KDFW", "KPHL",
                "KSEA", "KATL", "KMSY", "KOKC", "KMSP"]
DATE_RANGE = ("2026-07-03", "2026-07-23")


def load_all(conn) -> dict[str, list[dict]]:
    """Load joined rows per station."""
    data = {}
    for sid in ALL_STATIONS:
        rows = join_radar_obs(conn, sid, DATE_RANGE)
        data[sid] = rows
        s = summary_stats(rows)
        print(f"  {sid}: {s['total_rows']} snapshots, "
              f"{s['with_radar_match']} con radar ({s['match_rate']*100:.0f}%)")
    return data


def daily_error(rows: list[dict]) -> list[dict]:
    """Compute daily prediction error: for each day, take pred_at_09h vs today_max_obs."""
    by_date = defaultdict(list)
    for r in rows:
        date = r["ts"][:10]
        by_date[date].append(r)
    daily = []
    for date, day_rows in by_date.items():
        # Sort by time
        day_rows.sort(key=lambda x: x["ts"])
        # Pred del día = ens_med de snapshot ~09-10h local
        # Aproximación: primer snapshot con ens_med not None despues de 09:00 local
        # Aquí simplifico: mediana de ens_med primeras 3h del día (después de sunrise-ish)
        preds = [r["ens_med"] for r in day_rows if r["ens_med"] is not None]
        if len(preds) < 3:
            continue
        pred = median(preds[:5])  # aprox pred temprano
        # Obs = max final del día
        maxes = [r["today_max_obs"] for r in day_rows if r["today_max_obs"] not in (None, -999)]
        if not maxes:
            continue
        obs = max(maxes)
        # Radar signature del día
        dbz5s = [r["dbz_5x5"] for r in day_rows if r.get("dbz_5x5") is not None]
        dbz9s = [r["dbz_9x9"] for r in day_rows if r.get("dbz_9x9") is not None]
        # Wind
        winds = [r["wind_mph"] for r in day_rows if r.get("wind_mph") is not None]
        gusts = [r["wind_gust_mph"] for r in day_rows if r.get("wind_gust_mph") is not None]
        # Convective flag ya parseado
        conv = sum(1 for r in day_rows if r.get("convective_ambient"))
        daily.append({
            "date": date,
            "pred": pred,
            "obs": obs,
            "err": pred - obs,
            "err_abs": abs(pred - obs),
            "max_wind": max(winds) if winds else None,
            "max_gust": max(gusts) if gusts else None,
            "max_dbz5": max(dbz5s) if dbz5s else None,
            "max_dbz9": max(dbz9s) if dbz9s else None,
            "convective_pct": conv / len(day_rows) if day_rows else 0,
            "n_snapshots": len(day_rows),
        })
    return sorted(daily, key=lambda x: x["date"])


def cut_D_wind(data: dict[str, list[dict]]):
    """CORTE D — viento vs error del ensemble.
    Hipótesis: días con wind alto (gust >20mph) → mayor error absoluto."""
    print("\n" + "=" * 70)
    print("CORTE D — VIENTO vs ERROR ENSEMBLE")
    print("=" * 70)
    buckets = {"calm": [], "moderate": [], "windy": [], "gusty": []}
    for sid, rows in data.items():
        daily = daily_error(rows)
        for d in daily:
            gust = d["max_gust"] or 0
            if gust == 0:
                buckets["calm"].append(d)
            elif gust < 15:
                buckets["moderate"].append(d)
            elif gust < 25:
                buckets["windy"].append(d)
            else:
                buckets["gusty"].append(d)
    print(f"{'Bucket':10} {'N':>5} {'MAE °F':>7} {'RMSE':>7} {'bias':>7}")
    for name, days in buckets.items():
        if not days:
            print(f"  {name:10} {'0':>5}")
            continue
        errs = [d["err"] for d in days]
        abs_errs = [d["err_abs"] for d in days]
        n = len(days)
        mae = mean(abs_errs)
        rmse = (sum(e**2 for e in errs) / n) ** 0.5
        bias = mean(errs)
        print(f"  {name:10} {n:>5} {mae:>7.2f} {rmse:>7.2f} {bias:+7.2f}")


def cut_L3_lastmile(data: dict[str, list[dict]]):
    """CORTE L3 — aplanamiento last-mile.
    Hipótesis: días con slope ≥+1°F/h medido a las 15h local tienden a
    aplanarse (obs final < pred). Requiere reconstruir slope local por día."""
    print("\n" + "=" * 70)
    print("CORTE L3 — LAST-MILE FLATTENING (slope ≥+1°F/h a 15h local)")
    print("=" * 70)
    # Bucket por slope observado a las 15h
    high_slope_days = []
    low_slope_days = []
    for sid, rows in data.items():
        by_date = defaultdict(list)
        for r in rows:
            by_date[r["ts"][:10]].append(r)
        for date, day_rows in by_date.items():
            day_rows.sort(key=lambda x: x["ts"])
            # Find snapshots aprox 14h y 15h UTC (aprox local; simplifico)
            # Real: usar station.tz para local time. Aquí aprox.
            near_15h = [r for r in day_rows if "15:" in r["ts"][11:16] and r.get("current_f")]
            near_14h = [r for r in day_rows if "14:" in r["ts"][11:16] and r.get("current_f")]
            if not near_15h or not near_14h:
                continue
            slope = near_15h[0]["current_f"] - near_14h[-1]["current_f"]  # ~1h diff
            # Pred final vs obs final
            preds = [r["ens_med"] for r in day_rows if r.get("ens_med") is not None]
            maxes = [r["today_max_obs"] for r in day_rows if r["today_max_obs"] not in (None, -999)]
            if not preds or not maxes:
                continue
            pred = median(preds[:5]) if len(preds) >= 5 else median(preds)
            obs = max(maxes)
            err = pred - obs
            entry = {"date": date, "station": sid, "slope": slope, "err": err}
            if slope >= 1.0:
                high_slope_days.append(entry)
            else:
                low_slope_days.append(entry)
    for label, days in [("slope ≥+1°F/h @ 15h", high_slope_days),
                          ("slope <1°F/h @ 15h", low_slope_days)]:
        if not days:
            print(f"  {label}: N=0")
            continue
        errs = [d["err"] for d in days]
        n = len(days)
        bias = mean(errs)
        mae = mean(abs(e) for e in errs)
        print(f"  {label:25} N={n:4} bias={bias:+.2f}°F MAE={mae:.2f}°F")


def cut_L2_convective(data: dict[str, list[dict]]):
    """CORTE L2 — flag convectivo retroactivo.
    Compara parse_convective_flags() ya persistido vs dbz_5x5/9x9 del radar.
    Hipótesis: cuando METAR dice TS/CB pero dbz_5x5 < X → falso positivo
    METAR (o storm demasiado lejos)."""
    print("\n" + "=" * 70)
    print("CORTE L2 — CONVECTIVE FLAG RETROACTIVO (METAR vs radar)")
    print("=" * 70)
    # Solo estaciones convectivas con radar
    for sid in CONVECTIVE:
        rows = data.get(sid, [])
        if not rows:
            continue
        # Buckets
        conv_metar_and_radar = 0  # convective_ambient AND dbz >= 30
        conv_metar_no_radar = 0  # convective_ambient True but dbz < 30 or None
        no_conv_but_radar = 0  # convective_ambient False but dbz >= 30
        neither = 0
        with_radar = 0
        for r in rows:
            has_radar = r.get("dbz_5x5") is not None or r.get("dbz_9x9") is not None
            if not has_radar:
                continue
            with_radar += 1
            dbz_max = max(r.get("dbz_5x5") or -99, r.get("dbz_9x9") or -99)
            conv = bool(r.get("convective_ambient"))
            radar_strong = dbz_max >= 30
            if conv and radar_strong:
                conv_metar_and_radar += 1
            elif conv and not radar_strong:
                conv_metar_no_radar += 1
            elif not conv and radar_strong:
                no_conv_but_radar += 1
            else:
                neither += 1
        print(f"\n  {sid} (N={with_radar} con radar):")
        print(f"    METAR conv=Y & radar strong (≥30): {conv_metar_and_radar:4}")
        print(f"    METAR conv=Y & radar weak (<30):   {conv_metar_no_radar:4}")
        print(f"    METAR conv=N & radar strong:       {no_conv_but_radar:4}")
        print(f"    Neither:                            {neither:4}")


def cut_extra_dbz9_minus_5x5(data: dict[str, list[dict]]):
    """CORTE extra — hipótesis Fable D1 (memoria f1_radar_design_closed).
    dbz_9x9 - dbz_5x5 alto = storm cerca pero no encima (outflow).
    Distribución de esta diferencia y correlación con error del pred."""
    print("\n" + "=" * 70)
    print("CORTE extra — dbz_9x9 - dbz_5x5 (hipótesis D1 outflow)")
    print("=" * 70)
    diffs = []
    for sid in CONVECTIVE:
        rows = data.get(sid, [])
        for r in rows:
            d5 = r.get("dbz_5x5")
            d9 = r.get("dbz_9x9")
            if d5 is not None and d9 is not None:
                diffs.append((sid, d9 - d5, d5, d9))
    if not diffs:
        print("  N=0 (backfill aún no completo?)")
        return
    print(f"  N samples: {len(diffs)}")
    dvals = [d[1] for d in diffs]
    dvals.sort()
    n = len(dvals)
    print(f"  dbz_9x9 - dbz_5x5 distribution:")
    print(f"    min={dvals[0]}, p10={dvals[int(n*.10)]}, p50={dvals[n//2]}, "
          f"p90={dvals[int(n*.90)]}, max={dvals[-1]}")
    # Cases where 9x9 significantly > 5x5 (storm cerca pero no encima)
    outflow = [d for d in diffs if d[1] >= 15]
    print(f"  Casos 9x9 - 5x5 ≥ 15 dBZ (outflow candidates): {len(outflow)}")
    for sid, diff, d5, d9 in outflow[:10]:
        print(f"    {sid}: 5x5={d5} 9x9={d9} diff={diff}")


def main():
    print("NOTEBOOK L3+D+L2 SINGLE-PASS DESCRIPTIVE")
    print(f"Date range: {DATE_RANGE[0]} to {DATE_RANGE[1]}")
    print()
    conn = sqlite3.connect(str(DB_PATH))
    print("=== LOADING DATA ===")
    data = load_all(conn)
    print()
    cut_D_wind(data)
    cut_L3_lastmile(data)
    cut_L2_convective(data)
    cut_extra_dbz9_minus_5x5(data)


if __name__ == "__main__":
    main()
