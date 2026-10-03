import copy
import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.data import Session, sha256_file
from dwight.experiments import _provenance
from dwight.vendor_history import (download_history, estimate_history, fingerprint,
                                   verify_manifest)

UTC = timezone.utc
DAY = "2025-11-28"
SESSION = Session(DAY, datetime(2025, 11, 28, 14, 30, tzinfo=UTC),
                  datetime(2025, 11, 28, 18, 0, tzinfo=UTC))
NOW = SESSION.close + timedelta(hours=1)


def minutes():
    return [{"t": (SESSION.open + timedelta(minutes=offset)).isoformat(),
             "o": 100 + offset / 1000, "h": 102 + offset / 1000,
             "l": 99 + offset / 1000, "c": 101 + offset / 1000, "v": 10 + offset}
            for offset in range(210)]


def fetched(provider="massive"):
    rows = minutes()
    if provider == "massive":
        metadata = {"source": "massive", "feed": "massive_stocks_aggregates", "adjustment": "raw",
                    "volume_definition": "Massive_eligible_trade_minute_aggregate_volume",
                    "limitations": ["No real-time entitlement was checked"]}
        pages = [json.dumps({"results": rows[:100]}).encode(),
                 json.dumps({"results": rows[100:]}).encode()]
    else:
        metadata = {"source": "databento", "feed": "databento_DBEQ.BASIC_ohlcv_1m",
                    "adjustment": "raw", "dataset": "DBEQ.BASIC", "schema": "ohlcv-1m",
                    "volume_definition": "Databento_dataset_eligible_trade_volume",
                    "limitations": ["Estimate is not a provider-enforced spending cap"]}
        pages = [b"".join(json.dumps(row).encode() + b"\n" for row in rows)]
    return {"records": rows, "raw_pages": pages, "provenance": metadata}


class VendorHistoryTests(unittest.TestCase):
    def download(self, output, provider="massive", fixture=None, **options):
        fixture = fetched(provider) if fixture is None else fixture
        with patch("dwight.vendor_history.exchange_sessions", return_value=[SESSION]), \
                patch("dwight.vendor_history._write_parquet", return_value=False), \
                patch("dwight." + provider + "_data.fetch_" + provider + "_history",
                      return_value=fixture) as fetch:
            kwargs = dict(environ={}, now=NOW)
            if provider == "databento":
                kwargs.update(dataset="DBEQ.BASIC", max_cost_usd="0.50")
            kwargs.update(options)
            result = download_history(provider, output, DAY, DAY, **kwargs)
        return result, fetch

    def test_complete_sessions_private_csv_raw_and_research_provenance(self):
        for provider in ("massive", "databento"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                output = Path(temp).resolve() / "private-history"
                fixture = fetched(provider)
                manifest, fetch = self.download(output, provider, fixture)
                directory = Path(manifest["directory"])
                self.assertEqual(fetch.call_args.args[:2], (SESSION.open, SESSION.close))
                self.assertEqual(manifest["source"], provider)
                self.assertEqual(manifest["feed"], fixture["provenance"]["feed"])
                self.assertTrue(manifest["research_only"])
                self.assertFalse(manifest["live_feed"])
                self.assertFalse(manifest["synthetic"])
                self.assertEqual(manifest["source_acquisition"], "https_download")
                self.assertEqual(manifest["adjustment"], "raw")
                self.assertEqual(manifest["counts"], {"QQQ": {"1Min": 210, "5Min": 42}})
                self.assertEqual(manifest["dataset_sha256"], fingerprint(manifest))
                stored = json.loads((directory / "manifest.json").read_bytes())
                self.assertNotIn("directory", stored)
                extension = "json" if provider == "massive" else "jsonl"
                for number, raw in enumerate(fixture["raw_pages"], 1):
                    self.assertEqual((directory / f"raw/page-{number:06d}.{extension}").read_bytes(), raw)
                with (directory / "QQQ-5Min.csv").open(newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(rows[0]["timestamp"], SESSION.open.isoformat())
                self.assertEqual(rows[-1]["timestamp"], (SESSION.close - timedelta(minutes=5)).isoformat())
                self.assertEqual(rows[-1]["session_close"], SESSION.close.isoformat())
                self.assertEqual(float(rows[0]["volume"]), sum(row["v"] for row in minutes()[:5]))
                for entry in manifest["files"]:
                    self.assertEqual(sha256_file(directory / entry["path"]), entry["sha256"])
                verify_manifest(manifest, directory)
                if os.name == "posix":
                    for path in (output, *output.rglob("*")):
                        self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_experiment_reloader_retains_vendor_research_identity(self):
        for provider in ("massive", "databento"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                manifest, _ = self.download(Path(temp).resolve() / "private", provider)
                directory = Path(manifest["directory"])
                data = directory / "QQQ-5Min.csv"
                source, loaded = _provenance(data, "QQQ", data.read_bytes(), False)
                self.assertEqual(source["source"], provider)
                self.assertEqual(source["feed"], manifest["feed"])
                self.assertEqual(source["dataset_sha256"], manifest["dataset_sha256"])
                self.assertTrue(source["research_only"])
                self.assertFalse(source["live_feed"])
                self.assertEqual(loaded["provider_metadata"], manifest["provider_metadata"])

    def test_each_evidence_file_corruption_rejects_reload(self):
        for file in ("QQQ-1Min.csv", "QQQ-5Min.csv", "sessions.json", "raw/page-000001.json"):
            with self.subTest(file=file), tempfile.TemporaryDirectory() as temp:
                manifest, _ = self.download(Path(temp).resolve() / "private")
                directory = Path(manifest["directory"])
                evidence = directory / file
                evidence.write_bytes(evidence.read_bytes() + b" ")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    verify_manifest(manifest, directory)
                csv = directory / "QQQ-5Min.csv"
                with self.assertRaises(ValueError):
                    _provenance(csv, "QQQ", csv.read_bytes(), False)

    def test_source_feed_and_research_tampering_rejected_even_after_refingerprinting(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, _ = self.download(Path(temp).resolve() / "private")
            directory = Path(manifest["directory"])
            mutations = ({"source": "alpaca"}, {"feed": "sip"}, {"research_only": False},
                         {"live_feed": True}, {"adjustment": "split"}, {"synthetic": True})
            for values in mutations:
                with self.subTest(values=values):
                    altered = copy.deepcopy(manifest)
                    altered.update(values)
                    altered["dataset_sha256"] = fingerprint(altered)
                    with self.assertRaisesRegex(ValueError, "source metadata"):
                        verify_manifest(altered, directory)
            for field, value in (("source", "alpaca"), ("feed", "sip"), ("adjustment", "split")):
                with self.subTest(metadata_field=field):
                    altered = copy.deepcopy(manifest)
                    altered["provider_metadata"][field] = value
                    altered["dataset_sha256"] = fingerprint(altered)
                    with self.assertRaisesRegex(ValueError, "source metadata"):
                        verify_manifest(altered, directory)

    def test_provenance_fingerprint_detects_metadata_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, _ = self.download(Path(temp).resolve() / "private")
            altered = copy.deepcopy(manifest)
            altered["provider_metadata"]["limitations"].append("modified metadata")
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                verify_manifest(altered, manifest["directory"])

    def test_missing_minute_preserves_raw_failure_but_has_no_success_manifest(self):
        fixture = fetched()
        fixture["records"].pop(7)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp).resolve() / "private"
            with self.assertRaisesRegex(ValueError, "Missing regular-session minute"):
                self.download(output, fixture=fixture)
            self.assertEqual(list(output.glob("*/manifest.json")), [])
            failures = list(output.glob("*/failed.json"))
            self.assertEqual(len(failures), 1)
            failure = json.loads(failures[0].read_bytes())
            self.assertEqual(failure["error_type"], "ValueError")
            self.assertEqual(len(failure["raw_pages"]), 2)
            self.assertEqual((failures[0].parent / "raw/page-000001.json").read_bytes(), fixture["raw_pages"][0])

    def test_provider_source_and_missing_raw_evidence_never_issue_manifest(self):
        for mutation in ("source", "raw_pages"):
            fixture = fetched()
            if mutation == "source":
                fixture["provenance"]["source"] = "alpaca"
            else:
                fixture["raw_pages"] = []
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                output = Path(temp).resolve() / "private"
                with self.assertRaises(ValueError):
                    self.download(output, fixture=fixture)
                self.assertFalse(list(output.glob("*/manifest.json")))
                self.assertEqual(len(list(output.glob("*/failed.json"))), 1)

    def test_future_and_empty_session_ranges_fail_before_fetch_or_output(self):
        with tempfile.TemporaryDirectory() as temp, patch("dwight.massive_data.fetch_massive_history") as fetch:
            output = Path(temp).resolve() / "private"
            with patch("dwight.vendor_history.exchange_sessions", return_value=[SESSION]), \
                    self.assertRaisesRegex(ValueError, "not fully completed"):
                download_history("massive", output, DAY, DAY, now=SESSION.close)
            with patch("dwight.vendor_history.exchange_sessions", return_value=[]), \
                    self.assertRaisesRegex(ValueError, "No exchange sessions"):
                download_history("massive", output, DAY, DAY, now=NOW)
            with patch("dwight.vendor_history.exchange_sessions", return_value=[SESSION]), \
                    self.assertRaisesRegex(ValueError, "timezone aware"):
                download_history("massive", output, DAY, DAY, now=NOW.replace(tzinfo=None))
            fetch.assert_not_called()
            self.assertFalse(output.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX symlink contract")
    def test_symlink_output_and_parent_rejected_before_fetch(self):
        with tempfile.TemporaryDirectory() as temp, patch("dwight.massive_data.fetch_massive_history") as fetch, \
                patch("dwight.vendor_history.exchange_sessions", return_value=[SESSION]):
            root = Path(temp).resolve()
            actual = root / "actual"
            actual.mkdir()
            link = root / "link"
            link.symlink_to(actual, target_is_directory=True)
            for output in (link, link / "child"):
                with self.subTest(output=output), self.assertRaisesRegex(ValueError, "symbolic links"):
                    download_history("massive", output, DAY, DAY, now=NOW)
            fetch.assert_not_called()
            self.assertEqual(list(actual.iterdir()), [])

    def test_provider_specific_options_are_required_or_rejected_before_transport(self):
        with tempfile.TemporaryDirectory() as temp, patch("dwight.vendor_history.exchange_sessions") as calendar, \
                patch("dwight.databento_data.fetch_databento_history") as databento, \
                patch("dwight.massive_data.fetch_massive_history") as massive:
            cases = (("databento", {}), ("databento", {"dataset": "DBEQ.BASIC"}),
                     ("databento", {"max_cost_usd": 0}), ("massive", {"dataset": "DBEQ.BASIC"}),
                     ("massive", {"max_cost_usd": 0}), ("unsupported", {}))
            for provider, options in cases:
                with self.subTest(provider=provider, options=options), self.assertRaises(ValueError):
                    download_history(provider, Path(temp).resolve() / "private", DAY, DAY, **options)
            calendar.assert_not_called()
            databento.assert_not_called()
            massive.assert_not_called()

    def test_databento_explicit_dataset_and_cost_pass_to_only_selected_fetch(self):
        with tempfile.TemporaryDirectory() as temp, patch("dwight.massive_data.fetch_massive_history") as massive:
            marker = object()
            manifest, fetch = self.download(Path(temp).resolve() / "private", "databento", transport=marker)
            self.assertEqual(fetch.call_args.kwargs, {"dataset": "DBEQ.BASIC", "max_cost_usd": "0.50",
                                                     "transport": marker})
            massive.assert_not_called()
            self.assertEqual(manifest["provider_metadata"]["dataset"], "DBEQ.BASIC")

    def test_estimate_is_metadata_only_and_uses_session_half_open_bounds(self):
        with patch("dwight.vendor_history.exchange_sessions", return_value=[SESSION]), \
                patch("dwight.databento_data.estimate_databento_history",
                      return_value={"source": "databento", "estimated_cost_usd": "0.05"}) as estimate, \
                patch("dwight.databento_data.fetch_databento_history") as fetch:
            result = estimate_history(DAY, DAY, dataset="DBEQ.BASIC", environ={}, now=NOW)
            self.assertEqual(estimate.call_args.args, (SESSION.open, SESSION.close, {}))
            self.assertEqual(estimate.call_args.kwargs, {"dataset": "DBEQ.BASIC", "transport": None})
            self.assertEqual(result["session_count"], 1)
            self.assertEqual(result["session_start"], DAY)
            self.assertFalse(result["submits_orders"])
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
