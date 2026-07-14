#!/usr/bin/env python3
"""R6 request #2 de Fable — probe de venues BRTI antes del backfill:

Per Fable:
  - Bitstamp: /api/v2/ohlc/ acepta start/end, 1000/req -> historia viable.
  - Kraken: OHLC público solo ~720 candles a 1m = 12h -> NO viable retro.
  - Gemini: /v2/candles/BTCUSD/1m recent-only, verificar.

Probamos cada venue con el candle 1m del row mas viejo del DB (~2026-05-08)
y con un row reciente (~2026-07-08) como sanity check.
"""
import json
import sqlite3
import sys
import time

import requests

DB = "/home/popeye/crypto-predictor/calibration.db"


def get_probe_targets():
    conn = sqlite3.connect(DB)
    r_old = conn.execute(
        "SELECT id, target_at FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NOT NULL "
        "ORDER BY target_at ASC LIMIT 1"
    ).fetchone()
    r_new = conn.execute(
        "SELECT id, target_at FROM hourly_calls "
        "WHERE actual_price IS NOT NULL AND proxy_price_at_settle IS NOT NULL "
        "ORDER BY target_at DESC LIMIT 1"
    ).fetchone()
    return {"old": {"id": r_old[0], "target_at": int(r_old[1])},
            "new": {"id": r_new[0], "target_at": int(r_new[1])}}


def probe_bitstamp(ts):
    """/api/v2/ohlc/btcusd/  con start/end en unix seconds, step=60"""
    url = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
    params = {"step": 60, "limit": 1, "start": ts}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        j = r.json()
        # respuesta: {"data": {"pair":"BTC/USD","ohlc":[{...}]}}
        ohlc = j.get("data", {}).get("ohlc", [])
        if not ohlc:
            return {"ok": False, "reason": "empty ohlc", "raw": str(j)[:200]}
        c = ohlc[0]
        # candle format: open, close, high, low, volume, timestamp (all strings)
        cts = int(c["timestamp"])
        if cts != ts:
            return {"ok": False, "reason": f"ts mismatch got={cts} want={ts}",
                    "open": float(c["open"])}
        return {"ok": True, "open": float(c["open"]),
                "close": float(c["close"]), "ts": cts}
    except Exception as e:
        return {"ok": False, "reason": f"exception: {e}"}


def probe_kraken(ts):
    """/0/public/OHLC?pair=XBTUSD&interval=1&since=ts-60"""
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "XBTUSD", "interval": 1, "since": ts - 60}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            return {"ok": False, "reason": f"kraken err: {j['error']}"}
        data = j.get("result", {})
        # data: {"XXBTZUSD":[[ts,open,high,low,close,vwap,vol,count],...], "last": ts}
        candles = None
        for k, v in data.items():
            if k != "last" and isinstance(v, list):
                candles = v
                break
        if not candles:
            return {"ok": False, "reason": "no candles in result"}
        # buscar candle con ts exacto
        exact = [c for c in candles if int(c[0]) == ts]
        first_ts = int(candles[0][0])
        last_ts = int(candles[-1][0])
        if exact:
            return {"ok": True, "open": float(exact[0][1]),
                    "close": float(exact[0][4]), "ts": ts,
                    "series_range": [first_ts, last_ts, len(candles)]}
        return {"ok": False, "reason": f"target ts {ts} not in returned series",
                "series_range": [first_ts, last_ts, len(candles)],
                "series_span_hours": (last_ts - first_ts) / 3600.0}
    except Exception as e:
        return {"ok": False, "reason": f"exception: {e}"}


def probe_gemini(ts):
    """/v2/candles/BTCUSD/1m — no acepta rango, devuelve recent ~500 candles.
    Chequea si ts esta en el rango devuelto."""
    url = "https://api.gemini.com/v2/candles/BTCUSD/1m"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        candles = r.json()
        # format: [[ts_ms, open, high, low, close, volume], ...] desc
        if not candles:
            return {"ok": False, "reason": "empty response"}
        ts_ms = ts * 1000
        exact = [c for c in candles if int(c[0]) == ts_ms]
        first_ts = int(candles[0][0]) // 1000
        last_ts = int(candles[-1][0]) // 1000
        if exact:
            return {"ok": True, "open": float(exact[0][1]),
                    "close": float(exact[0][4]), "ts": ts,
                    "series_range": [first_ts, last_ts, len(candles)]}
        return {"ok": False, "reason": f"target ts {ts} not in recent window",
                "series_range": [first_ts, last_ts, len(candles)],
                "series_span_hours": abs(last_ts - first_ts) / 3600.0}
    except Exception as e:
        return {"ok": False, "reason": f"exception: {e}"}


def main():
    targets = get_probe_targets()
    print("=" * 78)
    print("Probes")
    print("=" * 78)
    for tag, tgt in targets.items():
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(tgt["target_at"], tz=timezone.utc)
        print(f"\n[{tag}] row_id={tgt['id']}  target_at={tgt['target_at']} "
              f"({dt.isoformat()})")
        for vname, fn in [("bitstamp", probe_bitstamp),
                          ("kraken", probe_kraken),
                          ("gemini", probe_gemini)]:
            res = fn(tgt["target_at"])
            status = "✓ OK" if res.get("ok") else "✗ NO"
            print(f"  {vname:>10}: {status}")
            for k, v in res.items():
                if k == "ok":
                    continue
                if k == "series_range" and isinstance(v, list):
                    from datetime import datetime as dt2
                    lo = dt2.fromtimestamp(v[0], tz=timezone.utc).isoformat()
                    hi = dt2.fromtimestamp(v[1], tz=timezone.utc).isoformat()
                    print(f"      {k}: [{lo}, {hi}], n={v[2]}")
                else:
                    print(f"      {k}: {v}")
            time.sleep(0.5)

    print()
    print("=" * 78)
    print("Verdict resumido:")
    print("=" * 78)
    print("(mira los OK/NO de arriba; si bitstamp OK en 'old' y 'new', historia")
    print("completa viable con Bitstamp+Coinbase; kraken/gemini a forward-only.)")


if __name__ == "__main__":
    main()
