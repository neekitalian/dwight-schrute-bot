# Hugging Face research Space

The public app is [neekthekid/dwight-schrute-bot](https://huggingface.co/spaces/neekthekid/dwight-schrute-bot).
This is the research interface, not the always-on paper trading worker.
The app runs the existing experiment on four fixed synthetic cases. It compares
the baseline, volume filter and classifier on a later chronological partition.
All results are labelled synthetic and ineligible for deployment.

No credential is needed by the running app. It has no credential entry, file
upload, broker operation, or custom-code input. The first request computes a
result; the process holds up to four dashboard cases and the compact legacy report in memory. Temporary data is
cleaned up. MLflow logging is disabled in this ephemeral demo; use the documented
local workflow for private datasets, persistent artifacts and MLflow tracking.

## Verified deployment

The first deployment reached RUNNING on CPU Basic. The public browser and API
both completed the actual synthetic experiment. Its trade counts and strategy
metrics matched the local run. Mobile layout and both interface tabs were checked.

- Application source: `4adcb8ba78bdcee52d7fcfe2c9c79da8218ce3e3`
- Space commit: `23c907debc0f5356106705a8dd2b153711b269db`
- Hosted model fingerprint: `d7e728a13c7bfcdd851779971f059db3104707682eb4dde5c0babc80ed7b6c3e`

Independent synthetic generation and training on different platforms can produce
different floating-point bytes and fingerprints. Each report retains its own
input/model identity; cross-platform checks compare behavior numerically, not
binary model equality. Existing frozen-release checksum enforcement is unchanged.
These hosted results remain synthetic and do not establish a trading edge.

The reporting framework refresh also reached RUNNING and completed the hosted
synthetic callback. Its committed source is `00a2392ab36e93fce678c086d76246f98a8c80dc`
and Space commit is `d917637855eeda801fef23aa4acd66c4b9ceb35c`. The served app
shows the revised TradingView monitoring wording. The public bundle contains 39
allowlisted files. Private experiment reports, campaign recipients and credentials
were not uploaded. The interface remains the fixed synthetic demo; the new
milestone reports run privately on the experiment host.

## Interactive analysis

The dashboard evaluates seeds 42, 43 and 44, plus path 42 with doubled costs.
All use the same 500 calendar day fixture generator. Equity, bar close drawdown,
trade ledgers, calibration and score contributions come from actual independent
policy replays. Every dashboard ledger and metric must match saved test evidence
before it can be shown. The cost case retrains and retunes the model; it is not
pure cost attribution or an independent price path.

The performance page shows all declared cases, including cases where the model
trails VWAP. Candles and simulated fills use explicit New York timestamps.
Drawdown is sampled at bar closes and cannot represent intrabar extremes. The
model analysis explains logistic score contributions, not causal effects.

Price sequence and news connections are both documented in the interface and
[transformer research plan](transformer-research.md). All are proposed and
unmeasured. The registry includes official model sources and distinguishes code
licenses from model artifact licenses. No transformer SDK or weights are loaded.
Adding these features requires a versioned schema, new preprocessing and retraining.

The dashboard has no upload, private run reader or account connection. Bounded JSON
caches retain public synthetic bars, ledgers and model coefficients; temporary files
are deleted. The legacy inputless API remains available. The current native
TradingView paper account requires a future manual proposal and result import flow;
this dashboard does not connect to that account or start milestone observation.

## Local development

Use a separate virtual environment to avoid changing the validated trading CLI
environment. From the repository root:

```sh
python3 -m venv build/space-venv
build/space-venv/bin/python -m pip install -r deploy/huggingface/requirements.txt
build/space-venv/bin/python -m deploy.huggingface.app
```

The app listens on port 7860. For a loopback-only preview, import and call `launch_app(server_name="127.0.0.1", server_port=7861)` to preview with the same theme and styles.

## Reproducible deployment

Commit the intended source first. Staging reads **committed Git blobs** from a
fixed allowlist, never an entire working directory. Broker credentials, `.env`,
private history, local journals, models, virtual environments and `.git` are not
included. It retains license/attribution and emits a source manifest with the
Git revision and file hashes. The chosen staging destination must not exist.

```sh
python3 scripts/build_hf_space.py --output build/hf-space
hf auth login
hf repos create neekthekid/dwight-schrute-bot --type space --space-sdk gradio
hf upload neekthekid/dwight-schrute-bot build/hf-space . --type space
hf spaces info neekthekid/dwight-schrute-bot
```

Use the Hugging Face CLI's local interactive login; do not put a token into chat,
source files, CLI arguments, or the Space runtime. Reuse an existing Space after
checking its owner and contents; do not overwrite an unrelated application.
An upload triggers a rebuild. A successful upload alone is not a successful
deployment: verify RUNNING status, the page, and the experiment callback.

The Gradio SDK, Python and direct numerical dependencies are pinned in the Space
metadata and requirements. Hosted Linux build and callback validation are required
in addition to local macOS tests. No GPU, hardware upgrade or paid storage is
requested by this deployment. Any paid plan requirement must be resolved by the
account owner; creation rules can change.

## Hosting boundary

Spaces on basic hardware can sleep, and ordinary disk state can disappear on
restart. That is acceptable for this reproducible demonstration. Do not attach
the persistent paper executor or rely on this process for scheduled market exits.
The separate worker needs durable account state, independent monitoring and the
remaining execution lifecycle work in [the workflow](workflow.md).

Official references: [Spaces overview](https://huggingface.co/docs/hub/spaces-overview),
[Gradio Spaces](https://huggingface.co/docs/hub/spaces-sdks-gradio),
[Space configuration](https://huggingface.co/docs/hub/spaces-config-reference).
