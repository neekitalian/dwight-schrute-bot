import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from zipfile import ZipFile


SPEC = importlib.util.spec_from_file_location("toolkit_builder", Path(__file__).resolve().parents[1]/"scripts/build_toolkit_release.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class ToolkitReleaseTests(unittest.TestCase):
    def test_vercel_source_allowlist_preserves_portal_without_extra_assets(self):
        for name in ("app.py", "vercel.json", ".vercelignore", "deploy/vercel/index.html"):
            self.assertTrue(BUILDER.allowed_path(name), name)
        for name in ("deploy/vercel/account.json", "deploy/vercel/export.html", ".vercel/project.json",
                     "deploy/vercel/.env", "deploy/vercel/private-notes/pricing.md"):
            self.assertFalse(BUILDER.allowed_path(name), name)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"repository"
        self.root.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Toolkit Test")
        self.git("config", "user.email", "toolkit-test@example.invalid")
        initial = {
            "LICENSE": "Apache license fixture\n", "NOTICE": "VWAP attribution fixture\n",
            "docs/VWAP-LICENSE": "Upstream license fixture\n",
            "pyproject.toml": '[project]\nname = "dwight-schrute-bot"\nversion = "0.3.0"\nrequires-python = ">=3.11"\n',
            "dwight/__init__.py": "", "dwight/__main__.py": "print('committed')\n",
            "vwap_bot/engine.py": "# committed engine\n",
            ".env.example": "APCA_API_KEY_ID=\nAPCA_API_SECRET_KEY=\nDWIGHT_DATA_FEED=sip\n",
            "scripts/launch.sh": "#!/bin/sh\nprintf 'test\\n'\n",
            "deploy/linux/dwight.service": "[Unit]\nDescription=Fixture\n",
            "deploy/linux/tradingview-nginx.conf.example": "# Unconfigured proxy fixture\n",
            "examples/tradingview/qqq_observer.pine": "// Uncompiled observation template\n",
            "examples/manual-fills.csv": "fill_id,data_kind,price\nfixture,synthetic,100\n",
            ".env": "APCA_API_SECRET_KEY=PRIVATE_FIXTURE\n",
            "private-data/QQQ.csv": "PRIVATE_PRICES\n",
            "models/model.json": "PRIVATE_MODEL\n",
            "docs/raw/private.md": "PRIVATE_NOTES\n",
            "docs/product-strategy.md": "PRIVATE_BUSINESS_STRATEGY\n",
            "docs/private-notes/pricing.md": "PRIVATE_BUSINESS_PRICING\n",
            "docs/guides/private-notes/sale.md": "PRIVATE_BUSINESS_SALE\n",
            "docs/internal-business/valuation.md": "PRIVATE_BUSINESS_VALUATION\n",
            "internal-business/exit.md": "PRIVATE_BUSINESS_EXIT\n",
            "scripts/internal-business/publish.py": "# PRIVATE_BUSINESS_SCRIPT\n",
            "docs/Private-Notes/revenue.md": "PRIVATE_BUSINESS_REVENUE\n",
            "docs/trading-exits.md": "Protective trading exits and stop loss rules\n",
            "docs/data-provider-costs.md": "Provider estimates and billing limits\n",
            "docs/product.md": "Technical user workflow and product boundaries\n",
            ".github/workflows/tests.yml": "name: excluded workflow\n",
        }
        for name, text in initial.items():
            self.write(name, text)
        (self.root/"scripts/launch.sh").chmod(0o755)
        self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE)

    def write(self, name, text):
        target = self.root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "fixture")

    def build(self, directory):
        return BUILDER.build_release(Path(self.temp.name)/directory, self.root)

    def rewrite(self, original, filename, mutate):
        target = Path(self.temp.name)/filename
        with ZipFile(original) as src, ZipFile(target, "w") as dst:
            for entry in src.infolist():
                dst.writestr(entry, mutate(entry.filename, src.read(entry)))
        return target

    def test_deterministic_zip_uses_committed_blobs_and_omits_private_data(self):
        self.write("dwight/__main__.py", "print('UNCOMMITTED_SECRET')\n")
        self.write("dwight/untracked.py", "# UNTRACKED_SECRET\n")
        first, second = self.build("first"), self.build("second")
        self.assertEqual(Path(first["archive"]).read_bytes(), Path(second["archive"]).read_bytes())
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["version"], "0.3.0")
        self.assertEqual(first["source_commit"], self.git("rev-parse", "HEAD").decode().strip())
        with ZipFile(first["archive"]) as archive:
            prefix = first["name"]+"/"
            self.assertEqual(archive.read(prefix+"dwight/__main__.py"), b"print('committed')\n")
            self.assertNotIn(prefix+"dwight/untracked.py", archive.namelist())
            for excluded in (".env", "private-data/QQQ.csv", "models/model.json", "docs/raw/private.md",
                             "docs/product-strategy.md", "docs/private-notes/pricing.md",
                             "docs/guides/private-notes/sale.md", "docs/internal-business/valuation.md",
                             "internal-business/exit.md", "scripts/internal-business/publish.py",
                             "docs/Private-Notes/revenue.md", ".github/workflows/tests.yml"):
                self.assertNotIn(prefix+excluded, archive.namelist())
            for included in ("docs/trading-exits.md", "docs/data-provider-costs.md", "docs/product.md"):
                self.assertIn(prefix+included, archive.namelist())
            self.assertIn(prefix+".env.example", archive.namelist())
            self.assertIn(prefix+"deploy/linux/tradingview-nginx.conf.example", archive.namelist())
            self.assertIn(prefix+"examples/tradingview/qqq_observer.pine", archive.namelist())
            self.assertEqual((archive.getinfo(prefix+"scripts/launch.sh").external_attr >> 16) & 0o777, 0o755)
            manifest = json.loads(archive.read(prefix+BUILDER.MANIFEST))
            self.assertTrue(BUILDER.REQUIRED <= manifest["files"].keys())
            self.assertFalse(any(b"PRIVATE_BUSINESS_" in archive.read(name) for name in archive.namelist()))
        verified = BUILDER.verify_release(first["archive"], first["sha256"])
        self.assertTrue(verified["verified"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build("first")

    def test_modified_payload_and_wrong_external_digest_fail_verification(self):
        release = self.build("release")
        changed = self.rewrite(release["archive"], "tampered.zip",
                               lambda name, data: data+b"modified" if name.endswith("dwight/__main__.py") else data)
        with self.assertRaisesRegex(ValueError, "checksum/metadata mismatch"):
            BUILDER.verify_release(changed)
        with self.assertRaisesRegex(ValueError, "ZIP checksum mismatch"):
            BUILDER.verify_release(release["archive"], "0"*64)

    def test_verifier_rejects_unmanifested_or_traversal_members(self):
        release = self.build("release")
        for name in (release["name"]+"/dwight/extra.py", release["name"]+"/../outside.py"):
            with self.subTest(name=name):
                changed = Path(self.temp.name)/(hashlib.sha256(name.encode()).hexdigest()+".zip")
                changed.write_bytes(Path(release["archive"]).read_bytes())
                with ZipFile(changed, "a") as archive:
                    info, data = BUILDER._zip_entry(name, b"unlisted")
                    archive.writestr(info, data)
                with self.assertRaises(ValueError):
                    BUILDER.verify_release(changed)

    def test_allowlisted_symlink_is_rejected(self):
        (self.root/"dwight/alias.py").symlink_to("__main__.py")
        self.commit()
        with self.assertRaisesRegex(ValueError, "regular committed"):
            self.build("symlink")

    def test_nonempty_env_credentials_and_secret_tokens_are_rejected(self):
        self.write(".env.example", "APCA_API_SECRET_KEY=fixture-secret\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "nonempty credential"):
            self.build("bad-env")
        self.write(".env.example", "APCA_API_SECRET_KEY=\n")
        self.write("dwight/leak.py", "token = '"+"ghp_"+"X"*36+"'\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "credential-shaped"):
            self.build("bad-token")

    def test_manual_fill_example_must_explicitly_be_synthetic(self):
        self.write("examples/manual-fills.csv", "fill_id,data_kind,price\nprivate,real,100\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "synthetic rows only"):
            self.build("real-example")


if __name__ == "__main__":
    unittest.main()
