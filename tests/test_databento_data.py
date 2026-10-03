import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler

from dwight.databento_data import (
    COUNT_URL, COST_URL, DATASET_LIST_URL, RANGE_URL, DatabentoDataError,
    _NoRedirect, _default_transport, check_databento_connection,
    estimate_databento_history, fetch_databento_history,
)

UTC = timezone.utc
# Deliberately invented values; these tests make no real network requests.
KEY = "db-" + "A" * 29
ENV = {"DATABENTO_API_KEY": KEY}
START = datetime(2025, 11, 28, 14, 30, tzinfo=UTC)
END = START + timedelta(minutes=10)


def bar(offset=0, **values):
    result = {"hd": {"ts_event": (START + timedelta(minutes=offset)).isoformat(),
                     "rtype": 33, "publisher_id": 2, "instrument_id": 123},
              "open": "100.000000000", "high": "102.000000000",
              "low": "99.000000000", "close": "101.000000000",
              "volume": "200", "symbol": "QQQ"}
    result.update(values)
    return result


def encoded(rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


class Response:
    def __init__(self, raw, url, status=200):
        self.stream, self.url, self.status = BytesIO(raw), url, status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, size):
        return self.stream.read(size)


class Transport:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return Response(reply, request.full_url)


class IncrementalResponse(Response):
    def __init__(self, raw, url):
        super().__init__(raw, url)
        self.chunk_reads = 0

    def read(self, size):
        raise AssertionError("Buffered read must not be used when read1 is available")

    def read1(self, size):
        self.chunk_reads += 1
        return self.stream.read(min(size, 4))


class DatabentoDataTests(unittest.TestCase):
    def fetch(self, transport, **kwargs):
        return fetch_databento_history(START, END, ENV, dataset="XNAS.ITCH",
                                       max_cost_usd="1.00", transport=transport, **kwargs)

    def test_metadata_auth_probe_has_no_data_or_trading_claim(self):
        transport = Transport([b'["XNAS.ITCH", "EQUS.MINI"]'])
        result = check_databento_connection(ENV, transport=transport)
        self.assertEqual(result["status"], "connected")
        self.assertTrue(result["capabilities"]["metadata_authentication"])
        for name in ("qqq_data", "historical_data", "live_data", "account_read", "order_execution"):
            self.assertFalse(result["capabilities"][name])
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn("XNAS.ITCH", json.dumps(result))
        self.assertEqual(len(transport.requests), 1)
        request, timeout = transport.requests[0]
        self.assertEqual(request.full_url, DATASET_LIST_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 10)
        expected = "Basic " + base64.b64encode((KEY + ":").encode()).decode()
        self.assertEqual(request.get_header("Authorization"), expected)
        self.assertNotIn(KEY, request.full_url)
        self.assertIsNone(request.data)

    def test_no_credentials_or_invalid_credentials_make_no_requests(self):
        transport = Transport([])
        for env, status in (({}, "setup_required"), ({"DATABENTO_API_KEY": KEY + "\n"}, "invalid_configuration"),
                            ({"DATABENTO_API_KEY": True}, "invalid_configuration")):
            with self.subTest(status=status):
                self.assertEqual(check_databento_connection(env, transport=transport)["status"], status)
        self.assertEqual(transport.requests, [])

    def test_provider_http_error_and_reason_are_sanitized(self):
        error = HTTPError(DATASET_LIST_URL, 401, KEY, {}, BytesIO(KEY.encode()))
        result = check_databento_connection(ENV, transport=Transport([error]))
        self.assertEqual(result["status"], "authentication_failed")
        self.assertNotIn(KEY, json.dumps(result))
        error = HTTPError(COST_URL, 403, KEY, {}, BytesIO(KEY.encode()))
        with self.assertRaises(DatabentoDataError) as caught:
            self.fetch(Transport([error]))
        self.assertEqual(caught.exception.status, "permission_denied")
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn(KEY, str(caught.exception))

    def test_default_transport_blocks_redirects_and_proxy_overrides(self):
        with patch("dwight.databento_data.build_opener") as build:
            _default_transport()
        handlers = build.call_args.args
        proxy = next(handler for handler in handlers if isinstance(handler, ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in handlers))
        https = next(handler for handler in handlers if isinstance(handler, HTTPSHandler))
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)

    def test_metadata_probe_redirect_large_response_and_shape_fail_closed(self):
        replies = [lambda req: Response(b"[]", "https://example.com/"),
                   b"x" * 262145, b'{}', b'["https://example.com"]', b'["XNAS.ITCH","XNAS.ITCH"]',
                   (b'["' + KEY.encode() + b'"]')]
        for reply in replies:
            result = check_databento_connection(ENV, transport=Transport([reply]))
            self.assertIn(result["status"], ("redirect_rejected", "invalid_response"))
            self.assertNotIn(KEY, json.dumps(result))

    def test_production_incremental_reader_is_preferred_and_assembles_small_chunks(self):
        response = IncrementalResponse(b'["XNAS.ITCH"]', DATASET_LIST_URL)
        transport = Transport([lambda _: response])
        result = check_databento_connection(ENV, transport=transport)
        self.assertEqual(result["status"], "connected")
        self.assertGreater(response.chunk_reads, 1)
        self.assertEqual(len(transport.requests), 1)

    def test_elapsed_deadline_aborts_incremental_read_without_retry(self):
        response = IncrementalResponse(b'["XNAS.ITCH"]', DATASET_LIST_URL)
        transport = Transport([lambda _: response])
        # The first chunk arrived after the total ten-second deadline. The
        # reader must check elapsed time after the read, including partial data.
        with patch("dwight.databento_data.time.monotonic", side_effect=[0, 0, 11]):
            result = check_databento_connection(ENV, transport=transport)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(response.chunk_reads, 1)
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn(KEY, json.dumps(result))

    def test_estimate_only_requests_free_metadata(self):
        transport = Transport([b"0.125", b"10"])
        plan = estimate_databento_history(START, END, ENV, dataset="XNAS.ITCH", transport=transport)
        self.assertEqual(plan["estimated_cost_usd"], "0.125")
        self.assertEqual(plan["estimated_records"], 10)
        self.assertFalse(plan["retrieval_started"])
        self.assertTrue(plan["ten_minute_aligned"])
        self.assertEqual(plan["cost_control"], "estimated_cost_gate_not_provider_billing_cap")
        self.assertEqual(len(transport.requests), 2)
        for request, _ in transport.requests:
            self.assertIn(request.full_url.split("?")[0], (COST_URL, COUNT_URL))
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query["symbols"], ["QQQ"])
            self.assertEqual(query["schema"], ["ohlcv-1m"])
            self.assertEqual(query["dataset"], ["XNAS.ITCH"])
            self.assertNotIn("limit", query)
            self.assertNotIn(KEY, request.full_url)

    def test_explicit_budget_and_config_bounds_reject_before_requests(self):
        transport = Transport([])
        for budget in (None, True, -1, float("inf"), "NaN", "", {}, "1e99999"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                fetch_databento_history(START, END, ENV, dataset="XNAS.ITCH",
                                        max_cost_usd=budget, transport=transport)
        for kwargs in ({"max_bytes": 1023}, {"max_records": True}, {"max_records": 0},
                       {"timeout": 0}, {"timeout": float("nan")}, {"timeout": 61}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.fetch(transport, **kwargs)
        with self.assertRaises(TypeError):
            fetch_databento_history(START, END, ENV, dataset="XNAS.ITCH", transport=transport)
        self.assertEqual(transport.requests, [])

    def test_invalid_dataset_dates_and_naive_times_reject_before_requests(self):
        transport = Transport([])
        for start, end, dataset in ((START, END, "https://example.com"), (START, END, ""),
                                    (END, START, "XNAS.ITCH"), (START.replace(tzinfo=None), END, "XNAS.ITCH"),
                                    (START, END + timedelta(seconds=1), "XNAS.ITCH"),
                                    (START, END + timedelta(days=3661), "XNAS.ITCH")):
            with self.subTest(dataset=dataset), self.assertRaises(ValueError):
                fetch_databento_history(start, end, ENV, dataset=dataset, max_cost_usd=1, transport=transport)
        self.assertEqual(transport.requests, [])

    def test_over_budget_zero_records_and_record_bound_never_stream(self):
        for replies, kwargs in (([b"1.01", b"10"], {}), ([b"0", b"0"], {}),
                                 ([b"0", b"11"], {"max_records": 10})):
            transport = Transport(replies)
            with self.assertRaises(ValueError):
                self.fetch(transport, **kwargs)
            self.assertEqual(len(transport.requests), 2)
            self.assertFalse(any(request.get_method() == "POST" for request, _ in transport.requests))

    def test_malformed_cost_count_and_nonfinite_metadata_never_stream(self):
        for replies in ([b"NaN"], [b"-1"], [b'"Infinity"'], [b"true"], [b"{}"],
                        [b"0", b"10.0"], [b"0", b"-1"], [b"0", b"true"]):
            transport = Transport(replies)
            with self.assertRaises(DatabentoDataError):
                self.fetch(transport)
            self.assertFalse(any(request.get_method() == "POST" for request, _ in transport.requests))

    def test_history_post_preserves_raw_and_exact_source(self):
        raw = encoded([bar(offset) for offset in range(10)])
        transport = Transport([b"0.25", b"10", raw])
        result = self.fetch(transport)
        self.assertEqual(result["raw_pages"], [raw])
        self.assertEqual(len(result["records"]), 10)
        self.assertEqual(result["records"][0], {"t": START.isoformat(), "o": 100, "h": 102,
                                                "l": 99, "c": 101, "v": 200})
        provenance = result["provenance"]
        self.assertEqual(provenance["source"], "databento")
        self.assertEqual(provenance["feed"], "databento_XNAS.ITCH_ohlcv_1m")
        self.assertEqual(provenance["dataset"], "XNAS.ITCH")
        self.assertEqual(provenance["publisher_ids"], [2])
        self.assertEqual(provenance["instrument_ids"], [123])
        self.assertEqual(provenance["adjustment"], "raw")
        self.assertNotIn(KEY, json.dumps(provenance))
        request, _ = transport.requests[-1]
        self.assertEqual(request.full_url, RANGE_URL)
        self.assertEqual(request.get_method(), "POST")
        form = parse_qs(request.data.decode())
        for name, value in (("symbols", "QQQ"), ("encoding", "json"), ("compression", "none"),
                            ("pretty_px", "true"), ("pretty_ts", "true"), ("map_symbols", "true"),
                            ("stype_in", "raw_symbol"), ("stype_out", "instrument_id")):
            self.assertEqual(form[name], [value])
        self.assertEqual(form["limit"], ["1000001"])
        self.assertNotIn(KEY.encode(), request.data)

    def test_no_trade_gaps_remain_absent_for_strict_session_validation(self):
        raw = encoded([bar(0), bar(2)])
        result = self.fetch(Transport([b"0", b"2", raw]))
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["records"][1]["t"], (START + timedelta(minutes=2)).isoformat())

    def test_pretty_nanosecond_zero_timestamp_is_supported(self):
        row = bar()
        row["hd"]["ts_event"] = "2025-11-28T14:30:00.000000000Z"
        result = self.fetch(Transport([b"0", b"1", encoded([row])]))
        self.assertEqual(result["records"][0]["t"], START.isoformat())

    def test_wrong_symbol_schema_timestamp_prices_volume_and_extra_fields_are_rejected(self):
        cases = []
        for field, value in (("symbol", "SPY"), ("open", True), ("high", "99"),
                             ("low", "0"), ("close", "NaN"), ("volume", "1.5"),
                             ("volume", -1), ("volume", "9007199254740992"), ("extra", KEY)):
            cases.append(bar(**{field: value}))
        for field, value in (("rtype", 32), ("publisher_id", False), ("instrument_id", 0),
                             ("ts_event", "2025-11-28T14:30:00.000000001Z"),
                             ("ts_event", END.isoformat()), ("ts_event", "2025-11-28T14:30:00")):
            row = bar()
            row["hd"][field] = value
            cases.append(row)
        for row in cases:
            with self.subTest(row=row), self.assertRaises(DatabentoDataError) as caught:
                self.fetch(Transport([b"0", b"1", encoded([row])]))
            self.assertNotIn(KEY, str(caught.exception))

    def test_duplicate_timestamps_including_different_venues_are_rejected(self):
        for raw in (encoded([bar(), bar()]),
                    encoded([bar(), dict(bar(), hd={**bar()["hd"], "publisher_id": 3})])):
            with self.assertRaises(DatabentoDataError):
                self.fetch(Transport([b"0", b"2", raw]))

    def test_truncation_count_mismatch_and_byte_limit_fail_without_retry(self):
        for raw, count, kwargs in ((encoded([bar()]), b"2", {}),
                                   (encoded([bar(i) for i in range(10)]), b"10", {"max_bytes": 1024}),
                                   (b"", b"1", {}), (encoded([bar()]) + b"\n", b"1", {}),
                                   (b'{"symbol":"QQQ","symbol":"SPY"}\n', b"1", {})):
            transport = Transport([b"0", count, raw])
            with self.assertRaises(DatabentoDataError):
                self.fetch(transport, **kwargs)
            self.assertEqual(len(transport.requests), 3)

    def test_reflected_auth_generic_failures_and_failed_stream_never_expose_or_retry(self):
        auth = "Basic " + base64.b64encode((KEY + ":").encode()).decode()
        for raw in (KEY.encode(), auth.encode(), auth[6:].encode()):
            transport = Transport([b"0", b"1", raw])
            with self.assertRaises(DatabentoDataError) as caught:
                self.fetch(transport)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn(auth, str(caught.exception))
            self.assertEqual(len(transport.requests), 3)
        for exception in (RuntimeError(KEY), TimeoutError(KEY)):
            transport = Transport([b"0", b"1", exception])
            with self.assertRaises(DatabentoDataError) as caught:
                self.fetch(transport)
            self.assertEqual(caught.exception.status, "unavailable")
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(len(transport.requests), 3)


if __name__ == "__main__":
    unittest.main()
