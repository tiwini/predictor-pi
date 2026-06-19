# BTC Predictor — Empirical Summary (v2, timezone-fixed)

Computed 2026-06-18 from `calibration.db` over all settled hourly calls.

> **v1 had a timezone bug** (double offset; my server is already in AST so subtracting 4 was wrong). v2 numbers below are the corrected ones. Codex caught this — credit there.

## Aggregate calibration

- **N = 611** settled hourly calls
- Empirical WR: **70.0%**
- Target: 70.0%
- Aggregate is excellent. Per-hour buckets below show structure the aggregate hides.

## Hour-of-day (AST, correct)

| Hour | N | WR% | Δ vs 70 | E[r/σ_h] |
|---:|---:|---:|---:|---:|
| 00 | 25 | 72.0 | +2.0 | −0.02 |
| 01 | 25 | 76.0 | +6.0 | −0.09 |
| 02 | 25 | 68.0 | −2.0 | −0.17 |
| 03 | 25 | 68.0 | −2.0 | −0.38 |
| 04 | 25 | 76.0 | +6.0 | +0.01 |
| 05 | 25 | 68.0 | −2.0 | −0.04 |
| 06 | 25 | 72.0 | +2.0 | −0.22 |
| 07 | 25 | 68.0 | −2.0 | −0.14 |
| 08 | 25 | 56.0 | −14.0 | **+0.22** |
| **09** | 26 | **80.8** | +10.8 | −0.77 |
| 10 | 26 | 73.1 | +3.1 | −0.12 |
| 11 | 26 | 65.4 | −4.6 | +0.10 |
| **12** | 26 | **53.8** | −16.2 | **+0.31** |
| **13** | 26 | **50.0** | −20.0 | **+0.32** |
| **14** | 25 | **80.0** | +10.0 | −0.21 |
| 15 | 25 | 64.0 | −6.0 | +0.15 |
| 16 | 25 | 72.0 | +2.0 | +0.09 |
| **17** | 26 | 76.9 | +6.9 | −0.63 |
| **18** | 26 | **84.6** | +14.6 | −0.09 |
| 19 | 26 | 76.9 | +6.9 | −0.18 |
| 20 | 27 | 63.0 | −7.0 | +0.05 |
| 21 | 26 | 76.9 | +6.9 | −0.20 |
| 22 | 25 | 64.0 | −6.0 | +0.11 |
| 23 | 25 | 76.0 | +6.0 | −0.45 |

### Key findings (corrected)

- **Worst hours: 12 AST (53.8%) and 13 AST (50.0%).** Both show positive drift `E[r/σ_h] ≈ +0.30-0.32`, meaning price systematically rises in those hours. The model assumes mean-zero, so the p70 call is too low → BTC exceeds it more often than 30% target.
- **Best hours: 09 AST (80.8%), 14 AST (80.0%), 18 AST (84.6%).** Negative `E[r/σ_h]` (prices tend to drift down) → model conservative on the upper tail → wins more often.
- **User memory "money hours 09 AST + 16-18 AST WR 88-100%"** — partially confirmed. 09 AST = 80.8%, 17-18 AST = 76.9-84.6%. Real but exaggerated; not 88-100%.

### What this tells us (per Codex)

The 13 AST collapse is **mean-bias first, σ-bias second**:
- `E[r/σ] = +0.32` (drift)
- Avg gross return ≈ +10.9 bp/h
- Sigma-only fix needs ×1.98 multiplier (absorbs drift)
- De-meaned fix needs only ×1.14 multiplier

Conclusion: **add per-hour μ with shrinkage** (N≈25 per hour is too thin without pooling). Don't deploy per-hour σ multipliers blindly.

## Edge sign (memory check)

| Edge | N | WR% |
|---|---:|---:|
| negative | 446 | **68.4** |
| positive | 136 | 76.5 |

→ **User memory "edge negativo siempre pierde" is FALSE as invariant.** Edge negativo wins 68.4% of the time. The signal is statistical (positive edge does better by 8pp), not a hard rule.

## σ_h regime

| Bucket | N | WR% |
|---|---:|---:|
| low | 152 | 69.1 |
| mid-lo | 153 | 66.0 |
| **mid-hi** | 153 | **76.5** |
| high | 153 | 68.6 |

→ Codex insight: mid-hi WR 76.5% means **model TOO conservative** (σ too wide). Mid-lo 66.0% means σ too narrow. Not a "raise λ in high vol" rule. The right move is **adaptive λ_t** that reacts to variance change (`|r²/σ² − 1|`), not just level.

## What changed since v1

| Topic | v1 (wrong) | v2 (correct) |
|---|---|---|
| 09 AST WR | 48% (refuted memory) | 80.8% (confirms memory) |
| 14 AST WR | 84.6% (was 18 AST) | 80.0% |
| Worst hour | "09 AST" | 13 AST (50%) |
| Best hour | "14 AST" | 18 AST (84.6%) |
| Headline finding | "hour 9 collapse" | Hours 12-13 collapse with positive drift |
| Edge rule | "memory contradicted" | edge neg WR 68.4% — statistical signal not invariant |
