"""Invented observations test research mechanics, not trading performance."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from dwight.data import Session
from dwight.tokenized_research import compare_market_data, QUANTITY_UNIT

UTC = timezone.utc
OPEN = datetime(2026, 10, 2, 13, 30, tzinfo=UTC)
FINISH = datetime(2026, 10, 3, 10, tzinfo=UTC)


def bar(stamp=OPEN, close=100.0, volume=10.0):
    return {"timestamp": stamp.isoformat(), "open": close, "high": close + 1,
            "low": close - 1, "close": close, "volume": volume}


def source():
    return {"symbol": "QQQ", "asset_class": "equity_etf", "currency": "USD",
            "provider": "fixture", "feed": "fixture", "adjustment": "raw",
            "interval_minutes": 5, "timestamp_label": "start", "data_kind": "synthetic",
            "collected_at": FINISH.isoformat()}


def snapshot():
    return {"schema_version": 1, "data_kind": "synthetic", "execution_enabled": False,
            "interval_minutes": 5, "timestamp_label": "start",
            "instrument": {"symbol": "QQQx", "base": "QQQx", "quote": "USD", "venue": "kraken",
                           "asset_class": "tokenized_equity", "pair": "QQQxUSD", "issuer": "unknown",
                           "chain": None, "contract": None},
            "collection": {"started_at": (FINISH - timedelta(seconds=20)).isoformat(),
                           "finished_at": FINISH.isoformat()},
            "bars": [bar(close=101), bar(OPEN + timedelta(minutes=5), close=102)],
            "orderbook": {"observed_at": (FINISH - timedelta(seconds=10)).isoformat(),
                          "bids": [[100, 5], [99, 10]], "asks": [[102, 5], [103, 10]],
                          "quantity_unit": QUANTITY_UNIT}}


class TokenizedResearchTests(unittest.TestCase):
    def setUp(self):
        calendar = patch("dwight.tokenized_research.exchange_sessions", return_value=[
            Session("2026-10-02", OPEN, OPEN + timedelta(hours=6.5))])
        self.calendar = calendar.start()
        self.addCleanup(calendar.stop)

    def compare(self, qqq=None, token=None, meta=None, **kwargs):
        return compare_market_data(qqq if qqq is not None else [bar(), bar(OPEN + timedelta(minutes=5))],
                                   token if token is not None else snapshot(),
                                   qqq_source=meta if meta is not None else source(), **kwargs)

    def test_exact_interval_join_and_signed_absolute_divergence(self):
        result = self.compare()
        self.assertEqual(result["status"], "overlap")
        self.assertEqual(result["coverage"]["common_bar_count"], 2)
        self.assertAlmostEqual(result["divergence"]["mean_signed_bps"], 150)
        self.assertAlmostEqual(result["divergence"]["max_absolute_bps"], 200)
        self.assertEqual(result["aligned_series"][0]["bar_end"], (OPEN + timedelta(minutes=5)).isoformat())
        self.assertEqual(result["sources"]["qqq"]["feed"], "fixture")
        self.assertFalse(result["execution_enabled"])
        self.assertTrue(result["research_only"])
        json.dumps(result, allow_nan=False)

    def test_synthetic_label_propagates_from_either_source(self):
        for side in ("qqq", "token"):
            token, meta = snapshot(), source()
            token["data_kind"] = "observed_public_market"
            meta["data_kind"] = "historical_real"
            (meta if side == "qqq" else token)["data_kind"] = "synthetic"
            result = self.compare(token=token, meta=meta)
            self.assertEqual(result["data_kind"], "synthetic")
            self.assertIn("SYNTHETIC FIXTURE", result["limitations"][0])

    def test_observation_class_keeps_both_source_kinds(self):
        token, meta = snapshot(), source()
        token["data_kind"], meta["data_kind"] = "observed_public_market", "historical_real"
        result = self.compare(token=token, meta=meta)
        self.assertEqual(result["data_kind"], "observed_market_comparison")
        self.assertEqual(result["sources"]["qqq"]["data_kind"], "historical_real")
        self.assertEqual(result["sources"]["qqqx"]["data_kind"], "observed_public_market")

    def test_no_overlap_has_unknown_metrics_and_no_invented_reference(self):
        token = snapshot()
        token["bars"] = [bar(OPEN + timedelta(minutes=10), 120)]
        result = self.compare(token=token)
        self.assertEqual(result["status"], "no_overlap")
        self.assertIsNone(result["divergence"]["mean_signed_bps"])
        self.assertEqual(result["coverage"]["qqq_covered_fraction"], 0)
        self.assertEqual(result["aligned_series"], [])
        self.assertFalse(result["token_series"][0]["reference_available"])

    def test_missing_rows_not_forward_filled_and_coverage_is_explicit(self):
        result = self.compare(token=dict(snapshot(), bars=[bar(close=101)]))
        coverage = result["coverage"]
        self.assertEqual(coverage["common_bar_count"], 1)
        self.assertEqual(coverage["missing_token_for_qqq"], 1)
        self.assertEqual(coverage["qqq_covered_fraction"], 0.5)

    def test_weekend_and_extended_prices_cannot_enter_reference(self):
        token = snapshot()
        token["bars"] += [bar(OPEN - timedelta(minutes=5), 90), bar(FINISH - timedelta(hours=1), 150)]
        qqq = [bar(), bar(OPEN + timedelta(minutes=5)), bar(OPEN - timedelta(minutes=5), 90),
               bar(FINISH - timedelta(hours=1), 150)]
        result = self.compare(qqq=qqq, token=token)
        self.assertEqual(result["coverage"]["common_bar_count"], 2)
        self.assertEqual(result["coverage"]["token_bars_outside_regular_sessions"], 2)
        self.assertEqual(result["coverage"]["qqq_bars_outside_regular_sessions"], 2)
        self.assertEqual(result["coverage"]["token_bars_outside_qqq_availability"], 2)

    def test_complete_interval_uses_earlier_collection_cutoff(self):
        meta = dict(source(), collected_at=(OPEN + timedelta(minutes=5)).isoformat())
        result = self.compare(meta=meta)
        self.assertEqual(result["coverage"]["common_bar_count"], 1)
        self.assertEqual(result["coverage"]["qqq_incomplete_or_after_cutoff_bars"], 1)
        self.assertEqual(result["coverage"]["token_incomplete_or_after_cutoff_bars"], 1)
        self.assertEqual(result["as_of"], meta["collected_at"])

    def test_forming_bar_is_excluded_until_exact_interval_end(self):
        for seconds, expected in ((299, 0), (300, 1)):
            result = self.compare(meta=dict(source(), collected_at=(OPEN + timedelta(seconds=seconds)).isoformat()))
            self.assertEqual(result["coverage"]["common_bar_count"], expected)

    def test_early_close_calendar_is_respected(self):
        opened = datetime(2025, 11, 28, 14, 30, tzinfo=UTC)
        closed = opened + timedelta(hours=3.5)
        self.calendar.return_value = [Session("2025-11-28", opened, closed)]
        rows = [bar(closed - timedelta(minutes=5)), bar(closed)]
        result = self.compare(qqq=rows, token=dict(snapshot(), bars=deepcopy(rows)))
        self.assertEqual(result["coverage"]["common_bar_count"], 1)
        self.assertEqual(result["coverage"]["token_bars_outside_regular_sessions"], 1)

    def test_vwap_uses_each_market_own_volume_and_does_not_require_shared_prefix(self):
        qqq = [bar(volume=10), bar(OPEN + timedelta(minutes=5), close=110, volume=30)]
        token = snapshot()
        token["bars"] = [bar(close=102, volume=30), bar(OPEN + timedelta(minutes=5), close=112, volume=10)]
        result = self.compare(qqq=qqq, token=token)
        final = result["aligned_series"][-1]
        self.assertAlmostEqual(final["qqq_vwap"], 107.5)
        self.assertAlmostEqual(final["qqqx_vwap"], 104.5)
        self.assertTrue(final["qqq_vwap_prefix_complete"])
        self.assertTrue(final["qqqx_vwap_prefix_complete"])

    def test_missing_session_prefix_is_labelled_and_zero_volume_is_unknown(self):
        second = OPEN + timedelta(minutes=5)
        result = self.compare(qqq=[bar(second, volume=0)], token=dict(snapshot(), bars=[bar(second, volume=0)]))
        first = result["aligned_series"][0]
        self.assertFalse(first["qqq_vwap_prefix_complete"])
        self.assertFalse(first["qqqx_vwap_prefix_complete"])
        self.assertIsNone(first["qqq_vwap"])
        self.assertIsNone(first["qqqx_vwap"])

    def test_zero_volume_closes_keep_coverage_but_have_separate_active_metrics(self):
        token = snapshot()
        token["bars"][0]["volume"] = 0
        result = self.compare(token=token)
        self.assertEqual(result["coverage"]["common_bar_count"], 2)
        self.assertEqual(result["coverage"]["common_active_bar_count"], 1)
        self.assertEqual(result["coverage"]["common_zero_volume_token_bars"], 1)
        self.assertEqual(result["coverage"]["common_zero_volume_qqq_bars"], 0)
        self.assertAlmostEqual(result["divergence"]["mean_absolute_bps"], 150)
        self.assertAlmostEqual(result["divergence"]["active_bar_mean_absolute_bps"], 200)
        self.assertAlmostEqual(result["divergence"]["active_bar_mean_signed_bps"], 200)
        self.assertFalse(result["aligned_series"][0]["token_had_trades"])
        self.assertTrue(result["aligned_series"][0]["qqq_had_trades"])

    def test_no_both_positive_volume_intervals_have_unknown_active_metrics(self):
        token = snapshot()
        token["bars"] = [bar(close=110, volume=0)]
        result = self.compare(qqq=[bar(volume=0)], token=token)
        self.assertEqual(result["coverage"]["common_active_bar_count"], 0)
        self.assertEqual(result["coverage"]["common_zero_volume_token_bars"], 1)
        self.assertEqual(result["coverage"]["common_zero_volume_qqq_bars"], 1)
        for key in ("active_bar_mean_absolute_bps", "active_bar_mean_signed_bps", "active_bar_max_absolute_bps"):
            self.assertIsNone(result["divergence"][key])
        self.assertAlmostEqual(result["divergence"]["mean_absolute_bps"], 1000)

    def test_vwap_resets_at_each_calendar_session(self):
        second = OPEN + timedelta(days=3)
        self.calendar.return_value.append(Session("2026-10-05", second, second + timedelta(hours=6.5)))
        finish = second + timedelta(hours=8)
        token = snapshot()
        token["collection"] = {"started_at": finish.isoformat(), "finished_at": finish.isoformat()}
        token["orderbook"] = None
        token["bars"] = [bar(close=101), bar(second, close=150)]
        result = self.compare(qqq=[bar(), bar(second, close=140)], token=token,
                              meta=dict(source(), collected_at=finish.isoformat()))
        self.assertEqual(result["aligned_series"][-1]["qqq_vwap"], 140)
        self.assertEqual(result["aligned_series"][-1]["qqqx_vwap"], 150)

    def test_book_sweeps_actual_levels_and_insufficient_depth_does_not_extrapolate(self):
        book = self.compare()["orderbook"]
        self.assertEqual(book["status"], "fresh")
        self.assertAlmostEqual(book["spread_bps"], 2 / 101 * 10000)
        one, ten, hundred = book["buy_sweeps"]
        self.assertTrue(one["complete"])
        self.assertEqual(one["average_price_usd"], 102)
        self.assertEqual(ten["average_price_usd"], 102.5)
        self.assertFalse(hundred["complete"])
        self.assertEqual(hundred["filled_units"], 15)
        self.assertIsNone(hundred["full_fill_average_price_usd"])
        self.assertIsNone(hundred["slippage_vs_mid_bps"])
        self.assertIsNone(one["fees_usd"])
        self.assertFalse(one["actual_fill"])
        self.assertIn("base quantity", one["quantity_unit"])
        self.assertEqual(book["sell_sweeps"][1]["average_price_usd"], 99.5)

    def test_decimal_quantity_boundary_does_not_create_false_shortfall(self):
        token = snapshot()
        token["orderbook"]["asks"] = [[102 + index / 10, 0.1] for index in range(10)]
        one = self.compare(token=token)["orderbook"]["buy_sweeps"][0]
        self.assertTrue(one["complete"])
        self.assertEqual(one["filled_units"], 1)

    def test_stale_book_retains_observation_but_does_not_emit_sweeps(self):
        result = self.compare(max_quote_age_seconds=5)
        self.assertEqual(result["orderbook"]["status"], "stale")
        self.assertEqual(result["orderbook"]["buy_sweeps"], [])
        self.assertEqual(result["orderbook"]["age_seconds"], 10)

    def test_absent_or_one_sided_book_is_unknown(self):
        for book, expected in ((None, "unavailable"), (dict(snapshot()["orderbook"], bids=[]), "insufficient_two_sided_book")):
            result = self.compare(token=dict(snapshot(), orderbook=book))
            self.assertEqual(result["orderbook"]["status"], expected)
            self.assertIsNone(result["orderbook"]["spread_bps"])
            self.assertEqual(result["orderbook"]["buy_sweeps"], [])

    def test_missing_quote_not_replaced_by_historical_closes(self):
        result = self.compare()
        self.assertEqual(result["qqq_quote"]["status"], "unavailable")
        self.assertIsNone(result["qqq_quote"]["spread_bps"])

    def test_qqq_quote_fresh_stale_and_after_comparison_are_distinct(self):
        quote = {"symbol": "QQQ", "currency": "USD", "provider": "fixture", "feed": "fixture",
                 "observed_at": (FINISH - timedelta(seconds=5)).isoformat(),
                 "received_at": FINISH.isoformat(), "bid": 99, "ask": 101}
        self.assertAlmostEqual(self.compare(qqq_quotes=quote)["qqq_quote"]["spread_bps"], 200)
        stale = dict(quote, observed_at=(FINISH - timedelta(minutes=1)).isoformat())
        self.assertEqual(self.compare(qqq_quotes=stale)["qqq_quote"]["status"], "stale")
        self.assertIsNone(self.compare(qqq_quotes=stale)["qqq_quote"]["spread_bps"])
        future = dict(quote, received_at=(FINISH + timedelta(seconds=1)).isoformat())
        self.assertEqual(self.compare(qqq_quotes=future)["qqq_quote"]["status"], "after_comparison")

    def test_quote_requires_matching_source_identity_and_time_evidence(self):
        quote = {"symbol": "QQQ", "currency": "USD", "provider": "fixture", "feed": "fixture",
                 "observed_at": (FINISH - timedelta(seconds=5)).isoformat(),
                 "received_at": FINISH.isoformat(), "bid": 99, "ask": 101}
        for update in ({"symbol": "QQQx"}, {"currency": "USDT"}, {"feed": "different"},
                       {"provider": "unknown"}, {"bid": 102}, {"bid": 0}, {"received_at": None},
                       {"observed_at": (FINISH + timedelta(seconds=1)).isoformat()}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.compare(qqq_quotes=dict(quote, **update))

    def test_snapshot_schema_execution_and_conflicting_receipt_times_rejected(self):
        for update in ({"schema_version": True}, {"schema_version": 2}, {"execution_enabled": True},
                       {"data_kind": []}, {"collection": []},
                       {"collection_finished_at": (FINISH + timedelta(seconds=1)).isoformat()}):
            token = snapshot()
            token.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.compare(token=token)

    def test_clean_provenance_keeps_fingerprints_but_omits_arbitrary_private_fields(self):
        meta = dict(source(), dataset_sha256="a" * 64, manifest_sha256="b" * 64,
                    input_sha256="c" * 64, path="/private/path", api_key="fixture-secret")
        public_source = self.compare(meta=meta)["sources"]["qqq"]
        self.assertEqual(public_source["manifest_sha256"], "b" * 64)
        self.assertNotIn("path", public_source)
        self.assertNotIn("api_key", public_source)

    def test_price_volume_and_timestamp_validation(self):
        malformed = [dict(bar(), close=True), dict(bar(), volume=float("nan")), dict(bar(), low=101),
                     dict(bar(), open=-1), dict(bar(), volume=-1), dict(bar(), high=float("inf")),
                     dict(bar(), timestamp=OPEN.replace(tzinfo=None).isoformat()),
                     dict(bar(), timestamp="2026-10-02T15:30:00+02:00"),
                     dict(bar(), timestamp=(OPEN + timedelta(seconds=1)).isoformat()),
                     dict(bar(), timestamp=(OPEN + timedelta(minutes=1)).isoformat())]
        for row in malformed:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.compare(qqq=[row])
            with self.subTest(token=row), self.assertRaises(ValueError):
                self.compare(token=dict(snapshot(), bars=[row]))

    def test_duplicate_bar_timestamps_rejected_before_overlap(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.compare(qqq=[bar(), bar()])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.compare(token=dict(snapshot(), bars=[bar(), bar()]))

    def test_unordered_rows_are_chronologically_sorted(self):
        qqq, token = [bar(), bar(OPEN + timedelta(minutes=5))], snapshot()
        token["bars"].reverse()
        result = self.compare(qqq=list(reversed(qqq)), token=token)
        self.assertEqual(result["aligned_series"][0]["timestamp"], OPEN.isoformat())

    def test_source_metadata_identity_raw_policy_and_no_guesses(self):
        for key, value in (("symbol", "SPY"), ("currency", "USDT"), ("adjustment", "split"),
                           ("asset_class", "stock"), ("interval_minutes", 1),
                           ("timestamp_label", "end"), ("data_kind", "demo"), ("provider", ""),
                           ("feed", None), ("collected_at", None)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.compare(meta=dict(source(), **{key: value}))

    def test_exact_token_usd_venue_identity_and_collection_required(self):
        for key, value in (("symbol", "QQQ"), ("base", "QQQ"), ("quote", "USDT"),
                           ("venue", "some_dex"), ("pair", "QQQxUSDT"), ("asset_class", "crypto")):
            token = snapshot()
            token["instrument"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.compare(token=token)
        token = snapshot()
        token["collection"]["started_at"] = (FINISH + timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "reversed"):
            self.compare(token=token)

    def test_native_pair_metadata_cannot_contradict_identity(self):
        token = snapshot()
        token["instrument"]["provider_pair_metadata"] = {"base": "QQQx", "quote": "USDT",
            "altname": "QQQxUSD", "aclass_base": "tokenized_asset", "aclass_quote": "currency"}
        with self.assertRaisesRegex(ValueError, "contradicts"):
            self.compare(token=token)

    def test_orderbook_bad_sort_crossed_unknown_units_and_future_time_rejected(self):
        for update in ({"bids": [[99, 5], [100, 5]]}, {"asks": [[99, 5]]},
                       {"asks": [[102, 0]]}, {"asks": [[102, True]]},
                       {"quantity_unit": "shares"}, {"bids": [[100, 5], [100, 3]]},
                       {"observed_at": (FINISH + timedelta(seconds=1)).isoformat()}):
            token = snapshot()
            token["orderbook"].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.compare(token=token)

    def test_json_unsafe_arithmetic_is_rejected(self):
        huge = dict(bar(), open=1e308, high=1e308, low=1e308, close=1e308, volume=1e308)
        with self.assertRaisesRegex(ValueError, "finite range"):
            self.compare(qqq=[huge])

    def test_empty_inputs_return_unknown_not_zero_performance(self):
        result = self.compare(qqq=[], token=dict(snapshot(), bars=[], orderbook=None))
        self.assertEqual(result["status"], "no_overlap")
        self.assertIsNone(result["coverage"]["qqq_covered_fraction"])
        self.assertIsNone(result["coverage"]["token_regular_covered_fraction"])
        self.assertIsNone(result["divergence"]["mean_absolute_bps"])
        self.calendar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
