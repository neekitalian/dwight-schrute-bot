"""Native TradingView paper evidence, without account access or network calls."""
import csv
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dwight.manual import ACCOUNT, ManualPaperError, ManualPaperJournal


NOW = datetime(2020, 1, 2, 15, 0, tzinfo=timezone.utc)


def proposal(**changes):
    value = {"proposal_id": "QQQ-vwap-20200102T1500-v1", "account": ACCOUNT,
             "symbol": "QQQ", "side": "buy", "quantity": "3", "entry": "100",
             "stop": "99", "target": "102", "source": "vwap_pullback",
             "model": "logistic-v1", "version": "test-only-commit",
             "signal_at": NOW.isoformat(), "available_at": NOW.isoformat(),
             "expires_at": (NOW + timedelta(minutes=5)).isoformat()}
    return {**value, **changes}


def fill(fill_id, side, quantity, price, fee="0", *, minute=1, sequence=0, **changes):
    value = {"fill_id": fill_id, "filled_at": (NOW + timedelta(minutes=minute)).isoformat(),
             "sequence": str(sequence), "symbol": "QQQ", "side": side,
             "quantity": str(quantity), "price": str(price), "fee": str(fee),
             "data_kind": "synthetic", "proposal_id": ""}
    return {**value, **changes}


class ManualJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "private" / "manual.sqlite3"
        self.journal = ManualPaperJournal(self.path)

    def csv(self, rows, fields=None):
        path = self.root / "fills.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields or list(fill("id", "buy", 1, 100)))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def report(self):
        return self.journal.report(now=NOW + timedelta(days=1))

    def test_private_journal_and_no_account_performance_claim(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        report = self.report()
        self.assertEqual(report["account"], ACCOUNT)
        self.assertFalse(report["submits_orders"])
        self.assertFalse(report["broker_verified"])
        self.assertIsNone(report["account_equity"])
        self.assertIsNone(report["account_return_pct"])
        self.assertEqual(report["data_kind"], "no_fills")
        json.dumps(report, allow_nan=False)

    def test_proposal_copy_idempotency_and_exact_risk_math(self):
        source = proposal()
        first = self.journal.add_proposal(source, now=NOW)
        source["quantity"] = 99
        second = self.journal.add_proposal(proposal(quantity=3, entry=100.0), now=NOW)
        self.assertEqual(first, second)
        self.assertEqual(first["planned_stop_risk_before_costs"], "3")
        self.assertEqual(first["planned_reward_before_costs"], "6")
        self.assertEqual(first["reward_risk_ratio_before_costs"], "2")
        self.assertEqual(len(self.journal.list_proposals(now=NOW)), 1)
        self.assertEqual(len(self.journal.report(now=NOW)["proposal_events"]), 1)
        with self.assertRaisesRegex(ManualPaperError, "conflicts"):
            self.journal.add_proposal(proposal(quantity=4), now=NOW)

    def test_rejects_wrong_account_symbol_and_short_proposals(self):
        for changes in ({"account": "alpaca"}, {"symbol": "SPY"}, {"side": "sell"}):
            with self.subTest(changes=changes), self.assertRaises(ManualPaperError):
                self.journal.add_proposal(proposal(**changes), now=NOW)
        self.assertEqual(self.journal.list_proposals(now=NOW), [])

    def test_rejects_bad_money_and_risk_ordering(self):
        bad = ({"entry": "nan"}, {"entry": "Infinity"}, {"quantity": True},
               {"quantity": "0"}, {"quantity": "-1"}, {"quantity": "0.000000001"},
               {"target": "1e10000"}, {"stop": "100"}, {"stop": "101"},
               {"target": "100"}, {"quantity": "1" * 81})
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ManualPaperError):
                self.journal.add_proposal(proposal(**changes), now=NOW)

    def test_rejects_unavailable_expired_and_naive_timestamps(self):
        bad = ({"signal_at": "2020-01-02T15:00:00"},
               {"available_at": (NOW + timedelta(seconds=1)).isoformat()},
               {"signal_at": (NOW + timedelta(minutes=1)).isoformat()},
               {"expires_at": NOW.isoformat()})
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ManualPaperError):
                self.journal.add_proposal(proposal(**changes), now=NOW)
        with self.assertRaises(ManualPaperError):
            self.journal.add_proposal(proposal(), now=NOW.replace(tzinfo=None))

    def test_timezone_offsets_normalize_to_same_proposal(self):
        first = self.journal.add_proposal(proposal(), now=NOW)
        tz = timezone(timedelta(hours=-5))
        second = self.journal.add_proposal(proposal(
            signal_at=NOW.astimezone(tz).isoformat(),
            available_at=NOW.astimezone(tz).isoformat(),
            expires_at=(NOW + timedelta(minutes=5)).astimezone(tz).isoformat()), now=NOW)
        self.assertEqual(first, second)

    def test_confirmation_is_terminal_human_decision_not_fill(self):
        self.journal.add_proposal(proposal(), now=NOW)
        confirmed = self.journal.set_status(proposal()["proposal_id"], "confirmed", now=NOW)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertFalse(confirmed["submits_orders"])
        self.assertEqual(self.journal.set_status(proposal()["proposal_id"], "confirmed", now=NOW), confirmed)
        self.assertEqual(self.journal.report(now=NOW)["fills"], [])
        with self.assertRaisesRegex(ManualPaperError, "terminal"):
            self.journal.set_status(proposal()["proposal_id"], "skipped", now=NOW)
        self.assertEqual(self.journal.list_proposals(now=NOW + timedelta(days=1))[0]["status"], "confirmed")

    def test_expiry_is_durable_after_rejected_late_confirmation(self):
        self.journal.add_proposal(proposal(), now=NOW)
        later = NOW + timedelta(minutes=5)
        with self.assertRaisesRegex(ManualPaperError, "terminal"):
            self.journal.set_status(proposal()["proposal_id"], "confirmed", now=later)
        recovered = ManualPaperJournal(self.path)
        self.assertEqual(recovered.list_proposals(now=later)[0]["status"], "expired")
        self.assertEqual([e["status"] for e in recovered.report(now=later)["proposal_events"]], ["pending", "expired"])
        with self.assertRaisesRegex(ManualPaperError, "clock"):
            recovered.list_proposals(now=NOW)

    def test_cannot_expire_early_or_reset_pending(self):
        self.journal.add_proposal(proposal(), now=NOW)
        for status in ("expired", "pending", "filled"):
            with self.subTest(status=status), self.assertRaises(ManualPaperError):
                self.journal.set_status(proposal()["proposal_id"], status, now=NOW)

    def test_fifo_multiple_lots_partial_fills_and_fees(self):
        rows = [fill("b1", "buy", 2, 100, "0.2", minute=1),
                fill("b2", "buy", 3, 110, "0.3", minute=2),
                fill("s1", "sell", 3, 120, "0.3", minute=3)]
        self.journal.import_fills(self.csv(rows))
        report = self.report()
        # 2*(120-100)+1*(120-110) - .2 - .1 - .3 = 49.4
        self.assertEqual(report["net_realized_pnl"], "49.4")
        self.assertEqual(report["open_position"]["quantity"], "2")
        self.assertEqual(report["open_position"]["cost_including_remaining_entry_fees"], "220.2")
        self.assertEqual(report["reconciliation_status"], "unknown_open_positions")
        self.assertIsNone(report["open_position"]["mark_price"])
        self.assertIsNone(report["open_position"]["unrealized_pnl"])
        self.assertEqual(len(report["fifo_matches"]), 2)
        self.assertEqual(report["closed_exit_count"], 1)
        self.journal.import_fills(self.csv([fill("s2", "sell", 2, 115, "0.2", minute=4)]))
        report = self.report()
        self.assertEqual(report["net_realized_pnl"], "59")
        self.assertEqual(report["net_trade_cash_flow"], "59")
        self.assertEqual(report["fees_imported"], "1")
        self.assertEqual(report["open_position"]["quantity"], "0")
        self.assertEqual(report["reconciliation_status"], "unverified_no_account_snapshot")
        self.assertIsNone(report["account_equity"])

    def test_fractional_inventory_and_fee_remainder_are_conserved(self):
        rows = [fill("b", "buy", ".3", 100, "1", minute=1)]
        rows += [fill(f"s{i}", "sell", ".1", 110, ".1", minute=i+1) for i in (1, 2, 3)]
        self.journal.import_fills(self.csv(rows))
        report = self.report()
        self.assertEqual(report["net_realized_pnl"], "1.7")
        self.assertEqual(report["net_trade_cash_flow"], "1.7")
        self.assertEqual(report["open_position"]["quantity"], "0")
        self.assertEqual(report["fees_imported"], "1.3")

    def test_idempotent_reimport_and_conflict_transaction_rollback(self):
        first = fill("b1", "buy", 1, 100)
        path = self.csv([first, first])
        result = self.journal.import_fills(path)
        self.assertEqual((result["inserted"], result["duplicates_skipped"]), (1, 1))
        result = self.journal.import_fills(path)
        self.assertEqual((result["inserted"], result["duplicates_skipped"]), (0, 2))
        bad = self.csv([fill("b2", "buy", 1, 100, minute=2), {**first, "price": "101"}])
        with self.assertRaisesRegex(ManualPaperError, "conflicts"):
            self.journal.import_fills(bad)
        self.assertEqual([f["fill_id"] for f in self.report()["fills"]], ["b1"])

    def test_oversell_rolls_back_entire_batch(self):
        with self.assertRaisesRegex(ManualPaperError, "inventory"):
            self.journal.import_fills(self.csv([fill("b", "buy", 1, 100), fill("s", "sell", 2, 110, minute=2)]))
        self.assertEqual(self.report()["fills"], [])
        self.journal.import_fills(self.csv([fill("b", "buy", 1, 100)]))
        with self.assertRaisesRegex(ManualPaperError, "inventory"):
            self.journal.import_fills(self.csv([fill("before", "sell", 1, 110, minute=0)]))
        self.assertEqual(len(self.report()["fills"]), 1)

    def test_out_of_order_rows_are_chronological_and_sequence_is_explicit(self):
        self.journal.import_fills(self.csv([
            fill("sale", "sell", 1, 102, sequence=2), fill("entry", "buy", 1, 100, sequence=1)]))
        self.assertEqual(self.report()["net_realized_pnl"], "2")
        with self.assertRaisesRegex(ManualPaperError, "sequence conflict"):
            self.journal.import_fills(self.csv([fill("duplicate-ordering", "buy", 1, 100, sequence=1)]))

    def test_retroactive_import_recalculates_fifo_from_complete_history(self):
        self.journal.import_fills(self.csv([fill("b2", "buy", 1, 110, minute=2), fill("s", "sell", 1, 120, minute=3)]))
        self.assertEqual(self.report()["net_realized_pnl"], "10")
        self.journal.import_fills(self.csv([fill("b1", "buy", 1, 100, minute=1)]))
        self.assertEqual(self.report()["net_realized_pnl"], "20")
        self.assertEqual(self.report()["open_position"]["lots"][0]["entry_fill_id"], "b2")

    def test_proposal_links_are_optional_but_unknown_links_reject_atomically(self):
        with self.assertRaisesRegex(ManualPaperError, "unknown proposal"):
            self.journal.import_fills(self.csv([
                fill("a", "buy", 1, 100), fill("b", "buy", 1, 100, minute=2, proposal_id="unknown")]))
        self.assertEqual(self.report()["fills"], [])
        self.journal.add_proposal(proposal(), now=NOW)
        self.journal.import_fills(self.csv([
            fill("late", "buy", 1, "100.1", minute=6, proposal_id=proposal()["proposal_id"])]))
        report = self.report()
        self.assertIn("entry_after_proposal_expired", report["fill_audit"][0]["issues"])
        self.assertIn("proposal_not_manually_confirmed", report["fill_audit"][0]["issues"])
        self.assertEqual(report["fill_audit"][0]["entry_price_difference"], "0.1")

    def test_paper_export_and_synthetic_evidence_cannot_mix(self):
        self.journal.import_fills(self.csv([fill("synthetic", "buy", 1, 100)]))
        with self.assertRaisesRegex(ManualPaperError, "cannot share"):
            self.journal.import_fills(self.csv([fill("claimed-export", "buy", 1, 100, minute=2, data_kind="paper_export")]))
        self.assertEqual(len(self.report()["fills"]), 1)

    def test_confirmation_timing_is_explicit_without_inventing_pretrade_approval(self):
        self.journal.add_proposal(proposal(), now=NOW)
        proposal_id = proposal()["proposal_id"]
        rows = [fill("before-ack", "buy", 1, 100, minute=1, proposal_id=proposal_id),
                fill("at-ack", "buy", 1, 100, minute=2, proposal_id=proposal_id),
                fill("after-ack", "buy", 1, 100, minute=3, proposal_id=proposal_id)]
        self.journal.import_fills(self.csv(rows))
        pending = self.journal.report(now=NOW+timedelta(minutes=3))
        for audit in pending["fill_audit"]:
            self.assertIsNone(audit["human_confirmation_at"])
            self.assertEqual(audit["confirmation_timing"], "not_confirmed")
            self.assertIn("proposal_not_manually_confirmed", audit["issues"])
        # Recording a decision later does not rewrite the fill's timestamp or
        # claim that the human necessarily authorized it before execution.
        confirmed = self.journal.set_status(proposal_id, "confirmed", now=NOW+timedelta(minutes=2))
        report = self.report()
        self.assertEqual([row["confirmation_timing"] for row in report["fill_audit"]],
                         ["after_fill", "at_fill", "before_fill"])
        for audit in report["fill_audit"]:
            self.assertEqual(audit["human_confirmation_at"], confirmed["status_at"])
            self.assertEqual(audit["issues"], [])
        self.assertEqual([row["filled_at"] for row in report["fills"]],
                         [row["filled_at"] for row in pending["fills"]])

    def test_oversized_linked_entry_is_preserved_and_flagged(self):
        self.journal.add_proposal(proposal(), now=NOW)
        proposal_id = proposal()["proposal_id"]
        self.journal.set_status(proposal_id, "confirmed", now=NOW)
        self.journal.import_fills(self.csv([
            fill("oversized", "buy", 3000, 100, proposal_id=proposal_id)]))
        report = self.report()
        audit = report["fill_audit"][0]
        self.assertEqual(audit["issues"], ["entry_quantity_exceeds_proposal"])
        self.assertEqual(audit["proposed_entry_quantity"], "3")
        self.assertEqual(audit["linked_entry_quantity_to_date"], "3000")
        self.assertEqual(audit["entry_quantity_excess"], "2997")
        self.assertEqual(report["open_position"]["quantity"], "3000")

    def test_partial_fills_accumulate_per_proposal_without_reset_on_exit_or_reimport(self):
        self.journal.add_proposal(proposal(quantity="0.3"), now=NOW)
        proposal_id = proposal()["proposal_id"]
        self.journal.set_status(proposal_id, "confirmed", now=NOW)
        rows = [fill("partial-one", "buy", ".1", 100, minute=1, proposal_id=proposal_id),
                fill("partial-two", "buy", ".2", 100, minute=2, proposal_id=proposal_id),
                fill("close", "sell", ".3", 101, minute=3, proposal_id=proposal_id),
                fill("extra-entry", "buy", ".01", 100, minute=4, proposal_id=proposal_id)]
        path = self.csv(list(reversed(rows)))
        self.journal.import_fills(path)
        first = self.report()
        audit = {row["fill_id"]: row for row in first["fill_audit"]}
        self.assertEqual(audit["partial-one"]["linked_entry_quantity_to_date"], "0.1")
        self.assertEqual(audit["partial-two"]["linked_entry_quantity_to_date"], "0.3")
        self.assertEqual(audit["partial-two"]["entry_quantity_excess"], "0")
        self.assertNotIn("entry_quantity_exceeds_proposal", audit["partial-two"]["issues"])
        self.assertIsNone(audit["close"]["linked_entry_quantity_to_date"])
        self.assertEqual(audit["extra-entry"]["linked_entry_quantity_to_date"], "0.31")
        self.assertEqual(audit["extra-entry"]["entry_quantity_excess"], "0.01")
        self.assertIn("entry_quantity_exceeds_proposal", audit["extra-entry"]["issues"])
        self.journal.import_fills(path)
        second = self.report()
        self.assertEqual(first["fill_audit"], second["fill_audit"])
        self.assertEqual(first["net_realized_pnl"], second["net_realized_pnl"])

    def test_entry_quantity_audit_keeps_different_proposals_and_unlinked_fills_separate(self):
        first_id, second_id = "first", "second"
        for proposal_id in (first_id, second_id):
            self.journal.add_proposal(proposal(proposal_id=proposal_id, quantity=1), now=NOW)
            self.journal.set_status(proposal_id, "confirmed", now=NOW)
        self.journal.import_fills(self.csv([
            fill("first-buy", "buy", 1, 100, minute=1, proposal_id=first_id),
            fill("second-buy", "buy", 1, 100, minute=2, proposal_id=second_id),
            fill("unlinked", "buy", 100, 100, minute=3)]))
        audit = self.report()["fill_audit"]
        self.assertEqual(audit[0]["linked_entry_quantity_to_date"], "1")
        self.assertEqual(audit[1]["linked_entry_quantity_to_date"], "1")
        self.assertEqual(audit[0]["issues"], [])
        self.assertEqual(audit[1]["issues"], [])
        self.assertEqual(audit[2]["issues"], ["no_proposal_link"])
        self.assertIsNone(audit[2]["linked_entry_quantity_to_date"])
        self.assertIsNone(audit[2]["confirmation_timing"])

    def test_rejects_bad_csv_without_partial_writes(self):
        cases = [{"quantity": "nan"}, {"price": "Infinity"}, {"fee": "-0.01"},
                 {"side": "short"}, {"symbol": "SPY"}, {"sequence": "1.5"},
                 {"filled_at": "2020-01-02 15:00:00"}, {"data_kind": "verified_broker"},
                 {"fill_id": "=spreadsheet_formula"}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ManualPaperError):
                self.journal.import_fills(self.csv([fill("valid", "buy", 1, 100), {**fill("bad", "buy", 1, 100, minute=2), **changes}]))
            self.assertEqual(self.report()["fills"], [])
        path = self.root / "malformed.csv"
        path.write_text("fill_id,fill_id\na,b\n")
        with self.assertRaisesRegex(ManualPaperError, "headers"):
            self.journal.import_fills(path)

    def test_future_fill_and_report_before_latest_fill_are_rejected(self):
        with self.assertRaisesRegex(ManualPaperError, "future"):
            self.journal.import_fills(self.csv([
                fill("future", "buy", 1, 100, filled_at="9999-01-01T00:00:00+00:00")]))
        self.assertEqual(self.report()["fills"], [])
        self.journal.import_fills(self.csv([fill("b", "buy", 1, 100)]))
        with self.assertRaisesRegex(ManualPaperError, "precede imported fills"):
            self.journal.report(now=NOW)

    def test_concurrent_reimports_preserve_exactly_one_execution(self):
        path = self.csv([fill("same", "buy", 1, 100)])
        first = ManualPaperJournal(self.path)
        second = ManualPaperJournal(self.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda journal: journal.import_fills(path), [first, second]))
        self.assertEqual(sum(result["inserted"] for result in results), 1)
        self.assertEqual(sum(result["duplicates_skipped"] for result in results), 1)
        self.assertEqual(len(self.report()["fills"]), 1)

    def test_committed_fixture_is_labeled_synthetic_and_has_expected_accounting(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "manual-fills.csv"
        self.journal.import_fills(path)
        report = self.journal.report()
        self.assertEqual(report["data_kind"], "synthetic")
        self.assertEqual(report["net_realized_pnl"], "49.4")
        self.assertEqual(report["open_position"]["quantity"], "2")
        self.assertEqual(report["open_position"]["cost_including_remaining_entry_fees"], "220.2")
        self.assertFalse(report["broker_verified"])

    def test_corrupt_database_is_rejected_without_reinitialization(self):
        broken = self.root / "broken.sqlite3"
        contents = b"this is not a database"
        broken.write_bytes(contents)
        with self.assertRaises(ManualPaperError):
            ManualPaperJournal(broken)
        self.assertEqual(broken.read_bytes(), contents)

    def test_corrupt_stored_payload_is_rejected(self):
        self.journal.import_fills(self.csv([fill("buy", "buy", 1, 100)]))
        with sqlite3.connect(self.path) as conn:
            payload = json.loads(conn.execute("SELECT payload FROM fills").fetchone()[0])
            payload["quantity"] = "NaN"
            conn.execute("UPDATE fills SET payload=?", (json.dumps(payload),))
        with self.assertRaises(ManualPaperError):
            self.report()
        with self.assertRaises(ManualPaperError):
            ManualPaperJournal(self.path)

    def test_symlink_journal_is_rejected(self):
        target = self.root / "another.sqlite3"
        target.symlink_to(self.path)
        with self.assertRaisesRegex(ManualPaperError, "symbolic link"):
            ManualPaperJournal(target)


if __name__ == "__main__":
    unittest.main()
