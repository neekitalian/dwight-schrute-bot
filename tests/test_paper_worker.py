"""Private worker tests use invented account state; never broker requests."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dwight.authorization import AuthorizationLedger
from dwight.paper import Quote, RiskPolicy, PaperError
from dwight.paper_worker import (ControlLedger, PaperWorker, STRATEGY_ID,
                                 ReportingThread, init_paper_workspace, proposal_for)
from dwight.signals import strategy_identity
from vwap_bot.engine import Config

NOW = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
REVISION = strategy_identity(Config())


def scope():
    return {"account_id": "fixture-account", "allocation_usd": 2000,
            "max_order_notional_usd": 1000, "daily_loss_limit_usd": 50,
            "max_stop_risk_usd": 10, "expires_at": (NOW + timedelta(hours=1)).isoformat(), "consent": True}


def candidate():
    return {"signal_id": "a" * 64, "symbol": "QQQ", "feed": "sip", "strategy_identity": REVISION,
            "direction": 1, "eligible": True, "initially_eligible": True,
            "available_at": (NOW - timedelta(seconds=60)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=60)).isoformat(), "stop": 499.001}


class Executor:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.authorizations = AuthorizationLedger(self.db)
        self.policy = RiskPolicy()
        self.client = self
        self.supervisions = 0
        self.submissions = []
        self.fail = False

    def get_asset(self):
        return {"symbol": "QQQ", "class": "us_equity", "tradable": True, "status": "active"}

    def reconcile(self):
        if self.fail:
            raise PaperError("fixture failure")
        return {"account": {"id": "fixture-account", "status": "ACTIVE", "trading_blocked": False,
                            "account_blocked": False, "equity": "10000", "cash": "10000"},
                "positions": [], "open_orders": []}

    def supervise(self, now=None):
        self.supervisions += 1
        return {"entries_allowed": True, "pending_jobs": [], "reasons": []}

    def submit(self, proposal, quote, **kwargs):
        self.submissions.append((proposal, kwargs))
        return {"id": "fixture-order"}


class Market:
    fixture_clock = True
    def latest_quote(self, feed): return Quote("QQQ", 500, 500.01, NOW, feed)


class PaperWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        init_paper_workspace(self.root)
        self.config = json.loads((self.root / "config.json").read_text())
        self.controls = ControlLedger(self.root / "controls.sqlite3")
        self.addCleanup(self.controls.db.close)
        self.executor = Executor()
        self.addCleanup(self.executor.db.close)
        self.worker = PaperWorker(self.root, self.executor, Market(), self.controls, self.config)
        self.worker.observation = {"received_at": NOW.isoformat(), "status": {"status": "ready"}, "signals": [candidate()]}

    def authorize(self):
        self.controls.enqueue("authorize", scope(), NOW)
        self.worker.tick(NOW)
        return self.controls.get("authorization_id")

    def test_initial_workspace_and_read_only_preflight_never_execute(self):
        self.controls.enqueue("authorize", scope(), NOW)
        status = self.worker.tick(NOW, read_only=True)
        self.assertEqual(status["state"], "preflight")
        self.assertEqual(self.executor.supervisions, 0)
        self.assertEqual(self.executor.submissions, [])
        self.assertIsNone(self.controls.get("authorization_id"))
        self.assertEqual(self.controls.db.execute("SELECT state FROM commands").fetchone()[0], "queued")

    def test_authorization_does_not_start_and_start_is_scoped(self):
        aid = self.authorize()
        self.assertFalse(self.controls.get("enabled"))
        self.assertEqual(self.executor.submissions, [])
        self.controls.enqueue("start", {"authorization_id": aid}, NOW)
        self.worker.tick(NOW)
        self.assertEqual(len(self.executor.submissions), 1)
        proposal, kwargs = self.executor.submissions[0]
        self.assertEqual(proposal.strategy_revision, REVISION)
        self.assertEqual(kwargs["signal_expires_at"], candidate()["expires_at"])
        self.worker.tick(NOW)
        self.assertEqual(len(self.executor.submissions), 1)

    def test_pause_preserves_supervision_and_can_resume(self):
        aid = self.authorize()
        self.controls.enqueue("start", {"authorization_id": aid}, NOW)
        self.controls.enqueue("pause", {}, NOW + timedelta(seconds=1))
        self.worker.tick(NOW + timedelta(seconds=1))
        self.assertFalse(self.controls.get("enabled"))
        before = self.executor.supervisions
        self.worker.tick(NOW + timedelta(seconds=2))
        self.assertGreater(self.executor.supervisions, before)
        self.assertEqual(self.executor.submissions, [])
        self.controls.enqueue("start", {"authorization_id": aid}, NOW + timedelta(seconds=3))
        self.worker.tick(NOW + timedelta(seconds=3))
        self.assertEqual(len(self.executor.submissions), 1)

    def test_data_failure_and_expiry_still_supervise_existing_orders(self):
        aid = self.authorize()
        self.controls.enqueue("start", {"authorization_id": aid}, NOW)
        self.worker.observation = None
        status = self.worker.tick(NOW)
        self.assertEqual(status["state"], "blocked")
        self.assertEqual(self.executor.submissions, [])
        before = self.executor.supervisions
        status = self.worker.tick(NOW + timedelta(hours=2))
        self.assertGreater(self.executor.supervisions, before)
        self.assertFalse(self.controls.get("enabled"))

    def test_bad_account_feed_or_start_reference_cannot_activate(self):
        wrong = scope()
        wrong["account_id"] = "another-account"
        self.controls.enqueue("authorize", wrong, NOW)
        self.worker.tick(NOW)
        self.assertIsNone(self.controls.get("authorization_id"))
        aid = self.authorize()
        self.controls.enqueue("start", {"authorization_id": "wrong"}, NOW)
        self.worker.tick(NOW)
        self.assertFalse(self.controls.get("enabled"))
        self.assertEqual(self.executor.submissions, [])

    def test_interrupted_commands_are_failed_without_reactivation(self):
        receipt = self.controls.enqueue("authorize", scope(), NOW)
        with self.controls.db:
            self.controls.db.execute("UPDATE commands SET state='processing' WHERE id=?", (receipt["command_id"],))
        self.worker.tick(NOW)
        self.assertEqual(self.controls.db.execute("SELECT state FROM commands").fetchone()[0], "failed")
        self.assertIsNone(self.controls.get("authorization_id"))

    def test_scope_requires_explicit_limits_consent_and_bounded_expiry(self):
        updates = [{"consent": False}, {"max_stop_risk_usd": True}, {"allocation_usd": float("nan")},
                   {"max_order_notional_usd": 3000}, {"expires_at": (NOW + timedelta(days=8)).isoformat()}]
        for change in updates:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.controls.enqueue("authorize", {**scope(), **change}, NOW)
        with self.assertRaises(ValueError):
            self.controls.enqueue("authorize", {**scope(), "broker_key": "fixture"}, NOW)
        with self.assertRaises(ValueError):
            self.controls.enqueue("close-unrelated", {}, NOW)

    def test_workspace_cannot_silently_change_feed_or_strategy(self):
        with self.assertRaises(ValueError): init_paper_workspace(self.root, "iex")
        self.assertEqual(self.config["strategy_id"], STRATEGY_ID)
        self.assertEqual((self.root / "config.json").stat().st_mode & 0o777, 0o600)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): init_paper_workspace(alias)

    def test_sizing_is_cash_and_stop_risk_limited_with_penny_prices(self):
        snapshot = self.executor.reconcile()
        snapshot["account"]["cash"] = "501"
        proposal = proposal_for(candidate(), Market().latest_quote("sip"), snapshot, RiskPolicy(), 2000, REVISION, NOW)
        self.assertEqual(proposal.qty, 1)
        self.assertEqual(proposal.stop_price, 499.00)
        self.assertEqual(proposal.take_profit_price, 502.03)
        snapshot["account"]["cash"] = "499"
        with self.assertRaises(ValueError):
            proposal_for(candidate(), Market().latest_quote("sip"), snapshot, RiskPolicy(), 2000, REVISION, NOW)

    def test_expired_revised_short_or_backfilled_candidate_cannot_trade(self):
        for change in ({"expires_at": NOW.isoformat()}, {"strategy_identity": "b" * 64},
                       {"initially_eligible": False}, {"direction": -1}, {"feed": "iex"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                proposal_for({**candidate(), **change}, Market().latest_quote("sip"), self.executor.reconcile(), RiskPolicy(), 2000, REVISION, NOW)

    def test_performance_is_optional_and_never_controls_supervision(self):
        self.worker.performance = {"status": "unavailable", "gross_realized_pnl_usd": None}
        before = self.executor.supervisions
        status = self.worker.tick(NOW)
        self.assertGreater(self.executor.supervisions, before)
        self.assertEqual(status["performance"], self.worker.performance)
        self.assertIsNone(status["pnl_usd"])

    def test_reporter_has_separate_read_client_and_publishes_account_bound_summary(self):
        readonly_client = object()
        reporter = ReportingThread(self.root, readonly_client)
        report = {"account_id": "fixture-account", "fixture": True,
                  "console_summary": {"status": "ready", "as_of": NOW.isoformat(),
                                      "gross_realized_pnl_usd": "4.25", "owned_fill_count": 3}}
        with patch("dwight.paper_reporting.collect_paper_report", return_value=report) as collect:
            result = reporter.collect_once()
        self.assertIs(collect.call_args.args[0], readonly_client)
        self.assertEqual(collect.call_args.args[1:], (self.root / "execution.sqlite3", self.root / "reporting.sqlite3"))
        self.assertEqual(collect.call_args.kwargs, {"max_pages": 20})
        self.assertEqual(result["account_id"], "fixture-account")
        self.assertTrue(result["fixture"])
        self.assertEqual(reporter.latest.get_nowait(), result)
        self.assertEqual(json.loads((self.root / "report-status.json").read_text()), result)
        self.assertEqual((self.root / "report-status.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.executor.submissions, [])

    def test_report_failure_replaces_prior_success_and_never_leaks_provider_error(self):
        reporter = ReportingThread(self.root, object())
        reporter.latest.put_nowait({"status": "ready", "gross_realized_pnl_usd": "99"})
        with patch("dwight.paper_reporting.collect_paper_report", side_effect=PaperError("private-key-and-payload")):
            summary = reporter.collect_once()
        self.assertEqual(summary["status"], "unavailable")
        self.assertIsNone(summary["gross_realized_pnl_usd"])
        self.assertIsNone(summary["account_id"])
        self.assertNotIn("private-key", json.dumps(summary))
        self.assertEqual(reporter.latest.get_nowait(), summary)
        self.assertEqual(self.executor.submissions, [])


if __name__ == "__main__": unittest.main()
