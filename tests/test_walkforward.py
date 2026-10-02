import copy
import csv
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dwight import walkforward as wf
from dwight.experiments import FEATURE_NAMES, fit_model
from examples.make_experiment_demo import generate
from vwap_bot.engine import Bar, Bot, Config


SMOKE = {
    "train_sessions": 80, "validation_sessions": 40, "test_sessions": 40,
    "holdout_sessions": 40, "max_windows": 3,
    "min_train_samples": 4, "min_validation_samples": 2,
    "min_test_samples": 2, "min_class_samples": 1, "min_validation_trades": 1,
}


class WindowPlanTests(unittest.TestCase):
    def plan(self, count=20, **kwargs):
        options = {**wf.WALKFORWARD_DEFAULTS, "train_sessions": 4,
                   "validation_sessions": 2, "test_sessions": 3,
                   "holdout_sessions": 4, **kwargs}
        days = [(datetime(2026, 1, 1)+timedelta(days=i)).date().isoformat() for i in range(count)]
        return days, wf.plan_windows(days, options)

    def test_whole_sessions_ordered_tests_disjoint_holdout_untouched(self):
        days, plan = self.plan()
        self.assertEqual(plan["final_holdout_sessions"], days[-4:])
        seen = set()
        for window in plan["windows"]:
            self.assertLess(max(window["train"]), min(window["validation"]))
            self.assertLess(max(window["validation"]), min(window["test"]))
            self.assertTrue(seen.isdisjoint(window["test"]))
            seen.update(window["test"])
            for partition in ("train", "validation", "test"):
                self.assertTrue(set(plan["final_holdout_sessions"]).isdisjoint(window[partition]))
        self.assertEqual([len(w["train"]) for w in plan["windows"]], [4, 7, 10])
        self.assertEqual(plan["unused_development_sessions"], days[15:16])

    def test_rolling_training_fixed_length(self):
        _, plan = self.plan(mode="rolling")
        self.assertEqual([len(w["train"]) for w in plan["windows"]], [4, 4, 4])
        self.assertNotEqual(plan["windows"][0]["train"][0], plan["windows"][1]["train"][0])

    def test_max_windows_and_incomplete_dataset_are_explicit(self):
        days, plan = self.plan(max_windows=1)
        self.assertEqual(len(plan["windows"]), 1)
        self.assertEqual(plan["unused_development_sessions"], days[9:-4])
        days, plan = self.plan(count=3)
        self.assertEqual(plan["windows"], [])
        self.assertEqual(plan["final_holdout_sessions"], days)
        self.assertFalse(plan["holdout_reservation_complete"])

    def test_options_reject_real_sample_reduction_and_bad_values(self):
        with self.assertRaisesRegex(ValueError, "synthetic"):
            wf._options({"min_train_samples": 2}, synthetic=False)
        for cfg in ({"mode": "shuffle"}, {"holdout_sessions": 0}, {"long_only": "yes"},
                    {"max_windows": 51}, {"thresholds": [0]}, {"thresholds": [float("nan")]},
                    {"thresholds": [True]}, {"thresholds": [0.5]*22}, {"unknown": 1}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                wf._options(cfg, synthetic=True)

    def test_long_only_gate_blocks_short_before_entry_and_before_inference(self):
        start = datetime(2026, 9, 28, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        bars = [Bar(start+timedelta(minutes=5*i), 100, 100.2, 98, 100, 1000) for i in range(3)]

        def signal(bot, bar, previous):
            bot.pending = {"direction": -1, "stop": 101, "reason": "test", "signal_time": bar.timestamp.isoformat()}

        def features(bot, pending):
            return {name: -1 if name == "direction" else 1 for name in FEATURE_NAMES}

        class MustNotRun:
            def predict_probability(self, features):
                raise AssertionError("short candidate reached inference")

        blocked = wf.WalkForwardBot(model=MustNotRun())
        unrestricted = wf.WalkForwardBot(long_only=False)
        with patch.object(Bot, "_signal", signal), patch("dwight.experiments.extract_features", features):
            for bar in bars:
                blocked.feed(bar)
                unrestricted.feed(bar)
        self.assertFalse(blocked.trades)
        self.assertIsNone(blocked.position)
        self.assertIsNone(blocked.pending)
        self.assertTrue(all(not row["taken"] and row["rejection_reason"] == "long_only_policy" for row in blocked.candidates))
        self.assertTrue(unrestricted.position or unrestricted.trades)

    def test_insufficient_session_report_has_no_fake_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate(root/"bars.csv", days=10)
            with patch.object(wf, "_replay", side_effect=AssertionError("must not replay")):
                report = wf.run_walkforward(root/"bars.csv", root/"runs", synthetic=True)
            self.assertEqual(report["status"], "insufficient_data")
            self.assertEqual(report["aggregate"], {})
            self.assertEqual(report["windows"], [])
            self.assertFalse(report["final_holdout"]["consumed"])
            self.assertIsNone(report["final_holdout"]["evaluation"])


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "research extra not installed")
class WalkForwardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.data = cls.root/"bars.csv"
        generate(cls.data, days=500, seed=42)
        cls.training_rows, cls.replay_calls = [], []
        original = wf._replay

        def capture_fit(rows, synthetic=False):
            cls.training_rows.append(copy.deepcopy(rows))
            return fit_model(rows, synthetic)

        def capture_replay(bars, settings, long_only, model=None, threshold=.5):
            cls.replay_calls.append({"days": sorted({b.timestamp.date().isoformat() for b in bars}),
                                     "has_model": model is not None, "threshold": threshold,
                                     "is_logistic": isinstance(model, wf.JSONModel),
                                     "frozen_train_sessions": model.artifact.get("train_sessions") if isinstance(model, wf.JSONModel) else None})
            return original(bars, settings, long_only, model, threshold)

        with patch.object(wf, "fit_model", capture_fit), patch.object(wf, "_replay", capture_replay):
            cls.report = wf.run_walkforward(cls.data, cls.root/"runs", synthetic=True, config=SMOKE)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_final_holdout_never_replayed_or_fitted(self):
        holdout = set(self.report["final_holdout"]["sessions"])
        self.assertEqual(len(holdout), 40)
        for call in self.replay_calls:
            self.assertTrue(holdout.isdisjoint(call["days"]))
        for rows in self.training_rows:
            self.assertTrue(holdout.isdisjoint({row["session"] for row in rows}))
        self.assertFalse(self.report["promotion_eligible"])
        self.assertEqual(self.report["status"], "completed_synthetic_smoke")
        self.assertEqual(self.report["window_counts"], {"planned": 3, "completed": 3, "insufficient": 0})

    def test_scaler_fitted_only_to_training_rows(self):
        for window, rows in zip(self.report["windows"], self.training_rows):
            self.assertTrue(all(row["session"] in window["train"] for row in rows))
            model = json.loads((Path(window["directory"])/"model.json").read_text())
            self.assertEqual(model["training_samples"], len(rows))
            for index, feature in enumerate(FEATURE_NAMES):
                expected = sum(row["features"][feature] for row in rows)/len(rows)
                self.assertAlmostEqual(model["mean"][index], expected, places=12)
            self.assertTrue(all(row["features"]["direction"] == 1 for row in rows))

    def test_threshold_uses_validation_only_and_test_uses_frozen_choice(self):
        for window in self.report["windows"]:
            eligible = [trial for trial in window["validation_threshold_trials"] if trial["trades"] >= 1]
            expected = max(eligible, key=lambda trial: (trial["net_pnl"], -trial["max_bar_close_drawdown"], -abs(trial["threshold"]-.5)))
            self.assertEqual(window["selected_threshold"], expected["threshold"])
            calls = [call for call in self.replay_calls if call["days"] == window["test"] and call["is_logistic"]
                     and call["frozen_train_sessions"] == window["train"]]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["threshold"], expected["threshold"])
            self.assertFalse(window["selection_uses_test"])

    def test_trades_have_costs_and_equity_corresponds_to_metrics(self):
        for window in self.report["windows"]:
            for variant in ("baseline", "simple_volume", "filtered"):
                directory = Path(window["directory"])
                trades = json.loads((directory/f"test-{variant}-trades.json").read_text())
                equity = json.loads((directory/f"test-{variant}-equity.json").read_text())
                metrics = window["test_evaluation"][variant]
                self.assertTrue(all(trade["direction"] == 1 for trade in trades))
                for trade in trades:
                    self.assertAlmostEqual(trade["fees"], 2*Config().commission*trade["quantity"])
                    self.assertAlmostEqual(trade["net_pnl"], trade["gross_pnl"]-trade["fees"])
                self.assertAlmostEqual(sum(trade["net_pnl"] for trade in trades), metrics["net_pnl"])
                self.assertAlmostEqual(equity[-1]["equity"], metrics["ending_equity"])
                self.assertTrue(equity[-1]["available_at"].split("T")[1].startswith("16:00"))
                self.assertEqual(len(equity), len(window["test"])*78)
        self.assertTrue(self.report["source_hashes"])
        self.assertEqual(self.report["source"], "synthetic")

    def test_reproducible_metrics_and_models(self):
        repeat = wf.run_walkforward(self.data, self.root/"repeat", synthetic=True, config=SMOKE)
        self.assertEqual(self.report["aggregate"], repeat["aggregate"])
        for first, second in zip(self.report["windows"], repeat["windows"]):
            self.assertEqual(first["test_evaluation"], second["test_evaluation"])
            self.assertEqual(first["model_sha256"], second["model_sha256"])

    def test_changed_test_prices_do_not_change_fit_or_threshold(self):
        # A separate run may change a window's future prices. Its training and
        # validation artifact/threshold must remain byte-identical.
        window = self.report["windows"][0]
        test_days = set(window["test"])
        changed = self.root/"changed-test.csv"
        with self.data.open(newline="") as source, changed.open("w", newline="") as target:
            reader = csv.DictReader(source)
            writer = csv.DictWriter(target, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if row["timestamp"].split("T")[0] in test_days:
                    for key in ("open", "high", "low", "close"):
                        row[key] = float(row[key])*1.2
                    row["volume"] = float(row["volume"])*2
                writer.writerow(row)
        result = wf.run_walkforward(changed, self.root/"changed-runs", synthetic=True, config={**SMOKE, "max_windows": 1})
        self.assertEqual(window["model_sha256"], result["windows"][0]["model_sha256"])
        self.assertEqual(window["selected_threshold"], result["windows"][0]["selected_threshold"])

    def test_insufficient_labels_do_not_fit_or_consume_test(self):
        with patch.object(wf, "fit_model", side_effect=AssertionError("must not train")):
            report = wf.run_walkforward(self.data, self.root/"insufficient", synthetic=True,
                                       config={**SMOKE, "min_train_samples": 100000, "max_windows": 1})
        window = report["windows"][0]
        self.assertEqual(window["status"], "insufficient_data")
        self.assertFalse(window["test_consumed"])
        self.assertEqual(window["test_evaluation"], {})
        self.assertNotIn("selected_threshold", window)
        self.assertFalse((Path(window["directory"])/"model.json").exists())

    def test_insufficient_test_samples_do_not_emit_fake_score(self):
        report = wf.run_walkforward(self.data, self.root/"insufficient-test", synthetic=True,
                                   config={**SMOKE, "min_test_samples": 100000, "max_windows": 1})
        window = report["windows"][0]
        self.assertEqual(window["status"], "insufficient_test_samples")
        self.assertTrue(window["test_consumed"])
        self.assertIn("selected_threshold", window)
        self.assertEqual(window["test_evaluation"], {})
        self.assertEqual(report["aggregate"], {})


if __name__ == "__main__":
    unittest.main()
