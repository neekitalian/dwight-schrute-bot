import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from dwight.paper import (
    AlpacaPaperClient, BrokerHTTPError, OrderProposal, PAPER_URL, PaperError,
    PaperExecutor, PreparationRequired, Quote, ReconciliationRequired, RiskPolicy,
    RiskRejected, _NoRedirect,
)


NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
PROPOSAL = OrderProposal("vwap-SPY-20261002T150000-model1", "SPY", 2, 500.01, 499.00, 503.00)
QUOTE = Quote("SPY", 500.00, 500.01, NOW, "sip")


class FakeBroker:
    """No sockets, keys, or real account use in these tests."""

    def __init__(self):
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
            {"id": "stop-1", "symbol": "SPY", "side": "sell", "type": "stop",
             "qty": order["qty"], "filled_qty": "0", "status": "new"},
            {"id": "target-1", "symbol": "SPY", "side": "sell", "type": "limit",
             "qty": order["qty"], "filled_qty": "0", "status": "new"},
        ] if protected else []
        self.positions = [{"symbol": "SPY", "qty": order["qty"], "market_value": "1000.02"}]


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

    def tearDown(self):
        self.executor.close()
        self.temp.cleanup()

    def submit(self, proposal=PROPOSAL, quote=QUOTE):
        return self.executor.submit(proposal, quote, now=NOW)

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
        self.broker.orders["other"] = {"id": "foreign", "status": "new", "symbol": "SPY"}
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
        self.broker.positions = [{"symbol": "SPY", "qty": "1", "market_value": "500"}]
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
        with self.assertRaises(ValueError): RiskPolicy(allowed_symbols=("AAPL",))
        with self.assertRaises(RiskRejected): RiskPolicy(max_order_notional=float("nan"))


if __name__ == "__main__":
    unittest.main()
