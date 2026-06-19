# predictor_web.py — Routes Summary

Flask app on :8000. Single file (~3800 lines) because each route is a single render-block. **Not included in this review folder** — request it if architecture review is needed.

## Routes (24)

### State / control
- `GET /` — main dashboard: modal bin, p10-p90 band, ext_diff, top bins, observed series
- `POST /api/station` — switch active station (`id=KPHX`)
- `POST /api/refresh` — re-fetch GFS ensemble + rebuild snapshot
- `POST /api/set`, `POST /api/clear` — manual overrides
- `GET /api/ping` — health check

### Analysis views
- `GET /ladder` — cumulative thresholds (≥X°F) with our_p vs kalshi_p
- `GET /edge` — per-bin edge table (our_p, kalshi_p, edge_pp, buy YES/NO)
- `GET /comparison` — model vs externals side-by-side
- `GET /calibration` — isotonic curve + Brier history
- `GET /timing` — peak hour modal + P(already happened)
- `GET /movement` — last 24h ensemble runs, U-turn detector
- `GET /cross` — D+0/D+1/D+2 forecast
- `GET /precip` — precipitation overlay
- `GET /status` — system diagnostics
- `GET /reweight` — Bayesian σ reweight inspection

### Bets
- `GET /bets` — settled + open positions, P&L, win rate, ROI
- `GET /history` — historical settles

### Export / misc
- `GET /export`, `GET /export/<table>.csv` — DB export
- `GET /notify`, `GET /alerts`, `GET /notify/test` — ntfy.sh push
- `GET /about`, `GET /tutorial.pdf`

## Common patterns

Each view:
1. Reads `state.last_snapshot` (built by `predictor.build_snapshot`)
2. Pulls market bins via `kalshi.fetch_bins`
3. Renders inline HTML (no template engine — single-file deploy choice)

The duplication is intentional: each route is self-contained for easy debugging. Refactor would consolidate the HTML scaffolding but not the analytical logic.
