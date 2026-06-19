"""Day-difficulty score for the high-temperature prediction task.

Combines four heuristics into a 0-100 score to tell the user when a day is
anomalous/unstable enough that prediction accuracy is likely to degrade:

    spread_score   — ensemble p90-p10 of daily max (°F); widens on fronts.
    effn_score     — Kish effective-N after Bayesian reweight; low values
                     mean morning residuals disagree strongly with model
                     members (regime shift).
    anomaly_score  — how far the forecast sits vs 30y climatology for the
                     same date (percentile). Extreme percentiles are harder.
    precip_score   — P(notable precipitation). Wet/transitional days move.

Each component is normalized to 0-100; the total is a max (worst-case) so a
single strong red flag dominates rather than being averaged away.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Difficulty:
    score: float                   # 0-100 overall
    label: str                     # "fácil" | "normal" | "difícil" | "muy difícil"
    recommend_skip: bool           # True if we suggest not betting today
    spread_f: Optional[float]
    eff_n: Optional[float]
    anomaly_pct: Optional[float]   # distance from p50 (0-50)
    precip_p: Optional[float]
    reasons: list[str]             # short human reasons for the score


def _spread_score(p10: Optional[float], p90: Optional[float]) -> Optional[float]:
    if p10 is None or p90 is None:
        return None
    s = max(0.0, p90 - p10)
    # 4°F feels normal; 12°F+ is very wide.
    return max(0.0, min(100.0, (s - 4.0) / 8.0 * 100.0))


def _effn_score(eff_n: Optional[float], total_members: int) -> Optional[float]:
    if eff_n is None or total_members <= 0:
        return None
    # eff_n = total → no info (uniform weights). eff_n small → regime shift.
    ratio = eff_n / total_members
    # ratio 1.0 → 0; ratio 0.2 → 100
    return max(0.0, min(100.0, (1.0 - ratio) / 0.8 * 100.0))


def _anomaly_score(percentile: Optional[float]) -> Optional[float]:
    if percentile is None:
        return None
    # percentile is 0-100 of temp vs historical; 50 is median (easy).
    # Extremes (<10 or >90) are harder.
    dist = abs(percentile - 50.0)  # 0-50
    # dist 15 → 0, dist 45 → 100
    return max(0.0, min(100.0, (dist - 15.0) / 30.0 * 100.0))


def _precip_score(p_notable: Optional[float]) -> Optional[float]:
    if p_notable is None:
        return None
    # p_notable 0 → 0, 0.6+ → 100
    return max(0.0, min(100.0, p_notable / 0.6 * 100.0))


def compute(*, ens_p10: Optional[float], ens_p90: Optional[float],
            eff_n: Optional[float], total_members: int,
            clim_percentile: Optional[float],
            p_notable_precip: Optional[float],
            regime_breaks: int = 0) -> Difficulty:
    s_spread = _spread_score(ens_p10, ens_p90)
    s_effn = _effn_score(eff_n, total_members)
    s_anom = _anomaly_score(clim_percentile)
    s_prec = _precip_score(p_notable_precip)

    parts = [x for x in (s_spread, s_effn, s_anom, s_prec) if x is not None]
    overall = max(parts) if parts else 0.0

    reasons: list[str] = []
    if s_spread is not None and s_spread >= 50:
        reasons.append(f"ensemble abierto ({(ens_p90 - ens_p10):.0f}°F p10-p90)")
    if s_effn is not None and s_effn >= 50:
        reasons.append(f"reweight colapsado (eff_N={eff_n:.1f}/{total_members})")
    if s_anom is not None and s_anom >= 50:
        reasons.append(f"anomalía climática (p{clim_percentile:.0f})")
    if s_prec is not None and s_prec >= 50:
        reasons.append(f"precipitación probable ({p_notable_precip * 100:.0f}%)")

    # Regime break overrides: if ≥2 past hours fell outside ensemble p1-p99,
    # the forecast is systematically wrong, not just noisy. Force max score.
    if regime_breaks >= 2:
        overall = 100.0
        reasons.insert(0, f"ruptura de régimen ({regime_breaks}h fuera de p1-p99)")

    if overall >= 75:
        label = "muy difícil"
        skip = True
    elif overall >= 55:
        label = "difícil"
        skip = True
    elif overall >= 30:
        label = "normal"
        skip = False
    else:
        label = "fácil"
        skip = False

    spread_f = (ens_p90 - ens_p10) if (ens_p10 is not None and ens_p90 is not None) else None
    anomaly_pct = abs(clim_percentile - 50.0) if clim_percentile is not None else None

    return Difficulty(
        score=overall,
        label=label,
        recommend_skip=skip,
        spread_f=spread_f,
        eff_n=eff_n,
        anomaly_pct=anomaly_pct,
        precip_p=p_notable_precip,
        reasons=reasons,
    )
