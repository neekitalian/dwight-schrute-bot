"""Historical execution cutoffs preserve event time and honest evidence scope."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import csv
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from dwight import manual
from dwight.manual import ManualPaperError, ManualPaperJournal
from dwight.manual_reporting import render_manual_report
from tests.test_manual import NOW, fill, proposal


class ManualCutoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root/"journal.sqlite3"
        self.journal = ManualPaperJournal(self.path)

    def csv(self, rows, name="fills.csv"):
        path = self.root/name
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def imported(self, rows):
        return self.journal.import_fills(self.csv(rows))

    def snapshot(self, minute, *, capture_minute=10):
        return self.journal.snapshot_at(NOW+timedelta(minutes=minute),
                                        now=NOW+timedelta(minutes=capture_minute))

    def test_actual_fifo_keeps_all_opening_history_and_excludes_future_exits(self):
        self.imported([fill("opening", "buy", 2, 100, ".2", minute=-60),
                       fill("second-buy", "buy", 3, 110, ".3", minute=1),
                       fill("first-exit", "sell", 3, 120, ".3", minute=2),
                       fill("future-exit", "sell", 2, 115, ".2", minute=3)])
        snapshot = self.snapshot(2)
        self.assertEqual([row["fill_id"] for row in snapshot["fills"]],
                         ["opening", "second-buy", "first-exit"])
        self.assertEqual(snapshot["net_realized_pnl"], "49.4")
        self.assertEqual(snapshot["fees_imported"], "0.8")
        self.assertEqual(snapshot["open_position"]["quantity"], "2")
        self.assertEqual(snapshot["open_position"]["cost_including_remaining_entry_fees"], "220.2")
        self.assertEqual(snapshot["closed_exit_count"], 1)
        self.assertEqual(len(snapshot["fifo_matches"]), 2)
        self.assertEqual(self.snapshot(3)["net_realized_pnl"], "59")
        self.assertEqual(self.snapshot(3)["open_position"]["quantity"], "0")
        self.assertEqual(snapshot["as_of"], snapshot["cutoff"])
        self.assertEqual(snapshot["cutoff"], manual._clock(NOW+timedelta(minutes=2)))
        self.assertIsNone(snapshot["account_equity"])
        self.assertIsNone(snapshot["account_return_pct"])
        self.assertFalse(snapshot["broker_verified"])

    def test_cutoff_includes_every_execution_at_exact_timestamp_in_sequence_order(self):
        self.imported([fill("opening", "buy", 2, 100, minute=1),
                       fill("exit-one", "sell", 1, 110, minute=2, sequence=0),
                       fill("exit-two", "sell", 1, 105, minute=2, sequence=1),
                       fill("future-buy", "buy", 1, 90, minute=3)])
        report = self.snapshot(2)
        self.assertEqual([row["fill_id"] for row in report["fills"]],
                         ["opening", "exit-one", "exit-two"])
        self.assertEqual(report["net_realized_pnl"], "15")
        self.assertEqual(report["closed_exit_count"], 2)

    def test_later_confirmation_does_not_change_historical_status_or_audit(self):
        source = proposal()
        self.journal.add_proposal(source, now=NOW)
        self.imported([fill("buy", "buy", 3, 100, minute=1,
                            proposal_id=source["proposal_id"])])
        self.journal.set_status(source["proposal_id"], "confirmed", now=NOW+timedelta(minutes=4))
        # Existing report() intentionally keeps its live-clock behavior.
        with self.assertRaisesRegex(ManualPaperError, "clock"):
            self.journal.report(now=NOW+timedelta(minutes=3))
        historical = self.snapshot(3)
        self.assertEqual(historical["proposals"][0]["status"], "pending")
        self.assertEqual(historical["proposals"][0]["status_at"], manual._clock(NOW))
        self.assertEqual([event["status"] for event in historical["proposal_events"]], ["pending"])
        audit = historical["fill_audit"][0]
        self.assertIn("proposal_not_manually_confirmed", audit["issues"])
        self.assertIsNone(audit["human_confirmation_at"])
        self.assertEqual(audit["confirmation_timing"], "not_confirmed")
        at_confirmation = self.snapshot(4)
        self.assertEqual(at_confirmation["proposals"][0]["status"], "confirmed")
        self.assertEqual(at_confirmation["fill_audit"][0]["confirmation_timing"], "after_fill")
        self.assertNotIn("proposal_not_manually_confirmed", at_confirmation["fill_audit"][0]["issues"])
        self.assertEqual([event["status"] for event in at_confirmation["proposal_events"]],
                         ["pending", "confirmed"])

    def test_natural_expiry_is_a_read_only_view_with_no_fabricated_event(self):
        self.journal.add_proposal(proposal(), now=NOW)
        before = self.path.read_bytes()
        before_mtime = self.path.stat().st_mtime_ns
        snapshot = self.snapshot(5)
        view = snapshot["proposals"][0]
        self.assertEqual(view["status"], "expired")
        self.assertEqual(view["status_at"], view["expires_at"])
        self.assertEqual(view["status_basis"], "derived_expiry")
        self.assertEqual([event["status"] for event in snapshot["proposal_events"]], ["pending"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, before_mtime)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM proposals").fetchone()[0], "pending")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        # Later durable expiry must not leak its recorded timestamp backward.
        self.journal.list_proposals(now=NOW+timedelta(minutes=9))
        replayed = self.snapshot(5)
        self.assertEqual(replayed["proposals"], snapshot["proposals"])
        self.assertEqual(replayed["proposal_events"], snapshot["proposal_events"])

    def test_late_import_is_captured_without_inventing_import_time(self):
        frozen_empty = self.snapshot(2, capture_minute=3)
        original_clock = manual._clock
        with patch("dwight.manual._clock", side_effect=lambda now=None:
                   original_clock(NOW+timedelta(minutes=8) if now is None else now)):
            self.imported([fill("late-evidence", "buy", 1, 100, minute=1)])
        current = self.snapshot(2, capture_minute=10)
        self.assertEqual(frozen_empty["fills"], [])
        self.assertEqual(len(current["fills"]), 1)
        self.assertEqual(current["snapshot_basis"],
                         "executions_through_cutoff_evidence_captured_at_generation")
        self.assertEqual(current["import_provenance"], "not_recorded")
        self.assertTrue(current["knowledge_at_cutoff_unknown"])
        self.assertEqual(current["captured_at"], manual._clock(NOW+timedelta(minutes=10)))
        self.assertNotIn("imported_at", current["fills"][0])
        self.assertNotIn("known_at", current["fills"][0])
        self.assertTrue(any("late imports" in line for line in current["limitations"]))

    def test_future_created_proposal_excluded_and_original_older_fill_link_rejected(self):
        later = NOW+timedelta(minutes=4)
        future_proposal = proposal(signal_at=later.isoformat(), available_at=later.isoformat(),
                                   expires_at=(later+timedelta(minutes=5)).isoformat())
        self.journal.add_proposal(future_proposal, now=later)
        snapshot = self.snapshot(2)
        self.assertEqual(snapshot["proposals"], [])
        self.assertEqual(snapshot["proposal_events"], [])
        self.imported([fill("older-fill", "buy", 1, 100, minute=1,
                            proposal_id=future_proposal["proposal_id"])])
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ManualPaperError, "proposal unavailable at cutoff"):
            self.snapshot(2)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        with sqlite3.connect(self.path) as connection:
            original = json.loads(connection.execute("SELECT payload FROM fills").fetchone()[0])
        self.assertEqual(original["proposal_id"], future_proposal["proposal_id"])

    def test_capture_clock_is_sampled_after_sqlite_read_snapshot_acquired(self):
        original_db, original_clock = self.journal._snapshot_db, manual._clock
        inside, capture_samples = [], []

        @contextmanager
        def watched_db():
            with original_db() as connection:
                self.assertTrue(connection.in_transaction)
                inside.append(True)
                try:
                    yield connection
                finally:
                    inside.pop()

        def clock(value=None):
            if value is None:
                self.assertTrue(inside)
                capture_samples.append(True)
                value = NOW+timedelta(minutes=10)
            return original_clock(value)

        with patch.object(self.journal, "_snapshot_db", watched_db), patch("dwight.manual._clock", clock):
            snapshot = self.journal.snapshot_at(NOW)
        self.assertEqual(capture_samples, [True])
        self.assertEqual(snapshot["captured_at"], original_clock(NOW+timedelta(minutes=10)))

    def test_concurrent_writer_cannot_mix_proposal_events_and_fill_versions(self):
        source = proposal()
        self.journal.add_proposal(source, now=NOW)
        self.imported([fill("opening", "buy", 2, 100, minute=1, proposal_id=source["proposal_id"])])
        writer_journal = ManualPaperJournal(self.path)
        new_fill_path = self.csv([fill("concurrent-exit", "sell", 1, 110, minute=2)], "later.csv")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        read_started, writer_committed = threading.Event(), threading.Event()
        original_read = self.journal._read_proposals

        def pause_after_proposals(connection):
            rows = original_read(connection)
            read_started.set()
            self.assertTrue(writer_committed.wait(5), "writer did not commit during the read snapshot")
            return rows

        def writer():
            self.assertTrue(read_started.wait(5))
            writer_journal.set_status(source["proposal_id"], "confirmed", now=NOW+timedelta(minutes=2))
            writer_journal.import_fills(new_fill_path)
            writer_committed.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(writer)
            with patch.object(self.journal, "_read_proposals", pause_after_proposals):
                frozen = self.snapshot(3)
            future.result(timeout=5)
        self.assertEqual(frozen["proposals"][0]["status"], "pending")
        self.assertEqual([event["status"] for event in frozen["proposal_events"]], ["pending"])
        self.assertEqual([row["fill_id"] for row in frozen["fills"]], ["opening"])
        self.assertEqual(frozen["net_realized_pnl"], "0")
        current = self.snapshot(3)
        self.assertEqual(current["proposals"][0]["status"], "confirmed")
        self.assertEqual(len(current["fills"]), 2)
        self.assertEqual(current["net_realized_pnl"], "10")

    def test_empty_cutoff_renders_unknown_activity_and_preserves_frozen_metadata(self):
        self.imported([fill("after-cutoff", "buy", 1, 100, minute=3)])
        snapshot = self.snapshot(2)
        self.assertEqual(snapshot["data_kind"], "no_fills")
        self.assertEqual(snapshot["evidence_status"], "no_imported_fills_through_cutoff")
        rendered = render_manual_report(snapshot, self.root/"report")
        saved = json.loads(Path(rendered["json"]).read_text())
        self.assertEqual(saved, snapshot)
        html = Path(rendered["html"]).read_text()
        self.assertIn("Account results and experiment activity are unknown", html)
        self.assertIn("Account equity: unknown", html)
        self.assertIn("Not measured", html)
        self.assertIn("does not establish that no trading occurred", html)
        self.assertNotIn("0 USD", html)

    def test_corrupt_event_history_refused_without_repairing_state(self):
        source = proposal()
        self.journal.add_proposal(source, now=NOW)
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM events")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ManualPaperError, "event history"):
            self.snapshot(2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cutoff_validation_and_missing_database_do_not_create_new_evidence(self):
        for cutoff in (None, "2020-01-02T15:00:00Z", NOW.replace(tzinfo=None),
                       NOW+timedelta(days=1)):
            with self.subTest(cutoff=cutoff), self.assertRaises(ManualPaperError):
                self.journal.snapshot_at(cutoff, now=NOW+timedelta(minutes=10))
        self.path.unlink()
        with self.assertRaisesRegex(ManualPaperError, "snapshot is invalid or unavailable"):
            self.snapshot(2)
        self.assertFalse(self.path.exists())

    def test_journal_identity_is_stable_across_restart_and_unique_per_journal(self):
        first = self.journal.report(now=NOW)["journal_instance_id"]
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        restarted = ManualPaperJournal(self.path)
        self.assertEqual(restarted.report(now=NOW)["journal_instance_id"], first)
        self.assertEqual(restarted.snapshot_at(NOW, now=NOW)["journal_instance_id"], first)
        independent = ManualPaperJournal(self.root/"other.sqlite3")
        self.assertNotEqual(independent.report(now=NOW)["journal_instance_id"], first)
        # Reusing the pathname for a newly initialized ledger gets a new ID.
        self.path.unlink()
        recreated = ManualPaperJournal(self.path)
        self.assertNotEqual(recreated.report(now=NOW)["journal_instance_id"], first)

    def test_legacy_identity_upgrade_is_additive_but_snapshot_never_performs_it(self):
        self.journal.add_proposal(proposal(), now=NOW)
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE journal_identity")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ManualPaperError, "identity is missing or invalid"):
            self.snapshot(2)
        self.assertEqual(self.path.read_bytes(), before)
        upgraded = ManualPaperJournal(self.path)
        report = upgraded.snapshot_at(NOW, now=NOW)
        self.assertRegex(report["journal_instance_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(len(report["proposals"]), 1)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT version FROM metadata").fetchall(), [(1,)])
        self.assertEqual(ManualPaperJournal(self.path).report(now=NOW)["journal_instance_id"],
                         report["journal_instance_id"])

    def test_empty_or_malformed_existing_identity_is_rejected_not_regenerated(self):
        for invalid in (None, "wrong", "A"*32):
            with self.subTest(invalid=invalid):
                with sqlite3.connect(self.path) as connection:
                    connection.execute("DELETE FROM journal_identity")
                    if invalid is not None:
                        connection.execute("INSERT INTO journal_identity VALUES(1, ?)", (invalid,))
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ManualPaperError, "identity is missing or invalid"):
                    self.snapshot(2)
                with self.assertRaisesRegex(ManualPaperError, "identity is missing or invalid"):
                    ManualPaperJournal(self.path)
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
