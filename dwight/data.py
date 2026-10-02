"""Reproducible Alpaca history with strict, calendar-aware RTH validation.

Dates are inclusive exchange-session dates. The normalized bar timestamp is
its interval START; a row is available only after the full interval has ended.
Raw HTTP response bytes are preserved separately and never contain headers.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import time
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo

from vwap_bot.engine import Bar

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
SCHEMA_VERSION = "alpaca-rth-bars-v1"


@dataclass(frozen=True)
class Session:
    date: str
    open: datetime
    close: datetime

    def __post_init__(self):
        date.fromisoformat(self.date)
        if self.open.tzinfo is None or self.close.tzinfo is None:
            raise ValueError("Session bounds must be timezone aware")
        duration = (self.close - self.open).total_seconds()
        if duration <= 0 or duration % 300:
            raise ValueError("Session duration must be a positive multiple of five minutes")
        if self.open.astimezone(NY).date().isoformat() != self.date:
            raise ValueError("Session date does not match its open")

    def as_dict(self):
        return {"date": self.date, "open": self.open.isoformat(), "close": self.close.isoformat()}


def _date(value: str | date) -> date:
    if isinstance(value, datetime):
        raise ValueError("Use YYYY-MM-DD session dates, not datetime bounds")
    return value if isinstance(value, date) else date.fromisoformat(value)


def exchange_sessions(start: str | date, end: str | date) -> list[Session]:
    """XNYS regular sessions, including holidays, DST and early closes."""
    first, last = _date(start), _date(end)
    if first > last:
        raise ValueError("start must not be after end")
    try:
        import exchange_calendars as xcals
    except ImportError:
        raise RuntimeError("Historical data requires exchange_calendars; install Dwight's data extra") from None
    # Calendar bounds are session bounds; pad holidays/weekends at both ends.
    cal = xcals.get_calendar("XNYS", start=(first - timedelta(days=10)).isoformat(),
                             end=(last + timedelta(days=10)).isoformat())
    sessions = []
    for label in cal.sessions_in_range(first.isoformat(), last.isoformat()):
        opened = cal.session_open(label).to_pydatetime().astimezone(UTC)
        closed = cal.session_close(label).to_pydatetime().astimezone(UTC)
        sessions.append(Session(label.date().isoformat(), opened, closed))
    return sessions


def session_close_for_bar(sessions: Iterable[Session], timestamp: datetime) -> datetime:
    """Get the actual close so consumers can exit on the last five-minute bar."""
    if timestamp.tzinfo is None:
        raise ValueError("Bar timestamp must be timezone aware")
    for session in sessions:
        if session.open <= timestamp < session.close:
            return session.close
    raise ValueError("Timestamp is outside the supplied exchange sessions")


def load_sessions(path: str | Path) -> list[Session]:
    rows = json.loads(Path(path).read_text())
    return [Session(row["date"], datetime.fromisoformat(row["open"]),
                    datetime.fromisoformat(row["close"])) for row in rows]


def normalize_minutes(records: Iterable[dict], sessions: Iterable[Session], *,
                      require_complete_session: bool = True,
                      as_of: datetime | None = None) -> tuple[list[Bar], list[Bar]]:
    """Validate every raw minute and aggregate complete sessions; never fill gaps.

    Extended-hours rows are validated but excluded. An absent regular-session
    minute, including a no-trade minute, fails closed: fabricating its OHLCV
    would alter VWAP. IEX may therefore fail this stricter research contract.
    """
    if not require_complete_session and (as_of is None or as_of.tzinfo is None):
        raise ValueError("Partial normalization requires an aware as_of timestamp")
    sessions = list(sessions)
    if not sessions:
        raise ValueError("No exchange sessions in requested range")
    by_day = {session.date: session for session in sessions}
    if len(by_day) != len(sessions):
        raise ValueError("Duplicate session dates")
    if sessions != sorted(sessions, key=lambda s: s.open):
        raise ValueError("Sessions must be in chronological order")
    minutes = {}
    seen = set()
    for row in records:
        try:
            timestamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("Minute timestamp must have a timezone")
            timestamp = timestamp.astimezone(UTC)
            if timestamp.second or timestamp.microsecond:
                raise ValueError("Expected start-labelled one-minute bars")
            if timestamp in seen:
                raise ValueError(f"Duplicate minute at {timestamp.isoformat()}")
            seen.add(timestamp)
            if any(isinstance(row[key], bool) for key in ("o", "h", "l", "c", "v")):
                raise ValueError("OHLCV cannot contain booleans")
            bar = Bar(timestamp, *(float(row[key]) for key in ("o", "h", "l", "c", "v")))
        except (KeyError, AttributeError, TypeError) as exc:
            raise ValueError("Malformed Alpaca minute bar") from exc
        session = by_day.get(timestamp.astimezone(NY).date().isoformat())
        if session and session.open <= timestamp < session.close:
            minutes[timestamp] = bar
    normalized, five_minute = [], []
    for session in sessions:
        cutoff = session.close if require_complete_session else min(session.close, as_of)
        # Return only minute groups whose entire five-minute interval is complete.
        expected = max(0, int((cutoff - session.open).total_seconds() // 300) * 5)
        session_bars = []
        for offset in range(expected):
            stamp = session.open + timedelta(minutes=offset)
            if stamp not in minutes:
                raise ValueError(f"Missing regular-session minute at {stamp.isoformat()}; no forward fill")
            session_bars.append(minutes[stamp])
        normalized.extend(session_bars)
        for offset in range(0, expected, 5):
            group = session_bars[offset:offset + 5]
            five_minute.append(Bar(group[0].timestamp, group[0].open,
                                   max(bar.high for bar in group), min(bar.low for bar in group),
                                   group[-1].close, sum(bar.volume for bar in group)))
    return normalized, five_minute


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward broker credentials to a redirected host.
        return None


def _request_page(url: str, headers: dict[str, str]) -> bytes:
    opener = build_opener(_NoRedirect())
    for attempt in range(4):
        try:
            with opener.open(Request(url, headers=headers, method="GET"), timeout=45) as response:
                return response.read()
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 429 or 500 <= status < 600:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
            raise RuntimeError(f"Alpaca historical data returned HTTP {status}; check credentials, entitlement and dates") from None
        except (URLError, TimeoutError, OSError):
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("Alpaca historical data connection failed after bounded retries") from None
    raise RuntimeError("Alpaca request exhausted retries")


def _credentials(environ: Mapping[str, str] | None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    key = env.get("APCA_API_KEY_ID") or env.get("ALPACA_API_KEY", "")
    secret = env.get("APCA_API_SECRET_KEY") or env.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY privately before downloading history")
    if any("\r" in value or "\n" in value for value in (key, secret)):
        raise ValueError("Invalid credential format")
    return key, secret


def fetch_alpaca_bars(start: datetime, end: datetime, symbols: Iterable[str] = ("SPY", "QQQ"),
                      feed: str = "sip", environ: Mapping[str, str] | None = None,
                      *, max_pages: int = 1000) -> dict[str, list[dict]]:
    """Read current or historical 1m bars. Bounds are inclusive; no order API.

    This lightweight reader does not persist raw responses. For research history
    use download_alpaca_dataset. Pass the result to normalize_minutes with an
    explicit as_of to exclude the currently forming five-minute candle.
    """
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise ValueError("Provide ordered timezone-aware datetime bounds")
    if feed not in ("sip", "iex"):
        raise ValueError("feed must be explicitly sip or iex")
    if isinstance(symbols, str):
        symbols = symbols.split(",")
    symbols = tuple(symbols)
    if not symbols or len(set(symbols)) != len(symbols) or any(
            not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol)
            for symbol in symbols):
        raise ValueError("Provide unique uppercase stock symbols")
    if type(max_pages) is not int or not 1 <= max_pages <= 100000:
        raise ValueError("max_pages must be an integer from 1 to 100000")
    key, secret = _credentials(environ)
    query = {"symbols": ",".join(symbols), "timeframe": "1Min", "start": start.isoformat(),
             "end": end.isoformat(), "feed": feed, "adjustment": "raw", "asof": "-",
             "limit": 10000, "sort": "asc"}
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    result, tokens = {symbol: [] for symbol in symbols}, set()
    for _ in range(max_pages):
        payload = json.loads(_request_page(ALPACA_BARS_URL + "?" + urlencode(query), headers))
        if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict):
            raise ValueError("Alpaca response does not contain a bars object")
        if set(payload["bars"]) - set(symbols) or any(not isinstance(rows, list) for rows in payload["bars"].values()):
            raise ValueError("Alpaca response contains unexpected symbols or malformed bars")
        for symbol, rows in payload["bars"].items():
            result[symbol].extend(rows)
        token = payload.get("next_page_token")
        if not token:
            return result
        if not isinstance(token, str) or token in tokens:
            raise ValueError("Invalid or repeated Alpaca pagination token")
        tokens.add(token)
        query["page_token"] = token
    raise ValueError("Alpaca page limit reached before history was complete")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bars(path: Path, bars: list[Bar], sessions: list[Session]) -> None:
    closes = {session.date: session.close.isoformat() for session in sessions}
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "session_close"])
        for bar in bars:
            writer.writerow([bar.timestamp.isoformat(), bar.open, bar.high, bar.low, bar.close,
                             bar.volume, closes[bar.timestamp.astimezone(NY).date().isoformat()]])


def _write_parquet(path: Path, bars: list[Bar], sessions: list[Session]) -> bool:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False
    closes = {session.date: session.close for session in sessions}
    table = pa.table({
        "timestamp": pa.array([bar.timestamp for bar in bars], type=pa.timestamp("us", tz="UTC")),
        **{key: [getattr(bar, key) for bar in bars] for key in ("open", "high", "low", "close", "volume")},
        "session_close": pa.array([closes[bar.timestamp.astimezone(NY).date().isoformat()] for bar in bars],
                                  type=pa.timestamp("us", tz="UTC")),
    })
    pq.write_table(table, path, compression="zstd")
    return True


def _records(pages: list[Path], symbol: str):
    for page in pages:
        data = json.loads(page.read_bytes())
        yield from data["bars"].get(symbol, [])


def download_alpaca_dataset(output_dir: str | Path, start: str | date, end: str | date,
                            symbols: Iterable[str] = ("SPY", "QQQ"), feed: str = "sip",
                            adjustment: str = "raw", environ: Mapping[str, str] | None = None,
                            *, now: datetime | None = None, max_pages: int = 10000) -> dict:
    """Download authenticated history into a new immutable-by-convention dataset.

    Credentials use APCA_API_KEY_ID / APCA_API_SECRET_KEY (with ALPACA_API_KEY /
    ALPACA_SECRET_KEY aliases when the corresponding APCA variable is empty). This
    function has no order endpoint. CSV is always available for current replay;
    Parquet is also written when the data extra's pyarrow dependency is installed.
    Returns the manifest plus an absolute `directory` (not stored in manifest).
    Failed runs retain raw pages and failed.json, never a success manifest.
    """
    first, last = _date(start), _date(end)
    if first > last:
        raise ValueError("start must not be after end")
    if feed not in ("sip", "iex"):
        raise ValueError("feed must be explicitly sip or iex")
    if adjustment not in ("raw", "split"):
        raise ValueError("Supported adjustment policies are raw and split")
    if isinstance(symbols, str):
        symbols = symbols.split(",")
    symbols = tuple(symbols)
    if not symbols or len(set(symbols)) != len(symbols) or any(
            not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol)
            for symbol in symbols):
        raise ValueError("Provide unique uppercase stock symbols")
    if type(max_pages) is not int or not 1 <= max_pages <= 100000:
        raise ValueError("max_pages must be an integer from 1 to 100000")
    key, secret = _credentials(environ)
    sessions = exchange_sessions(first, last)
    if not sessions:
        raise ValueError("No exchange sessions in requested range")
    collected_at = now or datetime.now(UTC)
    if collected_at.tzinfo is None:
        raise ValueError("now must include a timezone")
    if sessions[-1].close + timedelta(minutes=1) > collected_at:
        raise ValueError("Requested final session has not fully completed")
    directory = Path(output_dir).resolve() / (collected_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
    directory.mkdir(parents=True, exist_ok=False)
    raw_dir = directory / "raw"
    raw_dir.mkdir()
    query = {"symbols": ",".join(symbols), "timeframe": "1Min", "start": sessions[0].open.isoformat(),
             "end": (sessions[-1].close - timedelta(microseconds=1)).isoformat(), "feed": feed,
             "adjustment": adjustment, "asof": "-", "limit": 10000, "sort": "asc"}
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    pages, page_info, tokens = [], [], set()
    try:
        for number in range(1, max_pages + 1):
            content = _request_page(ALPACA_BARS_URL + "?" + urlencode(query), headers)
            page = raw_dir / f"page-{number:06d}.json"
            page.write_bytes(content)
            pages.append(page)
            page_info.append({"path": str(page.relative_to(directory)), "sha256": sha256_file(page),
                              "retrieved_at": datetime.now(UTC).isoformat()})
            payload = json.loads(content)
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict):
                raise ValueError("Alpaca response does not contain a bars object")
            if set(payload["bars"]) - set(symbols) or any(not isinstance(rows, list) for rows in payload["bars"].values()):
                raise ValueError("Alpaca response contains unexpected symbols or malformed bars")
            token = payload.get("next_page_token")
            if not token:
                break
            if not isinstance(token, str) or token in tokens:
                raise ValueError("Invalid or repeated Alpaca pagination token")
            tokens.add(token)
            query["page_token"] = token
        else:
            raise ValueError("Alpaca page limit reached before history was complete")
        session_path = directory / "sessions.json"
        session_path.write_text(json.dumps([session.as_dict() for session in sessions], indent=2) + "\n")
        files = [{"path": "sessions.json", "sha256": sha256_file(session_path)}]
        counts, bar_paths = {}, {}
        for symbol in symbols:
            minutes, five_minutes = normalize_minutes(_records(pages, symbol), sessions)
            counts[symbol] = {"1Min": len(minutes), "5Min": len(five_minutes)}
            bar_paths[symbol] = {}
            for timeframe, bars in (("1Min", minutes), ("5Min", five_minutes)):
                csv_path = directory / f"{symbol}-{timeframe}.csv"
                _write_bars(csv_path, bars, sessions)
                files.append({"path": csv_path.name, "sha256": sha256_file(csv_path)})
                bar_paths[symbol][timeframe] = csv_path.name
                parquet_path = csv_path.with_suffix(".parquet")
                if _write_parquet(parquet_path, bars, sessions):
                    files.append({"path": parquet_path.name, "sha256": sha256_file(parquet_path)})
        try:
            calendar_version = version("exchange_calendars")
        except PackageNotFoundError:
            calendar_version = "unavailable"
        manifest = {"schema_version": SCHEMA_VERSION, "source": "alpaca", "endpoint": ALPACA_BARS_URL,
                    "retrieved_at": collected_at.isoformat(), "start": first.isoformat(), "end": last.isoformat(),
                    "symbols": list(symbols), "feed": feed, "adjustment": adjustment, "asof": "-",
                    "calendar": "XNYS", "calendar_version": calendar_version,
                    "timestamp_convention": "interval_start", "availability": "interval_end",
                    "session_policy": "complete_regular_sessions_only", "gap_policy": "reject_no_forward_fill",
                    "volume_definition": "sum_of_source_one_minute_volume", "counts": counts,
                    "bars": bar_paths, "sessions": "sessions.json", "raw_pages": page_info, "files": files,
                    "limitations": ["No quote/spread data in this bar dataset", "Bar-derived VWAP is an approximation",
                                    "Historical revisions and split adjustments are not point-in-time corporate-action data"]}
        fingerprint = {key: manifest[key] for key in ("schema_version", "start", "end", "symbols", "feed", "adjustment", "files")}
        manifest["dataset_sha256"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
        return dict(manifest, directory=str(directory))
    except Exception as exc:
        # Do not persist provider error bodies, exception strings or credentials.
        (directory / "failed.json").write_text(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                                                          "raw_pages": page_info}, indent=2) + "\n")
        raise
