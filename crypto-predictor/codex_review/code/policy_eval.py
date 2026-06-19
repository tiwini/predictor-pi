"""Policy-level walk-forward evaluation for BTC hourly calls.

This evaluates bet decisions, not density calibration.  The primary metric is
Sharpe of a simulated equity curve over settled hourly calls.  Policies can fit
parameters on prior folds, then decide side/size on the next fold.
"""
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, stdev
from typing import Iterable, Protocol

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "hourly_calls.csv"
SECONDS_PER_WEEK = 7 * 24 * 3600
MIN_TRAIN_WEEKS = 2
SHARPE_ACCEPT_DELTA = 0.30
TSTAT_ACCEPT = 2.0


@dataclass(frozen=True)
class HourlyCall:
    id: int
    made_at: float
    target_at: float
    now_price: float
    sigma_h: float
    kalshi_strike: float | None
    kalshi_no_at_strike: float | None
    model_no_at_strike: float | None
    edge_pp: float | None
    actual_price: float
    won: bool

    @property
    def hour_ast(self) -> int:
        return int((self.made_at - 4 * 3600) // 3600 % 24)


@dataclass(frozen=True)
class Decision:
    side: str  # "NO" or "YES"
    stake: float = 1.0


@dataclass(frozen=True)
class BetResult:
    call: HourlyCall
    decision: Decision
    pnl: float
    ret: float


@dataclass(frozen=True)
class FoldResult:
    fold: int
    train_n: int
    test_n: int
    bets: int
    pnl: float
    sharpe: float
    max_drawdown: float
    params: dict


class Policy(Protocol):
    name: str

    def fit(self, train: list[HourlyCall]) -> "Policy": ...

    def decide(self, call: HourlyCall) -> Decision | None: ...

    def params(self) -> dict: ...


class FixedEdgeThresholdPolicy:
    """Dummy policy: bet when abs(edge_pp) >= threshold.

    Positive edge means model NO probability exceeds Kalshi NO price, so bet NO.
    Negative edge means bet YES. This is only a framework smoke test, not a
    recommended production policy.
    """

    def __init__(self, threshold_pp: float, stake: float = 1.0):
        self.threshold_pp = float(threshold_pp)
        self.stake = float(stake)
        self.name = f"edge>={self.threshold_pp:g}pp"

    def fit(self, train: list[HourlyCall]) -> "FixedEdgeThresholdPolicy":
        return self

    def decide(self, call: HourlyCall) -> Decision | None:
        if call.edge_pp is None:
            return None
        if call.edge_pp >= self.threshold_pp:
            return Decision("NO", self.stake)
        if call.edge_pp <= -self.threshold_pp:
            return Decision("YES", self.stake)
        return None

    def params(self) -> dict:
        return {"threshold_pp": self.threshold_pp, "stake": self.stake}


def _to_float(value: str) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def load_hourly_calls(path: str | Path = DATA_PATH) -> list[HourlyCall]:
    calls: list[HourlyCall] = []
    with Path(path).open(newline="") as f:
        for row in csv.DictReader(f):
            actual = _to_float(row.get("actual_price"))
            won = row.get("won")
            if actual is None or won in (None, ""):
                continue
            calls.append(HourlyCall(
                id=int(row["id"]),
                made_at=float(row["made_at"]),
                target_at=float(row["target_at"]),
                now_price=float(row["now_price"]),
                sigma_h=float(row["sigma_h"]),
                kalshi_strike=_to_float(row.get("kalshi_strike")),
                kalshi_no_at_strike=_to_float(row.get("kalshi_no_at_strike")),
                model_no_at_strike=_to_float(row.get("model_no_at_strike")),
                edge_pp=_to_float(row.get("edge_pp")),
                actual_price=actual,
                won=bool(int(won)),
            ))
    return sorted(calls, key=lambda c: c.made_at)


def settle_decision(call: HourlyCall, decision: Decision) -> BetResult | None:
    if call.kalshi_strike is None or call.kalshi_no_at_strike is None:
        return None
    no_price = call.kalshi_no_at_strike
    if not (0.0 < no_price < 1.0):
        return None
    side = decision.side.upper()
    if side == "NO":
        price = no_price
        win = call.actual_price <= call.kalshi_strike
    elif side == "YES":
        price = 1.0 - no_price
        win = call.actual_price > call.kalshi_strike
    else:
        raise ValueError(f"unknown side: {decision.side}")
    if not (0.0 < price < 1.0):
        return None
    ret = (1.0 / price - 1.0) if win else -1.0
    pnl = decision.stake * ret
    return BetResult(call=call, decision=decision, pnl=pnl, ret=ret)


def simulate_policy(calls: Iterable[HourlyCall], policy: Policy) -> list[BetResult]:
    out: list[BetResult] = []
    for call in calls:
        decision = policy.decide(call)
        if decision is None or decision.stake <= 0:
            continue
        result = settle_decision(call, decision)
        if result is not None:
            out.append(result)
    return out


def sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    sd = stdev(returns)
    return 0.0 if sd == 0 else mean(returns) / sd * math.sqrt(len(returns))


def max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def summarize(results: list[BetResult]) -> dict:
    pnls = [r.pnl for r in results]
    rets = [r.ret for r in results]
    return {
        "bets": len(results),
        "pnl": sum(pnls),
        "avg_ret": mean(rets) if rets else 0.0,
        "sharpe": sharpe(rets),
        "max_drawdown": max_drawdown(pnls),
    }


def weekly_walk_forward(calls: list[HourlyCall], policy_factory,
                        min_train_weeks: int = MIN_TRAIN_WEEKS) -> list[FoldResult]:
    if not calls:
        return []
    start = calls[0].made_at
    end = calls[-1].made_at
    test_start = start + min_train_weeks * SECONDS_PER_WEEK
    folds: list[FoldResult] = []
    fold = 1
    while test_start < end:
        test_end = min(test_start + SECONDS_PER_WEEK, end + 1)
        train = [c for c in calls if c.made_at < test_start]
        test = [c for c in calls if test_start <= c.made_at < test_end]
        if train and test:
            policy = policy_factory().fit(train)
            results = simulate_policy(test, policy)
            stats = summarize(results)
            folds.append(FoldResult(
                fold=fold,
                train_n=len(train),
                test_n=len(test),
                bets=stats["bets"],
                pnl=stats["pnl"],
                sharpe=stats["sharpe"],
                max_drawdown=stats["max_drawdown"],
                params=policy.params(),
            ))
            fold += 1
        test_start = test_end
    return folds


def paired_t_stat(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    sd = stdev(values)
    return 0.0 if sd == 0 else mean(values) / (sd / math.sqrt(len(values)))


def compare_policies(calls: list[HourlyCall], baseline_factory,
                     candidate_factory) -> dict:
    base = weekly_walk_forward(calls, baseline_factory)
    cand = weekly_walk_forward(calls, candidate_factory)
    n = min(len(base), len(cand))
    deltas = [cand[i].sharpe - base[i].sharpe for i in range(n)]
    return {
        "baseline": base[:n],
        "candidate": cand[:n],
        "sharpe_deltas": deltas,
        "mean_delta_sharpe": mean(deltas) if deltas else 0.0,
        "t_stat": paired_t_stat(deltas),
        "accepted": bool(deltas)
        and mean(deltas) > SHARPE_ACCEPT_DELTA
        and paired_t_stat(deltas) > TSTAT_ACCEPT,
    }


def _print_folds(label: str, folds: list[FoldResult]) -> None:
    print(label)
    for f in folds:
        print(
            f"fold={f.fold} train={f.train_n} test={f.test_n} bets={f.bets} "
            f"pnl={f.pnl:+.2f} sharpe={f.sharpe:+.2f} dd={f.max_drawdown:+.2f} "
            f"params={f.params}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DATA_PATH))
    args = ap.parse_args()
    calls = load_hourly_calls(args.csv)
    report = compare_policies(
        calls,
        baseline_factory=lambda: FixedEdgeThresholdPolicy(0.0),
        candidate_factory=lambda: FixedEdgeThresholdPolicy(5.0),
    )
    _print_folds("baseline edge>=0pp", report["baseline"])
    _print_folds("candidate edge>=5pp", report["candidate"])
    print("deltas", [round(x, 3) for x in report["sharpe_deltas"]])
    print(
        f"mean_delta_sharpe={report['mean_delta_sharpe']:+.3f} "
        f"t_stat={report['t_stat']:+.2f} accepted={report['accepted']}"
    )


if __name__ == "__main__":
    main()
