"""Shadow lifecycle checks with mocked market-data reads; never use credentials."""
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.data import Session
from dwight.experiments import FEATURE_NAMES, FEATURE_VERSION
from dwight.shadow import ShadowMonitor
from vwap_bot.engine import Config

UTC = timezone.utc
OPEN = datetime(2025, 11, 26, 14, 30, tzinfo=UTC)
SESSION = Session("2025-11-26", OPEN, OPEN + timedelta(minutes=390))


def minute_rows(count):
    return [{"t": (OPEN + timedelta(minutes=i)).isoformat(), "o": 100, "h": 102,
             "l": 99, "c": 101, "v": 1000 + i} for i in range(count)]


class _CandidateEveryBar:
    """Deterministic candidate producer isolates monitor persistence from strategy."""
    def __init__(self, *args, **kwargs):
        self.candidates = []

    def feed(self, bar):
        self.candidates.append({"signal_time": bar.timestamp.isoformat(),
                                "available_at": (bar.timestamp + timedelta(minutes=5)).isoformat(),
                                "taken": True, "probability": .75})


@contextmanager
def fixture(*, synthetic=False, rows=None):
    with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
        root = Path(temp)
        release = root / "release"
        release.mkdir()
        artifact = {"schema_version": 1, "kind": "logistic_regression",
                    "feature_version": FEATURE_VERSION, "feature_names": list(FEATURE_NAMES),
                    "mean": [0.] * len(FEATURE_NAMES), "scale": [1.] * len(FEATURE_NAMES),
                    "coefficients": [0.] * len(FEATURE_NAMES), "intercept": 0.,
                    "synthetic": synthetic, "threshold": .5, "strategy": asdict(Config()),
                    "symbol": "QQQ", "source": "synthetic" if synthetic else "alpaca",
                    "feed": "synthetic" if synthetic else "iex", "adjustment": "raw"}
        policy = {"mode": "shadow", "bar_settle_seconds": 60, "max_bar_delay_seconds": 120,
                  "poll_seconds": 30, "stop_file": str(root / "STOP")}
        manifest = {"synthetic": synthetic, "symbol": "QQQ", "feed": "iex", "mode": "shadow"}
        for name, content in (("model.json", artifact), ("policy.json", policy), ("release.json", manifest)):
            (release / name).write_text(json.dumps(content))
        stack.enter_context(patch("dwight.shadow.verify_release", return_value=manifest))
        calendar = stack.enter_context(patch("dwight.shadow.exchange_sessions", return_value=[SESSION]))
        reader = stack.enter_context(patch("dwight.shadow.fetch_alpaca_bars",
                                           return_value={"QQQ": rows or minute_rows(12)}))
        stack.enter_context(patch("dwight.shadow.CandidateBot", _CandidateEveryBar))
        yield release, root / "state", reader, calendar


class ShadowTests(unittest.TestCase):
    def test_catchup_decisions_never_include_later_replay_outcomes(self):
        class Outcomes(_CandidateEveryBar):
            def feed(self, bar):
                super().feed(bar)
                self.candidates[0].update(label=1, net_pnl=999, net_r=100,
                                          label_exit_time='future', label_available_at='future',
                                          simulated_entry=101, equity=99999)

        with fixture() as (release, state, reader, calendar), ShadowMonitor(release, state) as monitor:
            with patch('dwight.shadow.CandidateBot', Outcomes):
                result = monitor.step(OPEN + timedelta(minutes=11))
            outcomes = {'label', 'net_pnl', 'net_r', 'label_exit_time',
                        'label_available_at', 'simulated_entry', 'equity'}
            self.assertTrue(result['decisions'][0]['stale_or_catchup'])
            for decision in result['decisions']:
                self.assertFalse(outcomes.intersection(decision))
                self.assertEqual(decision['probability'], .75)
            stored = [json.loads(row[0]) for row in monitor.db.execute('SELECT payload FROM decisions')]
            self.assertEqual(stored, result['decisions'])

    def test_closed_and_warmup_do_not_fetch_authenticated_data(self):
        with fixture() as (release, state, reader, calendar), ShadowMonitor(release, state) as monitor:
            self.assertEqual(monitor.step(OPEN - timedelta(minutes=1))["status"], "market_closed")
            self.assertEqual(monitor.step(OPEN + timedelta(minutes=5, seconds=59))["status"], "warming_up")
            calendar.return_value = []
            self.assertEqual(monitor.step(OPEN)["status"], "market_closed")
            reader.assert_not_called()

    def test_settle_delay_only_admits_fully_completed_candles(self):
        with fixture() as (release, state, reader, calendar), ShadowMonitor(release, state) as monitor:
            now = OPEN + timedelta(minutes=10, seconds=59)
            result = monitor.step(now)
            self.assertEqual(result["new_bars"], 1)  # 09:35 candle not settled until 09:41.
            self.assertEqual(result["last_bar_close"], (OPEN + timedelta(minutes=5)).isoformat())
            self.assertEqual(result["status"], "late_bar")
            self.assertEqual(result["timely_bar_closes"], [])
            self.assertFalse(result["decisions"][0]["shadow_take"])
            self.assertTrue(result["decisions"][0]["stale_or_catchup"])
            result = monitor.step(OPEN + timedelta(minutes=11))
            self.assertEqual(result["new_bars"], 1)
            self.assertEqual(result["status"], "observed")
            self.assertEqual(result["timely_bar_closes"], [(OPEN+timedelta(minutes=10)).isoformat()])
            self.assertTrue(result["decisions"][0]["shadow_take"])
            self.assertFalse(result["submits_orders"])
            reader.assert_called_with(SESSION.open, OPEN + timedelta(minutes=11), ("QQQ",), "iex")

    def test_restart_does_not_duplicate_bars_or_decisions(self):
        with fixture(rows=minute_rows(6)) as (release, state, reader, calendar):
            with ShadowMonitor(release, state) as monitor:
                first = monitor.step(OPEN + timedelta(minutes=6))
                self.assertEqual(len(first["decisions"]), 1)
            with ShadowMonitor(release, state) as monitor:
                second = monitor.step(OPEN + timedelta(minutes=6, seconds=30))
                self.assertEqual(second["status"], "waiting_for_bar")
                self.assertNotIn("decisions", second)
                self.assertEqual(monitor.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 1)
                self.assertEqual(monitor.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)

    def test_vendor_correction_abstains_before_persisting_new_decisions(self):
        with fixture() as (release, state, reader, calendar), ShadowMonitor(release, state) as monitor:
            monitor.step(OPEN + timedelta(minutes=6))
            changed = minute_rows(12)
            changed[0]["v"] = 123456
            reader.return_value = {"QQQ": changed}
            result = monitor.step(OPEN + timedelta(minutes=11))
            self.assertEqual(result["status"], "data_revision_requires_review")
            self.assertFalse(result["submits_orders"])
            self.assertNotIn("decisions", result)
            self.assertEqual(monitor.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 1)
            self.assertEqual(monitor.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)

    def test_final_candle_flushes_after_close_before_monitor_closes(self):
        with fixture(rows=minute_rows(390)) as (release, state, reader, calendar), ShadowMonitor(release, state) as monitor:
            before_close = monitor.step(SESSION.close - timedelta(minutes=1))
            self.assertEqual(before_close["new_bars"], 77)
            final = monitor.step(SESSION.close + timedelta(minutes=1))
            self.assertEqual(final["status"], "observed")
            self.assertEqual(final["new_bars"], 1)
            self.assertEqual(final["last_bar_close"], SESSION.close.isoformat())
            self.assertEqual(monitor.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 78)
            self.assertFalse(final["submits_orders"])
            reader.reset_mock()
            closed = monitor.step(SESSION.close + timedelta(minutes=2, seconds=30))
            self.assertEqual(closed["status"], "market_closed")
            reader.assert_not_called()

    def test_revision_halt_survives_reverted_provider_and_restart(self):
        with fixture() as (release, state, reader, calendar):
            with ShadowMonitor(release, state) as monitor:
                monitor.step(OPEN + timedelta(minutes=6))
                corrected = minute_rows(12)
                corrected[0]["v"] = 55555
                reader.return_value = {"QQQ": corrected}
                first = monitor.step(OPEN + timedelta(minutes=11))
                self.assertEqual(first["status"], "data_revision_requires_review")
                reader.return_value = {"QQQ": minute_rows(12)}
                reader.reset_mock()
                halted = monitor.step(OPEN + timedelta(minutes=11, seconds=30))
                self.assertEqual(halted["status"], "data_revision_requires_review")
                self.assertEqual(halted["revised_bar"], first["revised_bar"])
                reader.assert_not_called()
            with ShadowMonitor(release, state) as restarted:
                halted = restarted.step(OPEN + timedelta(minutes=12))
                self.assertEqual(halted["status"], "data_revision_requires_review")
                self.assertEqual(restarted.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 1)
                self.assertEqual(restarted.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)
                reader.assert_not_called()

    def test_synthetic_release_refuses_before_network_or_state_creation(self):
        with fixture(synthetic=True) as (release, state, reader, calendar):
            with self.assertRaisesRegex(ValueError, "Synthetic"):
                ShadowMonitor(release, state)
            reader.assert_not_called()
            self.assertFalse(state.exists())

    def test_lock_prevents_second_process_using_same_state(self):
        with fixture() as (release, state, reader, calendar), ShadowMonitor(release, state):
            with self.assertRaisesRegex(ValueError, "Another shadow"):
                ShadowMonitor(release, state)
            reader.assert_not_called()

    def test_data_failure_abstains_and_emits_no_candidate(self):
        with fixture() as (release, state, reader, calendar), ShadowMonitor(release, state) as monitor:
            with patch.object(monitor, "step", side_effect=RuntimeError("provider error")):
                result = monitor.run(once=True)
            self.assertEqual(result["status"], "error_abstain")
            self.assertEqual(result["error_type"], "RuntimeError")
            self.assertFalse(result["submits_orders"])
            self.assertNotIn("provider error", json.dumps(result))
            self.assertEqual(monitor.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
