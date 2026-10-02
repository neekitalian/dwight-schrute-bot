"""Audit tests alter saved evidence rather than asserting canned success text."""
import importlib.util
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dwight.audit import audit_experiment, _trade_checks
from dwight.experiments import experiment
from examples.make_experiment_demo import generate
from vwap_bot.engine import Bar, Config


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "research extra not installed")
class SavedExperimentAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        root = Path(cls.root.name)
        generate(root / "input.csv", days=500, seed=42)
        report = experiment(root / "input.csv", "QQQ", root / "runs", synthetic=True,
                            config={"min_train_samples": 10, "min_validation_samples": 4,
                                    "min_test_samples": 4, "min_class_samples": 1,
                                    "min_validation_trades": 1})
        cls.source = Path(report["directory"])
        assert report["status"] == "completed_synthetic_smoke"

    @classmethod
    def tearDownClass(cls):
        cls.root.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "experiment"
        shutil.copytree(self.source, self.path)

    def edit(self, filename, mutate):
        path = self.path / filename
        value = json.loads(path.read_text())
        mutate(value)
        path.write_text(json.dumps(value))

    def checks(self, result):
        return {check["id"]: check for check in result["checks"]}

    def test_frozen_experiment_replays_and_does_not_claim_runtime_skills(self):
        result = audit_experiment(self.path)
        self.assertEqual(result["status"], "passed", result["checks"])
        self.assertGreater(result["audited_trade_records"], 0)
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["runtime_skills"]["used"], [])
        self.assertEqual(result["paper_compatibility"]["status"], "not_verified")
        self.assertGreater(result["paper_compatibility"]["short_trades"], 0)
        self.assertEqual(self.checks(result)["entry_timing"]["status"], "passed")
        json.dumps(result, allow_nan=False)

    def test_modified_input_is_not_replayed_as_verified_evidence(self):
        with (self.path / "input.csv").open("a") as stream:
            stream.write("\n")
        result = audit_experiment(self.path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.checks(result)["input_integrity"]["status"], "failed")
        self.assertEqual(self.checks(result)["replay_evidence"]["status"], "unverified")

    def test_changed_quantity_and_profit_are_detected(self):
        def change(trades):
            trades[0]["quantity"] += 10
            trades[0]["net_pnl"] += 123
        self.edit("test-filtered-trades.json", change)
        result = audit_experiment(self.path)
        checks = self.checks(result)
        self.assertEqual(result["status"], "failed")
        for key in ("test_filtered_trades", "position_sizing", "cost_accounting"):
            self.assertEqual(checks[key]["status"], "failed")

    def test_same_candle_entry_and_future_label_are_detected(self):
        self.edit("test-filtered-trades.json", lambda rows: rows[0].update(signal_time=rows[0]["entry_time"]))
        self.edit("test-filtered-candidates.json", lambda rows: rows[0].update(available_at="2099-01-01T00:00:00+00:00"))
        checks = self.checks(audit_experiment(self.path))
        self.assertEqual(checks["entry_timing"]["status"], "failed")
        self.assertEqual(checks["test_filtered_candidates"]["status"], "failed")

    def test_revised_model_cannot_pass_filtered_replay(self):
        self.edit("model.json", lambda model: model.update(intercept=model["intercept"] + 1))
        checks = self.checks(audit_experiment(self.path))
        self.assertEqual(checks["model_integrity"]["status"], "failed")
        self.assertEqual(checks["test_filtered"]["status"], "unverified")

    def test_missing_evidence_is_unverified_instead_of_success(self):
        (self.path / "test-filtered-trades.json").unlink()
        result = audit_experiment(self.path)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.checks(result)["test_filtered_artifacts"]["status"], "unverified")

    def test_partition_tampering_prevents_replay(self):
        self.edit("splits.json", lambda splits: splits["test"].append(splits["train"][0]))
        checks = self.checks(audit_experiment(self.path))
        self.assertEqual(checks["chronological_partitions"]["status"], "failed")
        self.assertEqual(checks["replay_evidence"]["status"], "unverified")


class TradeInvariantAuditTests(unittest.TestCase):
    def fixtures(self):
        config = Config()
        start = datetime(2026, 9, 28, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        bars = [Bar(start + timedelta(minutes=5 * i), 100, 103, 98, 100, 1000) for i in range(7)]
        equity, trades = config.capital, []
        for i in (1, 3, 5):
            entry, stop = 100 + config.slippage, 99
            distance = entry - stop
            unit_risk = distance + config.slippage + 2 * config.commission
            quantity = math.floor(min(equity * config.risk_fraction / unit_risk, equity / entry))
            target = math.ceil((entry + config.reward_r * distance) / config.tick) * config.tick
            fill = stop - config.slippage
            gross, fees = (fill - entry) * quantity, 2 * config.commission * quantity
            pnl = gross - fees
            equity += pnl
            trades.append(dict(direction=1, quantity=quantity, entry=entry, stop=stop, target=target,
                               risk=quantity * unit_risk, entry_time=bars[i].timestamp.isoformat(),
                               signal_time=bars[i - 1].timestamp.isoformat(), exit_time=bars[i].timestamp.isoformat(),
                               exit=fill, exit_reason="stop", gross_pnl=gross, fees=fees, net_pnl=pnl,
                               net_r=pnl / (quantity * unit_risk), equity=equity))
        return bars, trades, config

    def test_third_losing_entry_is_flagged_with_other_rules_valid(self):
        bars, trades, config = self.fixtures()
        issues = _trade_checks(trades, bars, config)
        self.assertEqual(len(issues["daily_loss_limit"]), 1)
        for name, violations in issues.items():
            if name != "daily_loss_limit":
                self.assertEqual(violations, [], name)

    def test_candle_touching_both_levels_cannot_report_target_fill(self):
        bars, trades, config = self.fixtures()
        trades = trades[:1]
        trades[0].update(exit=trades[0]["target"], exit_reason="target")
        issues = _trade_checks(trades, bars, config)
        self.assertTrue(issues["stop_target_execution"])

    def test_missing_directory_is_incomplete(self):
        with tempfile.TemporaryDirectory() as root:
            result = audit_experiment(Path(root))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["checks"][0]["status"], "unverified")

    def test_no_trades_do_not_pass_execution_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "empty.csv"
            data.write_text("timestamp,open,high,low,close,volume\n")
            report = experiment(data, "QQQ", root / "runs", synthetic=True)
            result = audit_experiment(Path(report["directory"]))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["audited_trade_records"], 0)
        checks = {check["id"]: check for check in result["checks"]}
        self.assertEqual(checks["entry_timing"]["status"], "unverified")
        self.assertEqual(checks["stop_target_execution"]["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
