# Dwight workflow and preparation

The equities workflow is restricted to **QQQ**. Collect, train and freeze new QQQ artifacts; existing artifacts retain their original identity and are never relabelled. Wider symbol policies are rejected.

## What has actually run

The local environment, synthetic baseline/model experiments, local MLflow logging, release integrity checks, mocked shadow/paper recovery tests and real public Polymarket snapshots have been exercised. Authenticated Alpaca history, real model selection, live shadow and remote deployment require preparation below. No order was submitted to any account. The Docker configuration has not been built locally because Docker is unavailable. See [validation evidence](validation.md).

The model runs inside Dwight. GitHub stores code/config; private disk holds data/models; MLflow tracks experiments. The model is not uploaded to the broker. CPU training is enough for the first classifier; Hugging Face Jobs can be added if measured compute demand justifies it.

## 1. Prepare the account

Use a dedicated Alpaca paper account, initially flat with no unrelated orders. Copy `.env.example` to `.env`, restrict access, and edit locally:

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

Default minimums are 100 training labels and 30 each in validation/test, with ten of each outcome per partition. These are engineering minimums, not statistical proof. Insufficient data produces a blocking report and no model. Collect more history or reconsider the strategy rather than relaxing real-data checks. Repeated examination of the holdout requires a fresh final period before promotion.

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

For continuous operation, prepare one Linux server with Docker/Compose and private persistent disk. No server has been provisioned. Clone the exact release commit, install `.env` privately, and copy its matching release directory. Prepare storage for container UID 10001:

```sh
mkdir -p runs
sudo chown 10001:10001 runs
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml logs --tail 50
```

The container build and startup need verification on that host. No public inbound port is needed. Do not scale replicas. Model mounts are read-only and the state persists. Build the image from the same source used to freeze the release.

`dwight health --state runs/shadow` checks the heartbeat. Docker marks unhealthy operation but does not itself send an alert or restart solely due to an unhealthy state; connect host monitoring before unattended use. Stop with `docker compose -f deploy/compose.yaml stop` or create `runs/STOP`. The stop file keeps restarts stopped.

Vendor corrections latch `data_revision_requires_review` across restarts. Preserve state/raw observations, investigate feature impact, and issue a reviewed new release before resuming. Do not delete the ledger as a shortcut.

## 9. Broker paper execution: next gate

The paper adapter/recovery tests are implemented; `dwight paper-check` can validate the account after keys are configured. **An automated paper execution loop is not implemented yet.** It needs separate proposal/account state, fresh quotes, actual fill accounting, stale-entry expiry, partial-fill protection and session-close handling, then real broker integration tests. Shadow's simulated portfolio cannot be used as broker truth.

The current library supports long whole-share QQQ GTC brackets. A partially filled parent may lack active exits and needs intervention. Unknown positions/orders or absent protection block entries. Timeout recovery looks up the existing client ID; it never blindly resubmits. GTC orders can persist into later sessions, so do not wire this library directly into an unattended loop. Entry limits are not guaranteed loss caps. Use one persistent ledger/account; resets and transfers invalidate its assumptions.

## 10. Monitor and iterate

Retain private heartbeats, journals, raw observed slices and release identity. Review data lag/revisions, duplicate handling, abstentions and later broker order errors. Use actual fills for paper performance after the executor is implemented. New data goes into offline experiments; every replacement repeats evaluation and shadow review. Deploy initially while flat.

## Final integration target: TradingView

User-designated chart: [TradingView layout d5qUHtf0](https://www.tradingview.com/chart/d5qUHtf0/). Connect this after real-data evaluation, shadow validation and the paper execution lifecycle are ready. The saved layout's symbol, interval, script and broker connection have not been verified; the intended Dwight equity scope remains QQQ on completed five-minute bars.

The initial connection should be **Dwight → Alpaca paper API**, with the **same Alpaca paper account connected through TradingView's Trading Panel** for monitoring. Alpaca documents paper-account support in its TradingView integration. Verify account identity and that the bot's orders/positions appear during the final integration test. A chart URL is a workspace reference, not an order endpoint. [Alpaca TradingView connection](https://alpaca.markets/learn/how-to-trade-options-on-tradingview-with-alpaca-trading-api-account)

If chart-generated signals are wanted later, the optional route is **TradingView → authenticated HTTPS webhook receiver → Dwight model and deterministic risk checks → broker paper API**. TradingView's native Paper Trading simulator is separate; Pine strategies cannot submit to it. The same-account Alpaca connection is the planned route, subject to account confirmation. [TradingView strategy FAQ](https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account)

For optional alerts, start with comparison in shadow. Confirm the exact script/version, QQQ symbol, session and volume feed; compare timestamps, bars and features against the Python strategy before routing alerts into decisions. Do not run two independent entry producers for the same account. The trained model stays on Dwight's server and broker fills remain execution truth.

The future receiver must authenticate requests, persist and deduplicate event IDs, reject stale/incorrect-symbol alerts, and queue processing before acknowledging promptly. TradingView documents a three-second webhook timeout, HTTPS support and a two-factor-authentication requirement. Never put broker credentials in alert messages. This requires a separate public HTTPS ingress; the current polling-only Compose service exposes no inbound port. [Official webhook documentation](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)

No TradingView connection, alert, webhook listener or account change has been created. At integration time, prepare access to the saved layout and selected paper account. If adding chart alerts, also confirm the plan's webhook features and two-factor authentication.

## Parallel Polymarket collection

```sh
dwight record-polymarket --limit 3 --output runs/polymarket
```

Each invocation is a bounded public snapshot, not a continuous tick recorder. No wallet is needed. The actual smoke collection recorded six books for three markets without errors. Event-probability experiments still need ongoing observations, resolution/fee metadata and a fill simulator. API connectivity alone is not strategy validation.

## GitHub preparation

The CI template is `examples/github-actions-tests.yml`. Activate it as `.github/workflows/tests.yml` using GitHub's editor or a credential with workflow permission; the existing push credential previously lacked that scope. Until then tests are local, not active CI. Tests require no trading keys.

The repository retains Apache-2.0. Dependency and market-data terms remain separate, particularly for a commercial product. Private credentials, datasets, trained artifacts and execution logs are excluded from commits.
