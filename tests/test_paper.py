import copy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from dwight.paper import (
    AlpacaPaperClient, BrokerHTTPError, OrderProposal, PAPER_URL, PaperError,
    PaperExecutor, PreparationRequired, Quote, ReconciliationRequired, RiskPolicy,
    RiskRejected, _NoRedirect,
)
from dwight.authorization import AuthorizationError, AuthorizationLedger


NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
PROPOSAL = OrderProposal("vwap-QQQ-20261002T150000-model1", "QQQ", 2, 500.01, 499.00, 503.00,
                         "qqq-vwap", "a" * 64)
QUOTE = Quote("QQQ", 500.00, 500.01, NOW, "sip")


def grant_spec(policy=None, **updates):
    spec = {"authorization_id": "fixture-consent", "granted_by": "fixture-operator",
            "provider": "alpaca", "account_mode": "paper", "account_id": "paper-account",
            "strategy_id": "qqq-vwap", "strategy_revision": "a" * 64,
            "symbols": ["QQQ"], "side": "buy", "allocation_usd": "3000",
            "risk_policy": json.loads(json.dumps(asdict(policy or RiskPolicy(max_order_notional=1100)))),
            "expires_at": (NOW + timedelta(hours=12)).isoformat()}
    return {**spec, **updates}


class FakeBroker:
    """No sockets, keys, or real account use in these tests."""

    def __init__(self):
        self.base_url = PAPER_URL
        self.account = {"id": "paper-account", "status": "ACTIVE", "trading_blocked": False,
                        "account_blocked": False, "equity": "100000", "last_equity": "100000",
                        "buying_power": "100000"}
        self.orders = {}
        self.positions = []
        self.clock = {"timestamp": NOW.isoformat(), "is_open": True}
        self.submits = 0
        self.mode = "ok"
        self.on_submit = None

    def get_account(self): return copy.deepcopy(self.account)
    def get_positions(self): return copy.deepcopy(self.positions)
    def get_clock(self): return copy.deepcopy(self.clock)

    def get_orders(self):
        return [copy.deepcopy(x) for x in self.orders.values()
                if x["status"] not in {"filled", "canceled", "expired", "rejected"}]

    def get_order_by_client_id(self, cid):
        return copy.deepcopy(self.orders.get(cid))

    def submit_order(self, payload):
        self.submits += 1
        if self.on_submit:
            self.on_submit(payload)
        if self.mode == "timeout_absent":
            raise TimeoutError("not known")
        order = copy.deepcopy(payload)
        order.update(id="order-" + str(self.submits), status="new", filled_qty="0", legs=[])
        self.orders[payload["client_order_id"]] = order
        if self.mode == "timeout_accepted":
            raise TimeoutError("accepted but response lost")
        return copy.deepcopy(order)

    def cancel_order(self, oid):
        for order in self.orders.values():
            if order["id"] == oid:
                order["status"] = "canceled"

    def fill(self, cid, *, protected=True):
        order = self.orders[cid]
        order.update(status="filled", filled_qty=order["qty"])
        order["legs"] = [
            {"id": "stop-1", "symbol": "QQQ", "side": "sell", "type": "stop",
             "qty": order["qty"], "filled_qty": "0", "status": "new"},
            {"id": "target-1", "symbol": "QQQ", "side": "sell", "type": "limit",
             "qty": order["qty"], "filled_qty": "0", "status": "new"},
        ] if protected else []
        self.positions = [{"symbol": "QQQ", "qty": order["qty"], "market_value": "1000.02"}]


class TransportTests(unittest.TestCase):
    def test_live_and_similar_origins_are_rejected(self):
        for url in ("https://api.alpaca.markets", PAPER_URL + "/", PAPER_URL + ".example.com",
                    "http://paper-api.alpaca.markets"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                AlpacaPaperClient("key", "secret", base_url=url)

    def test_missing_credentials_report_preparation_without_values(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(PreparationRequired) as error:
            AlpacaPaperClient.from_env()
        self.assertIn("ALPACA_API_KEY", str(error.exception))

    def test_live_environment_override_is_rejected(self):
        with patch.dict("os.environ", {"ALPACA_API_KEY": "key", "ALPACA_SECRET_KEY": "secret",
                                      "APCA_API_BASE_URL": "https://api.alpaca.markets"}, clear=True):
            with self.assertRaises(PreparationRequired):
                AlpacaPaperClient.from_env()

    def test_apca_credentials_take_precedence_over_conflicting_aliases(self):
        with patch.dict("os.environ", {"APCA_API_KEY_ID": "canonical-key",
                                      "APCA_API_SECRET_KEY": "canonical-secret",
                                      "ALPACA_API_KEY": "alias-key",
                                      "ALPACA_SECRET_KEY": "alias-secret"}, clear=True):
            client = AlpacaPaperClient.from_env()
        self.assertEqual((client._key, client._secret), ("canonical-key", "canonical-secret"))

    def test_header_newlines_rejected_without_exposing_credentials(self):
        with self.assertRaises(PreparationRequired) as error:
            AlpacaPaperClient("secret-key\nvalue", "secret")
        self.assertNotIn("secret-key", str(error.exception))

    def test_fixed_url_and_auth_headers(self):
        client = AlpacaPaperClient("key", "secret")
        client._opener = MagicMock()
        client._opener.open.return_value.__enter__.return_value.read.return_value = b'{"id":"paper"}'
        self.assertEqual(client.get_account(), {"id": "paper"})
        req = client._opener.open.call_args.args[0]
        self.assertEqual(req.full_url, PAPER_URL + "/v2/account")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Apca-api-key-id"), "key")

    def test_http_error_does_not_echo_body_or_secret(self):
        client = AlpacaPaperClient("key", "secret")
        client._opener = Mock()
        client._opener.open.side_effect = HTTPError(PAPER_URL, 401, "secret", {}, None)
        with self.assertRaises(BrokerHTTPError) as error:
            client.get_account()
        self.assertNotIn("secret", str(error.exception))

    def test_arbitrary_paths_and_redirects_rejected(self):
        client = AlpacaPaperClient("key", "secret")
        for method, path in (("GET", "//api.alpaca.markets"), ("POST", "/v2/positions"),
                             ("DELETE", "/v2/orders/../account")):
            with self.assertRaises(ValueError):
                client._request(method, path)
        with self.assertRaises(PaperError):
            _NoRedirect().redirect_request(None, None, 302, "", {}, "https://api.alpaca.markets")

    def test_non_qqq_submission_is_rejected_before_network_request(self):
        client = AlpacaPaperClient("key", "secret")
        client._opener = Mock()
        for symbol in ("SPY", "AAPL", "qqq", None):
            with self.subTest(symbol=symbol), self.assertRaisesRegex(RiskRejected, "Only QQQ"):
                client.submit_order({"symbol": symbol})
        client._opener.open.assert_not_called()

    def test_bracket_lookup_fetches_nested_order_by_id(self):
        client = AlpacaPaperClient("key", "secret")
        client._request = Mock(side_effect=[{"id": "abc", "order_class": "bracket"}, {"legs": []}])
        self.assertEqual(client.get_order_by_client_id("dw-one"), {"legs": []})
        self.assertEqual(client._request.call_args_list[0].kwargs["params"], {"client_order_id": "dw-one"})
        self.assertEqual(client._request.call_args_list[1].kwargs["params"], {"nested": "true"})


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "paper.sqlite3"
        self.broker = FakeBroker()
        self.policy = RiskPolicy(max_order_notional=1100)
        self.executor = PaperExecutor(self.broker, self.path, self.policy)
        self.executor.authorizations.approve(grant_spec(self.policy), now=NOW)

    def tearDown(self):
        self.executor.close()
        self.temp.cleanup()

    def submit(self, proposal=PROPOSAL, quote=QUOTE):
        return self.executor.submit(proposal, quote, authorization_id="fixture-consent", now=NOW)

    def test_spy_proposal_is_rejected_before_broker_access_or_intent(self):
        self.broker.get_account = Mock(side_effect=AssertionError("No broker access expected"))
        with self.assertRaisesRegex(RiskRejected, "Only QQQ"):
            self.submit(replace(PROPOSAL, symbol="SPY"), replace(QUOTE, symbol="SPY"))
        self.broker.get_account.assert_not_called()
        self.assertEqual(self.broker.submits, 0)
        self.assertEqual(self.executor.db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)

    def test_intent_is_durable_before_submit_and_idempotent_across_restart(self):
        def check(payload):
            import sqlite3
            with sqlite3.connect(self.path) as observer:
                row = observer.execute("SELECT state FROM paper_intents").fetchone()
                self.assertEqual(row[0], "submitting")
        self.broker.on_submit = check
        order = self.submit()
        self.executor.close()
        self.executor = PaperExecutor(self.broker, self.path, self.policy)
        duplicate = self.submit()
        self.assertEqual(order["id"], duplicate["id"])
        self.assertEqual(self.broker.submits, 1)
        self.assertEqual(order["order_class"], "bracket")
        self.assertEqual(order["time_in_force"], "gtc")

    def test_submission_timeout_looks_up_existing_order_without_retry(self):
        self.broker.mode = "timeout_accepted"
        self.assertEqual(self.submit()["status"], "new")
        self.assertEqual(self.submit()["status"], "new")
        self.assertEqual(self.broker.submits, 1)

    def test_ambiguous_order_is_never_automatically_resubmitted(self):
        self.broker.mode = "timeout_absent"
        with self.assertRaises(ReconciliationRequired): self.submit()
        self.broker.mode = "ok"
        with self.assertRaises(ReconciliationRequired): self.submit()
        self.assertEqual(self.broker.submits, 1)
        self.assertEqual(self.executor.db.execute("SELECT state FROM paper_intents").fetchone()[0], "ambiguous")

    def test_unknown_position_or_order_blocks_entries(self):
        self.broker.positions = [{"symbol": "QQQ", "qty": "1", "market_value": "450"}]
        with self.assertRaises(ReconciliationRequired): self.submit()
        self.broker.positions = []
        self.broker.orders["other"] = {"id": "foreign", "status": "new", "symbol": "QQQ"}
        with self.assertRaises(ReconciliationRequired): self.submit()
        self.assertEqual(self.broker.submits, 0)

    def test_restart_reconciles_entry_and_exit_fills(self):
        order = self.submit()
        cid = order["client_order_id"]
        self.broker.fill(cid)
        self.assertEqual(len(self.executor.reconcile()["positions"]), 1)
        self.broker.orders[cid]["legs"][0].update(status="filled", filled_qty="2")
        self.broker.orders[cid]["legs"][1]["status"] = "canceled"
        self.broker.positions = []
        self.assertEqual(self.executor.reconcile()["positions"], [])

    def test_unprotected_fill_blocks(self):
        order = self.submit()
        self.broker.fill(order["client_order_id"], protected=False)
        with self.assertRaisesRegex(ReconciliationRequired, "protective stop"):
            self.executor.reconcile()

    def test_held_or_pending_cancel_stop_is_not_confirmed_protection(self):
        order = self.submit()
        cid = order["client_order_id"]
        self.broker.fill(cid)
        for status in ("held", "pending_cancel", "pending_new", None):
            self.broker.orders[cid]["legs"][0]["status"] = status
            with self.subTest(status=status), self.assertRaisesRegex(ReconciliationRequired, "protective stop"):
                self.executor.reconcile()

    def test_partial_fill_blocks_until_protection_can_be_confirmed(self):
        order = self.submit()
        self.broker.orders[order["client_order_id"]].update(status="partially_filled", filled_qty="1")
        self.broker.positions = [{"symbol": "QQQ", "qty": "1", "market_value": "500"}]
        with self.assertRaisesRegex(ReconciliationRequired, "Partially filled"):
            self.executor.reconcile()

    def test_different_account_cannot_reuse_ledger(self):
        self.executor.reconcile()
        self.broker.account["id"] = "different"
        with self.assertRaisesRegex(ReconciliationRequired, "different paper account"):
            self.executor.reconcile()

    def test_exclusive_ledger_lock(self):
        with self.assertRaisesRegex(ReconciliationRequired, "Another execution process"):
            PaperExecutor(self.broker, self.path)

    def test_same_signal_with_changed_fields_is_rejected(self):
        self.submit()
        with self.assertRaisesRegex(ReconciliationRequired, "reused"):
            self.submit(replace(PROPOSAL, qty=1))
        self.assertEqual(self.broker.submits, 1)

    def test_cancel_pending_does_not_cancel_protective_legs(self):
        order = self.submit()
        cid = order["client_order_id"]
        self.broker.fill(cid)
        with self.assertRaises(RiskRejected): self.executor.cancel_pending(cid)
        self.broker.orders[cid].update(status="new", filled_qty="0", legs=[])
        self.broker.positions = []
        self.executor.cancel_pending(cid)
        self.assertEqual(self.broker.orders[cid]["status"], "canceled")

    def test_risk_rejections_do_not_create_intents(self):
        cases = [
            (replace(PROPOSAL, symbol="AAPL"), QUOTE),
            (replace(PROPOSAL, qty=0), QUOTE),
            (replace(PROPOSAL, qty=-1), QUOTE),
            (replace(PROPOSAL, qty=1.5), QUOTE),
            (replace(PROPOSAL, qty=3), QUOTE),
            (replace(PROPOSAL, stop_price=510), QUOTE),
            (replace(PROPOSAL, limit_price=500.001), QUOTE),
            (replace(PROPOSAL, stop_price=400), QUOTE),
            (PROPOSAL, replace(QUOTE, timestamp=NOW-timedelta(seconds=11))),
            (PROPOSAL, replace(QUOTE, timestamp=NOW+timedelta(seconds=1))),
            (PROPOSAL, replace(QUOTE, feed="iex")),
            (PROPOSAL, replace(QUOTE, bid=499)),
            (PROPOSAL, replace(QUOTE, ask=float("nan"))),
        ]
        for proposal, quote in cases:
            with self.subTest(proposal=proposal, quote=quote), self.assertRaises(RiskRejected):
                self.submit(proposal, quote)
        self.assertEqual(self.broker.submits, 0)
        self.assertEqual(self.executor.db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)

    def test_daily_loss_buying_power_and_market_hours(self):
        for key, value in (("equity", "99899"), ("buying_power", "1"), ("trading_blocked", True),
                           ("status", "ACCOUNT_UPDATED")):
            original = self.broker.account[key]
            self.broker.account[key] = value
            with self.subTest(field=key), self.assertRaises(RiskRejected): self.submit()
            self.broker.account[key] = original
        self.broker.clock["is_open"] = False
        with self.assertRaises(RiskRejected): self.submit()

    def test_position_and_gross_limits(self):
        self.executor.policy = replace(self.policy, max_position_notional=900)
        with self.assertRaisesRegex(RiskRejected, "Position notional"): self.submit()
        self.executor.policy = replace(self.policy, max_gross_notional=900)
        with self.assertRaisesRegex(RiskRejected, "gross exposure"): self.submit()

    def test_policy_rejects_nan_or_expanded_symbols(self):
        self.assertEqual(RiskPolicy().allowed_symbols, ("QQQ",))
        for symbols in (("SPY",), ("QQQ", "SPY"), ("AAPL",), ()):
            with self.subTest(symbols=symbols), self.assertRaisesRegex(ValueError, "only QQQ"):
                RiskPolicy(allowed_symbols=symbols)
        with self.assertRaises(RiskRejected): RiskPolicy(max_order_notional=float("nan"))


class AuthorizationTests(unittest.TestCase):
    setUp = ExecutionTests.setUp
    tearDown = ExecutionTests.tearDown
    submit = ExecutionTests.submit

    def test_missing_authorization_blocks_before_broker_access(self):
        self.broker.get_account = Mock(side_effect=AssertionError("No broker access expected"))
        for reference in (None, "missing-consent"):
            with self.assertRaises(RiskRejected):
                self.executor.submit(PROPOSAL, QUOTE, authorization_id=reference, now=NOW)
        self.broker.get_account.assert_not_called()
        self.assertEqual(self.broker.submits, 0)

    def test_real_paper_transport_contract_works_with_mocked_broker_calls(self):
        client = AlpacaPaperClient("fictional-key", "fictional-secret")
        self.assertEqual(client.base_url, PAPER_URL)
        with self.assertRaises(AttributeError):
            client.base_url = "https://api.alpaca.markets"
        # Use the actual client methods and route construction, while preventing
        # all sockets: only _request is replaced with fictional responses.
        def request(method, path, *, params=None, payload=None):
            if method == "GET":
                if path.startswith("/v2/orders/"):
                    self.assertEqual(params, {"nested": "true"})
                    return next(copy.deepcopy(order) for order in self.broker.orders.values()
                                if path == "/v2/orders/" + order["id"])
                return {"/v2/account": self.broker.get_account,
                        "/v2/positions": self.broker.get_positions,
                        "/v2/orders": self.broker.get_orders,
                        "/v2/clock": self.broker.get_clock,
                        "/v2/orders:by_client_order_id": lambda: self.broker.get_order_by_client_id(params["client_order_id"])}[path]()
            self.assertEqual((method, path), ("POST", "/v2/orders"))
            return self.broker.submit_order(payload)
        client._request = Mock(side_effect=request)
        self.executor.client = client
        self.assertEqual(self.submit()["status"], "new")
        self.assertEqual(self.submit()["status"], "new")
        self.assertEqual(self.broker.submits, 1)

    def test_wrong_account_endpoint_strategy_feed_and_policy_block(self):
        cases = [
            {"account_id": "another-account"}, {"strategy_id": "another-strategy"},
            {"strategy_revision": "b" * 64},
            {"risk_policy": {**grant_spec()["risk_policy"], "expected_feed": "iex"}},
            {"risk_policy": {**grant_spec()["risk_policy"], "max_daily_loss": 1000}},
            {"risk_policy": {**grant_spec()["risk_policy"], "max_daily_loss": 10}},
        ]
        for index, updates in enumerate(cases):
            reference = "mismatched-" + str(index)
            self.executor.authorizations.approve(grant_spec(authorization_id=reference, **updates), now=NOW)
            with self.subTest(updates=updates), self.assertRaises(RiskRejected):
                self.executor.submit(PROPOSAL, QUOTE, authorization_id=reference, now=NOW)
        self.broker.base_url = "https://api.alpaca.markets"
        with self.assertRaisesRegex(RiskRejected, "paper endpoint"):
            self.submit()
        self.assertEqual(self.broker.submits, 0)
        self.assertEqual(self.executor.db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)

    def test_allocation_caps_order_even_with_larger_risk_limits(self):
        self.executor.authorizations.approve(grant_spec(authorization_id="small-allocation", allocation_usd="999"), now=NOW)
        with self.assertRaisesRegex(RiskRejected, "allocation"):
            self.executor.submit(PROPOSAL, QUOTE, authorization_id="small-allocation", now=NOW)
        self.assertEqual(self.broker.submits, 0)

    def test_expiry_boundary_and_future_approval_block_new_intents(self):
        for timestamp in (NOW - timedelta(seconds=1), NOW + timedelta(hours=12)):
            self.broker.clock["timestamp"] = timestamp.isoformat()
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(RiskRejected, "future dated or expired"):
                self.executor.submit(PROPOSAL, replace(QUOTE, timestamp=timestamp),
                                     authorization_id="fixture-consent", now=timestamp)
        self.assertEqual(self.broker.submits, 0)

    def test_pause_resume_and_irreversible_revocation(self):
        store = self.executor.authorizations
        store.set_state("fixture-consent", "paused", now=NOW)
        with self.assertRaisesRegex(RiskRejected, "paused or revoked"):
            self.submit()
        store.set_state("fixture-consent", "active", now=NOW)
        store.set_state("fixture-consent", "revoked", now=NOW)
        with self.assertRaises(AuthorizationError):
            store.set_state("fixture-consent", "active", now=NOW)
        with self.assertRaises(RiskRejected):
            self.submit()
        states = [row[0] for row in self.executor.db.execute(
            "SELECT state FROM paper_authorization_events ORDER BY sequence")]
        self.assertEqual(states, ["active", "paused", "active", "revoked"])
        self.assertEqual(self.broker.submits, 0)

    def test_external_pause_during_broker_reads_is_seen_before_intent_commit(self):
        def get_clock():
            with sqlite3.connect(self.path) as operator:
                AuthorizationLedger(operator).set_state("fixture-consent", "paused", now=NOW)
            return copy.deepcopy(self.broker.clock)
        self.broker.get_clock = get_clock
        with self.assertRaisesRegex(RiskRejected, "paused or revoked"):
            self.submit()
        self.assertEqual(self.broker.submits, 0)
        self.assertEqual(self.executor.db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)

    def test_authorization_risk_and_intent_are_durable_before_broker_submit(self):
        def check(payload):
            with sqlite3.connect(self.path) as observer:
                row = observer.execute("SELECT authorization,strategy_id,strategy_revision FROM paper_intents").fetchone()
                grant = json.loads(row[0])
                self.assertEqual(grant["scope"]["authorization_id"], "fixture-consent")
                self.assertEqual(row[1:], (PROPOSAL.strategy_id, PROPOSAL.strategy_revision))
                evidence = json.loads(observer.execute(
                    "SELECT detail FROM paper_audit WHERE event='entry_risk_snapshot'").fetchone()[0])
                self.assertEqual(evidence["authorization_hash"], grant["scope_hash"])
                self.assertEqual(evidence["policy"], grant["scope"]["risk_policy"])
        self.broker.on_submit = check
        order = self.submit()
        receipt = json.loads(self.executor.db.execute(
            "SELECT detail FROM paper_audit WHERE event='broker_order' ORDER BY sequence DESC LIMIT 1").fetchone()[0])
        self.assertEqual(receipt["client_order_id"], order["client_order_id"])
        self.assertEqual(receipt["order"]["id"], order["id"])

    def test_audit_write_failure_rolls_back_intent_without_submitting(self):
        self.executor.db.execute("""CREATE TRIGGER fail_risk_audit BEFORE INSERT ON paper_audit
            WHEN NEW.event='entry_risk_snapshot' BEGIN SELECT RAISE(ABORT,'fixture write failure'); END""")
        self.executor.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit()
        self.assertEqual(self.broker.submits, 0)
        self.assertEqual(self.executor.db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)
        self.assertEqual(self.executor.db.execute("SELECT count(*) FROM paper_audit WHERE event='intent_persisted'").fetchone()[0], 0)

    def test_committed_order_can_arrive_after_pause_and_is_not_resubmitted(self):
        self.broker.on_submit = lambda payload: self.executor.authorizations.set_state("fixture-consent", "paused", now=NOW)
        order = self.submit()
        self.assertEqual(self.executor.authorizations.get("fixture-consent")["state"], "paused")
        self.assertEqual(self.submit()["id"], order["id"])
        self.assertEqual(self.broker.submits, 1)

    def test_restart_and_duplicate_recovery_survive_revocation_and_expiry(self):
        self.broker.mode = "timeout_accepted"
        order = self.submit()
        self.executor.authorizations.set_state("fixture-consent", "revoked", now=NOW)
        self.executor.close()
        self.executor = PaperExecutor(self.broker, self.path, self.policy)
        recovered = self.executor.submit(PROPOSAL, QUOTE, authorization_id="fixture-consent", now=NOW + timedelta(days=1))
        self.assertEqual(recovered["id"], order["id"])
        self.assertEqual(self.broker.submits, 1)
        with self.assertRaises(RiskRejected):
            self.submit(replace(PROPOSAL, signal_id="next-signal"))

    def test_same_signal_cannot_be_rebound_to_new_strategy_or_grant(self):
        self.submit()
        self.executor.authorizations.approve(grant_spec(authorization_id="second-consent"), now=NOW)
        with self.assertRaisesRegex(ReconciliationRequired, "lineage"):
            self.executor.submit(PROPOSAL, QUOTE, authorization_id="second-consent", now=NOW)
        with self.assertRaisesRegex(ReconciliationRequired, "lineage"):
            self.submit(replace(PROPOSAL, strategy_revision="b" * 64))
        self.assertEqual(self.broker.submits, 1)

    def test_corrupt_intent_authorization_snapshot_cannot_pass_duplicate_recovery(self):
        self.submit()
        snapshot = json.loads(self.executor.db.execute("SELECT authorization FROM paper_intents").fetchone()[0])
        snapshot["scope"]["allocation_usd"] = "999999"
        with self.executor.db:
            self.executor.db.execute("UPDATE paper_intents SET authorization=?", (json.dumps(snapshot),))
        with self.assertRaisesRegex(ReconciliationRequired, "lineage"):
            self.submit()
        self.assertEqual(self.broker.submits, 1)
        self.assertEqual(len(self.executor.reconcile()["open_orders"]), 1)

    def test_reconciliation_and_owned_cancellation_continue_after_revocation(self):
        order = self.submit()
        self.executor.authorizations.set_state("fixture-consent", "revoked", now=NOW)
        self.executor.cancel_pending(order["client_order_id"])
        self.assertEqual(self.broker.orders[order["client_order_id"]]["status"], "canceled")
        self.assertEqual(self.executor.reconcile()["positions"], [])

    def test_grants_and_control_history_are_immutable_and_detached_from_input(self):
        spec = grant_spec(authorization_id="immutable")
        self.executor.authorizations.approve(spec, now=NOW)
        spec["risk_policy"]["max_daily_loss"] = 10000
        self.assertEqual(self.executor.authorizations.get("immutable")["scope"]["risk_policy"]["max_daily_loss"], 100)
        for table in ("paper_authorizations", "paper_authorization_events"):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError), self.executor.db:
                self.executor.db.execute("DELETE FROM " + table)
        with self.assertRaises(AuthorizationError):
            self.executor.authorizations.approve(grant_spec(), now=NOW)

    def test_incomplete_expanded_and_nonfinite_grants_are_rejected(self):
        cases = [{"symbols": ["SPY"]}, {"side": "sell"}, {"account_mode": "live"},
                 {"provider": "binance"}, {"strategy_revision": "latest"},
                 {"strategy_revision": "../file"}, {"granted_by": "<script>"},
                 {"allocation_usd": True}, {"allocation_usd": "NaN"},
                 {"risk_policy": {**grant_spec()["risk_policy"], "expected_feed": []}},
                 {"risk_policy": {**grant_spec()["risk_policy"], "max_daily_loss": True}},
                 {"expires_at": "2026-10-02T16:00:00"}, {"expires_at": NOW.isoformat()},
                 {"unapproved_extra_permission": True}]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes), self.assertRaises(AuthorizationError):
                self.executor.authorizations.approve(grant_spec(authorization_id="invalid-" + str(index), **changes), now=NOW)
        self.assertEqual(self.executor.db.execute("SELECT count(*) FROM paper_authorizations").fetchone()[0], 1)

    def test_historical_intent_migration_preserves_reconciliation_without_inventing_consent(self):
        order = self.submit()
        self.executor.close()
        with sqlite3.connect(self.path) as historical:
            for column in ("authorization", "strategy_id", "strategy_revision"):
                historical.execute("ALTER TABLE paper_intents DROP COLUMN " + column)
        self.executor = PaperExecutor(self.broker, self.path, self.policy)
        self.assertIsNone(self.executor.db.execute("SELECT authorization FROM paper_intents").fetchone()[0])
        self.assertEqual(len(self.executor.reconcile()["open_orders"]), 1)
        self.assertEqual(self.broker.submits, 1)
        with self.assertRaisesRegex(ReconciliationRequired, "lineage"):
            self.submit()
        self.executor.cancel_pending(order["client_order_id"])

    def test_ledger_initialization_and_controls_do_not_commit_or_rollback_caller_work(self):
        self.executor.db.execute("CREATE TABLE unrelated(value TEXT)")
        for operation in (lambda: AuthorizationLedger(self.executor.db),
                          lambda: self.executor.authorizations.approve(grant_spec(authorization_id="new"), now=NOW),
                          lambda: self.executor.authorizations.set_state("fixture-consent", "paused", now=NOW)):
            self.executor.db.execute("INSERT INTO unrelated VALUES('pending')")
            with self.assertRaises(AuthorizationError):
                operation()
            self.assertTrue(self.executor.db.in_transaction)
            self.assertEqual(self.executor.db.execute("SELECT count(*) FROM unrelated").fetchone()[0], 1)
            self.executor.db.rollback()
            self.assertEqual(self.executor.db.execute("SELECT count(*) FROM unrelated").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
