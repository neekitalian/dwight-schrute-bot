# Dwight toolkit product boundary

Dwight 0.4.0 is a small research preview for one QQQ strategy and one private workspace. Its useful product is the repeatable process: prepare data, replay a baseline, test added features, inspect evidence, connect observations and keep a manual paper journal.

The downloadable toolkit contains source, configuration templates, a workspace initializer, data validation, research commands, private reports, a TradingView observation inbox and deployment runbooks. The Hugging Face app is a public demonstration using synthetic data. Neither package includes a proven profitable model or a managed brokerage account.

## License and deployment scope

The reusable core uses Apache 2.0. Retain the license and attribution notices when distributing or modifying it. [Apache license](https://www.apache.org/licenses/LICENSE-2.0)

This release has no tenant isolation, customer authentication, uptime commitment or managed execution service. Use a private workspace for each installation and account.

Market-data redistribution rights and model-weight licenses remain separate. Keep data and credentials in the private deployment. Code licensing does not grant rights to package a vendor's market dataset.

## Supported user flow

1. Explore synthetic examples in the public app.
2. Install the versioned toolkit and create a private workspace.
3. Download permitted QQQ history, validate provenance and inspect baseline results.
4. Run fixed chronological feature comparisons. Keep negative and insufficient results visible.
5. Configure a private TradingView observation inbox and verify actual alert delivery.
6. Review proposals and manually place orders in TradingView's native paper account.
7. Import normalized executions and review discrepancies before any broader integration.

An Alpaca API paper account is a separate future execution route. Native TradingView paper automation is not implemented or advertised. The inbox cannot turn a received alert into an order or a recorded fill.

## Release claims and acceptance

Release checks should establish that a clean source bundle installs, a workspace initializes, historical replay is reproducible, malformed or duplicated alerts are handled safely, and the public demo runs the declared version. They cannot establish profitability or replace a forward observation period.

The product can be extended with strategy and model adapters, but each addition needs an explicit feature schema, data version, cost model, chronological evaluation and deployment gate. Price transformers, news models and FinRL remain optional research directions until measured evidence exists.
