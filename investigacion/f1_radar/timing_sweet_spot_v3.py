#!/usr/bin/env python3
"""5º corte v3 — REDEFINIR trigger tras hallazgo KMIA hoy.

Hipótesis v3 (post-caso KMIA 07-24 15:16 EDT):
El edge no vive en el bin MODO Kalshi (el mercado ya lo pricea justo),
sino en el bin UNA POSICIÓN ENCIMA cuando nuestro pipeline calibrado ve
más upside que Kalshi todavía no ha digerido.

Trigger:
1. our_p_calibrated(bin_X) ≥ 2× kalshi_yes(bin_X)
2. bin_X = una posición ENCIMA del modo Kalshi actual
3. kalshi_yes(bin_X) ≤ 0.20  (precio bajo = payoff asimétrico 5:1+)
4. Cur ≥ max_obs (peak físico aún en curso)

Payoff: pagar ~14¢, ganar 86¢ si acierta.
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

RATIO_MIN = 2.0             # our_p_cal / kalshi_yes ≥ 2×
KAL_PRICE_MAX = 0.20        # kalshi ≤ 20¢
POST_WIN_CUTOFF_H = 1


def parse_ticker_date(ticker):
    try:
        dp = ticker.split("-")[1]
        yy = int(dp[:2]) + 2000
        mm = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}[dp[2:5]]
        return date(yy, mm, int(dp[5:]))
    except Exception:
        return None


def load_settles(conn) -> dict:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT station, date(ts) as d, MAX(today_max_obs) as max_obs
        FROM station_snapshots
        WHERE ts > datetime('now', '-25 days')
          AND today_max_obs IS NOT NULL AND today_max_obs > -900
        GROUP BY station, date(ts)
    """).fetchall()
    return {(r["station"], r["d"]): int(round(r["max_obs"])) for r in rows}


def analyze_station(conn, sid: str, settles: dict) -> list[dict]:
    tz = ZoneInfo(TZ_MAP[sid])
    peak_lo, peak_hi = PEAK_HOURS[sid]
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT ts, ticker, label, bin_lo, bin_hi, yes_mid, our_p_calibrated
        FROM kalshi_snapshots
        WHERE station = ? AND ts > datetime('now', '-21 days')
          AND yes_mid IS NOT NULL AND our_p_calibrated IS NOT NULL
        ORDER BY ts
    """, (sid,)).fetchall()

    # Group by (day_local, hour_local) with same-day ticker + pre-cutoff filter
    slots = defaultdict(list)
    for r in rows:
        tdate = parse_ticker_date(r["ticker"])
        if tdate is None:
            continue
        ts = datetime.fromisoformat(r["ts"])
        loc = ts.astimezone(tz)
        if tdate != loc.date():
            continue
        if loc.hour >= peak_hi + POST_WIN_CUTOFF_H:
            continue
        slot = (loc.date().isoformat(), loc.hour, loc.minute // 15)  # 15-min granularity
        slots[slot].append(dict(r))

    results = []
    for slot, bins in slots.items():
        # Latest per bin in the 15-min slot
        latest = {}
        for k in bins:
            key = (k["bin_lo"], k["bin_hi"])
            if key not in latest or k["ts"] > latest[key]["ts"]:
                latest[key] = k
        finite = [k for k in latest.values() if k["bin_hi"] < 200 and k["bin_lo"] > -50]
        if len(finite) < 2:
            continue
        # Ordenar por bin_lo ascending
        finite.sort(key=lambda x: x["bin_lo"])
        # Modo Kalshi (bin con más yes_mid entre finite)
        modo_idx = max(range(len(finite)), key=lambda i: finite[i]["yes_mid"])
        # Bin UNA POSICIÓN ENCIMA del modo
        target_idx = modo_idx + 1
        if target_idx >= len(finite):
            continue
        target = finite[target_idx]

        our_p = target["our_p_calibrated"] or 0
        kal = target["yes_mid"]
        if kal <= 0:
            continue
        ratio = our_p / kal

        # Trigger criteria
        crit_ratio = ratio >= RATIO_MIN
        crit_price = kal <= KAL_PRICE_MAX
        entry = crit_ratio and crit_price

        pnl = None
        settle = settles.get((sid, slot[0]))
        if entry and settle is not None:
            if target["bin_lo"] <= settle <= target["bin_hi"]:
                pnl = 1 - kal
            else:
                pnl = -kal

        results.append({
            "date": slot[0], "hour": slot[1], "q15": slot[2],
            "target_bin": target["label"],
            "our_p": our_p, "kal": kal, "ratio": ratio,
            "entry": entry, "pnl": pnl, "settle": settle,
        })
    return results


def main():
    print("=" * 70)
    print("5º CORTE v3 — TRIGGER: bin encima del modo con our_p/kal ≥ 2× y kal ≤ 20¢")
    print("=" * 70)
    conn = sqlite3.connect(str(DB_PATH))
    settles = load_settles(conn)
    print(f"Batch settles: {len(settles)}")

    all_results = {}
    for sid in STATIONS:
        all_results[sid] = analyze_station(conn, sid, settles)
        entries = [r for r in all_results[sid] if r["entry"]]
        w_pnl = [r for r in entries if r["pnl"] is not None]
        wins = [r for r in w_pnl if r["pnl"] > 0]
        total_pnl = sum(r["pnl"] for r in w_pnl)
        print(f"  {sid}: {len(all_results[sid])} slots, "
              f"{len(entries)} entries, {len(w_pnl)} scored, "
              f"{len(wins)} wins, cumulative_pnl={total_pnl:+.2f}")

    print()
    print("=" * 70)
    print("PER-STATION AGGREGATE")
    print("=" * 70)
    print(f"{'STA':6} {'entries':>8} {'wins':>5} {'win%':>6} {'ROI %':>8}")
    total_roi_all = []
    for sid in STATIONS:
        w_pnl = [r for r in all_results[sid] if r["entry"] and r["pnl"] is not None]
        if not w_pnl:
            continue
        wins = sum(1 for r in w_pnl if r["pnl"] > 0)
        n = len(w_pnl)
        roi = mean(r["pnl"] for r in w_pnl) * 100
        total_roi_all.extend(r["pnl"] for r in w_pnl)
        print(f"{sid:6} {n:>8} {wins:>5} {wins/n*100:>5.0f}% {roi:>+7.1f}")
    if total_roi_all:
        print(f"\n{'AGGREGATE':6} {len(total_roi_all):>8} "
              f"{sum(1 for p in total_roi_all if p>0):>5} "
              f"{sum(1 for p in total_roi_all if p>0)/len(total_roi_all)*100:>5.0f}% "
              f"{mean(total_roi_all)*100:>+7.1f}")

    print()
    print("=" * 70)
    print("TOP WINNERS (per-station, ROI > 0)")
    print("=" * 70)
    for sid in STATIONS:
        winners = [r for r in all_results[sid]
                   if r["entry"] and r["pnl"] is not None and r["pnl"] > 0]
        if not winners:
            continue
        print(f"\n{sid}:")
        for w in sorted(winners, key=lambda x: -x["pnl"])[:5]:
            print(f"  {w['date']} {w['hour']:02d}:{w['q15']*15:02d}Q  "
                  f"{w['target_bin']:15}  kal={w['kal']:.2f} our={w['our_p']:.2f}  "
                  f"ratio={w['ratio']:.1f}× pnl={w['pnl']:+.2f}  settle={w['settle']}")


if __name__ == "__main__":
    main()
