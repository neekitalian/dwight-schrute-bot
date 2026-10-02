"""One fully local fixture verifies the private end-to-end sample-review flow."""
from datetime import datetime, timedelta
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from dwight.firstrate import download_firstrate_sample, import_firstrate_sample
from scripts.review_qqq_sample import review_sample


@unittest.skipUnless(importlib.util.find_spec("matplotlib") and importlib.util.find_spec("exchange_calendars"),
                     "reporting and data extras required")
class QQQSampleReviewTests(unittest.TestCase):
    def test_private_review_keeps_real_data_gates_and_separates_direction_policies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "fixture.zip"
            start = datetime(2025, 11, 26, 9, 30)
            rows = ["timestamp,open,high,low,close,volume"]
            for minute in range(390):
                rows.append(f'{start+timedelta(minutes=minute):%Y-%m-%d %H:%M:%S},100,101,99,100,1000')
            # Generated fixture tests ingestion/formatting only. It is never
            # published or presented as an observed market sample.
            with ZipFile(archive, "w") as zipfile:
                zipfile.writestr("QQQ_1min_sample.csv", "\n".join(rows))
                zipfile.writestr("_readme_documentation.txt", "QQQ split-adjusted sample. US Eastern.")
            local_dataset = import_firstrate_sample(archive, root / "local-private-data")
            with self.assertRaisesRegex(ValueError, "local archives are unverified"):
                review_sample(Path(local_dataset["directory"]) / "manifest.json", root / "rejected-review")
            self.assertFalse((root / "rejected-review").exists())
            # Mock the downloader transport; this tests its provenance contract,
            # not vendor authenticity, and makes no network request.
            with patch("dwight.firstrate.urlopen", return_value=BytesIO(archive.read_bytes())):
                dataset = download_firstrate_sample(root / "private-data")
            output = root / "review"
            with patch.dict(os.environ, {"MPLCONFIGDIR": str(root / "plot-cache"), "XDG_CACHE_HOME": str(root / "cache")}), \
                    patch("dwight.enrichment._fit", side_effect=AssertionError("insufficient fixture must not train")), \
                    patch("dwight.experiments.fit_model", side_effect=AssertionError("insufficient fixture must not train")):
                result = review_sample(Path(dataset["directory"]) / "manifest.json", output)
            summary = json.loads(Path(result["summary"]).read_text())
            html = Path(result["html"]).read_text()
            self.assertEqual(result["chronological_status"], "insufficient_data")
            self.assertEqual(result["enrichment_status"], "insufficient_data")
            self.assertEqual(summary["bars"], 78)
            self.assertEqual(summary["source_acquisition"], "https_download")
            self.assertIn("dwight/firstrate.py", summary["source_hashes"])
            self.assertIn("dwight/enrichment.py", summary["source_hashes"])
            self.assertIn("dwight/reporting.py", summary["source_hashes"])
            self.assertEqual(summary["enrichment"]["minimum_sessions_for_one_window"], 240)
            self.assertEqual(summary["chronological_research"]["direction_policy"], "long_and_short")
            self.assertFalse(summary["promotion_eligible"])
            self.assertFalse(summary["submits_orders"])
            self.assertEqual(summary["global_holdout_status"], "full_sample_exploratory_already_inspected")
            changes = {key for key in summary["baseline_config"]
                       if summary["baseline_config"][key] != summary["stress_config"][key]}
            self.assertEqual(changes, {"commission", "slippage"})
            for key in changes:
                self.assertEqual(summary["stress_config"][key], 2*summary["baseline_config"][key])
            self.assertIn("No model was fitted.", html)
            self.assertEqual(html.count('src="data:image/png;base64,'), 2)
            self.assertNotIn("<script", html.lower())
            self.assertNotIn('href="input.csv', html)
            self.assertTrue(list(output.glob("chronological/*/report.json")))
            self.assertTrue((output / "chronological-charts/report.html").is_file())
            self.assertTrue(list(output.glob("enrichment/*/report.json")))
            self.assertFalse(list(output.rglob("model.json")))
            if os.name == "posix":
                self.assertEqual(output.stat().st_mode & 0o777, 0o700)
                for path in output.rglob("*"):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
            with self.assertRaisesRegex(ValueError, "nonexistent"):
                review_sample(Path(dataset["directory"]) / "manifest.json", output)


if __name__ == "__main__":
    unittest.main()
