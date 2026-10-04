# Market data connections

Dwight supports private Alpaca, Databento and Massive market data. These keys
provide data access. They do not connect or automate a TradingView paper account.

For the separate news relay configuration, see [News sources](news-sources.md).

## Add keys privately

From your installed Dwight workspace:

```sh
python -m dwight prepare-data-keys
```

Open the generated `.env` in a local editor and fill the relevant fields:

```ini
DATABENTO_API_KEY=
MASSIVE_API_KEY=
```

The command appends missing empty fields, preserves existing values and sets
permissions to `0600`. It does not print values, check the network, download
data or place orders. `.env` is excluded from Git and public bundles. Run later
commands from that directory so the local environment is loaded.

Get keys from your own [Databento account](https://databento.com/portal/keys) or
[Massive dashboard](https://massive.com/dashboard). A key exposed in chat,
screenshots or public source should be revoked and replaced before use.
Never paste keys into the public Hugging Face Space. Its Connections page
exports setup profiles containing field names only.

## Check access

```sh
python -m dwight connection-check --platform databento
python -m dwight connection-check --platform massive
```

| Check | What success establishes | What remains unverified |
|---|---|---|
| Databento | Authentication to nonbillable dataset metadata | Dataset entitlement, QQQ minutes, live data |
| Massive | Authenticated QQQ reference metadata | Minute history, live data and account trading |

The checks use fixed HTTPS endpoints. Keys stay in authentication headers.
Redirects, environment proxies and automatic provider retries are disabled.
Results contain status and capabilities, without provider payloads or secrets.

## Download completed QQQ sessions

Dates below are illustrative. Choose a range your plan permits. Both adapters
request raw one-minute bars and produce private one-minute and five-minute CSVs,
optional Parquet files, source responses and a checksum manifest.

For Massive:

```sh
python -m dwight download-history --provider massive \
  --start 2025-11-28 --end 2025-11-28 --output private-data/massive
```

For Databento, choose the exact licensed dataset first. For example, `XNAS.ITCH`
is a dataset identity, not a synonym for consolidated Alpaca SIP coverage.
Inspect a nonbillable metadata estimate before authorizing a download:

```sh
python -m dwight history-estimate --dataset XNAS.ITCH \
  --start 2025-11-28 --end 2025-11-28
```

A download requires an explicit estimated-cost allowance. Only execute this
next command after accepting the provider's applicable charges:

```sh
python -m dwight download-history --provider databento --dataset XNAS.ITCH \
  --start 2025-11-28 --end 2025-11-28 --max-cost-usd 0.50 \
  --output private-data/databento
```

Dwight requests a fresh cost and record estimate, and refuses time-series
retrieval if the estimate exceeds the allowance. **This is not a provider billing
cap.** Actual charges depend on Databento's billing; a failed or interrupted
download may still be billed. Byte, record and time limits bound processing.
Dwight never retries these streams automatically. Filling a key or running a
connection check never invokes this download.

## Use the dataset in research

The download prints its new private directory. The existing
[experiment](experiments-and-reports.md) and [walk-forward](walkforward.md)
commands automatically find `manifest.json` beside `QQQ-5Min.csv`. An explicit
`dataset_manifest` configuration can select that same manifest when needed.

```sh
python -m dwight walkforward --data private-data/massive/DATASET/QQQ-5Min.csv \
  --out runs/massive-walkforward --real-data
```

Replace `DATASET` with the directory printed by the downloader. Enough complete
sessions are still required by the unchanged research sample gates.

Every regular session must be complete, including exchange early closes.
Missing minutes are rejected, never fabricated or forward filled. Failed runs
retain private evidence and `failed.json`, with no success manifest. The
research reader verifies every source-file checksum as well as the input CSV.

Databento and Massive retain their own feed and volume definitions. They are
marked **research only** and cannot qualify a release for the current Alpaca
shadow worker. Keep training and deployment feed definitions consistent.
These bar datasets contain no executable quotes or order-book depth.

Local fixtures verify code behavior. They do not prove a user's entitlement,
actual historical coverage, model value or commercial redistribution rights.
Licensed data stays outside GitHub and Hugging Face.

Official references: [Databento historical HTTP API](https://databento.com/docs/api-reference-historical?historical=http),
[Databento schemas](https://databento.com/docs/schemas-and-data-formats),
[Massive authentication](https://massive.com/docs/rest/quickstart),
[Massive QQQ reference metadata](https://massive.com/docs/rest/stocks/tickers/ticker-overview),
[Massive minute aggregates](https://massive.com/docs/rest/stocks/aggregates/custom-bars).
