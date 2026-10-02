import csv
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from dwight.data import (Session, download_alpaca_dataset, exchange_sessions, normalize_minutes,
                         session_close_for_bar, sha256_file, fetch_alpaca_bars)

UTC = timezone.utc


def session(day="2025-11-28", hours=3.5):
    opened = datetime.fromisoformat(day + "T14:30:00+00:00")
    return Session(day, opened, opened + timedelta(hours=hours))


def records(s):
    return [{"t": (s.open + timedelta(minutes=i)).isoformat(), "o": 100 + i / 100,
             "h": 102 + i / 100, "l": 99 + i / 100, "c": 101 + i / 100, "v": 10 + i}
            for i in range(int((s.close - s.open).total_seconds() // 60))]


class DataTests(unittest.TestCase):
    def test_early_close_is_complete_and_aggregates_exactly(self):
        s = session()
        one, five = normalize_minutes(reversed(records(s)), [s])
        self.assertEqual(len(one), 210)
        self.assertEqual(len(five), 42)
        self.assertEqual(five[-1].timestamp, s.close - timedelta(minutes=5))
        self.assertEqual(five[0].open, 100)
        self.assertAlmostEqual(five[0].close, 101.04)
        self.assertEqual(five[0].volume, 60)
        self.assertEqual(session_close_for_bar([s], five[-1].timestamp), s.close)

    def test_gap_duplicate_nonfinite_and_misaligned_are_rejected(self):
        s = session()
        for kind in ("gap", "duplicate", "nonfinite", "offminute", "boolean"):
            rows = records(s)
            if kind == "gap":
                rows.pop(3)
            elif kind == "duplicate":
                rows.append(dict(rows[3]))
            elif kind == "nonfinite":
                rows[3]["v"] = float("nan")
            elif kind == "offminute":
                rows[3]["t"] = (s.open + timedelta(seconds=1)).isoformat()
            else:
                rows[3]["o"] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                normalize_minutes(rows, [s])

    def test_shadow_partial_session_excludes_forming_candle(self):
        s = session()
        one, five = normalize_minutes(records(s)[:8], [s], require_complete_session=False,
                                      as_of=s.open + timedelta(minutes=7, seconds=59))
        self.assertEqual(len(one), 5)
        self.assertEqual(len(five), 1)
        self.assertEqual(five[0].timestamp, s.open)
        with self.assertRaisesRegex(ValueError, "Missing"):
            normalize_minutes(records(s)[1:8], [s], require_complete_session=False,
                              as_of=s.open + timedelta(minutes=7))
        with self.assertRaisesRegex(ValueError, "as_of"):
            normalize_minutes(records(s)[:8], [s], require_complete_session=False)

    def test_current_reader_supports_partial_session_without_orders(self):
        s = session()
        payload = json.dumps({"bars": {"QQQ": records(s)[:6]}}).encode()
        with patch("dwight.data._request_page", return_value=payload) as request:
            result = fetch_alpaca_bars(s.open, s.open + timedelta(minutes=6), ["QQQ"], "iex",
                                      {"APCA_API_KEY_ID": "fake", "APCA_API_SECRET_KEY": "secret"})
            self.assertEqual(len(result["QQQ"]), 6)
            self.assertTrue(request.call_args.args[0].startswith("https://data.alpaca.markets/v2/stocks/bars?"))

    def test_extended_hours_do_not_enter_vwap_dataset(self):
        s = session()
        rows = records(s)
        extra = dict(rows[0], t=(s.open - timedelta(minutes=1)).isoformat(), v=9999999)
        one, five = normalize_minutes([extra, *rows], [s])
        self.assertEqual(len(one), 210)
        self.assertEqual(five[0].volume, 60)

    @unittest.skipUnless(importlib.util.find_spec("exchange_calendars"), "data extra not installed")
    def test_real_calendar_holiday_early_close_and_dst(self):
        sessions = exchange_sessions("2025-11-27", "2025-11-28")
        self.assertEqual(len(sessions), 1)  # Thanksgiving closed, Friday closes 13:00 ET.
        self.assertEqual(sessions[0].close.hour, 18)
        sessions = exchange_sessions("2025-03-07", "2025-03-10")
        self.assertEqual([s.open.hour for s in sessions], [14, 13])

    def test_download_paginates_preserves_raw_and_records_hashes(self):
        s = session()
        rows = records(s)
        payloads = [json.dumps({"bars": {"QQQ": rows[:50]}, "next_page_token": "next"}).encode(),
                    json.dumps({"bars": {"QQQ": rows[50:]}, "next_page_token": None}).encode()]
        with tempfile.TemporaryDirectory() as temp, patch("dwight.data.exchange_sessions", return_value=[s]), \
                patch("dwight.data._request_page", side_effect=payloads) as request:
            result = download_alpaca_dataset(temp, s.date, s.date, environ={
                "APCA_API_KEY_ID": "fake-key", "APCA_API_SECRET_KEY": "fake-secret"},
                now=s.close + timedelta(hours=1))
            directory = Path(result["directory"])
            self.assertEqual((directory / "raw/page-000001.json").read_bytes(), payloads[0])
            self.assertEqual(result["counts"]["QQQ"]["5Min"], 42)
            self.assertEqual(result['symbols'], ['QQQ'])
            query = parse_qs(urlparse(request.call_args_list[1].args[0]).query)
            self.assertEqual(query["page_token"], ["next"])
            self.assertEqual(query["feed"], ["sip"])
            self.assertEqual(query["timeframe"], ["1Min"])
            self.assertEqual(query['symbols'], ['QQQ'])
            for item in result["files"] + result["raw_pages"]:
                self.assertEqual(sha256_file(directory / item["path"]), item["sha256"])
            manifest = (directory / "manifest.json").read_text()
            self.assertNotIn("fake-key", manifest)
            self.assertNotIn("fake-secret", manifest)
            with (directory / result["bars"]["QQQ"]["5Min"]).open() as stream:
                last = list(csv.DictReader(stream))[-1]
            self.assertEqual(last["session_close"], s.close.isoformat())

    def test_incomplete_response_has_no_success_manifest(self):
        s = session()
        payload = json.dumps({"bars": {"QQQ": records(s)[:-1]}}).encode()
        with tempfile.TemporaryDirectory() as temp, patch("dwight.data.exchange_sessions", return_value=[s]), \
                patch("dwight.data._request_page", return_value=payload):
            with self.assertRaisesRegex(ValueError, "Missing regular-session"):
                download_alpaca_dataset(temp, s.date, s.date, symbols=["QQQ"], environ={
                    "APCA_API_KEY_ID": "fake-key", "APCA_API_SECRET_KEY": "fake-secret"},
                    now=s.close + timedelta(hours=1))
            self.assertFalse(list(Path(temp).glob("*/manifest.json")))
            self.assertEqual(len(list(Path(temp).glob("*/failed.json"))), 1)
            self.assertEqual(len(list(Path(temp).glob("*/raw/*.json"))), 1)

    def test_repeat_token_and_incomplete_session_fail_closed(self):
        s = session()
        payload = json.dumps({"bars": {"QQQ": []}, "next_page_token": "same"}).encode()
        with tempfile.TemporaryDirectory() as temp, patch("dwight.data.exchange_sessions", return_value=[s]), \
                patch("dwight.data._request_page", return_value=payload) as request:
            kwargs = {"symbols": ["QQQ"], "environ": {"APCA_API_KEY_ID": "fake", "APCA_API_SECRET_KEY": "secret"}}
            with self.assertRaisesRegex(ValueError, "not fully completed"):
                download_alpaca_dataset(temp, s.date, s.date, now=s.open, **kwargs)
            request.assert_not_called()
            with self.assertRaisesRegex(ValueError, "pagination token"):
                download_alpaca_dataset(temp, s.date, s.date, now=s.close + timedelta(hours=1), **kwargs)
            self.assertEqual(request.call_count, 2)

    def test_non_qqq_data_is_rejected_before_requests(self):
        s = session()
        with tempfile.TemporaryDirectory() as temp, patch("dwight.data._request_page") as request:
            for symbols in [("SPY",), ("QQQ", "SPY")]:
                with self.assertRaisesRegex(ValueError, "QQQ"):
                    fetch_alpaca_bars(s.open, s.close, symbols)
                with self.assertRaisesRegex(ValueError, "QQQ"):
                    download_alpaca_dataset(temp, s.date, s.date, symbols=symbols)
            request.assert_not_called()

    def test_credentials_aliases_work_and_canonical_values_take_precedence(self):
        s = session()
        payload = json.dumps({"bars": {"QQQ": []}}).encode()
        with patch("dwight.data._request_page", return_value=payload) as request:
            fetch_alpaca_bars(s.open, s.close, ["QQQ"], environ={
                "ALPACA_API_KEY": "alias-key", "ALPACA_SECRET_KEY": "alias-secret"})
            self.assertEqual(request.call_args.args[1]["APCA-API-KEY-ID"], "alias-key")
            fetch_alpaca_bars(s.open, s.close, ["QQQ"], environ={
                "APCA_API_KEY_ID": "canonical-key", "APCA_API_SECRET_KEY": "canonical-secret",
                "ALPACA_API_KEY": "alias-key", "ALPACA_SECRET_KEY": "alias-secret"})
            self.assertEqual(request.call_args.args[1]["APCA-API-KEY-ID"], "canonical-key")
            self.assertEqual(request.call_args.args[1]["APCA-API-SECRET-KEY"], "canonical-secret")

    def test_missing_credentials_do_not_issue_requests(self):
        with patch("dwight.data._request_page") as request:
            with self.assertRaisesRegex(RuntimeError, "privately"):
                download_alpaca_dataset("unused", "2025-01-01", "2025-01-02", environ={})
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
