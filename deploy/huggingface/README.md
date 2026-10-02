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
short_description: QQQ strategy research with a synthetic ML experiment
tags:
  - finance
  - research
  - synthetic-data
  - scikit-learn
---

# Dwight Schrute Bot — research lab

Run a reproducible **synthetic** demonstration of the QQQ VWAP research pipeline:
generate examples, fit a small classifier, select a threshold on validation sessions,
and compare the model against VWAP and a fixed volume filter on later sessions.

Prices are fabricated. The results test software behavior, not trading performance.
The demo cannot approve models for trading, connect accounts, or submit orders.
It accepts no API credentials, file uploads, or custom code.

The fixed example uses 500 calendar days and seed 42. The first request runs the
actual Python experiment; subsequent requests reuse its in-memory result until the
Space restarts. Temporary files are deleted. Hosted MLflow tracking and persistent
experiment storage are not configured here; the full local workflow supports MLflow.

Source and full workflow: [GitHub](https://github.com/neekitalian/dwight-schrute-bot).
The exact source revision and file hashes are in `source-manifest.json`.
The repository's original overview is retained as `PROJECT-README.md`.

## Deployment boundary

This Space is an interactive research app. Actual QQQ history, live shadow operation,
and the complete paper execution lifecycle still require validation. The continuous
broker-connected worker belongs on a separately supervised host with durable state.
Spaces may sleep and restart; local state is ephemeral.

## License

Apache-2.0. The unchanged VWAP engine retains its upstream attribution in `NOTICE`
and `docs/VWAP-LICENSE`. Market-data rights are separate; no licensed market dataset
or private experiment journal is included in this Space.
