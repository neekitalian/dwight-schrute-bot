"""Bounded, read-only connectivity probes with sanitized JSON-safe results.

Nothing runs at import time. ``check_connection`` never changes configuration,
saves provider payloads, or submits orders. Public probes never read environment
variables. Alpaca reads private credentials only when explicitly selected.

An optional ``transport(request, *, timeout)`` uses urllib's opener interface:
return a context manager whose response has ``read(size)``, ``getcode()``, and
``geturl()``. Tests can inject it without credentials or network access. The
default transport disables redirects and environment proxy overrides.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
import json
import os
import ssl
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .data import ALPACA_BARS_URL, _credentials

TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 262_144
PAPER_ACCOUNT_URL = "https://paper-api.alpaca.markets/v2/account"
PUBLIC_URLS = {
    "coinbase": "https://api.coinbase.com/v2/prices/BTC-USD/spot",
    "binance": "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT",
    "kraken": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
    "polymarket": "https://gamma-api.polymarket.com/markets?limit=1&active=true&closed=false",
}
SUPPORTED_PLATFORMS = tuple(PUBLIC_URLS) + ("alpaca", "tradingview", "ibkr", "schwab")

_ERROR_MESSAGES = {
    "authentication_failed": "Authentication was rejected. Check the private credentials for the selected environment.",
    "permission_denied": "Access was denied. Check provider availability and account or data permissions.",
    "rate_limited": "The provider rate limited this check. Retry later.",
    "unavailable": "The provider could not be reached or is temporarily unavailable. Retry later.",
    "invalid_response": "The provider returned an unexpected or oversized response. Connection was not verified.",
    "redirect_rejected": "The provider returned a redirect. It was rejected; connection was not verified.",
}


class _CheckFailure(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(_ERROR_MESSAGES[status])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_transport():
    context = ssl.create_default_context()
    # Python.org macOS installs can lack a system CA bundle. Add the optional
    # data extra's maintained CA roots while retaining certificate verification.
    try:
        import certifi
    except ImportError:
        pass
    else:
        context.load_verify_locations(cafile=certifi.where())
    return build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context)).open


def _result(platform, status, message, **capabilities):
    return {
        "platform": platform,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "capabilities": {"public_market_data": False, "account_read": False,
                         "qqq_data": False, "order_execution": False, **capabilities},
        "message": message,
    }


def _http_status(code):
    if type(code) is not int:
        raise _CheckFailure("invalid_response")
    if 300 <= code < 400:
        raise _CheckFailure("redirect_rejected")
    if code == 401:
        raise _CheckFailure("authentication_failed")
    if code in (403, 451):
        raise _CheckFailure("permission_denied")
    if code in (418, 429):
        raise _CheckFailure("rate_limited")
    if code >= 500:
        raise _CheckFailure("unavailable")
    if code != 200:
        raise _CheckFailure("invalid_response")


def _allowed_url(url):
    if url in PUBLIC_URLS.values() or url == PAPER_ACCOUNT_URL:
        return True
    # Only this internally constructed QQQ bars query is permitted.
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "data.alpaca.markets"
            and parsed.path == "/v2/stocks/bars" and not parsed.fragment)


def _reject_nonfinite(_):
    raise ValueError("Non-finite JSON numbers are invalid")


def _read_json(url, headers, transport):
    if not _allowed_url(url):
        raise _CheckFailure("invalid_response")
    request = Request(url, headers={"Accept": "application/json",
                                   "User-Agent": "dwight-connection-check/0.1", **headers}, method="GET")
    try:
        opener = transport or _default_transport()
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            _http_status(response.getcode())
            # A custom transport must not silently follow redirects either.
            if response.geturl() != url:
                raise _CheckFailure("redirect_rejected")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise _CheckFailure("invalid_response")
        return json.loads(raw, parse_constant=_reject_nonfinite)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        _http_status(code)
        raise _CheckFailure("invalid_response") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise _CheckFailure("unavailable") from None
    except (ValueError, TypeError, RecursionError):
        raise _CheckFailure("invalid_response") from None


def _positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        number = Decimal(str(value))
        return number.is_finite() and number > 0
    except InvalidOperation:
        return False


def _public_probe(platform, transport):
    payload = _read_json(PUBLIC_URLS[platform], {}, transport)
    valid = False
    if platform == "coinbase" and isinstance(payload, dict):
        data = payload.get("data")
        valid = (isinstance(data, dict) and data.get("currency") == "USD"
                 and data.get("base", "BTC") == "BTC" and _positive_number(data.get("amount")))
    elif platform == "binance" and isinstance(payload, dict):
        valid = payload.get("symbol") == "BTCUSDT" and _positive_number(payload.get("price"))
    elif platform == "kraken" and isinstance(payload, dict):
        if isinstance(payload.get("error"), list) and payload["error"]:
            raise _CheckFailure("unavailable")
        result = payload.get("result")
        if (payload.get("error") == [] and isinstance(result, dict) and len(result) == 1
                and next(iter(result)) in ("XXBTZUSD", "XBTUSD", "BTCUSD")):
            ticker = next(iter(result.values()))
            price = ticker.get("c") if isinstance(ticker, dict) else None
            valid = isinstance(price, list) and bool(price) and _positive_number(price[0])
    elif platform == "polymarket":
        valid = (isinstance(payload, list) and len(payload) <= 1
                 and all(isinstance(market, dict) and isinstance(market.get("id"), str)
                         and bool(market["id"]) for market in payload))
    if not valid:
        raise _CheckFailure("invalid_response")
    if platform == "polymarket":
        message = "Public market discovery is reachable. No wallet or private account connection was checked."
    else:
        pair = "BTC/USDT" if platform == "binance" else "BTC/USD"
        message = (f"Public {pair} quote access is reachable. "
                   "No private account connection or crypto strategy was checked; order execution is unavailable.")
    return _result(platform, "public_data_available", message, public_market_data=True)


def _qqq_probe(headers, transport, feed):
    # Explicit recent end tests SIP entitlement, matching data.py's raw 1Min
    # QQQ-only feed contract. A small page is a probe, not a complete dataset.
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)
    start = end - timedelta(days=7)
    query = {"symbols": "QQQ", "timeframe": "1Min", "start": start.isoformat(),
             "end": end.isoformat(), "feed": feed, "adjustment": "raw", "asof": "-",
             "limit": 1, "sort": "desc"}
    payload = _read_json(ALPACA_BARS_URL + "?" + urlencode(query), headers, transport)
    if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict):
        raise _CheckFailure("invalid_response")
    if set(payload["bars"]) - {"QQQ"}:
        raise _CheckFailure("invalid_response")
    rows = payload["bars"].get("QQQ", [])
    if not isinstance(rows, list) or len(rows) > 1:
        raise _CheckFailure("invalid_response")
    if not rows:
        return False
    row = rows[0]
    if not isinstance(row, dict) or any(not _positive_number(row.get(key)) for key in ("o", "h", "l", "c")):
        raise _CheckFailure("invalid_response")
    try:
        volume = row["v"]
        stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
        valid = (not isinstance(volume, bool) and isinstance(volume, (int, float)) and volume >= 0
                 and Decimal(str(volume)).is_finite() and stamp.tzinfo is not None
                 and stamp.second == 0 and stamp.microsecond == 0 and start <= stamp <= end
                 and Decimal(str(row["l"])) <= min(Decimal(str(row["o"])), Decimal(str(row["c"])))
                 and Decimal(str(row["h"])) >= max(Decimal(str(row["o"])), Decimal(str(row["c"]))))
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        valid = False
    if not valid:
        raise _CheckFailure("invalid_response")
    return True


def _alpaca_probe(environ, transport, feed):
    if feed not in ("sip", "iex"):
        return _result("alpaca", "invalid_configuration", "Choose the explicit Alpaca data feed sip or iex.")
    env = os.environ if environ is None else environ
    if any(env.get(name, "https://paper-api.alpaca.markets") != "https://paper-api.alpaca.markets"
           for name in ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL")):
        return _result("alpaca", "invalid_configuration", "Alpaca checks require the fixed paper endpoint. Remove any live or custom endpoint override.")
    try:
        key, secret = _credentials(env)
        if any(not isinstance(value, str) or len(value) > 4096
               or any(ord(char) < 33 or ord(char) > 126 for char in value) for value in (key, secret)):
            raise ValueError()
    except RuntimeError:
        return _result("alpaca", "setup_required", "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY privately using Alpaca paper credentials, then run this check again.")
    except (ValueError, TypeError):
        return _result("alpaca", "invalid_configuration", "Private Alpaca credential format is invalid. Replace the local environment values.")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    account = _read_json(PAPER_ACCOUNT_URL, headers, transport)
    if not isinstance(account, dict) or not isinstance(account.get("status"), str) or not account["status"]:
        raise _CheckFailure("invalid_response")
    try:
        has_data = _qqq_probe(headers, transport, feed)
    except _CheckFailure as exc:
        return _result("alpaca", "partial", f"Paper account authentication succeeded; the QQQ {feed.upper()} data probe failed. "
                       + _ERROR_MESSAGES[exc.status], account_read=True)
    if not has_data:
        return _result("alpaca", "partial", f"Paper account authentication succeeded; the QQQ {feed.upper()} request returned no bars in the probe window. Data access remains unverified.", account_read=True)
    return _result("alpaca", "connected", f"Paper account authentication and a QQQ {feed.upper()} minute-bar read succeeded. This check does not establish trading readiness or enable orders.", account_read=True, qqq_data=True)


def check_connection(platform: str, *, environ: Mapping[str, str] | None = None, transport=None, feed: str = "sip") -> dict:
    """Check a named provider through fixed GET endpoints, without side effects.

    ``capabilities`` records only what this invocation verified. In particular,
    public quote access is not private account connectivity, and order execution
    is always false. Exceptions and provider bodies are never reflected in the
    result. Alpaca uses paper authentication plus one recent QQQ bars page with
    an explicit sip (default) or iex feed;
    a partial result distinguishes authentication from data entitlement.
    """
    name = platform.strip().lower() if isinstance(platform, str) else ""
    if name not in SUPPORTED_PLATFORMS:
        return _result("unknown", "unsupported", "Choose one of the supported platform identifiers.")
    if name == "tradingview":
        return _result(name, "manual_only", "Use TradingView with the manual paper journal, or optionally configure a separate private alert inbox. Dwight has no native TradingView account API connection.")
    if name in ("ibkr", "schwab"):
        return _result(name, "not_implemented", "An account connector for this platform is not implemented. Complete provider setup separately; Dwight has not verified a connection.")
    try:
        return _alpaca_probe(environ, transport, feed) if name == "alpaca" else _public_probe(name, transport)
    except _CheckFailure as exc:
        return _result(name, exc.status, _ERROR_MESSAGES[exc.status])
