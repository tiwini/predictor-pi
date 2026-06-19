# predictor_web.py — Routes Summary

Flask app on :8001. ~2800 lines. **Not included in this review folder** — the analytical logic is in `predictor.py`, `hourly_call.py`, `calibration.py`.

## Routes (13)

### Main
- `GET /` — dashboard: current σ_h, ladder, Kalshi edge, position view
- `GET /candles?symbol=BTCUSDT&limit=60` — JSON klines for chart
- `GET /history` — settled prediction outcomes
- `GET /calibration` — Brier + reliability diagram

### Hourly-call (the headline product)
- `GET /hourly-call` — current active call, last 30 calls, empirical hit rate vs 70% target, streak
- `GET /intra15` — intra-hour 15-min checkpoints

### API
- `GET /api/query?price=X` — P(price > X) at current σ_h
- `GET /api/whatif?sigma_mult=Y` — re-ladder under hypothetical σ
- `GET /api/intra15` — intra-hour state JSON

### Docs
- `GET /tutorial`, `GET /tutorial.pdf`, `GET /tutorial-btc`, `GET /tutorial-btc.pdf`

## Common pattern

Each render-block:
1. Calls `predictor.snapshot()` → σ_h + ladder
2. Calls `kalshi.fetch_bins()` → market strikes
3. Maps each strike to `1 - P(price > strike)` via predictor's t4 CDF
4. Computes edge_pp = our_p − kalshi_p, renders table

`hourly_call.py` runs separately (cron / loop) and writes `hourly_calls` table at XX:00 UTC.
