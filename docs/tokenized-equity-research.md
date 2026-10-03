# QQQ and QQQx research

Status: a read-only collection and offline comparison workflow on `main`. It is
not included in the existing 0.7.1 release archive. No account connection, wallet,
token order submission or transfer is implemented by this workflow.

## Use the comparison

Install the current source with the data extra in a private environment:

```sh
python -m pip install '.[data]'
python -m dwight collect-tokenized --out private-data/qqqx/new-snapshot
python -m dwight compare-tokenized \
  --qqq-manifest private-data/massive/YOUR_DATASET/manifest.json \
  --token-snapshot private-data/qqqx/new-snapshot/snapshot.json \
  --out runs/tokenized/new-comparison
```

Use new output directories each time. Collection makes five bounded public
Kraken GET requests, without reading credentials. Comparison is offline and
does not load `.env`. Open the private `report.html` in your browser. The report
contains charts with hover inspection and keyboard sliders, coverage counts,
visible-depth estimates and expandable source notes. `report.json`, frozen input
metadata and file checksums are saved alongside it. Files are created with
private permissions and existing results are never replaced.

Supply an existing Dwight **raw-adjusted QQQ** dataset from Alpaca, Databento or
Massive. The manifest, source-file checksums and exchange sessions are checked.
Known split-adjusted samples are rejected, so a retrospectively adjusted share
price is not silently compared with a token's reported price. Local integrity
checks do not authenticate the provider or establish redistribution rights.

Kraken's public OHLC endpoint exposes recent rolling history and an unfinished
last interval. Dwight discards that final interval and checks completion times.
At five minutes the limit is approximately 60 clock hours, with up to 720
completed intervals; this is not a long-term token dataset. Missing intervals
are not invented. [Kraken OHLC API](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)

## Keep the instruments distinct

The collector verifies Kraken's exact `QQQxUSD` pair, `QQQx` base, `ZUSD` quote,
tokenized asset class, online status and venue metadata. The normalized report
labels the quote USD while preserving the provider identity. It never substitutes
ordinary QQQ, guesses a contract address or treats a public quote as account
access. An onchain adapter would need its own verified chain and contract.
[Kraken AssetPairs API](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)

QQQx offers price exposure under its issuer's terms. It does not confer the
same shareholder rights as the underlying asset. Kraken documents quantity
adjustments through a corporate-action multiplier. `lot_multiplier` in an API
pair response does not prove that issuer conversion. The collector therefore
records the conversion as unverified and reports quantities as **Kraken-reported
QQQx units**, not shares. Price differences are indicative and unadjusted; they
are not a certified economic premium or an arbitrage opportunity.
[Kraken xStocks FAQ](https://support.kraken.com/articles/xstocks-faq)

## Read the evidence

| Report | Interpretation |
| --- | --- |
| Matching five-minute closes | Join exact completed UTC intervals during QQQ regular sessions |
| Price difference | `(QQQx close / QQQ close - 1) * 10000`, in basis points, without an issuer conversion |
| Bar VWAP | Typical price times each source's own volume; a bar approximation, not consolidated or trade-level VWAP |
| Coverage | Matching intervals, gaps and token intervals without a QQQ reference |
| Order-book spread | A QQQx snapshot at its recorded receive time |
| Book sweeps | Hypothetical buys and sells of 1, 10 and 100 QQQx units against visible depth |

Historical bar closes are provider interval observations, not synchronized
bid and ask quotes. A zero-volume interval can carry an earlier price or a
provider mark. The report separates all reported closes from intervals with
positive volume on both sides, and counts matched token bars with zero volume.
Positive volume still does not establish simultaneous trade times.
Their timestamps allow a bar comparison, not a simultaneous
executable spread. Public endpoint requests also arrive at different times:
the collector preserves each response receive time and checksum. No forward
fill is used when QQQ is closed or reference data is missing.

VWAP resets by regular exchange session using each instrument's own volume.
Missing opening intervals make a complete session-to-date VWAP unavailable.
Token activity outside QQQ regular sessions is counted separately and is not
assigned an underlying price from a closed stock market.

Depth sweeps assume displayed liquidity remains available. They do not model
latency, queue priority, fees, hidden liquidity or actual fills. A size exceeding
visible depth has an unknown full-size average. The current underlying bar
dataset has no executable QQQ quotes, so an executable cross-market spread is
unknown. This report contains no strategy PnL, account equity or model training.

## Before any execution extension

Keep token market research separate from the existing QQQ strategy and paper
authorization. Execution would require independently verified economic units,
account eligibility, venue-specific data and session rules, fees, order and fill
accounting, and a new bounded instrument authorization. No stock grant allows a
QQQx order. No connector capability is upgraded merely because collection works.

Provider availability is account- and region-specific. Kraken lists excluded
jurisdictions and separate EEA restrictions for its order books and API. Public
market data does not prove that a customer may trade that market.
[Kraken availability](https://support.kraken.com/gb/articles/xstocks-availability)

Keep licensed QQQ prices, token snapshots and generated reports private.
Public demos may use explicitly labelled synthetic fixtures. Research software
verification does not establish trading performance or account eligibility.
