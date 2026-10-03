"""Bounded public QQQx market collection for research, with no trading API.

Kraken's public xStocks REST endpoints report venue quantities, not verified
onchain token balances. ``lot_multiplier`` is trading metadata, not the issuer's
corporate-action multiplier. No chain, contract, account eligibility or share
ownership is inferred here. Individual endpoint reads are not an atomic quote.

``transport(request, *, timeout)`` follows urllib's opener interface. ``clock``
returns a timezone-aware datetime and exists for deterministic offline tests.
Nothing runs, reads credentials, writes files or retries at import time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from http.client import HTTPException
import json
import math
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

ORIGIN = "https://api.kraken.com/0/public/"
PAIR = "QQQxUSD"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 524_288
MAX_COMPLETED_BARS = 720
MAX_BOOK_LEVELS = 50
_MESSAGES = {
    "invalid_configuration": "Choose a book depth from 1 to 50 and a valid UTC collection clock.",
    "invalid_response": "Kraken returned invalid or oversized market data. Research collection was not completed.",
    "unsupported_instrument": "One online Kraken QQQx/USD tokenized market could not be verified.",
    "redirect_rejected": "A redirected market-data request was rejected.",
    "permission_denied": "Public market-data access was denied. No account or trading access was checked.",
    "rate_limited": "Kraken rate limited this read. Retry later.",
    "unavailable": "Kraken public market data was unavailable. Retry later.",
}


class TokenizedDataError(RuntimeError):
    """Sanitized collection failure; provider bodies and exceptions stay private."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES[code])


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


def _now(clock):
    stamp = clock() if clock else datetime.now(timezone.utc)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise TokenizedDataError("invalid_configuration")
    return stamp.astimezone(timezone.utc)


def _iso(stamp):
    return stamp.isoformat()


def _status(code):
    if type(code) is not int:
        raise TokenizedDataError("invalid_response")
    if 300 <= code < 400:
        raise TokenizedDataError("redirect_rejected")
    if code in (401, 403, 451):
        raise TokenizedDataError("permission_denied")
    if code in (418, 429):
        raise TokenizedDataError("rate_limited")
    if code >= 500:
        raise TokenizedDataError("unavailable")
    if code != 200:
        raise TokenizedDataError("invalid_response")


def _reject_constant(_):
    raise ValueError("Invalid JSON constant")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _read(endpoint, query, transport, clock, provenance):
    # Every URL and query below is internal and fixed. No caller URL, secret,
    # response-provided pair name or proxy is used for an outgoing request.
    url = ORIGIN + endpoint + ("?" + urlencode(query) if query else "")
    requested = _now(clock)
    if provenance and requested < datetime.fromisoformat(provenance[-1]["received_at"]):
        raise TokenizedDataError("invalid_configuration")
    request = Request(url, method="GET", headers={"Accept": "application/json",
                                                "User-Agent": "dwight-tokenized-research/0.1"})
    try:
        with transport(request, timeout=TIMEOUT_SECONDS) as response:
            _status(response.getcode())
            if response.geturl() != url:
                raise TokenizedDataError("redirect_rejected")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        received = _now(clock)
        if received < requested:
            raise TokenizedDataError("invalid_configuration")
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise TokenizedDataError("invalid_response")
        payload = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        _status(code)
        raise TokenizedDataError("invalid_response") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise TokenizedDataError("unavailable") from None
    except (ValueError, TypeError, RecursionError):
        raise TokenizedDataError("invalid_response") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), list):
        raise TokenizedDataError("invalid_response")
    if payload["error"]:
        # Never surface the raw provider message, even on an unknown API error.
        if any(isinstance(item, str) and "rate limit" in item.lower() for item in payload["error"]):
            raise TokenizedDataError("rate_limited")
        raise TokenizedDataError("unavailable")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise TokenizedDataError("invalid_response")
    provenance.append({"endpoint": endpoint, "url": url, "requested_at": _iso(requested),
                       "received_at": _iso(received), "response_sha256": sha256(raw).hexdigest()})
    return result, received


def _number(value, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TokenizedDataError("invalid_response")
    try:
        number = Decimal(str(value))
        parsed = float(number)
        if not number.is_finite() or not math.isfinite(parsed) or parsed < 0 or (positive and parsed <= 0):
            raise ValueError()
        return parsed
    except (InvalidOperation, ValueError, OverflowError):
        raise TokenizedDataError("invalid_response") from None


def _integer(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise TokenizedDataError("invalid_response")
    return value


def _epoch(value, received, *, integer=True):
    seconds = _integer(value) if integer else _number(value)
    try:
        stamp = datetime.fromtimestamp(seconds, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise TokenizedDataError("invalid_response") from None
    if stamp > received + timedelta(seconds=60):
        raise TokenizedDataError("invalid_response")
    return stamp


def _instrument(result):
    # A single explicit query must yield exactly this instrument. An ordinary
    # equity named QQQ, alternate quote, dark pool or ambiguous pair is rejected.
    if set(result) != {PAIR} or not isinstance(result[PAIR], dict):
        raise TokenizedDataError("unsupported_instrument")
    row = result[PAIR]
    expected = {"aclass_base": "tokenized_asset", "base": "QQQx", "aclass_quote": "currency",
                "quote": "ZUSD", "altname": PAIR, "wsname": "QQQx/USD", "status": "online",
                "lot": "unit", "execution_venue": "international"}
    if any(row.get(key) != value for key, value in expected.items()):
        raise TokenizedDataError("unsupported_instrument")
    lot_multiplier = _number(row.get("lot_multiplier"))
    metadata = {**expected, "lot_multiplier": lot_multiplier}
    return {"symbol": "QQQx", "base": "QQQx", "quote": "USD", "venue": "kraken",
            "asset_class": "tokenized_equity", "pair": PAIR, "issuer": "unknown",
            "chain": None, "contract": None, "provider_pair_metadata": metadata,
            "quantity_unit": "Kraken-reported QQQx base quantity",
            "corporate_action_multiplier": None,
            "quantity_caveat": "Venue quantities are not verified onchain token amounts. "
                               "The lot multiplier is not the corporate-action multiplier; "
                               "no conversion to underlying shares or wallet tokens is established."}


def _single_pair(result):
    if set(result) != {PAIR} or not isinstance(result[PAIR], dict):
        raise TokenizedDataError("invalid_response")
    return result[PAIR]


def _bars(result, received):
    if set(result) != {PAIR, "last"}:
        raise TokenizedDataError("invalid_response")
    _epoch(result["last"], received)
    rows = result[PAIR]
    # Live REST can return 720 committed bars plus the obligatory final current
    # row. The returned research dataset is still bounded to 720 closed bars.
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_COMPLETED_BARS + 1:
        raise TokenizedDataError("invalid_response")
    bars, previous = [], None
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != 8:
            raise TokenizedDataError("invalid_response")
        stamp = _epoch(row[0], received)
        if row[0] % 300 or (previous is not None and stamp <= previous):
            raise TokenizedDataError("invalid_response")
        previous = stamp
        o, h, l, c = (_number(item) for item in row[1:5])
        vwap, volume = (_number(item, positive=False) for item in row[5:7])
        _integer(row[7], zero=True)
        if l > min(o, c) or h < max(o, c) or l > h or (vwap and not l <= vwap <= h):
            raise TokenizedDataError("invalid_response")
        if index == len(rows) - 1:
            # Kraken guarantees this row is uncommitted. It is omitted even if
            # a cached response arrives after the row's nominal close time.
            continue
        if stamp + timedelta(minutes=5) > received:
            raise TokenizedDataError("invalid_response")
        bars.append({"timestamp": _iso(stamp), "open": o, "high": h, "low": l,
                     "close": c, "volume": volume})
    return bars, {"raw_rows": len(rows), "completed_rows": len(bars), "unfinished_rows_omitted": 1,
                  "last_cursor": result["last"], "history_limit_completed_bars": MAX_COMPLETED_BARS,
                  "history": "Recent rolling window only; not a complete historical dataset"}


def _book(result, received, depth):
    row = _single_pair(result)
    sides, timestamps = {}, {}
    for side in ("bids", "asks"):
        levels = row.get(side)
        if not isinstance(levels, list) or not 1 <= len(levels) <= depth:
            raise TokenizedDataError("invalid_response")
        parsed, observed = [], []
        for level in levels:
            if not isinstance(level, list) or len(level) != 3:
                raise TokenizedDataError("invalid_response")
            price, qty = (_number(item) for item in level[:2])
            if parsed and ((side == "bids" and price >= parsed[-1][0])
                           or (side == "asks" and price <= parsed[-1][0])):
                raise TokenizedDataError("invalid_response")
            parsed.append([price, qty])
            observed.append(_iso(_epoch(level[2], received, integer=False)))
        sides[side], timestamps[side] = parsed, observed
    if sides["bids"][0][0] >= sides["asks"][0][0]:
        raise TokenizedDataError("invalid_response")
    return {"observed_at": _iso(received), **sides, "provider_level_timestamps": timestamps,
            "quantity_unit": "Kraken-reported QQQx base quantity", "depth_requested": depth}


def _ticker(result, received):
    row = _single_pair(result)
    for name, count in (("a", 3), ("b", 3), ("c", 2), ("v", 2)):
        if not isinstance(row.get(name), list) or len(row[name]) != count:
            raise TokenizedDataError("invalid_response")
    ask, bid, last = (_number(row[key][0]) for key in ("a", "b", "c"))
    if bid >= ask:
        raise TokenizedDataError("invalid_response")
    for key in ("a", "b"):
        for value in row[key][1:]:
            _number(value, positive=False)
    return {"observed_at": _iso(received), "bid": bid, "ask": ask, "last": last,
            "last_quantity": _number(row["c"][1], positive=False),
            "volume_today": _number(row["v"][0], positive=False),
            "volume_24h": _number(row["v"][1], positive=False),
            "provider_event_timestamp": None,
            "time_caveat": "Receive time is recorded; the ticker supplies no quote or last-trade event time"}


def collect_qqqx_snapshot(*, transport=None, clock=None, depth=20) -> dict:
    """Collect one private-research snapshot through five public GET requests.

    The caller chooses whether and where to save the returned JSON-safe object.
    Public reachability is not proof that any user's jurisdiction, account or
    strategy is eligible. The snapshot cannot submit orders or start a campaign.
    """
    if type(depth) is not int or not 1 <= depth <= MAX_BOOK_LEVELS:
        raise TokenizedDataError("invalid_configuration")
    started = _now(clock)
    opener = transport or _default_transport()
    provenance = []
    time_result, time_received = _read("Time", {}, opener, clock, provenance)
    server_time = _epoch(time_result.get("unixtime"), time_received)
    pairs, _ = _read("AssetPairs", {"pair": PAIR, "aclass_base": "tokenized_asset"}, opener, clock, provenance)
    instrument = _instrument(pairs)
    market_query = {"pair": PAIR, "asset_class": "tokenized_asset"}
    history, bars_received = _read("OHLC", {**market_query, "interval": 5}, opener, clock, provenance)
    bars, history_metadata = _bars(history, bars_received)
    book, book_received = _read("Depth", {**market_query, "count": depth}, opener, clock, provenance)
    orderbook = _book(book, book_received, depth)
    ticker_result, ticker_received = _read("Ticker", market_query, opener, clock, provenance)
    ticker = _ticker(ticker_result, ticker_received)
    finished = _now(clock)
    if finished < ticker_received or started > time_received:
        raise TokenizedDataError("invalid_configuration")
    return {"schema_version": 1, "data_kind": "observed_public_market", "execution_enabled": False,
            "interval_minutes": 5, "timestamp_label": "start", "instrument": instrument,
            "bars": bars, "bars_observed_at": _iso(bars_received), "history_metadata": history_metadata,
            "orderbook": orderbook, "ticker": ticker,
            "server_time": {"timestamp": _iso(server_time), "observed_at": _iso(time_received),
                            "receive_clock_difference_seconds": (time_received - server_time).total_seconds()},
            "collection_started_at": _iso(started), "collection_finished_at": _iso(finished),
            "collection": {"started_at": _iso(started), "finished_at": _iso(finished)},
            "provenance": {"provider": "kraken", "endpoints": provenance,
                           "atomic_snapshot": False, "authentication_used": False,
                           "collection_note": "Bars, book, ticker and server time were read separately. "
                                              "Receive times and response checksums are per endpoint."},
            "eligibility_verified": False,
            "sources": ["https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs",
                        "https://docs.kraken.com/api-reference/market-data/get-ohlc-data",
                        "https://docs.kraken.com/api-reference/market-data/get-order-book",
                        "https://support.kraken.com/articles/xstocks-faq"]}
