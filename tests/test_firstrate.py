from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from dwight.data import Session
from dwight.firstrate import import_firstrate_sample, parse_sample_minutes, download_firstrate_sample


def sample_zip(path, *, missing=False, duplicate=False):
    lines = ["timestamp,open,high,low,close,volume"]
    start = datetime(2025, 11, 28, 9, 30)
    for i in range(210 - int(missing)):
        lines.append(f'{start+timedelta(minutes=i):%Y-%m-%d %H:%M:%S},100,102,99,101,1000')
    if duplicate:
        lines.append(lines[-1])
    with ZipFile(path, "w") as archive:
        archive.writestr("QQQ_1min_sample.csv", "\n".join(lines))
        archive.writestr("_readme_documentation.txt", "QQQ split-adjusted sample data. Timezone is US Eastern.")
        archive.writestr("../../unsafe.txt", "Must never extract this unrelated member")


class FirstRateTests(unittest.TestCase):
    def test_vendor_timezone_handles_dst_without_relabelling(self):
        rows = parse_sample_minutes(b"timestamp,open,high,low,close,volume\n2025-03-07 09:30:00,100,102,99,101,1000\n2025-03-10 09:30:00,100,102,99,101,1000\n")
        self.assertTrue(rows[0]["t"].endswith("-05:00"))
        self.assertTrue(rows[1]["t"].endswith("-04:00"))

    def test_csv_extra_or_missing_fields_are_rejected(self):
        header = "timestamp,open,high,low,close,volume\n"
        for row in ("2025-03-07 09:30:00,100,102,99,101,1000,extra\n",
                    "2025-03-07 09:30:00,100,102,99,101\n"):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "row"):
                parse_sample_minutes((header + row).encode())

    def test_import_keeps_vendor_identity_private_sample_scope_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "sample.zip"
            sample_zip(archive)
            result = import_firstrate_sample(archive, root / "private")
            self.assertEqual(result["counts"]["QQQ"]["1Min"], 210)
            self.assertEqual(result["counts"]["QQQ"]["5Min"], 42)
            self.assertEqual(result["feed"], "firstrate_aggregate")
            self.assertEqual(result["adjustment"], "split")
            self.assertTrue(result["sample_only"])
            self.assertFalse(result["synthetic"])
            self.assertEqual(result["source_acquisition"], "local_archive")
            directory = Path(result["directory"])
            self.assertEqual((directory / "raw/sample.zip").read_bytes(), archive.read_bytes())
            self.assertFalse((root / "unsafe.txt").exists())
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / "raw/sample.zip").stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / "manifest.json").stat().st_mode & 0o777, 0o600)
            for record in result["files"]:
                self.assertEqual(hashlib.sha256((directory / record["path"]).read_bytes()).hexdigest(), record["sha256"])

    def test_missing_and_duplicate_minutes_fail_without_forward_fill(self):
        for kwargs in ({"missing": True}, {"duplicate": True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "sample.zip"
                sample_zip(archive, **kwargs)
                with self.assertRaises(ValueError):
                    import_firstrate_sample(archive, Path(temp) / "private")
                self.assertFalse(list(Path(temp).glob("private/*/manifest.json")))

    def test_experiment_provenance_checks_private_source_archive(self):
        from dwight.experiments import _provenance
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "sample.zip"
            sample_zip(archive)
            manifest = import_firstrate_sample(archive, root / "private")
            directory = Path(manifest["directory"])
            data = directory / "QQQ-5Min.csv"
            provenance, _ = _provenance(data, "QQQ", data.read_bytes(), False)
            self.assertEqual(provenance["source"], "firstrate")
            self.assertTrue(provenance["sample_only"])
            (directory / "raw/sample.zip").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "source file checksum"):
                _provenance(data, "QQQ", data.read_bytes(), False)

    def test_no_network_credentials_or_unbounded_download(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "sample.zip"
            sample_zip(archive)
            with patch("dwight.firstrate.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = archive.read_bytes()
                result = download_firstrate_sample(Path(temp) / "private")
                req = request.call_args.args[0]
                self.assertEqual(req.get_method(), "GET")
                self.assertNotIn("Authorization", req.headers)
                self.assertNotIn("Apca-api-key-id", req.headers)
                self.assertEqual(result["source"], "firstrate")
                self.assertEqual(result["source_acquisition"], "https_download")
                context = request.call_args.kwargs["context"]
                self.assertIsInstance(context, ssl.SSLContext)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)

    def test_tls_uses_certifi_roots_with_peer_and_hostname_verification(self):
        from dwight.firstrate import _tls_context
        try:
            import certifi
        except ImportError:
            self.skipTest("certifi is optional")
        with patch("dwight.firstrate.ssl.create_default_context", wraps=ssl.create_default_context) as create:
            context = _tls_context()
            create.assert_called_once_with(cafile=certifi.where())
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)

    def test_tls_system_fallback_keeps_verification_when_certifi_missing(self):
        from dwight.firstrate import _tls_context
        with patch.dict(sys.modules, {"certifi": None}), patch(
                "dwight.firstrate.ssl.create_default_context", wraps=ssl.create_default_context) as create:
            context = _tls_context()
            create.assert_called_once_with()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_local_timestamp_does_not_turn_claim_into_https_acquisition(self):
        from dwight.firstrate import sample_acquisition
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp)/"sample.zip"
            sample_zip(archive)
            result = import_firstrate_sample(archive, Path(temp)/"private",
                                             retrieved_at="2026-10-01T00:00:00+00:00")
            self.assertEqual(sample_acquisition(result), "local_archive")
        self.assertEqual(sample_acquisition({"retrieval_time_status": "provided",
                                            "retrieved_at": "2026-10-01T00:00:00+00:00"}),
                         "legacy_recorded_retrieval")
        self.assertEqual(sample_acquisition({"retrieved_at": "2026-10-01T00:00:00+00:00"}),
                         "legacy_recorded_retrieval")
        with self.assertRaisesRegex(ValueError, "recorded acquisition"):
            sample_acquisition({"source_acquisition": "https_download"})

    def test_provenance_rejects_synthetic_or_incoherent_vendor_flags(self):
        from dwight.experiments import _provenance
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp)/"sample.zip"
            sample_zip(archive)
            imported = import_firstrate_sample(archive, Path(temp)/"private")
            directory = Path(imported["directory"])
            path = directory/"manifest.json"
            original = json.loads(path.read_text())
            data = directory/"QQQ-5Min.csv"
            changes = [("synthetic", value) for value in (True, 0, None)]
            changes += [("sample_only", value) for value in (False, 1, None)]
            changes += [("research_only", False), ("live_feed", True), ("schema_version", 1)]
            keys = ("schema_version", "start", "end", "symbols", "feed", "adjustment", "files")
            for field, value in changes:
                with self.subTest(field=field, value=value):
                    changed = {**original, field: value}
                    # Recompute the checksum, proving schema validation rather
                    # than a stale fingerprint rejects the contradictory claim.
                    changed["dataset_sha256"] = hashlib.sha256(json.dumps(
                        {key: changed[key] for key in keys}, sort_keys=True).encode()).hexdigest()
                    path.write_text(json.dumps(changed))
                    with self.assertRaisesRegex(ValueError, "FirstRate research sample provenance"):
                        _provenance(data, "QQQ", data.read_bytes(), False)


if __name__ == "__main__":
    unittest.main()
