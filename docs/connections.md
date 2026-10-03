# Platform connections

The Connections page distinguishes working Dwight checks from setup guidance.
It can check public crypto prices and public prediction-market discovery. The
Alpaca, Databento and Massive checks run only in your private runtime. The public Hugging Face
Space has **no authenticated account connection flow**: do not enter API keys,
passwords, OAuth tokens, wallet secrets, or account exports there.

Every exported profile uses `mode: research_read_only` and
`execution_enabled: false`. It contains platform settings and credential
**environment-variable names**, never credential values. Generating a profile
does not open an account, complete OAuth, connect a wallet, or place an order.

| Platform ID | Working Dwight support | Account integration |
|---|---|---|
| `tradingview` | QQQ chart, private manual paper journal, normalized fill import; webhook signal route is a future role | Native account manual only; no account API or order submission |
| `alpaca` | Private paper-account read check and explicit SIP/IEX QQQ data check; existing stock research | Candidate first broker paper route; current order lifecycle is incomplete and must not run unattended |
| `ibkr` | Setup guidance and a profile | Market data and paper broker adapter planned |
| `schwab` | Setup guidance and a profile | OAuth, account and broker adapter planned; paper availability is not claimed |
| `coinbase` | Public BTC-USD spot-price check | No authenticated account integration; future Advanced Trade adapter |
| `binance` | Public BTCUSDT Spot price check | No authenticated account or testnet integration; future testnet adapter |
| `kraken` | Public XBTUSD Spot ticker check | No authenticated account integration; future adapter |
| `polymarket` | Public market discovery; existing public book snapshot recording | Research only; no wallet or authenticated trading integration |
| `databento` | Private metadata check, cost estimate and bounded QQQ history | Market data only; requires explicit dataset and download cost allowance |
| `massive` | Private QQQ reference check and bounded minute history | Market data only; coverage depends on your data plan |

The product separates **chart**, **signal source**, **market data** and
**execution account**. A TradingView alert may eventually reach Dwight's private
webhook and be validated before routing to an independently selected broker
paper account. That route would not place an order in TradingView's own Paper
Trading account. Each profile reports integration roles, account-access state
and execution status separately; `execution_enabled` remains false for every
profile.

Current source uses a shared, versioned capability record for history, live
observations, paper execution and real-money execution. Each reports Dwight's
implementation state and its software verification separately. Public ticker
checks do not establish a continuous feed, and a software test does not establish
any customer's entitlement or account permission. No catalog entry is execution
ready. The private [paper authorization ledger](paper-authorization.md) records
the narrower account and strategy scope; a setup profile never creates that grant.

Checks describe only the requests actually made. The result reports capabilities,
status, timestamp, and a message; it does not expose raw prices or account data.
`public_data_available` means the public response passed validation, not that an
account is connected. A successful public response does not verify account
access, region eligibility, data entitlements, strategy readiness, or a fill.
Public prices are observations, not executable quotes. The
QQQ VWAP stock strategy has **not** been ported or validated for crypto.

## Command line

List platform capabilities, export a credential-free profile, and check public
data access:

```bash
python -m dwight connections
python -m dwight connection-profile coinbase --output ./dwight-coinbase.json
python -m dwight connection-check --profile ./dwight-coinbase.json
```

A profile is optional when running a check directly:

```bash
python -m dwight connection-check --platform kraken
```

For Alpaca, first configure paper credentials in your private runtime, then
select the intended stock feed explicitly:

```bash
python -m dwight connection-profile alpaca --feed sip --output ./dwight-alpaca.json
python -m dwight connection-check --profile ./dwight-alpaca.json
# Or select the platform and feed directly:
python -m dwight connection-check --platform alpaca --feed sip
```

Use either `--profile FILE` or `--platform PLATFORM`. `--feed` accepts `sip` or
`iex` and applies to Alpaca. These commands do not place orders. TradingView
returns manual-workflow guidance; IBKR and Schwab remain planned adapters.

For Databento or Massive, prepare private key fields with
`python -m dwight prepare-data-keys`. Fill your local `.env`, then run
`python -m dwight connection-check --platform databento` or `--platform massive`.
Follow [market data connections](data-providers.md) for history downloads and
Databento cost controls. These provider checks do not run in the public Space.

## Stocks

### TradingView

Open Supercharts, choose **Trade**, then **Paper Trading → Connect** in
[TradingView's paper setup](https://www.tradingview.com/support/solutions/43000516466-paper-trading-main-functionality/).
Dwight's [manual paper workflow](manual-paper.md) journals QQQ proposals and
normalized execution evidence privately; a person places every native paper
order in TradingView. The journal's CSV contract is not a verified adapter for
native TradingView exports. [Pine strategies cannot place orders in the built-in
paper account](https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account).

### Alpaca

Generate **paper** API keys and keep `APCA_API_KEY_ID` and
`APCA_API_SECRET_KEY` in the private runtime used by Dwight. Alpaca's
[paper environment](https://docs.alpaca.markets/docs/paper-trading) uses a
separate account endpoint and credentials; the check is fixed to that endpoint.
See the [private setup workflow](workflow.md) for environment-file handling.

Choose `sip` or `iex` explicitly. A failed SIP request does not fall back to IEX.
Alpaca documents [different coverage and entitlements for these feeds](https://docs.alpaca.markets/docs/market-data-faq);
historical access does not establish current SIP access. Changing the volume
feed changes a VWAP experiment and requires matching research and observation
configuration. A connection check grants no strategy approval and enables no
orders; the broker execution lifecycle is still unfinished.

### Interactive Brokers

Dwight's IBKR adapter is planned. The platform's [TWS API supports TWS or
IB Gateway and paper sessions](https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/).
A future private implementation needs account/data permissions and a locally
configured gateway; retain read-only API settings during initial integration.
The [Client Portal Gateway for IBKR's Web API](https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/)
is a different authentication path from IB Gateway. Exporting an IBKR profile
does not install, authenticate, or check either gateway.

### Charles Schwab

Dwight's Schwab account and OAuth adapter is planned. Start with the official
[Trader API - Individual portal](https://developer.schwab.com/products/trader-api--individual)
for current application registration, callback, authorization, and access
requirements. A future private integration must complete Schwab's OAuth flow
and store secrets/tokens privately. This release makes no claim of a Schwab
paper API or a thinkorswim paperMoney API integration. Its profile performs no
authentication or account check.

## Crypto

### Coinbase

The working probe uses Coinbase App's unauthenticated
[BTC-USD spot-price endpoint](https://docs.cdp.coinbase.com/coinbase-app/track-apis/prices).
It reads an indicative price, not an Advanced Trade account, order book,
balance, or executable order quote. No API key is needed.

Coinbase Advanced Trade is a separate future account-integration path. Its
[sandbox returns static, predefined responses](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox).
Those mocks can exercise request/response handling; they do not establish
realistic paper fills or strategy performance.

### Binance

The probe reads `BTCUSDT` from Binance Spot's
[market-data-only service](https://developers.binance.com/en/docs/products/spot/faqs/market_data_only),
without credentials. The quote currency is **USDT**, not USD. Regional Binance
entities have their own eligibility and APIs; [Binance.US documentation](https://docs.binance.us/)
is separate from Binance.com. A public response does not verify account eligibility.

Binance provides a separate [Spot Test Network](https://developers.binance.com/en/docs/products/spot/testnet/general-info)
with virtual assets and separate credentials. It supports `/api`, not `/sapi`.
Dwight's public price probe does not connect that testnet, sign requests, or
submit orders.

### Kraken

The probe reads the public [Spot ticker](https://docs.kraken.com/api-reference/market-data/get-ticker-information)
for `XBTUSD` (bitcoin/USD). This is a last-trade market observation, not a
balance or account check. No API key is needed.

The documented [Kraken demo environment](https://docs.kraken.com/exchange/guides/futures/introduction)
at `demo-futures.kraken.com` is for Futures and is separate from production.
It should not be represented as a Kraken Spot paper account. Dwight's Spot
probe has no futures, wallet, or order integration.

## Prediction markets

The Polymarket check reads public market metadata through the
[Gamma discovery API](https://docs.polymarket.com/api-reference/markets/list-markets).
Dwight also has bounded public order-book snapshot recording, as described in
the repository's research workflow. Discovery and order books are separate
[API capabilities](https://docs.polymarket.com/api-reference/predictions/overview):
discovery success alone does not validate prices. No wallet, signing,
transaction, or authenticated trading integration is enabled. Public snapshots
do not form a complete historical tick archive.

## Profile API

```python
from dwight.connection_catalog import PLATFORMS, build_profile

profile = build_profile("alpaca", feed="sip")
# {'schema_version': 1, 'platform': 'alpaca',
#  'mode': 'research_read_only', 'execution_enabled': False,
#  'instrument': 'QQQ',
#  'credential_env_names': ['APCA_API_KEY_ID', 'APCA_API_SECRET_KEY'],
#  'integration_roles': ['market_data', 'paper_broker_candidate'],
#  'account_access': 'private_paper_read_check',
#  'execution_status': 'paper_client_incomplete',
#  'feed': 'sip'}

public_profile = build_profile("coinbase")
# instrument: BTC-USD; credential_env_names: []
```

Supported IDs are the `PLATFORMS` keys listed in the table. Invalid platform IDs
and feeds are rejected. Only Alpaca consumes private credential variables;
planned adapters and public probes export an empty credential-name list. The
catalog and profile builder make no network requests, read no secrets, and
accept no arbitrary endpoint URL.

## Readiness gates for the multi-platform route

The web console should not show a single green “connected” state. It should
show separate results for account identity, market-data entitlement, selected
paper mode, strategy compatibility, order permissions, protective-order
support and recovery. Only the complete route can become pilot-ready. A
public-price probe or a successful login is not an execution check.

For TradingView webhooks, the alert is a candidate input. Validate its fixed
schema, configured symbol and interval, completed-bar time, age, unique event
identity and replay status. Keep secrets out of alert content, reject unknown
actions, then fetch current data and broker state before applying Dwight’s own
risk rules. Never trust the alert’s requested quantity as the final order size.
TradingView webhooks send HTTP requests to an external endpoint, but do not
provide an API for Pine strategies to place orders in the built-in paper
account. See the [webhook setup guide](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)
and [Pine strategy limitations](https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account).

The first automated route remains Alpaca Paper for QQQ only, after completion
of the unfinished partial-fill protection, session exit, reconnect recovery and
broker-derived reporting work described in
[`automatic-paper-product.md`](automatic-paper-product.md). All other
platforms are explicitly staged; crypto and prediction-market execution need
their own strategy, sizing and settlement model.
