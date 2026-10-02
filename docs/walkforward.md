# Walk-forward QQQ experiments

Dwight can compare VWAP, a fixed volume filter, and the existing logistic model
over multiple chronological windows. This is offline research. It does not
connect to a broker, train FinRL, consume a final holdout, or approve deployment.

Each variant uses the same strategy configuration, initial capital, commissions,
slippage assumptions, and direction policy. The default is **long only**, including
the baseline that creates training labels. Short candidates are recorded as
rejected before inference or entry. Setting `long_only: false` is a separate
research configuration, not a deployable Alpaca configuration.

## Window plan

The latest 40 complete sessions are reserved first. Their dates appear in
`plan.json` and the report's `final_holdout` field. They are not replayed, scored,
used for normalization, or used for threshold selection. Parsing those bars to
check CSV integrity and session completeness does not evaluate their outcomes.
There is intentionally no switch in this command to score the final holdout.

Defaults use an expanding training history:

| Window | Training | Validation | Development test |
| --- | --- | --- | --- |
| 1 | Sessions 1–120 | 121–160 | 161–200 |
| 2 | Sessions 1–160 | 161–200 | 201–240 |
| 3 | Sessions 1–200 | 201–240 | 241–280 |

Test periods never overlap. A previous test period is ordinary observed history
by the next window and can become its validation or training data. All outcomes
used by a window must precede that window's test period. Rolling mode keeps only
the requested number of preceding training sessions instead of expanding.

Each window runs these steps:

1. Replay the training and validation baseline separately from flat state.
2. Purge labels crossing a partition boundary and check sample counts.
3. Fit the scaler and classifier on training rows only.
4. Replay the validation period at each predeclared threshold. Choose by net P&L,
   then lower drawdown, then proximity to 0.5, following the existing experiment.
5. Freeze the model and threshold before inspecting that window's test outcomes.
6. Check test sample counts, then replay all three variants on the same test bars.

The minimums remain 100 training labels, 30 validation labels, 30 test labels,
10 examples of each class in each partition, and 5 validation trades for an
eligible threshold. These are engineering gates, not evidence of statistical
power. Insufficient windows are explicitly reported without fabricated scores.
If training or validation is insufficient, that window's test is not consumed.
If test sample counts are insufficient, it has been inspected and is marked
consumed even though no comparative score is produced.

## Command line

The CLI requires an explicit evidence choice. For observed QQQ data:

```bash
python -m dwight walkforward \
  --data private-data/qqq/QQQ-5Min.csv \
  --out runs/walkforward-real \
  --real-data \
  --config private-data/walkforward-config.json
```

`--config` is an optional JSON **file path**, not an inline JSON string. It uses
the same configuration keys shown in the Python example below. Omit it to use
the default windows and sample gates. Inputs must be objects of at most 1 MiB;
duplicate JSON keys and non-finite values are rejected.

For a software smoke test with invented data:

```bash
python examples/make_experiment_demo.py \
  --output private-data/synthetic-vwap-5Min.csv --days 500 --seed 42
python -m dwight walkforward \
  --data private-data/synthetic-vwap-5Min.csv \
  --out runs/walkforward-synthetic \
  --synthetic
```

The default sample gates can leave synthetic windows insufficient. This is a
valid diagnostic result, not a CLI failure or a reason to invent scores. A
separate synthetic-only config can lower the documented sample thresholds for
plumbing tests. Real-data runs cannot use reduced sample gates.

`--real-data` asserts that the input is observed data; it does not prove source
provenance, a trading advantage, or deployment readiness. The runner retains
`unverified_csv` when appropriate. Neither mode evaluates the final holdout,
submits orders, or promotes a model. Both options together, or neither option,
are rejected. The command prints the full report JSON and writes run artifacts
under a fresh directory within `--out`; keep these private.

## Run from Python

```python
from pathlib import Path
from dwight.walkforward import run_walkforward

report = run_walkforward(
    Path("private-data/qqq/QQQ-5Min.csv"),
    Path("runs/walkforward"),
    config={
        "train_sessions": 120,
        "validation_sessions": 40,
        "test_sessions": 40,
        "holdout_sessions": 40,
        "max_windows": 12,
        "mode": "expanding",
        "long_only": True,
        "strategy": {"commission": 0.005, "slippage": 0.01},
        "dataset_manifest": "private-data/qqq/manifest.json",
    },
)
print(report["status"], report["directory"])
```

`data` is the existing start-labelled five-minute QQQ CSV format. A verified
Alpaca dataset manifest is checked with the same checksums and feed provenance
as the single-split experiment. If a real CSV has no manifest, its source is
explicitly `unverified_csv`. No data is downloaded or uploaded by this runner.
It accepts at most 50 windows and 21 threshold trials per window.

Synthetic fixtures must use `synthetic=True`. Only synthetic runs can lower the
sample requirements. Their status ends in `synthetic_smoke`; they remain
ineligible for promotion, even if every metric looks favorable.

## Output and interpretation

Every run writes an immutable directory containing:

* `input.csv`: the exact bytes fingerprinted and replayed, kept locally. Treat
  this dataset snapshot as private licensed data, including its reserved period.
* `report.json`: settings, dataset fingerprint, source-file hashes, sample
  counts, excluded sessions, per-window results, and holdout reservation.
* `plan.json`: exact whole-session partitions and unused development sessions.
* `window-NNN/report.json` and a model artifact when fitting succeeds.
* `window-NNN/test-*-trades.json` and `test-*-equity.json` for scored windows.

Equity points are timestamped when the completed candle is available. Fees and
slippage come from the replay engine. Each window restarts at the same capital
and flat state. Aggregate P&L is the sum of **independent** window replays; it is
not a continuous account curve or a compounded portfolio return. Insufficient
windows remain visible in planned/completed counts. A partial report must not
be presented as results for all windows.

Reading development-test outcomes makes them part of the research process.
Keep the final reservation frozen with the dataset fingerprint and exact dates.
Do not move it forward and train on its former dates just because new data was
downloaded. This runner records each reservation; it does not maintain a global
registry capable of preventing a researcher from reusing a holdout in another
run. Any future final evaluation needs a separately reviewed protocol with
frozen features, costs, strategy, candidate model, and selection rules.

## Limits that remain

Only full 78-bar weekday sessions are supported; early closes are excluded.
Holiday correctness depends on the dataset pipeline. Training labels come from
baseline-selected closed trades; filtering can change subsequent candidates.
The engine simulates next-bar fills and fixed costs, with no quotes, order queue,
latency, partial fills, or market impact. Its daily losing-trade limit is not
the Alpaca adapter's cash-loss policy. Matching long-only direction does not
close those execution gaps.

Synthetic data tests plumbing, not market profitability. Real results still
need uncertainty estimates, market-condition analysis, separate cost stress
tests, and verification against observed fills. No transformer or fundamental
model is evaluated by this initial walk-forward runner.
