"""Platform capabilities and credential-free research connection profiles.

This module performs no I/O, reads no environment variables, and authenticates
no account. Platform capabilities are distinct from implemented Dwight support.
"""

PLATFORMS = {
    "tradingview": {
        "id": "tradingview",
        "name": "TradingView",
        "category": "stocks",
        "integration_roles": ["chart", "signal_source_future", "native_paper_manual"],
        "account_access": "manual_only",
        "execution_status": "native_account_manual_only",
        "status_label": "Manual paper workflow",
        "summary": "Review QQQ proposals and journal normalized paper fills locally. Place native TradingView paper orders yourself.",
        "setup_steps": [
            "In TradingView Supercharts, open Trade and connect Paper Trading.",
            "Review the QQQ chart and your paper account, then use Dwight's private manual-paper journal.",
            "Place any paper order in TradingView and import normalized execution evidence locally.",
        ],
        "limitations": [
            "Dwight does not log into TradingView, read its account, or submit orders.",
            "Pine strategies cannot place orders in TradingView's built-in Paper Trading account; alerts do not change this.",
            "The current journal supports QQQ long inventory, and its CSV is a normalized interchange format, not a verified native export adapter.",
        ],
        "docs": [
            {"label": "TradingView paper setup", "url": "https://www.tradingview.com/support/solutions/43000516466-paper-trading-main-functionality/"},
            {"label": "Pine strategy and paper-account limitations", "url": "https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account"},
        ],
        "check_kind": "manual",
    },
    "alpaca": {
        "id": "alpaca",
        "name": "Alpaca",
        "category": "stocks",
        "integration_roles": ["market_data", "paper_broker_candidate"],
        "account_access": "private_paper_read_check",
        "execution_status": "paper_client_incomplete",
        "status_label": "Private read-only check",
        "summary": "Check an Alpaca paper account and QQQ market-data access from your private runtime. Existing QQQ research and observation use Alpaca data.",
        "setup_steps": [
            "Generate credentials for an Alpaca paper account, which uses separate keys from a live account.",
            "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY only in your private local environment or private deployment.",
            "Choose sip or iex explicitly and run the private connection check. Use the same feed for research and observation.",
        ],
        "limitations": [
            "The public Hugging Face Space has no authenticated account connection flow and must not receive your credentials.",
            "A successful check verifies only the requested read access; it does not approve a strategy or enable order execution.",
            "Historical SIP access does not establish real-time SIP entitlement. The check does not silently switch to IEX.",
            "The connection profile remains research_read_only with execution disabled; the broker execution lifecycle remains unfinished.",
        ],
        "docs": [
            {"label": "Alpaca paper accounts", "url": "https://docs.alpaca.markets/docs/paper-trading"},
            {"label": "Alpaca API authentication", "url": "https://docs.alpaca.markets/docs/authentication-1"},
            {"label": "SIP and IEX market data", "url": "https://docs.alpaca.markets/docs/market-data-faq"},
        ],
        "check_kind": "private",
    },
    "ibkr": {
        "id": "ibkr",
        "name": "Interactive Brokers",
        "category": "stocks",
        "integration_roles": ["market_data_future", "paper_broker_future"],
        "account_access": "planned",
        "execution_status": "planned",
        "status_label": "Adapter planned",
        "summary": "Setup guidance only. Dwight has no implemented IBKR account or market-data adapter.",
        "setup_steps": [
            "Review IBKR's API requirements and account/data permissions in its official documentation.",
            "For a future TWS API adapter, install Trader Workstation or IB Gateway and select the paper session.",
            "Keep the API in read-only mode while developing a private adapter; the current Dwight profile performs no IBKR connection.",
        ],
        "limitations": [
            "IBKR supports TWS API paper sessions through TWS or IB Gateway, but that platform capability is not implemented in Dwight.",
            "The Client Portal Gateway is a separate Web API authentication path; it is not the same product as IB Gateway.",
            "No credentials, gateway login, account verification, or order execution are available in the public Space.",
        ],
        "docs": [
            {"label": "Configure TWS or IB Gateway for the API", "url": "https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/"},
            {"label": "IBKR Web API and authentication", "url": "https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/"},
        ],
        "check_kind": "planned",
    },
    "schwab": {
        "id": "schwab",
        "name": "Charles Schwab",
        "category": "stocks",
        "integration_roles": ["market_data_future", "broker_future"],
        "account_access": "planned",
        "execution_status": "planned",
        "status_label": "Adapter planned",
        "summary": "Setup guidance only. Dwight has no implemented Schwab OAuth, account, or market-data adapter.",
        "setup_steps": [
            "Review Trader API - Individual in the Schwab Developer Portal and its current access requirements.",
            "A future private adapter needs a registered application, its approved callback, and Schwab's OAuth authorization flow.",
            "Keep application secrets and tokens in a private runtime. The current Dwight profile does not start OAuth or connect an account.",
        ],
        "limitations": [
            "Developer registration and platform API availability do not mean a Dwight integration is complete.",
            "No Schwab paper API or thinkorswim paperMoney API integration is claimed here.",
            "There is no authenticated Schwab flow in the public Hugging Face Space.",
        ],
        "docs": [
            {"label": "Schwab Trader API - Individual", "url": "https://developer.schwab.com/products/trader-api--individual"},
            {"label": "Schwab Developer Portal", "url": "https://developer.schwab.com/"},
        ],
        "check_kind": "planned",
    },
    "coinbase": {
        "id": "coinbase",
        "name": "Coinbase",
        "category": "crypto",
        "integration_roles": ["public_market_data"],
        "account_access": "not_connected",
        "execution_status": "planned",
        "status_label": "Public BTC-USD price check",
        "summary": "Read Coinbase's public BTC-USD spot price without credentials. This checks public data access, not an Advanced Trade account.",
        "setup_steps": [
            "Run the public data check; the Coinbase App spot-price endpoint requires no API key.",
            "Review the access-check status: Dwight validates the public price response and reports access, without displaying raw prices.",
            "For future private Advanced Trade integration, follow Coinbase's separate account authentication and API documentation.",
        ],
        "limitations": [
            "Dwight does not authenticate Coinbase accounts, read balances, submit orders, or connect wallets.",
            "Advanced Trade's sandbox returns static, predefined responses; it is not a realistic paper trading engine.",
            "The QQQ VWAP strategy has not been ported or validated for crypto. Public price access is not strategy support.",
        ],
        "docs": [
            {"label": "Public Coinbase spot prices", "url": "https://docs.cdp.coinbase.com/coinbase-app/track-apis/prices"},
            {"label": "Advanced Trade overview", "url": "https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/overview"},
            {"label": "Advanced Trade static sandbox", "url": "https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox"},
        ],
        "check_kind": "public",
    },
    "binance": {
        "id": "binance",
        "name": "Binance",
        "category": "crypto",
        "integration_roles": ["public_market_data", "spot_testnet_future"],
        "account_access": "not_connected",
        "execution_status": "planned",
        "status_label": "Public BTCUSDT price check",
        "summary": "Read Binance Spot's public BTCUSDT price through its market-data-only service, without an account or API key.",
        "setup_steps": [
            "Run the public data check against Binance's fixed market-data-only service.",
            "Use the official documentation for the Binance entity available to you; Binance.com and regional services such as Binance.US are separate APIs.",
            "If developing a future Spot adapter, review the separate Spot Test Network with virtual assets and testnet credentials.",
        ],
        "limitations": [
            "A public BTCUSDT price check is not account authentication or proof that a platform is available in your region.",
            "The Binance.com Spot Test Network supports /api endpoints, not /sapi; Dwight does not connect a testnet account.",
            "BTCUSDT is quoted in USDT, not USD. The QQQ VWAP strategy is not ported or validated for this market.",
            "Dwight has no Binance order, balance, withdrawal, or wallet integration.",
        ],
        "docs": [
            {"label": "Binance market-data-only service", "url": "https://developers.binance.com/en/docs/products/spot/faqs/market_data_only"},
            {"label": "Binance Spot Test Network", "url": "https://developers.binance.com/en/docs/products/spot/testnet/general-info"},
            {"label": "Binance.US API documentation", "url": "https://docs.binance.us/"},
        ],
        "check_kind": "public",
    },
    "kraken": {
        "id": "kraken",
        "name": "Kraken",
        "category": "crypto",
        "integration_roles": ["public_market_data"],
        "account_access": "not_connected",
        "execution_status": "planned",
        "status_label": "Public XBTUSD ticker check",
        "summary": "Read the Kraken Spot public XBTUSD ticker without credentials. Kraken's XBT notation denotes bitcoin.",
        "setup_steps": [
            "Run the public Spot ticker check; no API key or account is needed.",
            "Interpret XBTUSD as bitcoin priced in USD; review the response-validation status and check timestamp.",
            "For future private integration, consult the Spot API documentation; Kraken's Futures demo is a separate product.",
        ],
        "limitations": [
            "The public ticker check does not authenticate a Kraken account or read balances.",
            "The documented demo-futures.kraken.com environment belongs to Futures; this Spot check does not use or establish a Spot paper account.",
            "Dwight has no Kraken orders, wallet integration, or validated crypto version of its QQQ VWAP strategy.",
        ],
        "docs": [
            {"label": "Kraken Spot ticker", "url": "https://docs.kraken.com/api-reference/market-data/get-ticker-information"},
            {"label": "Kraken Futures demo and API environments", "url": "https://docs.kraken.com/exchange/guides/futures/introduction"},
        ],
        "check_kind": "public",
    },
    "polymarket": {
        "id": "polymarket",
        "name": "Polymarket",
        "category": "prediction_markets",
        "integration_roles": ["public_market_discovery", "public_orderbook_research"],
        "account_access": "not_connected",
        "execution_status": "not_available",
        "status_label": "Public market discovery",
        "summary": "Read public market metadata. Dwight also has bounded public order-book snapshot recording for prediction-market research.",
        "setup_steps": [
            "Run the public discovery check without wallet credentials.",
            "Review market definitions and resolution rules before using metadata in research.",
            "Use the existing public snapshot recorder when order-book evidence is needed, retaining observation and receipt timestamps.",
        ],
        "limitations": [
            "Market discovery is not a price quote, account connection, order execution, or evidence of market eligibility.",
            "Dwight does not connect wallets or sign transactions; public snapshots are not a complete historical tick archive.",
            "Prediction markets do not use the QQQ stock strategy or a BTC spot instrument.",
        ],
        "docs": [
            {"label": "Polymarket API overview", "url": "https://docs.polymarket.com/api-reference/predictions/overview"},
            {"label": "Public market discovery", "url": "https://docs.polymarket.com/api-reference/markets/list-markets"},
        ],
        "check_kind": "public",
    },
}


def build_profile(platform: str, feed: str = "sip") -> dict:
    """Build a serializable research profile containing no credential values.

    Profiles describe the selected workflow; they do not connect or authorize
    an account. Only Alpaca currently consumes private credential variables.
    All endpoints remain fixed in the read-only connection-check layer.
    """
    if not isinstance(platform, str) or platform not in PLATFORMS:
        raise ValueError("Unknown platform; select one of: " + ", ".join(PLATFORMS))
    if not isinstance(feed, str) or feed not in {"sip", "iex"}:
        raise ValueError("feed must be explicitly sip or iex")
    instruments = {"coinbase": "BTC-USD", "binance": "BTCUSDT",
                   "kraken": "XBTUSD", "polymarket": "prediction_market"}
    profile = {
        "schema_version": 1,
        "platform": platform,
        "mode": "research_read_only",
        "execution_enabled": False,
        "instrument": instruments.get(platform, "QQQ"),
        "credential_env_names": [],
        "integration_roles": list(PLATFORMS[platform]["integration_roles"]),
        "account_access": PLATFORMS[platform]["account_access"],
        "execution_status": PLATFORMS[platform]["execution_status"],
    }
    if platform == "alpaca":
        profile["credential_env_names"] = ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"]
        profile["feed"] = feed
    return profile
