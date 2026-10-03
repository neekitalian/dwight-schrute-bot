"""Public collector validation and failure tests with synthetic market payloads."""
import copy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from dwight.tokenized_data import (
    MAX_RESPONSE_BYTES, PAIR, TIMEOUT_SECONDS, TokenizedDataError,
    _default_transport, collect_qqqx_snapshot,
)

NOW = datetime(2026, 10, 3, 12, 2, tzinfo=timezone.utc)
EPOCH = int(NOW.timestamp())
START = EPOCH - EPOCH % 300


def fixtures():
    return {
        "Time": {"error": [], "result": {"unixtime": EPOCH, "rfc1123": "unused"}},
        "AssetPairs": {"error": [], "result": {PAIR: {
            "aclass_base": "tokenized_asset", "base": "QQQx", "aclass_quote": "currency",
            "quote": "ZUSD", "altname": PAIR, "wsname": "QQQx/USD", "status": "online",
            "lot": "unit", "lot_multiplier": 1, "execution_venue": "international"}}},
        "OHLC": {"error": [], "result": {PAIR: [
            [START - 600, "600", "602", "599", "601", "600.5", "7.4", 3],
            [START - 300, "601", "602", "600", "600", "0", "0", 0],
            [START, "600", "604", "598", "602", "601", "100", 60],
        ], "last": START}},
        "Depth": {"error": [], "result": {PAIR: {
            "bids": [["600", "2", EPOCH - 1.5], ["599", "3", EPOCH - 10]],
            "asks": [["601", "4", EPOCH - 2], ["602", "5", EPOCH - 20]],
        }}},
        "Ticker": {"error": [], "result": {PAIR: {
            "a": ["601", "1", "1.0"], "b": ["600", "1", "1.0"],
            "c": ["600.5", "0.01"], "v": ["7.4", "20.0"],
        }}},
    }


class Response:
    def __init__(self, payload, url, code=200):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.url = url
        self.code = code
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        self.read_sizes.append(size)
        return self.raw[:size]

    def geturl(self):
        return self.url

    def getcode(self):
        return self.code


class Transport:
    def __init__(self, data=None, *, code=200, url=None):
        self.data = fixtures() if data is None else data
        self.calls, self.responses = [], []
        self.code, self.url = code, url

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        endpoint = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        response = Response(self.data[endpoint], self.url or request.full_url, self.code)
        self.responses.append(response)
        return response


class TokenizedDataTests(unittest.TestCase):
    def collect(self, data=None, **kwargs):
        return collect_qqqx_snapshot(transport=Transport(data), clock=lambda: NOW, depth=2, **kwargs)

    def rejected(self, data, code="invalid_response"):
        with self.assertRaises(TokenizedDataError) as caught:
            self.collect(data)
        self.assertEqual(caught.exception.code, code)

    def test_fixed_public_requests_provenance_and_complete_bars(self):
        transport = Transport()
        with patch.dict("os.environ", {"KRAKEN_API_KEY": "do-not-read", "HTTPS_PROXY": "https://invalid"}):
            result = collect_qqqx_snapshot(transport=transport, clock=lambda: NOW, depth=2)
        self.assertEqual(len(transport.calls), 5)
        self.assertEqual([r.full_url.split("?")[0].rsplit("/", 1)[-1] for r, _ in transport.calls],
                         ["Time", "AssetPairs", "OHLC", "Depth", "Ticker"])
        for request, timeout in transport.calls:
            self.assertEqual(request.method, "GET")
            self.assertEqual(timeout, TIMEOUT_SECONDS)
            self.assertTrue(request.full_url.startswith("https://api.kraken.com/0/public/"))
            self.assertEqual(set(dict(request.header_items())), {"Accept", "User-agent"})
            self.assertIsNone(request.data)
        pair_query = parse_qs(urlsplit(transport.calls[1][0].full_url).query)
        self.assertEqual(pair_query, {"pair": [PAIR], "aclass_base": ["tokenized_asset"]})
        for request, _ in transport.calls[2:]:
            self.assertEqual(parse_qs(urlsplit(request.full_url).query)["asset_class"], ["tokenized_asset"])
        for recorded, response in zip(result["provenance"]["endpoints"], transport.responses):
            self.assertEqual(recorded["response_sha256"], sha256(response.raw).hexdigest())
            self.assertEqual(response.read_sizes, [MAX_RESPONSE_BYTES + 1])
        self.assertEqual(result["data_kind"], "observed_public_market")
        self.assertFalse(result["execution_enabled"])
        self.assertFalse(result["eligibility_verified"])
        self.assertFalse(result["provenance"]["atomic_snapshot"])
        self.assertEqual(len(result["bars"]), 2)
        self.assertEqual(result["history_metadata"]["unfinished_rows_omitted"], 1)
        self.assertEqual(result["bars"][1]["volume"], 0)
        self.assertEqual(result["instrument"]["quote"], "USD")
        self.assertEqual(result["instrument"]["provider_pair_metadata"]["quote"], "ZUSD")
        self.assertIsNone(result["instrument"]["corporate_action_multiplier"])
        self.assertIsNone(result["instrument"]["chain"])
        self.assertEqual(result["instrument"]["issuer"], "unknown")
        self.assertIsNone(result["ticker"]["provider_event_timestamp"])
        json.dumps(result, allow_nan=False)

    def test_responses_have_individual_times_not_atomic_time(self):
        stamps = iter(NOW + timedelta(seconds=i) for i in range(12))
        result = collect_qqqx_snapshot(transport=Transport(), clock=lambda: next(stamps), depth=2)
        observed = [row["received_at"] for row in result["provenance"]["endpoints"]]
        self.assertEqual(len(set(observed)), 5)
        self.assertEqual(result["bars_observed_at"], observed[2])
        self.assertEqual(result["orderbook"]["observed_at"], observed[3])
        self.assertEqual(result["ticker"]["observed_at"], observed[4])

    def test_current_row_omitted_even_if_cached_after_nominal_close(self):
        result = collect_qqqx_snapshot(transport=Transport(), clock=lambda: NOW + timedelta(minutes=30), depth=2)
        self.assertEqual(len(result["bars"]), 2)

    def test_rolling_720_completed_plus_unfinished_and_no_pagination(self):
        data = fixtures()
        data["OHLC"]["result"][PAIR] = [
            [START - 300 * (720 - i), "600", "600", "600", "600", "0", "0", 0]
            for i in range(721)]
        result = self.collect(data)
        self.assertEqual(len(result["bars"]), 720)
        data["OHLC"]["result"][PAIR].insert(0, [START - 300 * 721, "600", "600", "600", "600", "0", "0", 0])
        self.rejected(data)

    def test_instrument_identity_status_and_class_must_be_exact(self):
        for key, wrong in (("base", "QQQ"), ("aclass_base", "currency"), ("quote", "USDT"),
                           ("status", "post_only"), ("execution_venue", "dark_pool"),
                           ("altname", "QQQUSD"), ("wsname", "QQQ/USD"), ("lot", "contract")):
            with self.subTest(key=key):
                data = fixtures()
                data["AssetPairs"]["result"][PAIR][key] = wrong
                self.rejected(data, "unsupported_instrument")
        data = fixtures()
        data["AssetPairs"]["result"]["QQQUSD"] = copy.deepcopy(data["AssetPairs"]["result"][PAIR])
        self.rejected(data, "unsupported_instrument")

    def test_nonfinite_and_invalid_numbers_never_enter_snapshot(self):
        for value in ("NaN", "Infinity", "-1", True, None, "1e9999", "0"):
            with self.subTest(value=value):
                data = fixtures()
                data["OHLC"]["result"][PAIR][0][1] = value
                self.rejected(data)
        data = fixtures()
        data["Depth"]["result"][PAIR]["bids"][0][1] = "NaN"
        self.rejected(data)
        data = fixtures()
        data["Ticker"]["result"][PAIR]["v"][0] = "Infinity"
        self.rejected(data)

    def test_timestamp_alignment_order_duplicates_future_and_types(self):
        for value in (START - 601, START + 300, True, "1790000000", -10, 10**30):
            with self.subTest(value=value):
                data = fixtures()
                data["OHLC"]["result"][PAIR][0][0] = value
                self.rejected(data)
        data = fixtures()
        data["OHLC"]["result"][PAIR][1][0] = data["OHLC"]["result"][PAIR][0][0]
        self.rejected(data)
        data = fixtures()
        data["OHLC"]["result"][PAIR][1][0] = START  # Non-final uncommitted row.
        data["OHLC"]["result"][PAIR][-1][0] = START + 60
        self.rejected(data)
        data = fixtures()
        data["Time"]["result"]["unixtime"] = EPOCH + 120
        self.rejected(data)

    def test_invalid_ohlc_shape_range_volume_trade_count_and_pair(self):
        for index, value in ((2, "598"), (3, "602"), (5, "800"), (6, "-1"), (7, -1), (7, 1.5)):
            with self.subTest(index=index, value=value):
                data = fixtures()
                data["OHLC"]["result"][PAIR][0][index] = value
                self.rejected(data)
        data = fixtures()
        data["OHLC"]["result"][PAIR][0].append("unexpected")
        self.rejected(data)
        data = fixtures()
        data["OHLC"]["result"]["QQQUSD"] = data["OHLC"]["result"].pop(PAIR)
        self.rejected(data)

    def test_book_requires_positive_sorted_non_crossed_bounded_levels(self):
        mutations = [
            lambda book: book["bids"].reverse(),
            lambda book: book["asks"].reverse(),
            lambda book: book["asks"][0].__setitem__(0, "599"),
            lambda book: book["bids"][0].__setitem__(1, "0"),
            lambda book: book["asks"][0].__setitem__(2, EPOCH + 120),
            lambda book: book["bids"].append(["598", "1", EPOCH]),
            lambda book: book.__setitem__("asks", []),
            lambda book: book["bids"][1].__setitem__(0, "600"),
        ]
        for mutation in mutations:
            data = fixtures()
            mutation(data["Depth"]["result"][PAIR])
            self.rejected(data)

    def test_config_rejected_without_request_and_clock_must_be_aware(self):
        for depth in (0, 51, True, 2.0, "2"):
            transport = Transport()
            with self.assertRaises(TokenizedDataError) as caught:
                collect_qqqx_snapshot(transport=transport, depth=depth)
            self.assertEqual(caught.exception.code, "invalid_configuration")
            self.assertEqual(transport.calls, [])
        with self.assertRaises(TokenizedDataError):
            collect_qqqx_snapshot(transport=Transport(), clock=lambda: NOW.replace(tzinfo=None))
        stamps = iter([NOW, NOW, NOW - timedelta(seconds=1)])
        with self.assertRaises(TokenizedDataError):
            collect_qqqx_snapshot(transport=Transport(), clock=lambda: next(stamps))

    def test_http_status_error_body_and_network_errors_are_sanitized(self):
        for status, expected in ((302, "redirect_rejected"), (401, "permission_denied"),
                                 (451, "permission_denied"), (429, "rate_limited"),
                                 (500, "unavailable"), (404, "invalid_response")):
            with self.subTest(status=status):
                transport = Transport(code=status)
                with self.assertRaises(TokenizedDataError) as caught:
                    collect_qqqx_snapshot(transport=transport, clock=lambda: NOW)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(transport.responses[0].read_sizes, [])
        for exception in (URLError("private-sensitive-string"), TimeoutError("private-sensitive-string"),
                          HTTPError("https://private", 403, "private-sensitive-string", {}, io.BytesIO(b"private"))):
            def fail(*_, **__):
                raise exception
            with self.assertRaises(TokenizedDataError) as caught:
                collect_qqqx_snapshot(transport=fail, clock=lambda: NOW)
            self.assertNotIn("private", str(caught.exception))
        data = fixtures()
        data["Time"] = {"error": ["EPrivate:secret-sensitive-string"]}
        with self.assertRaises(TokenizedDataError) as caught:
            self.collect(data)
        self.assertNotIn("secret", str(caught.exception))

    def test_redirected_custom_transport_oversized_duplicate_json_and_raw_nan(self):
        with self.assertRaises(TokenizedDataError) as caught:
            collect_qqqx_snapshot(transport=Transport(url="https://evil.invalid"), clock=lambda: NOW)
        self.assertEqual(caught.exception.code, "redirect_rejected")
        for raw in (b"x" * (MAX_RESPONSE_BYTES + 1), b'{"error":[],"result":{"unixtime":NaN}}',
                    b'{"error":[],"result":{},"result":{"unixtime":1790000000}}'):
            data = fixtures()
            data["Time"] = raw
            self.rejected(data)

    def test_default_transport_verifies_tls_disables_proxy_and_redirects(self):
        context = __import__("ssl").create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, __import__("ssl").CERT_REQUIRED)
        with patch("dwight.tokenized_data.ssl.create_default_context", return_value=context), \
             patch("dwight.tokenized_data.build_opener") as build:
            _default_transport()
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[1].redirect_request(None, None, 302, "", {}, "https://evil"))
        self.assertIs(handlers[2]._context, context)


if __name__ == "__main__":
    unittest.main()
