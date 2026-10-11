import csv
from datetime import datetime, timedelta
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import stat
import tempfile
import unittest

from dwight import research_ticket
from dwight.research_ticket import freeze_ticket, verify_ticket


def install_manifest(workspace: Path, *, start="2026-09-28", end="2026-09-28", sessions=None):
    data_dir = workspace / "private-data" / "qqq"
    data_dir.mkdir(parents=True, mode=0o700)
    data_dir.chmod(0o700)
    bars = data_dir / "QQQ-5Min.csv"
    with bars.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for day in sessions or [start]:
            opened = datetime.fromisoformat(f"{day}T09:30:00-04:00")
            for index in range(78):
                stamp = opened + timedelta(minutes=5 * index)
                writer.writerow([stamp.isoformat(), 100, 100.2, 99.8, 100, 1000])
    bars.chmod(0o600)
    files = [{"path": bars.name, "sha256": hashlib.sha256(bars.read_bytes()).hexdigest()}]
    manifest = {"schema_version": "alpaca-rth-bars-v1", "source": "alpaca",
                "start": start, "end": end, "symbols": ["QQQ"], "feed": "sip",
                "adjustment": "raw", "bars": {"QQQ": {"5Min": bars.name}}, "files": files}
    fingerprint = {key: manifest[key] for key in (
        "schema_version", "start", "end", "symbols", "feed", "adjustment", "files")}
    manifest["dataset_sha256"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    path = data_dir / "manifest.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    return path, bars


def definition(manifest, *, start="2026-09-28", end="2026-09-28"):
    return {"title": "QQQ opening window comparison", "window": {"start": start, "end": end},
            "dataset_manifest": manifest.relative_to(manifest.parents[2]).as_posix(),
            "strategies": ["vwap_baseline", "relative_volume_filter"],
            "engine_config": {"capital": 10000, "commission": 0.005, "slippage": 0.01},
            "evaluation_role": "exploratory", "cost_stress_multiplier": 2}


class ResearchTicketTests(unittest.TestCase):
    def test_freeze_and_verify_binds_shared_protocol_without_running(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "workspace").resolve()
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            manifest, _ = install_manifest(root)
            result = freeze_ticket(definition(manifest), root)
            path = Path(result["ticket_path"])
            ticket = json.loads(path.read_text())

            self.assertEqual(result["status"], "frozen_not_run")
            self.assertTrue(result["data_ready"])
            self.assertFalse(result["runner_available"])
            self.assertFalse(result["execution_enabled"])
            self.assertFalse(result["replay_started"])
            self.assertEqual(ticket["definition"]["scope"]["window"]["start"], "2026-09-28")
            self.assertEqual([item["id"] for item in ticket["definition"]["strategies"]],
                             ["vwap_baseline", "relative_volume_filter"])
            self.assertEqual(ticket["definition"]["engine_config"]["capital"], 10000)
            self.assertEqual(ticket["results"], None)
            self.assertEqual(set(ticket["definition"]["implementation"]),
                             {"strategy_engine_sha256", "strategy_registry_sha256"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            verified = verify_ticket(path, root)
            self.assertEqual(verified["status"], "verified")
            self.assertTrue(verified["data_ready"])
            self.assertFalse(verified["runner_available"])

            original = research_ticket.SUPPORTED_STRATEGIES["vwap_baseline"]
            research_ticket.SUPPORTED_STRATEGIES["vwap_baseline"] = "altered-version"
            try:
                mismatch = verify_ticket(path, root)
            finally:
                research_ticket.SUPPORTED_STRATEGIES["vwap_baseline"] = original
            self.assertIn("strategy_implementation_changed", mismatch["issues"])

    def test_missing_exchange_session_is_frozen_but_blocks_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "workspace").resolve()
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            manifest, _ = install_manifest(root, start="2026-09-28", end="2026-09-29",
                                           sessions=["2026-09-28"])
            result = freeze_ticket(definition(manifest, start="2026-09-28", end="2026-09-29"), root)
            ticket = json.loads(Path(result["ticket_path"]).read_text())
            self.assertEqual(result["status"], "frozen_data_review_required")
            self.assertFalse(result["data_ready"])
            self.assertEqual(ticket["definition"]["coverage"]["missing_sessions"], ["2026-09-29"])

    def test_changed_ticket_bytes_are_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "workspace").resolve()
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            manifest, _ = install_manifest(root)
            result = freeze_ticket(definition(manifest), root)
            path = Path(result["ticket_path"])
            ticket = json.loads(path.read_text())
            ticket["definition"]["title"] = "modified after freezing"
            path.write_text(json.dumps(ticket))
            path.chmod(0o600)
            verified = verify_ticket(path, root)
            self.assertEqual(verified["status"], "mismatch")
            self.assertIn("ticket_bytes_changed", verified["issues"])
            self.assertIn("definition_changed", verified["issues"])

    def test_changed_market_data_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "workspace").resolve()
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            manifest, bars = install_manifest(root)
            result = freeze_ticket(definition(manifest), root)
            with bars.open("a") as stream:
                stream.write("2026-09-28T16:00:00-04:00,100,100,100,100,1000\n")
            bars.chmod(0o600)
            verified = verify_ticket(result["ticket_path"], root)
            self.assertEqual(verified["status"], "mismatch")
            self.assertTrue(any(issue.startswith("dataset_") for issue in verified["issues"]))

    def test_invalid_window_cohort_and_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "workspace").resolve()
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            manifest, _ = install_manifest(root)
            for changes in (
                {"window": {"start": "2026-09-29", "end": "2026-09-28"}},
                {"strategies": ["vwap_baseline"]},
                {"strategies": ["vwap_baseline", "vwap_baseline"]},
                {"dataset_manifest": "../outside.json"},
                {"evaluation_role": "production"},
                {"evaluation_role": []},
                {"cost_stress_multiplier": float("inf")},
            ):
                value = definition(manifest)
                value.update(changes)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    freeze_ticket(value, root)

    def test_workspace_must_be_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "workspace").resolve()
            root.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError, "private directory"):
                freeze_ticket({}, root)

    def test_cli_freezes_then_verifies_without_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "workspace").resolve()
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            manifest, _ = install_manifest(root)
            config = root / "research-ticket.json"
            config.write_text(json.dumps(definition(manifest)))
            config.chmod(0o600)
            repo = Path(__file__).resolve().parents[1]

            frozen = subprocess.run(
                [sys.executable, "-m", "dwight", "research-ticket-freeze",
                 "--workspace", str(root), "--config", str(config)],
                cwd=repo, check=True, capture_output=True, text=True,
            )
            payload = json.loads(frozen.stdout)
            self.assertEqual(payload["status"], "frozen_not_run")
            self.assertFalse(payload["replay_started"])

            verified = subprocess.run(
                [sys.executable, "-m", "dwight", "research-ticket-verify",
                 payload["ticket_path"], "--workspace", str(root)],
                cwd=repo, check=True, capture_output=True, text=True,
            )
            self.assertEqual(json.loads(verified.stdout)["status"], "verified")


if __name__ == "__main__":
    unittest.main()
