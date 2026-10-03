import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import ssl
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler

from dwight.massive_data import (AGGREGATES_PREFIX, FEED, REFERENCE_URL, MassiveDataError,
                                 _NoRedirect, _default_transport, check_massive_connection,
                                 fetch_massive_history)

UTC = timezone.utc
ENV = {"MASSIVE_API_KEY": "fixture-secret-key-never-public"}
START = datetime(2025, 11, 28, 14, 30, tzinfo=UTC)
END = START + timedelta(minutes=5)


def aggregate(offset=0):
    return {"t": int((START + timedelta(minutes=offset)).timestamp() * 1000),
            "o": 100, "h": 102, "l": 99, "c": 101, "v": 10}


def body(rows=None, **values):
    rows = [aggregate()] if rows is None else rows
    return {"status": "OK", "ticker": "QQQ", "adjusted": False,
            "results": rows, "resultsCount": len(rows), **values}


class Response:
    def __init__(self, content, url, code=200):
        self.raw = content if isinstance(content, bytes) else json.dumps(content).encode()
        self.url, self.code = url, code
        self.read_size = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        self.read_size = size
        return self.raw[:size]

    def geturl(self):
        return self.url

    def getcode(self):
        return self.code


class Transport:
    def __init__(self, payloads):
        self.payloads, self.requests, self.responses = iter(payloads), [], []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        payload = next(self.payloads)
        if isinstance(payload, BaseException):
            raise payload
        if callable(payload):
            payload = payload(request.full_url)
        response = payload if isinstance(payload, Response) else Response(payload, request.full_url)
        self.responses.append(response)
        return response


class MassiveDataTests(unittest.TestCase):
    def test_private_reference_probe_has_no_history_or_order_claim(self):
        transport = Transport([{"status": "OK", "results": {
            "ticker": "QQQ", "market": "stocks", "locale": "us", "name": "Private name"}}])
        result = check_massive_connection(ENV, transport=transport)
        self.assertEqual(result["status"], "reference_metadata_available")
        self.assertTrue(result["capabilities"]["reference_metadata_read"])
        self.assertFalse(result["capabilities"]["qqq_data"])
        self.assertFalse(result["capabilities"]["account_read"])
        self.assertFalse(result["capabilities"]["order_execution"])
        self.assertNotIn("Private name", json.dumps(result))
        self.assertNotIn(ENV["MASSIVE_API_KEY"], json.dumps(result))
        request = transport.requests[0][0]
        self.assertEqual(request.full_url, REFERENCE_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + ENV["MASSIVE_API_KEY"])

    def test_missing_and_malformed_keys_do_not_call_transport(self):
        for environ, expected in (({}, "missing_credentials"),
                                  ({"MASSIVE_API_KEY": "abc\r\nsecret"}, "invalid_credentials"),
                                  ({"MASSIVE_API_KEY": " has-spaces"}, "invalid_credentials"),
                                  ({"MASSIVE_API_KEY": 123}, "invalid_credentials")):
            with self.subTest(expected=expected):
                transport = Transport([])
                self.assertEqual(check_massive_connection(environ, transport=transport)["status"], expected)
                self.assertEqual(transport.requests, [])

    def test_status_failures_are_sanitized_and_never_read_provider_body(self):
        for code, expected in ((401, "authentication_failed"), (403, "permission_denied"),
                               (451, "permission_denied"), (429, "rate_limited"),
                               (500, "unavailable"), (302, "redirect_rejected"),
                               (404, "invalid_response")):
            with self.subTest(code=code):
                provider_body = BytesIO(ENV["MASSIVE_API_KEY"].encode())
                error = HTTPError(REFERENCE_URL, code, "sensitive provider error", {}, provider_body)
                transport = Transport([error])
                result = check_massive_connection(ENV, transport=transport)
                self.assertEqual(result["status"], expected)
                self.assertNotIn("sensitive", json.dumps(result))
                self.assertNotIn(ENV["MASSIVE_API_KEY"], json.dumps(result))
                self.assertEqual(len(transport.requests), 1)
                self.assertTrue(provider_body.closed)

    def test_history_http_error_traceback_suppresses_reflected_provider_credentials(self):
        secret = ENV["MASSIVE_API_KEY"]
        representations = (secret, base64.b64encode(secret.encode()).decode(),
                           "".join("\\u%04x" % ord(character) for character in secret))
        for code in (401, 403, 429, 500, 302, 404):
            for reflected in representations:
                with self.subTest(code=code, representation=representations.index(reflected)):
                    provider_body = BytesIO(reflected.encode())
                    reason = "sensitive-provider-reason:" + reflected
                    error = HTTPError(REFERENCE_URL + "?key=" + reflected, code,
                                      reason, {}, provider_body)
                    transport = Transport([error])
                    with self.assertRaises(MassiveDataError) as caught:
                        fetch_massive_history(START, END, ENV, transport=transport)
                    formatted = "".join(traceback.format_exception(caught.exception))
                    self.assertNotIn(secret, formatted)
                    self.assertNotIn(reflected, formatted)
                    self.assertNotIn("sensitive-provider-reason", formatted)
                    self.assertTrue(caught.exception.__suppress_context__)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(provider_body.closed)
                    self.assertEqual(len(transport.requests), 1)

    def test_redirect_response_url_rejected_without_read(self):
        response = Response(body(), "https://attacker.example/test")
        result = check_massive_connection(ENV, transport=Transport([response]))
        self.assertEqual(result["status"], "redirect_rejected")
        self.assertIsNone(response.read_size)

    def test_transport_failure_does_not_echo_exception_or_retry(self):
        transport = Transport([URLError(ENV["MASSIVE_API_KEY"])])
        result = check_massive_connection(ENV, transport=transport)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn(ENV["MASSIVE_API_KEY"], json.dumps(result))
        self.assertEqual(len(transport.requests), 1)

    def test_reference_schema_rejects_wrong_instrument_and_nonfinite_json(self):
        for payload in ({"status": "OK", "results": {"ticker": "SPY", "market": "stocks", "locale": "us"}},
                        b'{"status":"OK","results":NaN}',
                        b'{"status":"OK","status":"OK","results":{}}',
                        {"status": "NOT_AUTHORIZED", "message": "private provider error"}):
            with self.subTest(payload=payload):
                self.assertEqual(check_massive_connection(ENV, transport=Transport([payload]))["status"],
                                 "invalid_response")

    def test_history_normalizes_start_timestamps_and_keeps_provider_provenance(self):
        payload = body([aggregate(i) for i in range(5)])
        transport = Transport([payload])
        result = fetch_massive_history(START, END, ENV, transport=transport)
        self.assertEqual(result["records"][0]["t"], START.isoformat())
        self.assertEqual(result["records"][-1]["t"], (END - timedelta(minutes=1)).isoformat())
        self.assertEqual(result["provenance"]["source"], "massive")
        self.assertEqual(result["provenance"]["feed"], FEED)
        self.assertEqual(result["provenance"]["adjustment"], "raw")
        self.assertEqual(result["provenance"]["request_bounds"], "half_open")
        self.assertEqual(result["raw_pages"], [json.dumps(payload).encode()])
        parsed = urlsplit(transport.requests[0][0].full_url)
        self.assertEqual(parsed.netloc, "api.massive.com")
        self.assertEqual(parsed.path, AGGREGATES_PREFIX +
                         f"{int(START.timestamp()*1000)}/{int(END.timestamp()*1000)-1}")
        self.assertEqual(parse_qs(parsed.query), {"adjusted": ["false"], "sort": ["asc"], "limit": ["50000"]})
        self.assertNotIn(ENV["MASSIVE_API_KEY"], parsed.geturl())

    def test_pagination_keeps_bearer_auth_and_raw_policy(self):
        def first(url):
            return body([aggregate(0)], next_url=url.split("?", 1)[0] + "?cursor=page-two")
        transport = Transport([first, body([aggregate(i) for i in range(1, 5)])])
        result = fetch_massive_history(START, END, ENV, transport=transport)
        self.assertEqual(len(result["records"]), 5)
        self.assertEqual(len(result["raw_pages"]), 2)
        second = transport.requests[1][0]
        self.assertEqual(parse_qs(urlsplit(second.full_url).query),
                         {"cursor": ["page-two"], "adjusted": ["false"], "sort": ["asc"], "limit": ["50000"]})
        self.assertEqual(second.get_header("Authorization"), "Bearer " + ENV["MASSIVE_API_KEY"])

    def test_untrusted_pagination_never_sends_another_request(self):
        bad_urls = ("http://api.massive.com", "https://attacker.example", "https://api.massive.com:443",
                    "https://api.massive.com@attacker.example", "https://api.massive.com/v2/aggs/ticker/SPY")
        for bad_url in bad_urls:
            with self.subTest(url=bad_url):
                transport = Transport([body(next_url=bad_url)])
                with self.assertRaises(MassiveDataError):
                    fetch_massive_history(START, END, ENV, transport=transport)
                self.assertEqual(len(transport.requests), 1)
        for query in ("apiKey=exposed", "cursor=page&apiKey=exposed", "cursor=page&adjusted=true",
                      "cursor=page&sort=desc", "cursor=page&limit=1", "cursor=a&cursor=b",
                      "cursor=page#fragment", "cursor=page&unknown=secret", "cursor="):
            with self.subTest(query=query):
                transport = Transport([lambda url: body(next_url=url.split("?", 1)[0] + "?" + query)])
                with self.assertRaises(MassiveDataError):
                    fetch_massive_history(START, END, ENV, transport=transport)
                self.assertEqual(len(transport.requests), 1)

    def test_repeated_pagination_and_page_limit_fail_closed(self):
        def page(url):
            return body([], next_url=url.split("?", 1)[0] + "?cursor=repeated")
        transport = Transport([page, page])
        with self.assertRaisesRegex(MassiveDataError, "unexpected"):
            fetch_massive_history(START, END, ENV, transport=transport)
        self.assertEqual(len(transport.requests), 2)
        with self.assertRaisesRegex(MassiveDataError, "bounds"):
            fetch_massive_history(START, END, ENV, transport=Transport([page]), max_pages=1)

    def test_adjustment_symbol_count_and_row_validation(self):
        bad_rows = [dict(aggregate(), t=True), dict(aggregate(), t=aggregate()["t"] + 1),
                    dict(aggregate(), v=-1), dict(aggregate(), o=True), dict(aggregate(), h=float("inf")),
                    dict(aggregate(), c=103), dict(aggregate(), t=int(END.timestamp()*1000)),
                    dict(aggregate(), t=int((START - timedelta(minutes=1)).timestamp()*1000))]
        payloads = [body(adjusted=True), body(ticker="SPY"), body(resultsCount=4),
                    body([aggregate(), aggregate()]), body([aggregate(1), aggregate(0)]),
                    *[body([row]) for row in bad_rows]]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(MassiveDataError):
                fetch_massive_history(START, END, ENV, transport=Transport([payload]))

    def test_empty_history_is_explicit_and_caller_must_check_session_completeness(self):
        result = fetch_massive_history(START, END, ENV, transport=Transport([body([])]))
        self.assertEqual(result["records"], [])
        self.assertIn("omit intervals", result["provenance"]["limitations"][0])

    def test_credentials_echo_is_rejected_even_when_json_escaped(self):
        payload = body()
        payload["request_id"] = ENV["MASSIVE_API_KEY"]
        raw = json.dumps(payload).replace("fixture", "\\u0066ixture").encode()
        with self.assertRaises(MassiveDataError):
            fetch_massive_history(START, END, ENV, transport=Transport([raw]))

    def test_bytes_and_elapsed_bounds(self):
        with self.assertRaisesRegex(MassiveDataError, "bounds"):
            fetch_massive_history(START, END, ENV, transport=Transport([body()]), max_bytes=10)
        transport = Transport([body()])
        with patch("dwight.massive_data.time.monotonic", side_effect=[0, 0, 121]), \
                self.assertRaisesRegex(MassiveDataError, "bounds"):
            fetch_massive_history(START, END, ENV, transport=transport)
        self.assertEqual(len(transport.requests), 1)
        transport = Transport([])
        with patch("dwight.massive_data.time.monotonic", side_effect=[0, 121]), \
                self.assertRaisesRegex(MassiveDataError, "bounds"):
            fetch_massive_history(START, END, ENV, transport=transport)
        self.assertEqual(transport.requests, [])

    def test_dripping_response_is_bounded_during_read(self):
        response = Response(body(), REFERENCE_URL)
        response.read1 = lambda size: b" "
        with patch("dwight.massive_data.time.monotonic", side_effect=[0, 0, 1, 1, 11]):
            result = check_massive_connection(ENV, transport=Transport([response]))
        self.assertEqual(result["status"], "bound_reached")

    def test_invalid_bounds_and_limits_never_read_credentials_or_network(self):
        transport = Transport([])
        for start, end in ((START.replace(tzinfo=None), END), (START, START), (END, START),
                           (START + timedelta(seconds=1), END),
                           (START, datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(days=1))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                fetch_massive_history(start, end, {}, transport=transport)
        for options in ({"max_pages": True}, {"max_pages": 1001}, {"max_bytes": 0},
                        {"timeout_seconds": float("nan")}, {"timeout_seconds": 61},
                        {"max_duration_seconds": 301}, {"max_duration_seconds": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                fetch_massive_history(START, END, {}, transport=transport, **options)
        self.assertEqual(transport.requests, [])

    def test_default_transport_disables_proxies_redirects_and_keeps_tls_validation(self):
        with patch("dwight.massive_data.build_opener") as build:
            _default_transport()
        handlers = build.call_args.args
        self.assertEqual(next(handler for handler in handlers if isinstance(handler, ProxyHandler)).proxies, {})
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in handlers))
        tls = next(handler for handler in handlers if isinstance(handler, HTTPSHandler))._context
        self.assertEqual(tls.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tls.check_hostname)


if __name__ == "__main__":
    unittest.main()
