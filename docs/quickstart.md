# Start a private Dwight workspace

Dwight is a QQQ research toolkit: recorded data → reproducible replay → model
experiments → reviewed shadow observation → human paper orders. An installed
toolkit does not mean a profitable model or a connected paper account.

These commands start in a local clone of this repository. Python 3.11 or newer is
required. They install the local research dependencies, not a cloud service:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[data,research,reporting]'
mkdir -p runs
DWIGHT_WORKSPACE="$(pwd -P)/runs/dwight-private"
dwight init-workspace "$DWIGHT_WORKSPACE"
dwight toolkit-status "$DWIGHT_WORKSPACE"
```

Choose a new workspace name on later runs. Initialization refuses any existing
target, symlink traversal, or a missing parent directory. `pwd -P` supplies the
physical path on systems with directory aliases. You can instead provide an
absolute path outside the repository whose parent already exists.

The workspace and subdirectories have mode 0700; generated files have mode 0600.
It contains private data, runs, releases, research configs, a shadow policy and
an empty `.env.example`. Its `.gitignore` excludes workspace content. These
permissions are not encryption; keep backups private as well.

`workspace.json` records **QQQ, completed five-minute bars, shadow mode, and
manual execution in Paper Trading by TradingView**. It is onboarding metadata,
not a launcher. Supply the generated configs explicitly to the commands below.
Changing a feed also requires a matching shadow policy and newly evaluated
data/model artifacts; the toolkit does not silently change an existing release.

`toolkit-status` checks local paths, private permissions, configuration scope,
real-data minimum gates, installed dependency versions, credential presence,
and an optional selected dataset. It makes no network request, places no order,
and never prints credential values. Preparation flags indicate prerequisites
for a command, not API access, a model approval, live-feed entitlement, or an
account connection. Dependency versions are reported; the workspace is not a
dependency lockfile. Tracking with MLflow is optional.

## About ten minutes: exercise the software with invented prices

The exact duration depends on installation and CPU speed. This demonstration
requires no broker, data subscription, GPU, Hugging Face account or API key.

```sh
python examples/make_experiment_demo.py --seed 42 --days 500 \
  --output "$DWIGHT_WORKSPACE/private-data/synthetic-5Min.csv"
dwight experiment "$DWIGHT_WORKSPACE/private-data/synthetic-5Min.csv" \
  --symbol QQQ --synthetic \
  --config "$DWIGHT_WORKSPACE/configs/synthetic-experiment.json" \
  --output "$DWIGHT_WORKSPACE/runs/synthetic"
```

The experiment prints its new run directory. Substitute that directory below:

```sh
dwight report "$DWIGHT_WORKSPACE/runs/synthetic/RUN_ID" \
  --output "$DWIGHT_WORKSPACE/runs/synthetic-report" \
  --label "Synthetic software demonstration"
```

Open the returned `report.html` locally. Check the data label, partitions, trade
ledger, costs and comparison curves. The fixture is deterministic with the same
seed and settings, but its prices and exchange sessions are invented. Reduced
sample gates apply only with `--synthetic`; its model cannot enter live shadow.
Do not present the charts or P&L as market performance.

## Inspect a genuine QQQ sample without credentials

```sh
dwight download-qqq-sample \
  --output "$DWIGHT_WORKSPACE/private-data/firstrate"
```

The downloader uses the public ZIP linked by [FirstRate Data's QQQ product
page](https://firstratedata.com/i/etf/QQQ). It preserves the original download,
recorded acquisition time, source readme and checksums. Substitute its printed
dataset directory in the manifest path:

```sh
python scripts/review_qqq_sample.py \
  --manifest "$DWIGHT_WORKSPACE/private-data/firstrate/DATASET/manifest.json" \
  --output "$DWIGHT_WORKSPACE/runs/qqq-sample-review"
```

The downloader independently aggregates the source minute data. This complete
private review checks the saved hashes, runs the long-only baseline and a fixed
doubled-cost replay, and generates an embedded chart report. It also runs the standard chronological long-and-short experiment
and the separate long-only feature comparison; their results are explicitly
separated. The output directory must not exist yet.

The vendor sample is small and can change. An `insufficient_data` report is a
correct outcome. A local archive without recorded acquisition is not accepted
by this complete-review script. The whole sample is inspected by the review and
cannot afterward be called an untouched final holdout. Its split-adjusted vendor
feed is not Alpaca SIP/IEX and cannot be frozen as an Alpaca shadow release. Keep
raw data and reports private under the [vendor license](https://firstratedata.com/about/license).
See [source details](real-data.md).

## Prepare larger historical experiments

For the existing Alpaca collector, prepare your own market-data credentials and
confirm the relevant feed access. These credentials supply data; they do not
connect to the native TradingView paper account. Edit the file locally:

```sh
cp "$DWIGHT_WORKSPACE/.env.example" "$DWIGHT_WORKSPACE/.env"
chmod 600 "$DWIGHT_WORKSPACE/.env"
```

Set `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in that private file. Never paste
values into chat, GitHub, screenshots or public logs. Existing process environment
variables take precedence. `toolkit-status` reads that file without exporting
its values. Other CLI commands load `.env` from their current working directory,
so run a download from the workspace:

```sh
dwight toolkit-status "$DWIGHT_WORKSPACE"
(
  cd "$DWIGHT_WORKSPACE"
  dwight download-data --start 2024-10-01 --end 2026-10-01 \
    --symbols QQQ --feed sip --output private-data/alpaca
)
```

Dates are inclusive completed session dates; choose the range you actually
intend to study. Keep the returned directory intact. Missing or duplicate
minutes fail validation; no forward-filled prices are invented. Historical SIP
access does not prove real-time SIP entitlement. Matching volume feeds matters
for VWAP. See [Alpaca's market-data documentation](https://docs.alpaca.markets/us/docs/market-data-faq).

Set `dataset_manifest` in `workspace.json` to the returned manifest's relative
path, for example `private-data/alpaca/DATASET/manifest.json`, then run
`toolkit-status` again. This checks provenance and feed compatibility; it does
not train a model. Research commands infer an adjacent manifest or accept the
explicit `dataset_manifest` setting in their experiment config.

```sh
dwight experiment "$DWIGHT_WORKSPACE/private-data/alpaca/DATASET/QQQ-5Min.csv" \
  --symbol QQQ --config "$DWIGHT_WORKSPACE/configs/experiment.json" \
  --output "$DWIGHT_WORKSPACE/runs/real-experiments"
dwight walkforward \
  --data "$DWIGHT_WORKSPACE/private-data/alpaca/DATASET/QQQ-5Min.csv" \
  --real-data --config "$DWIGHT_WORKSPACE/configs/walkforward.json" \
  --out "$DWIGHT_WORKSPACE/runs/walkforward"
dwight enrichment \
  --data "$DWIGHT_WORKSPACE/private-data/alpaca/DATASET/QQQ-5Min.csv" \
  --real-data --config "$DWIGHT_WORKSPACE/configs/walkforward.json" \
  --out "$DWIGHT_WORKSPACE/runs/enrichment"
```

Keep the real gates: at least 100/30/30 labelled train/validation/test trades,
ten examples of each outcome per partition, and enough validation trades.
The default walk-forward/feature comparison needs at least 240 complete
sessions for its first 120/40/40 window plus a 40-session final reserve. That
minimum does not guarantee enough trades or statistical power. Currently,
research excludes early-close and incomplete sessions rather than pretending
they have a normal close. A report may therefore remain insufficient even with
two years of history. Collect more data or reconsider the strategy rather than
lowering real-data gates.

Record data hashes, code commit, feature version, configuration and exact
dependency versions with each comparison. Decide variants before viewing test
results. A period already examined is consumed research data; reserve a fresh
final period before making a deployment decision. No command above promotes a
model. Follow [release and shadow steps](workflow.md) only after review; shadow
observes decisions and has no execution authority.

## TradingView paper orders and the alert inbox are separate

The selected account is **Paper Trading by TradingView**. The supported order
flow is a reviewed proposal → a human places the paper order → normalized
execution evidence is imported into Dwight's private journal. A proposal marked
confirmed is a human acknowledgment, not proof of an order or fill. See the
[manual paper workflow](manual-paper.md), including the normalized CSV schema.
There is no verified native TradingView export adapter yet.

The optional TradingView alert inbox records unreviewed observations. Its HTTP
receipt does not validate the signal, confirm human review, create a proposal,
place an order or establish a fill. Follow its dedicated setup before exposing
any listener; a localhost listener alone cannot receive TradingView's remote
webhooks. Do not use the inbox as a route into the native paper account. The
[TradingView strategy FAQ](https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account)
documents the built-in account's strategy execution limitation. Data permissions
also remain separate from transport. Use permitted market data for experiments.

## What belongs in a public product

Publish source code, empty templates, documented schemas, unit tests and clearly
labelled synthetic examples. Keep credentials, licensed source prices, model
artifacts, private reports, account exports, journals and webhook observations
out of GitHub and public Hugging Face Spaces. Code licensing does not grant
market-data redistribution or model-provider rights. Selling hosted tooling,
support or a commercial deployment still requires checking those separate
terms. Neither a successful install nor a passing test suite establishes a
profitable or deployment-ready trading model.
