"""Native-paper milestone integration with invented local evidence only.

The real observer, SQLite stores, journal, campaign and renderers run together.
Only market-data/calendar reads and explicit clock-boundary tests are mocked.
Every account attachment and paper_export row below is a test fixture, never
evidence of a real account, execution, trading outcome, or sent message.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import csv
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from dwight import manual, manual_campaign as campaign
from dwight.data import Session
from dwight.manual import ACCOUNT, ManualPaperJournal
from dwight.manual_signals import ManualSignalStore


UTC = timezone.utc
OPEN = datetime(2020, 1, 2, 14, 30, tzinfo=UTC)
NOW = OPEN+timedelta(minutes=6)
RECIPIENTS = ["first@example.test", "second@example.test"]


def sessions(first, last):
    result = []
    day = first
    while day <= last:
        if day.weekday() < 5:
            opened = datetime.combine(day, time(14, 30), tzinfo=UTC)
            result.append(Session(day.isoformat(), opened, opened+timedelta(minutes=390)))
        day += timedelta(days=1)
    return result


class ManualCampaignTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.signals = self.root/"signals"
        self.journal_path = self.root/"paper.sqlite3"
        self.journal = ManualPaperJournal(self.journal_path)
        with ManualSignalStore(self.signals):
            pass
        self.rows = [{"t": (OPEN+timedelta(minutes=i)).isoformat(), "o": 100,
                      "h": 101, "l": 99, "c": 100, "v": 1000} for i in range(6)]
        self.reader = self.enterContext(patch("dwight.manual_signals.fetch_alpaca_bars",
                                              side_effect=lambda *args: {"QQQ": self.rows}))
        self.enterContext(patch("dwight.manual_signals.exchange_sessions", side_effect=sessions))
        self.enterContext(patch("dwight.manual_campaign.exchange_sessions", side_effect=sessions))
        self.attachment = self.root/"INVENTED-ACCOUNT-TEST-FIXTURE.txt"
        self.attachment.write_text("TEST FIXTURE ONLY: invented operator account values, not live account evidence.\n")
        self.serial = 0

    def prepared(self, *, recipients=RECIPIENTS, allocation="1000", now=NOW):
        self.serial += 1
        directory = self.root/f"campaign-{self.serial}"
        result = campaign.initialize(directory, self.signals, self.journal_path,
                                     recipients, allocation, now=now)
        return directory, result

    def observe(self, now=NOW):
        with ManualSignalStore(self.signals) as store:
            result = store.observe(now=now)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["signals"], [])
        return result

    def account_payload(self, **changes):
        result = {
            "schema_version": 1, "account": ACCOUNT, "account_alias": "INVENTED TEST ACCOUNT",
            "observed_at": NOW.isoformat(), "equity_usd": "10000", "cash_usd": "10000",
            "qqq_quantity": "0", "qqq_open_orders": 0, "chart_symbol": "QQQ",
            "chart_timeframe": "5Min", "chart_feed": "INVENTED TEST CHART FEED",
            "chart_realtime": True, "observation_feed": "sip",
            "feed_comparison_note": "Invented fixture; no live feed or account has been inspected.",
            "checks": {name: True for name in campaign.CHECKS},
            "evidence_files": [str(self.attachment)],
        }
        result.update(changes)
        self.assertEqual(set(result), campaign.ACCOUNT_FIELDS)
        return result

    def account(self, directory, *, payload=None, now=NOW, **changes):
        path = self.root/"account-fixture.json"
        path.write_text(json.dumps(payload if payload is not None else self.account_payload(**changes)))
        return campaign.record_account(directory, path, now=now)

    def started(self, *, recipients=RECIPIENTS):
        directory, _ = self.prepared(recipients=recipients)
        self.observe()
        account = self.account(directory)
        result = campaign.start(directory, account["snapshot_id"], now=NOW)
        self.assertEqual(result["status"], "observing")
        return directory

    def import_rows(self, rows):
        path = self.root/"INVENTED-PAPER-EXPORT-TEST-FIXTURE.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return self.journal.import_fills(path)

    def fill(self, identifier, side, quantity, price, *, hour=1, data_kind="paper_export"):
        return {"fill_id": identifier, "filled_at": (NOW+timedelta(hours=hour)).isoformat(),
                "sequence": "0", "symbol": "QQQ", "side": side, "quantity": str(quantity),
                "price": str(price), "fee": "0", "data_kind": data_kind, "proposal_id": ""}

    def milestone(self, directory, hours, *, now=None):
        artifact = campaign.report(directory, hours, now=now or NOW+timedelta(hours=hours))
        return artifact, json.loads(Path(artifact["json"]).read_text())

    def test_preparation_never_starts_clock_and_deadlines_are_fixed_only_on_start(self):
        directory, initial = self.prepared()
        self.assertEqual(initial["status"], "prepared")
        self.assertIsNone(initial["started_at"])
        before = campaign.status(directory, now=NOW+timedelta(days=20))
        self.assertIsNone(before["started_at"])
        self.assertEqual(before["due_hours"], [])
        self.assertTrue(all(row["due_at"] is None for row in before["milestones"]))
        self.observe()
        account = self.account(directory)
        self.assertFalse(account["broker_verified"])
        self.assertEqual(account["source"], "operator_supplied")
        active = campaign.start(directory, account["snapshot_id"], now=NOW)
        self.assertEqual(active["started_at"], campaign.stamp(NOW))
        self.assertEqual([row["hours"] for row in active["milestones"]], [12, 24, 48, 168])
        self.assertEqual([row["due_at"] for row in active["milestones"]],
                         [campaign.stamp(NOW+timedelta(hours=hour)) for hour in campaign.HOURS])
        with self.assertRaisesRegex(ValueError, "already started"):
            campaign.start(directory, account["snapshot_id"], now=NOW+timedelta(seconds=1))
        plan = json.loads((directory/"plan.json").read_text())
        self.assertEqual(plan["model"], "none-baseline")
        self.assertFalse(plan["manual_risk_policy"]["enforced_by_software"])
        self.assertEqual(plan["journal_instance_id"], self.journal.report(now=NOW)["journal_instance_id"])

    def test_start_requires_actual_durable_timely_observation_and_rejects_catchup(self):
        directory, _ = self.prepared()
        account = self.account(directory)
        with self.assertRaisesRegex(ValueError, "fresh durable"):
            campaign.start(directory, account["snapshot_id"], now=NOW)
        # A real observed bar first received at age120 is a catch-up record.
        self.observe(now=NOW+timedelta(seconds=60))
        with self.assertRaisesRegex(ValueError, "timely bar"):
            campaign.start(directory, account["snapshot_id"], now=NOW+timedelta(seconds=60))
        self.assertIsNone(campaign.status(directory, now=NOW+timedelta(seconds=60))["started_at"])

    def test_stale_future_account_capture_time_and_frozen_alias_are_checked(self):
        directory, _ = self.prepared()
        self.observe()
        stale = self.account(directory, observed_at=(NOW-timedelta(seconds=301)).isoformat())
        with self.assertRaisesRegex(ValueError, "five minutes"):
            campaign.start(directory, stale["snapshot_id"], now=NOW)
        with self.assertRaisesRegex(ValueError, "future dated"):
            self.account(directory, observed_at=(NOW+timedelta(seconds=1)).isoformat())
        captured_later = self.account(directory, now=NOW+timedelta(seconds=10))
        with self.assertRaisesRegex(ValueError, "precede capture"):
            campaign.start(directory, captured_later["snapshot_id"], now=NOW)
        campaign.start(directory, captured_later["snapshot_id"], now=NOW+timedelta(seconds=10))
        with self.assertRaisesRegex(ValueError, "alias"):
            self.account(directory, now=NOW+timedelta(seconds=20), account_alias="DIFFERENT TEST ACCOUNT")
        with self.assertRaisesRegex(ValueError, "precede experiment preparation"):
            self.account(directory, now=NOW-timedelta(seconds=1),
                         observed_at=(NOW-timedelta(seconds=1)).isoformat())

    def test_holdings_orders_realtime_and_review_checkboxes_gate_start(self):
        self.observe()
        false_checks = {name: True for name in campaign.CHECKS}
        false_checks["manual_exits_ready"] = False
        for changes in ({"qqq_quantity": "1"}, {"qqq_open_orders": 1},
                        {"chart_realtime": False}, {"checks": false_checks}):
            with self.subTest(changes=changes):
                directory, _ = self.prepared()
                account = self.account(directory, **changes)
                with self.assertRaisesRegex(ValueError, "flat QQQ"):
                    campaign.start(directory, account["snapshot_id"], now=NOW)
                self.assertIsNone(campaign.status(directory, now=NOW)["started_at"])

    def test_allocation_requires_both_supplied_cash_and_equity(self):
        self.observe()
        for changes in ({"equity_usd": "999"}, {"cash_usd": "999"}):
            with self.subTest(changes=changes):
                directory, _ = self.prepared()
                account = self.account(directory, **changes)
                with self.assertRaisesRegex(ValueError, "allocation exceeds"):
                    campaign.start(directory, account["snapshot_id"], now=NOW)
        for value in ("0", "-1", "nan", True):
            with self.subTest(allocation=value), self.assertRaises(ValueError):
                self.prepared(allocation=value)

    def test_account_schema_and_boolean_review_values_are_strict(self):
        directory, _ = self.prepared()
        good = self.account_payload()
        missing = copy.deepcopy(good)
        del missing["checks"]["risk_policy_reviewed"]
        bad_check = copy.deepcopy(good)
        bad_check["checks"]["risk_policy_reviewed"] = "true"
        for payload in ({**good, "unknown": "extra"}, {**good, "schema_version": True},
                        {**good, "qqq_open_orders": True}, {**good, "chart_realtime": 1},
                        {**good, "chart_symbol": "SPY"}, {**good, "chart_timeframe": "1Min"},
                        {**good, "observation_feed": "iex"}, missing, bad_check):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.account(directory, payload=payload)

    def test_all_cumulative_deadlines_freeze_unknown_no_fill_reports(self):
        directory = self.started()
        with self.assertRaisesRegex(ValueError, "not due"):
            campaign.report(directory, 12, now=NOW+timedelta(hours=12)-timedelta(microseconds=1))
        original_fetch_count = self.reader.call_count
        for hour in campaign.HOURS:
            artifact, value = self.milestone(directory, hour)
            self.assertEqual(value["window_start"], campaign.stamp(NOW))
            self.assertEqual(value["window_end"], campaign.stamp(NOW+timedelta(hours=hour)))
            self.assertEqual(value["coverage"]["elapsed_hours"], hour)
            self.assertLess(value["coverage"]["exchange_open_minutes"], hour*60)
            self.assertEqual(value["imported_accounting"]["fill_count"], 0)
            self.assertIsNone(value["imported_accounting"]["net_realized_pnl"])
            self.assertIsNone(value["account_equity"])
            self.assertFalse(value["evidence_complete"])
            self.assertFalse(value["broker_verified"])
            self.assertFalse(value["submits_orders"])
            self.assertFalse(value["sends_email"])
            html = Path(artifact["html"]).read_text()
            self.assertIn("Unknown: no imported execution evidence", html)
            self.assertIn("No imported fills does not prove no trades", html)
            self.assertIn("covered", value["coverage"]["coverage_note"].lower())
            self.assertEqual(campaign.report(directory, hour, now=NOW+timedelta(days=20)), artifact)
            for relative, expected in artifact["files"].items():
                path = directory/artifact["directory"]/relative
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with ZipFile(artifact["bundle"]) as archive:
                self.assertEqual(set(archive.namelist()),
                                 {"index.html", "milestone.json", "email.txt", "fills/report.html", "fills/report.json"})
        self.assertEqual(self.reader.call_count, original_fetch_count)
        self.assertEqual(campaign.status(directory, now=NOW+timedelta(hours=168))["due_hours"], [])

    def test_event_cutoffs_and_late_import_preserve_old_report_and_disclose_later_updates(self):
        directory = self.started()
        self.import_rows([self.fill("first-buy", "buy", 1, 100, hour=1),
                          self.fill("first-exit", "sell", 1, 110, hour=10),
                          self.fill("future-buy", "buy", 1, 200, hour=13),
                          self.fill("future-exit", "sell", 1, 190, hour=25)])
        first, first_value = self.milestone(directory, 12)
        self.assertEqual(first_value["imported_accounting"]["fill_count"], 2)
        self.assertEqual(first_value["imported_accounting"]["net_realized_pnl"], "10")
        original_json, original_html = Path(first["json"]).read_bytes(), Path(first["html"]).read_bytes()
        # These backfilled fake rows change a newly recomputed earlier FIFO
        # result, but must not replace the already frozen 12-hour report.
        self.import_rows([self.fill("late-buy", "buy", 1, 80, hour=2),
                          self.fill("late-exit", "sell", 1, 90, hour=3)])
        self.assertEqual(campaign.report(directory, 12, now=NOW+timedelta(hours=24)), first)
        self.assertEqual(Path(first["json"]).read_bytes(), original_json)
        self.assertEqual(Path(first["html"]).read_bytes(), original_html)
        _, next_value = self.milestone(directory, 24)
        self.assertEqual(next_value["imported_accounting"]["fill_count"], 5)
        self.assertEqual(next_value["imported_accounting"]["net_realized_pnl"], "20")
        self.assertEqual(next_value["prior_report_evidence_updates"], [{
            "hours": 12, "additional_execution_records_through_prior_cutoff": 2,
            "original_report_unchanged": True}])
        for hour in (48, 168):
            _, value = self.milestone(directory, hour)
            self.assertEqual(value["imported_accounting"]["fill_count"], 6)
            self.assertEqual(value["imported_accounting"]["net_realized_pnl"], "10")
            self.assertEqual(value["window_start"], campaign.stamp(NOW))

    def test_synthetic_and_pre_start_fills_cannot_become_native_paper_evidence(self):
        directory = self.started()
        self.import_rows([self.fill("invented", "buy", 1, 100, data_kind="synthetic")])
        with self.assertRaisesRegex(ValueError, "synthetic fills"):
            self.milestone(directory, 12)
        self.assertIsNone(campaign.status(directory, now=NOW+timedelta(hours=12))["milestones"][0]["report"])
        # Test the independent opening-inventory guard using another real
        # dedicated source/journal/campaign after the synthetic case.
        self.journal_path = self.root/"second-paper.sqlite3"
        self.journal = ManualPaperJournal(self.journal_path)
        second, _ = self.prepared()
        evidence = self.account(second)
        campaign.start(second, evidence["snapshot_id"], now=NOW)
        self.import_rows([self.fill("before-start", "buy", 1, 100, hour=-1)])
        with self.assertRaisesRegex(ValueError, "pre-start fills"):
            self.milestone(second, 12)

    def test_recreated_journal_and_same_identity_backup_rollback_are_detected(self):
        directory = self.started()
        backup = self.journal_path.read_bytes()
        self.import_rows([self.fill("reported", "buy", 1, 100)])
        self.milestone(directory, 12)
        self.journal_path.write_bytes(backup)
        with self.assertRaisesRegex(ValueError, "previously reported journal evidence"):
            self.milestone(directory, 24)
        self.journal_path.unlink()
        ManualPaperJournal(self.journal_path)
        with self.assertRaisesRegex(ValueError, "journal identity changed"):
            self.milestone(directory, 24)

    def test_recreated_observation_source_is_rejected(self):
        directory = self.started()
        replacement = self.root/"replacement-signals"
        with ManualSignalStore(replacement):
            pass
        (replacement/"manual-signals.sqlite3").replace(self.signals/"manual-signals.sqlite3")
        with self.assertRaisesRegex(ValueError, "source, feed or strategy changed"):
            self.milestone(directory, 12)

    def test_plan_and_archived_account_file_tampering_fail_closed(self):
        directory, _ = self.prepared()
        self.observe()
        account = self.account(directory)
        with sqlite3.connect(directory/"manual-campaign.sqlite3") as connection:
            payload = json.loads(connection.execute("SELECT payload FROM accounts").fetchone()[0])
        attachment = directory/payload["files"][0]["file"]
        original = attachment.read_bytes()
        attachment.write_text("TAMPERED TEST DATA")
        with self.assertRaisesRegex(ValueError, "account evidence changed"):
            campaign.start(directory, account["snapshot_id"], now=NOW)
        attachment.write_bytes(original)
        campaign.start(directory, account["snapshot_id"], now=NOW)
        attachment.write_text("TAMPERED AFTER START")
        with self.assertRaisesRegex(ValueError, "account evidence changed"):
            self.milestone(directory, 12)
        attachment.write_bytes(original)
        plan_path = directory/"plan.json"
        plan_path.write_bytes(plan_path.read_bytes()+b" ")
        with self.assertRaisesRegex(ValueError, "plan file differs"):
            campaign.status(directory, now=NOW)

    def test_recipient_claims_and_receipts_are_independent_durable_and_idempotent(self):
        directory = self.started(recipients=[*RECIPIENTS, RECIPIENTS[0]])
        now = NOW+timedelta(hours=168)
        due = campaign.report_due(directory, now=now)
        self.assertEqual([row["hours"] for row in due["reports_generated"]], list(campaign.HOURS))
        state = campaign.status(directory, now=now)
        self.assertTrue(all(set(row["deliveries"]) == set(RECIPIENTS) for row in state["milestones"]))
        with self.assertRaises(ValueError):
            campaign.claim_delivery(directory, 12, "unknown@example.test", now=now)
        for hour in campaign.HOURS:
            for recipient in RECIPIENTS:
                claim = campaign.claim_delivery(directory, hour, recipient, now=now)
                self.assertEqual(claim["status"], "sending")
                self.assertFalse(claim["sends_email"])
                self.assertTrue(Path(claim["bundle"]).is_file())
                with self.assertRaisesRegex(ValueError, "awaiting receipt"):
                    campaign.claim_delivery(directory, hour, recipient, now=now)
                with self.assertRaisesRegex(ValueError, "claim does not match"):
                    campaign.confirm_delivery(directory, hour, recipient, "wrong", "fixture-receipt", now=now)
                receipt = f"TEST-NOT-SENT-{hour}-{recipient}"
                confirmed = campaign.confirm_delivery(directory, hour, recipient, claim["claim"], receipt, now=now)
                self.assertFalse(confirmed["idempotent"])
                repeated = campaign.confirm_delivery(directory, hour, recipient, claim["claim"], receipt, now=now)
                self.assertTrue(repeated["idempotent"])
                with self.assertRaisesRegex(ValueError, "conflicting provider receipt"):
                    campaign.confirm_delivery(directory, hour, recipient, claim["claim"], "different", now=now)
        final = campaign.status(directory, now=now)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["pending_delivery_hours"], [])
        self.assertFalse(final["sends_email"])

    def test_frozen_artifact_tamper_prevents_delivery_confirmation(self):
        directory = self.started()
        artifact, _ = self.milestone(directory, 12)
        now = NOW+timedelta(hours=12)
        claim = campaign.claim_delivery(directory, 12, RECIPIENTS[0], now=now)
        Path(artifact["email"]).write_text("TAMPERED REPORT")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            campaign.confirm_delivery(directory, 12, RECIPIENTS[0], claim["claim"], "fixture-receipt", now=now)
        state = campaign.status(directory, now=now)
        self.assertEqual(state["milestones"][0]["deliveries"][RECIPIENTS[0]]["status"], "sending")

    def test_live_start_rechecks_bar_age_after_real_journal_write_lock_wait(self):
        directory, _ = self.prepared()
        self.observe()
        account = self.account(directory)
        current = [NOW+timedelta(seconds=59)]  # Completed bar age119.
        clock_seen, completed = threading.Event(), threading.Event()
        original_clock = campaign.clock

        def clock(value=None):
            if value is None:
                clock_seen.set()
                return original_clock(current[0])
            return original_clock(value)

        def attempt():
            try:
                return campaign.start(directory, account["snapshot_id"])
            finally:
                completed.set()

        blocker = sqlite3.connect(self.journal_path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with patch("dwight.manual_campaign.clock", side_effect=clock), ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(attempt)
                self.assertTrue(clock_seen.wait(3))
                self.assertFalse(completed.wait(.05))
                current[0] = NOW+timedelta(seconds=61)  # Bar stale while blocked.
                blocker.rollback()
                with self.assertRaisesRegex(ValueError, "timely bar"):
                    future.result(timeout=3)
        finally:
            blocker.rollback()
            blocker.close()
        state = campaign.status(directory, now=current[0])
        self.assertIsNone(state["started_at"])
        self.assertTrue(all(row["due_at"] is None for row in state["milestones"]))

    def test_live_report_due_preserves_live_capture_after_journal_lock_wait(self):
        directory = self.started()
        current = [NOW+timedelta(hours=12, seconds=1)]
        clock_seen, completed = threading.Event(), threading.Event()
        campaign_clock, journal_clock = campaign.clock, manual._clock

        def clock(value=None):
            if value is None:
                clock_seen.set()
            return campaign_clock(current[0] if value is None else value)

        def journal_time(value=None):
            return journal_clock(current[0] if value is None else value)

        def generate():
            try:
                result = campaign.report_due(directory)
                self.assertEqual([row["hours"] for row in result["reports_generated"]], [12])
                return result["reports_generated"][0]["artifacts"]
            finally:
                completed.set()

        blocker = sqlite3.connect(self.journal_path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with patch("dwight.manual_campaign.clock", side_effect=clock), \
                    patch("dwight.manual._clock", side_effect=journal_time), ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(generate)
                self.assertTrue(clock_seen.wait(3))
                self.assertFalse(completed.wait(.05))
                current[0] += timedelta(minutes=2)
                blocker.rollback()
                artifact = future.result(timeout=3)
        finally:
            blocker.rollback()
            blocker.close()
        report = json.loads(Path(artifact["json"]).read_text())
        self.assertEqual(report["evidence_captured_at"], campaign.stamp(current[0]))
        self.assertEqual(report["generated_at"], campaign.stamp(current[0]))
        self.assertEqual(report["window_end"], campaign.stamp(NOW+timedelta(hours=12)))


if __name__ == "__main__":
    unittest.main()
