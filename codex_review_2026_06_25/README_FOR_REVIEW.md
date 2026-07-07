# Weather Predictor — Round 4 Review (2026-06-25)

> Continuation of the 2026-06-18 review. Your previous P0 + P2 applied and confirmed. P1 backtested with N=7 and shelved (your proposed fix doesn't apply — details in `data/SUMMARY.md`). P3 applied and deployed today; we want you to verify the math is now tight. And we have a NEW concrete bleeding wound (KPHX YES-side) that the previous review didn't address.

## What changed since Round 3 (2026-06-18)

| change | files | tests |
|---|---|---|
| **P3 fix** — shared budget `w + λ + nudge_ext_used ≤ CAP=0.5` | `external_models.py`, `predictor.py`, `predictor_web.py` | +3 in `test_external_anchor.py` |
| **stations.py refactor** — single source of truth for 20-station config | new `stations.py`; consumers updated | unchanged (149→152 incl. P3) |
| **Persistencia wind / pressure / dewpoint / today_min_obs** in `analysis.db.station_snapshots` | `analysis_poller.py` only | n/a (write-only telemetry) |

## The 2 questions

### Q1 — KPHX YES-side hemorrhage (NEW)
`data/kphx_bets.csv` (165 rows). YES side: **5 wins / 66 bets, WR 7.6%, P&L −$391.45**. NO side: 68/99, WR 68.7%, +$224.56.

The YES side is *much* worse than random (5/66 → binomial p ≈ 10⁻¹⁰ vs 50%). The bets are getting recommended by `bets.py` based on `edge_pp ≥ 5pp`. Our model is cold-biased on KPHX (mean |actual − pred| = 2.45°F over the last 7 cold days, see `data/SUMMARY.md`).

**The question is not "why is our model cold" (we already know).** It's: **why does the bet-selection layer keep recommending YES on bins our cold-biased model assigns highest probability to?** The structural insight from the data: we predict bin X with high `our_p`, market prices it low (so edge_pp positive on YES X), but actual lands in X+1 or X+2 because we systematically underpredict. Each YES bet on the "wrong" bin loses ~$8.

Where in `bets.py` / `predictor.py` should the YES-side guard live, and what signal should gate it? Options we've considered but not validated:
- (A) block YES side when `ext_diff_pre < −1.0` (steer toward NO-tail); ROI on the −$391 if it had been on all YES bets.
- (B) require both our model AND ext_med to agree on the modal bin before YES (currently only our_p is checked).
- (C) cap YES-side stake when the bias_tracker EWMA shows a cold streak ≥ 3 days for that station.
- (D) something we're missing.

Be concrete: file:line where the guard goes, what test would catch it, and whether you'd combine multiple gates.

### Q2 — P3 watertightness (verify the new fix)
We implemented your P3 suggestion from Round 3. Sign-nudge now consumes from the shared CAP=0.5 budget.

Key code (`external_models.py`):
```python
def anchor_weight(ext_diff, lam=0.0, ext_used=0.0):
    if ext_diff is None or abs(ext_diff) < ANCHOR_EXT_DIFF_THRESHOLD:
        return 0.0
    w = (abs(ext_diff) - ANCHOR_EXT_DIFF_THRESHOLD) * 0.15
    headroom = ANCHOR_WEIGHT_CAP - (lam or 0.0) - max(0.0, ext_used)
    return min(ANCHOR_WEIGHT_CAP, max(0.0, w), max(0.0, headroom))

def posterior_shift_weight(ext_diff, ext_spread, clim_percentile, ext_used=0.0):
    ...
    cap = POSTERIOR_SHIFT_CAP - max(0.0, ext_used)
    return min(max(0.0, cap), max(0.0, w))
```

Computation of `nudge_ext_used` in `predictor.build_snapshot`:
```python
nudge_ext_used = 0.0
if (bias_info is not None and bias_info.get("sign_nudge")
        and pre_ext_diff is not None and abs(pre_ext_diff) > 1e-9):
    if bias_correction_f * pre_ext_diff > 0:
        nudge_ext_used = min(_ext.POSTERIOR_SHIFT_CAP,
                             abs(bias_correction_f) / abs(pre_ext_diff))
```

**Is this watertight?** Specifically:
- The `bias_correction_f * pre_ext_diff > 0` sign test — is it always correct? (intuition: when bias is +1 and pre_ext_diff is +2, we subtract 1 from daily_maxes, pred drops, distance to ext_med shrinks → toward externals.)
- The fraction `|bias_correction_f| / |pre_ext_diff|` as λ-equivalent — does it under- or over-charge the budget when nudge and shift act in different bin geometries?
- Edge case: nudge AWAY from externals (`bias_correction_f * pre_ext_diff < 0`). We currently set `ext_used = 0` for this — is that right, or should we credit headroom (we already moved AWAY, so externals have MORE to do)?
- Counter-example welcome if the bound leaks. Confirmation also welcome.

## Files we want you to read

`code/`:
| file | lines | why |
|---|---:|---|
| `external_models.py` | 270 | P3 changes here — Q2 primary |
| `predictor.py` | 1380 | `build_snapshot` ext_shift block ~L720-780 — Q2 secondary |
| `bias_tracker.py` | 345 | unchanged since Round 3, included for context on sign_nudge semantics |
| `bets.py` | — | Q1 — where YES-side recommendation lives |
| `stations.py` | 60 | new, single source of truth |
| `kalshi.py` | — | MarketBin + our_p_for_bin (bin geometry) |

`tests/`:
- `test_external_anchor.py` — 12 tests, 3 new for ext_used budget accounting
- `test_bias_tracker.py` — for context on what sign_nudge does

`data/`:
- **`SUMMARY.md`** — read first. Pre-computed: KPHX YES/NO breakdown, P1 backtest table, climatology percentiles, P3 deployment confirmation.
- `kphx_bets.csv` — 165 rows. The −$391 wound.
- `kphx_day_summary.csv` — 51 rows. Day-level brier, ext_diff_pre, clim_pct, ext_shift_f.

## Anti-asks (skip these)

- P1 (clim_pct loop) — we backtested with N=7 and it's documented as shelved until N≥30 (Aug-Sep). Don't reopen.
- Style / type hints / docstring suggestions.
- "split predictor_web.py into modules" — we know, not now.
- Wind/pressure/dewpoint backtest — needs 2-4 weeks of collection, not ready.

## What we want back

For Q1: a concrete guard with file:line, expected impact on KPHX P&L (back-of-envelope OK), and whether the same guard makes sense for other stations or is KPHX-specific.

For Q2: pass/fail on the budget bound, with counter-example if fail. If pass, edge-case clarifications welcome.

User is a climate analyst, not a quant. Spanish fine. Brutally honest > polite.
