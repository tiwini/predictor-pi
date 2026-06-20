# Weather Predictor — Project Intro for Codex

> **This is an introduction**, not a bug hunt. We want you to understand the project well enough to form an opinion on its overall design, then tell us what stands out (good or bad). We are not asking you to fix anything specific.

## What this is

Educational Flask app that predicts the daily maximum temperature for 20 US airport stations and compares the implied probability of each Kalshi `KXHIGH*` bin against our model. **No real money** — Kalshi is read-only public API; the "bets" are simulated at $10 flat with auto-entry at |edge|≥5pp so we can measure model edge over time with discipline.

Settle source matches Kalshi's: NWS Climatological Reports (`nws_cli.py`). Predictions made in METAR + ensemble world; settled in the same data Kalshi liquidates against.

## Why this exists

Two reasons:

1. **Build intuition for prediction-market mechanics** — the user is a climate-data analyst, not a quant. The project is a deliberately educational scaffold for understanding how a probabilistic forecast interacts with a binary bin market.
2. **Daily decision support** — runs 24/7 on a Raspberry Pi 4B on the user's home network (Tailscale-accessible from phone). Each morning the user opens `/stations` to see "where today is most pronosticable" and decides where to focus attention.

## How it works (10-second pipeline)

```
Open-Meteo GFS ensemble (31 members)         ← code/predictor.py
        ↓
fetch hourly obs (METAR) + ensemble forecast
        ↓
Bayesian σ-reweight on residual hours        ← code/predictor.py:build_snapshot
        ↓
sample daily-max distribution per member
        ↓
bias tracker: EWMA + sign-nudge + regime-break attenuation   ← code/bias_tracker.py
        ↓
external models anchor (Open-Meteo 6 + NWS) ← code/external_models.py
   combined-ceiling guard: w + λ ≤ 0.5
        ↓
posterior shift toward externals + isotonic calibration (gated n≥20, days≥7)
        ↓
final per-bin probability vector
        ↓
        ┌────────────────────────┬─────────────────────────┐
        ↓                        ↓                         ↓
   Kalshi compare           difficulty score           auto-bet sim
   (kalshi.py)              (difficulty.py)            (bets.py)
        ↓                        ↓                         ↓
        └──── rendered to /, /stations, /cross, ... (predictor_web.py) ────┘
```

Polling cadence is adaptive: 3 min in peak window (per-station `PEAK_HOURS`), 10 min otherwise.

## What's in this folder

```
code/
├── predictor.py            (1393 LOC) — ensemble fetch, Bayesian reweight, build_snapshot
├── bias_tracker.py         (348)  — EWMA + sign-nudge + smoothstep attenuation
├── external_models.py      (245)  — Open-Meteo 6 models + NWS narrative; combined ceiling
├── kalshi.py               (488)  — KXHIGH market fetch, bin math, market_cache.db
├── nws_cli.py              (150)  — NWS Climatological Report parser for settle
├── calibration.py          (496)  — prediction_snapshots, day_outcomes, day_summary, settle_day
├── isotonic.py             (163)  — PAV calibration, applied when n≥20 ∧ days≥7
├── difficulty.py           (123)  — 0–100 score combining spread, eff_n, climatology, precip
├── divergence.py           (114)  — D+1/D+2 monotony-of-spread detector
├── multi_day.py            (71)   — day_forecast for /cross
├── peak_timing.py          (148)  — modal hour of peak from today's ensemble
├── peak_window.py          (112)  — NEW empirical p10/p50/p90 of peak hour from last 7 days
├── climatology.py          (129)  — 30-year archive cache via Open-Meteo
├── bets.py                 (305)  — simulator P&L, gates against bias streaks / divergence
├── overnight.py            (119)  — divergence gate vs midnight ensemble
└── routes_summary.md       — index of the 28 Flask routes (predictor_web.py excluded for size)

data/
└── SUMMARY.md              — live production numbers from the Pi as of 2026-06-19

tests/                      — 149 unit tests, 0 DB/network, ~0.9s
```

`predictor_web.py` is intentionally excluded — it's 4131 lines of Flask render-blocks and doesn't contain analytical logic worth reviewing. See `code/routes_summary.md` for the endpoint map.

## Recommended reading order

1. **`data/SUMMARY.md`** — current state (live numbers, 2026-06-19)
2. **`code/predictor.py:build_snapshot`** — the heart of the pipeline (around line 800; search the function name)
3. **`code/bias_tracker.py`** — most-iterated module; smoothstep attenuation in `_sign_nudge` and `compute_bias`
4. **`code/external_models.py`** — combined ceiling `w + λ ≤ 0.5`; anchor_weight clamping
5. **`code/peak_window.py`** — newest module (added 2026-06-19); empirical clock derivation
6. **`code/routes_summary.md`** — context for what the user sees daily

You don't have to read all the rest. The small modules (difficulty, divergence, climatology, multi_day, peak_timing, isotonic) are mostly self-explanatory once the snapshot pipeline is clear.

## Historical context (prior reviews)

Codex reviewed this project once before (2026-06-18, separate folder). That review focused on 4 specific findings (P0/P1/P2/P3). **This is not that review.** This is a fresh introduction asking for general impressions now that the project has matured: 13 stations added, daily clock widget, empirical 7-day window, dashboard, 24/7 Pi deployment.

Prior review's verdicts that still stand:
- P0 backfill bug — fixed (UPSERT with COALESCE in `calibration.py:244`)
- P2 sign-nudge — converted from 3-tier step to smoothstep `1 - (3x² - 2x³)` between NEAR=0.5°F and FAR=1.5°F
- P1 (KPHX feedback loop) — **deferred**; KPHX has since turned profitable (+$1,448, was −$156), so the feedback loop hypothesis may be moot
- P3 (Δ_nudge budget leak) — deferred; codex confirmed leak is theoretical with low practical ROI

## What we'd value in your response

Pick any of these (or none — surprise us):

- **Architectural smells**: is the snapshot pipeline composing the right way? Anything you'd refactor or simplify?
- **Math/stats**: any of the Bayesian or attenuation choices feel off given the goal?
- **Operational risk**: anything that would scare you about running this 24/7 unattended (DB growth, retry handling, error swallowing, etc.)?
- **What's missing**: features or instrumentation that would make this materially better with low effort?
- **What you'd cut**: anything that looks over-engineered for an educational tool?

Brevity is welcome. We don't need a 5-page document; 3–6 specific observations with file:line references is the ideal shape.
