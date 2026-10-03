"""Ensure interactive charts carry the experiment evidence without invented fills."""
import json
import unittest

from deploy.huggingface import analytics, charts


class SpaceChartsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = analytics.run_dashboard_experiment()

    def test_homepage_preview_preserves_every_equity_mark_and_source_payload(self):
        before = json.dumps(self.payload, sort_keys=True, allow_nan=False)
        figure = charts.preview_figure(self.payload)
        self.assertEqual(len(figure.data), 3)
        for trace, (name, variant) in zip(figure.data, self.payload["test"]["variants"].items(), strict=True):
            curve = variant["curve"]
            self.assertEqual(trace.name, charts.CHART_LABELS[name])
            self.assertEqual(list(trace.x), [charts._clock(row["timestamp"]) for row in curve])
            self.assertEqual(list(trace.y), [row["equity"] for row in curve])
            self.assertEqual(list(trace.customdata), [row["timestamp"] for row in curve])
        self.assertEqual(len(figure.layout.shapes), 1)
        reference = figure.layout.shapes[0]
        self.assertEqual(reference.y0, self.payload["test"]["initial_capital"])
        self.assertEqual(reference.y1, self.payload["test"]["initial_capital"])
        self.assertTrue(figure.to_json())
        self.assertEqual(json.dumps(self.payload, sort_keys=True, allow_nan=False), before)

    def test_equity_and_drawdown_use_every_saved_mark(self):
        figure = charts.equity_figure(self.payload)
        self.assertEqual(len(figure.data), 6)
        for index, (name, variant) in enumerate(self.payload["test"]["variants"].items()):
            equity, drawdown = figure.data[index * 2:index * 2 + 2]
            self.assertEqual(list(equity.y), [row["equity"] for row in variant["curve"]])
            self.assertEqual(list(drawdown.y), [-100 * row["drawdown_fraction"] for row in variant["curve"]])
            self.assertEqual(list(equity.x), [charts._clock(row["timestamp"]) for row in variant["curve"]])
            self.assertEqual(equity.line.color, charts.COLORS[name])
        self.assertEqual(figure.layout.shapes[0].y0, self.payload["test"]["initial_capital"])
        self.assertIn("New York", figure.layout.xaxis2.title.text)

    def test_session_candles_indicators_volume_and_fills_match_records(self):
        day = charts.default_session(self.payload)
        figure = charts.session_figure(self.payload, day)
        candles = [row for row in self.payload["test"]["candles"] if row["session"] == day]
        trades = [row for row in self.payload["test"]["variants"]["filtered"]["trades"] if row["entry_time"][:10] == day]
        self.assertTrue(trades)
        self.assertEqual(figure.data[0].type, "candlestick")
        for key in ("open", "high", "low", "close"):
            self.assertEqual(list(getattr(figure.data[0], key)), [row[key] for row in candles])
        self.assertEqual(list(figure.data[1].y), [row["vwap"] for row in candles])
        self.assertEqual(list(figure.data[2].y), [row["ema20"] for row in candles])
        self.assertEqual(list(figure.data[3].y), [row["volume"] for row in candles])
        markers = [trace for trace in figure.data if trace.type == "scatter" and trace.mode == "markers"]
        self.assertEqual(sum(len(trace.y) for trace in markers), 2 * len(trades))
        exits = next(trace for trace in markers if trace.name == "Simulated exit")
        self.assertEqual(list(exits.y), [row["exit"] for row in trades])
        self.assertEqual(list(exits.x), [charts._clock(row["exit_time"]) for row in trades])
        self.assertIn("Exact intrabar fill time is unknown", exits.hovertemplate)
        self.assertFalse(figure.layout.xaxis.rangeslider.visible)

    def test_empty_session_does_not_invent_trade_markers(self):
        trade_days = {row["entry_time"][:10] for row in self.payload["test"]["variants"]["filtered"]["trades"]}
        day = next(day for day in charts.sessions(self.payload) if day not in trade_days)
        figure = charts.session_figure(self.payload, day)
        self.assertEqual(len(figure.data), 4)
        self.assertIn("No simulated trades", figure.layout.annotations[0].text)

    def test_session_and_variant_are_constrained_to_evidence(self):
        for invalid in ("2020-01-01", "../data.csv", None, []):
            with self.subTest(session=invalid), self.assertRaises(ValueError):
                charts.session_figure(self.payload, invalid)
        for invalid in ("transformer", "live", {}, None):
            with self.subTest(variant=invalid), self.assertRaises(ValueError):
                charts.session_figure(self.payload, charts.default_session(self.payload), invalid)
        days = charts.sessions(self.payload)
        self.assertEqual(days, self.payload["report"]["partitions"]["test"])
        self.assertEqual(charts.default_session(self.payload), self.payload["test"]["variants"]["filtered"]["trades"][-1]["entry_time"][:10])

    def test_diagnostics_use_nonempty_calibration_bins_and_score_contributions(self):
        figure = charts.diagnostics_figure(self.payload)
        diagonal, calibration, contributions = figure.data
        self.assertEqual(list(diagonal.y), [0, 1])
        self.assertIn("reference", diagonal.name)
        bins = [row for row in self.payload["classifier"]["calibration"]["calibration_bins"] if row["count"]]
        self.assertEqual(list(calibration.x), [row["mean_probability"] for row in bins])
        self.assertEqual(list(calibration.y), [row["observed_win_rate"] for row in bins])
        self.assertEqual([row[0] for row in calibration.customdata], [row["count"] for row in bins])
        terms = sorted(self.payload["classifier"]["feature_contributions"], key=lambda row: row["mean_abs_contribution"])
        self.assertEqual(list(contributions.x), [row["mean_abs_contribution"] for row in terms])
        self.assertIn("not a causal effect", contributions.hovertemplate)

    def test_outcome_totals_match_all_three_portfolios(self):
        figure = charts.outcomes_figure(self.payload)
        self.assertEqual(list(figure.data[0].y), [row["net_r"] for row in self.payload["test"]["variants"]["filtered"]["trades"]])
        for trace, (name, variant) in zip(figure.data[1:], self.payload["test"]["variants"].items(), strict=True):
            self.assertEqual(trace.name, charts.CHART_LABELS[name])
            self.assertAlmostEqual(sum(trace.y), variant["metrics"]["net_pnl"])
            self.assertEqual(len(trace.y), len(charts.sessions(self.payload)))

    def test_charts_are_json_serializable_and_do_not_mutate_evidence(self):
        before = json.dumps(self.payload, sort_keys=True, allow_nan=False)
        for figure in (charts.equity_figure(self.payload),
                       charts.session_figure(self.payload, charts.default_session(self.payload)),
                       charts.diagnostics_figure(self.payload), charts.outcomes_figure(self.payload)):
            encoded = figure.to_json()
            self.assertTrue(encoded)
            self.assertEqual(figure.layout.paper_bgcolor, "#0a0a0a")
            self.assertNotIn("https://", encoded)
        self.assertEqual(json.dumps(self.payload, sort_keys=True, allow_nan=False), before)


if __name__ == "__main__":
    unittest.main()
