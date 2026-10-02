"""Calendar semantics of native-paper coverage, independent of strategy prices."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dwight.data import Session
from dwight.manual_campaign import coverage


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OPEN = datetime(2020, 1, 2, 14, 30, tzinfo=UTC)
CLOSE = OPEN + timedelta(minutes=390)
REGULAR = Session("2020-01-02", OPEN, CLOSE)


def stamp(value):
    return value.isoformat(timespec="microseconds")


def bar(close, *, delay=60, first_seen=None):
    return {"timestamp": stamp(close - timedelta(minutes=5)), "available_at": stamp(close),
            "first_seen_at": stamp(first_seen or close + timedelta(seconds=delay)),
            "session": REGULAR.date, "payload_sha256": "a" * 64}


def signal(index, available, *, delay=60, initially_eligible=True, accepted=True, direction=1):
    return {"signal_id": f"{index:064x}", "signal_time": stamp(available - timedelta(minutes=5)),
            "available_at": stamp(available), "first_seen_at": stamp(available + timedelta(seconds=delay)),
            "initially_eligible": initially_eligible, "accepted": accepted, "direction": direction}


def observation(sequence, observed_at, status):
    return {"sequence": sequence, "observed_at": stamp(observed_at), "status": status,
            "latest_bar_close": None, "new_bars": 0, "new_signal_count": 0}


def snapshot(*, bars=(), signals=(), observations=()):
    return {"schema_version": 1, "store_instance_id": "instance", "strategy_identity": "b" * 64,
            "feed": "sip", "symbol": "QQQ", "bars": list(bars), "signals": list(signals),
            "observations": list(observations),
            "policy": {"settle_seconds": 60, "max_age_seconds": 120, "interval_seconds": 300,
                       "regular_session_minutes": 390, "maximum_observable_bars_per_regular_session": 77,
                       "closing_bar_observable": False},
            "cutoff_status": {"latest_observation": None, "halted": False,
                              "data_error": False, "stopped": False}}


class ManualCoverageTests(unittest.TestCase):
    def measure(self, data=None, *, since=OPEN, until=CLOSE, sessions=(REGULAR,)):
        with patch("dwight.manual_campaign.exchange_sessions", return_value=list(sessions)) as calendar:
            result = coverage(data or snapshot(), since, until)
        calendar.assert_called_once_with(since.astimezone(NY).date(), until.astimezone(NY).date())
        return result

    def test_regular_session_separates_open_time_from_77_observation_opportunities(self):
        bars = [bar(OPEN + timedelta(minutes=5 * i)) for i in range(1, 78)]
        result = self.measure(snapshot(bars=bars))
        self.assertEqual(result["elapsed_hours"], 6.5)
        self.assertEqual(result["exchange_open_minutes"], 390)
        self.assertEqual(result["supported_open_minutes"], 390)
        self.assertEqual(result["expected_observation_bars"], 77)
        self.assertEqual(result["timely_observed_bars"], 77)
        self.assertEqual(result["covered_bar_minutes"], 385)
        self.assertEqual(result["missing_timely_bars"], 0)
        self.assertEqual(result["excluded_session_closing_bars"], 1)
        self.assertEqual(result["excluded_early_close_sessions"], [])
        self.assertIn("not continuous service uptime", result["coverage_note"])

    def test_real_no_signal_window_still_counts_bar_coverage(self):
        result = self.measure(snapshot(bars=[bar(OPEN + timedelta(minutes=5))]),
                              until=OPEN + timedelta(minutes=6))
        self.assertEqual(result["forward_signal_count"], 0)
        self.assertEqual(result["initially_eligible_long_signals"], 0)
        self.assertEqual(result["expected_observation_bars"], 1)
        self.assertEqual(result["timely_observed_bars"], 1)
        self.assertEqual(result["missing_timely_bars"], 0)

    def test_partial_start_and_end_count_only_settled_closes(self):
        since = OPEN + timedelta(minutes=7)
        until = OPEN + timedelta(minutes=16)
        bars = [bar(OPEN + timedelta(minutes=10)), bar(OPEN + timedelta(minutes=15))]
        result = self.measure(snapshot(bars=bars), since=since, until=until)
        self.assertEqual(result["exchange_open_minutes"], 9)
        self.assertEqual(result["expected_observation_bars"], 2)
        self.assertEqual(result["timely_observed_bars"], 2)
        self.assertEqual(result["covered_bar_minutes"], 10)
        # Covered bar minutes represent completed data intervals, not nine minutes of uptime.
        self.assertGreater(result["covered_bar_minutes"], result["exchange_open_minutes"])
        before_settle = self.measure(snapshot(bars=bars), since=since, until=until - timedelta(microseconds=1))
        self.assertEqual(before_settle["expected_observation_bars"], 1)
        self.assertEqual(before_settle["timely_observed_bars"], 1)

    def test_close_at_window_start_is_an_opportunity_only_after_settlement(self):
        since = OPEN + timedelta(minutes=5)
        almost = self.measure(since=since, until=since + timedelta(seconds=59, microseconds=999999))
        exact = self.measure(snapshot(bars=[bar(since)]), since=since, until=since + timedelta(seconds=60))
        self.assertEqual(almost["expected_observation_bars"], 0)
        self.assertEqual(exact["expected_observation_bars"], 1)
        self.assertEqual(exact["timely_observed_bars"], 1)

    def test_duplicate_bar_receipts_do_not_multiply_coverage(self):
        first = bar(OPEN + timedelta(minutes=5))
        second = bar(OPEN + timedelta(minutes=10))
        result = self.measure(snapshot(bars=[first, dict(first), second, dict(second)]),
                              until=OPEN + timedelta(minutes=11))
        self.assertEqual(result["expected_observation_bars"], 2)
        self.assertEqual(result["timely_observed_bars"], 2)
        self.assertEqual(result["covered_bar_minutes"], 10)
        self.assertEqual(result["missing_timely_bars"], 0)

    def test_catchup_before_start_never_becomes_forward_bar_or_signal(self):
        since = OPEN + timedelta(minutes=8)
        before = OPEN + timedelta(minutes=5)
        forward = OPEN + timedelta(minutes=10)
        old = bar(before, first_seen=since + timedelta(seconds=30))
        data = snapshot(bars=[old, dict(old), bar(forward)], signals=[
            signal(1, before, delay=210, initially_eligible=False), signal(2, forward)])
        result = self.measure(data, since=since, until=OPEN + timedelta(minutes=11))
        self.assertEqual(result["catchup_bars"], 1)
        self.assertEqual(result["timely_observed_bars"], 1)
        self.assertEqual(result["late_observed_bars"], 0)
        self.assertEqual(result["forward_signal_count"], 1)
        self.assertEqual(result["initially_eligible_long_signals"], 1)

    def test_timely_window_includes_60_seconds_and_excludes_120_seconds(self):
        first, second, third = [OPEN + timedelta(minutes=value) for value in (5, 10, 15)]
        result = self.measure(snapshot(bars=[bar(first, delay=60), bar(second, delay=119.999999), bar(third, delay=120)]),
                              until=OPEN + timedelta(minutes=17))
        self.assertEqual(result["expected_observation_bars"], 3)
        self.assertEqual(result["timely_observed_bars"], 2)
        self.assertEqual(result["late_observed_bars"], 1)
        self.assertEqual(result["missing_timely_bars"], 1)

    def test_receipt_exactly_at_cutoff_counts_but_receipt_after_cutoff_does_not(self):
        close = OPEN + timedelta(minutes=5)
        until = close + timedelta(seconds=60)
        exact = self.measure(snapshot(bars=[bar(close)], signals=[signal(1, close)]), until=until)
        after = self.measure(snapshot(bars=[bar(close, delay=60.000001)],
                                      signals=[signal(2, close, delay=60.000001)]), until=until)
        self.assertEqual((exact["timely_observed_bars"], exact["forward_signal_count"]), (1, 1))
        self.assertEqual((after["timely_observed_bars"], after["forward_signal_count"]), (0, 0))
        self.assertEqual(after["missing_timely_bars"], 1)

    def test_weekend_elapsed_time_is_not_exchange_exposure(self):
        saturday = datetime(2020, 1, 4, 14, 30, tzinfo=UTC)
        result = self.measure(since=saturday, until=saturday + timedelta(hours=24), sessions=())
        self.assertEqual(result["elapsed_hours"], 24)
        self.assertEqual(result["exchange_open_minutes"], 0)
        self.assertEqual(result["supported_open_minutes"], 0)
        self.assertEqual(result["expected_observation_bars"], 0)
        self.assertEqual(result["missing_timely_bars"], 0)

    def test_early_close_open_minutes_are_disclosed_as_unsupported(self):
        opened = datetime(2020, 11, 27, 14, 30, tzinfo=UTC)
        early = Session("2020-11-27", opened, opened + timedelta(minutes=210))
        result = self.measure(since=opened, until=early.close, sessions=(early,))
        self.assertEqual(result["exchange_open_minutes"], 210)
        self.assertEqual(result["supported_open_minutes"], 0)
        self.assertEqual(result["expected_observation_bars"], 0)
        self.assertEqual(result["excluded_early_close_sessions"], [early.date])
        self.assertEqual(result["excluded_session_closing_bars"], 0)

    def test_forward_signals_and_errors_are_counted_within_the_window(self):
        close = OPEN + timedelta(minutes=5)
        until = OPEN + timedelta(minutes=12)
        data = snapshot(signals=[signal(1, close),
                                 signal(2, close, accepted=False),
                                 signal(3, close, direction=-1, accepted=False),
                                 signal(4, close, initially_eligible=False),
                                 signal(5, OPEN - timedelta(minutes=5), delay=700),
                                 signal(6, until, delay=60)],
                        observations=[observation(1, OPEN - timedelta(seconds=1), "error_abstain"),
                                      observation(2, OPEN, "error_abstain"),
                                      observation(3, OPEN + timedelta(minutes=1), "data_revision_requires_review"),
                                      observation(4, until, "stopped"),
                                      observation(5, until, "market_closed"),
                                      observation(6, until + timedelta(microseconds=1), "error_abstain")])
        result = self.measure(data, until=until)
        self.assertEqual(result["forward_signal_count"], 4)
        self.assertEqual(result["initially_eligible_long_signals"], 1)
        self.assertEqual(result["error_or_stop_observations"], 3)
        self.assertEqual(result["cutoff_status"], data["cutoff_status"])

    def test_closing_bar_is_not_an_observable_opportunity_after_market_close(self):
        result = self.measure(snapshot(bars=[bar(CLOSE)]), until=CLOSE + timedelta(minutes=5))
        self.assertEqual(result["exchange_open_minutes"], 390)
        self.assertEqual(result["expected_observation_bars"], 77)
        self.assertEqual(result["excluded_session_closing_bars"], 1)
        self.assertEqual(result["timely_observed_bars"], 0)
        self.assertEqual(result["late_observed_bars"], 0)

    def test_reversed_window_is_rejected_instead_of_negative_elapsed_time(self):
        with patch("dwight.manual_campaign.exchange_sessions", return_value=[REGULAR]):
            with self.assertRaises(ValueError):
                coverage(snapshot(), CLOSE, OPEN)


if __name__ == "__main__":
    unittest.main()
