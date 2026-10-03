from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from dwight.connection_checks import (MAX_RESPONSE_BYTES, PAPER_ACCOUNT_URL, PUBLIC_URLS,
                                      TIMEOUT_SECONDS, _NoRedirect, check_connection)


CREDS = {"APCA_API_KEY_ID": "FAKE-key-private", "APCA_API_SECRET_KEY": "FAKE-secret-private"}


def bar():
    stamp = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
    return {"t": stamp.isoformat(), "o": 500, "h": 501, "l": 499, "c": 500.5, "v": 100}


class Response(BytesIO):
    def __init__(self, payload, url, code=200):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.url, self.code, self.read_sizes = url, code, []

    def getcode(self):
        return self.code

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class Transport:
    def __init__(self, *payloads, code=200, response_url=None):
        self.payloads = list(payloads)
        self.calls, self.responses = [], []
        self.code, self.response_url = code, response_url

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        response = Response(payload, self.response_url or request.full_url, self.code)
        self.responses.append(response)
        return response


class ConnectionChecksTests(unittest.TestCase):
    def assert_sanitized(self, result):
        serialized = json.dumps(result, allow_nan=False)
        for private in (*CREDS.values(), "private-account-id", "account_number", "portfolio_value", "cash", "123456.78"):
            self.assertNotIn(private, serialized)
        self.assertEqual(set(result), {"platform", "checked_at", "status", "capabilities", "message"})
        self.assertIsNotNone(datetime.fromisoformat(result["checked_at"]).tzinfo)
        self.assertFalse(result["capabilities"]["order_execution"])
        self.assertTrue(all(type(value) is bool for value in result["capabilities"].values()))

    def test_alpaca_paper_account_and_qqq_explicit_feed_only(self):
        for feed in ("sip", "iex"):
            with self.subTest(feed=feed):
                transport = Transport({"status": "ACTIVE", "id": "private-account-id",
                                       "cash": "123456.78", "account_number": "hidden"},
                                      {"bars": {"QQQ": [bar()]}, "next_page_token": "never-follow-this"})
                result = check_connection("Alpaca", environ=CREDS, transport=transport, feed=feed)
                self.assertEqual(result["status"], "connected")
                self.assertTrue(result["capabilities"]["account_read"])
                self.assertTrue(result["capabilities"]["qqq_data"])
                self.assertEqual(len(transport.calls), 2)
                first, second = [request for request, timeout in transport.calls]
                self.assertEqual(first.full_url, PAPER_ACCOUNT_URL)
                self.assertEqual(urlsplit(second.full_url).netloc, "data.alpaca.markets")
                query = parse_qs(urlsplit(second.full_url).query)
                for key, value in {"symbols": "QQQ", "timeframe": "1Min", "feed": feed,
                                   "adjustment": "raw", "asof": "-", "limit": "1", "sort": "desc"}.items():
                    self.assertEqual(query[key], [value])
                self.assertNotIn("page_token", query)
                for request, timeout in transport.calls:
                    self.assertEqual(request.get_method(), "GET")
                    self.assertIsNone(request.data)
                    self.assertEqual(timeout, TIMEOUT_SECONDS)
                    self.assertEqual(dict((k.lower(), v) for k, v in request.header_items())["apca-api-secret-key"], CREDS["APCA_API_SECRET_KEY"])
                for response in transport.responses:
                    self.assertEqual(response.read_sizes, [MAX_RESPONSE_BYTES + 1])
                    self.assertTrue(response.closed)
                self.assert_sanitized(result)

    def test_missing_invalid_credentials_or_endpoint_never_send_requests(self):
        cases = [({}, "setup_required"), ({"APCA_API_KEY_ID": "only-key"}, "setup_required"),
                 ({**CREDS, "APCA_API_SECRET_KEY": "bad\r\nheader"}, "invalid_configuration"),
                 ({**CREDS, "APCA_API_SECRET_KEY": 7}, "invalid_configuration"),
                 ({**CREDS, "APCA_API_BASE_URL": "https://api.alpaca.markets"}, "invalid_configuration"),
                 ({**CREDS, "ALPACA_BASE_URL": "http://127.0.0.1/"}, "invalid_configuration")]
        for env, status in cases:
            with self.subTest(status=status, names=list(env)):
                transport = Mock()
                result = check_connection("alpaca", environ=env, transport=transport)
                self.assertEqual(result["status"], status)
                transport.assert_not_called()
                self.assert_sanitized(result)
        transport = Mock()
        self.assertEqual(check_connection("alpaca", environ=CREDS, transport=transport, feed="custom")["status"], "invalid_configuration")
        transport.assert_not_called()

    def test_alias_credentials_match_existing_data_reader(self):
        transport = Transport({"status": "ACTIVE"}, {"bars": {"QQQ": [bar()]}})
        result = check_connection("alpaca", environ={"APCA_API_KEY_ID": "", "APCA_API_SECRET_KEY": "",
                                                     "ALPACA_API_KEY": "alias-key", "ALPACA_SECRET_KEY": "alias-secret"},
                                  transport=transport)
        self.assertEqual(result["status"], "connected")
        headers = {key.lower(): value for key, value in transport.calls[0][0].header_items()}
        self.assertEqual(headers["apca-api-key-id"], "alias-key")
        self.assertEqual(headers["apca-api-secret-key"], "alias-secret")

    def test_data_entitlement_failure_and_empty_bars_are_partial(self):
        for payload in (HTTPError("ignored", 403, "private-account-id", {}, BytesIO(b"secret")),
                        {"bars": {}}, {"bars": {"QQQ": []}}):
            transport = Transport({"status": "ACTIVE"}, payload)
            result = check_connection("alpaca", environ=CREDS, transport=transport)
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["capabilities"]["account_read"])
            self.assertFalse(result["capabilities"]["qqq_data"])
            self.assert_sanitized(result)

    def test_malformed_or_wrong_symbol_data_is_not_success(self):
        bad_bar = bar()
        bad_bar["c"] = float("nan")
        for payload in ({}, {"bars": {"AAPL": [bar()]}}, {"bars": {"QQQ": [bar(), bar()]}},
                        {"bars": {"QQQ": [bad_bar]}}, {"bars": {"QQQ": [{"t": "invalid"}]}}):
            result = check_connection("alpaca", environ=CREDS,
                                      transport=Transport({"status": "ACTIVE"}, payload))
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["capabilities"]["qqq_data"])
        transport = Transport({"error": "private-account-id"})
        result = check_connection("alpaca", environ=CREDS, transport=transport)
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual(len(transport.calls), 1)

    def test_public_probes_do_not_read_env_or_send_credentials(self):
        class ForbiddenEnvironment(dict):
            def get(self, *args):
                raise AssertionError("Public check read environment")
        cases = {"coinbase": {"data": {"amount": "60000", "currency": "USD"}},
                 "binance": {"symbol": "BTCUSDT", "price": "60000"},
                 "kraken": {"error": [], "result": {"XXBTZUSD": {"c": ["60000", "1"]}}},
                 "polymarket": [{"id": "public-market"}]}
        with patch("dwight.connection_checks.os.environ", ForbiddenEnvironment()):
            for platform, payload in cases.items():
                with self.subTest(platform=platform):
                    transport = Transport(payload)
                    result = check_connection(platform, environ=ForbiddenEnvironment(), transport=transport)
                    self.assertEqual(result["status"], "public_data_available")
                    self.assertTrue(result["capabilities"]["public_market_data"])
                    self.assertFalse(result["capabilities"]["account_read"])
                    request, timeout = transport.calls[0]
                    self.assertEqual(request.full_url, PUBLIC_URLS[platform])
                    self.assertEqual(request.get_method(), "GET")
                    self.assertIsNone(request.data)
                    self.assertEqual(set(key.lower() for key, _ in request.header_items()), {"accept", "user-agent"})
                    self.assert_sanitized(result)

    def test_public_provider_error_and_price_schema_validation(self):
        cases = [("coinbase", {"data": {"amount": "nan", "currency": "USD"}}),
                 ("coinbase", {"data": {"amount": "100", "currency": "EUR"}}),
                 ("binance", {"symbol": "ETHUSDT", "price": "100"}),
                 ("binance", {"symbol": "BTCUSDT", "price": True}),
                 ("kraken", {"error": [], "result": {"ETHUSD": {"c": ["100"]}}}),
                 ("polymarket", [{"id": "one"}, {"id": "two"}]), ("polymarket", {"error": "private-account-id"})]
        for platform, payload in cases:
            with self.subTest(platform=platform):
                result = check_connection(platform, transport=Transport(payload))
                self.assertEqual(result["status"], "invalid_response")
                self.assert_sanitized(result)
        result = check_connection("kraken", transport=Transport({"error": ["private-account-id"]}))
        self.assertEqual(result["status"], "unavailable")
        self.assert_sanitized(result)

    def test_http_network_redirect_and_body_failures_are_sanitized(self):
        for code, status in ((301, "redirect_rejected"), (401, "authentication_failed"),
                             (403, "permission_denied"), (451, "permission_denied"),
                             (429, "rate_limited"), (500, "unavailable"), (400, "invalid_response")):
            with self.subTest(code=code):
                error = HTTPError("https://secret.invalid/", code, "private-account-id", {}, BytesIO(b"private-account-id"))
                result = check_connection("binance", transport=Transport(error))
                self.assertEqual(result["status"], status)
                self.assert_sanitized(result)
        for error in (URLError("private-account-id"), TimeoutError("private-account-id"), IncompleteRead(b"private-account-id")):
            result = check_connection("binance", transport=Transport(error))
            self.assertEqual(result["status"], "unavailable")
            self.assert_sanitized(result)
        for body in (b"private-account-id", b" " * (MAX_RESPONSE_BYTES + 1), b'{"x":NaN}'):
            result = check_connection("binance", transport=Transport(body))
            self.assertEqual(result["status"], "invalid_response")
            self.assert_sanitized(result)
        transport = Transport({"symbol": "BTCUSDT", "price": "1"}, response_url="http://127.0.0.1/")
        result = check_connection("binance", transport=transport)
        self.assertEqual(result["status"], "redirect_rejected")
        self.assertEqual(transport.responses[0].read_sizes, [])

    def test_default_opener_disables_redirects_and_proxies(self):
        with patch("dwight.connection_checks.build_opener") as build:
            build.return_value.open = Transport({"symbol": "BTCUSDT", "price": "100"})
            self.assertEqual(check_connection("binance")["status"], "public_data_available")
            handlers = build.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], _NoRedirect)
            self.assertIsNone(handlers[1].redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_manual_unimplemented_and_unknown_never_call_transport(self):
        for platform, status in (("tradingview", "manual_only"), ("ibkr", "not_implemented"),
                                 ("schwab", "not_implemented"),
                                 ("http://127.0.0.1/private-account-id", "unsupported")):
            transport = Mock()
            result = check_connection(platform, environ=CREDS, transport=transport)
            self.assertEqual(result["status"], status)
            transport.assert_not_called()
            self.assert_sanitized(result)


if __name__ == "__main__":
    unittest.main()
