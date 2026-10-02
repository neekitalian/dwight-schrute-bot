"""Visual evidence tests use journal output, never claimed account results."""
import copy
import csv
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest

from dwight.manual import ACCOUNT, ManualPaperJournal
from dwight.manual_reporting import render_manual_report


NOW = datetime(2020, 1, 2, 15, tzinfo=timezone.utc)


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)


class ManualReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS /var and /tmp aliases are intentionally excluded by path policy.
        self.root = Path(self.temp.name).resolve()
        self.journal = ManualPaperJournal(self.root / "account.sqlite3")

    def fill(self, identifier, side, quantity, price, fee="0", minute=1, **changes):
        return {"fill_id": identifier, "filled_at": (NOW + timedelta(minutes=minute)).isoformat(),
                "sequence": "0", "symbol": "QQQ", "side": side,
                "quantity": str(quantity), "price": str(price), "fee": str(fee),
                "data_kind": "synthetic", "proposal_id": "", **changes}

    def import_rows(self, rows):
        path = self.root / "fills.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.journal.import_fills(path)

    def report(self):
        return self.journal.report(now=NOW + timedelta(days=3))

    def render(self, report=None, name="rendered"):
        result = render_manual_report(report if report is not None else self.report(), self.root / name)
        return result, Path(result["html"]).read_text()

    def test_partial_exits_keep_net_realized_pnl_fees_and_open_inventory_distinct(self):
        self.import_rows([self.fill("buy", "buy", 10, 100, 1),
                          self.fill("first-exit", "sell", 3, 110, ".3", minute=2),
                          self.fill("second-exit", "sell", 2, 90, ".2", minute=3)])
        report = self.report()
        self.assertEqual(report["net_realized_pnl"], "9")
        self.assertEqual(report["fees_imported"], "1.5")
        self.assertEqual(report["open_position"]["quantity"], "5")
        self.assertEqual(report["open_position"]["cost_including_remaining_entry_fees"], "500.5")
        result, html = self.render(report)
        self.assertIn("SYNTHETIC ACCOUNTING EXAMPLE", html)
        for display in ("9 USD", "1.5 USD", "5 shares", "500.5 USD", "29.4 USD", "Sell executions: 2"):
            self.assertIn(display, html)
        self.assertIn("Account equity: unknown", html)
        self.assertIn("Account return: unknown", html)
        self.assertIn("Account drawdown: unknown", html)
        self.assertIn("No win rate is calculated", html)
        self.assertIn("no_proposal_link", html)
        self.assertIn("Remaining FIFO inventory", html)
        self.assertFalse(result["broker_verified"])
        self.assertFalse(result["submits_orders"])
        self.assertFalse(result["email_sent"])

    def test_exact_json_hash_privacy_and_no_network_resources(self):
        self.import_rows([self.fill("buy", "buy", 1, 100)])
        report = self.report()
        original = copy.deepcopy(report)
        result, html = self.render(report)
        frozen = Path(result["json"]).read_bytes()
        digest = hashlib.sha256(frozen).hexdigest()
        self.assertEqual(result["report_sha256"], digest)
        self.assertIn(digest, html)
        self.assertEqual(json.loads(frozen), report)
        self.assertEqual(report, original)
        self.assertEqual(Path(result["html"]).parent.stat().st_mode & 0o777, 0o700)
        for key in ("html", "json"):
            self.assertEqual(Path(result[key]).stat().st_mode & 0o777, 0o600)
        parser = Tags()
        parser.feed(html)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("iframe", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertNotIn("link", parser.tags)
        self.assertEqual([value for attr, value in parser.attributes if attr in {"src", "href"}], ["report.json"])
        self.assertIn("default-src 'none'", html)

    def test_empty_journal_is_not_zero_account_performance(self):
        result, html = self.render()
        self.assertEqual(result["data_kind"], "no_fills")
        self.assertIn("NO IMPORTED FILLS", html)
        self.assertIn("Not measured", html)
        self.assertIn("There is no realized PnL series to plot", html)
        self.assertIn("Account results and experiment activity are unknown", html)
        self.assertNotIn("<svg", html)
        self.assertNotIn("0 USD", html)

    def test_paper_export_label_is_not_independent_verification(self):
        self.import_rows([self.fill("supplied", "buy", 1, 100, data_kind="paper_export")])
        result, html = self.render()
        self.assertEqual(result["data_kind"], "paper_export")
        self.assertIn("USER-SUPPLIED PAPER EVIDENCE", html)
        self.assertIn("not been independently verified", html)
        self.assertIn("authenticity of an account export", html)
        self.assertIn("No sell executions", html)
        self.assertIn("0 USD", html)  # No realized exits, not an account return.

    def test_single_exit_zero_or_negative_has_a_valid_point_and_correct_value(self):
        for price, pnl in ((100, "0"), (95, "-5")):
            with self.subTest(price=price):
                self.journal = ManualPaperJournal(self.root / f"account-{price}.sqlite3")
                self.import_rows([self.fill("buy", "buy", 1, 100),
                                  self.fill("sell", "sell", 1, price, minute=2)])
                _, html = self.render(name=f"render-{price}")
                self.assertEqual(html.count('<circle class="pnl-point"'), 1)
                self.assertNotIn("polyline", html)
                self.assertIn(f"{pnl} USD", html)
                self.assertNotIn('cy="nan', html)
                self.assertNotIn('cy="inf', html)

    def test_proposal_text_is_escaped_and_linked_flags_do_not_claim_compliance(self):
        malicious = '</td><script>alert("x")</script><img src=x onerror=alert(1)>'
        proposal = {"proposal_id": "example", "account": ACCOUNT, "symbol": "QQQ", "side": "buy",
                    "quantity": "1", "entry": "100", "stop": "99", "target": "102",
                    "source": malicious, "model": malicious, "version": malicious,
                    "signal_at": NOW.isoformat(), "available_at": NOW.isoformat(),
                    "expires_at": (NOW + timedelta(minutes=5)).isoformat()}
        self.journal.add_proposal(proposal, now=NOW)
        self.journal.set_status("example", "confirmed", now=NOW + timedelta(minutes=2))
        self.import_rows([self.fill("over", "buy", 2, "100.5", proposal_id="example")])
        _, html = self.render()
        self.assertIn("entry_quantity_exceeds_proposal", html)
        self.assertIn("after_fill", html)
        self.assertIn("0.5", html)
        self.assertIn("does not establish strategy or risk-rule compliance", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn(malicious, html)
        parser = Tags()
        parser.feed(html)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in parser.attributes))

    def test_tampered_accounting_or_evidence_claims_fail_before_output_exists(self):
        self.import_rows([self.fill("buy", "buy", 2, 100), self.fill("sell", "sell", 1, 110, minute=2)])
        report = self.report()
        changes = [("net_realized_pnl", "1000"), ("fees_imported", "2"),
                   ("broker_verified", True), ("submits_orders", True),
                   ("account_equity", 10010), ("account_return_pct", 1),
                   ("data_kind", "paper_export"), ("schema_version", True),
                   ("fill_audit", []), ("realized_pnl_curve", [])]
        for i, (key, value) in enumerate(changes):
            with self.subTest(key=key):
                altered = copy.deepcopy(report)
                altered[key] = value
                target = self.root / f"invalid-{i}"
                with self.assertRaises(ValueError):
                    render_manual_report(altered, target)
                self.assertFalse(target.exists())

    def test_missing_or_nonfinite_shapes_fail_cleanly(self):
        for i, value in enumerate((None, [], {}, {"x": float("nan")}, {**self.report(), "fills": None})):
            target = self.root / f"malformed-{i}"
            with self.subTest(value=value), self.assertRaises(ValueError):
                render_manual_report(value, target)
            self.assertFalse(target.exists())

    def test_existing_output_and_symlink_ancestors_are_never_overwritten(self):
        result, _ = self.render()
        html = Path(result["html"])
        original = html.read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.render()
        self.assertEqual(html.read_bytes(), original)
        linked = self.root / "linked"
        linked.symlink_to(html.parent, target_is_directory=True)
        for path in (linked, linked / "child"):
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                render_manual_report(self.report(), path)
        self.assertFalse((html.parent / "child").exists())
        missing = self.root / "absent" / "result"
        with self.assertRaisesRegex(ValueError, "parent directory"):
            render_manual_report(self.report(), missing)
        with self.assertRaisesRegex(ValueError, "parent traversal"):
            render_manual_report(self.report(), self.root / ".." / "elsewhere")
        self.assertFalse(missing.parent.exists())

    def test_long_series_is_labeled_sampled_but_json_preserves_every_execution(self):
        rows = [self.fill("opening", "buy", 1001, 100)]
        rows.extend(self.fill(f"exit-{i}", "sell", 1, 101 if i % 2 else 99, minute=i+2) for i in range(1001))
        self.import_rows(rows)
        result, html = self.render()
        self.assertIn("Showing the last 250 of 1002 rows", html)
        self.assertIn("selected points from 1001 sell executions", html)
        saved = json.loads(Path(result["json"]).read_text())
        self.assertEqual(len(saved["fills"]), 1002)
        self.assertEqual(len(saved["realized_pnl_curve"]), 1001)
        self.assertLessEqual(html.count('<circle class="pnl-point"'), 1002)
        self.assertEqual(saved["net_realized_pnl"], "-1")


if __name__ == "__main__":
    unittest.main()
