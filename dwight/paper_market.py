"""Bounded Alpaca paper transport and same-feed QQQ quotes.

Nothing executes at import. The paper client retains the execution library's
fixed paper origin; the quote reader has only one market-data GET operation.
Requests have a five-second timeout and never retry. A mutation timeout remains
ambiguous and must be reconciled by the caller, never blindly submitted again.
Transport injection follows urllib's context-manager response interface.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
import re
import ssl
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, build_opener

from .paper import AlpacaPaperClient, BrokerHTTPError, PAPER_URL, PaperError, PreparationRequired, Quote

TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 1_048_576
QUOTE_URL = "https://data.alpaca.markets/v2/stocks/quotes/latest"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport():
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        pass
    else:
        context.load_verify_locations(cafile=certifi.where())
    return build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context)).open


def _credentials(key, secret):
    for value in (key, secret):
        if (not isinstance(value, str) or not 1 <= len(value) <= 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise PreparationRequired("Prepare valid Alpaca paper credentials privately")
    return key, secret


def _environment(environ):
    env = os.environ if environ is None else environ
    for name in ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL"):
        if env.get(name, PAPER_URL) != PAPER_URL:
            raise PreparationRequired("Alpaca endpoints must be the fixed paper origin")
    return _credentials(env.get("APCA_API_KEY_ID") or env.get("ALPACA_API_KEY"),
                        env.get("APCA_API_SECRET_KEY") or env.get("ALPACA_SECRET_KEY"))


def _constant(_):
    raise ValueError("Nonfinite JSON")


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Nonfinite JSON")
    return parsed


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _status(status, method):
    if type(status) is not int:
        raise PaperError("Alpaca returned an invalid response status")
    if 300 <= status < 400:
        raise PaperError("Alpaca redirects are forbidden")
    if status != 200 and not (method == "DELETE" and status == 204):
        raise BrokerHTTPError(status)


def _request_json(url, method, key, secret, transport, payload=None):
    request = Request(url, method=method, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "Dwight-Paper-Worker/0.1"},
        data=None if payload is None else json.dumps(payload, allow_nan=False).encode())
    try:
        with transport(request, timeout=TIMEOUT_SECONDS) as response:
            _status(response.getcode(), method)
            if response.geturl() != url:
                raise PaperError("Alpaca redirects are forbidden")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise PaperError("Alpaca returned an invalid or oversized response")
        if not raw and method == "DELETE":
            return None
        return json.loads(raw, parse_constant=_constant, parse_float=_float, object_pairs_hook=_unique)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        _status(status, method)
        raise PaperError("Alpaca returned an invalid response") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise PaperError("Alpaca request failed; any mutation outcome may be unknown") from None
    except (ValueError, TypeError, RecursionError):
        raise PaperError("Alpaca returned invalid JSON") from None


class HardenedAlpacaPaperClient(AlpacaPaperClient):
    """The existing paper interface through one bounded, verified transport."""

    def __init__(self, key, secret, *, base_url=PAPER_URL, timeout=TIMEOUT_SECONDS, transport=None):
        if base_url != PAPER_URL:
            raise PreparationRequired("Only the fixed Alpaca paper origin is allowed")
        if type(timeout) not in (int, float) or timeout != TIMEOUT_SECONDS:
            raise ValueError("The paper worker timeout is fixed at five seconds")
        self._key, self._secret = _credentials(key, secret)
        self.timeout = TIMEOUT_SECONDS
        self._transport = _transport() if transport is None else transport

    @classmethod
    def from_env(cls, *, environ=None, transport=None):
        return cls(*_environment(environ), transport=transport)

    def _request(self, method, path, *, params=None, payload=None):
        get_paths = {"/v2/account", "/v2/positions", "/v2/orders", "/v2/clock",
                     "/v2/calendar", "/v2/orders:by_client_order_id", "/v2/assets/QQQ",
                     "/v2/account/activities/FILL"}
        order_path = isinstance(path, str) and re.fullmatch(r"/v2/orders/[a-zA-Z0-9-]+", path)
        if not ((method == "GET" and (path in get_paths or order_path))
                or (method == "POST" and path == "/v2/orders")
                or (method == "DELETE" and order_path)):
            raise ValueError("Unsupported paper operation")
        if method == "POST" and (not isinstance(payload, dict) or payload.get("symbol") != "QQQ"):
            raise PaperError("Only QQQ paper orders are supported")
        if method != "POST" and payload is not None:
            raise ValueError("Only an order submission may carry a payload")
        url = PAPER_URL + path + ("?" + urlencode(params) if params else "")
        return _request_json(url, method, self._key, self._secret, self._transport, payload)

    def get_asset(self):
        """Read only the exact QQQ asset definition; no arbitrary instrument URL."""
        return self._request("GET", "/v2/assets/QQQ")

    def get_fill_activities(self, after, until, page_token=None, page_size=100):
        """Read one bounded page of genuine paper executions, oldest first.

        Alpaca filters activity creation time, not execution time. Callers must
        retain a fixed window across pages and refresh that full window later
        to discover late booked executions. No mutation or retry occurs here.
        """
        def timestamp(value):
            try:
                stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError()
                return stamp.astimezone(timezone.utc)
            except (ValueError, TypeError, AttributeError, OverflowError):
                raise ValueError("Fill queries require explicit timezone-aware timestamps") from None

        start, end = timestamp(after), timestamp(until)
        if start >= end:
            raise ValueError("Fill query window must have a positive duration")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Fill page size must be an integer from one to 100")
        if page_token is not None and (not isinstance(page_token, str) or not 1 <= len(page_token) <= 256
                                      or any(ord(char) < 33 or ord(char) > 126 for char in page_token)):
            raise ValueError("Fill pagination token is invalid")
        params = {"after": start.isoformat(), "until": end.isoformat(), "direction": "asc", "page_size": page_size}
        if page_token is not None:
            params["page_token"] = page_token
        result = self._request("GET", "/v2/account/activities/FILL", params=params)
        if not isinstance(result, list) or len(result) > page_size:
            raise PaperError("Alpaca returned an invalid fill activity page")
        return result


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PaperError("QQQ quote contains invalid numbers")
    try:
        number = Decimal(str(value))
        parsed = float(number)
        if not number.is_finite() or not math.isfinite(parsed) or parsed <= 0:
            raise ValueError()
        return parsed
    except (ValueError, InvalidOperation, OverflowError):
        raise PaperError("QQQ quote contains invalid numbers") from None


class PaperMarket:
    """One fixed, explicitly selected SIP or IEX latest QQQ quote reader."""

    def __init__(self, key, secret, *, transport=None):
        self._key, self._secret = _credentials(key, secret)
        self._transport = _transport() if transport is None else transport
        self.last_received_at = None

    @classmethod
    def from_env(cls, *, environ=None, transport=None):
        return cls(*_environment(environ), transport=transport)

    def latest_quote(self, feed):
        """Return the provider's event timestamp, never substituting receive time.

        The executor evaluates quote age after account reconciliation. A closed
        market's old quote may be read successfully but does not permit entry.
        Positive displayed sizes verify that both sides were present; this does
        not promise execution for any particular order quantity.
        """
        if not isinstance(feed, str) or feed not in {"sip", "iex"}:
            raise ValueError("Choose the explicit SIP or IEX quote feed")
        url = QUOTE_URL + "?" + urlencode({"symbols": "QQQ", "feed": feed, "currency": "USD"})
        self.last_received_at = None
        payload = _request_json(url, "GET", self._key, self._secret, self._transport)
        received = datetime.now(timezone.utc)
        if not isinstance(payload, dict) or not isinstance(payload.get("quotes"), dict) or set(payload["quotes"]) != {"QQQ"}:
            raise PaperError("Alpaca quote response must contain only QQQ")
        row = payload["quotes"]["QQQ"]
        if not isinstance(row, dict):
            raise PaperError("Alpaca QQQ quote is invalid")
        bid, ask = _positive(row.get("bp")), _positive(row.get("ap"))
        _positive(row.get("bs"))
        _positive(row.get("as"))
        if bid > ask:
            raise PaperError("Alpaca QQQ quote is crossed")
        value = row.get("t")
        try:
            if not isinstance(value, str):
                raise ValueError()
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError()
            stamp = stamp.astimezone(timezone.utc)
            if stamp > received:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise PaperError("Alpaca QQQ quote timestamp is invalid or future dated") from None
        self.last_received_at = received
        return Quote("QQQ", bid, ask, stamp, feed)
