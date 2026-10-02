"""Official, credential-free QQQ sample ingestion for private research only.

The vendor sample is split-adjusted, multi-venue data. It is never labelled as
Alpaca SIP/IEX, never a live feed, and cannot by itself qualify a paper release.
"""
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import ssl
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import ZipFile

from .data import NY, exchange_sessions, normalize_minutes, _write_bars, _write_parquet, sha256_file

SAMPLE_URL = "https://frd001.s3.us-east-2.amazonaws.com/frd_sample_etf_QQQ.zip"
PRODUCT_URL = "https://firstratedata.com/i/etf/QQQ"
LICENSE_URL = "https://firstratedata.com/about/license"
MAX_ARCHIVE_BYTES = 50_000_000
MAX_MEMBER_BYTES = 100_000_000
MINUTE_MEMBER = "QQQ_1min_sample.csv"
README_MEMBER = "_readme_documentation.txt"


def parse_sample_minutes(content: bytes) -> list[dict]:
    """Localize vendor start-labelled US Eastern minutes; preserve actual OHLCV."""
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames != ["timestamp", "open", "high", "low", "close", "volume"]:
        raise ValueError("Unexpected QQQ sample CSV schema")
    records = []
    for number, row in enumerate(reader, 2):
        try:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Unexpected CSV field count")
            stamp = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            if stamp.second:
                raise ValueError("Expected whole-minute start timestamp")
            records.append({"t": stamp.replace(tzinfo=NY).isoformat(),
                            **{key: row[field] for key, field in zip(
                                ("o", "h", "l", "c", "v"), ("open", "high", "low", "close", "volume"))}})
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid QQQ sample row {number}") from exc
    if not records:
        raise ValueError("QQQ sample contains no minute records")
    return records


def sample_acquisition(manifest):
    """Describe recorded acquisition, not cryptographic vendor authenticity.

    Older download manifests recorded an acquisition timestamp but not a method.
    Retain that distinction rather than retroactively claiming an HTTPS receipt.
    """
    method = manifest.get("source_acquisition")
    if method is None:
        recorded = (manifest.get("retrieved_at") is not None
                    and manifest.get("retrieval_time_status") in (None, "provided"))
        method = "legacy_recorded_retrieval" if recorded else "local_archive"
    if method not in ("https_download", "local_archive", "legacy_recorded_retrieval"):
        raise ValueError("invalid FirstRate acquisition method")
    if method != "local_archive":
        try:
            stamp = datetime.fromisoformat(manifest["retrieved_at"])
            statuses = ("provided",) if method == "https_download" else (None, "provided")
            if stamp.tzinfo is None or manifest.get("retrieval_time_status") not in statuses:
                raise ValueError("missing recorded acquisition")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid FirstRate recorded acquisition") from exc
    return method


def _import_firstrate_sample(archive_path, output_dir, *, retrieved_at=None, source_acquisition="local_archive"):
    """Import a claimed vendor sample; acquisition is recorded by the caller.

    Reads only explicitly named members, without extracting arbitrary ZIP paths.
    The resulting manifest is compatible with Dwight's dataset hash convention.
    """
    if retrieved_at is not None:
        acquisition = datetime.fromisoformat(retrieved_at)
        if acquisition.tzinfo is None:
            raise ValueError("Source retrieval timestamp must include a timezone")
    archive_path = Path(archive_path)
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("QQQ sample archive exceeds size limit")
    archive_bytes = archive_path.read_bytes()
    with ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = archive.namelist()
        for name in (MINUTE_MEMBER, README_MEMBER):
            if names.count(name) != 1 or archive.getinfo(name).file_size > MAX_MEMBER_BYTES:
                raise ValueError("Missing, duplicate, or oversized QQQ sample member")
        minute_bytes = archive.read(MINUTE_MEMBER)
        readme = archive.read(README_MEMBER)
    text = readme.decode("utf-8-sig").lower()
    if "qqq" not in text or "split-adjusted sample" not in text or "us eastern" not in text:
        raise ValueError("Sample documentation does not establish QQQ, adjustment and timezone")
    records = parse_sample_minutes(minute_bytes)
    stamps = [datetime.fromisoformat(row["t"]) for row in records]
    first, last = min(stamps).date(), max(stamps).date()
    sessions = exchange_sessions(first, last)
    minutes, five_minutes = normalize_minutes(records, sessions)
    imported_at = datetime.now(timezone.utc)
    if sessions[-1].close > imported_at:
        raise ValueError("Sample contains a session that has not completed")
    directory = Path(output_dir).resolve() / (imported_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.parent.chmod(0o700)
    directory.mkdir(mode=0o700, exist_ok=False)
    raw_dir = directory / "raw"
    raw_dir.mkdir(mode=0o700)
    (raw_dir / "sample.zip").write_bytes(archive_bytes)
    (raw_dir / MINUTE_MEMBER).write_bytes(minute_bytes)
    (raw_dir / README_MEMBER).write_bytes(readme)
    (directory / "sessions.json").write_text(json.dumps([s.as_dict() for s in sessions], indent=2) + "\n")
    for period, bars in (("1Min", minutes), ("5Min", five_minutes)):
        _write_bars(directory / f"QQQ-{period}.csv", bars, sessions)
        _write_parquet(directory / f"QQQ-{period}.parquet", bars, sessions)
    for path in directory.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    files = [{"path": str(path.relative_to(directory)), "sha256": sha256_file(path)}
             for path in sorted(directory.rglob("*")) if path.is_file()]
    manifest = {
        "schema_version": "firstrate-sample-rth-bars-v1", "source": "firstrate",
        "feed": "firstrate_aggregate", "adjustment": "split", "symbols": ["QQQ"],
        "start": first.isoformat(), "end": last.isoformat(), "synthetic": False,
        "sample_only": True, "research_only": True, "live_feed": False,
        "source_url": SAMPLE_URL, "product_url": PRODUCT_URL, "license_url": LICENSE_URL,
        "imported_at": imported_at.isoformat(), "retrieved_at": retrieved_at,
        "source_acquisition": source_acquisition,
        "retrieval_time_status": "provided" if retrieved_at is not None else "unknown_local_import",
        "source_timezone": "America/New_York", "calendar": "XNYS",
        "timestamp_convention": "interval_start", "availability": "interval_end",
        "gap_policy": "reject_no_forward_fill", "sessions": "sessions.json",
        "bars": {"QQQ": {"1Min": "QQQ-1Min.csv", "5Min": "QQQ-5Min.csv"}},
        "counts": {"QQQ": {"raw_1Min": len(records), "1Min": len(minutes), "5Min": len(five_minutes),
                            "sessions": len(sessions)}}, "files": files,
        "license_scope": "Private evaluation and internal models; do not redistribute raw data; attribute published research",
        "limitations": ["Official sample only; short coverage cannot establish robust out-of-sample performance",
                        "Vendor split-adjusted aggregation is not Alpaca SIP or IEX",
                        "No executable bid/ask quotes or order-book depth",
                        "Bar-derived VWAP approximation; not trade-level VWAP",
                        "Do not mix with other feeds without a separately evaluated feature/volume definition"],
    }
    keys = ("schema_version", "start", "end", "symbols", "feed", "adjustment", "files")
    manifest["dataset_sha256"] = hashlib.sha256(json.dumps(
        {key: manifest[key] for key in keys}, sort_keys=True).encode()).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    (directory / "manifest.json").chmod(0o600)
    return dict(manifest, directory=str(directory))


def import_firstrate_sample(archive_path, output_dir="private-data/firstrate", *, retrieved_at=None):
    """Import a local archive as an unverified vendor claim, even with a date.

    Filename, README markers and checksums cannot establish vendor origin.
    Only the downloader records an HTTPS acquisition through this API.
    """
    return _import_firstrate_sample(archive_path, output_dir, retrieved_at=retrieved_at)


def _tls_context():
    """Use bundled public CA roots when available; never relax verification."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def download_firstrate_sample(output_dir="private-data/firstrate"):
    """One bounded GET of the vendor's freely linked sample; no credentials."""
    request = Request(SAMPLE_URL, headers={"User-Agent": "dwight-research/0.2", "Accept": "application/zip"}, method="GET")
    with urlopen(request, timeout=45, context=_tls_context()) as response:
        content = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("QQQ sample archive exceeds size limit")
    downloaded_at = datetime.now(timezone.utc).isoformat()
    # Keep the response even if vendor formatting changes and normalization fails.
    output = Path(output_dir)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    downloads = output / "downloads"
    downloads.mkdir(mode=0o700, exist_ok=True)
    downloads.chmod(0o700)
    source_dir = downloads / uuid4().hex
    source_dir.mkdir(mode=0o700, exist_ok=False)
    path = source_dir / "frd_sample_etf_QQQ.zip"
    path.write_bytes(content)
    path.chmod(0o600)
    (source_dir / "download.json").write_text(json.dumps({"source_url": SAMPLE_URL,
        "retrieved_at": downloaded_at, "sha256": sha256_file(path), "bytes": len(content)}, indent=2) + "\n")
    (source_dir / "download.json").chmod(0o600)
    return _import_firstrate_sample(path, output_dir, retrieved_at=downloaded_at,
                                   source_acquisition="https_download")
