# Distributable Dwight source toolkit

The toolkit is a reproducible source ZIP for installing and studying Dwight.
It includes application code, strategy code, configuration templates, tests,
documentation, and deployment runbooks. It does not include a trained model,
market-price history, broker credentials, account records, or a running service.
It is a source bundle, not a wheel, PyPI sdist, container image, or certified
trading system. Installation never enables broker execution.

## Build from a reviewed commit

Python 3.11+ and Git are sufficient to build and verify the ZIP. No package
installation or network access occurs in either command.

```sh
python3 scripts/build_toolkit_release.py build --output dist/toolkit
```

The name follows `dwight-toolkit-VERSION-COMMIT.zip`. Version comes from the
committed `pyproject.toml`, and the suffix identifies the full commit recorded
inside the manifest. `--revision FULL_COMMIT_SHA` selects a different already
available commit. No branch fetch, tag, GitHub Release, asset upload or publishing
occurs. The destination ZIP and checksum must not already exist.

The builder reads Git blobs from the resolved commit. Dirty and untracked files
cannot be substituted for the source described by that commit. New or corrected
files must be reviewed and committed before they appear in a release.

Only supported source/configuration/documentation paths are eligible. Git data,
GitHub workflows, environment files other than the empty `.env.example`, private
datasets, runs, caches, model files, raw archives and dependency directories are
excluded. The sole bundled CSV is the fixed manual-fill example, whose rows must
declare `data_kind=synthetic`. Symlinks and files containing recognizable token
or private-key patterns are rejected. Credential fields in `.env.example` must
be empty. These checks complement source review; they cannot prove arbitrary
source text contains no confidential information.

Each ZIP member has a fixed timestamp and preserves only regular-file contents
and executable permissions. Members are sorted and stored without compression,
avoiding compression-library variation. The same source commit and builder
version produce identical ZIP bytes, even if the checkout has local changes.
The builder records per-file SHA-256, size and mode in `release-manifest.json` and
also writes an external `.zip.sha256` checksum.

## Verify without extracting or executing

```sh
python3 scripts/build_toolkit_release.py verify dist/toolkit/RELEASE.zip \
  --expected-sha256 TRUSTED_SHA256
```

Verification checks the complete archive roster, paths, file types, hashes,
sizes, permissions, source scope, project version and required attribution. It
does not extract files, import their Python modules, execute scripts, install
packages or access the network. A changed, extra, missing or unsafe member fails.

Obtain `TRUSTED_SHA256` through a trusted release channel. The manifest and a
checksum copied alongside an archive provide integrity checks, not independent
publisher authentication. No release signing is implemented in this first cut.

## Install locally

After verification, extract the single top-level directory and enter it. Run the
following with your Python 3.11+ interpreter. Dependency installation may access
the configured package index; the source builder itself never does.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m dwight doctor
.venv/bin/python examples/make_experiment_demo.py
.venv/bin/python -m dwight replay private-data/synthetic-vwap-5Min.csv --symbol QQQ
```

The base package supports synthetic replay without optional research libraries.
Choose only the extras needed for the task:

```sh
.venv/bin/python -m pip install '.[data,research,reporting]'
# Optional local experiment tracker:
.venv/bin/python -m pip install '.[tracking]'
```

For the previously recorded Python 3.11 dependency versions, install
`requirements.lock` and then `pip install --no-deps .`. That file pins package
versions; it is not a platform-independent wheelhouse or hash-verified lock.
Reproducing source bytes does not guarantee identical numerical results across
operating systems, BLAS libraries or Python versions. An entirely offline
installation also requires separately prepared compatible dependency wheels and
build dependencies; those are not bundled here.

`pyproject.toml` defines the build dependency on setuptools. A fresh pip install
may fetch it into a build environment. The documented version range starts at
Python 3.11; the exact production Python/OS combination still needs deployment
verification.

## Configure a paper research installation

Copy `.env.example` to a private `.env` and set its permissions to `0600`. Fill
credentials locally; never include this file when rebuilding or sharing the
toolkit. `DWIGHT_DATA_FEED` must match the data and model experiment. The empty
template is not a configured paper account.

Read [the workflow](workflow.md), [data preparation](real-data.md), and the
[Linux deployment runbook](server-experiment.md). The server installation script
expects a Git checkout and a reviewed full commit SHA; it cannot use an extracted
ZIP as a substitute for that checkout. The source kit includes the runbook and
service templates for inspection. Provisioning, credentials, model qualification,
shadow observation, and account connection are separate tasks.

## License and commercial distribution

The archive retains `LICENSE`, `NOTICE`, and `docs/VWAP-LICENSE`, including the
vendored VWAP engine's upstream commit attribution. The toolkit identifies its
license as Apache 2.0. Distribute these notices with the source and review the
licenses of any dependencies or additional models you choose to bundle.

Market-data subscriptions, redistribution rights, model weights and hosted
services are separate from this source release. The ZIP ships none of those
assets and makes no claim of observed profitability or readiness for live money.
