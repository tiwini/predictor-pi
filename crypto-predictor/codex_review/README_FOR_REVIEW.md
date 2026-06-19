# Crypto Predictor (BTC) — Code & Design Review (v2)

> Educational tool (no real money). Predicts BTC 1-hour distribution from realized volatility, publishes a p70 "hourly call" each hour (BTC ≤ value at next hour close should hit ~70% of the time), compares implied bin probabilities against Kalshi BTC markets.

## TL;DR

- 24h of 1-min klines → EWMA(λ=0.97) σ_1m → σ_h = σ_1m · √60 → Student-t df=4
- 611 settled hourly calls, **70.0% empirical hit rate vs 70.0% target** (aggregate is excellent)
- **Average masks per-hour structure.** See `data/SUMMARY.md`:
  - Worst: 12-13 AST (50-54% WR) — driven by positive drift `E[r/σ] ≈ +0.30`
  - Best: 09 AST (80.8%), 14 AST (80.0%), 18 AST (84.6%) — negative drift
- v1 SUMMARY.md had a **timezone bug** (already detected and fixed). v2 numbers are correct.

## Read this first

1. **`data/SUMMARY.md`** (v2, timezone-fixed) — empirical breakdowns. Includes `E[r/σ_h]` per hour so the mean-bias vs σ-bias question is pre-decided by the data.
2. **`code/predictor.py`** (271 lines) — model core: EWMA σ + t4 distribution
3. **`code/hourly_call.py`** (223 lines) — p70 inversion + Kalshi mapping + record

`code/predictor_web.py` (2853 lines) excluded — see `code/routes_summary.md`.

## Findings already settled (from prior Codex round)

A previous Codex review confirmed these — included here so we don't ask twice:

- **Mean-bias > σ-bias** in the bad-hour bucket (13 AST): `E[r/σ] = +0.32`. Sigma-only fix needs ×1.98 multiplier; de-meaned needs only ×1.14. Conclusion: **add per-hour μ with shrinkage** (N≈25/hour is too thin without pooling).
- **df=4 is fine.** MLE on standardized residuals gives df ≈ 3.95, ΔLL = 0.003. Don't move it.
- **EWMA λ=0.97 tuning criterion (|z|>2 ratio) is heuristic, not MLE.** Better: maximize conditional log-likelihood under t4. Also: adaptive λ_t based on `|r²/σ² − 1|` reacts faster to variance change.
- **User memory "edge negativo siempre pierde" is FALSE as invariant.** Negative edge wins 68.4% of hourly calls. Statistical signal, not hard rule.
- **User memory "money hours 09 AST + 16-18 AST"** — partially confirmed. 09 AST = 80.8%, 17-18 AST = 76.9-84.6%. Real but exaggerated; not 88-100%.

## The 3 questions we want THIS Codex round to answer

### Q1 — Implementation of per-hour μ with shrinkage

Prior finding: bad-hour bucket has `E[r/σ] = +0.32` and a fix needs hierarchical pooling (N≈25/hour too thin).

**Propose the formulation.** Hierarchical pooling sketch:
- `μ_hour ~ Normal(μ_global, τ²)` prior
- Posterior `μ̂_hour = (N_h · r̄_h / σ² + μ_global / τ²) / (N_h / σ² + 1 / τ²)`
- How to pick `τ²` empirically (cross-validated MSE? empirical Bayes via marginal MLE?)
- Where in `predictor.py` does this hook? Show the diff at file:line level.

Bonus: should we also shrink toward day-of-week patterns, or is hour enough?

### Q2 — Adaptive λ_t formulation

Prior finding: heuristic `|z|>2` tuning is weak; regime-dependent λ_t is the right move. The sketch from prior round:

```
σ_t² = λ_t · σ_{t-1}² + (1 − λ_t) · r_{t-1}²
λ_t = λ_max − (λ_max − λ_min) · sigmoid(a + b · |r_{t-1}²/σ_{t-1}² − 1| + c · vol_pct_t)
```

**Make it concrete.** Specific (λ_min, λ_max, a, b, c) values from fitting? Or some calibration recipe Codex can outline. How to validate it didn't overfit (rolling backtest on `hourly_calls`)?

### Q3 — How would you re-validate after Q1+Q2 are in?

We're worried about p-hacking ourselves into a better-looking 70%. Once we add per-hour μ and adaptive λ_t, **what's the validation discipline**?
- Walk-forward split? Leave-one-month-out?
- What's the null hypothesis we're testing (calibration uniform across hours, day-of-week, σ regime)?
- Concrete Brier / log-loss thresholds we'd accept as "this is a real improvement, not noise"?

## What we changed recently

- EWMA λ retune 0.94 → 0.97
- Hourly call flow: publish p70 at each XX:00, settle previous hour
- Kalshi integration: edge_pp computed against current strike
- Backtest replays historical klines

## File index

### `code/`
| File | Lines | Purpose |
|---|---:|---|
| `predictor.py` | 271 | Model core: EWMA σ + t4 distribution + P(price > X) |
| `hourly_call.py` | 223 | p70 inversion + Kalshi mapping + record |
| `calibration.py` | 555 | SQLite schema, Brier, hourly_calls settle |
| `kalshi.py` | 123 | Kalshi BTC market fetch |
| `backtest.py` | 161 | Replay historical klines |
| `tune.py` | 146 | Re-fit λ from settled data |
| `routes_summary.md` | — | Replacement for excluded predictor_web.py |

### `tests/`
`test_predictor.py`, `test_hourly_call.py`, `test_calibration.py`

### `data/`
- **`SUMMARY.md`** (v2, timezone-fixed)
- `hourly_calls.csv` (611 rows, ISO timestamps appended)
- `predictions_sample.csv` (10k recent ladder snapshots with σ_h)

## How to run locally

```bash
cd crypto-predictor
./venv/bin/python3 -m pytest tests/
./venv/bin/python3 predictor_web.py 8001
```

Public APIs only: Binance, Kalshi.

## Feedback style

- Concrete file:line citations and code snippets for proposed diffs
- Numerics over prose: formulations, parameter values, threshold suggestions
- Skip: nits, file-splits, type hints. We know.
