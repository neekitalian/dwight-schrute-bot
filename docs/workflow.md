# Dwight workflow and preparation

The equities workflow is restricted to **QQQ**. Collect, train and freeze new QQQ artifacts; existing artifacts retain their original identity and are never relabelled. Wider symbol policies are rejected.

## What has actually run

The local environment, synthetic baseline/model experiments, local MLflow logging, release integrity checks, mocked shadow/paper recovery tests and real public Polymarket snapshots have been exercised. Authenticated Alpaca history, real model selection and live shadow require the preparation below. The Linux installer verifies a pinned checkout before installing disabled services. No order was submitted to any account. The Docker configuration has not been built locally because Docker is unavailable. See [validation evidence](validation.md).

The model runs inside Dwight. GitHub stores code/config; private disk holds data/models; MLflow tracks experiments. The model is not uploaded to the broker. CPU training is enough for the first classifier; Hugging Face Jobs can be added if measured compute demand justifies it.

## 1. Prepare market data and keep account roles separate

The selected execution account is **Paper Trading by TradingView**. Dwight proposes and the user enters orders manually; imported fills supply account evidence. See [manual paper workflow](manual-paper.md). Alpaca credentials below provide market data and do not connect or change the TradingView account. A separate Alpaca paper account is optional future work. Copy `.env.example` to `.env`, restrict access, and edit locally:

```sh
cp .env.example .env
chmod 600 .env
```

Required entries are `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`. Never paste values into chat or commit them. The CLI reads simple KEY=value entries without executing shell expressions. Existing environment variables take precedence; use the APCA names consistently. `dwight doctor` reports presence only. `dwight paper-check` performs broker GETs and local reconciliation; it submits/cancels nothing and prints only status/counts. Unexpected positions/orders block reconciliation.

## 2. Preserve real historical data

```sh
dwight download-data --start 2024-10-01 --end 2026-10-01 \
  --symbols QQQ --feed sip --output private-data
```

Dates are inclusive session dates. Choose completed sessions and keep the returned directory intact. It contains raw pages, calendar, one/five-minute CSV/Parquet and hashes. Missing minutes fail validation, including no-trade minutes; no OHLCV is fabricated. Ingestion recognizes holidays, daylight saving and early closes.

Historical SIP access does not imply live SIP entitlement. Confirm the same real-time feed before live shadow. Access errors never fall back to IEX. Changing feed requires a separate dataset, training and release. Volume differences matter for VWAP.

## 3–6. Baseline, examples, training and evaluation

Substitute the directory printed by the downloader:

```sh
dwight experiment private-data/DATASET/QQQ-5Min.csv --symbol QQQ \
  --tracking-uri sqlite:///runs/experiments/mlflow.db --output runs/experiments
```

This runs the four research stages together. Read `report.json`, `candidates.json`, split trade records and `model.json` when produced. Baseline, a fixed relative-volume filter and the model filter are replayed independently. Features use observed signal-time information; labels use later closed trades. Scaling is fitted on training only; validation chooses the threshold; final evaluation uses later sessions.

Default minimums are 100 training labels and 30 each in validation/test, with ten of each outcome per partition. These are engineering minimums, not statistical proof. Insufficient data produces a blocking report and no model. Collect more history or reconsider the strategy rather than relaxing real-data checks. Repeated examination of the holdout requires a fresh final period before promotion. The [walk-forward command](walkforward.md) compares successive unseen windows with a separately reserved final period, matching long-only direction across baseline and filters. Its models are research-only and cannot be used as a deployment release.

For the current long-only research path, use the [fixed QQQ study](qqq-study.md). The single-split experiment now accepts explicit `long_only: true`; the choice is recorded in the model and report and enforced by audits, report replay and shadow. Legacy experiments without that option retain their original long-and-short behavior. Do not silently reuse their results as a long-only evaluation.

The pinned strategy currently requires full 78-bar sessions. Early/partial sessions are explicitly excluded. Costs are fixed simulator assumptions; quotes and spread features are not yet collected. Historical next-bar-open fills are not executable broker fills.

MLflow uses a local SQLite store and private artifacts. Model identity is its run, data/code hashes and JSON checksum; no automatic registry promotion occurs. Tracking failures are recorded. The installed skinny package provides tracking; a hosted MLflow UI is not configured. All data and artifacts stay outside GitHub.

## 7. Freeze a reviewed shadow candidate

```sh
dwight freeze-release runs/experiments/RUN --symbol QQQ --feed sip \
  --policy configs/shadow-policy.json --output releases/candidate
```

Freeze after committing code. Review the report, including weak/negative results, before using a model. A shadow candidate permits observation only and remains `paper_approved: false`. Existing release directories are immutable; create a new one for replacements. Code, model, report, feature, feed and policy mismatches fail verification. Synthetic releases can be checked offline but cannot enter live shadow.

## 8. Run live shadow

On an awake local computer first:

```sh
dwight shadow --release releases/candidate --state runs/shadow --once
dwight shadow --release releases/candidate --state runs/shadow
```

The monitor polls every 30 seconds with a 60-second candle settlement delay. Warmup reconstructs simulated intraday state; stale catch-up candidates are logged as ineligible. It flushes the final completed bar after close and skips early closes. Data/model errors abstain. The service has no order capability.

For continuous operation, use one Ubuntu server with private persistent disk and the [systemd installation runbook](server-experiment.md). The reviewed installer installs the fixed commit and runs its tests before registering disabled services. A valid real-data release and private credentials are required before activation. No public inbound application port is needed. Do not run a systemd worker and a Compose worker against the same state.

`dwight health --state /var/lib/dwight/shadow` checks the heartbeat. Restart recovery and external failure notification still need a real-data deployment test. Stop with `sudo systemctl stop dwight-shadow.service` and create the stop file declared in the frozen policy. Preserve all journals across restarts.

Vendor corrections latch `data_revision_requires_review` across restarts. Preserve state/raw observations, investigate feature impact, and issue a reviewed new release before resuming. Do not delete the ledger as a shortcut.

## 9. Manual TradingView paper workflow and optional broker route

Use the [private proposal and fill journal](manual-paper.md) for the selected TradingView native paper account. The journal records human decisions and imported fills; it does not submit orders. Automatic conversion of shadow signals into actionable proposals still requires current prices, account-aware sizing and parity tests.

For a separately chosen Alpaca paper account, the paper adapter/recovery tests are implemented; `dwight paper-check` can validate the account after keys are configured. **An automated paper execution loop is not implemented yet.** It needs separate proposal/account state, fresh quotes, actual fill accounting, stale-entry expiry, partial-fill protection and session-close handling, then real broker integration tests. Shadow's simulated portfolio cannot be used as broker truth.

The current library supports long whole-share QQQ GTC brackets. A partially filled parent may lack active exits and needs intervention. Unknown positions/orders or absent protection block entries. Timeout recovery looks up the existing client ID; it never blindly resubmits. GTC orders can persist into later sessions, so do not wire this library directly into an unattended loop. Entry limits are not guaranteed loss caps. Use one persistent ledger/account; resets and transfers invalidate its assumptions.

## 10. Monitor and iterate

Retain private heartbeats, journals, raw observed slices and release identity. Review data lag/revisions, duplicate handling, abstentions and later broker order errors. Use actual fills for paper performance after the executor is implemented. New data goes into offline experiments; every replacement repeats evaluation and shadow review. Deploy initially while flat.

## Final integration target: TradingView

User-designated chart: [TradingView layout d5qUHtf0](https://www.tradingview.com/chart/d5qUHtf0/). Connect this after real-data evaluation, shadow validation and the paper execution lifecycle are ready. The saved layout's symbol, interval, script and broker connection have not been verified; the intended Dwight equity scope remains QQQ on completed five-minute bars.

The confirmed account is **Paper Trading by TradingView**, which is separate from an Alpaca paper account. The supported path is Dwight research → reviewed proposal → human paper order → normalized fill import → private results. Pine strategies cannot place orders into the built-in paper account. [TradingView strategy FAQ](https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account)

A chart URL is a workspace reference, not an execution endpoint. A future switch to an Alpaca paper account would be a separate account and integration decision; this workflow does not silently make that switch.

TradingView is a human monitoring target only. Its current [terms, section 3](https://www.tradingview.com/policies/) restrict non display uses of its content, including automated trading and algorithmic decisions. Sending an alert through a Python script does not remove that restriction. Obtain explicit permission or applicable licensed rights before considering any TradingView data driven automation. Dwight currently uses Alpaca data for decisions and has no TradingView alert receiver or UI automation.

The report charts use our own rendering and our own permitted input data. A familiar dark candlestick style does not require TradingView data or APIs.

## Parallel Polymarket collection

```sh
dwight record-polymarket --limit 3 --output runs/polymarket
```

Each invocation is a bounded public snapshot, not a continuous tick recorder. No wallet is needed. The actual smoke collection recorded six books for three markets without errors. Event-probability experiments still need ongoing observations, resolution/fee metadata and a fill simulator. API connectivity alone is not strategy validation.

## GitHub preparation

The initial `.github/workflows/tests.yml` was created through GitHub's editor but failed validation before any job ran. Replace it with the corrected `examples/github-actions-tests.yml` through an authorized workflow editor; the current integration cannot write workflows and the CLI credential lacks that permission. The corrected template uses literal temporary cache paths instead of a runner context unavailable at job environment evaluation. Tests require no trading keys. Do not describe CI as passing before the repaired run completes.

The repository retains Apache-2.0. Dependency and market-data terms remain separate, particularly for a commercial product. Private credentials, datasets, trained artifacts and execution logs are excluded from commits.
