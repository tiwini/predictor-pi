# Weather Predictor — Code & Design Review

> Educational tool (no real money). Predicts daily max temperature for 7 US airports and compares the bin distribution against Kalshi prediction markets (KXHIGH* series). Settles with NWS Climatological Reports.

## TL;DR

- Bayesian 31-member GFS ensemble → bias tracker (EWMA + sign-nudge + regime-break) → external Open-Meteo anchor → posterior shift λ → Kalshi bin probabilities
- **489 settled bets, 52.8% WR, +$2,963 net on $4,890 stake (+60.6% ROI)**
- Iterated through 3 review rounds with another assistant ("Fable"). Asking for fully independent second opinion.

## Read this first

1. **`data/SUMMARY.md`** — empirical breakdowns we pre-computed. Start here so your review is data-grounded, not vibes-based.
2. **`code/predictor.py`** (1380 lines) — the snapshot pipeline
3. **`code/bias_tracker.py`** (345 lines) — most-iterated module, has the new 3-tier sign-nudge attenuation
4. **`code/external_models.py`** (245 lines) — combined ceiling `w + λ ≤ 0.5`

`code/predictor_web.py` (3800 lines of Flask) intentionally excluded — see `code/routes_summary.md` for the endpoint map. If architecture review needed, ask.

## The 3 questions we actually want answered

### Q1 — Combined ceiling correctness
`external_models.anchor_weight` returns `min(CAP, max(0, w), max(0, headroom))` where `headroom = CAP − λ` and `CAP = 0.5`. The intent: model can never receive more than 50% external influence from anchor + posterior shift combined.

**Is this formulation watertight, or can sign-nudge sneak past it?** Sign-nudge is applied BEFORE the shift/blend in `predictor.build_snapshot`. If a +1°F nudge pushes the prediction one way and then the external anchor pulls it back, do we end up with effective external influence > 50%?

Show a counter-example if the bound leaks, or confirm it's tight.

### Q2 — Sign-nudge attenuation tier vs smooth
`bias_tracker._sign_nudge` returns nudge attenuated by `ext_diff`:
- `|ext_diff| < 0.5°F`: full ±1°F nudge
- `0.5 ≤ |ext_diff| < 1.5` and nudge AWAY from externals: 0.5× nudge
- `|ext_diff| ≥ 1.5` and AWAY: vetoed (0)
- TOWARD externals: full

**Is the 3-tier step function defensible, or should this be a smooth attenuation (e.g., `tanh(ext_diff)` modulation)?** The tiers came from Fable's review — we never validated empirically.

### Q3 — Why is KPHX losing money?
`data/SUMMARY.md` shows KPHX: 156 bets, 44.9% WR, **−$156 P&L**. The largest sample and the only consistent loser. Avg edge_pp = −2.9 (we mostly bet NO, against extreme heat). PHX has been hotter than our model on multiple heatwave days (11-jun NO [107-108] @ 78¢ lost).

**Trace the bias path for KPHX heatwave days and tell us where the under-prediction enters.** Possibilities: EWMA decay too aggressive, sign-nudge being vetoed by ext_diff exactly when we need it most, posterior shift λ capping at 0.5 when externals are 2-3°F higher, climatology percentile blunt at the upper tail.

Hint: also check why `data/SUMMARY.md` shows sign-nudge telemetry with 0 joined bets (sec "Sign-nudge telemetry"). Either the flag is never being written (`calibration.record_ext_signal`) or the join key (`station_id, date`) doesn't match.

## What we changed recently

- **Round 1**: isotonic recalibration (gated N≥20, ≥7d), divergence detector, external models card
- **Round 2**: `external_models.anchor_weight` refactored — removed `bias` double-counting, added `lam` param + combined ceiling
- **Round 3**: sign-nudge 3-tier attenuation + new telemetry columns in `daily_ext_signal` (`pred_pre_bias`, `sign_nudge_applied`, `nudge_f`, `streak_len`, `ewma_pre`, `bias_path`)

## File index

### `code/`
| File | Lines | Purpose |
|---|---:|---|
| `predictor.py` | 1380 | Snapshot pipeline (fetch → reweight → bias → shift → blend) |
| `bias_tracker.py` | 345 | EWMA + regime_break + sign_nudge attenuation |
| `external_models.py` | 245 | Open-Meteo fetch + anchor_weight + blend_with_external |
| `calibration.py` | 486 | SQLite schema, isotonic gate, settle from NWS CLI, telemetry write |
| `isotonic.py` | 163 | PAV regression (N≥20, ≥7d gate) |
| `divergence.py` | 114 | 24h forecast U-turn detector |
| `overnight.py` | 119 | Overnight divergence (anti-loss filter) |
| `climatology.py` | 129 | 30-year normals percentile |
| `kalshi.py` | 475 | MarketBin dataclass + Kalshi API |
| `nws_cli.py` | 137 | NWS Climatological Report parser |
| `bets.py` | — | $10 auto-bet sim when \|edge\| ≥ 5pp |
| `routes_summary.md` | — | Replacement for the excluded predictor_web.py |

### `tests/` (7 files, 149 tests total in upstream)
`test_bias_tracker.py`, `test_external_anchor.py`, `test_predictor.py`, `test_posterior_shift.py`, `test_divergence.py`, `test_isotonic.py`, `test_regime_trigger.py`

### `data/`
- **`SUMMARY.md`** — pre-computed empirical breakdowns (per-station, per-side, per-edge, per-price, sign-nudge telemetry). Read first.
- `simulated_bets.csv` (627 rows)
- `daily_ext_signal.csv` (40 rows, the new telemetry table)
- `day_summary.csv` (147 rows, brier per settled day)
- `day_outcomes.csv` (151 rows, settled max_obs_f)

## How to run locally

```bash
cd weather-predictor
./venv/bin/python3 -m pytest tests/          # 149 tests, ~0.7s, no DB/network
./venv/bin/python3 predictor_web.py            # Flask on :8000
```

Public APIs only: NOMADS (GFS), Open-Meteo, NWS, Kalshi.

## What kind of feedback we want

- **Be concrete**: file:line citations, counter-examples when claiming something is wrong
- **Math correctness > regime gaps > code structure > nits**
- **Skip**: type hints, docstring style, "you could split into modules" — we know
- **Don't be polite**: if the combined ceiling leaks, show a numeric example. If the tier function is bad, show what should replace it.

User is a climate data analyst (not a quant), fluent Spanish (respond either language is fine).
