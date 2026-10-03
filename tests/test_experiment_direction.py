"""Direction stays identical through research, frozen identity and shadow."""
from dataclasses import asdict
from datetime import timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight import experiments
from dwight.experiments import CandidateBot, FEATURE_NAMES, FEATURE_VERSION, JSONModel
from dwight.ops import release, sha256, verify_release
from dwight.shadow import ShadowMonitor
from examples.make_experiment_demo import generate
from tests.test_shadow import OPEN, fixture
from vwap_bot.engine import Bar, Bot, Config


def constant_artifact():
    return {"schema_version": 1, "kind": "logistic_regression", "feature_version": FEATURE_VERSION,
            "feature_names": list(FEATURE_NAMES), "mean": [0.] * len(FEATURE_NAMES),
            "scale": [1.] * len(FEATURE_NAMES), "coefficients": [0.] * len(FEATURE_NAMES),
            "intercept": 0., "synthetic": True, "threshold": .5}


def force_short(bot, bar, previous):
    bot.pending = {"direction": -1, "stop": 102, "signal_time": bar.timestamp.isoformat(),
                   "reason": "direction-test"}


def forced_features(bot, pending):
    return {name: pending["direction"] if name == "direction" else 1 for name in FEATURE_NAMES}


class CandidateDirectionTests(unittest.TestCase):
    def test_long_only_blocks_before_inference_and_simulated_exposure(self):
        class MustNotRun:
            def predict_probability(self, features):
                raise AssertionError("short candidate reached model inference")

        blocked = CandidateBot(model=MustNotRun(), long_only=True)
        legacy = CandidateBot()
        with patch.object(Bot, "_signal", force_short), patch.object(experiments, "extract_features", forced_features):
            for index in range(3):
                bar = Bar(OPEN+timedelta(minutes=5*index), 100, 100.2, 99.8, 100, 1000)
                blocked.feed(bar)
                legacy.feed(bar)
        self.assertTrue(blocked.candidates)
        self.assertTrue(all(not row["taken"] and row["rejection_reason"] == "long_only_policy"
                            and "probability" not in row for row in blocked.candidates))
        self.assertIsNone(blocked.pending)
        self.assertIsNone(blocked.position)
        self.assertFalse(blocked.trades)
        self.assertEqual(legacy.position["direction"], -1)

    def test_malformed_direction_settings_are_rejected(self):
        for value in (None, True, 1, "short_only", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "direction policy"):
                JSONModel({**constant_artifact(), "direction_policy": value})
        for value in (None, 1, "yes"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                CandidateBot(long_only=value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                experiments.experiment(Path("must-not-be-read.csv"), "QQQ", Path("must-not-be-created"),
                                       config={"long_only": value})
        with self.assertRaisesRegex(ValueError, "synthetic"):
            experiments.experiment(Path("must-not-be-read.csv"), "QQQ", Path("must-not-be-created"),
                                   config={"long_only": True, "min_train_samples": 1})

    def test_shadow_applies_the_identical_gate_and_preserves_legacy_behavior(self):
        for direction in (None, "long_only", "long_and_short"):
            with self.subTest(direction=direction), fixture() as (release_dir, state, reader, calendar):
                path = release_dir/"model.json"
                artifact = json.loads(path.read_text())
                if direction is not None:
                    artifact["direction_policy"] = direction
                path.write_text(json.dumps(artifact))
                calls = []

                def probability(model, features):
                    calls.append(features["direction"])
                    return .75

                with patch("dwight.shadow.CandidateBot", CandidateBot), \
                        patch.object(Bot, "_signal", force_short), \
                        patch.object(experiments, "extract_features", forced_features), \
                        patch.object(JSONModel, "predict_probability", probability), \
                        ShadowMonitor(release_dir, state) as monitor:
                    result = monitor.step(OPEN+timedelta(minutes=11))
                decisions = result["decisions"]
                self.assertTrue(decisions)
                if direction == "long_only":
                    self.assertFalse(calls)
                    self.assertTrue(all(not row["taken"] and not row["shadow_take"]
                                        and row["rejection_reason"] == "long_only_policy" for row in decisions))
                else:
                    self.assertTrue(calls)
                    self.assertTrue(any(row["taken"] for row in decisions))
                self.assertFalse(result["submits_orders"])


class ReleaseDirectionTests(unittest.TestCase):
    def candidate(self, root, direction=None):
        directory = root/"candidate"
        directory.mkdir()
        source = Path(experiments.__file__).parent.parent
        code_hash = hashlib.sha256((source/"dwight/experiments.py").read_bytes()+
                                   (source/"vwap_bot/engine.py").read_bytes()).hexdigest()
        artifact = {**constant_artifact(), "symbol": "QQQ", "strategy": asdict(Config()),
                    "source": "synthetic", "feed": "synthetic", "input_sha256": "fixture",
                    "code_sha256": code_hash}
        if direction is not None:
            artifact["direction_policy"] = direction
        (directory/"model.json").write_text(json.dumps(artifact))
        report = {**artifact, "status": "completed_synthetic_smoke", "selected_threshold": .5,
                  "model_sha256": sha256(directory/"model.json")}
        if direction is not None:
            report["experiment_settings"] = {"long_only": direction == "long_only"}
        (directory/"report.json").write_text(json.dumps(report))
        policy = root/"policy.json"
        policy.write_text(json.dumps({"mode": "shadow", "feed": "synthetic", "allowed_symbols": ["QQQ"],
                                      "bar_settle_seconds": 60, "max_bar_delay_seconds": 120, "poll_seconds": 30}))
        return directory, policy

    def test_new_and_legacy_direction_identity_are_frozen(self):
        for direction in (None, "long_only", "long_and_short"):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                candidate, policy = self.candidate(root, direction)
                result = release(candidate, root/"release", feed="synthetic", symbol="QQQ", policy_path=policy)
                self.assertEqual(result["direction_policy"], direction or "long_and_short")
                self.assertFalse(verify_release(root/"release")["paper_approved"])
                manifest_path = root/"release/release.json"
                manifest = json.loads(manifest_path.read_text())
                del manifest["direction_policy"]
                manifest_path.write_text(json.dumps(manifest))
                if direction == "long_only":
                    with self.assertRaisesRegex(ValueError, "release direction"):
                        verify_release(root/"release")
                else:
                    self.assertFalse(verify_release(root/"release")["paper_approved"])

    def test_long_only_cannot_be_relabelled_by_report_settings_or_policy(self):
        for change in ("missing_report_direction", "opposite_report_direction", "bad_report_direction",
                       "opposite_setting", "opposite_policy"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                candidate, policy = self.candidate(root, "long_only")
                report_path = candidate/"report.json"
                report = json.loads(report_path.read_text())
                if change == "missing_report_direction":
                    del report["direction_policy"]
                elif change == "opposite_report_direction":
                    report["direction_policy"] = "long_and_short"
                elif change == "bad_report_direction":
                    report["direction_policy"] = None
                elif change == "opposite_setting":
                    report["experiment_settings"]["long_only"] = False
                else:
                    values = json.loads(policy.read_text())
                    values["direction_policy"] = "long_and_short"
                    policy.write_text(json.dumps(values))
                report_path.write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError, "direction"):
                    release(candidate, root/"release", feed="synthetic", symbol="QQQ", policy_path=policy)
                self.assertFalse((root/"release").exists())


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "research dependencies are optional")
class ExperimentDirectionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.data = cls.root/"synthetic.csv"
        generate(cls.data, days=500, seed=42)
        cls.options = {"min_train_samples": 4, "min_validation_samples": 2, "min_test_samples": 2,
                       "min_class_samples": 1, "min_validation_trades": 1}

    def test_all_experiment_paths_keep_long_only_and_record_identity(self):
        flags, fitted_rows = [], []
        original_replay, original_fit = experiments._replay, experiments.fit_model

        def replay(*args, **kwargs):
            flags.append(kwargs.get("long_only", False))
            return original_replay(*args, **kwargs)

        def fit(rows, synthetic=False):
            fitted_rows.extend(rows)
            return original_fit(rows, synthetic)

        with patch.object(experiments, "_replay", replay), patch.object(experiments, "fit_model", fit):
            report = experiments.experiment(self.data, "QQQ", self.root/"long", synthetic=True,
                                            config={**self.options, "long_only": True})
        self.assertEqual(report["status"], "completed_synthetic_smoke")
        self.assertTrue(flags and all(flags))
        self.assertTrue(fitted_rows and all(row["features"]["direction"] == 1 for row in fitted_rows))
        self.assertEqual(report["direction_policy"], "long_only")
        self.assertFalse(report["promotion_eligible"])
        directory = Path(report["directory"])
        artifact = JSONModel.load(directory/"model.json").artifact
        self.assertEqual(artifact["direction_policy"], "long_only")
        self.assertFalse(artifact["promotion_eligible"])
        for path in directory.glob("*-trades.json"):
            with self.subTest(path=path.name):
                self.assertTrue(all(trade["direction"] == 1 for trade in json.loads(path.read_text())))
        candidates = json.loads((directory/"candidates.json").read_text())
        rejected = [row for rows in candidates.values() for row in rows if row["features"]["direction"] == -1]
        self.assertTrue(rejected)
        self.assertTrue(all(not row["taken"] and "label" not in row for row in rejected))

    def test_omitted_flag_matches_explicit_two_direction_replay(self):
        legacy = experiments.experiment(self.data, "QQQ", self.root/"legacy", synthetic=True, config=self.options)
        explicit = experiments.experiment(self.data, "QQQ", self.root/"both", synthetic=True,
                                         config={**self.options, "long_only": False})
        self.assertEqual(legacy["direction_policy"], "long_and_short")
        self.assertEqual(legacy["selected_threshold"], explicit["selected_threshold"])
        for partition in ("train", "validation", "test"):
            for variant in ("baseline", "simple_volume", "filtered"):
                self.assertEqual(legacy["evaluation"][partition][variant], explicit["evaluation"][partition][variant])


if __name__ == "__main__":
    unittest.main()
