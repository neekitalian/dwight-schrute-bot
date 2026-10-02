"""Actual CLI -> normalization -> baseline observer -> journal integration.

Only the exchange calendar, Alpaca data read and wall clocks are substituted.
The invented fixture is a software check, never market or performance evidence.
No credentials, broker calls or order transports are involved.
"""
from contextlib import ExitStack, redirect_stdout
from datetime import timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from dwight.__main__ import main
from dwight import manual, manual_signals
from dwight.signals import SignalObserver
from tests.test_signals import OPEN, SESSION, strategy_bars


def minute_fixture(bars):
    """Five identical minute candles aggregate exactly to each fixture bar."""
    return [{"t": (bar.timestamp+timedelta(minutes=minute)).isoformat(),
             "o": bar.open, "h": bar.high, "l": bar.low, "c": bar.close,
             "v": bar.volume/5}
            for bar in bars for minute in range(5)]


class ManualSignalFlowTests(unittest.TestCase):
    def invoke(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["dwight", *map(str, args)]), redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_real_signal_reaches_one_private_proposal_and_unverified_no_fill_report(self):
        bars = strategy_bars()
        observer = SignalObserver()
        for bar in bars:
            observer.feed(bar)
        expected_signal = observer.signals[0]
        now = OPEN+timedelta(minutes=126)
        rows = minute_fixture(bars)
        # This next candle is still forming. Its radically different opening
        # price must never become the human proposal's entry reference.
        rows.append({"t": (OPEN+timedelta(minutes=125)).isoformat(),
                     "o": 900, "h": 905, "l": 895, "c": 901, "v": 200})
        original_signal_clock, original_journal_clock = manual_signals._clock, manual._clock
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary).resolve()
            signal_dir, journal_path = root/"signals", root/"paper"/"account.sqlite3"
            # Loading dotenv is outside this deterministic boundary test.
            stack.enter_context(patch("dwight.__main__.load_env"))
            calendar = stack.enter_context(patch("dwight.manual_signals.exchange_sessions",
                                                  return_value=[SESSION]))
            reader = stack.enter_context(patch("dwight.manual_signals.fetch_alpaca_bars",
                                                return_value={"QQQ": rows}))
            stack.enter_context(patch("dwight.manual_signals._clock", side_effect=lambda value=None:
                                      original_signal_clock(now if value is None else value)))
            stack.enter_context(patch("dwight.manual._clock", side_effect=lambda value=None:
                                      original_journal_clock(now if value is None else value)))

            observed = self.invoke("manual-observe", "--once", "--feed", "sip",
                                   "--signals", signal_dir)
            self.assertEqual(observed["status"], "ready")
            self.assertEqual(observed["signal_count"], 1)
            self.assertFalse(observed["submits_orders"])
            # Service output is a heartbeat; private signal terms have their
            # own explicit inspection command.
            self.assertNotIn("signals", observed)
            self.assertNotIn("stop", observed)
            self.assertNotIn("features", observed)
            calendar.assert_called_with(OPEN.date(), OPEN.date())
            reader.assert_called_with(SESSION.open, now, ("QQQ",), "sip")

            listed = self.invoke("manual-signals", "--feed", "sip", "--signals", signal_dir)
            self.assertEqual(len(listed["signals"]), 1)
            signal = listed["signals"][0]
            self.assertTrue(signal["accepted"])
            self.assertTrue(signal["eligible"])
            self.assertEqual(signal["stop"], expected_signal["stop"])
            self.assertEqual(signal["reason"], expected_signal["reason"])
            self.assertEqual(signal["features"], expected_signal["features"])
            self.assertFalse(listed["account_verified"])
            self.assertFalse(listed["portfolio_gates_applied"])
            self.assertFalse(signal["account_risk_verified"])
            self.assertFalse(signal["submits_orders"])

            repeated = self.invoke("manual-observe", "--once", "--feed", "sip",
                                   "--signals", signal_dir)
            self.assertEqual(repeated["status"], "waiting_for_bar")
            self.assertEqual(repeated["signal_count"], 1)
            self.assertEqual(reader.call_count, 2)

            prepare_args = ("manual-prepare", signal["signal_id"], "--signals", signal_dir,
                            "--feed", "sip", "--entry", "104.30", "--quantity", "2",
                            "--price-observed-at", now.isoformat(), "--state", journal_path)
            prepared = self.invoke(*prepare_args)
            self.assertEqual(prepared["status"], "delivered")
            self.assertFalse(prepared["idempotent_retry"])
            self.assertTrue(prepared["actionable"])
            self.assertFalse(prepared["account_risk_verified"])
            self.assertFalse(prepared["portfolio_risk_enforced"])
            self.assertFalse(prepared["submits_orders"])
            proposal = prepared["proposal"]
            self.assertEqual(proposal["entry"], "104.3")
            self.assertEqual(proposal["quantity"], "2")
            self.assertEqual(proposal["stop"], "102.78")
            self.assertEqual(Decimal(proposal["stop"]), Decimal(str(expected_signal["stop"])))
            self.assertEqual(proposal["target"], "107.34")
            self.assertNotEqual(Decimal(proposal["entry"]), Decimal(str(rows[-1]["o"])))
            self.assertEqual(proposal["planned_stop_risk_before_costs"], "3.04")
            self.assertEqual(proposal["planned_reward_before_costs"], "6.08")
            self.assertEqual(proposal["status"], "pending")
            self.assertEqual(proposal["model"], "none-baseline")
            self.assertEqual(proposal["expires_at"], signal["expires_at"])
            self.assertEqual(prepared["reference"]["price_basis"], "human_supplied_reference")
            self.assertFalse(prepared["reference"]["quote_verified"])
            self.assertEqual(prepared["reference"]["original_signal_stop"], expected_signal["stop"])

            retry = self.invoke(*prepare_args)
            self.assertTrue(retry["idempotent_retry"])
            self.assertEqual(retry["proposal"], proposal)
            pending = self.invoke("manual-list", "--state", journal_path)
            self.assertEqual(pending["proposals"], [proposal])
            # A human confirmation remains a journal decision and creates no
            # fill, position evidence or verified account balance.
            confirmed = self.invoke("manual-status", proposal["proposal_id"], "confirmed",
                                    "--state", journal_path)
            self.assertEqual(confirmed["status"], "confirmed")
            self.assertFalse(confirmed["submits_orders"])
            report = self.invoke("manual-report", "--state", journal_path)
            self.assertEqual(report["data_kind"], "no_fills")
            self.assertEqual(report["fills"], [])
            self.assertEqual(report["fill_audit"], [])
            self.assertEqual(report["performance_scope"], "imported_fills_only")
            self.assertEqual(report["reconciliation_status"], "unverified_no_account_snapshot")
            self.assertFalse(report["broker_verified"])
            self.assertFalse(report["submits_orders"])
            self.assertIsNone(report["account_equity"])
            self.assertIsNone(report["account_return_pct"])
            self.assertEqual([event["status"] for event in report["proposal_events"]],
                             ["pending", "confirmed"])

            # Also exercise the private renderer with the real no-fill report.
            rendered = self.invoke("manual-report", "--state", journal_path,
                                   "--html-output", root/"report")
            frozen = json.loads(Path(rendered["json"]).read_text())
            self.assertEqual(frozen, report)
            self.assertTrue(Path(rendered["html"]).is_file())
            self.assertEqual(stat.S_IMODE(Path(rendered["html"]).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(rendered["json"]).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(journal_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(signal_dir.stat().st_mode), 0o700)

            with sqlite3.connect(journal_path) as journal:
                self.assertEqual(journal.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 1)
                self.assertEqual(journal.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
            with sqlite3.connect(signal_dir/"manual-signals.sqlite3") as store:
                self.assertEqual(store.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
                self.assertEqual(store.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
                self.assertIsNotNone(store.execute("SELECT delivered_at FROM outbox").fetchone()[0])
                self.assertEqual(dict(store.execute("SELECT kind,COUNT(*) FROM bars GROUP BY kind")),
                                 {"1m": 125, "5m": 25})
            # Every command after observation is local; neither prepare nor
            # reporting refreshes a quote or calls any account/order API.
            self.assertEqual(reader.call_count, 2)


if __name__ == "__main__":
    unittest.main()
