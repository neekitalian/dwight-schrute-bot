"""Verify that the public research demo exposes real, synthetic-only results."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.huggingface import research


class SpaceResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        research._cached_report_json.cache_clear()
        cls.temporary_paths = []
        original = tempfile.TemporaryDirectory

        def capture_directory(*args, **kwargs):
            temporary = original(*args, **kwargs)
            cls.temporary_paths.append(Path(temporary.name))
            return temporary

        with patch.object(research, "TemporaryDirectory", side_effect=capture_directory):
            cls.report = research.run_synthetic_experiment()

    def test_actual_pipeline_returns_unapproved_synthetic_model(self):
        self.assertEqual(self.report["status"], "completed_synthetic_smoke")
        self.assertEqual(self.report["symbol"], "QQQ")
        self.assertTrue(self.report["synthetic"])
        self.assertFalse(self.report["promotion_eligible"])
        self.assertFalse(self.report["space_demo"]["broker_connected"])
        self.assertEqual(self.report["complete_sessions"], 358)
        self.assertRegex(self.report["model_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("filtered", self.report["evaluation"]["test"])

    def test_presentation_compares_final_test_values(self):
        summary, rows, model, shown_report = research.present_experiment(self.report)
        self.assertEqual(len(rows), 3)
        for row, key in zip(rows, ("baseline", "simple_volume", "filtered")):
            actual = self.report["evaluation"]["test"][key]
            self.assertEqual(row[1], actual["trades"])
            self.assertEqual(row[2], round(actual["net_pnl"], 2))
        self.assertIn("Deployment eligible: no", summary)
        self.assertIn(self.report["model_sha256"], model)
        self.assertIs(shown_report, self.report)

    def test_cleanup_and_no_internal_paths_or_tracking(self):
        self.assertTrue(self.temporary_paths)
        text = json.dumps(self.report)
        self.assertNotIn("directory", self.report)
        self.assertNotIn("tracking_uri", self.report["experiment_settings"])
        self.assertNotIn("dataset_manifest", self.report["experiment_settings"])
        for path in self.temporary_paths:
            self.assertFalse(path.exists())
            self.assertNotIn(str(path), text)

    def test_cached_result_is_not_mutable_across_visitors(self):
        first = research.run_synthetic_experiment()
        first["synthetic"] = False
        first["evaluation"]["test"]["baseline"]["net_pnl"] = -999999
        with patch.object(research, "experiment", side_effect=AssertionError("should use cache")):
            second = research.run_synthetic_experiment()
        self.assertEqual(second, self.report)

    def test_failure_cleans_temporary_files(self):
        temporary_paths = []

        def fail_generation(path, **kwargs):
            temporary_paths.append(path.parent)
            path.write_text("incomplete fixture")
            raise ValueError("fixture failure")

        with patch.object(research, "generate", side_effect=fail_generation):
            with self.assertRaises(ValueError):
                research._cached_report_json.__wrapped__()
        self.assertEqual(len(temporary_paths), 1)
        self.assertFalse(temporary_paths[0].exists())

    def test_public_report_refuses_real_or_approved_data(self):
        for overrides in ({"synthetic": False}, {"promotion_eligible": True}, {"symbol": "SPY"}):
            with self.assertRaises(ValueError):
                research._public_report({**self.report, **overrides}, 358)


if __name__ == "__main__":
    unittest.main()
