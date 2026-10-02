"""Read-only, cutoff-bound evidence for a separate native-paper milestone ledger."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from dwight.data import Session
from dwight.manual_signals import ManualSignalError, ManualSignalStore, read_observation_snapshot
from tests.test_manual_signals import candidate, minutes


OPEN = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
NOW = OPEN + timedelta(minutes=6)
SESSION = Session("2020-01-02", OPEN, OPEN + timedelta(minutes=390))


class ObservationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.rows = minutes()
        self.fetch = self.enterContext(patch("dwight.manual_signals.fetch_alpaca_bars",
                                            side_effect=lambda *args: {"QQQ": self.rows}))
        self.calendar = self.enterContext(patch("dwight.manual_signals.exchange_sessions", return_value=[SESSION]))

    def snapshot(self, **kwargs):
        return read_observation_snapshot(self.state, cutoff=kwargs.pop("cutoff", NOW),
                                         now=kwargs.pop("now", NOW), **kwargs)

    def one_signal(self, store, now=NOW, **changes):
        record = candidate(**changes)
        with patch("dwight.manual_signals.signals.inspect_session", return_value={
                "status": "ready", "latest_bar_close": (OPEN + timedelta(minutes=5)).isoformat(),
                "signals": [record]}):
            return store.observe(now=now)

    def test_real_no_signal_session_has_observations_and_77_policy_bars(self):
        self.rows = minutes(385)
        observed_at = OPEN + timedelta(minutes=386)
        with ManualSignalStore(self.state) as store:
            result = store.observe(now=observed_at)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["signals"], [])
        evidence = self.snapshot(cutoff=observed_at, now=observed_at)
        self.assertEqual(len(evidence["bars"]), 77)
        self.assertEqual(evidence["signals"], [])
        self.assertEqual(len(evidence["observations"]), 1)
        self.assertEqual(evidence["latest_observation"]["new_bars"], 77)
        self.assertEqual(evidence["policy"]["regular_session_minutes"], 390)
        self.assertEqual(evidence["policy"]["maximum_observable_bars_per_regular_session"], 77)
        self.assertFalse(evidence["policy"]["closing_bar_observable"])
        self.assertFalse(evidence["policy"]["bar_coverage_is_uptime"])

    def test_catchup_keeps_availability_distinct_from_first_seen(self):
        receipt = NOW + timedelta(seconds=50)
        with ManualSignalStore(self.state) as store:
            self.one_signal(store, now=receipt, eligible=False, stale_or_catchup=True, signal_age_seconds=110)
        evidence = self.snapshot(start=NOW, cutoff=receipt, now=receipt)
        self.assertEqual(len(evidence["signals"]), 1)
        signal = evidence["signals"][0]
        self.assertEqual(datetime.fromisoformat(signal["available_at"]), OPEN + timedelta(minutes=5))
        self.assertEqual(datetime.fromisoformat(signal["first_seen_at"]), receipt)
        self.assertFalse(signal["initially_eligible"])
        self.assertLess(datetime.fromisoformat(signal["available_at"]), NOW)
        self.assertEqual(evidence["bars"][0]["first_seen_at"], signal["first_seen_at"])

    def test_repeated_polls_deduplicate_and_old_cutoff_prefix_is_stable(self):
        later = NOW + timedelta(seconds=10)
        with ManualSignalStore(self.state) as store:
            self.one_signal(store)
            first = self.snapshot()
            self.one_signal(store, now=later)
            old = self.snapshot(now=later)
            latest = self.snapshot(cutoff=later, now=later)
        self.assertEqual(first["prefix_sha256"], old["prefix_sha256"])
        self.assertEqual(first["sequence_watermark"], old["sequence_watermark"])
        self.assertEqual(len(old["observations"]), 1)
        self.assertEqual(len(latest["observations"]), 2)
        self.assertEqual(len(latest["bars"]), 1)
        self.assertEqual(len(latest["signals"]), 1)
        self.assertNotEqual(first["prefix_sha256"], latest["prefix_sha256"])
        self.assertEqual(old["cutoff_status"]["latest_observation"]["observed_at"], first["latest_observation"]["observed_at"])
        self.assertEqual(old["current_status"]["latest_observation"]["observed_at"], latest["latest_observation"]["observed_at"])

    def test_atomic_observation_failure_cannot_leave_phantom_bars_or_signals(self):
        with ManualSignalStore(self.state) as store:
            real = store._record_status

            def fail_success(status, now, **details):
                if status == "ready":
                    raise RuntimeError("simulated failure after bars were inserted")
                return real(status, now, **details)

            with patch.object(store, "_record_status", side_effect=fail_success):
                result = self.one_signal(store)
            self.assertEqual(result["status"], "error_abstain")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        evidence = self.snapshot()
        self.assertEqual(evidence["bars"], [])
        self.assertEqual(evidence["signals"], [])
        self.assertEqual(evidence["latest_observation"]["status"], "error_abstain")

    def test_uuid_stable_after_restart_and_different_for_new_store(self):
        with ManualSignalStore(self.state) as store:
            store.observe(now=NOW)
            instance = store.store_instance_id
        with ManualSignalStore(self.state) as store:
            self.assertEqual(instance, store.store_instance_id)
        self.assertEqual(self.snapshot()["store_instance_id"], instance)
        with ManualSignalStore(self.root / "replacement") as store:
            self.assertNotEqual(instance, store.store_instance_id)

    def test_later_data_error_and_revision_are_current_not_retroactive(self):
        error_at, halt_at = NOW + timedelta(seconds=10), NOW + timedelta(seconds=20)
        with ManualSignalStore(self.state) as store:
            store.observe(now=NOW)
            original = self.snapshot()
            self.fetch.side_effect = RuntimeError("secret response")
            store.observe(now=error_at)
            old = self.snapshot(now=error_at)
            self.assertFalse(old["cutoff_status"]["data_error"])
            self.assertTrue(old["current_status"]["data_error"])
            self.assertEqual(old["prefix_sha256"], original["prefix_sha256"])
            self.fetch.side_effect = lambda *args: {"QQQ": self.rows}
            self.rows[1]["c"] = 100.5
            store.observe(now=halt_at)
            (self.state / "STOP").write_text("stop")
        old = self.snapshot(now=halt_at)
        current = self.snapshot(cutoff=halt_at, now=halt_at)
        self.assertFalse(old["cutoff_status"]["halted"])
        self.assertTrue(old["current_status"]["halted"])
        self.assertTrue(old["current_status"]["stopped"])
        self.assertFalse(old["cutoff_status"]["stopped"])
        self.assertTrue(current["cutoff_status"]["halted"])
        self.assertNotIn("secret response", json.dumps(current))

    def test_reader_does_not_fetch_create_mutate_or_advance_clock(self):
        with ManualSignalStore(self.state) as store:
            store.observe(now=NOW)
            clock_before = store._meta("last_clock")
            database = store.path.read_bytes()
            mode = store.path.stat().st_mode
            children = sorted(path.name for path in self.state.iterdir())
            self.fetch.reset_mock()
            self.calendar.reset_mock()
            evidence = store.evidence_snapshot(cutoff=NOW, now=NOW + timedelta(days=2))
            self.fetch.assert_not_called()
            self.calendar.assert_not_called()
            self.assertEqual(store._meta("last_clock"), clock_before)
            self.assertEqual(store.path.read_bytes(), database)
            self.assertEqual(store.path.stat().st_mode, mode)
            self.assertEqual(sorted(path.name for path in self.state.iterdir()), children)
            evidence["bars"].clear()
            self.assertEqual(len(self.snapshot()["bars"]), 1)
        missing = self.root / "missing"
        with self.assertRaisesRegex(ManualSignalError, "does not create"):
            read_observation_snapshot(missing, now=NOW)
        self.assertFalse(missing.exists())

    def test_price_free_whitelist_has_no_features_stop_prices_references_or_destinations(self):
        with ManualSignalStore(self.state) as store:
            self.one_signal(store, stop=98765.4321, features={"secret_feature": 98765.4321}, reason="private reason")
        evidence = self.snapshot()
        text = json.dumps(evidence)
        for secret in ("98765.4321", "secret_feature", "private reason", str(self.state)):
            self.assertNotIn(secret, text)
        self.assertEqual(set(evidence["bars"][0]), {"timestamp", "available_at", "first_seen_at", "session", "payload_sha256"})
        self.assertEqual(set(evidence["signals"][0]), {"signal_id", "signal_time", "available_at", "first_seen_at",
                                                     "initially_eligible", "accepted", "direction"})

    def test_cutoff_and_timezone_validation(self):
        with ManualSignalStore(self.state):
            pass
        for options in ({"cutoff": NOW + timedelta(seconds=1)},
                        {"start": NOW + timedelta(seconds=1)},
                        {"cutoff": NOW.replace(tzinfo=None)},
                        {"now": NOW.replace(tzinfo=None)}):
            with self.subTest(options=options), self.assertRaises((ManualSignalError, ValueError)):
                self.snapshot(**options)

    def test_missing_uuid_requires_explicit_writer_upgrade_without_reader_mutation(self):
        with ManualSignalStore(self.state) as store:
            store.observe(now=NOW)
            with store.db:
                store.db.execute("DELETE FROM metadata WHERE key='store_instance_id'")
            original = store.path.read_bytes()
            with self.assertRaisesRegex(ManualSignalError, "legacy"):
                self.snapshot()
            self.assertEqual(store.path.read_bytes(), original)
        with ManualSignalStore(self.state) as store:
            self.assertEqual(self.snapshot()["store_instance_id"], store.store_instance_id)

    def test_current_source_guard_and_noncanonical_evidence_reject(self):
        with ManualSignalStore(self.state) as store:
            store.observe(now=NOW)
            with patch("dwight.manual_signals.signals.strategy_identity", return_value="changed"):
                with self.assertRaisesRegex(ManualSignalError, "identity"):
                    self.snapshot()
            with store.db:
                store.db.execute("UPDATE observations SET observed_at=?", (NOW.isoformat(),))
            with self.assertRaisesRegex(ManualSignalError, "canonical"):
                self.snapshot()

    def test_unlinked_legacy_rows_do_not_count_as_completed_evidence(self):
        with ManualSignalStore(self.state) as store:
            self.one_signal(store)
            with store.db:
                store.db.execute("DELETE FROM observations")
        evidence = self.snapshot()
        self.assertEqual(evidence["bars"], [])
        self.assertEqual(evidence["signals"], [])
        self.assertEqual(evidence["integrity"], {"unlinked_bar_count": 1, "unlinked_signal_count": 1})

    def test_read_transaction_is_consistent_when_another_writer_commits_mid_snapshot(self):
        real_connect = sqlite3.connect
        later = NOW + timedelta(seconds=10)
        begin_write, finished = threading.Event(), threading.Event()
        errors = []
        with ManualSignalStore(self.state) as store:
            result = store.observe(now=NOW)
            # WAL permits this writer to commit while the read snapshot stays open.
            store.db.execute("PRAGMA journal_mode=WAL")
            event = {**result, "status": "error_abstain", "observed_at": later.isoformat(timespec="microseconds"),
                     "new_bars": 0, "new_signals": [], "signals": []}

            def writer():
                try:
                    if not begin_write.wait(3):
                        raise RuntimeError("reader never began")
                    with real_connect(store.path) as connection:
                        connection.execute("INSERT OR REPLACE INTO metadata VALUES('data_error','1')")
                        connection.execute("INSERT INTO observations(observed_at,status,payload) VALUES(?,?,?)",
                                           (event["observed_at"], event["status"],
                                            json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            class SnapshotConnection:
                def __init__(self, connection):
                    self.connection = connection

                def execute(self, query, *args):
                    cursor = self.connection.execute(query, *args)
                    if query == "SELECT key,value FROM metadata":
                        begin_write.set()
                        if not finished.wait(3):
                            raise RuntimeError("concurrent writer did not finish")
                    return cursor

                def close(self):
                    self.connection.close()

            thread = threading.Thread(target=writer)
            thread.start()
            try:
                with patch("dwight.manual_signals.sqlite3.connect",
                           side_effect=lambda *args, **kwargs: SnapshotConnection(real_connect(*args, **kwargs))):
                    snapshot = self.snapshot(cutoff=later, now=later)
            finally:
                begin_write.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(snapshot["current_status"]["data_error"])
            self.assertEqual(len(snapshot["observations"]), 1)
            current = self.snapshot(cutoff=later, now=later)
            self.assertTrue(current["current_status"]["data_error"])
            self.assertEqual(len(current["observations"]), 2)


if __name__ == "__main__":
    unittest.main()
