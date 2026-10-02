# Real QQQ data and the deployment boundary

Dwight can privately download the official [FirstRate Data QQQ sample](https://firstratedata.com/i/etf/QQQ) without an account or a payment. The product page links the exact ZIP used by `dwight.firstrate.download_firstrate_sample`. This is a real vendor sample, not synthetic prices and not an Alpaca feed.

```sh
dwight download-qqq-sample --output private-data/firstrate
```

The same downloader is available in Python:

```python
from dwight.firstrate import download_firstrate_sample
manifest = download_firstrate_sample("private-data/firstrate")
print(manifest["directory"])
```

To import an already downloaded official sample:

```python
from dwight.firstrate import import_firstrate_sample
manifest = import_firstrate_sample("private-data/sample.zip")
```

The downloader records the response acquisition timestamp. A local import with no acquisition timestamp explicitly records it as unknown; import time never substitutes for retrieval time.

The importer preserves the exact ZIP and readme, converts the documented US Eastern start timestamps to aware timestamps, validates every minute, selects complete exchange sessions, and independently aggregates 1-minute data into 5-minute bars. It does not trust or copy the vendor's 5-minute file. Missing and duplicate minutes fail rather than being filled. Manifests hash source and normalized files. Private directories use mode 0700 and files 0600.

The source is `firstrate`, feed `firstrate_aggregate`, adjustment `split`. It remains research-only and cannot be relabelled `sip` or `iex` for broker deployment. The [format FAQ](https://firstratedata.com/about/FAQ) documents start timestamps and Eastern time. The [vendor license](https://firstratedata.com/about/license) permits private analysis and internal models and restricts raw-data redistribution. Attribute FirstRate Data when publishing derived research. Keep all raw/normalized datasets in ignored `private-data/`; do not upload them to the public repository or a public dashboard. The software's Apache license does not cover the data.

## Complete private review

Use the manifest inside the directory printed by the downloader:

```sh
python scripts/review_qqq_sample.py \
  --manifest private-data/firstrate/DOWNLOAD_DIRECTORY/manifest.json \
  --output runs/qqq-private-review
```

The output must be a new directory. The command validates the recorded acquisition and hashes, replays the long-only baseline across the full sample, reruns with exactly twice the configured commission and slippage, and saves the trade ledger, marked equity and a portable `report.html`. It also runs the existing standard chronological experiment and the separate feature comparison with unchanged real-data gates. The chronological path includes both directions and is clearly separated from the long-only baseline. It does not send orders or emails, start observation clocks, or upload results.

All generated artifacts are private. The report includes real prices, so keep it outside a public Space and repository. An imported local archive with unverified acquisition is rejected by this specific complete-review script.

## Observed sample, 2 October 2026 UTC

The official download contained QQQ data from **17 September through 1 October 2026**:

- 10,135 source one-minute rows including extended hours.
- 11 complete regular sessions: 4,290 one-minute bars and 858 five-minute bars.
- ZIP SHA-256: `13325fe4e7ca31041c7f3d004318e1b28408ddd54a66980f7107b4ad44cba147`.
- Acquisition time: `2026-10-02T18:21:17.244986+00:00`.

A default VWAP replay with the existing long-only gate made **one simulated trade**, returning **-$5.20** on the default $10,000 account after configured fixed costs. Doubling the configured commission and slippage produced a $5.59 simulated loss, $0.39 below the baseline. This is a pipeline check, not evidence of a stable return distribution or a deployable predictive edge. The full sample was inspected; none of it should subsequently be described as an untouched final holdout.

The chronological research run retained the real-data sample gates. Its baseline produced 1/1/0 labelled trades across train/validation/test, below the 100/30/30 minimums and minimum class counts. It correctly returned `insufficient_data` and did not train or emit a model. That standard research path currently includes both directions; its results are distinct from the separate long-only baseline replay.

## What still requires user preparation

A production-matched QQQ dataset and a paper broker connection still need Alpaca credentials and appropriate market-data access. The existing Alpaca collector can fetch historical minute bars once credentials are provided privately; live paper signals must use the same feed and adjustment definition as the approved model.

Other official options reviewed:

- [Alpha Vantage](https://www.alphavantage.co/documentation/) documents intraday equities as a premium API; its demonstration examples do not establish QQQ access.
- [Kibot's guest API](https://www.kibot.com/api/api-access.html) provides free daily US stocks/ETFs, but its permitted free intraday symbols do not include QQQ. Daily data is not a substitute for five-minute VWAP inputs.
- [Databento](https://databento.com/stocks) offers equity history including QQQ; account access and applicable data licensing are separate setup steps.

No accounts were created, subscriptions purchased, access restrictions bypassed, or TradingView data scraped for this sample.
