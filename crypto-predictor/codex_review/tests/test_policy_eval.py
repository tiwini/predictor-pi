import csv
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1] / "code"
if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))

import policy_eval as pe


HEADER = [
    "id", "symbol", "made_at", "target_at", "now_price", "sigma_h",
    "quantile", "call_value", "kalshi_strike", "kalshi_no_at_strike",
    "kalshi_no_at_call", "model_no_at_strike", "edge_pp", "actual_price",
    "won", "settled_at", "made_at_iso", "target_at_iso", "settled_at_iso",
]


def _write_csv(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for row in rows:
            base = {k: "" for k in HEADER}
            base.update(row)
            w.writerow(base)


def _row(i, week, edge_pp, actual_price, strike=100.0, no_price=0.50):
    made = 1_800_000_000 + week * pe.SECONDS_PER_WEEK + i * 3600
    return {
        "id": i + week * 100,
        "symbol": "BTCUSDT",
        "made_at": made,
        "target_at": made + 3600,
        "now_price": 100.0,
        "sigma_h": 0.01,
        "quantile": 0.70,
        "call_value": 101.0,
        "kalshi_strike": strike,
        "kalshi_no_at_strike": no_price,
        "model_no_at_strike": no_price + edge_pp / 100.0,
        "edge_pp": edge_pp,
        "actual_price": actual_price,
        "won": 1 if actual_price <= 101.0 else 0,
        "settled_at": made + 3610,
    }


def test_load_hourly_calls_skips_unsettled(tmp_path):
    path = tmp_path / "calls.csv"
    _write_csv(path, [
        _row(1, 0, 6.0, 99.0),
        {"id": 2, "symbol": "BTCUSDT", "made_at": 1, "target_at": 2,
         "now_price": 100, "sigma_h": 0.01},
    ])

    calls = pe.load_hourly_calls(path)

    assert len(calls) == 1
    assert calls[0].edge_pp == 6.0


def test_fixed_edge_policy_simulates_yes_and_no_bets(tmp_path):
    path = tmp_path / "calls.csv"
    _write_csv(path, [
        _row(1, 0, 6.0, 99.0),   # NO wins at 50c => +1
        _row(2, 0, -6.0, 101.0), # YES wins at 50c => +1
        _row(3, 0, 2.0, 99.0),   # below threshold, no bet
    ])
    calls = pe.load_hourly_calls(path)

    results = pe.simulate_policy(calls, pe.FixedEdgeThresholdPolicy(5.0))

    assert len(results) == 2
    assert sum(r.pnl for r in results) == 2.0


def test_walk_forward_dummy_threshold_comparison_runs(tmp_path):
    path = tmp_path / "calls.csv"
    rows = []
    for week in range(4):
        rows.append(_row(1, week, 6.0, 99.0))
        rows.append(_row(2, week, 3.0, 101.0))
    _write_csv(path, rows)
    calls = pe.load_hourly_calls(path)

    report = pe.compare_policies(
        calls,
        baseline_factory=lambda: pe.FixedEdgeThresholdPolicy(0.0),
        candidate_factory=lambda: pe.FixedEdgeThresholdPolicy(5.0),
    )

    assert len(report["baseline"]) >= 1
    assert len(report["candidate"]) == len(report["baseline"])
    assert "mean_delta_sharpe" in report
    assert "t_stat" in report
    assert isinstance(report["accepted"], bool)
