import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from dwight.experiments import experiment
from dwight.reporting import generate_report
from examples.make_experiment_demo import generate


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "research extra not installed")
class ReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        source = cls.root/"input.csv"
        generate(source, days=500, seed=42)
        cls.report = experiment(source, "QQQ", cls.root/"experiments", synthetic=True, config={
            "min_train_samples": 10, "min_validation_samples": 4,
            "min_test_samples": 4, "min_class_samples": 1,
            "min_validation_trades": 1,
        })
        cls.experiment_dir = Path(cls.report["directory"])
        if cls.report["status"] != "completed_synthetic_smoke":
            raise AssertionError("fixture did not create a complete synthetic experiment")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def copied_run(self, name):
        target = self.root/name
        shutil.copytree(self.experiment_dir, target)
        return target

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "reporting extra not installed")
    def test_portable_report_separates_replay_and_forward_measurements(self):
        audit = {
            "run_id": self.report["run_id"], "symbol": "QQQ", "synthetic": True,
            "status": "passed", "checks": [{"name": "<script>Bad headline</script>", "status": "passed", "detail": "Stored evidence agrees."}],
            "runtime_skills": {"description": "No agent skills are invoked by the trading runtime."},
            "paper_compatibility": {"detail": "Broker execution is not verified."},
        }
        paths = generate_report(self.experiment_dir, self.root/"rendered", milestone_label="12-hour review", campaign_status={
            "status": "waiting_for_credentials", "elapsed_hours": 0,
            "shadow_decisions": 0, "broker_paper_fills": 0,
            "blockers": ["Alpaca paper keys", "Always-on worker"],
        }, audit=audit)
        html = Path(paths["html"]).read_text()
        email = Path(paths["email"]).read_text()
        summary = json.loads(Path(paths["summary"]).read_text())
        self.assertIn("SYNTHETIC REPLAY", html)
        self.assertIn("generated QQQ data", email)
        self.assertIn("Elapsed observation hours: 0.", email)
        self.assertIn("Exchange sessions observed: not measured.", email)
        self.assertIn("Broker paper fills: 0.", email)
        self.assertNotRegex(email, r"[-\u2010-\u2015\u2212]")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(html.count('src="data:image/png;base64,'), 2)
        self.assertNotIn('src="http', html)
        self.assertFalse(summary["email_sent"])
        self.assertTrue(summary["replay_verified"])
        self.assertEqual(summary["test_metrics"], self.report["evaluation"]["test"])
        self.assertEqual(summary["campaign_measurements"]["broker_paper_fills"], 0)
        for key in ("price_chart", "performance_chart"):
            raw = Path(paths[key]).read_bytes()
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(raw), 10000)

    def test_tampered_data_is_rejected_before_any_report_is_created(self):
        directory = self.copied_run("tampered-input")
        with (directory/"input.csv").open("a") as stream:
            stream.write("\n")
        target = self.root/"bad-input-report"
        with self.assertRaisesRegex(ValueError, "input checksum"):
            generate_report(directory, target)
        self.assertFalse(target.exists())

    def test_saved_trades_must_match_independent_replay(self):
        directory = self.copied_run("tampered-trades")
        path = directory/"test-filtered-trades.json"
        trades = json.loads(path.read_text())
        trades[0]["net_pnl"] += 5
        path.write_text(json.dumps(trades))
        with self.assertRaisesRegex(ValueError, "trades do not match"):
            generate_report(directory, self.root/"bad-trades-report")

    def test_saved_model_and_metrics_cannot_be_swapped(self):
        directory = self.copied_run("tampered-metrics")
        report = copy.deepcopy(self.report)
        report["evaluation"]["test"]["baseline"]["net_pnl"] += 5
        (directory/"report.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "metrics do not match"):
            generate_report(directory, self.root/"bad-metrics-report")
        report = copy.deepcopy(self.report)
        report["model_sha256"] = "wrong"
        (directory/"report.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "model checksum"):
            generate_report(directory, self.root/"bad-model-report")

    def test_mismatched_audit_and_invalid_forward_counter_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "audit does not belong"):
            generate_report(self.experiment_dir, self.root/"bad-audit", audit={"run_id": "other"})
        with self.assertRaisesRegex(ValueError, "invalid campaign measurement"):
            generate_report(self.experiment_dir, self.root/"bad-counter", campaign_status={"broker_paper_fills": -1})

    def test_missing_observations_are_unknown_not_zero(self):
        with patch("dwight.reporting._charts", return_value={}):
            paths = generate_report(self.experiment_dir, self.root/"unknown-observations")
        email = Path(paths["email"]).read_text()
        self.assertIn("Broker paper fills: not measured.", email)
        self.assertIn("No compliance conclusion has been recorded.", email)
        self.assertNotIn("Broker paper fills: 0.", email)
        self.assertIn("Most recent worker status: not measured.", email)
        self.assertIn("Worker health is not fully measured", email)

    def test_observing_campaign_exposes_coverage_gaps_errors_and_actual_window(self):
        campaign = {
            "status": "observing", "elapsed_hours": 12,
            "observed_market_minutes": 15, "expected_market_minutes": 390,
            "missing_market_minutes": 375, "stale_decisions": 4,
            "error_observations": 18, "latest_status": "error_abstain",
            "last_observation_age_seconds": 7200,
            "window_start": "2026-10-02T09:30:00-04:00",
            "window_end": "2026-10-02T21:30:00-04:00",
        }
        with patch("dwight.reporting._charts", return_value={}):
            paths = generate_report(self.experiment_dir, self.root/"coverage-gaps", campaign_status=campaign)
        summary = json.loads(Path(paths["summary"]).read_text())
        for key in ("email", "html"):
            text = Path(paths[key]).read_text()
            self.assertIn("Market minutes expected: 390.", text)
            self.assertIn("Market minutes missing: 375.", text)
            self.assertIn("Stale or catchup decisions: 4.", text)
            self.assertIn("Recorded error observations: 18.", text)
            self.assertIn("Most recent worker status: error abstain.", text)
            self.assertIn("Last observation age at window end in seconds: 7200.", text)
            self.assertIn("Forward observation is incomplete or contains recorded errors.", text)
            self.assertIn("October 2, 2026 at 13:30:00 UTC through October 3, 2026 at 01:30:00 UTC", text)
        self.assertEqual(summary["campaign_measurements"]["missing_market_minutes"], 375)
        self.assertEqual(summary["campaign_measurements"]["latest_status"], "error_abstain")
        self.assertEqual(summary["campaign_measurements"]["window_end"], "2026-10-03T01:30:00+00:00")
        self.assertNotRegex(Path(paths["email"]).read_text(), r"[-\u2010-\u2015\u2212]")

    def test_observation_window_requires_timezone_and_order(self):
        with self.assertRaisesRegex(ValueError, "include a timezone"):
            generate_report(self.experiment_dir, self.root/"naive-window", campaign_status={"window_start": "2026-10-02T09:30:00"})
        with self.assertRaisesRegex(ValueError, "ends before it starts"):
            generate_report(self.experiment_dir, self.root/"reverse-window", campaign_status={
                "window_start": "2026-10-03T09:30:00+00:00",
                "window_end": "2026-10-02T09:30:00+00:00",
            })


if __name__ == "__main__":
    unittest.main()
