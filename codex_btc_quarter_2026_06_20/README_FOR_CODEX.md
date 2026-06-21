# BTC quarter-streak feature — Codex design review 2026-06-20

> **This is a small new feature, not a bug-hunt.** Shipped today, ~0 production data yet. We want your judgment on **5 specific design choices** before we let it run for weeks and accumulate biased data.

## What the feature does

Every 15 minutes (at xx:00 / xx:15 / xx:30 / xx:45 UTC), a background poller:

1. Hits `/api/quarter-signal` on the BTC predictor (port 8001) and captures:
   - `price` (Binance BTCUSDT last)
   - `tension_score` (6-signal aggregate, range `[-5, +5]`)
   - `p_above_next` (model's P(close ≥ now_price) at the next quarter close)
2. Picks a **side**:
   - `tension_score > +0.1` → **UP**
   - `tension_score < -0.1` → **DOWN**
   - `|tension_score| ≤ 0.1` → **FLAT** (no bet)
3. Inserts a row in `btc_quarter.db` with `won = NULL` and `price_out = NULL`.
4. Sleeps 15 minutes, hits the same endpoint, captures the new price.
5. Sets `won = 1` if:
   - side == UP and `price_out ≥ price_in`
   - side == DOWN and `price_out < price_in`
6. Updates `streak_after` (consecutive wins counting back from this row, ignoring FLAT/NULL).

The dashboard (port 8080) just renders SQLite stats: current streak, best streak, win-rate, last 15 rows, and any pending row (with countdown).

**No real money.** This is the same "intuition-building" toy bracket as the rest of the project. We just want a fun "is tension a useful signal at 15 min" tracker on the user's phone.

## Files in this package

- `code/btc_quarter_poller.py` — the full poller (~186 lines, the new file)
- `code/predictor_web_excerpts.py` — the new `/api/quarter-signal` endpoint + the pre-existing `_build_intra15` and `_compute_tension` it leans on
- `code/dashboard_btc_quarter_route.py` — the new `/btc-quarter` route (read-only)

## The 5 questions we want Codex to weigh in on

### Q1 — Horizon mismatch

`_compute_tension` aggregates signals that are mostly **slow**:

| Signal | Refresh | Native horizon |
|---|---|---|
| OB top20 imbalance | ~live | seconds |
| Taker flow | 5-min window | ~5 min |
| Funding rate | 8h epoch | hours-days |
| Fear & Greed | daily | days |
| Binance vs BRTI basis | live | live |
| +60min momentum band | recomputed per fetch | 60 min |

Two of six components (funding, F&G) update once per day-ish, and the heaviest weighted one (+60min momentum, max ±1.5) is explicitly a **1-hour** signal.

**Is using this composite for 15-min direction predictions a meaningful experiment, or just measuring "BTC bull-vs-bear regime" with extra noise?** If the latter, what would Codex actually want as the 15-min signal — drop to OB + taker flow only? Use `p_above_next` from intra15 with threshold 0.5? Something else entirely?

### Q2 — FLAT threshold

I gate FLAT at `|tension_score| < 0.1`. With a `[-5, +5]` range and 6 components, that's basically "always non-flat". The predictor's own labels are:

- `|score| ≥ 1.5` → bullish / bearish
- `|score| ≥ 0.5` → lean bull / lean bear
- `|score| < 0.5` → neutral

**Should FLAT cover `|score| < 0.5` (only bet when tension picks a lean), or even `|score| < 1.5` (only bet on conviction)? Or is "always bet" right because we want sample size and we'll stratify post-hoc?**

The risk of moving the threshold up: most quarters become FLAT and we get ~no data per day.
The risk of leaving it at 0.1: every row is a coin-flip-noise bet and we never see signal even if it's there.

### Q3 — Settle price source

I capture `price_out` from Binance `now_price` (same field as `price_in`). Kalshi's 15-min BTC markets settle on **CFB BRTI** (Coinbase ref index), not Binance.

The endpoint also exposes `brti_mid` via `_external["brti_mid"]` (see how `/api/intra15` already does the basis adjustment).

**Should we settle this tracker against BRTI mid for honesty (what Kalshi would have settled to), even at the cost of slightly noisier reads when BRTI is stale? Or keep Binance for self-consistency with the signal source?**

Memory note for context: the project has a memory entry `crypto_kalshi_settles_cfb.md` flagging this drift as ~2 bps typical.

### Q4 — Tie-breaking

UP wins on `price_out >= price_in`; DOWN wins on `price_out < price_in`. Strict equality goes to UP.

15-min BTC moves with tick precision ~$0.01 — exact equality should be near-zero probability, but if Binance's last-trade caching ever returns the same value twice, **UP gets a free win**.

**Is this bias worth caring about? Should ties be FLAT instead?**

### Q5 — Selection bias in win-rate

If BTC is in a bull market for the audit window, `tension_score > 0` will fire ~70% of the time AND `price_out ≥ price_in` will be true ~55% of the time independently. Naive win-rate will look great but won't reflect predictor quality.

**What's the right "honest" metric to surface in the dashboard?** Some options:

- Win-rate **stratified by side** (separate UP win-rate and DOWN win-rate)
- Win-rate vs **regime baseline** (e.g., 1-day rolling % of 15-min candles where close ≥ open, as the baseline for UP)
- Win-rate **conditional on |score| ≥ X** (filter to higher-conviction calls)
- Just expose all three and let the user pick

Currently the dashboard shows only the aggregate `wins / settled`. Codex's preference?

## Constraints / context to hold

- User is a climate-data analyst, not a quant. Build intuition, not edge.
- This runs on a Raspberry Pi alongside ~5 other services. Compute and disk are abundant (101 GB free, 4 cores idle most of the day).
- The `_compute_tension` function and `_build_intra15` are **frozen** — we don't want to re-tune them as part of this review (prior Codex rounds already settled the math). If a question can only be answered by changing them, say so and we'll spec a follow-up.
- Schema is new (one table, `quarter_predictions`); easy to ALTER if needed.
- The poller writes one row every 15 minutes. ~35k rows/year — DB growth is irrelevant.

## What we want back

For each of Q1–Q5: a 2-4 sentence judgment with one **concrete recommendation**, severity-tagged:

- `[CHANGE BEFORE FIRST DAY]` — broken or biased enough that running it for a week would be worthless
- `[CHANGE BEFORE FIRST WEEK]` — fine to start collecting data, but should be fixed before drawing conclusions
- `[LEAVE]` — current choice is defensible; here's why

If you see something we didn't ask about (e.g., a race in the poller, a SQL injection vector, a missing index), drop a 6th point at the end. Don't pad if there's nothing.

## Out of scope

- Tuning EWMA λ, Student-t df, or anything in `_compute_tension` weights
- Whether the dashboard's HTML is pretty
- Whether the feature is worth building (it's already built; the user enjoys it)
