"""Study lifecycle checks using temporary, invented unit-test evidence only.

No credentials, downloaded market prices, genuine model outcomes or network
requests are used here. Lifecycle mocks do not qualify a real research model.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dwight import study
from dwight.data import Session, sha256_file
from vwap_bot.engine import Bar, Config


RECIPE = json.loads((Path(__file__).resolve().parents[1]/"configs/qqq-study.json").read_text())


def stress_fixture(model_sha256="unit-test-model-digest"):
    def metrics(pnl, drawdown):
        return {"trades": 30, "net_pnl": pnl, "max_bar_close_drawdown": drawdown}
    return {"fixture": "Invented unit-test metrics, not market performance", "refitted": False,
            "model_sha256": model_sha256,
            "cases": {name: {"evaluation": {"baseline": metrics(3, .03),
                                            "simple_volume": metrics(4, .02),
                                            "filtered": metrics(5, .01)}}
                      for name in ("standard", "double_cost")}}


class StudyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/"study"
        self.recipe = deepcopy(RECIPE)
        study.prepare(self.root, self.recipe)

    def state(self):
        return json.loads((self.root/"state.json").read_text())

    def fixture_history(self):
        folder = self.root/"unit-test-fixtures"
        folder.mkdir(mode=0o700)
        data = folder/"not-market-data.csv"
        data.write_text("Invented lifecycle sentinel. This file is not price data.\n")
        manifest = folder/"not-an-alpaca-manifest.json"
        manifest.write_text(json.dumps({"fixture": True, "source": "unit_test_fixture"}))
        state = self.state()
        state.update(dataset_manifest=str(manifest.relative_to(self.root)),
                     manifest_sha256=sha256_file(manifest), status="history_ready")
        study._save(self.root/"state.json", state)
        return data, manifest

    @contextmanager
    def evaluation_fixture(self):
        data, manifest = self.fixture_history()

        def fake_experiment(path, symbol, output, *, synthetic, config):
            self.assertEqual(path, data)
            self.assertEqual(symbol, "QQQ")
            self.assertIs(synthetic, False)
            self.assertIs(config["long_only"], True)
            self.assertEqual(config["min_train_samples"], 100)
            self.assertEqual(config["min_test_samples"], 30)
            self.assertEqual(config["dataset_manifest"], str(manifest))
            directory = output/"invented-evidence"
            directory.mkdir(parents=True, mode=0o700)
            (directory/"model.json").write_text('{"fixture": "not a trained model"}\n')
            report = {"directory": str(directory), "status": "completed_research",
                      "fixture": "Invented lifecycle evidence only", "source": "unit_test_fixture",
                      "feed": "unit_test_fixture", "sample_counts": {}, "partitions": {},
                      "evaluation": {}, "blocking_reasons": [],
                      "model_sha256": sha256_file(directory/"model.json")}
            study._save(directory/"report.json", report)
            return report

        def fake_charts(experiment_dir, output, **kwargs):
            output.mkdir(mode=0o700)
            path = output/"report.html"
            path.write_text("<p>Invented unit-test evidence, not market results.</p>")
            return {"html": str(path)}

        with ExitStack() as stack:
            stack.enter_context(patch.object(study, "_manifest", return_value=(data, manifest)))
            stack.enter_context(patch.object(study, "source_sha256", return_value="unit-test-source-digest"))
            runner = stack.enter_context(patch.object(study, "experiment", side_effect=fake_experiment))
            stack.enter_context(patch.object(study.JSONModel, "load", return_value=SimpleNamespace(artifact={"fixture": True})))
            auditor = stack.enter_context(patch("dwight.audit.audit_experiment", return_value={"status": "passed", "fixture": True}))
            stress = stack.enter_context(patch.object(study, "_stress", side_effect=lambda workspace, report, *_: stress_fixture(report["model_sha256"])))
            charts = stack.enter_context(patch("dwight.reporting.generate_report", side_effect=fake_charts))
            yield runner, auditor, stress, charts

    def test_prepare_preserves_real_gates_and_rejects_reuse_or_lookback_overlap(self):
        state = self.state()
        self.assertFalse(state["experiment_started"])
        self.assertFalse(state["forward_observation_started"])
        self.assertFalse(state["submits_orders"])
        with self.assertRaises(FileExistsError):
            study.prepare(self.root, self.recipe)
        for mutation in ({"end": "2026-09-17"},
                         {"experiment": {**self.recipe["experiment"], "long_only": False}},
                         {"experiment": {**self.recipe["experiment"], "min_train_samples": 99}},
                         {"minimum_filtered_test_trades": 29}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                study.prepare(self.root.parent/"invalid", {**self.recipe, **mutation})

    def test_protocol_tamper_blocks_collection_and_evaluation(self):
        altered = deepcopy(self.recipe)
        altered["experiment"]["thresholds"] = [.4, .5, .6]
        study._save(self.root/"protocol.json", altered)
        with patch.object(study, "download_alpaca_dataset") as download, patch.object(study, "experiment") as runner:
            with self.assertRaisesRegex(ValueError, "recipe changed"):
                study.collect(self.root, environ={})
            with self.assertRaisesRegex(ValueError, "recipe changed"):
                study.evaluate(self.root)
            download.assert_not_called()
            runner.assert_not_called()

    def test_missing_credentials_makes_no_network_request(self):
        with patch.object(study, "download_alpaca_dataset", side_effect=AssertionError("No request permitted")) as download:
            state = study.collect(self.root, environ={})
        self.assertEqual(state["status"], "awaiting_data_credentials")
        self.assertFalse(state["experiment_started"])
        download.assert_not_called()
        self.assertFalse((self.root/"datasets").exists())

    def test_cached_history_is_verified_without_credentials_or_redownload(self):
        data, manifest = self.fixture_history()
        with patch.object(study, "_manifest", return_value=(data, manifest)) as check, \
                patch.object(study, "download_alpaca_dataset") as download:
            result = study.collect(self.root, environ={})
        self.assertEqual(result["status"], "history_ready")
        check.assert_called_once()
        download.assert_not_called()
        with patch.object(study, "_manifest", side_effect=ValueError("changed fixture")), \
                patch.object(study, "download_alpaca_dataset") as download:
            with self.assertRaisesRegex(ValueError, "changed fixture"):
                study.collect(self.root, environ={})
            download.assert_not_called()

    def test_completed_evaluation_is_cached_and_checks_all_recorded_artifacts(self):
        with self.evaluation_fixture() as (runner, auditor, stress, charts):
            first = study.evaluate(self.root)
            self.assertEqual(first["status"], "ready_for_shadow_review")
            self.assertFalse(first["forward_observation_started"])
            self.assertFalse(first["submits_orders"])
            self.assertTrue(first["artifacts"])
            self.assertIn("summary.json", first["artifacts"])
            self.assertIn("charts/report.html", first["artifacts"])
            self.assertNotIn("state.json", first["artifacts"])
            before = (self.root/"state.json").read_bytes()
            self.assertEqual(study.evaluate(self.root), first)
            self.assertEqual((self.root/"state.json").read_bytes(), before)
            runner.assert_called_once()
            auditor.assert_called_once()
            stress.assert_called_once()
            charts.assert_called_once()
            (self.root/"charts/report.html").write_text("Altered fixture chart")
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                study.evaluate(self.root)
            runner.assert_called_once()

    def test_source_change_blocks_completed_cache_and_never_refits(self):
        with self.evaluation_fixture() as (runner, _, _, _):
            study.evaluate(self.root)
            with patch.object(study, "source_sha256", return_value="changed-unit-test-source"):
                with self.assertRaisesRegex(ValueError, "code changed"):
                    study.evaluate(self.root)
            runner.assert_called_once()

    def test_interrupted_evaluation_records_type_only_and_refuses_retry(self):
        data, manifest = self.fixture_history()
        private_sentinel = "unit-test-private-sentinel-must-not-persist"
        with patch.object(study, "_manifest", return_value=(data, manifest)), \
                patch.object(study, "source_sha256", return_value="unit-test-source-digest"), \
                patch.object(study, "experiment", side_effect=RuntimeError(private_sentinel)) as runner:
            with self.assertRaises(RuntimeError):
                study.evaluate(self.root)
            state = self.state()
            self.assertEqual(state["status"], "evaluation_requires_review")
            self.assertTrue(state["experiment_started"])
            self.assertNotIn("artifacts", state)
            self.assertNotIn(private_sentinel, json.dumps(state))
            with self.assertRaisesRegex(ValueError, "Interrupted evaluation"):
                study.evaluate(self.root)
            runner.assert_called_once()


class StudyStressReviewTests(unittest.TestCase):
    def test_cost_stress_reuses_the_same_frozen_model_and_threshold_without_refitting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            experiment_dir = root/"unit-test-experiment"
            experiment_dir.mkdir()
            (experiment_dir/"input.csv").write_text("Invented read_bars fixture only")
            opened = datetime(2025, 1, 2, 9, 30, tzinfo=ZoneInfo("America/New_York"))
            bars = [Bar(opened+timedelta(minutes=5*i), 100, 101, 99, 100, 1000) for i in range(78)]
            settings = Config()
            report = {"directory": str(experiment_dir), "partitions": {"test": ["2025-01-02"]},
                      "strategy": asdict(settings), "model_sha256": "unit-test-model-digest"}
            model = SimpleNamespace(artifact={"threshold": .65})
            calls = []

            def replay(observed, config, predictor, threshold, *, long_only):
                calls.append((observed, config, predictor, threshold, long_only))
                return SimpleNamespace(trades=[], marked_equity=[config.capital]*len(observed))

            with patch.object(study, "read_bars", return_value=iter(bars)), \
                    patch.object(study, "_replay", side_effect=replay), \
                    patch.object(study, "_metrics", return_value={"fixture": "No actual metrics"}), \
                    patch("dwight.experiments.fit_model", side_effect=AssertionError("Stress must never refit")) as fit:
                result = study._stress(root, report, model, 2)
            fit.assert_not_called()
            self.assertFalse(result["refitted"])
            self.assertEqual(result["model_sha256"], report["model_sha256"])
            self.assertEqual(len(calls), 6)
            filtered = [call for call in calls if call[2] is model]
            self.assertEqual(len(filtered), 2)
            self.assertEqual([call[3] for call in filtered], [.65, .65])
            self.assertTrue(all(call[4] is True and call[0] == bars for call in calls))
            self.assertEqual(calls[0][1].commission, settings.commission)
            self.assertEqual(calls[3][1].commission, 2*settings.commission)
            self.assertEqual(calls[3][1].slippage, 2*settings.slippage)
            self.assertEqual(asdict(settings), report["strategy"])

    def test_every_screening_gate_is_required_and_never_approves_paper(self):
        report = {"model_sha256": "unit-test-model-digest"}
        baseline = stress_fixture()
        result = study._review(report, baseline, {"status": "passed"}, RECIPE)
        self.assertEqual(result["status"], "ready_for_shadow_review")
        self.assertFalse(result["paper_approved"])
        self.assertFalse(result["automatically_frozen"])
        for case in ("standard", "double_cost"):
            for field, value in (("trades", 29), ("net_pnl", 4), ("max_bar_close_drawdown", .025)):
                stress = deepcopy(baseline)
                stress["cases"][case]["evaluation"]["filtered"][field] = value
                with self.subTest(case=case, field=field):
                    result = study._review(report, stress, {"status": "passed"}, RECIPE)
                    self.assertEqual(result["status"], "keep_research_only")
        result = study._review(report, baseline, {"status": "incomplete", "check_counts": {"passed": 100}}, RECIPE)
        self.assertEqual(result["status"], "keep_research_only")

    def test_missing_cost_case_cannot_pass_review(self):
        for cases in ({}, {"standard": stress_fixture()["cases"]["standard"]}):
            with self.subTest(cases=list(cases)):
                try:
                    result = study._review({"model_sha256": "fixture"}, {"cases": cases}, {"status": "passed"}, RECIPE)
                except ValueError:
                    continue
                self.assertNotEqual(result["status"], "ready_for_shadow_review")

    def test_changed_model_refit_or_missing_comparator_is_rejected(self):
        report = {"model_sha256": "unit-test-model-digest"}
        for failure in ("changed_model", "refitted", "missing_comparator"):
            stress = stress_fixture()
            if failure == "changed_model":
                stress["model_sha256"] = "different-unit-test-model"
            elif failure == "refitted":
                stress["refitted"] = True
            else:
                del stress["cases"]["double_cost"]["evaluation"]["simple_volume"]
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                study._review(report, stress, {"status": "passed"}, RECIPE)


class StudyManifestTests(unittest.TestCase):
    def test_mocked_transport_roundtrip_and_raw_normalized_manifest_tampering(self):
        # The real downloader/validator operates on invented provider responses;
        # this temporary fixture is never reused as genuine Alpaca evidence.
        opened = datetime(2025, 1, 2, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        session = Session("2025-01-02", opened, opened+timedelta(minutes=390))
        rows = [{"t": (opened+timedelta(minutes=i)).isoformat(), "o": 100, "h": 102,
                 "l": 99, "c": 101, "v": 1000} for i in range(390)]
        payload = json.dumps({"bars": {"QQQ": rows}, "next_page_token": None}).encode()
        for failure in ("raw", "normalized", "manifest", "counts", "policy", "missing_raw"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp, \
                    patch("dwight.data.exchange_sessions", return_value=[session]), \
                    patch("dwight.study.exchange_sessions", return_value=[session]), \
                    patch("dwight.data._request_page", return_value=payload) as request, \
                    patch("dwight.data._write_parquet", return_value=False):
                root = Path(temp).resolve()/"fixture-study"
                recipe = {**deepcopy(RECIPE), "start": session.date, "end": session.date}
                study.prepare(root, recipe)
                state = study.collect(root, environ={"APCA_API_KEY_ID": "invented-unit-test-key",
                                                      "APCA_API_SECRET_KEY": "invented-unit-test-secret"})
                self.assertEqual(state["status"], "history_ready")
                request.assert_called_once()
                self.assertEqual(study.collect(root, environ={}), state)
                request.assert_called_once()
                manifest_path = root/state["dataset_manifest"]
                manifest = json.loads(manifest_path.read_text())
                if failure == "raw":
                    (manifest_path.parent/manifest["raw_pages"][0]["path"]).write_text("Changed invented response")
                elif failure == "normalized":
                    (manifest_path.parent/manifest["bars"]["QQQ"]["5Min"]).write_text("Changed invented normalized bars")
                else:
                    if failure == "manifest":
                        manifest["fixture_tampered"] = True
                    elif failure == "counts":
                        manifest["counts"]["QQQ"]["5Min"] = 77
                    elif failure == "policy":
                        manifest["gap_policy"] = "fabricated_forward_fill"
                    else:
                        manifest["raw_pages"] = []
                    study._save(manifest_path, manifest)
                    if failure != "manifest":
                        # Bypass only the outer digest to exercise the deeper
                        # independently checked manifest invariants.
                        state["manifest_sha256"] = sha256_file(manifest_path)
                        study._save(root/"state.json", state)
                with self.assertRaises(ValueError):
                    study.collect(root, environ={})
                request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
