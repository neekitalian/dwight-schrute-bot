import copy
import csv
from datetime import datetime, timedelta
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dwight.experiments import (
    CandidateBot, FEATURE_NAMES, FEATURE_VERSION, JSONModel, complete_sessions,
    experiment, extract_features, fit_model, purge_labels, split_sessions,
)
from vwap_bot.engine import Bar, Bot, Config

START = datetime(2026, 9, 28, 9, 30, tzinfo=ZoneInfo("America/New_York"))


def bar(i, **overrides):
    values = dict(open=100, high=100.2, low=99.8, close=100, volume=1000)
    values.update(overrides)
    return Bar(START+timedelta(minutes=5*i), **values)


def sample_rows(count=60):
    rows = []
    for i in range(count):
        positive = int(i % 2 == 0)
        values = {name: (2*positive-1)+(i % 7)/10 for name in FEATURE_NAMES}
        values["direction"] = 1
        rows.append({"features": values, "label": positive})
    return rows


def constant_model():
    return JSONModel({"schema_version": 1, "kind": "logistic_regression",
                      "feature_names": list(FEATURE_NAMES), "feature_version": FEATURE_VERSION,
                      "mean": [0]*len(FEATURE_NAMES), "scale": [1]*len(FEATURE_NAMES),
                      "coefficients": [0]*len(FEATURE_NAMES), "intercept": 0,
                      "synthetic": True})


class ExperimentTests(unittest.TestCase):
    def candidate_bot(self):
        bot = CandidateBot(Config(ema_period=2, atr_period=2))
        bot.feed(bar(0))
        bot.feed(bar(1))
        bot.highs = [(0, 99), (1, 100)]
        bot.lows = [(0, 97), (1, 98)]
        bot.setup = dict(direction=1, index=1, extreme=101, level=100, touched=False)
        bot.feed(bar(2, open=100, high=101, low=99.9, close=100.9))
        return bot

    def test_features_frozen_before_future_outcome(self):
        first, second = self.candidate_bot(), self.candidate_bot()
        before = copy.deepcopy(first.candidates[0]["features"])
        self.assertNotIn("label", first.candidates[0])
        self.assertEqual(first.candidates[0]["available_at"], (START+timedelta(minutes=15)).isoformat())
        first.feed(bar(3, open=101, high=105, low=100.5, close=104))
        second.feed(bar(3, open=101, high=101.2, low=98, close=99))
        self.assertEqual(first.candidates[0]["features"], before)
        self.assertEqual(second.candidates[0]["features"], before)
        self.assertEqual(first.candidates[0]["label"], 1)
        self.assertEqual(second.candidates[0]["label"], 0)
        self.assertGreater(first.candidates[0]["net_pnl"], 0)

    def test_feature_reference_uses_prior_volume(self):
        bot = self.candidate_bot()
        features = extract_features(bot, bot.pending)
        self.assertEqual(features["relative_volume"], 1)
        self.assertEqual(features["direction"], 1)
        self.assertAlmostEqual(features["session_fraction"], 15/390)

    def test_partial_and_early_sessions_excluded(self):
        full = [bar(i) for i in range(78)]
        early = [Bar(b.timestamp+timedelta(days=1), b.open, b.high, b.low, b.close, b.volume) for b in full[:42]]
        sessions, excluded = complete_sessions(full+early)
        self.assertEqual(list(sessions), ["2026-09-28"])
        self.assertEqual(excluded[0]["bars"], 42)
        with self.assertRaises(ValueError):
            complete_sessions(full[:2]+full[3:])

    def test_splits_are_chronological_and_whole_session(self):
        split = split_sessions({f"2026-01-{i:02}": [] for i in range(1, 11)})
        self.assertEqual([len(split[k]) for k in ("train", "validation", "test")], [6, 2, 2])
        self.assertLess(max(split["train"]), min(split["validation"]))
        self.assertLess(max(split["validation"]), min(split["test"]))

    def test_boundary_overlapping_labels_are_purged(self):
        boundary = START+timedelta(days=1)
        rows = [
            {"label": 1, "available_at": START.isoformat(), "label_available_at": (boundary-timedelta(minutes=5)).isoformat()},
            {"label": 0, "available_at": START.isoformat(), "label_available_at": (boundary+timedelta(minutes=5)).isoformat()},
            {"available_at": START.isoformat()},
        ]
        kept, purged = purge_labels(rows, START, boundary)
        self.assertEqual(kept, rows[:1])
        self.assertEqual(purged, 1)

    def test_filtered_replay_can_take_new_candidates_after_skipped_loss(self):
        def signal(bot, b, prev):
            bot.pending = {"direction": 1, "stop": 99,
                           "signal_time": b.timestamp.isoformat(), "reason": "test"}

        def features(bot, pending):
            return {name: len(bot.bars) for name in FEATURE_NAMES}

        class SkipFirst:
            def predict_probability(self, values):
                return 0 if values[FEATURE_NAMES[0]] == 1 else 1

        baseline, filtered = CandidateBot(), CandidateBot(model=SkipFirst())
        with patch.object(Bot, "_signal", signal), patch("dwight.experiments.extract_features", features):
            for b in [bar(0), bar(1, low=98), bar(2, low=98), bar(3, high=104, close=103)]:
                baseline.feed(b)
                filtered.feed(b)
        self.assertEqual([t["exit_reason"] for t in baseline.trades], ["stop", "stop"])
        self.assertEqual([t["exit_reason"] for t in filtered.trades], ["stop", "target"])
        self.assertNotIn(filtered.trades[-1]["signal_time"], [t["signal_time"] for t in baseline.trades])

    def test_model_version_shape_and_finite_checks(self):
        good = constant_model()
        self.assertEqual(good.predict_probability({name: 1 for name in FEATURE_NAMES}), .5)
        for field, value in (("feature_version", "wrong"), ("scale", [0]*len(FEATURE_NAMES)), ("intercept", float("nan"))):
            bad = {**good.artifact, field: value}
            with self.assertRaises(ValueError):
                JSONModel(bad)
        with self.assertRaises(ValueError):
            good.predict_probability({"future_pnl": 1})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"model.json"
            path.write_text(json.dumps(good.artifact))
            with self.assertRaises(ValueError):
                JSONModel.load(path, "incorrect-checksum")
        overflowing = JSONModel({**good.artifact, "coefficients": [1e308]*len(FEATURE_NAMES)})
        with self.assertRaises(ValueError):
            overflowing.predict_probability({name: 1e308 for name in FEATURE_NAMES})

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "research extra not installed")
    def test_training_json_matches_sklearn_and_is_reproducible(self):
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        rows = sample_rows()
        model = fit_model(rows, synthetic=True)
        self.assertEqual(model.artifact, fit_model(rows, synthetic=True).artifact)
        values = [[row["features"][name] for name in FEATURE_NAMES] for row in rows]
        scale = StandardScaler().fit(values)
        fitted = LogisticRegression(C=1.0, solver="liblinear", random_state=0, max_iter=1000).fit(scale.transform(values), [row["label"] for row in rows])
        for row, expected in zip(rows, fitted.predict_proba(scale.transform(values))[:, 1]):
            self.assertAlmostEqual(model.predict_probability(row["features"]), expected, places=12)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"model.json"
            path.write_text(json.dumps(model.artifact))
            loaded = JSONModel.load(path)
            self.assertEqual(loaded.predict_probability(rows[0]["features"]), model.predict_probability(rows[0]["features"]))
        self.assertFalse(model.artifact["promotion_eligible"])
        self.assertTrue(model.artifact["synthetic"])

    def test_insufficient_data_does_not_create_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root/"bars.csv"
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                for i in range(78):
                    b = bar(i)
                    writer.writerow([b.timestamp.isoformat(), b.open, b.high, b.low, b.close, b.volume])
            report = experiment(path, "TEST", root/"runs", synthetic=True)
            self.assertEqual(report["status"], "insufficient_data")
            self.assertFalse(report["promotion_eligible"])
            self.assertFalse((Path(report["directory"])/"model.json").exists())
            with self.assertRaises(ValueError):
                experiment(path, "TEST", root/"real", config={"min_train_samples": 2})

    def test_manifest_provenance_and_csv_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root/"SPY-5Min.csv"
            data.write_text("timestamp,open,high,low,close,volume\n")
            manifest = {"schema_version": 1, "source": "alpaca", "start": "2026-01-01", "end": "2026-02-01",
                        "symbols": ["SPY"], "feed": "sip", "adjustment": "raw",
                        "bars": {"SPY": {"5Min": data.name}},
                        "files": [{"path": data.name, "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}]}
            fingerprint = {key: manifest[key] for key in ("schema_version", "start", "end", "symbols", "feed", "adjustment", "files")}
            manifest["dataset_sha256"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
            (root/"manifest.json").write_text(json.dumps(manifest))
            report = experiment(data, "SPY", root/"runs")
            self.assertEqual(report["source"], "alpaca")
            self.assertEqual(report["feed"], "sip")
            self.assertEqual(report["dataset_sha256"], manifest["dataset_sha256"])
            data.write_text(data.read_text()+"2026-09-28T09:30:00-04:00,100,101,99,100,1000\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                experiment(data, "SPY", root/"tampered")
            with self.assertRaises(ValueError):
                experiment(data, "QQQ", root/"wrong-symbol")


if __name__ == "__main__":
    unittest.main()
