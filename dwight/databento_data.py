"""Private, bounded Databento historical OHLCV reads for QQQ research.

Metadata requests are free according to Databento's historical API documentation.
The connectivity probe only verifies metadata authentication. It never streams
billable time series, proves QQQ entitlement, or accesses a trading account.

``estimate_databento_history`` is a non-billable planning operation. Retrieval
requires an explicit finite ``max_cost_usd`` and checks a fresh provider estimate
before exactly one streaming request. This is an estimated-cost gate, not a
provider-enforced cap on the final invoice. Failed streaming reads are never
automatically retried because duplicate requests can incur duplicate charges.

The injectable ``transport(request, *, timeout)`` returns urllib-style responses
with read(size), getcode(), geturl() and context-manager methods. No function
runs at import time, reads a .env file, writes files, or reflects error bodies.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
import re
import ssl
import time
from typing import Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

UTC = timezone.utc
HISTORICAL_URL = "https://hist.databento.com/v0/"
DATASET_LIST_URL = HISTORICAL_URL + "metadata.list_datasets"
COST_URL = HISTORICAL_URL + "metadata.get_cost"
COUNT_URL = HISTORICAL_URL + "metadata.get_record_count"
RANGE_URL = HISTORICAL_URL + "timeseries.get_range"
SCHEMA = "ohlcv-1m"
MAX_METADATA_BYTES = 262_144
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_RECORDS = 1_000_000
DEFAULT_TIMEOUT = 30
MAX_RANGE_DAYS = 3660

_DATASET_PATTERN = re.compile(r"[A-Z0-9]{2,16}\.[A-Z0-9]{2,16}\Z")
_KEY_PATTERN = re.compile(r"db-[A-Za-z0-9]{29}\Z")
_MINUTE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:\.0{1,9})?(?:Z|\+00:00)\Z")
_ERRORS = {
    "authentication_failed": "Databento authentication was rejected. Replace the private API key.",
    "permission_denied": "Databento access was denied. Check the account and dataset permissions.",
    "rate_limited": "Databento rate limited this request. Retry later.",
    "unavailable": "Databento could not be reached or is temporarily unavailable.",
    "redirect_rejected": "Databento returned a redirect. It was rejected without forwarding the key.",
    "invalid_response": "Databento returned an unexpected, incomplete or oversized response.",
    "invalid_request": "Databento rejected the requested dataset, schema or historical range.",
}


class DatabentoDataError(RuntimeError):
    """A provider failure with a fixed, safe message and machine-readable status."""
    def __init__(self, status: str):
        self.status = status
        super().__init__(_ERRORS[status])


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
    key = env.get("DATABENTO_API_KEY", "")
    if not key:
        raise RuntimeError("Set DATABENTO_API_KEY privately before checking or downloading history")
    if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
        raise ValueError("Private Databento credential format is invalid")
    return key


def _authorization(key: str) -> str:
    return "Basic " + base64.b64encode((key + ":").encode("ascii")).decode("ascii")


def _status(code):
    if type(code) is not int:
        raise DatabentoDataError("invalid_response")
    if 300 <= code < 400:
        raise DatabentoDataError("redirect_rejected")
    if code == 401:
        raise DatabentoDataError("authentication_failed")
    if code in (403, 451):
        raise DatabentoDataError("permission_denied")
    if code == 429:
        raise DatabentoDataError("rate_limited")
    if code >= 500:
        raise DatabentoDataError("unavailable")
    if code in (400, 404, 406, 422):
        raise DatabentoDataError("invalid_request")
    if code != 200:
        raise DatabentoDataError("invalid_response")


def _bounds(max_bytes, max_records, timeout):
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 128 * 1024 * 1024:
        raise ValueError("max_bytes must be an integer from 1024 to 134217728")
    if type(max_records) is not int or not 1 <= max_records <= 1_000_000:
        raise ValueError("max_records must be an integer from 1 to 1000000")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("timeout must be finite, positive and at most 60 seconds")


def _read(url: str, key: str, *, transport, timeout, max_bytes, form=None) -> bytes:
    parsed = urlsplit(url)
    allowed_paths = {"/v0/metadata.list_datasets", "/v0/metadata.get_cost",
                     "/v0/metadata.get_record_count", "/v0/timeseries.get_range"}
    if (parsed.scheme != "https" or parsed.netloc != "hist.databento.com"
            or parsed.path not in allowed_paths or parsed.fragment
            or (form is not None and url != RANGE_URL)):
        raise ValueError("Databento requests require the fixed historical endpoints")
    auth = _authorization(key)
    headers = {"Authorization": auth, "Accept": "application/json",
               "Accept-Encoding": "identity", "User-Agent": "dwight-databento-history/0.1"}
    body = None
    if form is not None:
        body = urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, headers=headers, data=body, method="POST" if form is not None else "GET")
    started = time.monotonic()
    try:
        opener = transport or _default_transport()
        with opener(request, timeout=timeout) as response:
            _status(response.getcode())
            if response.geturl() != url:
                raise DatabentoDataError("redirect_rejected")
            # HTTPResponse.read(size) can wait for the full size while a peer
            # drips bytes. read1 returns after an available socket chunk, so
            # elapsed-time checks can interrupt that stream. Keep read only as
            # the fallback for small injected fixture responses.
            read_chunk = getattr(response, "read1", response.read)
            chunks, total = [], 0
            while True:
                if time.monotonic() - started > timeout:
                    raise DatabentoDataError("unavailable")
                chunk = read_chunk(min(65_536, max_bytes + 1 - total))
                if time.monotonic() - started > timeout:
                    raise DatabentoDataError("unavailable")
                if not isinstance(chunk, bytes):
                    raise DatabentoDataError("invalid_response")
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DatabentoDataError("invalid_response")
                chunks.append(chunk)
            raw = b"".join(chunks)
        if time.monotonic() - started > timeout:
            raise DatabentoDataError("unavailable")
    except HTTPError as exc:
        status = exc.code
        exc.close()
        try:
            _status(status)
        except DatabentoDataError as failure:
            raise DatabentoDataError(failure.status) from None
        raise DatabentoDataError("invalid_response") from None
    except DatabentoDataError:
        raise
    except Exception:
        raise DatabentoDataError("unavailable") from None
    # Raw evidence must never contain reflected credentials, even on HTTP 200.
    if key.encode("ascii") in raw or auth.encode("ascii") in raw or auth[6:].encode("ascii") in raw:
        raise DatabentoDataError("invalid_response")
    return raw


def _reject_constant(_):
    raise ValueError("Non-finite JSON values are invalid")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON object keys are invalid")
        result[name] = value
    return result


def _json(raw: bytes):
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant,
                          object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise DatabentoDataError("invalid_response") from None


def _check_result(status, message, authenticated=False):
    return {"platform": "databento", "checked_at": datetime.now(UTC).isoformat(),
            "status": status, "message": message,
            "capabilities": {"metadata_authentication": authenticated,
                             "public_market_data": False, "account_read": False,
                             "qqq_data": False, "historical_data": False,
                             "live_data": False, "order_execution": False}}


def check_databento_connection(environ: Mapping[str, str] | None = None, *, transport=None) -> dict:
    """Authenticate using free metadata only; return no key, account or datasets."""
    try:
        key = _key(environ)
    except RuntimeError:
        return _check_result("setup_required", "Set DATABENTO_API_KEY in Dwight's private local environment.")
    except (ValueError, TypeError, AttributeError):
        return _check_result("invalid_configuration", "The private Databento API key format is invalid.")
    try:
        datasets = _json(_read(DATASET_LIST_URL, key, transport=transport, timeout=10,
                               max_bytes=MAX_METADATA_BYTES))
        if (not isinstance(datasets, list) or not 1 <= len(datasets) <= 1000
                or any(not isinstance(item, str) or not _DATASET_PATTERN.fullmatch(item) for item in datasets)
                or len(set(datasets)) != len(datasets)):
            raise DatabentoDataError("invalid_response")
    except DatabentoDataError as exc:
        return _check_result(exc.status, str(exc))
    return _check_result("connected", "Databento metadata authentication succeeded. QQQ history, dataset entitlements and live data have not been verified.", True)


def _range(start: datetime, end: datetime, dataset: str):
    if not isinstance(dataset, str) or not _DATASET_PATTERN.fullmatch(dataset):
        raise ValueError("Choose an explicit Databento dataset ID, such as XNAS.ITCH")
    if (not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.tzinfo is None or end.tzinfo is None):
        raise ValueError("Provide ordered timezone-aware datetime bounds")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end or end - start > timedelta(days=MAX_RANGE_DAYS):
        raise ValueError("Historical range must be positive and at most 3660 days")
    if any(stamp.second or stamp.microsecond for stamp in (start, end)):
        raise ValueError("Historical range bounds must align to whole minutes")
    return start, end, {"dataset": dataset, "symbols": "QQQ", "schema": SCHEMA,
                        "start": start.isoformat(), "end": end.isoformat(), "stype_in": "raw_symbol"}


def _decimal(value, *, configuration=False) -> Decimal:
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError()
        if isinstance(value, str) and len(value) > 64:
            raise ValueError()
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or not math.isfinite(float(amount)):
            raise ValueError()
        return amount
    except (ValueError, InvalidOperation, OverflowError):
        if configuration:
            raise ValueError("max_cost_usd must be an explicit finite non-negative amount") from None
        raise DatabentoDataError("invalid_response") from None


def _plan(start, end, key, *, dataset, transport, max_records, timeout):
    start, end, query = _range(start, end, dataset)
    cost = _decimal(_json(_read(COST_URL + "?" + urlencode(query), key,
                               transport=transport, timeout=timeout, max_bytes=MAX_METADATA_BYTES)))
    count = _json(_read(COUNT_URL + "?" + urlencode(query), key,
                       transport=transport, timeout=timeout, max_bytes=MAX_METADATA_BYTES))
    if type(count) is not int or count < 0:
        raise DatabentoDataError("invalid_response")
    if count > max_records:
        raise ValueError("Databento estimated record count exceeds max_records; choose a shorter range")
    plan = {"source": "databento", "dataset": dataset,
            "feed": "databento_" + dataset + "_ohlcv_1m", "symbol": "QQQ", "schema": SCHEMA,
            "start": start.isoformat(), "end": end.isoformat(), "end_exclusive": True,
            "estimated_cost_usd": str(cost), "estimated_records": count,
            "estimated_at": datetime.now(UTC).isoformat(), "retrieval_started": False,
            "cost_control": "estimated_cost_gate_not_provider_billing_cap",
            "estimate_limitation": "Databento bills actual bytes; a cost estimate does not cap the final charge.",
            "ten_minute_aligned": int((end - start).total_seconds()) % 600 == 0}
    return start, end, query, cost, count, plan


def estimate_databento_history(start: datetime, end: datetime, environ: Mapping[str, str] | None = None,
                               *, dataset: str, transport=None, max_records=DEFAULT_MAX_RECORDS,
                               timeout=DEFAULT_TIMEOUT) -> dict:
    """Return a free metadata cost/count plan without retrieving billable bars."""
    _bounds(DEFAULT_MAX_BYTES, max_records, timeout)
    _range(start, end, dataset)
    key = _key(environ)
    return _plan(start, end, key, dataset=dataset, transport=transport,
                 max_records=max_records, timeout=timeout)[-1]


def _price(value):
    result = _decimal(value)
    if result <= 0:
        raise DatabentoDataError("invalid_response")
    return float(result)


def _records(raw, start, end, max_records):
    rows, publishers, instruments = [], set(), set()
    seen = set()
    for line in raw.splitlines():
        if not line.strip():
            raise DatabentoDataError("invalid_response")
        if len(line) > 8192:
            raise DatabentoDataError("invalid_response")
        row = _json(line)
        if not isinstance(row, dict) or row.get("symbol") != "QQQ" or not isinstance(row.get("hd"), dict):
            raise DatabentoDataError("invalid_response")
        if set(row) != {"hd", "open", "high", "low", "close", "volume", "symbol"}:
            raise DatabentoDataError("invalid_response")
        header = row["hd"]
        if set(header) != {"ts_event", "rtype", "publisher_id", "instrument_id"}:
            raise DatabentoDataError("invalid_response")
        if (type(header.get("rtype")) is not int or header["rtype"] != 33
                or type(header.get("publisher_id")) is not int or not 0 < header["publisher_id"] <= 65535
                or type(header.get("instrument_id")) is not int or not 0 < header["instrument_id"] <= 2**32 - 1):
            raise DatabentoDataError("invalid_response")
        timestamp = header.get("ts_event")
        if not isinstance(timestamp, str) or not _MINUTE_PATTERN.fullmatch(timestamp):
            raise DatabentoDataError("invalid_response")
        try:
            stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            raise DatabentoDataError("invalid_response") from None
        if not start <= stamp < end or stamp in seen:
            # Venue bars cannot be silently summed or collapsed into a new feed.
            raise DatabentoDataError("invalid_response")
        seen.add(stamp)
        try:
            prices = [_price(row[name]) for name in ("open", "high", "low", "close")]
            volume = _decimal(row["volume"])
        except KeyError:
            raise DatabentoDataError("invalid_response") from None
        if volume != volume.to_integral_value() or volume > 2**53 - 1:
            raise DatabentoDataError("invalid_response")
        opened, high, low, close = prices
        if not low <= min(opened, close) <= max(opened, close) <= high:
            raise DatabentoDataError("invalid_response")
        rows.append({"t": stamp.isoformat(), "o": opened, "h": high, "l": low,
                     "c": close, "v": int(volume)})
        publishers.add(header["publisher_id"])
        instruments.add(header["instrument_id"])
        if len(rows) > max_records:
            raise DatabentoDataError("invalid_response")
    if not rows:
        raise DatabentoDataError("invalid_response")
    rows.sort(key=lambda item: item["t"])
    return rows, sorted(publishers), sorted(instruments)


def fetch_databento_history(start: datetime, end: datetime, environ: Mapping[str, str] | None = None,
                            *, dataset: str, max_cost_usd, transport=None,
                            max_bytes=DEFAULT_MAX_BYTES, max_records=DEFAULT_MAX_RECORDS,
                            timeout=DEFAULT_TIMEOUT) -> dict:
    """Return source minute bars and raw NDJSON after an explicit cost gate.

    Bounds are start-inclusive/end-exclusive. This does not fill missing bars or
    verify full regular-session coverage; pass records to normalize_minutes.
    The exact dataset stays in provenance. It is never relabeled Alpaca SIP.
    Any error aborts without retry; the provider may have billed received bytes.
    """
    _bounds(max_bytes, max_records, timeout)
    _range(start, end, dataset)
    budget = _decimal(max_cost_usd, configuration=True)
    key = _key(environ)
    start, end, query, cost, count, plan = _plan(start, end, key, dataset=dataset,
                                              transport=transport, max_records=max_records, timeout=timeout)
    if cost > budget:
        raise ValueError("Databento estimate exceeds max_cost_usd; no time series was requested")
    if count == 0:
        raise ValueError("Databento metadata reports no records; no time series was requested")
    form = {**query, "stype_out": "instrument_id", "encoding": "json", "compression": "none",
            "pretty_px": "true", "pretty_ts": "true", "map_symbols": "true", "limit": max_records + 1}
    raw = _read(RANGE_URL, key, transport=transport, timeout=timeout, max_bytes=max_bytes, form=form)
    rows, publishers, instruments = _records(raw, start, end, max_records)
    if len(rows) != count:
        raise DatabentoDataError("invalid_response")
    plan.update(retrieval_started=True, max_cost_usd=str(budget))
    return {"records": rows, "raw_pages": [raw],
            "provenance": {"source": "databento", "dataset": dataset,
                           "feed": plan["feed"], "schema": SCHEMA, "adjustment": "raw",
                           "endpoint": RANGE_URL, "start": start.isoformat(), "end": end.isoformat(),
                           "end_exclusive": True, "timestamp_convention": "interval_start",
                           "availability": "interval_end", "encoding": "json_lines",
                           "publisher_ids": publishers, "instrument_ids": instruments,
                           "cost_plan": plan,
                           "limitations": ["Databento dataset and venue coverage must be reviewed separately from Alpaca feeds",
                                           "Absent no-trade minutes remain absent; complete-session validation rejects gaps",
                                           "No quote or spread data and no point-in-time corporate-action adjustments",
                                           "Bar-derived VWAP is an approximation", "Estimated cost is not a provider-enforced billing cap"]}}
