# QQQ feature-enrichment comparison

Dwight has a separate research path to test whether four additional, causal
technical features improve its existing take/skip model. Existing frozen v1
models, feature names, and shadow releases are unchanged. This path cannot
submit orders, score the reserved final holdout, or promote a model.

The experiment compares these four portfolios on identical development windows:

| Portfolio | Rule |
| --- | --- |
| VWAP baseline | Original candidates, subject to the same direction policy |
| Simple volume filter | Current volume at least the mean of preceding session bars |
| Technical v1 | Existing ten features and regularized logistic regression |
| Session context v2 | Same classifier and ten features, plus the four features below |

The default direction policy is long only for all four portfolios and the
training-label baseline. Every portfolio is replayed independently through the
same engine; filtering an early trade can change subsequent candidates. Results
are not obtained by deleting losing trades from an existing portfolio.

## New features

The separately versioned schema is
`vwap-candidate-v2-session-context-research`. Every feature is captured at the
completed candidate bar and uses only the observed prefix of its current session.

| Feature | Exact definition |
| --- | --- |
| Session return / ATR | Current close minus session first open, divided by current trailing ATR |
| Observed session range / ATR | Maximum high minus minimum low observed so far, divided by current trailing ATR |
| Realized log volatility | Root mean square of up to twelve recent close-to-close log returns |
| One-bar VWAP change / ATR | Current cumulative bar-derived VWAP minus the previous bar's cumulative VWAP, divided by current trailing ATR |

The session range does not use the eventual daily high or low. Normalization is
fit only on each window's training examples. No fundamental, news, constituent,
or economic facts are invented; the existing point-in-time context interface
remains separate until authentic timestamped vintages are supplied.

## Fixed experiment protocol

Both model arms use logistic regression with `C=1`, `solver=liblinear`,
`random_state=0`, and `max_iter=1000`. There is no model-family or parameter sweep.
Each arm uses the predeclared threshold grid, default `[0.35, 0.5, 0.65]`.
Validation net P&L selects a threshold, with lower drawdown and proximity to
0.5 as tie breakers. Both arms and thresholds are frozen before either evaluates
the development-test outcomes. The report retains both arms, even when the new
features perform worse. There is no test-selected winner.

Window planning and sample gates are shared with [walk-forward research](walkforward.md):
120 initial training sessions, 40 validation sessions, 40 development-test
sessions, and 40 separately reserved final sessions by default. Training needs
100 labeled trades; validation and test need 30 each. Each partition needs ten
examples of both outcomes, and a threshold needs five validation trades. Smaller
requirements are accepted only for explicitly synthetic software tests.

Before replay or fitting, the runner saves `recipe.json`, including source
hashes, input hash, settings, feature definitions, exact session windows, learner
settings and selection rules. Models and the report reference its SHA-256.
This is a local predeclared recipe, not an external preregistration or evidence
that a researcher never inspected the data previously. Preserve earlier holdout
reservations when extending data; do not shift the reservation and reuse its
old dates for development.

The final reserved period is checked for input integrity but never replayed,
fit, or scored. If there is too little history, no model is fitted. If either
arm lacks enough validation trades, the paired test remains unconsumed. If the
test itself has too few labels, it is marked consumed but no comparative scores
are published for that window. Insufficient windows remain in the reported
denominator. Minimum counts are engineering gates, not statistical-power claims.

## Run from the CLI

```sh
dwight enrichment --data private-data/qqq/QQQ-5Min.csv --out runs/enrichment-real --real-data
```

Use `--config` for a JSON experiment configuration. `--real-data` and `--synthetic` are mutually exclusive and one is required. The default manifest lives next to the input file.

## Run from Python

```python
from pathlib import Path
from dwight.enrichment import run_enrichment

report = run_enrichment(
    Path("private-data/qqq/QQQ-5Min.csv"),
    Path("runs/enrichment-real"),
    synthetic=False,
    config={"dataset_manifest": "private-data/qqq/manifest.json"},
)
print(report["status"], report["directory"])
```

The interface accepts the existing walk-forward configuration keys, including
window sizes, direction policy, cost assumptions, and dataset manifest. Source
provenance is verified using the existing downloader's hashes. An observed CSV
without verified provenance remains `unverified_csv`; it does not become Alpaca
data because the user marks it real.

For a reproducible software test:

```sh
python examples/make_experiment_demo.py --output private-data/synthetic-vwap-5Min.csv
python - <<'PY'
import json
from pathlib import Path
from dwight.enrichment import run_enrichment
report = run_enrichment(
    Path("private-data/synthetic-vwap-5Min.csv"),
    Path("runs/enrichment-synthetic"),
    synthetic=True,
    config=json.loads(Path("configs/synthetic-walkforward.json").read_text()),
)
print(report["status"], report["directory"])
PY
```

## Artifacts and remaining limits

Each private run contains the exact CSV snapshot, verified dataset manifest if
available, immutable recipe and session plan, per-window training/validation
candidates, model coefficients/scalers, four test trade/equity series, calibration
and local inference latency, plus an aggregate report. These research model
artifacts deliberately use a different `kind` so the v1 deployment loader rejects
them. JSON coefficients are inspectable; no executable pickle is produced.

Every fold starts flat with the same capital. Aggregate P&L sums independent
windows; it is not a continuous account return. The engine still uses next-bar
OHLC fills and fixed costs, without observed quotes, partial fills, queue position
or market impact. Only full 78-bar sessions are supported. Early closes are
excluded explicitly. These limits remain even when model metrics improve.

The verified FirstRate public QQQ sample acquired for this work contains eleven
complete sessions from September 17 through October 1, 2026. It can exercise real
data ingestion and a baseline replay. It cannot support the unchanged 240-session
minimum window plan or establish an AI improvement. Its feed and split-adjusted
provenance remain distinct from Alpaca/raw requirements for shadow releases.

FirstRate acquisition records distinguish the downloader's HTTPS response from
a locally supplied archive. A local archive is an unverified vendor claim even
if its caller supplies a retrieval timestamp; reports say so. Older manifests
with a retrieval timestamp retain the narrower description of recorded
acquisition. Checksums and acquisition logs establish the recorded source chain,
not cryptographic vendor signing or a guarantee of global authenticity.
