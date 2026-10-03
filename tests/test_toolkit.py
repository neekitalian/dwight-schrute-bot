import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.experiments import DEFAULTS
from dwight.toolkit import init_workspace, toolkit_status


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS may give /var as an alias for /private/var; callers use a real
        # path because the initializer intentionally rejects symlink traversal.
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / "workspace"

    def write_json(self, relative, value):
        path = self.root / relative
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def status(self, environ=None):
        with patch("dwight.toolkit.metadata.version", return_value="1.2.3"):
            return toolkit_status(self.root, environ=environ or {})

    def sample_manifest(self):
        return {"schema_version": "firstrate-sample-rth-bars-v1", "source": "firstrate",
                "feed": "firstrate_aggregate", "adjustment": "split", "symbols": ["QQQ"],
                "start": "2025-11-26", "end": "2025-11-26", "dataset_sha256": "0" * 64,
                "bars": {"QQQ": {"5Min": "QQQ-5Min.csv"}},
                "files": [{"path": "QQQ-5Min.csv", "sha256": "0" * 64}]}

    def test_init_private_defaults_and_refuses_overwrite(self):
        result = init_workspace(self.root)
        self.assertFalse(result["submits_orders"])
        settings = json.loads((self.root / "workspace.json").read_text())
        self.assertEqual((settings["symbol"], settings["timeframe"], settings["mode"], settings["execution"]),
                         ("QQQ", "5Min", "shadow", "manual"))
        real = json.loads((self.root / "configs/experiment.json").read_text())
        self.assertEqual(real, DEFAULTS)
        self.assertNotIn("tracking_uri", json.loads((self.root / "configs/walkforward.json").read_text()))
        self.assertFalse((self.root / ".env").exists())
        self.assertIn("APCA_API_KEY_ID=\n", (self.root / ".env.example").read_text())
        self.assertIn("DATABENTO_API_KEY=\n", (self.root / ".env.example").read_text())
        self.assertIn("MASSIVE_API_KEY=\n", (self.root / ".env.example").read_text())
        if os.name == "posix":
            for path in (self.root, *self.root.rglob("*")):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        previous = (self.root / "workspace.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            init_workspace(self.root)
        self.assertEqual(previous, (self.root / "workspace.json").read_bytes())

    def test_symlink_and_missing_parent_rejected(self):
        self.root.symlink_to(self.parent / "missing", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            init_workspace(self.root)
        self.root.unlink()
        alias = self.parent / "alias"
        alias.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            init_workspace(alias / "new")
        with self.assertRaisesRegex(ValueError, "parent directory"):
            init_workspace(self.parent / "absent" / "new")
        self.assertFalse((self.parent / "absent").exists())

    def test_status_presence_only_read_only_and_no_model_approval(self):
        init_workspace(self.root)
        sentinel = "not-a-real-secret-do-not-print"
        env_file = self.root / ".env"
        env_file.write_text(f'APCA_API_KEY_ID={sentinel}\nAPCA_API_SECRET_KEY={sentinel}\n')
        env_file.chmod(0o600)
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        environment = dict(os.environ)
        result = self.status()
        self.assertEqual(result["status"], "checked")
        self.assertTrue(result["preparation"]["synthetic_demo"])
        self.assertTrue(result["preparation"]["alpaca_history_download"])
        self.assertFalse(result["preparation"]["selected_dataset_research"])
        self.assertFalse(result["submits_orders"])
        self.assertFalse(result["model_approved"])
        self.assertFalse(result["network_checked"])
        self.assertFalse(result["paper_account_connected"])
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertEqual(environment, dict(os.environ))

    def test_missing_keys_and_alias_presence(self):
        init_workspace(self.root)
        self.assertFalse(self.status()["preparation"]["alpaca_history_download"])
        aliases = {"ALPACA_API_KEY": "key-sentinel", "ALPACA_SECRET_KEY": "secret-sentinel"}
        result = self.status(aliases)
        self.assertTrue(result["preparation"]["alpaca_history_download"])
        self.assertNotIn("secret-sentinel", json.dumps(result))

    def test_unsafe_env_and_escape_paths_fail_closed(self):
        init_workspace(self.root)
        (self.root / ".env").symlink_to(self.parent / "unavailable-secrets")
        result = self.status()
        self.assertIn("workspace_env_unsafe_or_invalid", result["issues"])
        self.assertFalse(result["preparation"]["alpaca_history_download"])
        (self.root / ".env").unlink()
        settings = json.loads((self.root / "workspace.json").read_text())
        settings["runs_dir"] = "../outside"
        self.write_json("workspace.json", settings)
        self.assertIn("workspace_invalid_or_not_private", self.status()["issues"])

    def test_scope_and_real_sample_gates_cannot_silently_change(self):
        init_workspace(self.root)
        settings = json.loads((self.root / "workspace.json").read_text())
        for key, value in (("symbol", "SPY"), ("mode", "live"), ("execution", "automatic")):
            self.write_json("workspace.json", {**settings, key: value})
            self.assertFalse(self.status()["preparation"]["synthetic_demo"])
        self.write_json("workspace.json", settings)
        self.write_json("configs/experiment.json", {**DEFAULTS, "min_train_samples": 1})
        self.assertIn("workspace_invalid_or_not_private", self.status()["issues"])

    def test_dataset_integrity_and_shadow_source_are_separate(self):
        init_workspace(self.root)
        dataset = self.root / "private-data" / "sample"
        dataset.mkdir(mode=0o700)
        (dataset / "QQQ-5Min.csv").write_text("fixture bytes checked by mocked provenance\n")
        (dataset / "QQQ-5Min.csv").chmod(0o600)
        self.write_json("private-data/sample/manifest.json", self.sample_manifest())
        settings = json.loads((self.root / "workspace.json").read_text())
        settings["dataset_manifest"] = "private-data/sample/manifest.json"
        self.write_json("workspace.json", settings)
        source = {"source": "firstrate", "feed": "firstrate_aggregate", "adjustment": "split",
                  "source_acquisition": "https_download"}
        with patch("dwight.experiments._provenance", return_value=(source, {})):
            result = self.status()
        self.assertTrue(result["preparation"]["selected_dataset_research"])
        self.assertFalse(result["dataset"]["shadow_source_compatible"])
        with patch("dwight.experiments._provenance", return_value=({**source, "source_acquisition": "local_archive"}, {})):
            self.assertFalse(self.status()["preparation"]["selected_dataset_research"])
        with patch("dwight.experiments._provenance", side_effect=ValueError("secret in unsafe exception")):
            result = self.status()
        self.assertEqual(result["dataset"]["status"], "invalid_or_unavailable")
        self.assertNotIn("secret in unsafe exception", json.dumps(result))

    def test_malformed_optional_dataset_metadata_is_a_redacted_preparation_issue(self):
        init_workspace(self.root)
        dataset = self.root / "private-data" / "sample"
        dataset.mkdir(mode=0o700)
        (dataset / "QQQ-5Min.csv").write_text("fixture\n")
        (dataset / "QQQ-5Min.csv").chmod(0o600)
        settings = json.loads((self.root / "workspace.json").read_text())
        settings["dataset_manifest"] = "private-data/sample/manifest.json"
        self.write_json("workspace.json", settings)
        private = "private-dataset-sentinel-must-not-be-printed"
        bad_fields = [
            {"files": [1]}, {"files": None}, {"files": {"path": private}},
            {"files": [{}]}, {"files": [{"path": [], "sha256": "0" * 64}]},
            {"files": [{"path": "QQQ-5Min.csv", "sha256": private}]},
            {"bars": []}, {"bars": {"QQQ": private}},
            {"bars": {"QQQ": {"5Min": [private]}}}, {"source": [private]},
            {"adjustment": {"value": private}}, {"dataset_sha256": [private]},
        ]
        for changes in bad_fields:
            with self.subTest(changes=changes):
                self.write_json("private-data/sample/manifest.json", {**self.sample_manifest(), **changes})
                with patch("dwight.experiments._provenance", side_effect=AssertionError("shape must reject before provenance")):
                    result = self.status()
                self.assertEqual(result["dataset"]["status"], "invalid_or_unavailable")
                self.assertIn("dataset_provenance_invalid_or_unavailable", result["issues"])
                self.assertFalse(result["preparation"]["selected_dataset_research"])
                self.assertNotIn(private, json.dumps(result))
        # The underlying helper rereads the file. Even a concurrent malformed
        # replacement must not turn this optional preparation check into a crash.
        self.write_json("private-data/sample/manifest.json", self.sample_manifest())
        with patch("dwight.experiments._provenance", side_effect=AttributeError(private)):
            result = self.status()
        self.assertEqual(result["dataset"]["status"], "invalid_or_unavailable")
        self.assertNotIn(private, json.dumps(result))

    @unittest.skipUnless(os.name == "posix", "POSIX permission validation")
    def test_broad_file_permissions_report_preparation_issue(self):
        init_workspace(self.root)
        (self.root / "workspace.json").chmod(0o644)
        self.assertIn("workspace_invalid_or_not_private", self.status()["issues"])


if __name__ == "__main__":
    unittest.main()
