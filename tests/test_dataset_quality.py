import copy
import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dwight.experiments import RECOVERED_GAP_POLICY, RECOVERED_RISK_LIMITATION, experiment
from dwight.reporting import generate_report


class DatasetQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root/"QQQ-5Min.csv"
        day = datetime(2026, 9, 1, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        days = []
        while len(days) < 20:
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)
        self.missing_day, self.early_day = days[10].date().isoformat(), days[18].date().isoformat()
        # Invented flat prices: no broker access, training or outcome selection.
        with self.data.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for index, day in enumerate(days):
                if index == 10:
                    continue
                for bar in range(42 if index == 18 else 78):
                    writer.writerow([(day+timedelta(minutes=bar*5)).isoformat(), 100, 100, 100, 100, 1000])
        exclusions = [{"session": self.missing_day, "expected_minutes": 390,
                       "observed_minutes": 389, "missing_minutes": 1,
                       "early_close": False, "complete": False,
                       "reason": "incomplete_regular_session", "action": "exclude_entire_session"}]
        coverage = {"gap_policy": RECOVERED_GAP_POLICY, "requested_sessions": 20,
                    "complete_sessions": 19, "excluded_sessions": 1, "missing_rth_minutes": 1,
                    "excluded_session_fraction": .05, "max_excluded_session_fraction": .05,
                    "exclusions": exclusions}
        self.quality = {"gap_policy": RECOVERED_GAP_POLICY, "requested_session_count": 20,
                        "retained_session_count": 19, "excluded_session_count": 1,
                        "excluded_session_dates": [self.missing_day], "missing_minute_count": 1,
                        "excluded_session_fraction": .05, "max_excluded_session_fraction": .05,
                        "exclusion_reason": "incomplete_regular_session",
                        "risk_limitation": RECOVERED_RISK_LIMITATION}
        for name, value in (("coverage.json", coverage), ("exclusions.json", exclusions)):
            (self.root/name).write_text(json.dumps(value))
        self.manifest = {"schema_version": 1, "source": "alpaca", "feed": "sip", "adjustment": "raw",
                         "start": days[0].date().isoformat(), "end": days[-1].date().isoformat(),
                         "symbols": ["QQQ"], "bars": {"QQQ": {"5Min": self.data.name}},
                         "gap_policy": RECOVERED_GAP_POLICY, "research_only": True,
                         "coverage": "coverage.json", "exclusions": "exclusions.json",
                         "dataset_quality": copy.deepcopy(self.quality),
                         "files": [{"path": name, "sha256": hashlib.sha256((self.root/name).read_bytes()).hexdigest()}
                                   for name in (self.data.name, "coverage.json", "exclusions.json")]}
        keys = ("schema_version", "start", "end", "symbols", "feed", "adjustment", "files")
        self.manifest["dataset_sha256"] = hashlib.sha256(json.dumps({key: self.manifest[key] for key in keys}, sort_keys=True).encode()).hexdigest()
        self.save_manifest()

    def save_manifest(self):
        (self.root/"manifest.json").write_text(json.dumps(self.manifest))

    def test_recovery_quality_survives_experiment_and_report_with_separate_early_close_exclusion(self):
        report = experiment(self.data, "QQQ", self.root/"experiments")
        self.assertEqual(report["status"], "insufficient_data")
        self.assertTrue(report["research_only"])
        self.assertEqual(report["dataset_quality"], self.quality)
        self.assertEqual(report["complete_sessions"], 18)
        self.assertEqual([row["session"] for row in report["excluded_sessions"]], [self.early_day])
        directory = Path(report["directory"])
        self.assertEqual(json.loads((directory/"report.json").read_text())["dataset_quality"], self.quality)
        self.assertEqual(json.loads((directory/"dataset-manifest.json").read_text())["dataset_quality"], self.quality)
        with patch("dwight.reporting._charts", return_value={}):
            paths = generate_report(directory, self.root/"rendered")
        for kind in ("html", "email"):
            text = Path(paths[kind]).read_text()
            self.assertIn("1 whole sessions with missing minutes were excluded from 20 requested sessions", text)
            self.assertIn("no missing bars were forward filled", text)
            self.assertIn("not a continuous market history", text)
            self.assertIn("understate risk", text)
            self.assertIn("Early close sessions are still excluded by the strategy", text)
            self.assertIn("All fills are simulated", text)
            self.assertIn("September 15, 2026", text)
            self.assertNotIn("cover the complete recorded test period", text)
            self.assertLess(text.index("understate risk"), text.index("VWAP baseline"))
        summary = json.loads(Path(paths["summary"]).read_text())
        self.assertEqual(summary["dataset_quality"], self.quality)
        self.assertTrue(summary["research_only"])

    def test_unbound_or_inconsistent_summary_is_rejected_before_output_creation(self):
        for field, value in (("excluded_session_count", 0), ("excluded_session_dates", []),
                             ("missing_minute_count", 0)):
            with self.subTest(field=field):
                self.manifest["dataset_quality"] = {**self.quality, field: value}
                self.save_manifest()
                target = self.root/field
                with self.assertRaisesRegex(ValueError, "quality summary disagrees"):
                    experiment(self.data, "QQQ", target)
                self.assertFalse(target.exists())

    def test_coverage_bytes_and_research_only_status_are_required(self):
        self.manifest["research_only"] = False
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "research only"):
            experiment(self.data, "QQQ", self.root/"invalid-research")
        self.manifest["research_only"] = True
        self.save_manifest()
        with (self.root/"coverage.json").open("a") as stream:
            stream.write(" ")
        with self.assertRaisesRegex(ValueError, "coverage checksum"):
            experiment(self.data, "QQQ", self.root/"invalid-checksum")

    def test_report_cannot_silently_drop_the_recorded_coverage_limit(self):
        report = experiment(self.data, "QQQ", self.root/"experiments")
        directory = Path(report["directory"])
        report.pop("dataset_quality")
        (directory/"report.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "dataset quality disagrees"):
            generate_report(directory, self.root/"undisclosed")
        self.assertFalse((self.root/"undisclosed").exists())

    def test_synthetic_and_default_gap_policy_keep_existing_metadata_and_prose(self):
        for synthetic in (True, False):
            with self.subTest(synthetic=synthetic):
                self.manifest["gap_policy"] = "reject_no_forward_fill"
                self.manifest.pop("dataset_quality", None)
                self.manifest.pop("research_only", None)
                self.save_manifest()
                report = experiment(self.data, "QQQ", self.root/f"runs-{synthetic}", synthetic=synthetic)
                self.assertNotIn("dataset_quality", report)
                self.assertNotIn("research_only", report)
                with patch("dwight.reporting._charts", return_value={}):
                    paths = generate_report(Path(report["directory"]), self.root/f"rendered-{synthetic}")
                email = Path(paths["email"]).read_text()
                self.assertIn("Equity and drawdown cover the complete recorded test period.", email)
                self.assertNotIn("whole sessions with missing minutes", email)
                self.assertNotIn("dataset_quality", json.loads(Path(paths["summary"]).read_text()))


if __name__ == "__main__":
    unittest.main()
