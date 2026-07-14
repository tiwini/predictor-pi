#!/usr/bin/env python3
"""Backfill retroactivo de bitstamp_price_at_settle usando Bitstamp
/api/v2/ohlc/btcusd/ open del candle 1m en target_at.

Solo toca rows con actual_price NOT NULL y bitstamp_price_at_settle NULL.
Commit por row (resumable). 429: backoff 2/4/8. sleep 0.35s entre reqs.
"""
import signal
import sqlite3
import sys
import time

import requests

DB = "/home/popeye/crypto-predictor/calibration.db"
SLEEP = 0.35
BACKOFF = [2, 4, 8]
STOP = False


def _sig(*_):
    global STOP
    STOP = True
    print("\n[stop] Ctrl+C — sale tras row actual.", flush=True)


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def bitstamp_open_at(target_at: float) -> float | None:
    for attempt, wait in enumerate([0] + BACKOFF):
        if wait:
            time.sleep(wait)
        try:
            r = requests.get(
                "https://www.bitstamp.net/api/v2/ohlc/btcusd/",
                params={"step": 60, "limit": 1, "start": int(target_at)},
                timeout=10,
            )
        except requests.RequestException as e:
            print(f"  net err (retry {attempt}): {e}", flush=True)
            continue
        if r.status_code == 429:
            print(f"  429 (retry {attempt}, waited {wait}s)", flush=True)
            continue
        if r.status_code != 200:
            print(f"  http {r.status_code}: {r.text[:80]}", flush=True)
            return None
        try:
            data = r.json().get("data", {}).get("ohlc", [])
        except ValueError:
            return None
        if not data:
            return None
        c = data[0]
        try:
            if int(c["timestamp"]) != int(target_at):
                return None
            return float(c["open"])
        except (KeyError, ValueError):
            return None
    return None


def main():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, symbol, target_at FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND bitstamp_price_at_settle IS NULL "
        "ORDER BY id"
    ).fetchall()
    total = len(rows)
    print(f"[bitstamp backfill] {total} rows pendientes")
    if total == 0:
        return
    ok = miss = err = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        if STOP:
            break
        if r["symbol"] != "BTCUSDT":
            continue
        px = bitstamp_open_at(float(r["target_at"]))
        if px is None:
            miss += 1
            print(f"[{i}/{total}] id={r['id']} no candle", flush=True)
        else:
            try:
                conn.execute(
                    "UPDATE hourly_calls SET bitstamp_price_at_settle=? "
                    "WHERE id=?", (px, r["id"]))
                conn.commit()
                ok += 1
                if i % 50 == 0 or i == total:
                    rate = i / max(1.0, time.time() - t0)
                    eta = (total - i) / max(0.01, rate)
                    print(f"[{i}/{total}] id={r['id']} px={px:.2f}  "
                          f"ok={ok} miss={miss} err={err}  "
                          f"rate={rate:.2f}/s eta={eta:.0f}s", flush=True)
            except sqlite3.Error as e:
                err += 1
                print(f"[{i}/{total}] id={r['id']} db err: {e}", flush=True)
        time.sleep(SLEEP)
    conn.close()
    print(f"\n[done] ok={ok} miss={miss} err={err}  "
          f"tiempo={time.time()-t0:.1f}s")
    if STOP:
        sys.exit(130)


if __name__ == "__main__":
    main()
