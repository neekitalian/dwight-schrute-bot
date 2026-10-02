"""Observation intake boundary: persistence, hostile input and HTTP isolation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from dwight.manual import ManualPaperJournal
from dwight.tradingview import (CAPABILITY_ENV, MAX_BODY_BYTES, DuplicateConflict,
                                StaleEvent, TradingViewError, TradingViewInbox,
                                capability_from_env, decode_event, make_server)


NOW = datetime(2026, 10, 5, 14, 35, 3, tzinfo=timezone.utc)
# Fixed test fixture, never a deployment credential.
TEST_CAPABILITY = "test-fixture-not-for-deployment-000000"


def event(**changes):
    return {"schema_version": 1, "event_id": "QQQ-5-20261005T143500-v1",
            "kind": "bar_observation", "symbol": "QQQ", "exchange": "NASDAQ",
            "timeframe": "5", "bar_open_at": "2026-10-05T14:30:00Z",
            "bar_close_at": "2026-10-05T14:35:00Z", "sent_at": "2026-10-05T14:35:01Z",
            "bar_closed": True, "open": 100, "high": 102, "low": 99,
            "close": 101, "volume": 12345, "source": "dwight_qqq_observer",
            "strategy_version": "observer-v1", **changes}


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS /var is an alias; inbox paths intentionally reject symlinks.
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "private" / "inbox.sqlite3"
        self.inbox = TradingViewInbox(self.path)

    def test_persistent_idempotency_does_not_refresh_received_time_or_approve(self):
        first = self.inbox.accept(event(), now=NOW)
        self.assertTrue(first["inserted"])
        reopened = TradingViewInbox(self.path)
        # An exact late retry acknowledges stored evidence; it is no new signal.
        result = reopened.accept(event(open="100.00"), now=NOW + timedelta(days=1))
        self.assertTrue(result["duplicate"])
        rows = reopened.list_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["received_at"], NOW.isoformat(timespec="microseconds"))
        self.assertEqual(rows[0]["status"], "unreviewed")
        self.assertFalse(rows[0]["submits_orders"])
        self.assertFalse(rows[0]["creates_fills"])
        self.assertFalse(rows[0]["source_verified"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_identity_and_same_bar_conflicts_never_overwrite(self):
        self.inbox.accept(event(), now=NOW)
        for change in ({"close": 102}, {"event_id": "different-id"},
                       {"sent_at": "2026-10-05T14:35:02Z"}):
            with self.subTest(change=change), self.assertRaises(DuplicateConflict):
                self.inbox.accept(event(**change), now=NOW)
        self.assertEqual(self.inbox.list_events()[0]["event"]["close"], "101")

    def test_schema_numbers_closed_bar_and_aware_time_are_enforced(self):
        changes = [{"symbol": "SPY"}, {"exchange": "AMEX"}, {"timeframe": 5},
                   {"timeframe": "1"}, {"kind": "buy"}, {"schema_version": True},
                   {"bar_closed": 1}, {"open": float("nan")}, {"volume": "Infinity"},
                   {"close": True}, {"low": "0"}, {"volume": -1}, {"volume": "1e9000"},
                   {"close": "0.000000001"}, {"high": 100}, {"close": 103},
                   {"bar_open_at": "2026-10-05T14:30:00"},
                   {"bar_close_at": "2026-10-05T14:34:59Z"},
                   {"sent_at": "2026-10-05T14:34:59Z"}, {"event_id": "bad\nidentifier"},
                   {"side": "buy"}, {"status": "confirmed"}, {"quantity": 10},
                   {"account": "tradingview_native_paper"}, {"fill_id": "fake"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(TradingViewError):
                self.inbox.accept(event(**change), now=NOW)
        self.assertEqual(self.inbox.list_events(), [])

    def test_freshness_uses_bar_close_not_claimed_send_time(self):
        with self.assertRaises(StaleEvent):
            self.inbox.accept(event(sent_at="2026-10-05T14:45:00Z"), now=NOW + timedelta(minutes=10))
        with self.assertRaises(StaleEvent):
            self.inbox.accept(event(), now=NOW - timedelta(seconds=4))
        with self.assertRaises(StaleEvent):
            self.inbox.accept(event(sent_at="2026-10-05T14:40:00Z"), now=NOW)
        self.assertEqual(self.inbox.list_events(), [])

    def test_strict_bounded_json(self):
        invalid = [b"[]", b"null", b"{", b"\xff", b"{\"open\":1,\"open\":2}",
                   b"{\"open\":NaN}", b"[" * 1200 + b"]" * 1200,
                   b" " * (MAX_BODY_BYTES + 1)]
        for body in invalid:
            with self.subTest(body=body[:30]), self.assertRaises(TradingViewError):
                decode_event(body)
        self.assertEqual(decode_event(json.dumps(event()).encode())["symbol"], "QQQ")

    def test_concurrent_retry_is_one_observation(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.inbox.accept(event(), now=NOW), range(4)))
        self.assertEqual(sum(row["inserted"] for row in results), 1)
        self.assertEqual(len(self.inbox.list_events()), 1)

    def test_observation_never_becomes_manual_proposal_or_fill(self):
        manual = ManualPaperJournal(self.root / "manual.sqlite3")
        with patch.object(ManualPaperJournal, "add_proposal", side_effect=AssertionError("no proposal integration")), patch.object(
                ManualPaperJournal, "import_fills", side_effect=AssertionError("no fill integration")):
            self.inbox.accept(event(), now=NOW)
        report = manual.report(now=NOW)
        self.assertEqual(report["fills"], [])
        self.assertEqual(manual.list_proposals(now=NOW), [])
        with sqlite3.connect(self.path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"inbox_metadata", "alert_observations"})
        with self.assertRaises(TradingViewError):
            TradingViewInbox(manual.path)

    def test_corruption_and_symlink_fail_closed(self):
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a database")
        with self.assertRaises(TradingViewError):
            TradingViewInbox(corrupt)
        link = self.root / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(TradingViewError):
            TradingViewInbox(link)

    def test_symlink_ancestors_rejected_before_directory_or_file_creation(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "alias"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(TradingViewError, "symbolic links"):
            TradingViewInbox(link / "new-directory" / "inbox.sqlite3")
        self.assertEqual(list(target.iterdir()), [])
        with self.assertRaisesRegex(TradingViewError, "parent traversal"):
            TradingViewInbox(self.root / "private" / ".." / "other.sqlite3")
        self.assertFalse((self.root / "other.sqlite3").exists())

    def test_unrelated_existing_files_rejected_without_permission_or_content_changes(self):
        unrelated = self.root / "unrelated.sqlite3"
        with sqlite3.connect(unrelated) as connection:
            connection.execute("CREATE TABLE notes (body TEXT)")
            connection.execute("INSERT INTO notes VALUES ('keep this record')")
        empty = self.root / "empty.sqlite3"
        empty.touch()
        corrupt = self.root / "corrupt-data.sqlite3"
        corrupt.write_bytes(b"not a database")
        wal = self.root / "unrelated-wal.sqlite3"
        connection = sqlite3.connect(wal)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE notes (body TEXT)")
        connection.commit()
        connection.close()
        for path in (unrelated, empty, corrupt, wal):
            path.chmod(0o644)
            before = path.read_bytes()
            directory_entries = set(self.root.iterdir())
            with self.subTest(path=path.name), self.assertRaises(TradingViewError):
                TradingViewInbox(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(set(self.root.iterdir()), directory_entries)

    def test_existing_inbox_permissions_are_hardened_only_after_validation(self):
        self.inbox.accept(event(), now=NOW)
        self.path.chmod(0o644)
        reopened = TradingViewInbox(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(reopened.list_events()), 1)

    def test_missing_capability_has_no_broker_key_fallback(self):
        with self.assertRaises(TradingViewError):
            capability_from_env({"APCA_API_SECRET_KEY": "A" * 40})
        for value in ("short", "x" * 129, "x" * 32 + "/", "é" * 32):
            with self.assertRaises(TradingViewError):
                capability_from_env({CAPABILITY_ENV: value})
        self.assertEqual(capability_from_env({CAPABILITY_ENV: TEST_CAPABILITY}), TEST_CAPABILITY)
        with self.assertRaises(TradingViewError):
            make_server(self.inbox, host="0.0.0.0", capability=TEST_CAPABILITY)


class HttpInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inbox = TradingViewInbox(Path(self.temp.name).resolve() / "inbox.sqlite3")
        self.server = make_server(self.inbox, port=0, capability=TEST_CAPABILITY, clock=lambda: NOW)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="POST", path=None, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path or "/alerts/" + TEST_CAPABILITY,
                               body=json.dumps(event()) if body is None else body,
                               headers={"Content-Type": "application/json"} if headers is None else headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_authentication_health_and_no_uri_logging(self):
        output = io.StringIO()
        with redirect_stderr(output):
            status, value = self.request(path="/alerts/not-the-capability")
            self.assertEqual(status, 401)
            self.assertEqual(self.inbox.list_events(), [])
            status, value = self.request("GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertFalse(value["submits_orders"])
            self.assertFalse(value["broker_connected"])
            self.assertEqual(self.request("GET")[0], 404)
            self.assertEqual(self.request(path="/alerts/" + TEST_CAPABILITY + "?extra=1")[0], 401)
            self.assertEqual(self.request(path="/alerts/%74" + TEST_CAPABILITY[1:])[0], 401)
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as stream:
                stream.sendall(b"POST /alerts/\xc3\xa9 HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
                response = http.client.HTTPResponse(stream)
                response.begin()
                self.assertEqual(response.status, 401)
                response.read()
            self.assertEqual(self.request()[0], 202)
        self.assertNotIn(TEST_CAPABILITY, output.getvalue())
        self.assertNotIn(TEST_CAPABILITY, json.dumps(value))

    def test_http_duplicate_conflict_invalid_and_stale_statuses(self):
        self.assertEqual(self.request()[0], 202)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request(body=json.dumps(event(close=102)))[0], 409)
        self.assertEqual(self.request(body=json.dumps(event(status="confirmed")))[0], 400)
        stale = event(event_id="old", bar_open_at="2026-10-05T14:20:00Z", bar_close_at="2026-10-05T14:25:00Z")
        self.assertEqual(self.request(body=json.dumps(stale))[0], 422)
        self.assertEqual(len(self.inbox.list_events()), 1)

    def test_http_body_and_media_bounds(self):
        self.assertEqual(self.request(body="x" * (MAX_BODY_BYTES + 1))[0], 413)
        self.assertEqual(self.request(body="{bad")[0], 400)
        self.assertEqual(self.request(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request(headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})[0], 415)
        self.assertEqual(self.request(headers={"Content-Type": "application/json", "Content-Length": "invalid"})[0], 411)
        self.assertEqual(self.inbox.list_events(), [])

    def test_incomplete_body_times_out_without_persisting(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.putrequest("POST", "/alerts/" + TEST_CAPABILITY)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "100")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 408)
            response.read()
        finally:
            connection.close()
        self.assertEqual(self.inbox.list_events(), [])
        self.assertEqual(self.request()[0], 202)


if __name__ == "__main__":
    unittest.main()
