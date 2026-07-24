#!/usr/bin/env python3
"""5º corte: timing sweet spot per station (validación hipótesis KMIA N=1 hoy).

Hipótesis (memoria kmia_timing_hypothesis_2026_07_24):
- Cuando spread ensemble ≤1°F + |ext_diff|≤1°F + kalshi_modo_price<65¢ simultáneos,
  hay edge para entrar bet YES en el bin modo Kalshi.
- El sweet spot temporal puede estar antes de la ventana peak oficial.

Método:
- Data: last 21 days × 5 convectivas + 5 curadas (10 stations)
- Por cada día × estación × hora local, computar los 3 criterios
- Si entry criterion cumplido, simular bet YES bin modo → settle vs bin real
- Agregate ROI por hora del día por estación

Ejecutar: ./venv/bin/python3 investigacion/f1_radar/timing_sweet_spot.py
"""
import sqlite3
import sys
from collections import defaultdict, Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "weather-predictor"))
import nws_cli

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

# Entry criteria
SPREAD_MAX = 1.0        # °F
EXT_DIFF_MAX = 1.0      # °F
KALSHI_MODO_MAX = 0.65  # 65 cents

DAYS_BACK = 21


def get_settles(stations: list[str], days: int) -> dict:
    """Fetch NWS CLI settles for last N days."""
    settles = {}
    today = date.today()
    for d_offset in range(1, days + 1):
        d = today - timedelta(days=d_offset)
        for sid in stations:
            try:
                r = nws_cli.fetch_max_min_for(sid, d)
                if r[0] is not None:
                    settles[(sid, d.isoformat())] = int(round(r[0]))
            except Exception:
                pass
    return settles


def analyze_station(conn, sid: str, settles: dict) -> list[dict]:
    """For each hour × day, check entry criterion + simulated PnL."""
    tz = ZoneInfo(TZ_MAP[sid])
    # Load kalshi snapshots grouped by (date_local, hour_local)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT ts, ticker, label, bin_lo, bin_hi, yes_mid
        FROM kalshi_snapshots
        WHERE station = ? AND ts > datetime('now', '-21 days')
          AND yes_mid IS NOT NULL
        ORDER BY ts
    """, (sid,)).fetchall()

    # Group Kalshi by (date_local, hour_local, ticker_date)
    kalshi_by_slot = defaultdict(list)  # (day, hour) -> list of (label, bin_lo, bin_hi, yes_mid)
    for r in rows:
        ts = datetime.fromisoformat(r["ts"])
        loc = ts.astimezone(tz)
        # Kalshi ticker date = day of settle. Format: PREFIX-26JUL23-XXX
        ticker = r["ticker"]
        # Extract date from ticker: e.g. KXHIGHMIA-26JUL23-T89
        try:
            date_part = ticker.split("-")[1]  # 26JUL23
            yy = int(date_part[:2]) + 2000
            mmm = date_part[2:5]
            dd = int(date_part[5:])
            mm = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}[mmm]
            ticker_date = date(yy, mm, dd)
        except:
            continue
        # Only same-day tickers (settle today according to local date)
        if ticker_date != loc.date():
            continue
        slot = (loc.date().isoformat(), loc.hour)
        kalshi_by_slot[slot].append({
            "label": r["label"], "bin_lo": r["bin_lo"], "bin_hi": r["bin_hi"],
            "yes_mid": r["yes_mid"],
        })

    # Load station snapshots grouped by (date_local, hour_local)
    ss_by_slot = defaultdict(list)
    ss_rows = conn.execute("""
        SELECT ts, ens_p10, ens_p90, ext_diff_f, ext_med_f, pred_calibrated_f, ens_med
        FROM station_snapshots
        WHERE station = ? AND ts > datetime('now', '-21 days')
    """, (sid,)).fetchall()
    for r in ss_rows:
        ts = datetime.fromisoformat(r["ts"])
        loc = ts.astimezone(tz)
        slot = (loc.date().isoformat(), loc.hour)
        ss_by_slot[slot].append(dict(r))

    # For each slot, compute entry criterion + PnL
    results = []
    for slot, kalshi_bins in kalshi_by_slot.items():
        ss = ss_by_slot.get(slot, [])
        if not ss:
            continue
        # Use median-ish snapshot from that hour
        ss_avg = ss[len(ss)//2]
        p10 = ss_avg.get("ens_p10")
        p90 = ss_avg.get("ens_p90")
        ext_diff = ss_avg.get("ext_diff_f")
        if p10 is None or p90 is None:
            continue
        spread = p90 - p10
        # Latest kalshi in the hour = last update
        # Find modo bin (highest yes_mid among finite bins)
        finite_bins = [k for k in kalshi_bins if k["bin_hi"] < 200 and k["bin_lo"] > -50]
        if not finite_bins:
            continue
        # Latest per bin
        modo = max(finite_bins, key=lambda k: k["yes_mid"])
        modo_price = modo["yes_mid"]

        # Entry criterion
        crit1 = spread <= SPREAD_MAX
        crit2 = ext_diff is not None and abs(ext_diff) <= EXT_DIFF_MAX
        crit3 = modo_price < KALSHI_MODO_MAX
        entry = crit1 and crit2 and crit3

        # PnL if entered
        pnl = None
        settle = settles.get((sid, slot[0]))
        if entry and settle is not None:
            # Bet YES modo bin at modo_price
            # If settle within bin: win 1 - modo_price; else lose modo_price
            if modo["bin_lo"] <= settle <= modo["bin_hi"]:
                pnl = 1 - modo_price
            else:
                pnl = -modo_price

        results.append({
            "date": slot[0], "hour": slot[1],
            "spread": spread, "ext_diff": ext_diff,
            "modo_label": modo["label"], "modo_price": modo_price,
            "entry": entry, "pnl": pnl, "settle": settle,
        })
    return results


def main():
    print("=" * 70)
    print("5º CORTE — TIMING SWEET SPOT PER STATION")
    print("=" * 70)
    print(f"Criteria: spread ≤{SPREAD_MAX}°F AND |ext_diff|≤{EXT_DIFF_MAX}°F "
          f"AND kalshi_modo <{KALSHI_MODO_MAX}")
    print()

    conn = sqlite3.connect(str(DB_PATH))

    print(f"Fetching settles ({DAYS_BACK} days × {len(STATIONS)} stations)...")
    settles = get_settles(STATIONS, DAYS_BACK)
    print(f"  got {len(settles)} settle values")

    all_results = {}
    for sid in STATIONS:
        all_results[sid] = analyze_station(conn, sid, settles)
        n_entries = sum(1 for r in all_results[sid] if r["entry"])
        n_wpnl = sum(1 for r in all_results[sid] if r["pnl"] is not None)
        print(f"  {sid}: {len(all_results[sid])} slots, {n_entries} entries "
              f"({n_wpnl} with settle-based PnL)")

    print()
    print("=" * 70)
    print("ROI POR ESTACIÓN × HORA LOCAL (solo slots con entry criterion + settle)")
    print("=" * 70)

    for sid in STATIONS:
        # Group entries by hour, compute mean PnL
        by_hour = defaultdict(list)
        for r in all_results[sid]:
            if r["entry"] and r["pnl"] is not None:
                by_hour[r["hour"]].append(r["pnl"])
        if not by_hour:
            print(f"\n{sid}: 0 entries")
            continue
        print(f"\n{sid}:")
        print(f"  {'hour':>4} {'N':>3} {'ROI %':>7} {'win%':>6}")
        for hh in sorted(by_hour.keys()):
            pnls = by_hour[hh]
            roi = mean(pnls) * 100
            wins = sum(1 for p in pnls if p > 0) / len(pnls) * 100
            bar = '█' * int(roi + 20) if roi > -20 else '░'
            print(f"  {hh:>4} {len(pnls):>3} {roi:>+6.1f} {wins:>5.0f}%  {bar}")

    print()
    print("=" * 70)
    print("BEST HOUR PER STATION")
    print("=" * 70)
    for sid in STATIONS:
        by_hour = defaultdict(list)
        for r in all_results[sid]:
            if r["entry"] and r["pnl"] is not None:
                by_hour[r["hour"]].append(r["pnl"])
        if not by_hour:
            continue
        hour_roi = {hh: (mean(pnls), len(pnls)) for hh, pnls in by_hour.items()}
        best = max(hour_roi.items(), key=lambda x: x[1][0] if x[1][1] >= 3 else -999)
        if best[1][1] >= 3:
            print(f"  {sid}: best hour={best[0]:02d}h  ROI={best[1][0]*100:+.1f}%  (N={best[1][1]})")
        else:
            print(f"  {sid}: not enough entries (max N={max(x[1] for x in hour_roi.values())})")


if __name__ == "__main__":
    main()
