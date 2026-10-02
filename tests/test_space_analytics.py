"""Check dashboard provenance and visual evidence against actual replay results."""
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.huggingface import analytics


class DashboardAnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        analytics._cached_dashboard_json.cache_clear()
        cls.temporary_paths = []
        original = tempfile.TemporaryDirectory

        def capture(*args, **kwargs):
            temporary = original(*args, **kwargs)
            cls.temporary_paths.append(Path(temporary.name))
            return temporary

        with patch.object(analytics, "TemporaryDirectory", side_effect=capture):
            cls.payload = analytics.run_dashboard_experiment()
            cls.summary = analytics.run_robustness_summary()

    def test_artifacts_are_synthetic_unapproved_and_cleaned(self):
        payload = self.payload
        self.assertEqual(payload["report"]["symbol"], "QQQ")
        self.assertEqual(payload["report"]["status"], "completed_synthetic_smoke")
        self.assertTrue(payload["report"]["synthetic"])
        self.assertFalse(payload["report"]["promotion_eligible"])
        self.assertTrue(payload["model"]["synthetic"])
        self.assertFalse(payload["classifier"]["transformer_active"])
        self.assertEqual(payload["audit"]["status"], "passed")
        self.assertEqual(payload["audit"]["runtime_skills"]["used"], [])
        self.assertFalse(payload["provenance"]["real_market_data"])
        self.assertEqual(payload["provenance"]["broker_orders_submitted"], 0)
        self.assertEqual(payload["provenance"]["transformer_experiments_run"], 0)
        serialized = json.dumps(payload, allow_nan=False)
        self.assertNotIn("directory", payload["report"])
        self.assertNotIn("tracking_uri", payload["report"]["experiment_settings"])
        self.assertEqual(len(self.temporary_paths), 4)
        for path in self.temporary_paths:
            self.assertFalse(path.exists())
            self.assertNotIn(str(path), serialized)
        self.assertLess(len(serialized), 8_000_000)

    def test_equity_trade_ledger_and_drawdown_reconcile_for_each_policy(self):
        payload = self.payload
        capital = payload["test"]["initial_capital"]
        expected = payload["report"]["evaluation"]["test"]
        for name, variant in payload["test"]["variants"].items():
            self.assertEqual(variant["metrics"], expected[name])
            self.assertEqual(len(variant["trades"]), expected[name]["trades"])
            self.assertAlmostEqual(sum(trade["net_pnl"] for trade in variant["trades"]), expected[name]["net_pnl"])
            self.assertEqual(len(variant["curve"]), len(payload["test"]["candles"]) + 1)
            self.assertEqual(variant["curve"][0]["equity"], capital)
            self.assertEqual(variant["curve"][0]["drawdown_fraction"], 0)
            self.assertEqual(variant["curve"][-1]["equity"], expected[name]["ending_equity"])
            self.assertEqual(variant["curve"][-1]["realized_equity"], expected[name]["ending_equity"])
            peak = capital
            for row in variant["curve"]:
                peak = max(peak, row["equity"])
                self.assertAlmostEqual(row["drawdown_fraction"], (peak - row["equity"]) / peak)
            self.assertAlmostEqual(max(row["drawdown_fraction"] for row in variant["curve"]),
                                   expected[name]["max_bar_close_drawdown"])

    def test_candle_and_mark_timestamps_preserve_information_availability(self):
        payload = self.payload
        candles = payload["test"]["candles"]
        sessions = payload["report"]["partitions"]["test"]
        self.assertEqual(len(candles), 78 * len(sessions))
        self.assertEqual(sorted({row["session"] for row in candles}), sessions)
        for candle, mark in zip(candles, payload["test"]["variants"]["baseline"]["curve"][1:], strict=True):
            self.assertEqual(datetime.fromisoformat(candle["available_at"]) - datetime.fromisoformat(candle["timestamp"]), timedelta(minutes=5))
            self.assertEqual(mark["timestamp"], candle["available_at"])
            self.assertEqual(mark["bar_timestamp"], candle["timestamp"])
        for candle in candles[::78]:
            self.assertEqual(candle["ema20"], candle["close"])
            self.assertAlmostEqual(candle["vwap"], (candle["high"] + candle["low"] + candle["close"]) / 3)
        self.assertIn(payload["test"]["default_session"], sessions)

    def test_probabilities_and_explanations_match_saved_classifier(self):
        payload = self.payload
        rows = payload["test"]["probabilities"]
        calibration = payload["classifier"]["calibration"]
        self.assertEqual(len(rows), payload["report"]["sample_counts"]["test"]["labeled"])
        self.assertEqual(len(rows), calibration["samples"])
        self.assertAlmostEqual(sum((row["probability"] - row["label"]) ** 2 for row in rows) / len(rows), calibration["brier_score"])
        self.assertEqual(sum(row["count"] for row in calibration["calibration_bins"]), len(rows))
        example = payload["classifier"]["example"]
        score = example["intercept"] + sum(row["contribution"] for row in example["contributions"])
        self.assertAlmostEqual(example["logit"], score)
        self.assertAlmostEqual(example["probability"], 1 / (1 + math.exp(-score)))
        self.assertEqual(example["signal_time"], rows[0]["signal_time"])
        self.assertEqual(example["probability"], rows[0]["probability"])
        model = payload["model"]
        candidates = [row for row in payload["test"]["variants"]["baseline"]["candidates"] if "label" in row]
        for i, entry in enumerate(payload["classifier"]["feature_contributions"]):
            expected = sum(abs(model["coefficients"][i] * (row["features"][entry["feature"]] - model["mean"][i]) / model["scale"][i]) for row in candidates) / len(candidates)
            self.assertAlmostEqual(entry["mean_abs_contribution"], expected)
        self.assertIn("not causal", payload["classifier"]["interpretation"])

    def test_four_cases_are_fixed_and_cost_case_uses_same_input(self):
        self.assertEqual(tuple(row["case_id"] for row in self.summary["rows"]), analytics.CASE_IDS)
        self.assertEqual(self.summary["ordinary_cases"], 3)
        self.assertEqual(self.summary["ordinary_cases_model_ahead"], 1)
        cost = analytics.run_dashboard_experiment("coststress")
        self.assertEqual(cost["report"]["input_sha256"], self.payload["report"]["input_sha256"])
        self.assertEqual(cost["report"]["strategy"]["commission"], self.payload["report"]["strategy"]["commission"] * 2)
        self.assertEqual(cost["report"]["strategy"]["slippage"], self.payload["report"]["strategy"]["slippage"] * 2)
        self.assertEqual(cost["report"]["space_demo"]["seed"], 42)
        for case_id in ("seed43", "seed44"):
            case = analytics.run_dashboard_experiment(case_id)
            self.assertNotEqual(case["report"]["input_sha256"], self.payload["report"]["input_sha256"])
            self.assertEqual(case["report"]["space_demo"]["seed"], int(case_id[-2:]))
        self.assertEqual(analytics._cached_dashboard_json.cache_info().maxsize, 4)
        self.assertEqual(analytics._cached_dashboard_json.cache_info().currsize, 4)

    def test_cached_values_are_detached_between_visitors(self):
        first = analytics.run_dashboard_experiment()
        first["report"]["synthetic"] = False
        first["test"]["variants"]["baseline"]["curve"][0]["equity"] = -1
        first["model"]["intercept"] = 9999
        first["audit"]["checks"].clear()
        with patch.object(analytics, "experiment", side_effect=AssertionError("must use bounded cache")):
            second = analytics.run_dashboard_experiment()
        self.assertEqual(second, self.payload)

    def test_invalid_cases_cannot_supply_paths_or_increase_work(self):
        for invalid in ("../private-data/data.csv", "seed100", "", 42, True, None, {}, []):
            with self.subTest(invalid=invalid), patch.object(analytics, "generate", side_effect=AssertionError("must reject before generation")):
                with self.assertRaises(ValueError):
                    analytics.run_dashboard_experiment(invalid)

    def test_failures_still_remove_temporary_artifacts(self):
        paths = []

        def fail_generation(path, **kwargs):
            paths.append(path.parent)
            path.write_text("partial fixture")
            raise ValueError("fixture failed")

        with patch.object(analytics, "generate", side_effect=fail_generation):
            with self.assertRaisesRegex(ValueError, "fixture failed"):
                analytics._cached_dashboard_json.__wrapped__("seed42")
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())


if __name__ == "__main__":
    unittest.main()
