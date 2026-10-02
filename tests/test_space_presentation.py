"""Check that dashboard prose and tables describe the actual experiment evidence."""
from copy import deepcopy
from datetime import datetime
from html.parser import HTMLParser
import re
import unittest
from zoneinfo import ZoneInfo

from deploy.huggingface import analytics, presentation
from deploy.huggingface.transformer_analysis import connection_analysis


class _Tables(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_data(self, value):
        if self.cell is not None:
            self.cell.append(value)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


class SpacePresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = analytics.run_dashboard_experiment("seed42")
        cls.other = analytics.run_dashboard_experiment("seed43")
        cls.cost = analytics.run_dashboard_experiment("coststress")

    def test_metric_cards_show_actual_positive_and_negative_differences(self):
        for payload, expected_sign in ((self.payload, 1), (self.other, -1)):
            with self.subTest(case=payload["case_id"]):
                metrics = payload["test"]["variants"]["filtered"]["metrics"]
                baseline = payload["test"]["variants"]["baseline"]["metrics"]
                delta = metrics["net_pnl"] - baseline["net_pnl"]
                self.assertGreater(expected_sign * delta, 0)
                html = presentation.metric_cards(payload)
                cards = re.findall(r'class="dw-label">([^<]+)</div><div class="dw-value ([^"]*)">([^<]+)</div><small>([^<]+)</small>', html)
                self.assertEqual(len(cards), 4)
                self.assertEqual(cards[0][2], f'{metrics["net_pnl"]:+,.2f}')
                self.assertEqual(cards[1][2], f'{delta:+,.2f}')
                self.assertEqual(cards[1][1], "dw-positive" if delta >= 0 else "dw-negative")
                self.assertEqual(cards[2][2], f'{100 * metrics["max_bar_close_drawdown"]:.2f}%')
                self.assertEqual(cards[3][2], str(metrics["trades"]))
                self.assertIn(f'{100 * metrics["win_rate"]:.1f}%', cards[3][3])

    def test_comparison_table_values_match_each_independent_policy(self):
        rows = _Tables(presentation.comparison_table(self.payload)).rows
        self.assertEqual(len(rows), 4)
        self.assertIn("Independent replay", rows[0])
        for row, (name, label) in zip(rows[1:], analytics.VARIANT_LABELS.items(), strict=True):
            m = self.payload["test"]["variants"][name]["metrics"]
            self.assertEqual(row, [label, str(m["trades"]), f'{m["net_pnl"]:+,.2f}',
                                   f'{100*m["return_fraction"]:+.2f}%', f'{100*m["win_rate"]:.1f}%',
                                   f'{100*m["max_bar_close_drawdown"]:.2f}%', f'{m["expectancy_r"]:+.2f}R'])

    def test_stress_case_is_not_presented_as_an_independent_price_path(self):
        summary = analytics.run_robustness_summary()
        html = presentation.robustness_view(summary)
        rows = _Tables(html).rows[1:]
        self.assertEqual(len(rows), 4)
        for row, actual in zip(rows, summary["rows"], strict=True):
            self.assertEqual(row[0], actual["label"])
            self.assertEqual(row[4], f'{actual["filtered_minus_baseline"]:+,.2f}')
            self.assertEqual(row[6], actual["audit_status"])
        self.assertIn(f'leads on {summary["ordinary_cases_model_ahead"]} of {summary["ordinary_cases"]}', html)
        self.assertIn("retrain", html)
        self.assertIn("doubled costs on path 42", html)
        self.assertIn("not an independent price path", html)
        self.assertIn("pure cost attribution", html)
        note = presentation.method_note(self.cost)
        settings = self.cost["report"]["strategy"]
        self.assertIn(f'Commission is ${settings["commission"]:g} per share per side', note)
        self.assertIn(f'adverse slippage is ${settings["slippage"]:g}', note)

    def test_method_and_diagnostics_preserve_actual_settings_and_scope(self):
        payload = self.payload
        report, settings = payload["report"], payload["report"]["strategy"]
        note = presentation.method_note(payload)
        for name, days in report["partitions"].items():
            self.assertIn(f'{name.title()}:** {days[0]} to {days[-1]} · {len(days)} sessions', note)
        for key in ("input_sha256", "model_sha256", "code_sha256"):
            self.assertIn(report[key], note)
        self.assertIn(f'{100*settings["risk_fraction"]:.2f}% of realized equity', note)
        self.assertIn(f'price target at {settings["reward_r"]:g} times the entry to stop distance', note)
        self.assertIn("Net R includes costs and tick rounding", note)
        self.assertIn("Opening gaps are handled first", note)
        self.assertIn(f'{settings["max_losses"]} losing trades per day', note)
        self.assertIn("not a guaranteed cash loss cap", note)
        self.assertIn("not actual QQQ prices", note)
        self.assertIn("EMA20 is an optional touch level", note)
        self.assertIn("Both VWAP and EMA reset each session", note)
        self.assertIn("No transformer is running", note)
        self.assertIn("long QQQ entries only", note)
        self.assertIn("clocks have not started", note)
        self.assertIn("including model coefficients in memory", note)
        self.assertTrue(payload["provenance"]["artifacts_removed_after_run"])
        self.assertFalse(payload["classifier"]["transformer_active"])
        self.assertEqual(payload["provenance"]["broker_orders_submitted"], 0)
        diagnostics = presentation.diagnostics_note(payload)
        self.assertEqual(len(payload["model"]["feature_names"]), 10)
        self.assertIn(f'Take threshold **{report["selected_threshold"]:g}**', diagnostics)
        self.assertIn("not a measured win rate", diagnostics)
        calibration = payload["classifier"]["calibration"]
        self.assertIn(f'{calibration["brier_score"]:.3f}', diagnostics)
        self.assertIn(f'{calibration["log_loss"]:.3f}', diagnostics)
        for name in ("train", "validation", "test"):
            self.assertIn(f'{report["sample_counts"][name]["labeled"]} {name}', diagnostics)

    def test_trade_table_dates_and_times_are_new_york_session_values(self):
        for variant in analytics.VARIANT_LABELS:
            trades = self.payload["test"]["variants"][variant]["trades"]
            session = trades[-1]["entry_time"][:10]
            selected = [t for t in trades if t["entry_time"][:10] == session]
            rows = presentation.trade_rows(self.payload, session, variant)
            self.assertEqual(len(rows), len(selected))
            for row, trade in zip(rows, selected, strict=True):
                self.assertEqual(row, [trade["entry_time"][11:16], "Long" if trade["direction"] == 1 else "Short",
                                       trade["quantity"], round(trade["entry"], 4), round(trade["stop"], 4),
                                       round(trade["target"], 4), trade["exit_time"][11:16], round(trade["exit"], 4),
                                       trade["exit_reason"].replace("_", " "), round(trade["net_pnl"], 2), round(trade["net_r"], 3)])
            # The same instants encoded in another timezone must still map to
            # the New York session displayed by the adjacent candle chart.
            for zone in ("UTC", "Asia/Tokyo"):
                equivalent = deepcopy(self.payload)
                for trade in equivalent["test"]["variants"][variant]["trades"]:
                    for field in ("entry_time", "exit_time"):
                        trade[field] = datetime.fromisoformat(trade[field]).astimezone(ZoneInfo(zone)).isoformat()
                with self.subTest(variant=variant, zone=zone):
                    self.assertEqual(presentation.trade_rows(equivalent, session, variant), rows)

    def test_audit_table_contains_each_real_check_and_evidence_count(self):
        audit = self.payload["audit"]
        html = presentation.audit_view(self.payload)
        rows = _Tables(html).rows[1:]
        self.assertEqual(len(rows), len(audit["checks"]))
        for row, actual in zip(rows, audit["checks"], strict=True):
            self.assertEqual(row, [actual["name"], actual["status"], str(actual["evidence_count"]), actual["detail"]])
        self.assertIn(f'Replay audit: {audit["status"]}', html)
        self.assertEqual(audit["runtime_skills"]["used"], [])
        self.assertIn("Runtime agent skills used: none", html)
        self.assertIn("does not certify profit", html)

    def test_transformer_connections_are_proposals_without_measured_uplift(self):
        current = self.payload["classifier"]
        self.assertFalse(current["transformer_active"])
        comparison = _Tables(presentation.transformer_comparison()).rows[1:]
        arms = connection_analysis("both")["experiments"]
        self.assertEqual(len(comparison), len(arms))
        for row, arm in zip(comparison, arms, strict=True):
            self.assertEqual(row, [arm["title"], arm["features"], arm["status"], "Not measured on real QQQ"])
        for choice in ("price", "news", "both"):
            analysis = connection_analysis(choice)
            flow = presentation.transformer_flow(choice)
            details = presentation.transformer_details(choice)
            self.assertFalse(analysis["connected"])
            self.assertIsNone(analysis["measured_uplift"])
            self.assertEqual(flow.count("PROPOSED · NOT CONNECTED"), len(analysis["selected_paths"]))
            self.assertIn("Their trading contribution has not been measured", flow)
            self.assertIn("Transformer outputs never alter the fixed risk policy", flow)
            for path in analysis["selected_paths"]:
                self.assertIn(path["title"], details)
                self.assertIn(path["baseline_comparison"], details)
                for model in path["models"]:
                    self.assertIn(model["url"], details)
                    self.assertIn(model["license"], details)
            self.assertNotRegex(flow + details, r"[+]\d+(?:\.\d+)?%")
        protocol = presentation.transformer_protocol()
        self.assertIn("requires a new schema", protocol)
        self.assertIn("training data only", protocol)
        self.assertIn("forward shadow", protocol)


if __name__ == "__main__":
    unittest.main()
