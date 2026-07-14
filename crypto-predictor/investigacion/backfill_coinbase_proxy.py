#!/usr/bin/env python3
"""Backfill retroactivo de proxy_price_at_settle usando Coinbase Pro
open del candle 1m en target_at.

- Solo toca rows con actual_price IS NOT NULL AND proxy_price_at_settle IS NULL.
- Commit por row (resumable ante Ctrl+C, kill, corte de red).
- 429: exponential backoff 2s/4s/8s; si al 3er intento sigue 429, se salta.
- sleep 0.35s entre requests -> ~2.9 req/s (safe vs limit publico 10 req/s).

Correr:
  cd ~/predictor-pi/crypto-predictor && ./venv/bin/python3 backfill_coinbase_proxy.py
"""
import signal
import sqlite3
import sys
import time

import requests

DB_PATH = "/home/popeye/crypto-predictor/calibration.db"
SLEEP_BETWEEN = 0.35
BACKOFFS = [2, 4, 8]
STOP = False


def _sigint(_s, _f):
    global STOP
    STOP = True
    print("\n[stop] Ctrl+C recibido — termina la row actual y sale.", flush=True)


signal.signal(signal.SIGINT, _sigint)
signal.signal(signal.SIGTERM, _sigint)


def coinbase_open_at(target_at: float) -> float | None:
    """Devuelve open Coinbase BTC-USD del candle 1m en target_at.
    None si el candle no existe / err definitivo tras retries."""
    for attempt, wait in enumerate([0] + BACKOFFS):
        if wait:
            time.sleep(wait)
        try:
            r = requests.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                params={"granularity": 60,
                        "start": int(target_at),
                        "end": int(target_at) + 60},
                timeout=10.0,
            )
        except requests.RequestException as e:
            print(f"  net err (retry {attempt}): {e}", flush=True)
            continue
        if r.status_code == 429:
            print(f"  429 rate-limit (retry {attempt}, waited {wait}s)", flush=True)
            continue
        if r.status_code != 200:
            print(f"  http {r.status_code}: {r.text[:80]}", flush=True)
            return None
        data = r.json()
        if not data:
            return None
        for k in data:
            if int(k[0]) == int(target_at):
                return float(k[3])
        return None
    return None


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, symbol, target_at FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NULL "
        "ORDER BY id"
    ).fetchall()
    total = len(rows)
    print(f"[backfill] {total} rows pendientes de proxy_price_at_settle")
    if total == 0:
        return
    ok = miss = err = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        if STOP:
            break
        if r["symbol"] != "BTCUSDT":
            print(f"[{i}/{total}] id={r['id']} symbol={r['symbol']} — skip (solo BTC)")
            continue
        px = coinbase_open_at(float(r["target_at"]))
        if px is None:
            miss += 1
            print(f"[{i}/{total}] id={r['id']} — no candle disponible")
        else:
            try:
                conn.execute(
                    "UPDATE hourly_calls SET proxy_price_at_settle=? WHERE id=?",
                    (px, r["id"]),
                )
                conn.commit()
                ok += 1
                if i % 50 == 0 or i == total:
                    rate = i / max(1.0, time.time() - t0)
                    eta = (total - i) / max(0.01, rate)
                    print(f"[{i}/{total}] id={r['id']} px={px:.2f}  "
                          f"ok={ok} miss={miss} err={err}  "
                          f"rate={rate:.2f}/s  eta={eta:.0f}s", flush=True)
            except sqlite3.Error as e:
                err += 1
                print(f"[{i}/{total}] id={r['id']} db err: {e}", flush=True)
        time.sleep(SLEEP_BETWEEN)
    conn.close()
    dt = time.time() - t0
    print(f"\n[done] procesadas {ok + miss + err}/{total}  "
          f"ok={ok} miss={miss} err={err}  tiempo={dt:.1f}s")
    if STOP:
        sys.exit(130)


if __name__ == "__main__":
    main()
