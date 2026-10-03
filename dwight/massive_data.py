"""Private, bounded Massive QQQ historical readers with no trading endpoints.

``fetch_massive_history`` reads raw, one-minute aggregates over [start, end).
Rows use UTC interval-start timestamps and retain Massive's volume definition.
The caller must validate complete exchange sessions before creating a dataset.
Reference metadata checks do not prove bar, real-time or account entitlement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import json
import os
import ssl
import time
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from vwap_bot.engine import Bar

UTC = timezone.utc
MASSIVE_HOST = "api.massive.com"
REFERENCE_URL = "https://api.massive.com/v3/reference/tickers/QQQ"
AGGREGATES_PREFIX = "/v2/aggs/ticker/QQQ/range/1/minute/"
FEED = "massive_stocks_aggregates"
MAX_PAGE_BYTES = 16 * 1024 * 1024
MAX_CHECK_BYTES = 262_144
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ROWS = 2_000_000
MAX_DURATION_SECONDS = 300

_MESSAGES = {
    "missing_credentials": "Set MASSIVE_API_KEY privately before checking Massive.",
    "invalid_credentials": "The private Massive key has an invalid format.",
    "authentication_failed": "Massive rejected authentication. Check the private API key.",
    "permission_denied": "Massive denied access. Check the selected data plan and provider availability.",
    "rate_limited": "Massive rate limited this request. Retry later; no automatic retry was made.",
    "unavailable": "Massive could not be reached. No automatic retry was made.",
    "redirect_rejected": "Massive returned a redirect. Credentials were not forwarded.",
    "invalid_response": "Massive returned an unexpected response. Data was not accepted.",
    "bound_reached": "Massive history exceeded the configured request bounds. Choose a smaller window.",
}


class MassiveDataError(RuntimeError):
    """A sanitized provider failure, excluding payloads, URLs and credentials."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(_MESSAGES[status])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_transport():
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        pass
    else:
        context.load_verify_locations(cafile=certifi.where())
    return build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context)).open


def _key(environ: Mapping[str, str] | None) -> str:
    env = os.environ if environ is None else environ
    key = env.get("MASSIVE_API_KEY", "")
    if not key:
        raise MassiveDataError("missing_credentials")
    if (not isinstance(key, str) or len(key) > 512
            or any(ord(character) < 33 or ord(character) > 126 for character in key)):
        raise MassiveDataError("invalid_credentials")
    return key


def _status(code):
    if type(code) is not int:
        raise MassiveDataError("invalid_response")
    if 300 <= code < 400:
        raise MassiveDataError("redirect_rejected")
    if code == 401:
        raise MassiveDataError("authentication_failed")
    if code in (403, 451):
        raise MassiveDataError("permission_denied")
    if code == 429:
        raise MassiveDataError("rate_limited")
    if code >= 500:
        raise MassiveDataError("unavailable")
    if code != 200:
        raise MassiveDataError("invalid_response")


def _reject_nonfinite(_):
    raise ValueError("Non-finite JSON")


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys")
        result[key] = value
    return result


def _contains_key(value, key):
    if isinstance(value, str):
        return key in value
    if isinstance(value, dict):
        return any(_contains_key(k, key) or _contains_key(v, key) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _read(url, key, transport, *, timeout, byte_limit):
    # URL paths are internally constructed and validated again before transport.
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != MASSIVE_HOST or parsed.fragment
            or not (parsed.path == "/v3/reference/tickers/QQQ"
                    or parsed.path.startswith(AGGREGATES_PREFIX))):
        raise MassiveDataError("invalid_response")
    if any(name.lower() in ("apikey", "api_key", "token", "key", "authorization")
           for name, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise MassiveDataError("invalid_response")
    request = Request(url, headers={"Authorization": "Bearer " + key,
                                  "Accept": "application/json",
                                  "User-Agent": "dwight-massive-history/0.1"}, method="GET")
    try:
        opener = transport or _default_transport()
        with opener(request, timeout=timeout) as response:
            _status(response.getcode())
            if response.geturl() != url:
                raise MassiveDataError("redirect_rejected")
            # Production HTTPResponse supports read1: bound both body size and
            # elapsed read time even when a server drips bytes continuously.
            # The simple read fallback supports small injected fixture streams.
            read_chunk = getattr(response, "read1", None)
            if read_chunk is None:
                raw = response.read(byte_limit + 1)
            else:
                deadline = time.monotonic() + timeout
                chunks, received = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise MassiveDataError("bound_reached")
                    chunk = read_chunk(min(65_536, byte_limit + 1 - received))
                    if not isinstance(chunk, bytes):
                        raise MassiveDataError("invalid_response")
                    if time.monotonic() >= deadline:
                        raise MassiveDataError("bound_reached")
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if received > byte_limit:
                        raise MassiveDataError("bound_reached")
                raw = b"".join(chunks)
        if not isinstance(raw, bytes) or len(raw) > byte_limit:
            raise MassiveDataError("bound_reached")
        payload = json.loads(raw, parse_constant=_reject_nonfinite,
                             object_pairs_hook=_reject_duplicates)
        if (not isinstance(payload, dict) or payload.get("status") not in ("OK", "DELAYED")
                or "error" in payload or "message" in payload or _contains_key(payload, key)):
            raise MassiveDataError("invalid_response")
        return payload, raw
    except HTTPError as exc:
        code = exc.code
        exc.close()
        try:
            _status(code)
        except MassiveDataError as failure:
            # A provider reason or URL can echo credentials. Suppress the
            # original HTTPError in programmatic tracebacks as well as JSON.
            raise MassiveDataError(failure.status) from None
        raise MassiveDataError("invalid_response") from None
    except MassiveDataError:
        raise
    except (URLError, TimeoutError, OSError, HTTPException):
        raise MassiveDataError("unavailable") from None
    except (ValueError, TypeError, RecursionError):
        raise MassiveDataError("invalid_response") from None


def check_massive_connection(environ: Mapping[str, str] | None = None, *, transport=None) -> dict:
    """Read fixed QQQ reference metadata once; never fetch bars or save a payload.

    Optional transport follows urllib's ``open(request, timeout=...)`` contract.
    The result is JSON-safe and excludes all provider fields and key material.
    """
    capabilities = {"reference_metadata_read": False, "public_market_data": False,
                    "account_read": False, "qqq_data": False, "order_execution": False}
    try:
        key = _key(environ)
        payload, _ = _read(REFERENCE_URL, key, transport, timeout=10,
                           byte_limit=MAX_CHECK_BYTES)
        ticker = payload.get("results")
        if (not isinstance(ticker, dict) or ticker.get("ticker") != "QQQ"
                or ticker.get("market") != "stocks" or ticker.get("locale") != "us"):
            raise MassiveDataError("invalid_response")
        capabilities["reference_metadata_read"] = True
        status = "reference_metadata_available"
        message = ("Private Massive authentication and QQQ reference metadata are reachable. "
                   "Minute history and real-time entitlement have not been verified.")
    except MassiveDataError as exc:
        status, message = exc.status, str(exc)
    return {"platform": "massive", "checked_at": datetime.now(UTC).isoformat(),
            "status": status, "capabilities": capabilities, "message": message}


def _milliseconds(stamp):
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = stamp.astimezone(UTC) - epoch
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _next_url(value, path):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise MassiveDataError("invalid_response")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != MASSIVE_HOST
            or parsed.path != path or parsed.fragment):
        raise MassiveDataError("invalid_response")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    names = [name for name, _ in pairs]
    if (len(names) != len(set(names)) or set(names) - {"cursor", "adjusted", "sort", "limit"}
            or not any(name == "cursor" and value for name, value in pairs)):
        raise MassiveDataError("invalid_response")
    options = dict(pairs)
    if (options.get("adjusted", "false") != "false" or options.get("sort", "asc") != "asc"
            or options.get("limit", "50000") != "50000"):
        raise MassiveDataError("invalid_response")
    # Retain the server cursor and enforce the same raw/ascending policy.
    return urlunsplit(("https", MASSIVE_HOST, path,
                       urlencode({"cursor": options["cursor"], "adjusted": "false",
                                  "sort": "asc", "limit": "50000"}), ""))


def fetch_massive_history(start: datetime, end: datetime,
                         environ: Mapping[str, str] | None = None, *, transport=None,
                         max_pages: int = 100, max_bytes: int = 64 * 1024 * 1024,
                         timeout_seconds: float = 30,
                         max_duration_seconds: float = 120) -> dict:
    """Return raw QQQ minutes, successful HTTP bodies and explicit provenance.

    Bounds are timezone-aware and half-open [start, end), on exact minute
    boundaries. History only: the final interval must have ended. Limits cap
    pages, total bytes, rows, per-request time and overall elapsed duration.
    Requests use only the fixed Massive host with Bearer authentication;
    redirects, environment proxies and automatic retries are disabled.
    """
    if (not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.tzinfo is None or end.tzinfo is None or start >= end
            or start.second or start.microsecond or end.second or end.microsecond):
        raise ValueError("Use ordered timezone-aware minute bounds [start, end)")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start < datetime(1970, 1, 1, tzinfo=UTC) or end > datetime.now(UTC):
        raise ValueError("Massive history must use completed intervals after 1970")
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise ValueError("max_pages must be an integer from 1 to 1000")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_TOTAL_BYTES:
        raise ValueError("max_bytes must be an integer from 1 to 268435456")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 60):
        raise ValueError("timeout_seconds must be positive and at most 60")
    if (isinstance(max_duration_seconds, bool)
            or not isinstance(max_duration_seconds, (int, float))
            or not 0 < max_duration_seconds <= MAX_DURATION_SECONDS):
        raise ValueError("max_duration_seconds must be positive and at most 300")
    key = _key(environ)
    path = AGGREGATES_PREFIX + f"{_milliseconds(start)}/{_milliseconds(end) - 1}"
    url = urlunsplit(("https", MASSIVE_HOST, path,
                     urlencode({"adjusted": "false", "sort": "asc", "limit": "50000"}), ""))
    initial_url = url
    records, raw_pages, visited = [], [], set()
    total_bytes, latest = 0, None
    began = time.monotonic()
    for _ in range(max_pages):
        remaining = max_duration_seconds - (time.monotonic() - began)
        if remaining <= 0 or total_bytes >= max_bytes:
            raise MassiveDataError("bound_reached")
        if url in visited:
            raise MassiveDataError("invalid_response")
        visited.add(url)
        payload, raw = _read(url, key, transport, timeout=min(timeout_seconds, remaining),
                             byte_limit=min(MAX_PAGE_BYTES, max_bytes - total_bytes))
        total_bytes += len(raw)
        if time.monotonic() - began > max_duration_seconds:
            raise MassiveDataError("bound_reached")
        if payload.get("ticker") != "QQQ" or payload.get("adjusted") is not False:
            raise MassiveDataError("invalid_response")
        rows = payload.get("results", [])
        if not isinstance(rows, list) or len(rows) > 50000:
            raise MassiveDataError("invalid_response")
        if "resultsCount" in payload and (type(payload["resultsCount"]) is not int
                                           or payload["resultsCount"] != len(rows)):
            raise MassiveDataError("invalid_response")
        next_value = payload.get("next_url")
        next_url = _next_url(next_value, path) if next_value is not None else None
        for row in rows:
            try:
                if (not isinstance(row, dict) or type(row["t"]) is not int or row["t"] % 60000
                        or any(isinstance(row[name], bool) or not isinstance(row[name], (int, float))
                               for name in ("o", "h", "l", "c", "v"))):
                    raise ValueError("Malformed minute")
                stamp = datetime.fromtimestamp(row["t"] / 1000, UTC)
                bar = Bar(stamp, *(float(row[name]) for name in ("o", "h", "l", "c", "v")))
                if (not start <= stamp < end or stamp + timedelta(minutes=1) > end
                        or latest is not None and stamp <= latest):
                    raise ValueError("Out-of-order minute")
            except (ValueError, KeyError, TypeError, OverflowError, OSError):
                raise MassiveDataError("invalid_response") from None
            latest = stamp
            records.append({"t": stamp.isoformat(), "o": bar.open, "h": bar.high,
                            "l": bar.low, "c": bar.close, "v": bar.volume})
            if len(records) > MAX_ROWS:
                raise MassiveDataError("bound_reached")
        raw_pages.append(raw)
        if next_url is None:
            return {"records": records, "raw_pages": raw_pages,
                    "provenance": {"source": "massive", "feed": FEED, "adjustment": "raw",
                                   "endpoint": initial_url.split("?", 1)[0],
                                   "request_start": start.isoformat(), "request_end": end.isoformat(),
                                   "request_bounds": "half_open", "symbols": ["QQQ"],
                                   "timestamp_convention": "interval_start",
                                   "availability": "interval_end", "raw_page_count": len(raw_pages),
                                   "volume_definition": "Massive_eligible_trade_minute_aggregate_volume",
                                   "limitations": ["Minute aggregates can omit intervals with no eligible trades",
                                                   "No quote or spread data in this bar dataset",
                                                   "Bar-derived VWAP is an approximation",
                                                   "This request does not verify real-time entitlement"]}}
        url = next_url
    raise MassiveDataError("bound_reached")
