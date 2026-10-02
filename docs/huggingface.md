# Hugging Face research Space

The intended public destination is `neekthekid/dwight-schrute-bot`.
This is the research interface, not the always-on paper trading worker.
The app runs the existing experiment on a fixed synthetic fixture. It compares
the baseline, volume filter and classifier on a later chronological partition.
All results are labelled synthetic and ineligible for deployment.

No credential is needed by the running app. It has no credential entry, file
upload, broker operation, or custom-code input. The first request computes a
result; the process holds a single cached result in memory. Temporary data is
cleaned up. MLflow logging is disabled in this ephemeral demo; use the documented
local workflow for private datasets, persistent artifacts and MLflow tracking.

## Local development

Use a separate virtual environment to avoid changing the validated trading CLI
environment. From the repository root:

```sh
python3 -m venv build/space-venv
build/space-venv/bin/python -m pip install -r deploy/huggingface/requirements.txt
build/space-venv/bin/python -m deploy.huggingface.app
```

The app listens on port 7860. For a loopback-only preview, import `build_app()`
and launch it with `server_name="127.0.0.1"`.

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
