"""Private console boundary: local scope, consent and safe worker evidence."""
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from dwight.paper_console import (ConsoleError, MAX_BODY_BYTES, make_console_server,
                                  read_status, sanitize_status, validate_command)


NOW = datetime(2026, 10, 5, 14, 35, 3, tzinfo=timezone.utc)
# Fixed fixture, never a deployed console capability.
TEST_CAPABILITY = "private-console-test-fixture-00000000"


def authorize(**changes):
    return {"action": "authorize", "payload": {
        "account_id": "paper-account-fixture", "allocation_usd": "5000",
        "max_order_notional_usd": "1000", "daily_loss_limit_usd": "100",
        "max_stop_risk_usd": "20", "expires_at": "2026-10-05T21:00:00Z",
        "consent": True, **changes}}


def snapshot(**changes):
    return {
        "updated_at": NOW.isoformat(), "state": "paused",
        "account": {"provider": "alpaca", "mode": "paper", "id": "paper-account-fixture",
                    "api_key": "must-not-leak-account-secret"},
        "strategy": {"id": "qqq-vwap", "revision": "a" * 64, "symbol": "QQQ",
                     "timeframe": "5m", "feed": "sip"},
        "readiness": {"ready": True, "failures": []},
        "authorization": {"id": "grant-fixture", "state": "active", "allocation_usd": "5000",
                          "expires_at": "2026-10-05T21:00:00Z", "secret": "must-not-leak-grant-secret"},
        "risk_policy": {"max_order_notional": 1000, "max_daily_loss": 100, "max_stop_risk": 20,
                        "expected_feed": "sip", "token": "must-not-leak-policy-secret"},
        "positions": [{"symbol": "QQQ", "qty": "2", "side": "long", "account_export": "must-not-leak-export"}],
        "orders": [{"symbol": "QQQ", "qty": "2", "status": "new", "raw_response": "must-not-leak-response"}],
        "health": {"worker": "healthy", "data": {"state": "fresh", "password": "must-not-leak-password"}},
        "equity_usd": "25000.00", "pnl_usd": 123456,
        "credentials": {"secret": "must-not-leak-top-level"}, **changes,
    }


class ConsoleProtocolTests(unittest.TestCase):
    def test_explicit_complete_scope_and_fixed_actions(self):
        requested = authorize()
        action, normalized = validate_command(requested)
        self.assertEqual(action, "authorize")
        self.assertEqual(normalized["allocation_usd"], 5000.0)
        self.assertIsInstance(normalized["allocation_usd"], float)
        self.assertEqual(requested["payload"]["allocation_usd"], "5000")
        self.assertEqual(validate_command({"action": "start", "payload": {"authorization_id": "grant-fixture"}}),
                         ("start", {"authorization_id": "grant-fixture"}))
        self.assertEqual(validate_command({"action": "pause", "payload": {}}), ("pause", {}))
        for change in ({"consent": False}, {"consent": 1}, {"account_id": ""},
                       {"account_id": "paper\naccount"}, {"expires_at": "2026-10-05T21:00:00"},
                       {"allocation_usd": float("inf")}, {"allocation_usd": True},
                       {"allocation_usd": 10},
                       {"max_stop_risk_usd": "-20"}, {"endpoint": "https://api.alpaca.markets"}):
            with self.subTest(change=change), self.assertRaises(ConsoleError):
                validate_command(authorize(**change))
        for value in ({"action": "close", "payload": {}}, {"action": "start", "payload": {}},
                      {"action": "pause", "payload": {"close_positions": True}},
                      {"action": "start", "payload": {"authorization_id": "grant-fixture", "force": True}},
                      [], {"action": "pause", "payload": {}, "execution_enabled": True}):
            with self.subTest(value=value), self.assertRaises(ConsoleError):
                validate_command(value)

    def test_worker_status_is_allowlisted_and_does_not_invent_profit(self):
        value = sanitize_status(snapshot(), now=NOW)
        self.assertTrue(value["readiness"]["ready"])
        self.assertFalse(value["stale"])
        self.assertEqual(value["positions"], [{"symbol": "QQQ", "qty": "2", "side": "long"}])
        self.assertEqual(value["equity_usd"], "25000.00")
        self.assertIsNone(value["pnl_usd"])
        self.assertNotIn("must-not-leak", json.dumps(value))
        self.assertIsNone(sanitize_status(snapshot(equity_usd=10 ** 400), now=NOW)["equity_usd"])
        for stamp in (None, "invalid", (NOW - timedelta(seconds=31)).isoformat(),
                      (NOW + timedelta(seconds=1)).isoformat()):
            with self.subTest(stamp=stamp):
                stale = sanitize_status(snapshot(updated_at=stamp), now=NOW)
                self.assertTrue(stale["stale"])
                self.assertFalse(stale["readiness"]["ready"])

    def test_missing_malformed_or_symlink_status_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            status = root / "status.json"
            self.assertFalse(read_status(status)["readiness"]["ready"])
            for content in (b"{", b"[]", b"{\"state\":\"running\",\"state\":\"paused\"}", b"x" * 262145):
                status.write_bytes(content)
                self.assertTrue(read_status(status)["stale"])
            status.write_text(json.dumps(snapshot()))
            alias = root / "alias.json"
            alias.symlink_to(status)
            self.assertTrue(read_status(alias)["stale"])

    def test_console_rejects_public_binding_and_bad_capabilities(self):
        for host in ("0.0.0.0", "::", "192.0.2.1", "localhost", "example.com"):
            with self.subTest(host=host), self.assertRaises(ConsoleError):
                make_console_server("status.json", lambda *_: None, host=host)
        for value in ("short", "x" * 129, "x" * 32 + "/", "é" * 32):
            with self.subTest(value=value), self.assertRaises(ConsoleError):
                make_console_server("status.json", lambda *_: None, capability=value)


class ConsoleHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.status_path = Path(self.temp.name).resolve() / "status.json"
        current = datetime.now(timezone.utc)
        self.status_path.write_text(json.dumps(snapshot(updated_at=current.isoformat())))
        self.commands = []
        self.backend = None

        def handler(action, payload):
            if self.backend is not None:
                return self.backend(action, payload)
            self.commands.append((action, payload))
            return {"command_id": f"command-{len(self.commands)}", "status": "queued",
                    "raw_secret": "must-not-leak-receipt"}

        self.server = make_console_server(self.status_path, handler, capability=TEST_CAPABILITY)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path="/api/status", *, body=None, headers=None, decode=True):
        options = {"X-Dwight-Session": TEST_CAPABILITY}
        if method == "POST":
            options.update({"Origin": self.server.expected_origin, "Content-Type": "application/json"})
        if headers is not None:
            options.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=options)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if decode else raw.decode()
        finally:
            connection.close()

    def test_bootstrap_fragment_is_not_html_or_a_query_capability(self):
        self.assertEqual(self.server.console_url,
                         f"{self.server.expected_origin}/#session={TEST_CAPABILITY}")
        self.assertEqual(self.server.startup_url, self.server.console_url)
        code, headers, page = self.request(path="/", headers={"X-Dwight-Session": ""}, decode=False)
        self.assertEqual(code, 200)
        self.assertNotIn(TEST_CAPABILITY, page)
        self.assertIn("history.replaceState", page)
        self.assertIn("textContent", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        code, _, _ = self.request(path="/api/status?session=" + TEST_CAPABILITY)
        self.assertEqual(code, 404)

    def test_account_evidence_requires_session_and_expected_host(self):
        for headers, expected in (({"X-Dwight-Session": ""}, 401),
                                  ({"X-Dwight-Session": "wrong-capability-0000000000000000000"}, 401),
                                  ({"Host": "attacker.example"}, 403)):
            with self.subTest(headers=headers):
                code, _, value = self.request(headers=headers)
                self.assertEqual(code, expected)
                self.assertNotIn("paper-account-fixture", json.dumps(value))
        code, headers, value = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("must-not-leak", json.dumps(value))

    def test_commands_require_same_origin_capability_and_explicit_consent(self):
        body = json.dumps(authorize())
        for headers, expected in (({"Origin": "https://attacker.example"}, 403),
                                  ({"Origin": "null"}, 403),
                                  ({"Host": "attacker.example"}, 403),
                                  ({"X-Dwight-Session": ""}, 401)):
            with self.subTest(headers=headers):
                code, _, _ = self.request("POST", "/api/commands", body=body, headers=headers)
                self.assertEqual(code, expected)
        code, _, _ = self.request("POST", "/api/commands", body=json.dumps(authorize(consent=False)))
        self.assertEqual(code, 400)
        self.assertEqual(self.commands, [])
        code, _, receipt = self.request("POST", "/api/commands", body=body)
        self.assertEqual(code, 202)
        self.assertEqual(receipt, {"command_id": "command-1", "status": "queued"})
        self.assertEqual(self.commands, [validate_command(authorize())])
        # Queueing does not rewrite worker readiness, authorization or state.
        self.assertEqual(json.loads(self.status_path.read_text())["state"], "paused")

    def test_real_control_ledger_accepts_normalized_http_authorization(self):
        from dwight.paper_worker import ControlLedger
        path = Path(self.temp.name).resolve() / "controls.sqlite3"

        def enqueue(action, payload):
            with ControlLedger(path) as ledger:
                return ledger.enqueue(action, payload, now=NOW)

        self.backend = enqueue
        code, _, receipt = self.request("POST", "/api/commands", body=json.dumps(authorize()))
        self.assertEqual(code, 202)
        self.assertEqual(receipt["status"], "queued")
        with ControlLedger(path) as ledger:
            row = ledger.db.execute("SELECT action,payload,state FROM commands WHERE id=?",
                                    (receipt["command_id"],)).fetchone()
        self.assertEqual(row["action"], "authorize")
        self.assertEqual(row["state"], "queued")
        self.assertIsInstance(json.loads(row["payload"])["allocation_usd"], float)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_worker_rejection_does_not_expose_provider_details(self):
        def reject(*_):
            raise ValueError("private fake credential must-not-leak-provider-detail")

        self.backend = reject
        code, _, value = self.request("POST", "/api/commands", body=json.dumps(authorize()))
        self.assertEqual(code, 409)
        self.assertNotIn("must-not-leak", json.dumps(value))

    def test_malformed_oversized_or_extra_command_fields_never_reach_worker(self):
        invalid = ("{", '[]', '{"action":"pause","action":"start","payload":{}}',
                   '{"action":"pause","payload":{},"secret":"hidden"}',
                   '{"action":"authorize","payload":{"allocation_usd":NaN}}',
                   ' ' * (MAX_BODY_BYTES + 1))
        for body in invalid:
            with self.subTest(body=body[:40]):
                code, _, _ = self.request("POST", "/api/commands", body=body)
                self.assertIn(code, {400, 413})
        code, _, _ = self.request("POST", "/api/commands", body='{"action":"pause","payload":{}}',
                                  headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 400)
        self.assertEqual(self.commands, [])

    def test_request_logs_do_not_reveal_capability_or_bodies(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.request(path="/" + TEST_CAPABILITY)
            self.request("POST", "/api/commands", body=json.dumps(authorize()))
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
