"""Private local onboarding and preparation checks; no network or order access."""
from __future__ import annotations

from importlib import metadata
import json
import os
from pathlib import Path
import re
import stat
import sys

WORKSPACE_DEFAULTS = {
    "schema_version": 1, "symbol": "QQQ", "timeframe": "5Min",
    "mode": "shadow", "execution": "manual", "account": "tradingview_native_paper",
    "historical_source": "alpaca", "feed": "sip", "adjustment": "raw",
    "data_dir": "private-data", "runs_dir": "runs", "release_dir": "releases",
    "dataset_manifest": None,
}
DEPENDENCIES = {
    "numpy": "research", "scikit-learn": "research", "matplotlib": "reporting",
    "exchange-calendars": "data", "pyarrow": "data", "mlflow-skinny": "tracking_optional",
}
ENV_EXAMPLE = """# Edit a private .env locally. Values must never enter GitHub or chat.
# These empty values are placeholders, not working credentials.
APCA_API_KEY_ID=
APCA_API_SECRET_KEY=
DATABENTO_API_KEY=
MASSIVE_API_KEY=
DWIGHT_DATA_FEED=sip
# Data credentials do not connect to Paper Trading by TradingView.
"""


def _no_links(path):
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise ValueError("Parent traversal is not allowed in workspace paths")
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Workspace paths must not contain symbolic links")
    return path


def _local_path(root, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError("Workspace paths must be nonempty relative paths")
    path = _no_links(root / value)
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Workspace path must remain inside the workspace")
    return path


def _private(path, directory=False):
    if path.is_symlink():
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    # POSIX permissions are meaningful on the supported Mac/Linux workflow.
    return expected and (os.name != "posix" or not (stat.S_IMODE(info.st_mode) & 0o077))


def _write_new(path, content):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def init_workspace(path):
    """Create a new private workspace; an existing path is never overwritten.

    The parent directory must exist and neither it nor the target may traverse a
    symlink. workspace.json is onboarding metadata; commands receive the
    generated research configs explicitly and do not automatically execute it.
    """
    from .experiments import DEFAULTS
    from .walkforward import WALKFORWARD_DEFAULTS

    root = _no_links(path)
    if root.exists():
        raise ValueError("Workspace already exists; choose a new directory")
    if not root.parent.is_dir():
        raise ValueError("Workspace parent directory must already exist")
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    for name in ("configs", "private-data", "runs", "releases"):
        (root / name).mkdir(mode=0o700)
        (root / name).chmod(0o700)
    synthetic = {**DEFAULTS, "min_train_samples": 10, "min_validation_samples": 4,
                 "min_test_samples": 4, "min_class_samples": 1, "min_validation_trades": 1}
    files = {
        "workspace.json": WORKSPACE_DEFAULTS,
        "configs/experiment.json": DEFAULTS,
        "configs/walkforward.json": WALKFORWARD_DEFAULTS,
        "configs/synthetic-experiment.json": synthetic,
        "configs/shadow-policy.json": {
            "version": "shadow-v1", "mode": "shadow", "allowed_symbols": ["QQQ"],
            "feed": "sip", "bar_settle_seconds": 60, "max_bar_delay_seconds": 120,
            "poll_seconds": 30, "stop_file": str(root / "runs" / "STOP"),
        },
    }
    for name, value in files.items():
        _write_new(root / name, json.dumps(value, indent=2, allow_nan=False) + "\n")
    _write_new(root / ".env.example", ENV_EXAMPLE)
    _write_new(root / ".gitignore", "# This entire workspace is private.\n*\n!.gitignore\n")
    return {"status": "created", "workspace": str(root), "symbol": "QQQ", "timeframe": "5Min",
            "mode": "shadow", "execution": "manual", "submits_orders": False,
            "created_files": sorted([*files, ".env.example", ".gitignore"]),
            "next_step": "Run toolkit-status, then follow docs/quickstart.md"}


def _read_object(path):
    if not _private(path) or path.stat().st_size > 1024 * 1024:
        raise ValueError("Configuration must be a private regular file under 1 MiB")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate configuration key")
            result[key] = value
        return result

    result = json.loads(path.read_text(), object_pairs_hook=pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite configuration")))
    if not isinstance(result, dict):
        raise ValueError("Configuration must be an object")
    return result


def _environment(root, environ):
    """Read only key/value presence; never execute, export, or return values."""
    values = dict(os.environ if environ is None else environ)
    path = root / ".env"
    if not path.exists() and not path.is_symlink():
        return values, "absent"
    if not _private(path) or path.stat().st_size > 1024 * 1024:
        return values, "unsafe"
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or not key.isascii() or not key.replace("_", "a").isalnum() or key[0].isdigit():
                return values, "invalid"
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.setdefault(key, value)
    except (OSError, UnicodeError):
        return values, "invalid"
    return values, "private_file_read"


def _manifest_shape(manifest, directory):
    """Reject malformed optional metadata before the provenance reader uses it.

    Do not include supplied field values in errors or readiness output. The
    provenance reader remains responsible for fingerprints and file hashes.
    """
    source = manifest.get("source")
    if manifest.get("schema_version") == "vendor-rth-bars-v1":
        from .vendor_history import verify_manifest
        verify_manifest(manifest, directory)
        return
    expected = {"alpaca": ("alpaca-rth-bars-v1", ("sip", "iex")),
                "firstrate": ("firstrate-sample-rth-bars-v1", ("firstrate_aggregate",))}
    if not isinstance(source, str) or source not in expected:
        raise ValueError("Unsupported dataset source")
    schema, feeds = expected[source]
    if (manifest.get("schema_version") != schema or manifest.get("feed") not in feeds
            or manifest.get("adjustment") not in ("raw", "split") or manifest.get("symbols") != ["QQQ"]):
        raise ValueError("Dataset source metadata is invalid")
    if any(not isinstance(manifest.get(key), str) for key in ("start", "end")):
        raise ValueError("Dataset dates are invalid")
    sha_valid = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    if not sha_valid(manifest.get("dataset_sha256")):
        raise ValueError("Dataset fingerprint is invalid")
    bars = manifest.get("bars")
    if not isinstance(bars, dict) or not isinstance(bars.get("QQQ"), dict):
        raise ValueError("Dataset bar metadata is invalid")
    _local_path(directory, bars["QQQ"].get("5Min"))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Dataset file metadata is invalid")
    paths = set()
    for entry in files:
        if not isinstance(entry, dict) or not sha_valid(entry.get("sha256")):
            raise ValueError("Dataset file metadata is invalid")
        file = _local_path(directory, entry.get("path"))
        if file in paths:
            raise ValueError("Dataset file metadata is duplicated")
        paths.add(file)


def _dataset(root, settings):
    name = settings["dataset_manifest"]
    if name is None:
        return {"status": "not_selected", "research_input_verified": False, "shadow_source_compatible": False}
    from .experiments import _provenance

    manifest_path = _local_path(root, name)
    manifest = _read_object(manifest_path)
    _manifest_shape(manifest, manifest_path.parent)
    data = _local_path(manifest_path.parent, manifest["bars"]["QQQ"]["5Min"])
    if not _private(data) or data.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Dataset CSV must be private and no larger than 128 MiB")
    provenance, _ = _provenance(data, "QQQ", data.read_bytes(), False, manifest_path)
    source = provenance["source"]
    recorded = source == "alpaca" or provenance.get("source_acquisition") in ("https_download", "legacy_recorded_retrieval")
    compatible = (source == "alpaca" and provenance["adjustment"] == "raw"
                  and provenance["feed"] == settings["feed"])
    return {"status": "manifest_verified" if recorded else "unverified_local_archive",
            "research_input_verified": recorded, "source": source, "feed": provenance["feed"],
            "adjustment": provenance["adjustment"], "shadow_source_compatible": compatible,
            "note": "Integrity and recorded source only; sample adequacy, entitlement and model quality are separate checks"}


def _configs(root, settings):
    from .experiments import DEFAULTS
    from .walkforward import _options

    configs = {name: _read_object(_no_links(root / "configs" / f"{name}.json")) for name in
               ("experiment", "walkforward", "synthetic-experiment", "shadow-policy")}
    for key in ("min_train_samples", "min_validation_samples", "min_test_samples", "min_class_samples", "min_validation_trades"):
        value = configs["experiment"].get(key, DEFAULTS[key])
        if type(value) is not int or value < DEFAULTS[key]:
            raise ValueError("Real experiment sample gates cannot be reduced")
    _options(configs["walkforward"], synthetic=False)
    policy = configs["shadow-policy"]
    if policy.get("mode") != "shadow" or policy.get("allowed_symbols") != ["QQQ"] or policy.get("feed") != settings["feed"]:
        raise ValueError("Shadow policy must match the workspace symbol and feed")
    # Stop files may be absolute because frozen releases can run from another
    # working directory; they must still name a path inside this workspace.
    stop = _no_links(policy.get("stop_file", ""))
    if not stop.is_relative_to(root / settings["runs_dir"]) or stop == root / settings["runs_dir"]:
        raise ValueError("Shadow stop path must stay in the private runs directory")


def toolkit_status(path, *, environ=None):
    """Read-only local preparation report, with credential presence only.

    No broker, data provider, order endpoint, model fit or environment mutation
    is invoked. A positive preparation result never approves a model or account.
    """
    root = _no_links(path)
    issues = []
    settings = None
    try:
        if not _private(root, directory=True):
            raise ValueError("Workspace must be a private directory")
        settings = _read_object(root / "workspace.json")
        if set(settings) != set(WORKSPACE_DEFAULTS):
            raise ValueError("Unknown or missing workspace settings")
        fixed = ("schema_version", "symbol", "timeframe", "mode", "execution", "account", "historical_source", "adjustment")
        if any(type(settings[key]) is not type(WORKSPACE_DEFAULTS[key]) or settings[key] != WORKSPACE_DEFAULTS[key] for key in fixed):
            raise ValueError("Workspace scope must remain QQQ five-minute shadow with manual paper execution")
        if settings["feed"] not in ("sip", "iex"):
            raise ValueError("Unsupported market-data feed")
        for key in ("data_dir", "runs_dir", "release_dir"):
            if not _private(_local_path(root, settings[key]), directory=True):
                raise ValueError("Workspace data and artifact directories must be private")
        _configs(root, settings)
    except (OSError, ValueError, TypeError, KeyError):
        issues.append("workspace_invalid_or_not_private")
        settings = None
    versions = {}
    for package, group in DEPENDENCIES.items():
        try:
            version = metadata.version(package)
        except metadata.PackageNotFoundError:
            version = None
        versions[package] = {"installed": version is not None, "version": version, "extra": group}
    environment, env_status = _environment(root, environ)
    present = lambda *names: any(bool(environment.get(name, "").strip()) for name in names)
    credentials = {"alpaca_key_present": present("APCA_API_KEY_ID", "ALPACA_API_KEY"),
                   "alpaca_secret_present": present("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY"),
                   "databento_key_present": present("DATABENTO_API_KEY"),
                   "massive_key_present": present("MASSIVE_API_KEY"),
                   "workspace_env_status": env_status, "values_reported": False}
    if env_status in ("unsafe", "invalid"):
        issues.append("workspace_env_unsafe_or_invalid")
    dataset = {"status": "not_checked", "research_input_verified": False, "shadow_source_compatible": False}
    if settings:
        try:
            dataset = _dataset(root, settings)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, ImportError):
            dataset["status"] = "invalid_or_unavailable"
            issues.append("dataset_provenance_invalid_or_unavailable")
    python_ok = sys.version_info >= (3, 11)
    if not python_ok:
        issues.append("python_3_11_or_newer_required")
    have = lambda *names: all(versions[name]["installed"] for name in names)
    local_ready = settings is not None and python_ok and not issues
    research = local_ready and have("numpy", "scikit-learn", "matplotlib")
    data_ready = local_ready and have("exchange-calendars", "pyarrow")
    return {
        "status": "needs_preparation" if issues else "checked", "workspace": str(root),
        "python": sys.version.split()[0], "dependencies": versions, "credentials": credentials,
        "dataset": dataset, "issues": issues,
        "preparation": {"synthetic_demo": research, "official_sample_download": data_ready,
                        "official_sample_review": data_ready and research,
                        "alpaca_history_download": data_ready and credentials["alpaca_key_present"] and credentials["alpaca_secret_present"],
                        "selected_dataset_research": research and dataset["research_input_verified"]},
        "submits_orders": False, "network_checked": False, "model_approved": False,
        "live_feed_entitlement": "not_checked", "paper_account_connected": False,
        "execution": "Human orders in TradingView; an alert inbox is not an order or fill",
        "limits": "Dependency versions are reported, not a reproducible lockfile or proof of API access. Research gates and release review still apply.",
    }
