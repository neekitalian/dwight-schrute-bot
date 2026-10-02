"""Real local journal integration; market data is mocked, never an order API."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.data import Session
from dwight.manual import ManualPaperJournal, PROPOSAL_FIELDS
from dwight.manual_signals import ManualSignalError, ManualSignalStore
from vwap_bot.engine import Config


OPEN = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
NOW = OPEN + timedelta(minutes=6)
SESSION = Session("2020-01-02", OPEN, OPEN + timedelta(minutes=390))


def minutes(count=5):
    return [{"t": (OPEN + timedelta(minutes=i)).isoformat(),
             "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.0, "v": 10}
            for i in range(count)]


def candidate(**changes):
    return {"direction": 1, "stop": 99.0, "reason": "baseline impulse/pullback/rejection",
            "signal_time": OPEN.isoformat(), "available_at": (OPEN + timedelta(minutes=5)).isoformat(),
            "feature_version": "test-v1", "features": {"direction": 1, "distance": 1.0},
            "accepted": True, "rejection_reason": None,
            "expires_at": (OPEN + timedelta(minutes=7)).isoformat(), "eligible": True,
            "stale_or_catchup": False, "signal_age_seconds": 60, **changes}


class ManualSignalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS /var is a system symlink; the public store deliberately rejects it.
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.journal_path = self.root / "manual" / "journal.sqlite3"
        self.rows = minutes()
        self.signal = candidate()
        self.session = SESSION
        self.fetch = self.enterContext(patch("dwight.manual_signals.fetch_alpaca_bars",
                                            side_effect=lambda *args: {"QQQ": self.rows}))
        self.calendar = self.enterContext(patch("dwight.manual_signals.exchange_sessions",
                                               side_effect=lambda *args: [self.session]))
        self.inspect = self.enterContext(patch("dwight.manual_signals.signals.inspect_session",
                                              side_effect=lambda *args, **kwargs: {
                                                  "status": "ready",
                                                  "latest_bar_close": (OPEN + timedelta(minutes=5)).isoformat(),
                                                  "signals": [self.signal]}))

    def observe(self, store, now=NOW):
        result = store.observe(now=now)
        self.assertIn(result["status"], {"ready", "waiting_for_bar"}, result)
        return result["signals"][0]

    def prepare(self, store, signal_id, **changes):
        args = {"entry": "100.001", "quantity": "3", "price_observed_at": NOW,
                "journal_path": self.journal_path, "now": NOW}
        args.update(changes)
        return store.prepare(signal_id, **args)

    def test_real_manual_schema_decimal_math_and_explicit_risk_review(self):
        with ManualSignalStore(self.state) as store:
            observed = self.observe(store)
            result = self.prepare(store, observed["signal_id"], entry=Decimal("100.001"), quantity=3)
            proposal = result["proposal"]
            self.assertEqual((proposal["entry"], proposal["stop"], proposal["target"]),
                             ("100.001", "99", "102.01"))
            self.assertEqual(proposal["planned_stop_risk_before_costs"], "3.003")
            self.assertEqual(proposal["planned_notional"], "300.003")
            self.assertEqual(proposal["source"], "vwap_pullback_signal_observer")
            self.assertEqual(proposal["model"], "none-baseline")
            self.assertEqual(proposal["version"], store.identity)
            payload = json.loads(store.db.execute("SELECT payload FROM outbox").fetchone()[0])
            self.assertEqual(set(payload), PROPOSAL_FIELDS)
            self.assertEqual(result["reference"]["price_basis"], "human_supplied_reference")
            self.assertEqual(result["reference"]["canonical_stop"], "99")
            self.assertTrue(result["human_review_required"])
            self.assertFalse(result["account_risk_verified"])
            self.assertFalse(result["portfolio_risk_enforced"])
            self.assertFalse(result["submits_orders"])
            self.assertTrue(result["actionable"])
            json.dumps(result, allow_nan=False)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.journal_path.stat().st_mode & 0o777, 0o600)
            self.fetch.assert_called_once_with(SESSION.open, NOW, ("QQQ",), "sip")
            kwargs = self.inspect.call_args.kwargs
            self.assertEqual((kwargs["settle_seconds"], kwargs["max_age_seconds"]), (60, 120))

    def test_same_bar_poll_restart_and_expiry_never_freshen(self):
        with ManualSignalStore(self.state) as store:
            first = self.observe(store)
            second_result = store.observe(now=NOW + timedelta(seconds=10))
            self.assertEqual(second_result["new_signals"], [])
            self.assertEqual(second_result["new_bars"], 0)
            second = second_result["signals"][0]
            self.assertEqual(first["first_observed_at"], second["first_observed_at"])
            self.assertEqual(first["expires_at"], second["expires_at"])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 6)
        with ManualSignalStore(self.state) as store:
            later = store.observe(now=NOW + timedelta(seconds=60))["signals"][0]
            self.assertEqual(first["signal_id"], later["signal_id"])
            self.assertEqual(first["expires_at"], later["expires_at"])
            self.assertFalse(later["eligible"])
            self.assertTrue(later["expired"])
            self.assertEqual(later["signal_age_seconds"], 120)

    def test_revised_minute_halts_even_if_five_minute_aggregate_is_unchanged(self):
        with ManualSignalStore(self.state) as store:
            signal = self.observe(store)
            # Nonterminal close does not affect this group's five-minute OHLCV.
            self.rows[1]["c"] = 100.5
            result = store.observe(now=NOW + timedelta(seconds=1))
            self.assertEqual(result["status"], "data_revision_requires_review")
            self.rows = minutes()
        with ManualSignalStore(self.state) as store:
            self.fetch.reset_mock()
            result = store.observe(now=NOW + timedelta(seconds=2))
            self.assertEqual(result["status"], "data_revision_requires_review")
            self.fetch.assert_not_called()
            self.assertFalse(store.list_signals(now=NOW + timedelta(seconds=2))[0]["eligible"])
            with self.assertRaisesRegex(ManualSignalError, "revision"):
                self.prepare(store, signal["signal_id"], now=NOW + timedelta(seconds=2))

    def test_disappeared_previously_recorded_minute_permanently_halts(self):
        with ManualSignalStore(self.state) as store:
            self.observe(store)
            self.rows.pop(1)
            result = store.observe(now=NOW + timedelta(seconds=1))
            self.assertEqual(result["status"], "data_revision_requires_review")
            self.assertIn("disappeared", result["revision"]["reason"])

    def test_missing_initial_minute_abstains_without_persisting_signal(self):
        self.rows.pop(1)
        with ManualSignalStore(self.state) as store:
            result = store.observe(now=NOW)
            self.assertEqual(result["status"], "error_abstain")
            self.assertEqual(store.list_signals(now=NOW), [])
            self.inspect.assert_not_called()

    def test_crash_after_journal_commit_recovers_same_proposal_even_after_expiry(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            with patch.object(store, "_acknowledge", side_effect=RuntimeError("simulated crash")):
                with self.assertRaises(RuntimeError):
                    self.prepare(store, signal_id)
            self.assertIsNone(store.db.execute("SELECT delivered_at FROM outbox").fetchone()[0])
            self.assertEqual(len(ManualPaperJournal(self.journal_path).list_proposals(now=NOW)), 1)
        with ManualSignalStore(self.state) as store:
            result = self.prepare(store, signal_id, now=NOW + timedelta(minutes=1))
            self.assertTrue(result["idempotent_retry"])
            self.assertFalse(result["actionable"])
            self.assertFalse(result["price_reference_fresh"])
            self.assertEqual(result["price_reference_age_seconds"], 60)
            self.assertEqual(result["proposal"]["status"], "expired")
            self.assertEqual(len(ManualPaperJournal(self.journal_path).list_proposals(now=NOW + timedelta(minutes=1))), 1)
            self.assertIsNotNone(store.db.execute("SELECT delivered_at FROM outbox").fetchone()[0])

    def test_crash_before_journal_commit_preserves_pending_payload_for_retry(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            with patch("dwight.manual_signals.ManualPaperJournal.add_proposal", side_effect=OSError("private failure")):
                with self.assertRaisesRegex(ManualSignalError, "retry the identical"):
                    self.prepare(store, signal_id)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
            result = self.prepare(store, signal_id, now=NOW + timedelta(seconds=20))
            self.assertTrue(result["actionable"])
            self.assertTrue(result["price_reference_fresh"])
            self.assertEqual(result["price_reference_age_seconds"], 20)
            self.assertTrue(result["idempotent_retry"])
            self.assertEqual(result["proposal"]["entry"], "100.001")

    def test_pending_undelivered_expired_payload_cannot_be_renewed(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            with patch("dwight.manual_signals.ManualPaperJournal.add_proposal", side_effect=OSError):
                with self.assertRaises(ManualSignalError):
                    self.prepare(store, signal_id)
            with self.assertRaisesRegex(ManualSignalError, "stale entry reference"):
                self.prepare(store, signal_id, now=NOW + timedelta(minutes=1))
            payload = json.loads(store.db.execute("SELECT payload FROM outbox").fetchone()[0])
            self.assertEqual(datetime.fromisoformat(payload["expires_at"]), OPEN + timedelta(minutes=7))

    def test_pending_retry_with_stale_reference_cannot_make_first_delivery(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            with patch("dwight.manual_signals.ManualPaperJournal.add_proposal", side_effect=OSError):
                with self.assertRaises(ManualSignalError):
                    self.prepare(store, signal_id)
            original = store.db.execute("SELECT payload,reference,planned_at FROM outbox").fetchone()
            with patch("dwight.manual_signals.ManualPaperJournal.add_proposal") as add:
                with self.assertRaisesRegex(ManualSignalError, "stale entry reference"):
                    self.prepare(store, signal_id, now=NOW + timedelta(seconds=35))
                add.assert_not_called()
            self.assertEqual(original, store.db.execute("SELECT payload,reference,planned_at FROM outbox").fetchone())
            self.assertEqual(ManualPaperJournal(self.journal_path).list_proposals(now=NOW + timedelta(seconds=35)), [])

    def test_stale_retry_does_not_create_missing_journal(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            with patch("dwight.manual_signals.ManualPaperJournal", side_effect=OSError):
                with self.assertRaises(ManualSignalError):
                    self.prepare(store, signal_id)
            self.assertFalse(self.journal_path.exists())
            with self.assertRaisesRegex(ManualSignalError, "stale entry reference"):
                self.prepare(store, signal_id, now=NOW + timedelta(seconds=35))
            self.assertFalse(self.journal_path.exists())

    def test_delivered_retry_with_stale_reference_is_not_actionable_before_signal_expiry(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            original = self.prepare(store, signal_id)
            with patch("dwight.manual_signals.ManualPaperJournal.add_proposal") as add:
                result = self.prepare(store, signal_id, now=NOW + timedelta(seconds=35))
                add.assert_not_called()
            self.assertEqual(result["proposal"], original["proposal"])
            self.assertEqual(result["proposal"]["status"], "pending")
            self.assertFalse(result["expired"])
            self.assertFalse(result["actionable"])
            self.assertFalse(result["price_reference_fresh"])
            self.assertEqual(result["price_reference_age_seconds"], 35)

    def test_retry_conflicts_reject_entry_quantity_timestamp_or_destination(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            self.prepare(store, signal_id)
            for change in ({"entry": "100.002"}, {"quantity": 4},
                           {"price_observed_at": NOW + timedelta(seconds=1)},
                           {"journal_path": self.root / "other.sqlite3"}):
                with self.subTest(change=change), self.assertRaisesRegex(ManualSignalError, "conflicts"):
                    self.prepare(store, signal_id, **change)

    def test_stale_future_and_short_signals_cannot_prepare(self):
        for label, changes in (
                ("stale", {"eligible": False, "stale_or_catchup": True}),
                ("short", {"direction": -1, "accepted": False}),
                ("future", {"available_at": (NOW + timedelta(seconds=1)).isoformat()})):
            with self.subTest(label=label), ManualSignalStore(self.root / label) as store:
                self.signal = candidate(**changes)
                signal_id = self.observe(store)["signal_id"]
                with self.assertRaises(ManualSignalError):
                    self.prepare(store, signal_id)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_reference_rejects_future_stale_invalid_and_fractional_values(self):
        changes = [{"price_observed_at": NOW + timedelta(microseconds=1)},
                   {"price_observed_at": NOW - timedelta(seconds=30)},
                   {"price_observed_at": NOW.replace(tzinfo=None)},
                   {"quantity": "1.5"}, {"quantity": True}, {"quantity": "0"},
                   {"quantity": "1e13"}, {"entry": "nan"}, {"entry": "Infinity"},
                   {"entry": "1e13"}, {"entry": "100.000000001"},
                   {"entry": "99"}, {"entry": "99.01"}, {"entry": "1e-10000000"}]
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            for change in changes:
                with self.subTest(change=change), self.assertRaises(ManualSignalError):
                    self.prepare(store, signal_id, **change)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_canonical_stop_removes_only_float_tick_artifact_and_keeps_original(self):
        self.signal = candidate(stop=99.00000000000001)
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            result = self.prepare(store, signal_id)
            self.assertEqual(result["proposal"]["stop"], "99")
            self.assertEqual(result["reference"]["original_signal_stop"], 99.00000000000001)
            self.assertEqual(result["provenance"]["stop"], 99.00000000000001)

    def test_clock_reversal_rejected_and_catchup_initial_eligibility_frozen(self):
        self.signal = candidate(eligible=False, stale_or_catchup=True)
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            with self.assertRaisesRegex(ManualSignalError, "clock"):
                store.observe(now=NOW - timedelta(seconds=1))
            self.signal = candidate(eligible=True)
            self.observe(store, now=NOW + timedelta(seconds=1))
            with self.assertRaises(ManualSignalError):
                self.prepare(store, signal_id, now=NOW + timedelta(seconds=1))

    def test_busy_writer_is_useful_and_other_store_available_between_polls(self):
        with ManualSignalStore(self.state) as first, ManualSignalStore(self.state) as second:
            observed = self.observe(first)
            self.assertEqual(second.list_signals(now=NOW)[0]["signal_id"], observed["signal_id"])
            with first._writer():
                with self.assertRaisesRegex(ManualSignalError, "another manual signal operation"):
                    second.observe(now=NOW)
                # Foreground CLI initialization and listing must also work
                # during an active worker's network fetch, not just between polls.
                with ManualSignalStore(self.state) as third:
                    self.assertEqual(third.list_signals(now=NOW)[0]["signal_id"], observed["signal_id"])
            self.prepare(second, observed["signal_id"])

    def test_live_clock_uses_fetch_completion_not_request_start_for_freshness(self):
        received = NOW + timedelta(seconds=61)
        with ManualSignalStore(self.state) as store:
            with patch("dwight.manual_signals._clock", side_effect=[NOW, received]):
                result = store.observe()
            signal = result["signals"][0]
            self.assertEqual(datetime.fromisoformat(signal["first_observed_at"]), received)
            self.assertEqual(self.inspect.call_args.kwargs["now"], received)
            self.assertFalse(signal["eligible"])
            self.assertTrue(signal["expired"])
            self.assertEqual(signal["signal_age_seconds"], 121)
            self.assertEqual(datetime.fromisoformat(result["observed_at"]), received)

    def test_live_journal_open_delay_cannot_first_deliver_stale_reference(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            with patch("dwight.manual_signals._clock", side_effect=[NOW, NOW + timedelta(seconds=35)]):
                with patch("dwight.manual_signals.ManualPaperJournal.add_proposal") as add:
                    with self.assertRaisesRegex(ManualSignalError, "stale entry reference"):
                        self.prepare(store, signal_id, now=None)
                    add.assert_not_called()
            self.assertEqual(ManualPaperJournal(self.journal_path).list_proposals(now=NOW + timedelta(seconds=35)), [])
            self.assertIsNone(store.db.execute("SELECT delivered_at FROM outbox").fetchone()[0])

    def test_live_delivery_passes_transaction_deadline_and_rechecks_after_acknowledgement(self):
        real_add = ManualPaperJournal.add_proposal
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            clocks = [NOW, NOW, NOW + timedelta(seconds=20), NOW + timedelta(seconds=35)]
            with patch("dwight.manual_signals._clock", side_effect=clocks):
                with patch("dwight.manual._clock", return_value=NOW.isoformat(timespec="microseconds")):
                    with patch("dwight.manual_signals.ManualPaperJournal.add_proposal", autospec=True,
                               side_effect=lambda journal, payload, **kwargs: real_add(journal, payload, **kwargs)) as add:
                        result = self.prepare(store, signal_id, now=None)
            self.assertIsNone(add.call_args.kwargs["now"])
            self.assertEqual(datetime.fromisoformat(add.call_args.kwargs["create_before"]), NOW + timedelta(seconds=30))
            self.assertEqual(result["proposal"]["status"], "pending")
            self.assertFalse(result["expired"])
            self.assertFalse(result["actionable"])
            self.assertFalse(result["price_reference_fresh"])
            self.assertEqual(result["price_reference_age_seconds"], 35)

    def test_stop_calendar_and_identity_guards(self):
        with ManualSignalStore(self.state) as store:
            observed = self.observe(store)
            (self.state / "STOP").write_text("stop")
            self.assertEqual(store.observe(now=NOW)["status"], "stopped")
            with self.assertRaisesRegex(ManualSignalError, "stopped"):
                self.prepare(store, observed["signal_id"])
            (self.state / "STOP").unlink()
            with patch("dwight.manual_signals.signals.strategy_identity", return_value="changed"):
                with self.assertRaisesRegex(ManualSignalError, "identity changed"):
                    store.observe(now=NOW)
                with self.assertRaisesRegex(ManualSignalError, "identity changed"):
                    self.prepare(store, observed["signal_id"])
        with ManualSignalStore(self.root / "closed") as store:
            self.assertEqual(store.observe(now=SESSION.close)["status"], "market_closed")
        self.session = Session(SESSION.date, OPEN, OPEN + timedelta(minutes=210))
        with ManualSignalStore(self.root / "shortday") as store:
            self.assertEqual(store.observe(now=NOW)["status"], "unsupported_early_close")

    def test_identity_feed_and_path_restrictions(self):
        for kwargs in ({"feed": "other"}, {"config": Config(reward_r=3)}):
            with self.assertRaises(ManualSignalError):
                ManualSignalStore(self.state, **kwargs)
        with ManualSignalStore(self.state):
            pass
        with self.assertRaisesRegex(ManualSignalError, "different feed"):
            ManualSignalStore(self.state, feed="iex")
        (self.root / "linked").symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(ManualSignalError, "symbolic"):
            ManualSignalStore(self.root / "linked" / "child")
        with self.assertRaisesRegex(ManualSignalError, "traversal"):
            ManualSignalStore(self.root / "state" / ".." / "other")

    def test_journal_rejects_reserved_state_sidecars_lock_stop_and_case_variants(self):
        reserved = ["manual-signals.sqlite3", "manual-signals.sqlite3-journal",
                    "manual-signals.sqlite3-wal", "manual-signals.sqlite3-shm",
                    "manual-signals.lock", "STOP",
                    "MANUAL-SIGNALS.SQLITE3-JOURNAL", "Manual-Signals.Sqlite3-Wal", "stop"]
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            original_signal = store.db.execute("SELECT payload FROM signals").fetchone()
            for name in reserved:
                with self.subTest(name=name), self.assertRaisesRegex(ManualSignalError, "reserved"):
                    self.prepare(store, signal_id, journal_path=self.state / name)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
                self.assertEqual(store.db.execute("SELECT payload FROM signals").fetchone(), original_signal)
                self.assertEqual(store.db.execute("PRAGMA quick_check").fetchone(), ("ok",))
                self.assertTrue(store.path.exists())
            self.assertFalse((self.state / "STOP").exists())

    def test_provider_exception_payload_is_never_stored_or_returned(self):
        self.fetch.side_effect = RuntimeError("private-key-secret HTTP body")
        with ManualSignalStore(self.state) as store:
            result = store.observe(now=NOW)
            self.assertEqual(result["status"], "error_abstain")
            self.assertNotIn("private-key-secret", json.dumps(result))
            self.assertNotIn(b"private-key-secret", store.path.read_bytes())

    def test_later_provider_failure_blocks_old_signal_until_successful_recheck(self):
        with ManualSignalStore(self.state) as store:
            signal_id = self.observe(store)["signal_id"]
            self.fetch.side_effect = RuntimeError("private provider body")
            self.assertEqual(store.observe(now=NOW)["status"], "error_abstain")
            self.assertFalse(store.list_signals(now=NOW)[0]["eligible"])
            with self.assertRaises(ManualSignalError):
                self.prepare(store, signal_id)
            self.fetch.side_effect = lambda *args: {"QQQ": self.rows}
            self.assertTrue(self.observe(store)["eligible"])
            self.assertTrue(self.prepare(store, signal_id)["actionable"])


if __name__ == "__main__":
    unittest.main()
