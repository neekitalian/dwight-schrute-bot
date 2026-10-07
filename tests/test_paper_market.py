"""Bounded paper and quote HTTP behavior using offline transport fixtures."""
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
import io
import json
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from dwight.paper import BrokerHTTPError, PAPER_URL, PaperError, PreparationRequired, Quote
from dwight.paper_market import (
    HardenedAlpacaPaperClient, MAX_RESPONSE_BYTES, PaperMarket, TIMEOUT_SECONDS, _transport,
)


def quote_payload(**updates):
    row = {"bp": 600.01, "ap": 600.03, "bs": 10, "as": 20,
           "t": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    row.update(updates)
    return {"quotes": {"QQQ": row}}


class Response:
    def __init__(self, request, payload, *, status=200, final_url=None):
        self.url = final_url or request.full_url
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status, self.read_sizes = status, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read(self, size):
        self.read_sizes.append(size)
        return self.raw[:size]


class Transport:
    def __init__(self, payload=None, *, status=200, final_url=None):
        self.payload = quote_payload() if payload is None else payload
        self.status, self.final_url = status, final_url
        self.calls, self.responses = [], []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        response = Response(request, self.payload, status=self.status, final_url=self.final_url)
        self.responses.append(response)
        return response


class PaperMarketTests(unittest.TestCase):
    def test_same_feed_quote_fixed_origin_bounded_read_and_event_time(self):
        payload, transport = quote_payload(), None
        transport = Transport(payload)
        market = PaperMarket("fixture-key", "fixture-secret", transport=transport)
        for feed in ("sip", "iex"):
            quote = market.latest_quote(feed)
            self.assertIsInstance(quote, Quote)
            self.assertEqual((quote.symbol, quote.bid, quote.ask, quote.feed), ("QQQ", 600.01, 600.03, feed))
            self.assertEqual(quote.timestamp, datetime.fromisoformat(payload["quotes"]["QQQ"]["t"]))
            self.assertLess(quote.timestamp, market.last_received_at)
            request, timeout = transport.calls[-1]
            self.assertEqual(timeout, 5)
            self.assertEqual(request.method, "GET")
            self.assertIsNone(request.data)
            parsed = urlsplit(request.full_url)
            self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                             ("https", "data.alpaca.markets", "/v2/stocks/quotes/latest"))
            self.assertEqual(parse_qs(parsed.query), {"symbols": ["QQQ"], "feed": [feed], "currency": ["USD"]})
            self.assertEqual(transport.responses[-1].read_sizes, [MAX_RESPONSE_BYTES + 1])

    def test_future_missing_naive_or_invalid_quote_time_rejected(self):
        for value in (None, "bad", "2026-10-01T14:00:00", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()):
            with self.subTest(value=value):
                market = PaperMarket("fixture-key", "fixture-secret", transport=Transport(quote_payload(t=value)))
                with self.assertRaises(PaperError):
                    market.latest_quote("sip")
                self.assertIsNone(market.last_received_at)

    def test_old_quote_preserves_old_event_time_without_claiming_readiness(self):
        stamp = "2026-01-01T14:00:00Z"
        market = PaperMarket("fixture-key", "fixture-secret", transport=Transport(quote_payload(t=stamp)))
        self.assertEqual(market.latest_quote("iex").timestamp.isoformat(), "2026-01-01T14:00:00+00:00")

    def test_invalid_quote_numbers_crossed_and_empty_sides_rejected(self):
        for field, value in (("bp", True), ("bp", "NaN"), ("ap", "Infinity"), ("bp", 0),
                             ("bp", 601), ("bs", 0), ("as", -1), ("bs", "1e999")):
            with self.subTest(field=field, value=value):
                market = PaperMarket("fixture-key", "fixture-secret", transport=Transport(quote_payload(**{field: value})))
                with self.assertRaises(PaperError):
                    market.latest_quote("sip")

    def test_exact_qqq_only_response_and_explicit_feed(self):
        payload = quote_payload()
        payload["quotes"]["SPY"] = payload["quotes"]["QQQ"]
        for invalid in (payload, {"quotes": {}}, {"quotes": {"QQQ": None}}, {}):
            with self.assertRaises(PaperError):
                PaperMarket("fixture-key", "fixture-secret", transport=Transport(invalid)).latest_quote("sip")
        for feed in (None, "delayed_sip", "", [], True):
            transport = Transport()
            with self.assertRaises(ValueError):
                PaperMarket("fixture-key", "fixture-secret", transport=transport).latest_quote(feed)
            self.assertEqual(transport.calls, [])

    def test_environment_loading_rejects_overrides_and_bad_credentials(self):
        for client in (PaperMarket, HardenedAlpacaPaperClient):
            transport = Transport()
            client.from_env(environ={"ALPACA_API_KEY": "fixture-key", "ALPACA_SECRET_KEY": "fixture-secret"}, transport=transport)
            self.assertEqual(transport.calls, [])
            for env in ({}, {"APCA_API_KEY_ID": "fixture-key", "APCA_API_SECRET_KEY": "fixture-secret",
                             "ALPACA_BASE_URL": "https://api.alpaca.markets"}):
                with self.assertRaises(PreparationRequired):
                    client.from_env(environ=env, transport=transport)
            for value in ("", "bad\nvalue", "bad\tvalue", "bad value", "k" * 4097, None):
                with self.assertRaises(PreparationRequired):
                    client(value, "fixture-secret", transport=transport)

    def test_one_request_no_retries_and_sanitized_errors(self):
        for error in (URLError("credential-sensitive-string"), TimeoutError("credential-sensitive-string"),
                      IncompleteRead(b"credential-sensitive-string"),
                      HTTPError("https://private.invalid", 401, "credential-sensitive-string", {}, io.BytesIO(b"secret"))):
            calls = []
            def fail(request, *, timeout):
                calls.append(request)
                raise error
            with self.assertRaises(PaperError) as caught:
                PaperMarket("fixture-key", "fixture-secret", transport=fail).latest_quote("sip")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("sensitive", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))

    def test_status_redirects_bounds_and_strict_json(self):
        for status in (301, 401, 403, 429, 500):
            transport = Transport(status=status)
            with self.assertRaises(PaperError):
                PaperMarket("fixture-key", "fixture-secret", transport=transport).latest_quote("sip")
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.responses[0].read_sizes, [])
        with self.assertRaisesRegex(PaperError, "redirect"):
            PaperMarket("fixture-key", "fixture-secret", transport=Transport(final_url="https://evil.invalid")).latest_quote("sip")
        for raw in (b"x" * (MAX_RESPONSE_BYTES + 1), b'{"quotes":{},"quotes":{}}', b'{"bp":NaN}', b'{"bp":1e9999}', b'not-json'):
            with self.assertRaises(PaperError):
                PaperMarket("fixture-key", "fixture-secret", transport=Transport(raw)).latest_quote("sip")

    def test_default_tls_proxy_and_redirect_policy(self):
        context = ssl.create_default_context()
        with patch("dwight.paper_market.ssl.create_default_context", return_value=context), \
             patch("dwight.paper_market.build_opener") as build:
            _transport()
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[1].redirect_request(None, None, 302, "", {}, "https://evil"))
        self.assertIs(handlers[2]._context, context)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class HardenedPaperClientTests(unittest.TestCase):
    def test_read_operations_and_exact_qqq_asset_use_fixed_paper_origin(self):
        transport = Transport({"symbol": "QQQ", "tradable": True})
        client = HardenedAlpacaPaperClient("fixture-key", "fixture-secret", transport=transport)
        self.assertEqual(client.base_url, PAPER_URL)
        self.assertEqual(client.get_asset()["symbol"], "QQQ")
        client.get_account()
        client.get_positions()
        client.get_clock()
        client.get_calendar("2026-10-05", "2026-10-05")
        for request, timeout in transport.calls:
            self.assertTrue(request.full_url.startswith(PAPER_URL + "/v2/"))
            self.assertEqual(request.method, "GET")
            self.assertEqual(timeout, TIMEOUT_SECONDS)
        self.assertEqual(urlsplit(transport.calls[0][0].full_url).path, "/v2/assets/QQQ")

    def test_existing_order_interface_submission_and_cancellation_are_fixture_only(self):
        transport = Transport({"id": "fixture-order"})
        client = HardenedAlpacaPaperClient("fixture-key", "fixture-secret", transport=transport)
        client.submit_order({"symbol": "QQQ", "side": "buy", "qty": "1"})
        self.assertEqual(transport.calls[0][0].method, "POST")
        self.assertEqual(json.loads(transport.calls[0][0].data)["symbol"], "QQQ")
        client.get_order("fixture-order")
        self.assertIn("nested=true", transport.calls[-1][0].full_url)
        transport.payload, transport.status = b"", 204
        self.assertIsNone(client.cancel_order("fixture-order"))
        self.assertEqual(transport.calls[-1][0].method, "DELETE")

    def test_client_refuses_live_origin_timeout_and_unapproved_paths(self):
        transport = Transport()
        for url in ("https://api.alpaca.markets", PAPER_URL + "/", "https://evil.invalid"):
            with self.assertRaises(PreparationRequired):
                HardenedAlpacaPaperClient("fixture-key", "fixture-secret", base_url=url, transport=transport)
        for timeout in (True, 4, 15, 0):
            with self.assertRaises(ValueError):
                HardenedAlpacaPaperClient("fixture-key", "fixture-secret", timeout=timeout, transport=transport)
        client = HardenedAlpacaPaperClient("fixture-key", "fixture-secret", transport=transport)
        for method, path in (("GET", "/v2/assets/SPY"), ("GET", "https://evil.invalid"),
                             ("POST", "/v2/account"), ("DELETE", "/v2/positions/QQQ"),
                             ("GET", "/v2/orders/../../evil")):
            with self.assertRaises(ValueError):
                client._request(method, path)
        with self.assertRaises(PaperError):
            client.submit_order({"symbol": "SPY"})
        self.assertEqual(transport.calls, [])

    def test_lookup_404_preserves_existing_reconciliation_contract(self):
        client = HardenedAlpacaPaperClient("fixture-key", "fixture-secret", transport=Transport(status=404))
        self.assertIsNone(client.get_order_by_client_id("fixture-client-id"))

    def test_fill_page_is_read_only_fixed_window_and_explicit_ascending(self):
        transport = Transport([])
        client = HardenedAlpacaPaperClient("fixture-key", "fixture-secret", transport=transport)
        start = datetime(2026, 10, 5, 14, tzinfo=timezone.utc)
        end = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
        token = "20261005140100000::fixture-activity"
        self.assertEqual(client.get_fill_activities(start, end, token, 25), [])
        request, timeout = transport.calls[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "paper-api.alpaca.markets", "/v2/account/activities/FILL"))
        self.assertEqual(parse_qs(parsed.query), {"after": [start.isoformat()], "until": [end.isoformat()],
                                                "direction": ["asc"], "page_size": ["25"], "page_token": [token]})
        self.assertEqual((request.method, request.data, timeout), ("GET", None, 5))

    def test_fill_query_rejects_ambiguous_window_and_unbounded_pages_before_http(self):
        transport = Transport([])
        client = HardenedAlpacaPaperClient("fixture-key", "fixture-secret", transport=transport)
        start, end = "2026-10-05T14:00:00Z", "2026-10-05T15:00:00Z"
        for args in ((None, end), ("2026-10-05T14:00:00", end), (end, start), (start, start),
                     (start, end, ""), (start, end, "bad\nvalue"), (start, end, "x" * 257),
                     (start, end, None, True), (start, end, None, 0), (start, end, None, 101)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                client.get_fill_activities(*args)
        self.assertEqual(transport.calls, [])

    def test_fill_page_must_be_bounded_array_and_failure_is_not_retried(self):
        for payload in ({"activities": []}, None, [{}] * 101):
            transport = Transport(payload)
            client = HardenedAlpacaPaperClient("fixture-key", "fixture-secret", transport=transport)
            with self.assertRaises(PaperError):
                client.get_fill_activities("2026-10-05T14:00:00Z", "2026-10-05T15:00:00Z")
            self.assertEqual(len(transport.calls), 1)
        transport = Transport([], status=429)
        client = HardenedAlpacaPaperClient("fixture-key", "fixture-secret", transport=transport)
        with self.assertRaises(BrokerHTTPError):
            client.get_fill_activities("2026-10-05T14:00:00Z", "2026-10-05T15:00:00Z")
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
