"""Backtest histórico — simula predicciones pasadas y mete los outcomes
en la misma DB de calibración para validar el modelo sin esperar.

Para cada XX:00 UTC en los últimos N días:
- made_at = target_at - lead*60   (default lead=60min, igual que la app live)
- σ_1m = EWMA sobre las `lookback` klines 1m previas a made_at
- actual = open del candle 1m que empieza en target_at
- Idempotente: salta pares (symbol, target_at) ya presentes en DB.

Uso: venv/bin/python backtest.py --days 30
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import sys
import time

import requests

import calibration as cal
import predictor as p


def fetch_range(symbol: str, start_ms: int, end_ms: int) -> list[p.Kline]:
    """Pull klines 1m [start_ms, end_ms) en chunks de 1000."""
    out: list[p.Kline] = []
    cursor = start_ms
    while cursor < end_ms:
        r = requests.get(
            f"{p.BINANCE_BASE}/klines",
            params={"symbol": symbol, "interval": "1m",
                    "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for k in data:
            out.append(p.Kline(
                open_time=int(k[0]),
                open=float(k[1]), high=float(k[2]),
                low=float(k[3]), close=float(k[4]),
                volume=float(k[5]),
            ))
        last_t = int(data[-1][0])
        cursor = last_t + 60_000
        if len(data) < 1000:
            break
        time.sleep(0.1)  # rate-limit polite
    return out


def existing_targets(db_path: str, symbol: str,
                     start_ts: float, end_ts: float) -> set[float]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT DISTINCT target_at FROM predictions "
            "WHERE symbol=? AND target_at BETWEEN ? AND ?",
            (symbol, start_ts, end_ts),
        ).fetchall()
    return {r[0] for r in rows}


def backtest_symbol(symbol: str, days: int, lead_min: int,
                    lookback_min: int, db_path: str) -> int:
    now = time.time()
    end_ts = math.floor(now / 3600) * 3600   # último XX:00 ya cerrado
    start_ts = end_ts - days * 86400
    # Necesitamos klines desde antes de start_ts (lookback + lead) y hasta end_ts+1m
    pad = (lookback_min + lead_min + 5) * 60
    fetch_start_ms = int((start_ts - pad) * 1000)
    fetch_end_ms = int((end_ts + 120) * 1000)
    print(f"[{symbol}] fetching ~{(fetch_end_ms-fetch_start_ms)//60000} klines...",
          file=sys.stderr)
    klines = fetch_range(symbol, fetch_start_ms, fetch_end_ms)
    if len(klines) < lookback_min:
        print(f"[{symbol}] too few klines: {len(klines)}", file=sys.stderr)
        return 0

    by_t = {k.open_time: k for k in klines}
    ot_sorted = sorted(by_t.keys())
    closes = [by_t[t].close for t in ot_sorted]

    skip = existing_targets(db_path, symbol, start_ts, end_ts)
    added = 0
    t = start_ts
    with sqlite3.connect(db_path) as c:
        while t <= end_ts:
            target_ms = int(t * 1000)
            if t in skip or target_ms not in by_t:
                t += 3600
                continue
            made_at = t - lead_min * 60
            made_ms = int(made_at * 1000)
            idx = bisect.bisect_right(ot_sorted, made_ms) - 1
            if idx < lookback_min:
                t += 3600
                continue
            window = closes[idx - lookback_min + 1: idx + 1]
            rets = p.log_returns(window)
            sigma_1m = p.ewma_sigma(rets)
            if sigma_1m <= 0:
                t += 3600
                continue
            sigma_h = sigma_1m * math.sqrt(lead_min)
            now_price = window[-1]
            pred = p.Prediction(
                symbol=symbol, now_price=now_price,
                sigma_1m=sigma_1m, sigma_horizon=sigma_h,
                horizon_min=float(lead_min), fetched_at=made_at,
                n_candles=lookback_min, target_at=t,
                drift_z=p.hourly_drift_z(made_at),
            )
            ladder = p.threshold_ladder_abs(pred, n=10)
            actual_price = by_t[target_ms].open
            cur = c.execute(
                "INSERT INTO predictions(symbol, made_at, target_at, "
                "horizon_min, now_price, sigma_h, ladder_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (symbol, made_at, t, float(lead_min),
                 now_price, sigma_h, json.dumps(ladder)),
            )
            c.execute(
                "INSERT INTO outcomes(prediction_id, settled_at, actual_price) "
                "VALUES (?,?,?)",
                (cur.lastrowid, now, actual_price),
            )
            added += 1
            t += 3600
        c.commit()
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--lead", type=int, default=60,
                    help="min antes del cierre (lead de la pred)")
    ap.add_argument("--lookback", type=int, default=1440,
                    help="klines 1m para EWMA (default 1440 = 24h)")
    ap.add_argument("--symbols",
                    default="BTCUSDT,ETHUSDT,XRPUSDT,DOGEUSDT,SOLUSDT")
    ap.add_argument("--db", default=cal.DB_PATH)
    args = ap.parse_args()

    cal.init_db(args.db)
    total = 0
    for s in args.symbols.split(","):
        s = s.strip().upper()
        n = backtest_symbol(s, args.days, args.lead, args.lookback, args.db)
        print(f"[{s}] +{n} predictions")
        total += n
    print(f"\nTOTAL: +{total} predictions con outcomes")


if __name__ == "__main__":
    main()
