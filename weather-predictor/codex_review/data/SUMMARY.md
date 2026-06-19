# Weather Predictor — Empirical Summary

Computed 2026-06-18 from `calibration.db` over all settled bets.

## Aggregate

- **N = 489** settled bets
- Win rate: **52.8%**
- P&L: **+$2,963.21** on **$4,890** stake
- ROI: **+60.6%**

## Per-station (sorted by sample size)

| Station | N | WR% | P&L | avg edge_pp |
|---|---:|---:|---:|---:|
| KPHX | 156 | 44.9 | **−$156.02** | −2.9 |
| KLGA | 113 | 52.2 | +$694.34 | −3.8 |
| KBOS | 72 | 61.1 | +$1,083.56 | −4.6 |
| KLAS | 70 | 54.3 | +$620.25 | +1.6 |
| KLAX | 45 | **71.1** | +$826.99 | +2.8 |
| KMIA | 17 | 41.2 | −$75.34 | −6.9 |
| KMDW | 16 | 50.0 | −$30.59 | −5.2 |

→ **KPHX, KMIA, KMDW are net losers.** KPHX has the largest sample (156) and worst loss. Cold-bias hypothesis holds: avg edge_pp is −2.9 meaning we mostly bet NO (favoring lower temps) and PHX disagrees.

## By side

| Side | N | WR% | P&L | avg edge_pp |
|---|---:|---:|---:|---:|
| NO | 294 | **77.6** | +$965.02 | −20.3 |
| YES | 195 | 15.4 | **+$1,998.19** | +24.6 |

→ **YES bets win 15% of the time but generate 67% of P&L** — these are the tail-negation bets (cheap YES on bins our model says are tail). When they hit they pay 5-10×.

## By |edge_pp| bucket

| Edge | N | WR% | P&L |
|---|---:|---:|---:|
| 5-10pp | 151 | 66.9 | +$57 |
| 10-20pp | 139 | 52.5 | +$153 |
| 20-30pp | 79 | 46.8 | +$1,193 |
| 30-50pp | 78 | 48.7 | +$1,418 |
| 50+pp | 42 | 21.4 | +$142 |

→ **20-50pp edges drive the bulk of profit** ($2,611 / $2,963 = 88%). The 50+pp bucket is mostly tail-bet territory (low WR, payout-driven).

## By entry_price bucket

| Price | N | WR% | P&L |
|---|---:|---:|---:|
| <10¢ | 127 | 11.0 | **+$2,596** |
| 10-25¢ | 48 | 20.8 | +$232 |
| 25-50¢ | 57 | 38.6 | +$1.51 |
| 50-75¢ | 97 | 69.1 | +$82 |
| 75¢+ | 160 | 90.6 | +$52 |

→ **Cheap tail bets (<10¢) generate 88% of the P&L** despite only 11% win rate. Mid-price bets (25-50¢) break even. Expensive favorites (75¢+) win frequently but contribute almost nothing.

## Sign-nudge telemetry (NEW)

| Path | N | WR% | P&L |
|---|---:|---:|---:|
| none | 26 | 61.5 | +$239 |
| (applied paths have N=0 in join) | — | — | — |

→ **Telemetry join is broken or sign-nudge has never been logged as applied since 40 daily_ext_signal rows exist.** Codex should investigate: either the `sign_nudge_applied` flag is not being written correctly in `calibration.record_ext_signal`, or `daily_ext_signal.date` does not align with `simulated_bets.date` format.

## Key questions for Codex

1. **Why is KPHX losing $156 over 156 bets?** Look at the cold-bias chain: bias_tracker EWMA + sign_nudge + posterior_shift. Are we under-shifting upward when PHX heatwave is in progress?
2. **The <10¢ bucket carries the system.** Are we systematically betting too small ($10 flat) on these tail bets given their EV?
3. **The 50+pp edge bucket is paradoxically the worst WR (21%) but still +$142.** Is this a tail-bet artifact, or is the edge metric over-confident there?
4. **Why does sign-nudge telemetry have 0 joined bets?** Schema bug or genuine no-application?
