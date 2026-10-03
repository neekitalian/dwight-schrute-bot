"""Offline, evidence-preserving recovery of a completed Alpaca acquisition.

Only timestamps enter the coverage decision. An incomplete regular session is
excluded in full, with a fixed 5% cap. Original HTTP bytes and the trusted study
protocol are retained; this module cannot infer the feed from a bar response.
No network, model fitting, outcome selection, or order API is used here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
from uuid import uuid4

from .data import (ALPACA_BARS_URL, SCHEMA_VERSION, UTC, NY, exchange_sessions,
                   normalize_minutes, sha256_file, _private_output_directory,
                   _private_file, _write_private_json, _write_bars, _write_parquet)

GAP_POLICY = "exclude_incomplete_sessions_no_forward_fill"
MAX_EXCLUDED_SESSION_FRACTION = 0.05


def _safe_path(value):
    path = Path(value).absolute()
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise ValueError("Recovery paths must not traverse symbolic links")
    return path


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Expected an aware timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid timestamp in acquisition evidence") from None
    if result.tzinfo is None:
        raise ValueError("Acquisition timestamps must be timezone aware")
    return result.astimezone(UTC)


def _inputs(failed_download, protocol_path):
    source, protocol = _safe_path(failed_download), _safe_path(protocol_path)
    if not source.is_dir() or not protocol.is_file():
        raise ValueError("Recovery requires a failed acquisition and its trusted original protocol")
    for name in ("failed.json", "sessions.json", "raw"):
        _safe_path(source / name)
    if (source / "manifest.json").exists():
        raise ValueError("Recovery accepts failed acquisitions only")
    recipe = json.loads(protocol.read_bytes())
    if (not isinstance(recipe, dict) or recipe.get("schema_version") != 1
            or recipe.get("symbol") != "QQQ" or recipe.get("feed") not in ("sip", "iex")
            or recipe.get("adjustment") != "raw"
            or not isinstance(recipe.get("start"), str) or not isinstance(recipe.get("end"), str)):
        raise ValueError("Trusted protocol must specify QQQ, explicit Alpaca feed, and raw adjustment")
    sessions = exchange_sessions(recipe["start"], recipe["end"])
    if not sessions or json.loads((source / "sessions.json").read_bytes()) != [s.as_dict() for s in sessions]:
        raise ValueError("Saved calendar differs from the trusted protocol")
    failure = json.loads((source / "failed.json").read_bytes())
    if not isinstance(failure, dict) or failure.get("status") != "failed":
        raise ValueError("Invalid failed acquisition evidence")
    pages = failure.get("raw_pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("Failed acquisition has no recorded raw pages")
    expected_names = [f"raw/page-{i:06d}.json" for i in range(1, len(pages) + 1)]
    if any(not isinstance(p, dict) for p in pages) or [p.get("path") for p in pages] != expected_names:
        raise ValueError("Recorded raw pages must be contiguous and ordered")
    if sorted(str(p.relative_to(source)) for p in (source / "raw").iterdir()) != expected_names:
        raise ValueError("Raw directory differs from the recorded page inventory")
    previous = None
    for page in pages:
        path = _safe_path(source / page["path"])
        if not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", str(page.get("sha256", ""))):
            raise ValueError("Invalid recorded raw page hash")
        acquired = _stamp(page.get("retrieved_at"))
        if acquired < sessions[-1].close or (previous is not None and acquired < previous):
            raise ValueError("Raw acquisition times are inconsistent")
        previous = acquired
    return source, protocol, recipe, sessions, pages


def _page_rows(source, pages):
    tokens = set()
    previous = None
    for index, entry in enumerate(pages):
        content = (source / entry["path"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("Recorded raw page checksum mismatch")
        payload = json.loads(content)
        if (not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict)
                or set(payload["bars"]) != {"QQQ"} or not isinstance(payload["bars"]["QQQ"], list)
                or "next_page_token" not in payload):
            raise ValueError("Raw page must contain exactly QQQ and explicit pagination evidence")
        token = payload["next_page_token"]
        if index == len(pages) - 1:
            if token is not None:
                raise ValueError("Final raw page does not prove completed pagination")
        elif not isinstance(token, str) or not token or token in tokens:
            raise ValueError("Intermediate pagination tokens must be nonempty and unique")
        else:
            tokens.add(token)
        for row in payload["bars"]["QQQ"]:
            if not isinstance(row, dict):
                raise ValueError("Invalid raw minute record")
            stamp = _stamp(row.get("t"))
            if stamp.second or stamp.microsecond:
                raise ValueError("Raw bars must have minute-aligned timestamps")
            if previous is not None and stamp <= previous:
                raise ValueError("Raw timestamps must be unique and in ascending page order")
            previous = stamp
            yield row, stamp


def _intervals(stamps):
    result = []
    if not stamps:
        return result
    first = last = stamps[0]
    for stamp in stamps[1:]:
        if stamp == last + timedelta(minutes=1):
            last = stamp
        else:
            result.append({"start": first.isoformat(), "end_inclusive": last.isoformat()})
            first = last = stamp
    result.append({"start": first.isoformat(), "end_inclusive": last.isoformat()})
    return result


def audit_failed_download(failed_download, protocol_path, *, report_path=None):
    """Validate exact raw evidence and inspect timestamp coverage, never OHLCV.

    The optional report is a new private JSON file; existing files are refused.
    Coverage above the fixed exclusion cap is reported but cannot be recovered.
    """
    source, protocol, recipe, sessions, pages = _inputs(failed_download, protocol_path)
    by_date = {s.date: s for s in sessions}
    stamps = {s.date: set() for s in sessions}
    total = outside = 0
    first = last = None
    for _, stamp in _page_rows(source, pages):
        if stamp < sessions[0].open or stamp >= sessions[-1].close:
            raise ValueError("Raw timestamp is outside the trusted acquisition bounds")
        total += 1
        first = stamp if first is None else first
        last = stamp
        day = stamp.astimezone(NY).date().isoformat()
        session = by_date.get(day)
        if session and session.open <= stamp < session.close:
            stamps[day].add(stamp)
        else:
            outside += 1
    coverage, exclusions = [], []
    for session in sessions:
        count = int((session.close - session.open).total_seconds() // 60)
        missing = [session.open + timedelta(minutes=i) for i in range(count)
                   if session.open + timedelta(minutes=i) not in stamps[session.date]]
        entry = {"session": session.date, "expected_minutes": count,
                 "observed_minutes": len(stamps[session.date]), "missing_minutes": len(missing),
                 "early_close": count < 390, "complete": not missing,
                 "missing_intervals": _intervals(missing)}
        coverage.append(entry)
        if missing:
            exclusions.append(dict(entry, reason="incomplete_regular_session", action="exclude_entire_session"))
    retained = [entry for entry in coverage if entry["complete"]]
    fraction = len(exclusions) / len(sessions)
    report = {"schema_version": "alpaca-coverage-audit-v1", "source_attempt": str(source),
              "trusted_protocol_sha256": sha256_file(protocol),
              "source_failed_sha256": sha256_file(source / "failed.json"),
              "source_sessions_sha256": sha256_file(source / "sessions.json"),
              "start": recipe["start"], "end": recipe["end"], "feed": recipe["feed"],
              "adjustment": "raw", "symbols": ["QQQ"], "raw_page_count": len(pages),
              "raw_page_hashes_verified": True, "pagination_complete": True,
              "timestamp_only_audit": True, "outcomes_inspected": False,
              "raw_rows": total, "outside_regular_session_rows": outside,
              "first_timestamp": first.isoformat() if first else None,
              "last_timestamp": last.isoformat() if last else None,
              "requested_sessions": len(sessions), "complete_sessions": len(retained),
              "complete_full_sessions": sum(not row["early_close"] for row in retained),
              "complete_early_sessions": sum(row["early_close"] for row in retained),
              "excluded_sessions": len(exclusions), "excluded_session_fraction": fraction,
              "max_excluded_session_fraction": MAX_EXCLUDED_SESSION_FRACTION,
              "within_exclusion_cap": fraction <= MAX_EXCLUDED_SESSION_FRACTION,
              "expected_rth_minutes": sum(row["expected_minutes"] for row in coverage),
              "observed_rth_minutes": sum(row["observed_minutes"] for row in coverage),
              "missing_rth_minutes": sum(row["missing_minutes"] for row in coverage),
              "retained_rth_minutes": sum(row["expected_minutes"] for row in retained),
              "retained_five_minute_bars": sum(row["expected_minutes"] for row in retained) // 5,
              "gap_policy": GAP_POLICY, "exclusions": exclusions, "session_coverage": coverage,
              "limitations": ["Missing-bar causes are not inferred", "Excluding stressed sessions can understate risk",
                              "HTTP request URLs were not persisted by the original collector; feed and query recipe rely on the trusted original protocol"]}
    if report_path is not None:
        target = _safe_path(report_path)
        _private_output_directory(target.parent)
        _write_private_json(target, report)
    return report


def _copy_private(source, destination, expected_hash=None):
    digest = hashlib.sha256()
    with source.open("rb") as reader, _private_file(destination, binary=True) as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
            writer.write(chunk)
    result = digest.hexdigest()
    if expected_hash is not None and result != expected_hash:
        raise ValueError("Source evidence changed during recovery")
    return result


def recover_history(failed_download, protocol_path, output_dir, *, now=None):
    """Create one private research dataset beneath output_dir, entirely offline.

    The output parent may already be private; a new unique dataset child is
    always used. Recovery never changes the source attempt or original protocol.
    """
    audit = audit_failed_download(failed_download, protocol_path)
    if not audit["within_exclusion_cap"] or not audit["complete_sessions"]:
        raise ValueError("Incomplete sessions exceed the fixed 5% recovery cap")
    source, protocol, recipe, sessions, pages = _inputs(failed_download, protocol_path)
    imported = now or datetime.now(UTC)
    if imported.tzinfo is None or imported < _stamp(pages[-1]["retrieved_at"]):
        raise ValueError("Recovery import time must follow raw acquisition")
    output = _safe_path(output_dir)
    if output == source or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Recovery output must be separate from the source attempt")
    _private_output_directory(output)
    directory = output / (imported.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-recovered-" + uuid4().hex[:12])
    _private_output_directory(directory)
    _private_output_directory(directory / "raw")
    try:
        files = []
        for entry in pages:
            _copy_private(source / entry["path"], directory / entry["path"], entry["sha256"])
        for original, filename, digest in ((protocol, "source-protocol.json", audit["trusted_protocol_sha256"]),
                (source / "failed.json", "source-failed.json", audit["source_failed_sha256"]),
                (source / "sessions.json", "sessions.json", audit["source_sessions_sha256"])):
            files.append({"path": filename, "sha256": _copy_private(original, directory / filename, digest)})
        for filename, value in (("coverage.json", audit), ("exclusions.json", audit["exclusions"])):
            _write_private_json(directory / filename, value)
            files.append({"path": filename, "sha256": sha256_file(directory / filename)})
        excluded = {entry["session"] for entry in audit["exclusions"]}
        retained = [session for session in sessions if session.date not in excluded]
        # Validate all OHLCV, including excluded rows, but form bars only for the
        # retained complete sessions. No price is inspected to select exclusions.
        minutes, five = normalize_minutes((row for row, _ in _page_rows(directory, pages)), retained)
        if len(minutes) != audit["retained_rth_minutes"] or len(five) != audit["retained_five_minute_bars"]:
            raise ValueError("Recovered bars disagree with audited timestamp coverage")
        bar_paths = {}
        for timeframe, bars in (("1Min", minutes), ("5Min", five)):
            filename = f"QQQ-{timeframe}.csv"
            _write_bars(directory / filename, bars, retained)
            files.append({"path": filename, "sha256": sha256_file(directory / filename)})
            bar_paths[timeframe] = filename
            parquet = (directory / filename).with_suffix(".parquet")
            if _write_parquet(parquet, bars, retained):
                files.append({"path": parquet.name, "sha256": sha256_file(parquet)})
        try:
            calendar_version = version("exchange_calendars")
        except PackageNotFoundError:
            calendar_version = "unavailable"
        manifest = {"schema_version": SCHEMA_VERSION, "source": "alpaca", "endpoint": ALPACA_BARS_URL,
                    "start": recipe["start"], "end": recipe["end"], "symbols": ["QQQ"],
                    "feed": recipe["feed"], "adjustment": "raw", "asof": "-", "calendar": "XNYS",
                    "calendar_version": calendar_version, "timestamp_convention": "interval_start",
                    "availability": "interval_end", "session_policy": "complete_regular_sessions_only",
                    "gap_policy": GAP_POLICY, "max_excluded_session_fraction": MAX_EXCLUDED_SESSION_FRACTION,
                    "research_only": True, "outcomes_inspected": False, "source_attempt": str(source),
                    "dataset_quality": {"gap_policy": GAP_POLICY,
                        "requested_session_count": audit["requested_sessions"],
                        "retained_session_count": audit["complete_sessions"],
                        "excluded_session_count": audit["excluded_sessions"],
                        "excluded_session_dates": [row["session"] for row in audit["exclusions"]],
                        "missing_minute_count": audit["missing_rth_minutes"],
                        "excluded_session_fraction": audit["excluded_session_fraction"],
                        "max_excluded_session_fraction": MAX_EXCLUDED_SESSION_FRACTION,
                        "exclusion_reason": "incomplete_regular_session",
                        "risk_limitation": "Excluding incomplete sessions can omit stressed markets and understate risk."},
                    "trusted_protocol_sha256": audit["trusted_protocol_sha256"],
                    "source_protocol": "source-protocol.json", "source_failed": "source-failed.json",
                    "retrieved_at": pages[0]["retrieved_at"], "imported_at": imported.isoformat(),
                    "acquisition_started_at": pages[0]["retrieved_at"], "acquisition_completed_at": pages[-1]["retrieved_at"],
                    "request_recipe_evidence": "trusted_original_protocol_not_persisted_http_request",
                    "volume_definition": "sum_of_source_one_minute_volume",
                    "counts": {"QQQ": {"1Min": len(minutes), "5Min": len(five)}},
                    "bars": {"QQQ": bar_paths}, "sessions": "sessions.json", "coverage": "coverage.json",
                    "exclusions": "exclusions.json", "raw_pages": pages, "files": files,
                    "limitations": audit["limitations"] + ["No quote/spread data in this bar dataset", "Bar-derived VWAP is an approximation",
                        "Historical raw prices do not provide point-in-time corporate-action evidence"]}
        keys = ("schema_version", "start", "end", "symbols", "feed", "adjustment", "files")
        manifest["dataset_sha256"] = hashlib.sha256(json.dumps({k: manifest[k] for k in keys}, sort_keys=True).encode()).hexdigest()
        _write_private_json(directory / "manifest.json", manifest)
        return dict(manifest, directory=str(directory))
    except Exception as exc:
        _write_private_json(directory / "recovery-failed.json", {"status": "failed", "error_type": type(exc).__name__})
        raise


recover_failed_download = recover_history
