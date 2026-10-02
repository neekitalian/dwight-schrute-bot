# Hugging Face research Space

The public app is [neekthekid/dwight-schrute-bot](https://huggingface.co/spaces/neekthekid/dwight-schrute-bot).
This is the research interface, not the always-on paper trading worker.
The **Start here** tab links the versioned 0.3.0 toolkit and offers four fixed
paths: explore the demo, research private QQQ history, connect an observation
inbox, or record manual TradingView paper trades. This tab provides directions
only; it cannot create workspaces or connect accounts on the public server.
The app runs the existing experiment on four fixed synthetic cases. It compares
the baseline, volume filter and classifier on a later chronological partition.
All results are labelled synthetic and ineligible for deployment. A separate
Walk-forward tab evaluates one fixed synthetic path across three chronological
windows, on demand, while leaving a final 40-session holdout unscored.

No credential is needed by the running app. It has no credential entry, file
upload, broker operation, or custom-code input. The first request computes a
result; the process holds up to four dashboard cases, the compact legacy report,
and one on-demand walk-forward report in memory. Temporary data is
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

The interactive analysis release is verified RUNNING on CPU Basic.
Application source: `f89d5bd91ed7d2d518a82500a98ce8517c9e4e21`.
Space commit: `bb9731f5cb8cc6b0ca4b628b75aff9db03bba554`.
The deployment includes 44 allowlisted files. All 147 local tests passed, and
hosted dashboard, session, connection and legacy API calls completed. Browser
checks confirmed chart hover, negative case selection, both transformer branches,
model diagnostics and a 390 pixel mobile layout without page overflow. No browser
JavaScript errors were observed. These checks verify the app, not a trading edge.

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
TradingView paper account has a private local [manual proposal and normalized fill
import workflow](manual-paper.md). The user still places orders manually; the CSV
format is not a tested adapter for native TradingView exports. This dashboard does
not read the private journal, connect to that account, or start milestone observation.

## Walk-forward view

The **Evaluate walk-forward** button runs a separate, fixed public experiment.
Opening the page or changing an ordinary dashboard case does not run it. The
inputless `walkforward` API uses the same callback. It accepts no data, paths,
configuration, credentials or uploaded files.

The generator uses 500 calendar days and seed 42. The expanding window plan uses
80 initial training sessions, 40 validation sessions, 40 test sessions, a final
40-session reservation and at most three windows. All variants are long only.
Synthetic-only sample gates are 4 training labels, 2 validation labels, 2 test
labels, 1 example of each outcome per partition and 1 validation trade. The
threshold candidates are 0.35, 0.5 and 0.65. These reduced counts check software,
not statistical power. They cannot support a trading or deployment claim.

The tab shows:

- A timeline with training, validation and test dates for each window, plus
  separate unused development dates and reserved final-holdout dates.
- Grouped P&L bars for VWAP, the simple volume filter and Dwight's classifier on
  each development test. Every window starts flat at the same capital. These
  independent results are not joined into a fabricated account equity curve.
- All declared window statuses, exact dates, inspected-test flags, labeled sample
  counts, selected thresholds and blockers. Insufficient metrics remain missing,
  never silently replaced with zero or dropped.
- Public synthetic evidence with input and code fingerprints. Internal artifact
  directories and dataset paths are excluded.

One JSON result is cached per process; a lock prevents simultaneous first requests
from duplicating the calculation. Each visitor receives a detached copy. Data and
model artifacts are created in a temporary directory and removed before results
are returned. The shared bounded research queue controls CPU concurrency.

The final period is parsed only to check input integrity and reserve exact dates.
It is not replayed, fitted, tuned or scored. Earlier development tests may become
training or validation history for a later chronological window. Once inspected,
those development outcomes are not fresh holdout evidence. The underlying runner
and its remaining limitations are described in [walk-forward research](walkforward.md).

The manual-account note explains that actual TradingView paper orders remain
human-operated. The FinRL note describes an offline research adapter, not an
active or evaluated agent in the public app. Fundamental and news data need
separate point-in-time preparation. No FinRL or transformer model is trained by
this tab; the displayed learned policy remains logistic regression.

This long-only walk-forward configuration differs from the older Performance
tab's long-and-short single-split configuration. Their scores are not directly
interchangeable. All displayed runs remain synthetic.

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
