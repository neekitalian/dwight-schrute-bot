"""Licensed research snapshots stay private even under permissive POSIX umasks."""
import csv
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from dwight.enrichment import run_enrichment
from dwight.experiments import experiment, _write
from dwight.walkforward import run_walkforward


@unittest.skipUnless(os.name == "posix", "POSIX mode enforcement")
class ResearchPrivacyTests(unittest.TestCase):
    def test_all_runners_restrict_existing_outputs_snapshots_and_fold_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "fixture.csv"
            with data.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                for day in range(5):
                    start = datetime(2026, 9, 21 + day, 9, 30, tzinfo=ZoneInfo("America/New_York"))
                    for i in range(78):
                        writer.writerow([(start + timedelta(minutes=5*i)).isoformat(), 100, 101, 99, 100, 1000])
            config = {"train_sessions": 1, "validation_sessions": 1, "test_sessions": 1,
                      "holdout_sessions": 1, "max_windows": 1}
            runners = {
                "experiment": lambda output: experiment(data, "QQQ", output, synthetic=True),
                "walkforward": lambda output: run_walkforward(data, output, synthetic=True, config=config),
                "enrichment": lambda output: run_enrichment(data, output, synthetic=True, config=config),
            }
            prior_umask = os.umask(0)
            try:
                for name, runner in runners.items():
                    with self.subTest(runner=name):
                        output = root / name
                        output.mkdir(mode=0o777)
                        for attempt in range(2):
                            output.chmod(0o777)  # Existing directory may predate private defaults.
                            report = runner(output)
                            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
                            directory = Path(report["directory"])
                            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                            self.assertEqual((directory / "input.csv").stat().st_mode & 0o777, 0o600)
                            for artifact in directory.rglob("*"):
                                expected = 0o700 if artifact.is_dir() else 0o600
                                self.assertEqual(artifact.stat().st_mode & 0o777, expected, str(artifact))
            finally:
                os.umask(prior_umask)

    def test_rewriting_existing_json_removes_public_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "report.json"
            artifact.write_text("{}")
            artifact.chmod(0o666)
            _write(artifact, {"status": "insufficient_data"})
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
