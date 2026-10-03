"""Direction-aware audits must replay the chosen portfolio, not relabel its trades."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from dwight.audit import audit_experiment
from dwight.experiments import experiment
from dwight.reporting import generate_report
from examples.make_experiment_demo import generate


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "research extra not installed")
class AuditDirectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        data = root / "input.csv"
        generate(data, days=500, seed=42)
        cls.sources = {}
        for long_only in (False, True):
            report = experiment(data, "QQQ", root / "runs", synthetic=True, config={
                "long_only": long_only, "min_train_samples": 10,
                "min_validation_samples": 4, "min_test_samples": 4,
                "min_class_samples": 1, "min_validation_trades": 1,
            })
            assert report["status"] == "completed_synthetic_smoke", report["blocking_reasons"]
            cls.sources[long_only] = Path(report["directory"])

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def copy_run(self, long_only):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "experiment"
        shutil.copytree(self.sources[long_only], path)
        return path

    @staticmethod
    def read(path, name):
        return json.loads((path / name).read_text())

    @staticmethod
    def write(path, name, payload):
        (path / name).write_text(json.dumps(payload, allow_nan=False) + "\n")

    @staticmethod
    def checks(result):
        return {check["id"]: check for check in result["checks"]}

    def test_long_only_replays_independently_and_suppresses_short_candidates(self):
        path = self.sources[True]
        result = audit_experiment(path)
        self.assertEqual(result["status"], "passed", result["checks"])
        self.assertEqual(result["direction_policy"], "long_only")
        self.assertEqual(result["paper_compatibility"]["short_trades"], 0)
        self.assertEqual(result["paper_compatibility"]["status"], "not_verified")
        self.assertGreater(result["audited_trade_records"], 0)
        candidates = self.read(path, "candidates.json")
        short_candidates = []
        for partition in ("train", "validation", "test"):
            self.assertEqual(self.checks(result)[partition + "_baseline_engine"]["status"], "passed")
            for variant in ("baseline", "simple-volume", "filtered"):
                trades = self.read(path, f"{partition}-{variant}-trades.json")
                self.assertTrue(all(trade["direction"] == 1 for trade in trades))
            for rows in (candidates[partition], self.read(path, f"{partition}-filtered-candidates.json")):
                short_candidates.extend(row for row in rows if row["features"]["direction"] == -1)
        self.assertTrue(short_candidates, "The fixture must exercise real short-candidate rejection")
        for candidate in short_candidates:
            self.assertFalse(candidate["taken"])
            self.assertEqual(candidate["rejection_reason"], "long_only_policy")
            self.assertNotIn("label", candidate)
            self.assertNotIn("probability", candidate)
        # A post-hoc deletion of short trades would miss their effect on the
        # later account path; the independent baseline checks above must pass.
        unrestricted = audit_experiment(self.sources[False])
        self.assertEqual(unrestricted["status"], "passed", unrestricted["checks"])
        self.assertGreater(unrestricted["paper_compatibility"]["short_trades"], 0)

    def test_absent_legacy_direction_fields_preserve_long_and_short_replay(self):
        path = self.copy_run(False)
        report = self.read(path, "report.json")
        report.pop("direction_policy")
        report["experiment_settings"].pop("long_only")
        model = self.read(path, "model.json")
        model.pop("direction_policy")
        self.write(path, "model.json", model)
        report["model_sha256"] = hashlib.sha256((path / "model.json").read_bytes()).hexdigest()
        self.write(path, "report.json", report)
        result = audit_experiment(path)
        self.assertEqual(result["status"], "passed", result["checks"])
        self.assertEqual(result["direction_policy"], "long_and_short")
        self.assertGreater(result["paper_compatibility"]["short_trades"], 0)
        for partition in ("train", "validation", "test"):
            self.assertEqual(self.checks(result)[partition + "_baseline_engine"]["status"], "passed")

        with patch("dwight.reporting._charts", return_value={}):
            paths = generate_report(path, path.parent / "legacy-report", audit=result)
        summary = json.loads(Path(paths["summary"]).read_text())
        self.assertTrue(summary["replay_verified"])
        self.assertEqual(summary["test_metrics"], report["evaluation"]["test"])

    def test_report_charts_receive_verified_long_only_portfolios(self):
        path = self.sources[True]
        report = self.read(path, "report.json")
        with tempfile.TemporaryDirectory() as temporary, patch("dwight.reporting._charts", return_value={}) as charts:
            paths = generate_report(path, Path(temporary) / "report")
            summary = json.loads(Path(paths["summary"]).read_text())
            self.assertTrue(summary["replay_verified"])
            self.assertEqual(summary["test_metrics"], report["evaluation"]["test"])
            portfolios = charts.call_args.args[1]
            self.assertEqual(set(portfolios), {"baseline", "simple_volume", "filtered"})
            for variant, bot in portfolios.items():
                filename = variant.replace("_", "-")
                self.assertEqual(bot.trades, self.read(path, f"test-{filename}-trades.json"))
                self.assertTrue(all(trade["direction"] == 1 for trade in bot.trades))
            self.assertGreater(sum(len(bot.trades) for bot in portfolios.values()), 0)

    def test_changed_short_decision_cannot_pass_long_only_candidate_audit(self):
        path = self.copy_run(True)
        candidates = self.read(path, "candidates.json")
        partition, candidate = next((name, row) for name, rows in candidates.items()
                                    for row in rows if row["features"]["direction"] == -1)
        candidate["taken"] = True
        self.write(path, "candidates.json", candidates)
        result = audit_experiment(path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.checks(result)[partition + "_baseline_candidates"]["status"], "failed")

    def test_valid_model_checksum_cannot_hide_direction_policy_mismatch(self):
        path = self.copy_run(True)
        report = self.read(path, "report.json")
        model = self.read(path, "model.json")
        model["direction_policy"] = "long_and_short"
        self.write(path, "model.json", model)
        report["model_sha256"] = hashlib.sha256((path / "model.json").read_bytes()).hexdigest()
        self.write(path, "report.json", report)
        result = audit_experiment(path)
        self.assertEqual(result["status"], "failed")
        checks = self.checks(result)
        self.assertEqual(checks["model_integrity"]["status"], "failed")
        self.assertEqual(checks["test_filtered"]["status"], "unverified")
        output = path.parent / "invalid-model-report"
        with self.assertRaisesRegex(ValueError, "direction policy"):
            generate_report(path, output)
        self.assertFalse(output.exists())

    def test_invalid_or_inconsistent_report_policy_withholds_replay(self):
        for policy in ("short_only", None, [], "long_and_short"):
            with self.subTest(policy=policy):
                path = self.copy_run(True)
                report = self.read(path, "report.json")
                report["direction_policy"] = policy
                self.write(path, "report.json", report)
                result = audit_experiment(path)
                self.assertEqual(result["status"], "failed")
                checks = self.checks(result)
                self.assertEqual(checks["direction_policy"]["status"], "failed")
                self.assertEqual(checks["replay_evidence"]["status"], "unverified")
                self.assertNotIn("train_baseline_trades", checks)
                output = path.parent / "invalid-direction-report"
                with self.assertRaises(ValueError):
                    generate_report(path, output)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
