# Live state — 2026-06-19 (pulled from production Pi)

## Headline

- **497 settled bets** across 7 stations (auto-bet at |edge|≥5pp, $10 flat)
- **52.3% win rate**, **+$7,897 net P&L** on $4,970 stake (**+158.9% ROI**, simulated)
- 152 day_summary rows over 2026-04-17 → 2026-06-18 (~63 days of operation)
- Educational, no real money — Kalshi prices read-only from public API

13 new stations were added 2026-06-19 (no settled bets yet). The numbers below are the 7 long-running stations.

## P&L per station (since 2026-04-20)

| station | n | stake | P&L | WR | ROI |
|---|---|---|---|---|---|
| KLGA (NYC=Central Park) | 113 | $1,130 | +$1,824 | 52.2% | 161% |
| KBOS | 72 | $720 | +$1,803 | 61.1% | 250% |
| KPHX | 164 | $1,640 | +$1,448 | 43.9% | 88% |
| KLAS | 70 | $700 | +$1,320 | 54.3% | 188% |
| KLAX | 45 | $450 | +$1,277 | 71.1% | 283% |
| KMDW | 16 | $160 | +$129 | 50.0% | 81% |
| KMIA | 17 | $170 | +$94 | 41.2% | 56% |

KPHX had been the lone loser in earlier reviews — now recovered as the bias tracker has absorbed the summer regime. WR still below 50% but +ROI from tail-negation bets (NO [≥X] when externals show heat but model sits lower).

## Brier 30d — us vs Kalshi (lower is better)

| station | n | ours | kalshi |
|---|---|---|---|
| KBOS | 6 | 0.1006 | 0.0676 |
| KLGA | 11 | 0.1154 | 0.0556 |
| KPHX | 25 | 0.1660 | 0.0596 |
| KLAS | 15 | 0.1734 | 0.0645 |
| KMDW | 8 | 0.1891 | 0.0919 |
| KLAX | 2 | 0.1992 | 0.0663 |
| KMIA | 5 | 0.2201 | 0.0657 |

**Kalshi's Brier is consistently lower** — the market integrates more information than our local model. The +ROI does NOT come from beating Kalshi calibration overall; it comes from cases where the market's tail is mispriced relative to our combined ensemble + bias + externals view (the "edge ≥5pp" gate).

## Coverage

- 20 stations supported (KPHX/KLAX/KLAS/KLGA/KBOS/KMIA/KMDW + 13 added 2026-06-19: KIAH/KSFO/KAUS/KDEN/KSAT/KDCA/KDFW/KPHL/KSEA/KATL/KMSY/KOKC/KMSP)
- Polling: 3 min in peak window, 10 min otherwise (PEAK_HOURS per station)
- Settle source: NWS Climatological Report (same source Kalshi liquidates against — `nws_cli.py`)
- 12,406 `prediction_snapshots` rows; auto-bet logged in `simulated_bets`
- 24/7 since 2026-06-19 on Raspberry Pi 4B 8GB

## What changed recently

| When | What |
|---|---|
| 2026-06-19 | 13 stations added · daily clock widget (PR + local time) · `peak_window.py` empirical p10/p50/p90 from last 7 days of Open-Meteo archive · `/stations` dashboard with 3-min cache + auto-prewarm · Pi migration with auto-start cron |
| 2026-06-18 | NWS CLI parser (replaced Open-Meteo settle source to match Kalshi) · BRTI proxy hardening (BTC sibling project) |
| 2026-05-08 | Kalshi swap (was Robinhood) · KLGA settles vs KNYC (Central Park) |
| 2026-04-29 | Bias tracker per-station (EWMA + sign-nudge) · external_models combined ceiling · divergence detector D+1/D+2 |
| 2026-04-21 | Difficulty score · cross-station ranking · isotonic calibration (gated) |
