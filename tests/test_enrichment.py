import copy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dwight.connectors.csv import read_bars
from dwight.enrichment import (
    ARMS, ENRICHED_NAMES, ENRICHED_VERSION, EnrichedBot, _fit,
    enriched_features, run_enrichment,
)
from dwight.experiments import FEATURE_NAMES, JSONModel, fit_model
from dwight.walkforward import WalkForwardBot
from examples.make_experiment_demo import generate
from vwap_bot.engine import Bar, Config


START = datetime(2026, 9, 28, 9, 30, tzinfo=ZoneInfo("America/New_York"))


def bar(i, **changes):
    values = dict(open=100, high=100.2, low=99.8, close=100, volume=1000)
    values.update(changes)
    return Bar(START+timedelta(minutes=5*i), **values)


class EnrichmentTests(unittest.TestCase):
    def candidate(self):
        bot = EnrichedBot(Config(ema_period=2, atr_period=2))
        bot.feed(bar(0))
        bot.feed(bar(1))
        bot.highs, bot.lows = [(0, 99), (1, 100)], [(0, 97), (1, 98)]
        bot.setup = dict(direction=1, index=1, extreme=101, level=100, touched=False)
        bot.feed(bar(2, high=101, low=99.9, close=100.9))
        return bot

    def test_enrichment_observes_only_signal_time_prefix(self):
        first, second = self.candidate(), self.candidate()
        features = copy.deepcopy(first.candidates[0]["features"])
        atr = sum(first.trs[-2:])/2
        self.assertEqual(first.candidates[0]["feature_version"], ENRICHED_VERSION)
        self.assertEqual(set(features), set(ENRICHED_NAMES))
        self.assertAlmostEqual(features["session_return_atr"], .9/atr)
        self.assertAlmostEqual(features["observed_session_range_atr"], 1.2/atr)
        self.assertGreater(features["realized_log_volatility_12"], 0)
        self.assertGreater(features["vwap_change_1_atr"], 0)
        first.feed(bar(3, open=101, high=105, low=100.5, close=104))
        second.feed(bar(3, open=101, high=101.2, low=98, close=99))
        self.assertEqual(first.candidates[0]["features"], features)
        self.assertEqual(second.candidates[0]["features"], features)
        self.assertNotEqual(first.candidates[0]["label"], second.candidates[0]["label"])

    def test_session_feature_history_resets(self):
        bot = self.candidate()
        next_day = Bar(START+timedelta(days=1), 200, 201, 199, 200, 1000)
        bot.feed(next_day)
        self.assertEqual(len(bot.bars), 1)
        with self.assertRaises(ValueError):
            enriched_features(bot, {"direction": 1, "stop": 199})

    def test_enriched_unfiltered_replay_preserves_existing_v1_trades(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)/"synthetic.csv"
            generate(data, days=100)
            old, enriched = WalkForwardBot(), EnrichedBot()
            for b in read_bars(data):
                old.feed(b)
                enriched.feed(b)
            old.finish()
            enriched.finish()
            self.assertGreater(len(old.trades), 0)
            self.assertEqual(old.trades, enriched.trades)
            self.assertEqual(old.marked_equity, enriched.marked_equity)
            self.assertEqual(len(old.candidates), len(enriched.candidates))
            for left, right in zip(old.candidates, enriched.candidates):
                self.assertEqual(left["features"], {name: right["features"][name] for name in FEATURE_NAMES})
                self.assertEqual(left["taken"], right["taken"])

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "research extra unavailable")
    def test_v1_arm_exactly_preserves_old_training_and_inference(self):
        rows = [{"features": {name: (i % 5)+j/100 for j, name in enumerate(ENRICHED_NAMES)},
                 "label": int(i % 5 > 2)} for i in range(50)]
        old = fit_model(rows, synthetic=True)
        same = _fit(rows, "technical_v1", True)
        for key in ("mean", "scale", "coefficients", "intercept"):
            self.assertEqual(same.artifact[key], old.artifact[key])
        for row in rows:
            projected = {name: row["features"][name] for name in FEATURE_NAMES}
            self.assertAlmostEqual(same.predict_probability(row["features"]), old.predict_probability(projected))
        for arm in ARMS:
            with self.assertRaises(ValueError):
                JSONModel(_fit(rows, arm, True).artifact)

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "research extra unavailable")
    def test_smoke_never_replays_final_holdout_and_keeps_both_arms(self):
        from dwight import enrichment
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root/"synthetic.csv"
            generate(data)
            observed = set()
            real_replay = enrichment._replay

            def spy(bars, *args, **kwargs):
                observed.update(b.timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat() for b in bars)
                return real_replay(bars, *args, **kwargs)

            config = {"train_sessions": 80, "validation_sessions": 40, "test_sessions": 40,
                      "holdout_sessions": 40, "max_windows": 3,
                      "min_train_samples": 4, "min_validation_samples": 2,
                      "min_test_samples": 2, "min_class_samples": 1, "min_validation_trades": 1}
            with patch("dwight.enrichment._replay", side_effect=spy):
                report = run_enrichment(data, root/"runs", synthetic=True, config=config)
            self.assertEqual(report["window_counts"]["completed"], 3)
            self.assertFalse(set(report["final_holdout"]["sessions"]) & observed)
            self.assertFalse(report["final_holdout"]["consumed"])
            self.assertIsNone(report["selected_variant"])
            self.assertFalse(report["promotion_eligible"])
            self.assertEqual(set(report["aggregate"]), {"baseline", "simple_volume", *ARMS})
            recipe = json.loads((Path(report["directory"])/"recipe.json").read_text())
            self.assertEqual(recipe["learner"]["C"], 1)
            for fold in report["windows"]:
                self.assertEqual(set(fold["arms"]), set(ARMS))
                self.assertFalse(fold["selection_uses_test"])

    def test_too_little_data_is_reported_without_fitting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root/"synthetic.csv"
            generate(data, days=14)
            with patch("dwight.enrichment._fit") as fit:
                report = run_enrichment(data, root/"runs", synthetic=True)
                fit.assert_not_called()
            self.assertEqual(report["status"], "insufficient_data")
            self.assertEqual(report["aggregate"], {})
            self.assertFalse(report["final_holdout"]["reservation_complete"])
            with self.assertRaisesRegex(ValueError, "reduced sample"):
                run_enrichment(data, root/"reduced", synthetic=False, config={"min_train_samples": 2})


if __name__ == "__main__":
    unittest.main()
