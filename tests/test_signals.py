"""Signal-time causality and calendar eligibility; no broker or data network."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from dwight.data import Session
from dwight.experiments import FEATURE_NAMES, FEATURE_VERSION
from dwight.signals import SignalObserver, inspect_session, strategy_identity
from vwap_bot.engine import Bar, Bot, Config

OPEN = datetime.fromisoformat("2026-09-28T09:30:00-04:00")
SESSION = Session("2026-09-28", OPEN, OPEN+timedelta(minutes=390))


def strategy_bars(direction=1):
    """Real default-rule fixture: confirmed rising pivots, impulse and rejection.

    No strategy method or indicator state is mocked. The signal is on bar 24,
    after two right-hand bars confirm each structural pivot. Reflection around
    100 produces the matching real short setup for the long-only policy test.
    """
    closes = [100, 100.2, 100.6, 101.2, 101.8, 101.3, 100.8, 100.4,
              100.7, 101.3, 102, 102.5, 102, 101.4, 101, 101.2, 101.7,
              102.3, 102.8, 102.4, 101.8, 101.5]
    values = []
    for i, close in enumerate(closes):
        opened = close-.1 if not i or close >= closes[i-1] else close+.1
        values.append((opened, close+.15, close-.15, close))
    values += [(101.5, 104.2, 101.4, 104), (104, 104.1, 102.8, 103.4),
               (103.4, 104.5, 103, 104.3)]
    if direction == -1:
        values = [(200-o, 200-l, 200-h, 200-c) for o, h, l, c in values]
    return [Bar(OPEN+timedelta(minutes=5*i), *value, 1000) for i, value in enumerate(values)]


def feed(bars):
    observer = SignalObserver()
    for bar in bars:
        observer.feed(bar)
    return observer


class SignalObserverTests(unittest.TestCase):
    def test_real_default_strategy_keeps_exact_pending_terms_and_features(self):
        bars = strategy_bars()
        baseline, observer = Bot(), SignalObserver()
        for bar in bars:
            baseline.feed(bar)
            observer.feed(bar)
        self.assertEqual(len(observer.signals), 1)
        signal = observer.signals[0]
        for key in ("direction", "stop", "reason", "signal_time"):
            self.assertEqual(signal[key], baseline.pending[key])
        self.assertEqual(signal["signal_time"], bars[24].timestamp.isoformat())
        self.assertEqual(signal["available_at"], (OPEN+timedelta(minutes=125)).isoformat())
        self.assertEqual(signal["feature_version"], FEATURE_VERSION)
        self.assertEqual(set(signal["features"]), set(FEATURE_NAMES))
        self.assertTrue(signal["accepted"])
        self.assertIsNone(signal["rejection_reason"])
        self.assertIsNone(observer.pending)

    def test_bookkeeping_matches_engine_at_every_prefix_including_after_its_trade(self):
        bars = strategy_bars()
        bars += [Bar(OPEN+timedelta(minutes=5*i), 105, 111, 104, 110, 1200)
                 for i in range(len(bars), 29)]
        baseline, observer = Bot(), SignalObserver()
        for bar in bars:
            baseline.feed(bar)
            observer.feed(bar)
            for key in ("bars", "highs", "lows", "trs", "pv", "vol", "ema"):
                self.assertEqual(getattr(observer, key), getattr(baseline, key), key)
        self.assertTrue(baseline.trades)
        for name in ("equity", "position", "trades", "curve", "losses", "model"):
            self.assertFalse(hasattr(observer, name), name)

    def test_causal_prefixes_and_opposite_future_paths_preserve_signal(self):
        bars = strategy_bars()
        whole = feed(bars)
        for length in range(1, len(bars)+1):
            prefix = feed(bars[:length])
            self.assertEqual(prefix.signals, [signal for signal in whole.signals
                                           if datetime.fromisoformat(signal["signal_time"])
                                           <= bars[length-1].timestamp])
        first, second = feed(bars), feed(bars)
        frozen = copy.deepcopy(first.signals)
        next_time = OPEN+timedelta(minutes=125)
        first.feed(Bar(next_time, 105, 120, 104, 119, 5000))
        second.feed(Bar(next_time, 99, 100, 85, 86, 5000))
        first.finish()
        second.finish()
        self.assertEqual(first.signals[:1], frozen)
        self.assertEqual(second.signals[:1], frozen)
        prohibited = {"entry", "entry_price", "quantity", "label", "label_available_at",
                      "label_exit_time", "net_pnl", "net_r", "equity", "exit", "probability"}
        self.assertFalse(prohibited.intersection(first.signals[0]))

    def test_confirmed_pivots_wait_for_right_bars(self):
        bars, observer = strategy_bars(), SignalObserver()
        for bar in bars[:6]:
            observer.feed(bar)
        self.assertEqual(observer.highs, [])
        observer.feed(bars[6])
        self.assertEqual(observer.highs, [(4, 101.95)])
        for bar in bars[7:23]:
            observer.feed(bar)
        self.assertNotIn((21, 101.35), observer.lows)
        observer.feed(bars[23])
        self.assertIn((21, 101.35), observer.lows)
        self.assertEqual(observer.signals, [])
        observer.feed(bars[24])
        self.assertEqual(len(observer.signals), 1)

    def test_real_short_is_recorded_but_never_accepted(self):
        bars = strategy_bars(-1)
        result = inspect_session(bars, SESSION, now=OPEN+timedelta(minutes=126))
        self.assertEqual(len(result["signals"]), 1)
        short = result["signals"][0]
        self.assertEqual(short["direction"], -1)
        self.assertFalse(short["accepted"])
        self.assertFalse(short["eligible"])
        self.assertEqual(short["rejection_reason"], "long_only_policy")
        self.assertFalse(short["stale_or_catchup"])

    def test_external_snapshot_mutation_cannot_rewrite_history(self):
        observer = feed(strategy_bars())
        expected = observer.signals
        snapshot = observer.signals
        snapshot[0]["stop"] = 1
        snapshot[0]["features"]["direction"] = -1
        snapshot[0]["label"] = 1
        snapshot.clear()
        self.assertEqual(observer.signals, expected)
        json.dumps(observer.signals, allow_nan=False)

    def test_no_execution_path_and_defensive_methods_refuse(self):
        with patch.object(Bot, "_enter", side_effect=AssertionError("must not enter")), \
                patch.object(Bot, "_exit", side_effect=AssertionError("must not exit")):
            observer = feed(strategy_bars())
            observer.pending = {"accidental": "state"}
            observer.feed(Bar(OPEN+timedelta(minutes=125), 105, 106, 104, 105, 1000))
            observer.finish()
        self.assertIsNone(observer.pending)
        with self.assertRaisesRegex(RuntimeError, "cannot enter"):
            observer._enter(strategy_bars()[0])
        with self.assertRaisesRegex(RuntimeError, "cannot exit"):
            observer._exit(100, OPEN, "test")
        with self.assertRaisesRegex(RuntimeError, "no portfolio"):
            observer.stats()

    def test_invalid_engine_stop_abstains_and_clears_pending(self):
        for stop in (float("nan"), float("inf"), 0, -1, 200, True):
            def invalid(bot, bar, prev):
                bot.pending = dict(direction=1, stop=stop, reason="invalid",
                                   signal_time=bar.timestamp.isoformat())
            observer = SignalObserver()
            with patch.object(Bot, "_signal", invalid), self.assertRaisesRegex(ValueError, "stop"):
                observer.feed(strategy_bars()[0])
            self.assertEqual(observer.signals, [])
            self.assertIsNone(observer.pending)

    def test_finite_inputs_that_overflow_indicators_abstain(self):
        observer = SignalObserver()
        huge = replace(strategy_bars()[0], volume=1e308)
        with self.assertRaisesRegex(ValueError, "Nonfinite indicator"):
            observer.feed(huge)
        self.assertEqual(observer.signals, [])

    def test_identity_is_stable_sensitive_to_config_feature_and_source(self):
        first = strategy_identity()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, strategy_identity(Config()))
        self.assertNotEqual(first, strategy_identity(Config(swing=3)))
        with patch("dwight.signals.experiments.FEATURE_VERSION", "changed"):
            self.assertNotEqual(first, strategy_identity())
        with patch("dwight.signals.Path.read_bytes", return_value=b"changed source"):
            self.assertNotEqual(first, strategy_identity())


class SessionInspectionTests(unittest.TestCase):
    def test_settlement_and_expiry_boundaries_are_exact_and_fixed(self):
        bars = strategy_bars()
        close = OPEN+timedelta(minutes=125)
        with self.assertRaisesRegex(ValueError, "not yet settled"):
            inspect_session(bars, SESSION, now=close+timedelta(seconds=59))
        for age in (60, 119):
            result = inspect_session(bars, SESSION, now=close+timedelta(seconds=age))
            signal = result["signals"][0]
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["latest_bar_close"], close.isoformat())
            self.assertEqual(signal["expires_at"], (close+timedelta(seconds=120)).isoformat())
            self.assertTrue(signal["eligible"])
            self.assertFalse(signal["stale_or_catchup"])
            self.assertEqual(signal["signal_age_seconds"], age)
        expired = inspect_session(bars, SESSION, now=close+timedelta(seconds=120))["signals"][0]
        self.assertFalse(expired["eligible"])
        self.assertTrue(expired["stale_or_catchup"])

    def test_old_prefix_is_catchup_even_with_long_age_policy(self):
        bars = strategy_bars()
        result = inspect_session(bars, SESSION, now=OPEN+timedelta(minutes=131),
                                 max_age_seconds=3600)
        self.assertFalse(result["signals"][0]["eligible"])
        self.assertTrue(result["signals"][0]["stale_or_catchup"])
        self.assertEqual(result["signals"][0]["signal_age_seconds"], 360)
        # A newer bar exists, so an older signal cannot become actionable again.
        bars += [Bar(OPEN+timedelta(minutes=125), 105, 106, 104, 105, 1000)]
        result = inspect_session(bars, SESSION, now=OPEN+timedelta(minutes=131),
                                 max_age_seconds=3600)
        self.assertFalse(result["signals"][0]["eligible"])

    def test_fixed_expiry_is_capped_at_session_close(self):
        # Move the real pattern late in a full prefix, using low flat warmup bars.
        pattern = strategy_bars()
        offset = 52
        prefix = [Bar(OPEN+timedelta(minutes=5*i), 98, 98.1, 97.9, 98, 1000)
                  for i in range(offset)]
        bars = prefix+[replace(bar, timestamp=bar.timestamp+timedelta(minutes=5*offset))
                       for bar in pattern]
        result = inspect_session(bars, SESSION, now=SESSION.close-timedelta(minutes=4),
                                 max_age_seconds=3600)
        self.assertTrue(result["signals"])
        latest = result["signals"][-1]
        self.assertEqual(latest["expires_at"], SESSION.close.isoformat())
        self.assertTrue(latest["eligible"])

    def test_missing_prefix_gap_duplicate_unclosed_and_other_session_refused(self):
        bars = strategy_bars()
        now = OPEN+timedelta(minutes=126)
        cases = [[], bars[1:], bars[:5]+bars[6:], bars[:5]+bars[4:],
                 [replace(bars[0], timestamp=OPEN-timedelta(minutes=5))]+bars,
                 bars+[replace(bars[-1], timestamp=SESSION.close)],
                 bars+[replace(bars[-1], timestamp=OPEN+timedelta(days=1))],
                 bars+[replace(bars[-1], timestamp=OPEN+timedelta(minutes=125))]]
        for invalid in cases:
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                inspect_session(invalid, SESSION, now=now)

    def test_unsupported_early_close_and_closed_market_emit_no_signals(self):
        early = Session(SESSION.date, OPEN, OPEN+timedelta(minutes=210))
        result = inspect_session(strategy_bars(), early, now=OPEN+timedelta(minutes=126))
        self.assertEqual(result["status"], "unsupported_early_close")
        self.assertEqual(result["signals"], [])
        for now in (OPEN-timedelta(seconds=1), SESSION.close, SESSION.close+timedelta(days=1)):
            result = inspect_session([], SESSION, now=now)
            self.assertEqual(result["status"], "market_closed")
            self.assertEqual(result["signals"], [])

    def test_strict_parameters_timestamps_config_and_bar_numbers(self):
        bars, now = strategy_bars(), OPEN+timedelta(minutes=126)
        for field, invalid_values in (("settle_seconds", [True, 1.5, -1, 301, float("nan"), "60"]),
                                      ("max_age_seconds", [True, 1.5, 0, 3601, float("inf"), "120"])):
            for value in invalid_values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    inspect_session(bars, SESSION, now=now, **{field: value})
        with self.assertRaises(ValueError):
            inspect_session(bars, SESSION, now=now.replace(tzinfo=None))
        for config in ({}, Config(use_ema=1), Config(capital=True)):
            with self.assertRaises(ValueError):
                SignalObserver(config)
            with self.assertRaises(ValueError):
                strategy_identity(config)
        with self.assertRaisesRegex(ValueError, "booleans"):
            inspect_session([replace(bars[0], volume=True)], SESSION, now=now)


if __name__ == "__main__":
    unittest.main()
