# predictor_web.py — Routes Summary

Flask app on :8000. Single file (~4100 lines) excluded from this folder because the analytical logic lives in the small modules in `code/`. Request the full file if architecture review is needed.

## Routes (28)

### State / control
- `GET /` — main page: hero (expected max), ensemble band, current obs, modal Kalshi bin, top bins, climate context, **daily clock widget**, signals
- `POST /api/station` — switch active station (`id=KPHX`)
- `POST /api/refresh` — re-fetch GFS ensemble + rebuild snapshot
- `POST /api/set`, `POST /api/clear` — manual overrides
- `GET /api/ping` — heartbeat

### Dashboards
- `GET /stations` — **NEW (2026-06-19)** — card grid of all 20 stations, sorted by forecastability (difficulty asc), one-tap navigation; results cached 3 min
- `GET /cross` — D+0/D+1/D+2 cross-station table with edge × stability ranking + bet/skip recommendation pill

### Analysis views
- `GET /ladder` — cumulative thresholds (≥X°F) with our_p vs kalshi_p
- `GET /edge` — per-bin edge table
- `GET /comparison` — model vs externals (GFS/ECMWF/ICON/MétéoFR/UKMO/GraphCast + NWS narrative)
- `GET /calibration` — isotonic curve + Brier history vs Kalshi
- `GET /timing` — peak hour modal + P(already happened) (today's ensemble)
- `GET /movement` — last 24h ensemble runs, U-turn detector
- `GET /precip` — precipitation overlay D+0/D+1/D+2
- `GET /status` — system diagnostics
- `GET /reweight` — Bayesian σ reweight inspection (bias mode/regime/n)

### Bets
- `GET /bets` — settled + open positions, P&L, win rate, ROI (auto-bet at |edge|≥5pp)
- `GET /history` — historical settles by day

### Export / misc
- `GET /export`, `GET /export/<table>.csv`
- `GET /notify`, `GET /alerts`, `GET /notify/test`
- `GET /about`, `GET /tutorial.pdf`

## The daily clock widget (new)

Bottom of `/`. Visualizes the day in 4 colored zones (pre-confidence, growing confidence, **decisive window** = empirical p10–p90 of peak hour from `peak_window.py`, post-peak), with a red marker at the modal expected peak and a white cursor at "now". Each timestamp is rendered in both station-local AND Puerto Rico time (operator is in PR).

The decisive zone collapses the question "if we don't hit X by hour Y, will we hit it today?" into a glance.

## Pre-warm + cache

`_warm_cross_cache()` runs after every poll, computes results for all 20 stations in parallel, and stores them in `_stations_cache` (TTL 3 min). Also pre-warms `peak_window.get()` for all 20 stations (TTL 24 h). Steady-state `/stations` hit is ~50 ms; first cold load is ~40 s.

## Common patterns
Each route reads `state.last_snapshot` (built by `predictor.build_snapshot`), pulls market bins via `kalshi.fetch_bins`, renders inline HTML (no template engine). Duplication intentional — each route is self-contained for debugging.
