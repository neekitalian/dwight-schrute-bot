"""Exercise the optional RL replay contract on invented data, without training.

Run: python -m examples.finrl_smoke --output runs/finrl-smoke
No network or broker calls. This is NOT a FinRL-trained policy or evidence of edge.
"""
import argparse
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from dwight.connectors.csv import read_bars
from dwight.context import PointInTimeContext
from dwight.finrl import DecisionReplay
from examples.make_experiment_demo import generate


def run(output: Path):
    output.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(prefix="dwight-finrl-smoke-") as temp:
        csv = Path(temp) / "synthetic.csv"
        generate(csv, days=60, seed=42)
        bars = list(read_bars(csv))
    # An invented context snapshot deliberately expires during this episode.
    # This tests missing-context rejection; its value has no financial meaning.
    start = bars[0].timestamp
    context = PointInTimeContext(("macro_score",), [{
        "symbol": "QQQ", "published_at": (start-timedelta(hours=1)).isoformat(),
        "available_at": start.isoformat(), "expires_at": (start+timedelta(days=7)).isoformat(),
        "source": "synthetic_contract_fixture", "features": {"macro_score": 0.0},
    }])
    arms = {}
    for name, action, snapshot in (("long_vwap_take_all", 1, None),
                                   ("skip_all", 0, None),
                                   ("context_expiry_check", 1, context)):
        replay = DecisionReplay(bars, context=snapshot, synthetic=True)
        replay.reset()
        rewards = []
        while not replay.terminated:
            _, reward, _, _, _ = replay.step(action)
            rewards.append(reward)
        report = replay.report()
        report["reward_sum"] = sum(rewards)
        (output / f"{name}.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        arms[name] = {"metrics": report["metrics"], "context_coverage": report["context_coverage"],
                      "decision_count": len(report["decisions"]),
                      "context_blocked": sum(d["reason"] == "context_unavailable" for d in report["decisions"])}
    summary = {"status": "completed_synthetic_contract_smoke", "synthetic": True,
               "finrl_trained": False, "promotion_eligible": False,
               "purpose": "Replay/API contract checks only; invented data and fixed actions", "arms": arms}
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/finrl-smoke"))
    args = parser.parse_args()
    print(json.dumps(run(args.output), indent=2))


if __name__ == "__main__":
    main()
