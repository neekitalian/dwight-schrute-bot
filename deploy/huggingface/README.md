---
title: Dwight Schrute Bot
emoji: 🥬
colorFrom: green
colorTo: gray
sdk: gradio
sdk_version: 6.29.0
python_version: "3.11"
app_file: app.py
license: apache-2.0
short_description: QQQ research with interactive performance and model analysis
tags:
  - finance
  - research
  - synthetic-data
  - scikit-learn
  - plotly
---

# Dwight research

Explore a reproducible **synthetic** QQQ VWAP experiment through interactive performance charts,
a trade explorer, classifier diagnostics, replay evidence and proposed transformer connections.

**Prices are invented. No account is connected. No orders are submitted.**
The current research classifier is logistic regression. Price sequence and news transformer paths
are documented research proposals; no transformer is loaded, evaluated or credited with improvement.

## Explore the app

- **Start here:** four setup paths and a versioned source toolkit download. Directions
  keep private historical research and TradingView paper workflows off the public Space.
- **Performance:** simulated equity, drawdown, returns, session P&L and trade risk multiples.
  Compare VWAP, a simple volume filter and Dwight on the same later test sessions.
- **Walk-forward:** three chronological long-only tests, with training and validation dates,
  per-window P&L and a final 40-session period kept unscored. Runs only when requested.
- **Trade explorer:** select a session and policy to inspect candles, session VWAP, EMA20,
  long and short simulated entries, exits and the trade ledger. Times use New York.
- **Model analysis:** held out calibration and fitted logistic score contributions.
  These explain the classifier's behavior; they do not show causal feature importance.
- **Transformer connections:** both price forecasts and news sentiment as possible new features.
  Includes candidate model sources, limitations, licenses and a four arm evaluation plan.
- **Method and evidence:** chronological splits, replay checks, costs, model identity and boundaries.

The app evaluates all four declared fixtures: seeds 42, 43 and 44, plus seed 42 under doubled
commission and slippage. Each has 500 calendar days of invented bars. The cost case retrains
under changed costs, so it is neither an independent path nor pure cost attribution. Results
across these fixtures are software checks, not evidence of a QQQ trading edge.

The separate walk-forward view uses seed 42, expanding training windows and matching long-only
policies. Its reduced sample requirements test the software rather than establish statistical
power. Every window is shown, including weak results. Independent window results are not joined
into an account equity curve, and the final reserved period is never scored. This policy differs
from the Performance tab's long-and-short test, so their results are not interchangeable.

The first request trains and independently replays the policies on CPU, then checks them against
the saved evidence. Later requests reuse a bounded process memory cache. Temporary input files,
trade files and artifact files are removed; JSON results and model coefficients remain in memory
until restart. No model release is published. MLflow and persistent experiment storage are not
configured in the public Space; the private local workflow supports them.

## Deployment boundary

This app has no credential entry, user uploads, custom code, broker operation or model promotion
action. It does not ingest private journals or expose the milestone email campaign. The original
inputless `/synthetic_experiment` endpoint is retained; `/dashboard` accepts only the fixed cases
and evaluated strategy names. The inputless `/walkforward` endpoint evaluates the fixed window
plan. Public charts use generated data only.

A private 11-session official QQQ sample has been validated and replayed; it is too small to
qualify a model. Transformer performance, forward shadow observation and the complete paper
execution lifecycle remain unverified. Paper Trading by TradingView is a separate simulator;
the repository's private manual proposal and normalized fill import CLI does not place orders or
run in this public app. Native TradingView export compatibility still needs testing. Continuous
broker workers and milestone reporting belong on a separately supervised host with durable state.
The 12 hour, 24 hour, 48 hour and one week observation clocks have not started.

Spaces may sleep and restart. No GPU or hardware upgrade is needed for this demo.

Source and full workflow: [GitHub](https://github.com/neekitalian/dwight-schrute-bot).
The exact source revision and file hashes are in `source-manifest.json`.
The original project overview is retained as `PROJECT-README.md`.

## License

The application is Apache-2.0. The unchanged VWAP engine retains its upstream attribution
in `NOTICE` and `docs/VWAP-LICENSE`. Candidate transformer artifact licenses and market/news data
rights are separate. No third party model weights, licensed market data, private credentials
or experiment journals are bundled in this Space.
