"""Broker fixtures only: no credentials, network requests or real account records."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dwight.paper import PAPER_URL
from dwight.paper_reporting import PaperReportError, collect_paper_report, render_paper_report


NOW = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)
CREATED = (NOW - timedelta(hours=2)).isoformat()


def fill(aid, oid, side, quantity, price, minute, **updates):
    return {"id": aid, "order_id": oid, "activity_type": "FILL", "type": "partial_fill",
            "symbol": "QQQ", "side": side, "qty": str(quantity), "price": str(price),
            "cum_qty": "999",  # Deliberately unsuitable for execution accounting.
            "transaction_time": (NOW - timedelta(minutes=minute)).isoformat(), **updates}


def entry(cid="entry-1", oid="parent-1", quantity="4", filled="4", sold="3"):
    return {"id": oid, "client_order_id": cid, "symbol": "QQQ", "side": "buy", "qty": quantity,
            "filled_qty": filled, "filled_avg_price": "987654", "order_class": "bracket", "status": "filled",
            "legs": [{"id": "stop-" + oid, "symbol": "QQQ", "side": "sell", "qty": quantity,
                      "filled_qty": "0", "type": "stop", "status": "new"},
                     {"id": "target-" + oid, "symbol": "QQQ", "side": "sell", "qty": quantity,
                      "filled_qty": sold, "type": "limit", "status": "partially_filled"}]}


class ReportingBroker:
    """Exposes GET-shaped methods only, making broker mutations impossible."""
    base_url = PAPER_URL
    fixture_clock = True

    def __init__(self):
        self.account = {"id": "fixture-paper-account", "equity": "123456", "last_equity": "111111"}
        self.orders = {"entry-1": entry()}
        self.positions = [{"symbol": "QQQ", "qty": "1"}]
        self.activities = [fill("buy-a", "parent-1", "buy", 2, 100, 100),
                           fill("buy-b", "parent-1", "buy", 2, 102, 90),
                           fill("sell-a", "target-parent-1", "sell", 1, 105, 80),
                           fill("sell-b", "target-parent-1", "sell", 2, 103, 70)]
        self.calls = []
        self.pages = None
        self.failure_page = None
        self.final_account = None

    def get_account(self):
        self.calls.append(("account",))
        if self.final_account and sum(call[0] == "account" for call in self.calls) > 1:
            return copy.deepcopy(self.final_account)
        return copy.deepcopy(self.account)

    def get_order_by_client_id(self, cid):
        self.calls.append(("order", cid))
        return copy.deepcopy(self.orders.get(cid))

    def get_positions(self):
        self.calls.append(("positions",))
        return copy.deepcopy(self.positions)

    def get_fill_activities(self, after, until, page_token=None, page_size=100):
        page_index = sum(call[0] == "fills" for call in self.calls)
        self.calls.append(("fills", after, until, page_token, page_size))
        if self.failure_page == page_index:
            raise TimeoutError("DO-NOT-EXPOSE-secret-token")
        if self.pages is not None:
            return copy.deepcopy(self.pages[page_index])
        return copy.deepcopy(self.activities if page_token is None else [])


class PaperReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.execution = self.root / "execution.sqlite3"
        self.state = self.root / "reporting.sqlite3"
        self.broker = ReportingBroker()
        with sqlite3.connect(self.execution) as db:
            db.executescript("""
                CREATE TABLE paper_meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE paper_intents(client_order_id TEXT PRIMARY KEY,payload TEXT,created_at TEXT,broker_order TEXT);
                CREATE TABLE paper_exit_intents(client_order_id TEXT PRIMARY KEY,parent_client_order_id TEXT,payload TEXT,created_at TEXT,broker_order TEXT);
                CREATE TABLE paper_audit(event TEXT,detail TEXT);
            """)
            db.execute("INSERT INTO paper_meta VALUES('account_id',?)", (self.broker.account["id"],))
            self.insert_entry(db, self.broker.orders["entry-1"])

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def insert_entry(db, order):
        payload = {"symbol": "QQQ", "side": "buy", "qty": order["qty"], "order_class": "bracket"}
        db.execute("INSERT INTO paper_intents VALUES(?,?,?,?)",
                   (order["client_order_id"], json.dumps(payload), CREATED, json.dumps(order)))

    def collect(self, **updates):
        return collect_paper_report(self.broker, self.execution, self.state, now=NOW, **updates)

    def report_count(self):
        with sqlite3.connect(self.state) as db:
            return db.execute("SELECT count(*) FROM paper_reports").fetchone()[0]

    def test_individual_partial_executions_fifo_and_unknown_costs(self):
        before = self.execution.read_bytes()
        report = self.collect()
        self.assertEqual(self.execution.read_bytes(), before)
        self.assertEqual(report["metrics"], {
            "buy_quantity": "4", "sell_quantity": "3", "open_quantity": "1", "realized_quantity": "3",
            "matched_disposals": 2, "gross_realized_pnl_usd": "9", "fees_usd": None,
            "net_realized_pnl_usd": None, "account_return_pct": None})
        self.assertEqual([match["gross_realized_pnl_usd"] for match in report["fifo_matches"]], ["5", "3", "1"])
        self.assertEqual(report["reconciliation"]["status"], "matched")
        self.assertEqual(report["console_summary"]["account_id"], self.broker.account["id"])
        self.assertEqual(report["console_summary"]["gross_realized_pnl_usd"], "9")
        self.assertTrue(report["fixture"])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.state) as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT record FROM fill_activities")]
        self.assertEqual(len(records), 4)
        self.assertEqual({row["activity_id"] for row in records}, {"buy-a", "buy-b", "sell-a", "sell-b"})
        self.assertTrue(all("raw" in row for row in records))

    def test_pages_fixed_cutoff_and_identical_duplicates_are_idempotent(self):
        a = self.broker.activities
        self.broker.pages = [a[:2], [a[1], *a[2:]], []]
        report = self.collect()
        self.assertEqual(report["window"]["pages"], 3)
        self.assertEqual(report["window"]["rows"], 5)
        self.assertEqual(report["ledger"]["activity_count"], 4)
        calls = [call for call in self.broker.calls if call[0] == "fills"]
        self.assertEqual([call[3] for call in calls], [None, "buy-b", "sell-b"])
        self.assertEqual(len({call[1] for call in calls}), 1)
        self.assertEqual({call[2] for call in calls}, {NOW.isoformat()})
        self.broker.calls.clear()
        repeated = self.collect()
        self.assertEqual(repeated["ledger"]["checksum_sha256"], report["ledger"]["checksum_sha256"])
        self.assertEqual(self.report_count(), 2)

    def test_page_failure_never_publishes_partial_window_or_error_secrets(self):
        self.broker.failure_page = 1
        with self.assertRaises(PaperReportError) as error:
            self.collect()
        self.assertNotIn("secret", str(error.exception))
        self.assertFalse(self.state.exists())

    def test_failure_preserves_previous_report_and_immutable_activities(self):
        first = self.collect()
        before = self.state.read_bytes()
        self.broker.calls.clear()
        self.broker.failure_page = 1
        with self.assertRaises(PaperReportError):
            self.collect()
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.report_count(), 1)
        with sqlite3.connect(self.state) as db:
            compact = json.loads(db.execute("SELECT report FROM paper_reports").fetchone()[0])
            self.assertEqual(compact["ledger"], first["ledger"])
            self.assertEqual(compact["metrics"], first["metrics"])
            self.assertNotIn("owned_executions", compact)
            for table, column in (("fill_activities", "record"), ("owned_orders", "identity"), ("paper_reports", "report"), ("report_meta", "value")):
                with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f"UPDATE {table} SET {column}={column}")
                with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f"DELETE FROM {table}")

    def test_conflicting_duplicate_or_corrected_activity_rejected(self):
        self.broker.activities.append({**self.broker.activities[0], "price": "999"})
        with self.assertRaisesRegex(PaperReportError, "Conflicting duplicate"):
            self.collect()
        self.assertFalse(self.state.exists())
        self.broker.activities.pop()
        self.collect()
        before = self.state.read_bytes()
        self.broker.activities[0]["price"] = "101"
        with self.assertRaisesRegex(PaperReportError, "immutable"):
            self.collect()
        self.assertEqual(self.state.read_bytes(), before)

    def test_disappeared_activity_rejected(self):
        self.collect()
        self.broker.activities.pop()
        with self.assertRaisesRegex(PaperReportError, "missing"):
            self.collect()
        self.assertEqual(self.report_count(), 1)

    def test_nonempty_short_page_still_requires_empty_page_and_bound(self):
        with self.assertRaisesRegex(PaperReportError, "page bound"):
            self.collect(max_pages=1)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.collect(max_pages=2)["window"]["empty_page_observed"], True)

    def test_repeated_token_and_oversized_page_rejected(self):
        self.broker.pages = [self.broker.activities, self.broker.activities]
        with self.assertRaisesRegex(PaperReportError, "did not advance"):
            self.collect()
        self.broker.calls.clear()
        self.broker.pages = [self.broker.activities * 26]
        with self.assertRaisesRegex(PaperReportError, "oversized"):
            self.collect()

    def test_unrelated_qqq_and_other_symbol_fills_excluded(self):
        self.broker.activities += [fill("foreign-qqq", "manual-qqq", "buy", 100, 200, 60),
                                   fill("foreign-spy", "manual-spy", "sell", 50, 100, 50, symbol="SPY")]
        report = self.collect()
        self.assertEqual(report["metrics"]["gross_realized_pnl_usd"], "9")
        self.assertEqual(report["ledger"]["activity_count"], 6)
        self.assertEqual(report["ledger"]["owned_activity_count"], 4)
        self.assertEqual(report["console_summary"]["excluded_fill_count"], 2)
        self.assertEqual({row["activity_id"] for row in report["excluded_executions"]}, {"foreign-qqq", "foreign-spy"})

    def test_live_endpoint_and_account_mismatches_fail_closed(self):
        self.broker.base_url = "https://api.alpaca.markets"
        with self.assertRaisesRegex(PaperReportError, "paper endpoint"):
            self.collect()
        self.broker.base_url = PAPER_URL
        self.broker.account["id"] = "other-account"
        with self.assertRaisesRegex(PaperReportError, "different paper accounts"):
            self.collect()
        self.assertFalse(self.state.exists())

    def test_state_ledger_cannot_be_reused_for_another_account(self):
        self.collect()
        self.broker.account["id"] = "other-account"
        with sqlite3.connect(self.execution) as db:
            db.execute("UPDATE paper_meta SET value=? WHERE key='account_id'", ("other-account",))
        with self.assertRaisesRegex(PaperReportError, "scope mismatch"):
            self.collect()

    def test_account_change_during_collection_rejects_all_evidence(self):
        self.broker.final_account = {"id": "other-account"}
        with self.assertRaisesRegex(PaperReportError, "changed during"):
            self.collect()
        self.assertFalse(self.state.exists())

    def test_missing_execution_account_binding_is_not_inferred(self):
        with sqlite3.connect(self.execution) as db:
            db.execute("DELETE FROM paper_meta")
        with self.assertRaisesRegex(PaperReportError, "no verified paper account"):
            self.collect()
        self.assertEqual(self.broker.calls, [])

    def test_malformed_or_future_activities_reject_without_partial_state(self):
        cases = [{"id": ""}, {"order_id": ""}, {"price": "NaN"}, {"qty": "0"}, {"qty": "-1"},
                 {"transaction_time": "2026-10-02T15:00:00"}, {"activity_type": "FEE"}, {"side": "sell_short"},
                 {"transaction_time": (NOW + timedelta(seconds=1)).isoformat()}, {"account_id": "wrong"}]
        original = copy.deepcopy(self.broker.activities)
        for updates in cases:
            with self.subTest(updates=updates):
                self.broker.activities = copy.deepcopy(original)
                self.broker.activities[0].update(updates)
                with self.assertRaises(PaperReportError):
                    self.collect()
                self.assertFalse(self.state.exists())

    def test_same_timestamp_fifo_ambiguity_and_oversell_rejected(self):
        self.broker.activities[1]["transaction_time"] = self.broker.activities[0]["transaction_time"]
        with self.assertRaisesRegex(PaperReportError, "Same-timestamp"):
            self.collect()
        self.broker.activities.pop(1)
        with self.assertRaisesRegex(PaperReportError, "oversell"):
            self.collect()

    def test_nanosecond_timestamps_not_truncated_into_ambiguous_ties(self):
        self.broker.activities[0]["transaction_time"] = "2026-10-02T14:20:00.000000001Z"
        self.broker.activities[1]["transaction_time"] = "2026-10-02T14:20:00.000000002Z"
        report = self.collect()
        self.assertEqual(report["metrics"]["gross_realized_pnl_usd"], "9")
        self.assertEqual(report["owned_executions"][0]["transaction_time"], "2026-10-02T14:20:00.000000001+00:00")

    def test_quantity_mismatch_is_retryable_and_withholds_gross(self):
        self.broker.orders["entry-1"]["filled_qty"] = "3"
        report = self.collect()
        self.assertEqual(report["reconciliation"]["status"], "mismatch")
        self.assertEqual(report["console_summary"]["status"], "reconciliation_required")
        self.assertIsNone(report["metrics"]["gross_realized_pnl_usd"])
        self.assertEqual(report["fifo_matches"], [])
        self.assertEqual(report["ledger"]["activity_count"], 4)
        self.broker.orders["entry-1"]["filled_qty"] = "4"
        self.assertEqual(self.collect()["metrics"]["gross_realized_pnl_usd"], "9")

    def test_position_mismatch_withholds_pnl(self):
        self.broker.positions[0]["qty"] = "2"
        report = self.collect()
        self.assertEqual(report["reconciliation"]["broker_qqq_position_quantity"], "2")
        self.assertIsNone(report["metrics"]["gross_realized_pnl_usd"])

    def test_supervisor_close_and_new_bracket_ids_belong_to_owned_entry(self):
        current = self.broker.orders["entry-1"]
        current["legs"][1]["filled_qty"] = "0"
        close = {"id": "supervisor-close", "client_order_id": "close-1", "symbol": "QQQ", "side": "sell",
                 "qty": "3", "filled_qty": "3", "order_class": "simple", "type": "market", "time_in_force": "day"}
        self.broker.orders["close-1"] = close
        for activity in self.broker.activities[2:]:
            activity["order_id"] = close["id"]
        with sqlite3.connect(self.execution) as db:
            saved = copy.deepcopy(current)
            saved["legs"] = []  # Fresh GET must discover the genuine bracket ids.
            db.execute("UPDATE paper_intents SET broker_order=?", (json.dumps(saved),))
            db.execute("INSERT INTO paper_exit_intents VALUES(?,?,?,?,?)",
                       ("close-1", "entry-1", json.dumps({"symbol": "QQQ", "side": "sell", "qty": "3"}), CREATED, json.dumps(close)))
        report = self.collect()
        self.assertEqual(report["metrics"]["gross_realized_pnl_usd"], "9")
        with sqlite3.connect(self.state) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM owned_orders").fetchone()[0], 4)

    def test_identity_replacement_detached_exit_and_wrong_symbol_rejected(self):
        self.broker.orders["entry-1"]["id"] = "replacement-parent"
        with self.assertRaisesRegex(PaperReportError, "identity changed"):
            self.collect()
        self.broker.orders["entry-1"] = entry()
        self.broker.orders["entry-1"]["replaces"] = "old-order"
        with self.assertRaisesRegex(PaperReportError, "replacement"):
            self.collect()
        self.broker.orders["entry-1"] = entry()
        with sqlite3.connect(self.execution) as db:
            db.execute("INSERT INTO paper_exit_intents VALUES('close-1','foreign-entry','{}',?,NULL)", (CREATED,))
        with self.assertRaisesRegex(PaperReportError, "no owned parent"):
            self.collect()

    def test_no_fills_does_not_invent_realized_performance(self):
        self.broker.activities = []
        self.broker.positions = []
        self.broker.orders["entry-1"] = entry(filled="0", sold="0")
        report = self.collect()
        self.assertEqual(report["window"]["pages"], 1)
        self.assertTrue(report["window"]["empty_page_observed"])
        self.assertIsNone(report["metrics"]["gross_realized_pnl_usd"])
        self.assertIsNone(report["console_summary"]["open_quantity"])
        self.assertEqual(report["console_summary"]["status"], "no_evidence")

    def test_evidenced_breakeven_realization_can_be_zero(self):
        self.broker.activities[2]["price"] = "100"
        self.broker.activities[3]["price"] = "101"
        report = self.collect()
        self.assertEqual(report["metrics"]["gross_realized_pnl_usd"], "0")
        self.assertIsNone(report["metrics"]["fees_usd"])
        self.assertIsNone(report["metrics"]["net_realized_pnl_usd"])

    def test_only_unrelated_activities_do_not_supply_owned_performance(self):
        self.broker.orders["entry-1"] = entry(filled="0", sold="0")
        self.broker.positions = []
        self.broker.activities = [fill("manual-fill", "manual-order", "buy", 1, 100, 50)]
        report = self.collect()
        self.assertEqual(report["ledger"]["excluded_activity_count"], 1)
        self.assertEqual(report["ledger"]["owned_activity_count"], 0)
        self.assertIsNone(report["metrics"]["gross_realized_pnl_usd"])
        self.assertIsNone(report["console_summary"]["open_quantity"])
        self.assertEqual(report["console_summary"]["status"], "no_evidence")

    def test_no_intents_does_not_invent_reporting_start_or_equity_returns(self):
        with sqlite3.connect(self.execution) as db:
            db.execute("DELETE FROM paper_intents")
        report = self.collect()
        self.assertIsNone(report["window"]["after"])
        self.assertFalse(report["window"]["empty_page_observed"])
        self.assertEqual(report["window"]["pages"], 0)
        self.assertFalse(any(call[0] == "fills" for call in self.broker.calls))
        self.assertIsNone(report["metrics"]["open_quantity"])
        self.assertIsNone(report["metrics"]["account_return_pct"])
        self.assertEqual(report["console_summary"]["status"], "no_evidence")

    def test_frozen_window_does_not_silently_drop_older_history(self):
        first = self.collect()
        with sqlite3.connect(self.execution) as db:
            db.execute("UPDATE paper_intents SET created_at=?", ((NOW - timedelta(minutes=100)).isoformat(),))
        self.assertEqual(self.collect()["window"]["after"], first["window"]["after"])
        with sqlite3.connect(self.execution) as db:
            db.execute("UPDATE paper_intents SET created_at=?", ((NOW - timedelta(days=1)).isoformat(),))
        with self.assertRaisesRegex(PaperReportError, "predates"):
            self.collect()

    def test_private_escaped_html_json_and_checksum_with_new_output_directory(self):
        dangerous = '<script>alert("broker")</script>'
        self.broker.activities[0]["id"] = dangerous
        report = self.collect()
        output = self.root / "frozen-report"
        result = render_paper_report(report, output)
        document = Path(result["html"]).read_text()
        self.assertNotIn(dangerous, document)
        self.assertIn("&lt;script&gt;", document)
        self.assertIn("Unavailable / Unavailable / Unavailable", document)
        self.assertNotIn("<script", document)
        self.assertNotIn("https://", document)
        self.assertNotIn("123456", document)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        manifest = json.loads(Path(result["manifest"]).read_text())
        for path in output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(hashlib.sha256(Path(result["json"]).read_bytes()).hexdigest(), manifest["checksum_sha256"])
        self.assertEqual(hashlib.sha256(Path(result["html"]).read_bytes()).hexdigest(), manifest["html_checksum_sha256"])
        self.assertEqual(json.loads(Path(result["json"]).read_text()), report)
        with self.assertRaisesRegex(PaperReportError, "new directory"):
            render_paper_report(report, output)

    def test_fixture_report_is_visibly_labelled_and_never_mixed(self):
        self.broker.fixture_clock = True
        report = self.collect()
        self.assertTrue(report["fixture"])
        result = render_paper_report(report, self.root / "fixture-report")
        document = Path(result["html"]).read_text()
        self.assertIn("FIXTURE", document)
        self.assertIn("not actual broker results", document)
        self.broker.fixture_clock = False
        with self.assertRaisesRegex(PaperReportError, "separate ledgers"):
            self.collect()

    def test_public_paths_and_symlinks_rejected(self):
        public = self.root / "public"
        public.mkdir(mode=0o755)
        with self.assertRaisesRegex(PaperReportError, "private directory"):
            collect_paper_report(self.broker, self.execution, public / "state.sqlite3", now=NOW)
        link = self.root / "state-link.sqlite3"
        link.symlink_to(self.execution)
        with self.assertRaisesRegex(PaperReportError, "symbolic"):
            collect_paper_report(self.broker, self.execution, link, now=NOW)
        report = self.collect()
        directory_link = self.root / "output-link"
        directory_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(PaperReportError, "symbolic"):
            render_paper_report(report, directory_link)

    def test_false_fixture_marker_does_not_label_a_broker_report_fixture(self):
        self.broker.fixture_clock = False
        report = self.collect()
        self.assertFalse(report["fixture"])
        self.assertFalse(report["console_summary"]["fixture"])

    def test_execution_hardlink_alias_rejected_without_chmod_or_mutation(self):
        os.link(self.execution, self.state)
        before = self.execution.read_bytes()
        mode = self.execution.stat().st_mode
        with self.assertRaisesRegex(PaperReportError, "separate from"):
            self.collect()
        self.assertEqual(self.execution.read_bytes(), before)
        self.assertEqual(self.execution.stat().st_mode, mode)

    def test_initial_publication_failure_leaves_no_incomplete_reporting_db(self):
        from dwight.paper_reporting import _schema
        def failed_schema(db):
            _schema(db)
            raise sqlite3.OperationalError("fixture disk failure")
        with patch("dwight.paper_reporting._schema", side_effect=failed_schema):
            with self.assertRaisesRegex(PaperReportError, "publication failed"):
                self.collect()
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertEqual(self.collect()["metrics"]["gross_realized_pnl_usd"], "9")

    def test_persisted_report_and_byte_bounds_stop_without_rewriting_evidence(self):
        self.collect()
        before = self.state.read_bytes()
        self.broker.calls.clear()
        with patch("dwight.paper_reporting.MAX_REPORTS", 1):
            with self.assertRaisesRegex(PaperReportError, "report bound"):
                self.collect()
        self.assertEqual(self.state.read_bytes(), before)
        with patch("dwight.paper_reporting.MAX_STATE_BYTES", 1):
            with self.assertRaisesRegex(PaperReportError, "32 MiB"):
                self.collect()
        self.assertEqual(self.state.read_bytes(), before)

    def test_renderer_recomputes_metrics_and_rejects_invented_costs(self):
        report = self.collect()
        for key, value in (("gross_realized_pnl_usd", "999"), ("fees_usd", "0"), ("account_return_pct", "1")):
            changed = copy.deepcopy(report)
            changed["metrics"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(PaperReportError, "do not match"):
                render_paper_report(changed, self.root / key)
            self.assertFalse((self.root / key).exists())

    def test_realized_curve_uses_actual_disposal_times_and_is_withheld_on_mismatch(self):
        report = self.collect()
        rendered = render_paper_report(report, self.root / "matched-curve")
        document = Path(rendered["html"]).read_text()
        self.assertIn("<polyline", document)
        self.assertIn("Before fees", document)
        # Buys are earlier than disposals and must not become invented curve origins.
        curve = document.split("<svg", 1)[1].split("</svg>", 1)[0]
        self.assertIn("2026-10-02T14:40:00+00:00", curve)
        self.assertNotIn("2026-10-02T14:20:00+00:00", curve)
        self.broker.positions[0]["qty"] = "2"
        mismatched = self.collect()
        rendered = render_paper_report(mismatched, self.root / "mismatched-curve")
        self.assertNotIn("<polyline", Path(rendered["html"]).read_text())


if __name__ == "__main__":
    unittest.main()
