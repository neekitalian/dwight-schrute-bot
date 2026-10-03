# Dwight Schrute Bot

A small toolkit for reproducible QQQ research and reviewed paper trading. The first integrated strategy is the existing **VWAP + market-structure pullback bot**, pinned to its source commit. Model research is separate from deterministic execution.

**0.7.1 research preview:** [download the toolkit](https://github.com/neekitalian/dwight-schrute-bot/releases/tag/v0.7.1), follow the [private workspace setup](docs/quickstart.md), or explore the [public research demo](https://huggingface.co/spaces/neekthekid/dwight-schrute-bot). No profitable model or connected account is bundled. [Product scope](docs/product.md) explains the reusable core and the remaining execution work.

The [Connections hub](docs/connections.md) adds public Coinbase, Binance, Kraken and Polymarket checks, private Alpaca, Databento and Massive checks, and downloadable setup profiles. [Market data connections](docs/data-providers.md) covers local key entry and QQQ history downloads. TradingView remains a manual paper workflow; IBKR and Schwab adapters are planned. Crypto checks do not enable a crypto strategy or authenticated account.

**Equities scope: QQQ only.** Data collection, experiments, shadow releases and paper-order policies enforce this scope. Public Polymarket research remains a separate connector.

**Working now:** private FirstRate QQQ sample ingestion, Alpaca historical ingestion, calendar validation, baseline and model-filtered replay, chronological and walk-forward classifier experiments, a manual TradingView paper journal, local MLflow tracking, frozen releases, a live-data shadow monitor, paper-order/recovery library, and public Polymarket book snapshots.

The manual paper journal can export a private, self-contained HTML report of imported fills, costs, realized P&L and proposal discrepancies. Account equity and drawdown remain unknown without account snapshots. See the [manual paper report workflow](docs/manual-paper.md).

The [baseline observation workflow](docs/manual-observer.md) reads completed QQQ bars from Alpaca, records causal VWAP setups, and prepares a fresh manual proposal only with an explicit human entry reference and quantity. It does not simulate an account or enforce account risk limits. Price history, proposals and reports remain private. No trained model is used in this baseline path.

The [native paper milestone workflow](docs/manual-milestones.md) freezes 12, 24, 48 and 168 hour cutoffs after reviewed preparation. Its private reports show observation coverage, imported fill accounting, strategy evidence checks and later corrections. Delivery claims and receipts are tracked separately; a connected sender is still required.

**Selected account:** Paper Trading by TradingView. Use [manual proposals and fill imports](docs/manual-paper.md); no automated orders are sent to that account. Alpaca is the planned market-data provider.

**Deployment boundary:** the deployable service is shadow only and cannot submit orders. The paper library is mock-tested; the CLI permits read-only account checks. An unattended paper execution loop and a Polymarket fill simulator remain unfinished. Synthetic models cannot run in live shadow. No account is connected and no order has been submitted.

Follow the [step-by-step workflow and preparation guide](docs/workflow.md).

The [fixed QQQ study runner](docs/qqq-study.md) prepares a recipe before collection, trains the CPU classifier with matching long-only evaluation, audits the result and stress-tests the frozen model under higher costs. All data and reports stay private; no model is automatically activated.

The [next product specification](docs/automatic-paper-product.md) targets a private web console and an automatic Python worker for a separately selected API paper account. It defines the customer flow and missing execution requirements; it does not enable automatic orders in this release.

The [TradingView observation inbox](docs/tradingview-alerts.md) receives validated QQQ five-minute bar messages into a private, durable journal. It is separate from model decisions and manual proposals. The local HTTP flow is tested; the Pine observer template still needs compilation and a real delivery test in TradingView. Linux service and HTTPS proxy templates are provided, but no public endpoint is activated.

[Real QQQ sample research](docs/real-data.md) now validates an official vendor download and reproduces a private historical replay. The observed 11-session sample is too small to train a deployable model. [Feature enrichment](docs/enrichment.md) compares four additional causal session features with the original classifier using fixed chronological windows. More features have not established better performance.

[Experiment reports](docs/experiments-and-reports.md) add strategy audits, candlestick and portfolio charts, and private 12 hour, 24 hour, 48 hour and one week milestone ledgers. The observation clock requires verified real data. [Linux service templates](docs/server-experiment.md) support a persistent shadow worker and report generation. Email delivery needs a connected sender and is tracked separately from report creation.

A [Hugging Face research app](docs/huggingface.md) adds a Gradio interface to the
synthetic experiment, with interactive equity, drawdown, candle and trade charts,
classifier diagnostics and four fixed robustness cases. The
[transformer research plan](docs/transformer-research.md) shows how price sequence
and news models could add features, with their value explicitly unmeasured.
Its deployment bundle contains allowlisted committed source
and attribution; it contains no credentials or private datasets. This research
interface is separate from the continuous shadow/paper worker.

Open the public [Dwight QQQ Research Lab](https://huggingface.co/spaces/neekthekid/dwight-schrute-bot).

An optional [FinRL research adapter](docs/finrl-research.md) lets an offline policy
accept or skip existing long QQQ VWAP candidates while preserving the replay's
stops, sizing and loss rules. Point-in-time context snapshots can add separately
prepared fundamental, macro or news features. The adapter and synthetic contract
checks are implemented; no FinRL model has been trained or promoted, and no
fundamental data feed is connected. The public Space still runs its existing
classifier demonstration.

## Quick start

Python 3.11+; basic replay has no third-party runtime dependencies. For data, training and tracking:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
dwight doctor
python examples/make_demo.py
dwight strategies
dwight replay examples/synthetic.csv --symbol SYNTHETIC
python -m unittest discover -s tests -v
```

Without installing, use `python3 -m dwight` in place of `dwight`.

Run the full offline synthetic experiment, including a real local classifier and MLflow logging:

```sh
python examples/make_experiment_demo.py
dwight experiment private-data/synthetic-vwap-5Min.csv --symbol QQQ --synthetic \
  --config configs/synthetic-experiment.json --output runs/experiment-smoke
```

This verifies software behavior, not trading performance. Reduced sample requirements are accepted only for synthetic runs. Real-data experiments validate the sibling dataset manifest and compare VWAP, a fixed volume filter, and the learned filter on chronological partitions. Models use JSON coefficients and preprocessing rather than pickle. Basic replay alone needs only `pip install .`; optional extras are `.[data,research,tracking]`.

```sh
dwight replay /path/to/qqq.csv --symbol QQQ --config examples/config.json
# Optional network read; no wallet or API key required:
dwight polymarket-discover --limit 5
dwight record-polymarket --limit 3 --output runs/polymarket
```

CSV schema: `timestamp,open,high,low,close,volume`. Supply one instrument, timezone-aware, start-labelled, completed 5-minute US regular-session bars. Intraday gaps and duplicate timestamps fail validation. The caller must provide valid exchange sessions, including holidays and early closes. Full rules and simulator limitations: [VWAP guide](docs/vwap.md).

Each replay creates `runs/<unique-id>/` containing the exact `input.csv`, `manifest.json`, `summary.json`, and `trades.json`. The manifest records the resolved settings, input and source hashes, and original VWAP commit. `runs/journal.sqlite3` stores successful runs and trade/equity records transactionally. Invalid data leaves a `failed.json` marker, never a successful journal row. Keep the whole runs directory private; it includes input data.

The original detailed CSV/SVG report command is also preserved:

```sh
python -m vwap_bot examples/synthetic.csv --symbol SYNTHETIC --output runs/charts
```

## Structure

```text
dwight/
  connectors/csv.py         validated historical input
  connectors/polymarket.py  public GET-only market discovery
  runner.py                strategy registry and replay lifecycle
  store.py                 SQLite transactional run journal
  research.py              model assessment/escalation contracts
  data.py                  Alpaca raw data, calendar validation, CSV/Parquet
  experiments.py           causal features, evaluation, JSON model, MLflow
  walkforward.py           successive unseen tests and reserved final holdout
  manual.py                human paper proposals and FIFO fill journal
  signals.py               causal baseline setups without simulated fills
  manual_signals.py        private observations and durable proposal preparation
  manual_campaign.py       fixed native paper milestones and delivery ledger
  toolkit.py               private workspace setup and preparation checks
  tradingview.py           authenticated observation inbox without execution
  context.py               timestamped QQQ research context, expiry and provenance
  finrl.py                 optional offline skip/take RL environment and trainer seam
  ops.py                   preparation checks and release integrity
  shadow.py                read-only current-data monitor and decision journal
  paper.py                 paper-only transport, limits, durable intents/recovery
  recorder.py              bounded public Polymarket book snapshots
vwap_bot/                  unchanged upstream replay engine
examples/                  config and synthetic-data generator
tests/                     strategy and framework regression tests
docs/                      architecture, strategy semantics, attribution
```

The VWAP engine currently owns its strategy-specific simulated fills and limits. It is not yet a broker-neutral signal generator: introducing an external executor requires separating signal generation from fill/account state and proving replay parity. Do not route its historical trade records directly to a broker.

The research cascade labels evidence as relevant/irrelevant or abstains. It cannot produce executable orders. Thresholds require calibration; confidence is not expected return. Concrete Jev/Kimi providers are deliberately not bundled until account access, model versions and evaluation requirements are established.

## Development and deployment

The GitHub Actions workflow has been created, but its initial run failed validation before executing tests because job-level environment expressions used an unavailable context. The corrected workflow is prepared at `examples/github-actions-tests.yml` and still needs to replace `.github/workflows/tests.yml` through an authorized workflow editor. Local tests remain the verified checks until that repair passes. `deploy/compose.yaml` prepares a persistent shadow service with a private state volume, read-only release and health check. Docker remains unverified. The [Ubuntu installer](docs/server-experiment.md) provides a separate systemd path and runs tests before registering disabled services. `requirements.lock` records the dependency versions used in local validation.

See [architecture and next milestones](docs/architecture.md). The synthetic workflow and public Polymarket snapshots need no credentials. Alpaca history/shadow need paper keys and the selected data entitlement. Secrets, private datasets, models and account journals must never be committed. Hugging Face Jobs is optional; this classifier runs locally on CPU. No live-money switch exists.

## Provenance and license

Apache License 2.0, preserving this repository's original license. VWAP source is vendored unchanged from [codex/vwap-paper-bot](https://github.com/neekitalian/VWAP-Trading-Strategy/tree/codex/vwap-paper-bot), commit `532618ed5b415c974bb5be3da3330cd638c38931`. See [NOTICE](NOTICE) and [retained upstream license](docs/VWAP-LICENSE). Updates should be reviewed against that pinned source and tested for parity.

Framework ideas were informed by [FriesTrader](https://github.com/YizhiSong/FriesTrader), [TradingAgents](https://github.com/TauricResearch/TradingAgents), and [NautilusTrader](https://github.com/nautechsystems/nautilus_trader). Those projects are not vendored or runtime dependencies. Future dependencies retain their own licenses; data-provider rights are separate from code licensing.
