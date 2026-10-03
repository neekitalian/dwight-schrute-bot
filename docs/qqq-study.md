# A fixed QQQ research run

This workflow collects authenticated Alpaca history, trains the existing CPU classifier, compares later periods and prepares evidence for a shadow review. It cannot place orders, start a forward campaign, purchase data, send mail or automatically approve a model.

## Recipe before results

`configs/qqq-study.json` requests QQQ SIP/raw minute data from 4 January 2016 through 16 September 2026. Alpaca documents history since 2016; actual access is checked by the request. Historical SIP access does not establish current SIP entitlement. [Coverage](https://docs.alpaca.markets/us/docs/about-market-data-api), [historical and live feed access](https://docs.alpaca.markets/us/docs/market-data-faq)

The range ends before the already-inspected 17 September through 1 October 2026 FirstRate sample. Those economic outcomes are not fresh holdout evidence even if downloaded from another feed. Future observation after a frozen candidate provides a separate later evaluation.

The recipe fixes these choices before outcomes are scored:

* QQQ, five-minute regular-session bars, long-only VWAP baseline
* First 60 percent of complete sessions for training, next 20 percent for validation, remaining sessions for a once-only test
* Training-only scaling and logistic regression; validation-only threshold selection from 0.35, 0.5 and 0.65
* At least 100/30/30 labelled trades in train/validation/test and at least ten examples of each outcome in each partition
* Baseline, fixed volume filter and learned filter compared with identical costs and strategy rules
* A second independent replay with twice the commission and slippage, using the exact same frozen classifier and threshold

The test period is consumed by this run. It must not later be called an untouched reserve. This is a single chronological study, separate from the [walk-forward workflow](walkforward.md), which reserves an additional final period. Do not pass that workflow's complete snapshot into this study. A longer history does not guarantee enough labelled VWAP trades or predictive value.

## Commands

Run from the repository with the data, research and reporting dependencies installed. Set Alpaca paper credentials in the private `.env` file; those credentials are used only for data requests here.

```sh
python scripts/run_qqq_study.py prepare \
  --workspace runs/studies/qqq-study \
  --config configs/qqq-study.json

python scripts/run_qqq_study.py collect --workspace runs/studies/qqq-study
python scripts/run_qqq_study.py evaluate --workspace runs/studies/qqq-study
python scripts/run_qqq_study.py status --workspace runs/studies/qqq-study
```

Preparation creates a private workspace and fingerprints the recipe. Collection retains raw responses, timestamps, the exchange calendar, normalized CSV/Parquet and hashes. Completed datasets are reused only after integrity checks. A failed download remains preserved and a later explicit collection attempt starts a new download; partial HTTP pages are not resumed or silently merged. Missing regular-session minutes fail validation; no prices are filled in. Early-close sessions remain in the collected dataset but are excluded by the current strategy's full-session requirement.

Commit the reviewed research code before evaluation. The study fingerprints the source when evaluation begins. Repeating a completed evaluation verifies and returns its saved evidence instead of refitting. An interrupted evaluation stops for review because test outcomes might already have been inspected. Do not delete its state or create another workspace to conceal repeated testing.

## Recover a completed acquisition with gaps

The default collector rejects missing regular-session minutes. If every HTTP page was retained and pagination finished, an operator can inspect timestamp coverage without scoring any returns. `audit_failed_download` in `dwight.history_recovery` verifies the original calendar, page inventory, checksums, timestamps and pagination. It records every missing interval. It does not infer why a minute is missing.

An explicit coverage amendment may exclude every incomplete session in full, with a fixed maximum of five percent of requested sessions. Before evaluating outcomes, prepare a new private recipe that copies the original protocol exactly and adds:

```json
{
  "incomplete_session_policy": "exclude_whole_session",
  "max_excluded_session_fraction": 0.05,
  "coverage_amendment": {
    "original_protocol_sha256": "CANONICAL_DIGEST_FROM_ORIGINAL_STATE",
    "outcomes_inspected": false,
    "reason": "Exclude all incomplete sessions before scoring outcomes"
  }
}
```

Use `prepare` with that complete amended recipe and a new workspace, then recover offline:

```sh
python scripts/run_qqq_study.py recover \
  --workspace runs/studies/qqq-complete-sessions \
  --source-attempt runs/studies/qqq-study/datasets/FAILED_ATTEMPT \
  --source-protocol runs/studies/qqq-study/protocol.json
```

Recovery preserves the failed attempt, exact raw pages, acquisition timestamps and original protocol. The recovered manifest records retained counts and every excluded session; no missing price or volume is invented. Strategy, date range, split fractions, model settings and screening rules cannot change through this amendment. The original collector did not persist HTTP request URLs, so feed/query identity relies on its trusted protocol; response hashes alone do not prove it.

Excluded sessions can coincide with market stress and therefore understate drawdown or losses. The recovered dataset is explicitly research only. Keep that limitation in every result, including a frozen diagnostic candidate. A passing numerical screen is not evidence that excluded periods were safe.

## Results

`summary.json` records sample counts, partition dates, metrics and limitations. If fitting succeeds, `charts/report.html` contains the private historical report; `cost-stress.json` and the associated equity/trade JSON files retain both cost cases. Account and broker results remain unobserved. Raw data and licensed price charts must remain outside public GitHub and Hugging Face deployments.

`candidate-review.json` applies a predeclared screening rule: the independent audit must pass; both cost cases need at least 30 filtered test trades, positive filtered net PnL above both comparators, and drawdown no worse than either comparator. These are conservative engineering screens, not a statistical confidence statement. Failure means retain the baseline and continue research; do not tune against the consumed test to make it pass. The review never automatically freezes or activates anything.

## Freeze and observe

A release reviewer must examine the actual report before invoking `dwight freeze-release` on its recorded experiment directory with the matching SIP shadow policy. The release binds code, data, features, direction, model checksum and risk policy. Freezing verifies identity; it does not prove profitability or permit orders. A deliberately weak model may only be frozen as a clearly labelled diagnostic observation candidate, never advertised as qualified.

Only after a real-data candidate exists, current feed access succeeds and the existing server is verified can the shadow worker begin meaningful forward observation. A market-closed check, delayed catch-up replay or synthetic fixture must not start a milestone clock. The model remains fixed during the observation window. Actual paper executions require a separate confirmed execution route and verified fill evidence; they are never inferred from shadow fills.
