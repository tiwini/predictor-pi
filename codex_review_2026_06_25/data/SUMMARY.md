# KPHX P&L breakdown — pre-computed

## Aggregate (all stations)
| metric | value |
|---|---:|
| total settled bets | 655 |
| total stake | $6,550 |
| total pnl | (positive overall — KPHX is the only consistent loser) |

## KPHX standalone (165 settled bets)
| metric | value |
|---|---:|
| n | 165 |
| wins | 73 |
| WR | 44.2% |
| pnl | **−$166.89** |

### By side — the smoking gun
| side | n | wins | WR | pnl |
|---|---:|---:|---:|---:|
| **YES** | 66 | 5 | **7.6%** | **−$391.45** |
| NO | 99 | 68 | 68.7% | +$224.56 |

YES side wins 5/66 → much worse than chance, but the bets keep getting recommended (edge ≥ 5pp threshold). NO side is profitable and consistent. Net P&L hole = the YES side.

## KPHX bets by month
| month | n | wins | WR | pnl |
|---|---:|---:|---:|---:|
| 2026-04 | 54 | 27 | 50.0% | −$34.53 |
| 2026-05 | 76 | 33 | 43.4% | −$122.95 |
| 2026-06 | 35 | 13 | 37.1% | −$9.42 |

## ext_diff_at_entry coverage
`ext_diff_at_entry` was added late: NULL in 164/165 KPHX bets. Cannot per-bet correlate ext_diff with outcome. Only `day_summary.ext_diff_pre` is populated (12 KPHX days).

## P1 (Codex 2026-06-18) backtest done 2026-06-25
Codex hypothesis: clim_pct feedback loop (ensemble cold → clim_pct low → heat_under=False → no posterior shift bonus +0.15 → stays cold).

Backtest on N=7 KPHX days where ext_diff_pre<0:

| date | actual | pred | ext_med | clim@pred | clim@ext_med |
|---|---:|---:|---:|---:|---:|
| 06-11 | 108 | 105.1 | 106.5 | 59.3 | 71.3 |
| 06-13 | 110 | 105.8 | 106.4 | 64.2 | 67.6 |
| 06-14 | 110 | 106.6 | 107.5 | 67.1 | 75.1 |
| 06-15 | 108 | 107.0 | 107.4 | 70.7 | 72.7 |
| 06-18 | 108 | 105.0 | 105.4 | 48.0 | 52.2 |
| 06-19 | 107 | 104.3 | 106.6 | 38.9 | 58.4 |
| 06-24 | 107 | 107.0 | 108.9 | 51.8 | 69.1 |

- Loop confirmed: heat_under fired 0/7.
- Fix proposed by Codex (clim_pct on ext_med): would still be 0/7 fires. p80 KPHX Jun = 109.3°F; ext_med mean = 106.7°F.
- BOTH our model AND ext_med run cold vs actual (mean actual 108.3°F).
- 6/7 days actual > pred. mean |actual − pred| = 2.45°F; mean |actual − ext_med| = 1.87°F.

KPHX climatology Jun 11–24 (n=420 historical days):
- p10=97.7  p50=105.1  p70=107.5  p75=108.3  p80=109.3  p85=109.8  p90=111.0
- 108°F (most common actual) → p74

## P3 fix (Codex 2026-06-18) deployed 2026-06-25
- `external_models.anchor_weight(ext_diff, lam, ext_used)` and `posterior_shift_weight(..., ext_used)` and `blend_with_external(..., ext_used)` now accept an `ext_used` discount.
- Headroom now: `CAP − λ − max(0, ext_used)` for anchor; `CAP − max(0, ext_used)` for shift.
- `predictor.build_snapshot` computes `nudge_ext_used = |bias_correction_f| / |pre_ext_diff|` when sign_nudge fired AND `bias_correction_f * pre_ext_diff > 0` (nudge moved pred toward externals). Passes to posterior_shift_weight and stores in `ext_shift_info`.
- `predictor_web._anchor_context` reads `nudge_ext_used` from `ext_shift_info` and propagates to `blend_with_external` at both call sites.
- 152/152 tests pass (149 existing + 3 new in test_external_anchor.py).

## stations.py refactor deployed 2026-06-25
- 20 stations centralized in `stations.py` (StationConfig dataclass + derived views).
- Removed duplicated config from `predictor.py`, `kalshi.py`, `nws_cli.py`, `predictor_web.py`, `analysis_poller.py`.
- No behavior change, all tests pass.
