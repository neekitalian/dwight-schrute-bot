"""Owned paper lifecycle fixtures only. No credentials, sockets or broker calls."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dwight.paper import ReconciliationRequired, RiskPolicy, RiskRejected
from dwight.paper_supervision import SupervisedPaperExecutor
from tests.test_paper import FakeBroker, NOW, PROPOSAL, QUOTE, grant_spec


class LifecycleBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.account["cash"] = "100000"
        self.clock["next_close"] = "2026-10-02T20:00:00+00:00"
        self.calendar = [{"date": "2026-10-02", "open": "09:30", "close": "16:00"}]
        self.cancels = []
        self.cancel_mode = "confirmed"
        self.exit_mode = "new"
        self.on_exit = None
        self.after_get_account = None

    def get_account(self):
        result = super().get_account()
        if self.after_get_account:
            self.after_get_account()
        return result

    def get_calendar(self, start, end):
        return copy.deepcopy(self.calendar)

    def get_orders(self):
        result = []
        for order in self.orders.values():
            if order["status"] not in {"filled", "canceled", "expired", "rejected"}:
                result.append(copy.deepcopy(order))
            else:
                result.extend(copy.deepcopy(leg) for leg in order.get("legs", [])
                              if leg["status"] not in {"filled", "canceled", "expired", "rejected"})
        return result

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        if self.cancel_mode == "timeout":
            raise TimeoutError("fixture ambiguous DELETE")
        for parent in self.orders.values():
            if parent["id"] == order_id:
                if self.cancel_mode == "fill_race":
                    self.fill(parent["client_order_id"])
                elif self.cancel_mode == "pending":
                    parent["status"] = "pending_cancel"
                    return
                else:
                    parent["status"] = "canceled"
                for leg in parent.get("legs", []):
                    if leg["status"] != "filled":
                        leg["status"] = "canceled"
                return
            if any(leg["id"] == order_id for leg in parent.get("legs", [])):
                for leg in parent["legs"]:
                    if leg["status"] != "filled":
                        leg["status"] = "pending_cancel" if self.cancel_mode == "pending" else "canceled"
                return

    def submit_order(self, payload):
        if payload["side"] == "buy":
            return super().submit_order(payload)
        self.submits += 1
        if self.on_exit:
            self.on_exit(payload)
        if self.exit_mode == "timeout_absent":
            raise TimeoutError("fixture no known exit")
        order = copy.deepcopy(payload)
        order.update(id="owned-close-" + str(self.submits), status="new", filled_qty="0", order_class="simple")
        if self.exit_mode == "rejected":
            order["status"] = "rejected"
        elif self.exit_mode in {"filled", "partial"}:
            quantity = int(order["qty"]) if self.exit_mode == "filled" else 1
            order.update(status="filled" if quantity == int(order["qty"]) else "partially_filled", filled_qty=str(quantity))
            remaining = int(self.positions[0]["qty"]) - quantity
            self.positions = [{"symbol": "QQQ", "qty": str(remaining), "market_value": str(remaining * 500)}] if remaining else []
        self.orders[payload["client_order_id"]] = order
        if self.exit_mode == "timeout_accepted":
            raise TimeoutError("fixture accepted exit response lost")
        return copy.deepcopy(order)

    def partial(self, cid):
        parent = self.orders[cid]
        parent.update(status="partially_filled", filled_qty="1")
        self.positions = [{"symbol": "QQQ", "qty": "1", "market_value": "500"}]


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "paper.sqlite3"
        self.broker = LifecycleBroker()
        self.policy = RiskPolicy(max_order_notional=1100)
        self.executor = SupervisedPaperExecutor(self.broker, self.path, self.policy)
        self.executor.authorizations.approve(grant_spec(self.policy), now=NOW)

    def tearDown(self):
        self.executor.close()
        self.temp.cleanup()

    def enter(self):
        return self.executor.submit(PROPOSAL, QUOTE, authorization_id="fixture-consent", now=NOW)

    def pulse(self, now=NOW):
        self.broker.clock["timestamp"] = now.isoformat()
        return self.executor.supervise(now=now)

    def close_rows(self):
        return self.executor.db.execute("SELECT * FROM paper_exit_intents").fetchall()

    def test_cash_guard_prevents_margin_entry(self):
        self.broker.account["cash"] = "999"
        with self.assertRaisesRegex(RiskRejected, "cash"):
            self.enter()
        self.assertEqual(self.broker.submits, 0)
        self.assertEqual(self.executor.db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)
        for cash in (None, "NaN", "0"):
            self.broker.account["cash"] = cash
            with self.subTest(cash=cash), self.assertRaises(RiskRejected):
                self.enter()

    def test_partial_entry_waits_for_terminal_then_closes_verified_quantity(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.broker.cancel_mode = "pending"
        result = self.pulse()
        self.assertFalse(result["entries_allowed"])
        self.assertEqual(self.broker.cancels, [self.broker.orders[cid]["id"]])
        self.assertEqual(len(self.close_rows()), 0)
        self.pulse()
        self.assertEqual(len(self.broker.cancels), 1)
        self.assertEqual(len(self.close_rows()), 0)
        self.broker.orders[cid]["status"] = "canceled"
        self.pulse()
        payload = json.loads(self.close_rows()[0]["payload"])
        self.assertEqual((payload["symbol"], payload["side"], payload["qty"], payload["type"]), ("QQQ", "sell", "1", "market"))
        self.assertEqual(self.broker.submits, 2)

    def test_cancel_full_fill_race_updates_close_quantity(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.broker.cancel_mode = "fill_race"
        self.pulse()
        self.assertEqual(len(self.close_rows()), 0)
        self.assertEqual(self.broker.orders[cid]["status"], "filled")
        self.pulse()
        self.assertEqual(json.loads(self.close_rows()[0]["payload"])["qty"], "2")

    def test_close_never_overlaps_pending_bracket_leg(self):
        cid = self.enter()["client_order_id"]
        self.broker.fill(cid)
        now = datetime(2026, 10, 2, 19, 56, tzinfo=timezone.utc)
        self.broker.cancel_mode = "pending"
        self.pulse(now)
        self.pulse(now)
        self.assertEqual(len(self.close_rows()), 0)
        self.assertEqual(len(self.broker.cancels), 1)
        for leg in self.broker.orders[cid]["legs"]:
            leg["status"] = "canceled"
        self.pulse(now)
        self.assertEqual(len(self.close_rows()), 1)

    def test_exit_is_durable_before_post_with_stable_restart_identity(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.pulse()
        def inspect(payload):
            with sqlite3.connect(self.path) as reader:
                row = reader.execute("SELECT client_order_id,state,payload FROM paper_exit_intents").fetchone()
                self.assertEqual(row[0], payload["client_order_id"])
                self.assertEqual(row[1], "submitting")
                self.assertEqual(json.loads(row[2]), payload)
        self.broker.on_exit = inspect
        self.pulse()
        close_id = self.close_rows()[0]["client_order_id"]
        self.executor.close()
        self.executor = SupervisedPaperExecutor(self.broker, self.path, self.policy)
        self.pulse()
        self.assertEqual(self.close_rows()[0]["client_order_id"], close_id)
        self.assertEqual(self.broker.submits, 2)

    def test_ambiguous_exit_accepted_is_looked_up_without_resubmission(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.pulse()
        self.broker.exit_mode = "timeout_accepted"
        self.pulse()
        self.pulse()
        self.assertEqual(self.broker.submits, 2)
        self.assertEqual(self.close_rows()[0]["state"], "new")

    def test_ambiguous_exit_absent_is_never_retried(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.pulse()
        self.broker.exit_mode = "timeout_absent"
        with self.assertRaisesRegex(ReconciliationRequired, "never resubmit"):
            self.pulse()
        self.broker.exit_mode = "new"
        with self.assertRaisesRegex(ReconciliationRequired, "never resubmit"):
            self.pulse()
        self.assertEqual(self.broker.submits, 2)
        self.assertEqual(self.close_rows()[0]["state"], "ambiguous")

    def test_rejected_exit_with_exposure_requires_operator(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.pulse()
        self.broker.exit_mode = "rejected"
        self.pulse()
        result = self.pulse()
        self.assertTrue(result["operator_required"])
        self.assertFalse(result["entries_allowed"])
        self.pulse()
        self.assertEqual(self.broker.submits, 2)

    def test_partial_exit_is_polled_without_second_sell(self):
        cid = self.enter()["client_order_id"]
        self.broker.fill(cid)
        for leg in self.broker.orders[cid]["legs"]:
            leg["status"] = "canceled"
        self.broker.exit_mode = "partial"
        self.pulse()
        result = self.pulse()
        self.assertEqual(result["owned_quantity_at_snapshot"], "1")
        self.assertFalse(result["entries_allowed"])
        self.assertEqual(self.broker.submits, 2)

    def test_filled_exit_reconciles_flat_without_base_position_mismatch(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.pulse()
        self.broker.exit_mode = "filled"
        self.pulse()
        result = self.pulse()
        self.assertTrue(result["entries_allowed"])
        self.assertEqual(self.executor.reconcile()["positions"], [])

    def test_pause_revocation_and_expiry_do_not_disable_exit_supervision(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.executor.authorizations.set_state("fixture-consent", "revoked", now=NOW)
        self.pulse()
        self.pulse()
        self.assertEqual(len(self.close_rows()), 1)
        self.assertEqual(self.broker.submits, 2)

    def test_unknown_orders_or_positions_cause_no_mutations(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.broker.orders["foreign"] = {"id": "unknown", "symbol": "QQQ", "side": "sell", "qty": "1", "filled_qty": "0", "status": "new"}
        with self.assertRaisesRegex(ReconciliationRequired, "Unknown broker order"):
            self.pulse()
        self.broker.orders.pop("foreign")
        self.broker.positions = [{"symbol": "SPY", "qty": "1"}]
        with self.assertRaisesRegex(ReconciliationRequired, "Unknown account position"):
            self.pulse()
        self.assertEqual(self.broker.cancels, [])
        self.assertEqual(len(self.close_rows()), 0)

    def test_inconsistent_positions_and_backwards_fills_halt(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.broker.positions[0]["qty"] = "2"
        with self.assertRaisesRegex(ReconciliationRequired, "positions differ"):
            self.pulse()
        self.broker.positions[0]["qty"] = "1"
        self.pulse()
        self.broker.orders[cid]["filled_qty"] = "0"
        self.broker.positions = []
        with self.assertRaisesRegex(ReconciliationRequired, "went backwards"):
            self.pulse()

    def test_terminal_replacement_and_short_exposure_halt(self):
        cid = self.enter()["client_order_id"]
        self.broker.fill(cid)
        self.pulse()
        self.broker.orders[cid]["status"] = "replaced"
        with self.assertRaises(ReconciliationRequired):
            self.pulse()
        self.broker.orders[cid]["status"] = "filled"
        for leg in self.broker.orders[cid]["legs"]:
            leg.update(status="filled", filled_qty="2")
        self.broker.positions = [{"symbol": "QQQ", "qty": "-2"}]
        with self.assertRaisesRegex(ReconciliationRequired, "oversold"):
            self.pulse()

    def test_stale_unfilled_entry_cancelled_without_sell(self):
        cid = self.enter()["client_order_id"]
        now = NOW + timedelta(seconds=121)
        self.pulse(now)
        result = self.pulse(now)
        self.assertEqual(self.broker.orders[cid]["status"], "canceled")
        self.assertEqual(result["pending_jobs"], 0)
        self.assertEqual(len(self.close_rows()), 0)

    def test_missing_stop_is_recovered_and_new_entries_halt(self):
        cid = self.enter()["client_order_id"]
        self.broker.fill(cid, protected=False)
        with self.assertRaises(ReconciliationRequired):
            self.executor.reconcile()
        self.pulse()
        self.assertEqual(len(self.close_rows()), 1)

    def test_early_close_calendar_drives_cutoff_and_exit(self):
        cid = self.enter()["client_order_id"]
        self.broker.fill(cid)
        self.broker.calendar[0]["close"] = "13:00"
        self.broker.clock["next_close"] = "2026-10-02T17:00:00+00:00"
        now = datetime(2026, 10, 2, 16, 56, tzinfo=timezone.utc)
        result = self.pulse(now)
        self.assertEqual(result["session_close"], "2026-10-02T17:00:00+00:00")
        self.assertFalse(result["entries_allowed"])
        self.assertEqual(len(self.broker.cancels), 1)
        self.pulse(now)
        self.assertEqual(len(self.close_rows()), 1)

    def test_after_hours_keeps_protection_and_does_not_queue_market_exit(self):
        cid = self.enter()["client_order_id"]
        self.broker.fill(cid)
        self.broker.clock["is_open"] = False
        now = datetime(2026, 10, 2, 20, 1, tzinfo=timezone.utc)
        result = self.pulse(now)
        self.assertFalse(result["market_open"])
        self.assertFalse(result["entries_allowed"])
        self.assertEqual(self.broker.cancels, [])
        self.assertEqual(len(self.close_rows()), 0)

    def test_clock_calendar_disagreement_blocks_mutation(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.broker.clock["next_close"] = "2026-10-02T19:00:00+00:00"
        with self.assertRaisesRegex(ReconciliationRequired, "close differs"):
            self.pulse()
        self.assertEqual(self.broker.cancels, [])

    def test_cancel_timeout_is_not_retried_and_is_disclosed(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.broker.cancel_mode = "timeout"
        self.assertTrue(self.pulse()["operator_required"])
        self.assertTrue(self.pulse()["operator_required"])
        self.assertEqual(len(self.broker.cancels), 1)
        self.assertEqual(len(self.close_rows()), 0)

    def test_orphan_sibling_exit_is_cancelled_before_flat_report(self):
        cid = self.enter()["client_order_id"]
        self.broker.fill(cid)
        self.broker.orders[cid]["legs"][0].update(status="filled", filled_qty="2")
        self.broker.positions = []
        with self.assertRaises(ReconciliationRequired):
            self.executor.reconcile()
        self.pulse()
        result = self.pulse()
        self.assertTrue(result["entries_allowed"])
        self.assertEqual(len(self.broker.cancels), 1)
        self.assertEqual(len(self.close_rows()), 0)

    def test_entry_cutoff_and_signal_expiry_block_before_post(self):
        now = datetime(2026, 10, 2, 19, 55, tzinfo=timezone.utc)
        self.broker.clock["timestamp"] = now.isoformat()
        with self.assertRaisesRegex(RiskRejected, "session window"):
            self.executor.submit(PROPOSAL, replace(QUOTE, timestamp=now), authorization_id="fixture-consent", now=now)
        self.broker.clock["timestamp"] = NOW.isoformat()
        with self.assertRaisesRegex(RiskRejected, "Signal expired"):
            self.executor.submit(PROPOSAL, QUOTE, authorization_id="fixture-consent", signal_expires_at=NOW, now=NOW)
        self.assertEqual(self.broker.submits, 0)

    def test_account_changed_during_final_clock_read_blocks_close_post(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.pulse()
        original = self.broker.get_clock
        reads = 0
        def clock_with_race():
            nonlocal reads
            reads += 1
            if reads == 2:
                self.broker.positions[0]["qty"] = "9"
            return original()
        self.broker.get_clock = clock_with_race
        with self.assertRaisesRegex(ReconciliationRequired, "positions differ"):
            self.pulse()
        self.assertEqual(len(self.close_rows()), 0)
        self.assertEqual(self.broker.submits, 1)

    def test_bracket_fill_during_final_clock_read_prevents_extra_cancel_or_sell(self):
        cid = self.enter()["client_order_id"]
        self.broker.fill(cid)
        original = self.broker.get_clock
        reads = 0
        def clock_with_fill():
            nonlocal reads
            reads += 1
            if reads == 2:
                legs = self.broker.orders[cid]["legs"]
                legs[0].update(status="filled", filled_qty="2")
                legs[1]["status"] = "canceled"
                self.broker.positions = []
            return original()
        self.broker.get_clock = clock_with_fill
        now = datetime(2026, 10, 2, 19, 56, tzinfo=timezone.utc)
        self.pulse(now)
        result = self.pulse(now)
        self.assertEqual(result["pending_jobs"], 0)
        self.assertEqual(self.broker.cancels, [])
        self.assertEqual(len(self.close_rows()), 0)

    def test_exit_audit_failure_rolls_back_close_intent_before_any_post(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.pulse()
        self.executor.db.execute("""CREATE TRIGGER exit_audit_failure BEFORE INSERT ON paper_audit
            WHEN NEW.event='supervision_exit_persisted' BEGIN SELECT RAISE(ABORT,'fixture storage failure'); END""")
        self.executor.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.pulse()
        self.assertEqual(len(self.close_rows()), 0)
        self.assertEqual(self.broker.submits, 1)

    def test_unconfirmed_cancellation_stall_requires_operator(self):
        cid = self.enter()["client_order_id"]
        self.broker.partial(cid)
        self.broker.cancel_mode = "pending"
        self.pulse()
        result = self.pulse(NOW + timedelta(seconds=61))
        self.assertTrue(result["cancellation_stalled"])
        self.assertTrue(result["operator_required"])
        self.assertEqual(len(self.broker.cancels), 1)
        self.assertEqual(len(self.close_rows()), 0)

    def test_malformed_calendar_and_clock_values_fail_closed(self):
        self.broker.clock["next_close"] = None
        with self.assertRaises(ReconciliationRequired):
            self.pulse()
        self.broker.clock["next_close"] = "2026-10-02T20:00:00+00:00"
        self.broker.calendar[0]["close"] = "16:00-03:00"
        with self.assertRaisesRegex(ReconciliationRequired, "calendar date"):
            self.pulse()
        self.assertEqual(self.broker.submits, 0)
        self.assertEqual(self.broker.cancels, [])


if __name__ == "__main__":
    unittest.main()
