"""Immutable, private definitions for fair historical strategy comparisons.

Tickets record a shared market window, verified data identity, strategy cohort,
and common simulation settings. They do not run a replay or connect an account.
"""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from .connectors.csv import read_bars
from .experiments import NY, _provenance, complete_sessions
from .toolkit import _manifest_shape
from vwap_bot.engine import Config

SCHEMA_VERSION = 1
KIND = "dwight_research_comparison_ticket"
SUPPORTED_STRATEGIES = {
    "vwap_baseline": "vwap-baseline-v1",
    "relative_volume_filter": "prior-relative-volume-filter-v1",
}
EVALUATION_ROLES = {"exploratory", "validation", "final_holdout"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONFIG_FIELDS = {field.name: field for field in fields(Config)}


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _private_path(path: Path, *, directory: bool) -> bool:
    if path.is_symlink():
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return expected and (os.name != "posix" or not (stat.S_IMODE(info.st_mode) & 0o077))


def _private_workspace(value: str | Path) -> Path:
    root = Path(value).expanduser().absolute()
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("Workspace paths must not contain symbolic links")
    if not _private_path(root, directory=True):
        raise ValueError("Research ticket workspace must be an existing private directory")
    return root


def _relative_file(root: Path, value: str, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"{label} must be a workspace-relative path")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{label} must remain inside the workspace")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError(f"{label} must not use symbolic links")
    if not _private_path(path, directory=False):
        raise ValueError(f"{label} must be a private regular file")
    return path


def _date(value, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD")
    return parsed


def _read_manifest(root: Path, relative: str):
    manifest_path = _relative_file(root, relative, "dataset_manifest")
    raw = manifest_path.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Dataset manifest exceeds 8 MiB")
    try:
        manifest = json.loads(raw, object_pairs_hook=_unique_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Dataset manifest is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Dataset manifest must contain an object")
    _manifest_shape(manifest, manifest_path.parent)
    bars_relative = manifest.get("bars", {}).get("QQQ", {}).get("5Min")
    bars_path = _relative_file(manifest_path.parent, bars_relative, "five-minute bars")
    raw_bars = bars_path.read_bytes()
    provenance, verified_manifest = _provenance(bars_path, "QQQ", raw_bars, False, manifest_path)
    if verified_manifest is None or provenance.get("source") == "synthetic":
        raise ValueError("A verified, nonsynthetic data manifest is required")
    return manifest_path, raw, manifest, bars_path, raw_bars, provenance


def _coverage(manifest: dict, bars_path: Path, start: date, end: date) -> dict:
    """Record complete, excluded, and absent exchange sessions in the window."""
    try:
        from .data import exchange_sessions
        expected = {session.date for session in exchange_sessions(start, end)}
    except (ImportError, RuntimeError) as exc:
        raise ValueError("Install Dwight's data extra to verify ticket session coverage") from exc

    bars = [bar for bar in read_bars(bars_path)
            if start.isoformat() <= bar.timestamp.astimezone(NY).date().isoformat() <= end.isoformat()]
    if not bars:
        raise ValueError("Ticket window contains no observed five-minute bars")
    complete, excluded = complete_sessions(bars)
    observed = set(complete) | {entry["session"] for entry in excluded}
    missing = sorted(expected - observed)
    if not complete:
        raise ValueError("Ticket window contains no full sessions supported by this replay engine")
    return {
        "calendar": "XNYS",
        "expected_sessions": len(expected),
        "complete_sessions": sorted(complete),
        "excluded_sessions": excluded,
        "missing_sessions": missing,
        "policy": "replay complete sessions only; disclose exclusions; block when exchange sessions are absent",
        "data_ready": not missing,
    }


def _code_identity() -> dict:
    engine = Path(__file__).parent.parent / "vwap_bot" / "engine.py"
    source = Path(__file__).parent / "experiments.py"
    return {
        "strategy_engine_sha256": _sha256(source.read_bytes() + engine.read_bytes()),
        "strategy_registry_sha256": _sha256(_canonical(SUPPORTED_STRATEGIES)),
    }


def _normalize(definition: dict, workspace: Path) -> dict:
    required = {"title", "window", "dataset_manifest", "strategies", "engine_config"}
    optional = {"evaluation_role", "cost_stress_multiplier"}
    if not isinstance(definition, dict) or required - definition.keys() or definition.keys() - required - optional:
        raise ValueError("Research ticket definition has missing or unsupported fields")
    title = definition["title"]
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        raise ValueError("Ticket title must contain 1 to 120 characters")

    window = definition["window"]
    if not isinstance(window, dict) or set(window) != {"start", "end"}:
        raise ValueError("Ticket window must contain start and end dates")
    start, end = _date(window["start"], "window.start"), _date(window["end"], "window.end")
    if start > end:
        raise ValueError("window.start must not be after window.end")

    strategy_ids = definition["strategies"]
    if (not isinstance(strategy_ids, list) or len(strategy_ids) < 2
            or any(not isinstance(item, str) or item not in SUPPORTED_STRATEGIES for item in strategy_ids)
            or len(set(strategy_ids)) != len(strategy_ids)):
        raise ValueError("Select at least two unique supported strategy IDs")

    role = definition.get("evaluation_role", "exploratory")
    if not isinstance(role, str) or role not in EVALUATION_ROLES:
        raise ValueError("evaluation_role must be exploratory, validation, or final_holdout")
    multiplier = definition.get("cost_stress_multiplier", 2)
    if type(multiplier) not in (int, float) or not math.isfinite(multiplier) or multiplier < 1:
        raise ValueError("cost_stress_multiplier must be a number greater than or equal to one")

    engine_config = definition["engine_config"]
    if not isinstance(engine_config, dict) or engine_config.keys() - _CONFIG_FIELDS.keys():
        raise ValueError("engine_config contains unsupported strategy settings")
    for name, value in engine_config.items():
        default = _CONFIG_FIELDS[name].default
        if isinstance(default, bool):
            if type(value) is not bool:
                raise ValueError(f"engine_config.{name} must be boolean")
        elif isinstance(default, int):
            if type(value) is not int:
                raise ValueError(f"engine_config.{name} must be an integer")
        elif type(value) not in (int, float):
            raise ValueError(f"engine_config.{name} must be numeric")
    settings = Config(**engine_config)

    (manifest_path, manifest_raw, manifest, bars_path, bars_raw,
     provenance) = _read_manifest(workspace, definition["dataset_manifest"])
    dataset_start = _date(manifest.get("start"), "dataset.start")
    dataset_end = _date(manifest.get("end"), "dataset.end")
    if start < dataset_start or end > dataset_end:
        raise ValueError("Ticket window must remain inside the verified dataset date range")
    coverage = _coverage(manifest, bars_path, start, end)
    if not manifest_path.is_relative_to(workspace):
        raise ValueError("Dataset manifest must remain inside the workspace")

    return {
        "title": title.strip(),
        "scope": {"symbol": "QQQ", "timeframe": "5Min", "timezone": "America/New_York",
                  "window": {"start": start.isoformat(), "end": end.isoformat(), "bounds": "inclusive session dates"}},
        "dataset": {
            "manifest_path": manifest_path.relative_to(workspace).as_posix(),
            "manifest_sha256": _sha256(manifest_raw),
            "bars_path": bars_path.relative_to(workspace).as_posix(),
            "bars_sha256": _sha256(bars_raw),
            "dataset_sha256": provenance["dataset_sha256"],
            "source": provenance["source"], "feed": provenance["feed"],
            "adjustment": provenance["adjustment"],
            "licence_status": "not_assessed_by_ticket",
        },
        "strategies": [{"id": strategy_id, "version": SUPPORTED_STRATEGIES[strategy_id]}
                       for strategy_id in strategy_ids],
        "engine_config": asdict(settings),
        "cost_stress_multiplier": float(multiplier),
        "evaluation_role": role,
        "coverage": coverage,
        "implementation": _code_identity(),
        "comparison_contract": "all selected strategies share data, window, capital, costs, and fill rules",
    }


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON fields are not allowed")
        value[key] = item
    return value


def _write_new_private(path: Path, value: dict) -> None:
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                     allow_nan=False).encode("utf-8") + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".ticket-", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def freeze_ticket(definition: dict, workspace: str | Path) -> dict:
    """Freeze a ticket once; persist it privately and never run its strategies."""
    root = _private_workspace(workspace)
    normalized = _normalize(definition, root)
    ticket_id = uuid.uuid4().hex
    ticket = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "ticket_id": ticket_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "frozen_not_run" if normalized["coverage"]["data_ready"] else "frozen_data_review_required",
        "definition": normalized,
        "definition_sha256": _sha256(_canonical(normalized)),
        "execution_enabled": False,
        "model_qualified": False,
        "results": None,
    }
    ticket["ticket_sha256"] = _sha256(_canonical(ticket))
    directory = root / "tickets"
    if not directory.exists():
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    if not _private_path(directory, directory=True):
        raise ValueError("Ticket output directory must be private")
    path = directory / f"{ticket_id}.json"
    _write_new_private(path, ticket)
    return {"status": ticket["status"], "ticket_id": ticket_id,
            "definition_sha256": ticket["definition_sha256"],
            "ticket_sha256": ticket["ticket_sha256"], "ticket_path": str(path),
            "data_ready": normalized["coverage"]["data_ready"],
            "runner_available": False,
            "missing_sessions": normalized["coverage"]["missing_sessions"],
            "execution_enabled": False, "replay_started": False}


def verify_ticket(ticket_path: str | Path, workspace: str | Path) -> dict:
    """Check frozen bytes, data files, and current strategy implementation identity."""
    root = _private_workspace(workspace)
    path = Path(ticket_path).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.resolve().is_relative_to((root / "tickets").resolve()):
        raise ValueError("Ticket path must be a private ticket file inside the workspace")
    if not _private_path(path, directory=False):
        raise ValueError("Ticket file must be private")
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("Ticket file exceeds 1 MiB")
    ticket = json.loads(raw, object_pairs_hook=_unique_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    if (not isinstance(ticket, dict) or ticket.get("schema_version") != SCHEMA_VERSION
            or ticket.get("kind") != KIND or not isinstance(ticket.get("definition"), dict)):
        raise ValueError("Unsupported research ticket")

    payload = dict(ticket)
    checksum = payload.pop("ticket_sha256", None)
    definition_hash = ticket.get("definition_sha256")
    reasons = []
    if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum) or _sha256(_canonical(payload)) != checksum:
        reasons.append("ticket_bytes_changed")
    if (not isinstance(definition_hash, str) or not _SHA256.fullmatch(definition_hash)
            or _sha256(_canonical(ticket["definition"])) != definition_hash):
        reasons.append("definition_changed")

    definition = ticket["definition"]
    implementation_matches = definition.get("implementation") == _code_identity()
    if not implementation_matches:
        reasons.append("strategy_implementation_changed")
    dataset_matches = False
    try:
        dataset = definition["dataset"]
        manifest_path, manifest_raw, _, bars_path, bars_raw, provenance = _read_manifest(root, dataset["manifest_path"])
        dataset_matches = (
            _sha256(manifest_raw) == dataset.get("manifest_sha256")
            and bars_path.relative_to(root).as_posix() == dataset.get("bars_path")
            and _sha256(bars_raw) == dataset.get("bars_sha256")
            and provenance.get("dataset_sha256") == dataset.get("dataset_sha256")
        )
        if not dataset_matches:
            reasons.append("dataset_changed")
    except (KeyError, OSError, ValueError, TypeError):
        reasons.append("dataset_unavailable_or_invalid")

    if ticket.get("execution_enabled") is not False or ticket.get("results") is not None:
        reasons.append("ticket_contains_unexpected_execution_or_results")
    return {"status": "verified" if not reasons else "mismatch",
            "ticket_id": ticket.get("ticket_id"),
            "ticket_sha256_matches": "ticket_bytes_changed" not in reasons,
            "definition_sha256_matches": "definition_changed" not in reasons,
            "dataset_matches": dataset_matches,
            "implementation_matches": implementation_matches,
            "data_ready": bool(definition.get("coverage", {}).get("data_ready")),
            "runner_available": False,
            "replay_started": False, "execution_enabled": False,
            "issues": reasons}
