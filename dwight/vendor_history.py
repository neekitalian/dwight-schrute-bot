"""Private Databento/Massive datasets for offline QQQ research.

These adapters are not broker or live-shadow connections. Source identity and
volume definitions stay attached to every dataset, including raw responses.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .data import (UTC, _date, exchange_sessions, normalize_minutes,
                   _private_output_directory, _private_file, _write_private_json,
                   _write_bars, _write_parquet, sha256_file)

SCHEMA = "vendor-rth-bars-v1"
FINGERPRINT_KEYS = ("schema_version", "source", "start", "end", "symbols", "feed", "adjustment",
                    "provider_metadata", "research_only", "live_feed", "synthetic",
                    "source_acquisition", "timestamp_convention", "availability", "calendar",
                    "session_policy", "gap_policy", "volume_definition", "counts", "bars",
                    "sessions", "raw_pages", "files")


def fingerprint(manifest):
    try:
        value = {key: manifest[key] for key in FINGERPRINT_KEYS}
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
    except (KeyError, TypeError, ValueError):
        raise ValueError("Incomplete vendor dataset provenance") from None


def _sessions(start, end, now=None):
    first, last = _date(start), _date(end)
    sessions = exchange_sessions(first, last)
    if not sessions:
        raise ValueError("No exchange sessions in requested range")
    collected = now or datetime.now(UTC)
    if not isinstance(collected, datetime) or collected.tzinfo is None:
        raise ValueError("now must be timezone aware")
    if sessions[-1].close + timedelta(minutes=1) > collected:
        raise ValueError("Requested final session has not fully completed")
    return first, last, sessions, collected.astimezone(UTC)


def estimate_history(start, end, *, dataset, environ=None, transport=None, now=None):
    """Nonbillable Databento metadata only; never requests time series."""
    from .databento_data import estimate_databento_history
    first, last, sessions, _ = _sessions(start, end, now)
    result = estimate_databento_history(sessions[0].open, sessions[-1].close,
                                       environ, dataset=dataset, transport=transport)
    return dict(result, session_start=first.isoformat(), session_end=last.isoformat(),
                session_count=len(sessions), submits_orders=False)


def _output_root(path):
    output = Path(path).expanduser().absolute()
    if ".." in output.parts or any(item.is_symlink() for item in (output, *output.parents)):
        raise ValueError("Dataset output cannot traverse symbolic links or parent paths")
    _private_output_directory(output)
    return output


def download_history(provider, output_dir, start, end, *, environ=None, dataset=None,
                     max_cost_usd=None, transport=None, now=None):
    """Explicit historical retrieval into a new private dataset.

Databento requires an explicit dataset and estimated-cost allowance. Its cost
estimate is not a provider-enforced billing cap. No requests or retries occur
merely because a key was filled. A failed coverage check never yields a manifest.
"""
    if provider not in ("databento", "massive"):
        raise ValueError("Choose databento or massive for vendor history")
    if provider == "databento" and (not dataset or max_cost_usd is None):
        raise ValueError("Databento history requires --dataset and --max-cost-usd")
    if provider == "massive" and (dataset is not None or max_cost_usd is not None):
        raise ValueError("Databento dataset and cost options do not apply to Massive")
    first, last, sessions, collected = _sessions(start, end, now)
    output = _output_root(output_dir)
    directory = output / (collected.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
    directory.mkdir(mode=0o700)
    raw_dir = directory / "raw"
    raw_dir.mkdir(mode=0o700)
    page_info = []
    try:
        if provider == "databento":
            from .databento_data import fetch_databento_history
            fetched = fetch_databento_history(sessions[0].open, sessions[-1].close, environ,
                                             dataset=dataset, max_cost_usd=max_cost_usd, transport=transport)
        else:
            from .massive_data import fetch_massive_history
            fetched = fetch_massive_history(sessions[0].open, sessions[-1].close, environ, transport=transport)
        metadata = fetched["provenance"]
        if metadata.get("source") != provider:
            raise ValueError("Vendor response source mismatch")
        extension = "jsonl" if provider == "databento" else "json"
        for number, content in enumerate(fetched["raw_pages"], 1):
            if not isinstance(content, bytes) or not content:
                raise ValueError("Vendor response must preserve nonempty raw bytes")
            path = raw_dir / f"page-{number:06d}.{extension}"
            with _private_file(path, binary=True) as stream:
                stream.write(content)
            page_info.append({"path": str(path.relative_to(directory)), "sha256": sha256_file(path)})
        if not page_info:
            raise ValueError("Vendor response has no raw evidence")
        minutes, five = normalize_minutes(fetched["records"], sessions)
        _write_private_json(directory / "sessions.json", [session.as_dict() for session in sessions])
        files = [{"path": "sessions.json", "sha256": sha256_file(directory / "sessions.json")}, *page_info]
        bars = {}
        for timeframe, rows in (("1Min", minutes), ("5Min", five)):
            path = directory / f"QQQ-{timeframe}.csv"
            _write_bars(path, rows, sessions)
            files.append({"path": path.name, "sha256": sha256_file(path)})
            bars[timeframe] = path.name
            parquet = path.with_suffix(".parquet")
            if _write_parquet(parquet, rows, sessions):
                files.append({"path": parquet.name, "sha256": sha256_file(parquet)})
        manifest = {
            "schema_version": SCHEMA, "source": provider, "retrieved_at": collected.isoformat(),
            "start": first.isoformat(), "end": last.isoformat(), "symbols": ["QQQ"],
            "feed": metadata["feed"], "adjustment": "raw", "provider_metadata": metadata,
            "source_acquisition": "https_download", "research_only": True, "live_feed": False,
            "synthetic": False, "timestamp_convention": "interval_start", "availability": "interval_end",
            "calendar": "XNYS", "session_policy": "complete_regular_sessions_only",
            "gap_policy": "reject_no_forward_fill",
            "volume_definition": metadata.get("volume_definition", "sum_of_source_one_minute_volume"),
            "counts": {"QQQ": {"1Min": len(minutes), "5Min": len(five)}},
            "bars": {"QQQ": bars}, "sessions": "sessions.json", "raw_pages": page_info, "files": files,
            "limitations": [*metadata.get("limitations", []),
                            "Offline research only; this source is not qualified for the Alpaca shadow worker",
                            "A successful download does not grant data redistribution rights"],
        }
        # Check source identity and file integrity before issuing a success manifest.
        manifest["dataset_sha256"] = fingerprint(manifest)
        verify_manifest(manifest, directory)
        _write_private_json(directory / "manifest.json", manifest)
        return dict(manifest, directory=str(directory))
    except Exception as exc:
        _write_private_json(directory / "failed.json", {"status": "failed", "error_type": type(exc).__name__,
                                                         "raw_pages": page_info})
        raise


def verify_manifest(manifest, directory):
    """Verify source, protected metadata and every evidence file hash.

Checksums attest to local integrity, not to provider authentication or licensing.
"""
    directory = Path(directory).absolute()
    source, feed = manifest.get("source"), manifest.get("feed")
    metadata = manifest.get("provider_metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Invalid vendor metadata")
    valid_feed = (source == "massive" and feed == "massive_stocks_aggregates") or (
        source == "databento" and isinstance(metadata.get("dataset"), str)
        and re.fullmatch(r"[A-Z0-9]+\.[A-Z0-9]+", metadata["dataset"])
        and feed == "databento_" + metadata["dataset"] + "_ohlcv_1m"
        and metadata.get("schema") == "ohlcv-1m")
    if (manifest.get("schema_version") != SCHEMA or not valid_feed
            or metadata.get("source") != source or metadata.get("feed") != feed
            or metadata.get("adjustment") != "raw" or manifest.get("adjustment") != "raw"
            or manifest.get("symbols") != ["QQQ"] or manifest.get("research_only") is not True
            or manifest.get("live_feed") is not False or manifest.get("synthetic") is not False
            or manifest.get("source_acquisition") != "https_download"
            or manifest.get("timestamp_convention") != "interval_start"
            or manifest.get("availability") != "interval_end" or manifest.get("calendar") != "XNYS"
            or manifest.get("session_policy") != "complete_regular_sessions_only"
            or manifest.get("gap_policy") != "reject_no_forward_fill"):
        raise ValueError("Invalid vendor research source metadata")
    if fingerprint(manifest) != manifest.get("dataset_sha256"):
        raise ValueError("Vendor dataset fingerprint mismatch")
    entries, pages, bars = manifest.get("files"), manifest.get("raw_pages"), manifest.get("bars")
    if (not isinstance(entries, list) or not entries or not isinstance(pages, list) or not pages
            or not isinstance(bars, dict) or bars.get("QQQ") != {"1Min": "QQQ-1Min.csv", "5Min": "QQQ-5Min.csv"}
            or manifest.get("sessions") != "sessions.json"):
        raise ValueError("Invalid vendor dataset evidence")
    names = set()
    evidence = {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"path", "sha256"}
                or not isinstance(entry["path"], str) or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("Invalid vendor evidence hash")
        relative = Path(entry["path"])
        path = directory / relative
        if (relative.is_absolute() or ".." in relative.parts or entry["path"] in names
                or any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file()):
            raise ValueError("Invalid vendor evidence path")
        if sha256_file(path) != entry["sha256"]:
            raise ValueError("Vendor evidence checksum mismatch")
        names.add(entry["path"])
        evidence[entry["path"]] = entry
    if not {"sessions.json", "QQQ-1Min.csv", "QQQ-5Min.csv"} <= names:
        raise ValueError("Vendor dataset requires session and minute evidence")
    extension = "jsonl" if source == "databento" else "json"
    required_pages = []
    for number, page in enumerate(pages, 1):
        expected = f"raw/page-{number:06d}.{extension}"
        if not isinstance(page, dict) or page.get("path") != expected or evidence.get(expected) != page:
            raise ValueError("Vendor raw page evidence mismatch")
        required_pages.append(expected)
    allowed = {"sessions.json", "QQQ-1Min.csv", "QQQ-5Min.csv", "QQQ-1Min.parquet", "QQQ-5Min.parquet", *required_pages}
    if names - allowed:
        raise ValueError("Unexpected vendor evidence files")
