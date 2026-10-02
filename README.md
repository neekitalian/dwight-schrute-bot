# Dwight Schrute Bot

A paper-first framework for reproducible trading research. The first integrated strategy is the existing **VWAP + market-structure pullback bot**, pinned to its source commit. Model research is separate from deterministic execution.

**Working now:** historical CSV replay, the VWAP engine, SQLite run journal, input/config/code fingerprints, optional public Polymarket market discovery, and a provider-independent research escalation interface.

**Not connected:** broker paper accounts, live order execution, trained ML models, or a Polymarket fill simulator. Synthetic demo results are software checks, not strategy performance evidence.

## Quick start

Python 3.11+; the core has no third-party runtime dependencies.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python examples/make_demo.py
dwight strategies
dwight replay examples/synthetic.csv --symbol SYNTHETIC
python -m unittest discover -s tests -v
```

Without installing, use `python3 -m dwight` in place of `dwight`.

```sh
dwight replay /path/to/qqq.csv --symbol QQQ --config examples/config.json
# Optional network read; no wallet or API key required:
dwight polymarket-discover --limit 5
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
vwap_bot/                  unchanged upstream replay engine
examples/                  config and synthetic-data generator
tests/                     strategy and framework regression tests
docs/                      architecture, strategy semantics, attribution
```

The VWAP engine currently owns its strategy-specific simulated fills and limits. It is not yet a broker-neutral signal generator: introducing an external executor requires separating signal generation from fill/account state and proving replay parity. Do not route its historical trade records directly to a broker.

The research cascade labels evidence as relevant/irrelevant or abstains. It cannot produce executable orders. Thresholds require calibration; confidence is not expected return. Concrete Jev/Kimi providers are deliberately not bundled until account access, model versions and evaluation requirements are established.

## Development and deployment

A ready-to-enable GitHub Actions workflow is provided at `examples/github-actions-tests.yml`; move it to `.github/workflows/tests.yml` using a credential with workflow permission. It installs the package and runs tests plus synthetic replay on Python 3.11–3.13. It is not active yet. Docker packaging is provided for local use; `docker build -t dwight .` then `docker run --rm dwight` lists strategies. A persistent service and scheduler are not included.

See [architecture and next milestones](docs/architecture.md). No credentials are required for the default workflow. Secrets, private datasets, run records and wallet keys must never be committed. No live-mode switch exists.

## Provenance and license

Apache License 2.0, preserving this repository's original license. VWAP source is vendored unchanged from [codex/vwap-paper-bot](https://github.com/neekitalian/VWAP-Trading-Strategy/tree/codex/vwap-paper-bot), commit `532618ed5b415c974bb5be3da3330cd638c38931`. See [NOTICE](NOTICE) and [retained upstream license](docs/VWAP-LICENSE). Updates should be reviewed against that pinned source and tested for parity.

Framework ideas were informed by [FriesTrader](https://github.com/YizhiSong/FriesTrader), [TradingAgents](https://github.com/TauricResearch/TradingAgents), and [NautilusTrader](https://github.com/nautechsystems/nautilus_trader). Those projects are not vendored or runtime dependencies. Future dependencies retain their own licenses; data-provider rights are separate from code licensing.
