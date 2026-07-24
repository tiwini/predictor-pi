#!/usr/bin/env python3
"""5º corte v2 — timing sweet spot per station. Fixes bugs de v1.

Fixes:
1. Filter snapshots to peak window only (no post-settle Kalshi contamination)
2. Use station_snapshots.today_max_obs as batch settle source (avoid slow NWS CLI)
3. Use our_p_calibrated persisted en kalshi_snapshots (no re-compute)

Método:
- Load kalshi_snapshots + station_snapshots del último 21d
- Filter tickers same-day only, snapshots antes del PEAK_HOURS[hi]+1h local
- Compute settle_ref = max(today_max_obs) del día (round to int para bin match)
- Per (station, day, hour) que cumpla entry criterion, simular bet
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "weather-predictor"))
from stations import PEAK_HOURS

DB_PATH = Path(__file__).resolve().parent.parent.parent / "weather-predictor" / "analysis.db"

STATIONS = ["KMIA", "KIAH", "KAUS", "KATL", "KMSY",
            "KNYC", "KBOS", "KDCA", "KPHL", "KPHX"]

TZ_MAP = {
    "KMIA": "America/New_York", "KATL": "America/New_York",
    "KIAH": "America/Chicago", "KAUS": "America/Chicago", "KMSY": "America/Chicago",
    "KNYC": "America/New_York", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KPHL": "America/New_York",
    "KPHX": "America/Phoenix",
}

SPREAD_MAX = 1.0
EXT_DIFF_MAX = 1.0
KALSHI_MODO_MAX = 0.65

# Post-window cutoff safety: excluir snapshots >1h después del cierre ventana
POST_WIN_CUTOFF_H = 1


def parse_ticker_date(ticker: str):
    """26JUL23 → date(2026,7,23)"""
    try:
        date_part = ticker.split("-")[1]
        yy = int(date_part[:2]) + 2000
        mmm = date_part[2:5]
        dd = int(date_part[5:])
        mm = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}[mmm]
        return date(yy, mm, dd)
    except Exception:
        return None


def load_settles_from_db(conn) -> dict:
    """Batch settle = max(today_max_obs) del día por estación.
    Snapshots de after peak window ya reflejan el settle real."""
    settles = {}
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT station, date(ts) as d, MAX(today_max_obs) as max_obs
        FROM station_snapshots
        WHERE ts > datetime('now', '-25 days')
          AND today_max_obs IS NOT NULL AND today_max_obs > -900
        GROUP BY station, date(ts)
    """).fetchall()
    for r in rows:
        settles[(r["station"], r["d"])] = int(round(r["max_obs"]))
    return settles


def analyze_station(conn, sid: str, settles: dict) -> list[dict]:
    tz = ZoneInfo(TZ_MAP[sid])
    peak_lo, peak_hi = PEAK_HOURS[sid]
    conn.row_factory = sqlite3.Row

    # Kalshi: filter same-day tickers + peak window ± cutoff
    kalshi = conn.execute("""
        SELECT ts, ticker, label, bin_lo, bin_hi, yes_mid, our_p_calibrated
        FROM kalshi_snapshots
        WHERE station = ? AND ts > datetime('now', '-21 days')
          AND yes_mid IS NOT NULL
        ORDER BY ts
    """, (sid,)).fetchall()

    # Group Kalshi by (day_local, hour_local) — only rows dentro peak window
    kalshi_by_slot = defaultdict(list)
    for r in kalshi:
        tdate = parse_ticker_date(r["ticker"])
        if tdate is None:
            continue
        ts = datetime.fromisoformat(r["ts"])
        loc = ts.astimezone(tz)
        # Same-day ticker
        if tdate != loc.date():
            continue
        # BEFORE peak_hi + cutoff hours (skip post-settle)
        if loc.hour >= peak_hi + POST_WIN_CUTOFF_H:
            continue
        slot = (loc.date().isoformat(), loc.hour)
        kalshi_by_slot[slot].append(dict(r))

    # Station snapshots grouped similarly
    ss = conn.execute("""
        SELECT ts, ens_p10, ens_p90, ext_diff_f
        FROM station_snapshots
        WHERE station = ? AND ts > datetime('now', '-21 days')
    """, (sid,)).fetchall()
    ss_by_slot = defaultdict(list)
    for r in ss:
        ts = datetime.fromisoformat(r["ts"])
        loc = ts.astimezone(tz)
        if loc.hour >= peak_hi + POST_WIN_CUTOFF_H:
            continue
        slot = (loc.date().isoformat(), loc.hour)
        ss_by_slot[slot].append(dict(r))

    results = []
    for slot, kalshi_bins in kalshi_by_slot.items():
        ss_hour = ss_by_slot.get(slot, [])
        if not ss_hour:
            continue
        # Use latest snapshot of that hour
        ss_latest = ss_hour[-1]
        p10 = ss_latest.get("ens_p10")
        p90 = ss_latest.get("ens_p90")
        ext_diff = ss_latest.get("ext_diff_f")
        if p10 is None or p90 is None:
            continue
        spread = p90 - p10

        # Latest per bin in the hour
        latest_per_bin = {}
        for k in kalshi_bins:
            key = (k["bin_lo"], k["bin_hi"])
            if key not in latest_per_bin or k["ts"] > latest_per_bin[key]["ts"]:
                latest_per_bin[key] = k
        finite_bins = [k for k in latest_per_bin.values()
                       if k["bin_hi"] < 200 and k["bin_lo"] > -50]
        if not finite_bins:
            continue
        modo = max(finite_bins, key=lambda k: k["yes_mid"])
        modo_price = modo["yes_mid"]

        crit1 = spread <= SPREAD_MAX
        crit2 = ext_diff is not None and abs(ext_diff) <= EXT_DIFF_MAX
        crit3 = modo_price < KALSHI_MODO_MAX
        entry = crit1 and crit2 and crit3

        settle = settles.get((sid, slot[0]))
        pnl = None
        if entry and settle is not None:
            if modo["bin_lo"] <= settle <= modo["bin_hi"]:
                pnl = 1 - modo_price
            else:
                pnl = -modo_price

        results.append({
            "date": slot[0], "hour": slot[1],
            "spread": spread, "ext_diff": ext_diff,
            "modo_label": modo["label"], "modo_price": modo_price,
            "entry": entry, "pnl": pnl, "settle": settle,
            "peak_hi_local": peak_hi,
        })
    return results


def main():
    print("=" * 70)
    print("5º CORTE v2 — TIMING SWEET SPOT (fixes: filter post-settle + batch settles)")
    print("=" * 70)
    print(f"Criteria: spread≤{SPREAD_MAX}°F | |ext_diff|≤{EXT_DIFF_MAX}°F | "
          f"kalshi_modo<{KALSHI_MODO_MAX}")
    print(f"Snapshots filter: strict same-day ticker + before peak_hi+{POST_WIN_CUTOFF_H}h local")
    print()

    conn = sqlite3.connect(str(DB_PATH))
    print("Loading batch settles from station_snapshots...")
    settles = load_settles_from_db(conn)
    print(f"  {len(settles)} settle values from DB (fast, batch)")
    print()

    all_results = {}
    for sid in STATIONS:
        all_results[sid] = analyze_station(conn, sid, settles)
        n_entries = sum(1 for r in all_results[sid] if r["entry"])
        n_wpnl = sum(1 for r in all_results[sid] if r["pnl"] is not None)
        print(f"  {sid}: {len(all_results[sid])} slots, {n_entries} entries "
              f"({n_wpnl} with settle-based PnL)")

    print()
    print("=" * 70)
    print("ROI POR ESTACIÓN × HORA LOCAL")
    print("=" * 70)
    for sid in STATIONS:
        by_hour = defaultdict(list)
        for r in all_results[sid]:
            if r["entry"] and r["pnl"] is not None:
                by_hour[r["hour"]].append(r["pnl"])
        if not by_hour:
            print(f"\n{sid}: 0 entries with settle")
            continue
        peak_hi = PEAK_HOURS[sid][1]
        peak_lo = PEAK_HOURS[sid][0]
        print(f"\n{sid} (peak window {peak_lo:02d}-{peak_hi:02d} local):")
        print(f"  {'hour':>4} {'N':>3} {'ROI %':>7} {'win%':>6}")
        for hh in sorted(by_hour.keys()):
            pnls = by_hour[hh]
            roi = mean(pnls) * 100
            wins = sum(1 for p in pnls if p > 0) / len(pnls) * 100
            in_window = '★' if peak_lo <= hh < peak_hi else ' '
            print(f"  {in_window}{hh:>3} {len(pnls):>3} {roi:>+6.1f} {wins:>5.0f}%")

    print()
    print("=" * 70)
    print("BEST HOUR per station (min N=3)")
    print("=" * 70)
    for sid in STATIONS:
        by_hour = defaultdict(list)
        for r in all_results[sid]:
            if r["entry"] and r["pnl"] is not None:
                by_hour[r["hour"]].append(r["pnl"])
        valid = [(hh, mean(pnls), len(pnls)) for hh, pnls in by_hour.items() if len(pnls) >= 3]
        if not valid:
            max_n = max((len(p) for p in by_hour.values()), default=0)
            print(f"  {sid}: no hour with N>=3 (max N={max_n})")
            continue
        best = max(valid, key=lambda x: x[1])
        pre_window = '(pre-window)' if best[0] < PEAK_HOURS[sid][0] else ''
        print(f"  {sid}: best hour={best[0]:02d}h  ROI={best[1]*100:+.1f}%  N={best[2]} {pre_window}")


if __name__ == "__main__":
    main()
