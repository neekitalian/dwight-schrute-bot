"""Deployment must use committed source and exclude unrelated local content."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_hf_space


class SpaceBundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-q")
        (self.root / "app.py").write_text("print('committed')\n")
        (self.root / "card.md").write_text("Space metadata\n")
        self.git("add", "app.py", "card.md")
        self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.STDOUT)

    def commit(self):
        self.git("-c", "user.name=Dwight test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")

    def stage(self):
        with patch.object(build_hf_space, "FILES", ("app.py",)), \
             patch.object(build_hf_space, "MAPPED_FILES", {"card.md": "README.md"}):
            return build_hf_space.stage(self.root / "bundle", root=self.root)

    def test_only_committed_allowlist_is_uploaded(self):
        (self.root / ".env").write_text("FAKE_SECRET=never-upload\n")
        (self.root / "private-data").mkdir()
        (self.root / "private-data/history.csv").write_text("private data\n")
        (self.root / "app.py").write_text("print('uncommitted edit')\n")
        report = self.stage()
        bundle = self.root / "bundle"
        self.assertEqual(sorted(p.name for p in bundle.iterdir()),
                         ["README.md", "app.py", "source-manifest.json"])
        raw = b"print('committed')\n"
        self.assertEqual((bundle / "app.py").read_bytes(), raw)
        self.assertEqual(report["files"]["app.py"], hashlib.sha256(raw).hexdigest())
        self.assertFalse(report["broker_execution_enabled"])

    def test_existing_bundle_is_never_overwritten(self):
        self.stage()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.stage()

    def test_committed_symlink_is_rejected_before_output(self):
        (self.root / "app.py").unlink()
        (self.root / "app.py").symlink_to(".env")
        self.git("add", "app.py")
        self.commit()
        with self.assertRaisesRegex(ValueError, "regular committed"):
            self.stage()
        self.assertFalse((self.root / "bundle").exists())


if __name__ == "__main__":
    unittest.main()
