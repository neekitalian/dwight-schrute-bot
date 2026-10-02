from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from dwight.context import MAX_CONTEXT_FEATURES, PointInTimeContext


UTC = timezone.utc
AVAILABLE = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
NAMES = ("earnings_revision", "news_sentiment")


def snapshot(**changes):
    row = {
        "symbol": "QQQ",
        "published_at": (AVAILABLE - timedelta(minutes=10)).isoformat(),
        "available_at": AVAILABLE.isoformat(),
        "expires_at": (AVAILABLE + timedelta(days=2)).isoformat(),
        "source": "fixture:timestamped-release",
        "features": {"earnings_revision": -0.2, "news_sentiment": 0},
    }
    row.update(changes)
    return row


def document(**changes):
    value = {"schema_version": 1, "symbol": "QQQ", "feature_names": list(NAMES),
             "snapshots": [snapshot()]}
    value.update(changes)
    return value


class ContextTests(unittest.TestCase):
    def test_future_publication_and_processing_delay_cannot_leak(self):
        context = PointInTimeContext(NAMES, [snapshot()])
        for at in (AVAILABLE - timedelta(days=1), AVAILABLE - timedelta(minutes=5),
                   AVAILABLE - timedelta(microseconds=1)):
            with self.subTest(at=at):
                result = context.lookup(at)
                self.assertFalse(result["available"])
                self.assertEqual(result["values"], [0.0, 0.0])
                self.assertEqual(result["age_days"], 0.0)
                for key in ("source", "published_at", "available_at", "expires_at"):
                    self.assertIsNone(result[key])
        result = context.lookup(AVAILABLE)
        self.assertTrue(result["available"])
        self.assertEqual(result["values"], [-0.2, 0.0])
        self.assertEqual(result["age_days"], 0.0)
        self.assertEqual(context.lookup(AVAILABLE + timedelta(hours=12))["age_days"], 0.5)

    def test_newest_expired_snapshot_never_revives_older_valid_vintage(self):
        newer = snapshot(available_at=(AVAILABLE + timedelta(hours=1)).isoformat(),
                         expires_at=(AVAILABLE + timedelta(hours=2)).isoformat(),
                         features={"earnings_revision": 0.4, "news_sentiment": 0.9})
        context = PointInTimeContext(NAMES, [newer, snapshot()])
        self.assertEqual(context.lookup(AVAILABLE)["values"], [-0.2, 0.0])
        self.assertEqual(context.lookup(AVAILABLE + timedelta(hours=1))["values"], [0.4, 0.9])
        self.assertTrue(context.lookup(AVAILABLE + timedelta(hours=2, microseconds=-1))["available"])
        self.assertFalse(context.lookup(AVAILABLE + timedelta(hours=2))["available"])
        self.assertFalse(context.lookup(AVAILABLE + timedelta(days=3))["available"])

    def test_timezone_conversion_same_instant_and_duplicate_vintages(self):
        eastern = timezone(timedelta(hours=-5))
        row = snapshot(available_at=AVAILABLE.astimezone(eastern).isoformat())
        context = PointInTimeContext(NAMES, [row])
        result = context.lookup(AVAILABLE.astimezone(timezone(timedelta(hours=9))))
        self.assertTrue(result["available"])
        self.assertEqual(result["available_at"], AVAILABLE.isoformat())
        with self.assertRaisesRegex(ValueError, "unique"):
            PointInTimeContext(NAMES, [snapshot(), row])
        self.assertTrue(PointInTimeContext(NAMES, [snapshot(
            available_at="2026-01-05T15:00:00Z")]).lookup(AVAILABLE)["available"])

    def test_empty_or_unpopulated_context_is_explicitly_missing(self):
        self.assertEqual(PointInTimeContext((), []).lookup(AVAILABLE)["values"], [])
        self.assertFalse(PointInTimeContext((), []).lookup(AVAILABLE)["available"])
        self.assertFalse(PointInTimeContext(NAMES, []).lookup(AVAILABLE)["available"])
        with self.assertRaises(ValueError):
            PointInTimeContext((), [snapshot(features={})])

    def test_input_and_lookup_mutation_cannot_change_stored_data(self):
        rows = [snapshot()]
        original = deepcopy(rows)
        context = PointInTimeContext(NAMES, rows)
        self.assertEqual(rows, original)
        rows[0]["features"]["earnings_revision"] = 99
        rows[0]["source"] = "modified"
        rows[0]["expires_at"] = AVAILABLE.isoformat()
        rows.clear()
        result = context.lookup(AVAILABLE)
        result["values"][0] = 55
        result["source"] = "also modified"
        self.assertEqual(context.lookup(AVAILABLE)["values"], [-0.2, 0.0])
        self.assertEqual(context.lookup(AVAILABLE)["source"], "fixture:timestamped-release")
        with self.assertRaises(AttributeError):
            context.feature_names = ("replacement",)

    def test_provenance_document_is_sorted_normalized_detached_and_round_trips(self):
        newer = snapshot(snapshot_id="second", available_at="2026-01-05T11:00:00-05:00")
        context = PointInTimeContext(NAMES, [newer, snapshot(snapshot_id="first")])
        output = context.to_dict()
        self.assertEqual([row["snapshot_id"] for row in output["snapshots"]], ["first", "second"])
        self.assertEqual(output["snapshots"][1]["available_at"], "2026-01-05T16:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps(output), encoding="utf-8")
            restored = PointInTimeContext.from_path(path)
            path.write_text(json.dumps(document()), encoding="utf-8")
            self.assertEqual(restored.to_dict(), context.to_dict())
        output["snapshots"][0]["features"]["earnings_revision"] = 999
        output["snapshots"][1]["snapshot_id"] = "changed"
        output["feature_names"].clear()
        self.assertEqual(context.lookup(AVAILABLE)["values"], [-0.2, 0.0])
        self.assertEqual(context.to_dict()["snapshots"][1]["snapshot_id"], "second")

    def test_schema_rejects_unknown_missing_wrong_symbol_and_invalid_source(self):
        rows = [snapshot(extra=True), snapshot(symbol="SPY"), snapshot(source=" "),
                snapshot(source=None), snapshot(snapshot_id=12), snapshot(features=[]),
                snapshot(features={"earnings_revision": 1}),
                snapshot(features={"earnings_revision": 1, "news_sentiment": 0, "extra": 2})]
        for name in snapshot():
            row = snapshot()
            del row[name]
            rows.append(row)
        for row in rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                PointInTimeContext(NAMES, [row])
        self.assertTrue(PointInTimeContext(NAMES, [snapshot(snapshot_id="vintage-1")])
                        .lookup(AVAILABLE)["available"])
        for value in (None, {}, "rows", (snapshot(),)):
            with self.subTest(snapshots=value), self.assertRaises(ValueError):
                PointInTimeContext(NAMES, value)
        with self.assertRaises(ValueError):
            PointInTimeContext(NAMES, [None])

    def test_feature_schema_and_finite_values(self):
        bad_names = [list(NAMES), ("same", "same"), ("",), (None,), ("contains space",),
                     ("x" * 65,), ("é",), tuple(f"f{i}" for i in range(MAX_CONTEXT_FEATURES + 1))]
        for names in bad_names:
            with self.subTest(names=names), self.assertRaises(ValueError):
                PointInTimeContext(names, [])
        for value in (True, False, "0.1", None, float("nan"), float("inf"), float("-inf"), 10 ** 400):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PointInTimeContext(NAMES, [snapshot(features={
                    "earnings_revision": value, "news_sentiment": 1})])

    def test_timestamp_validation_and_ordering(self):
        for name in ("published_at", "available_at", "expires_at"):
            for value in (-1, 0, float("nan"), True, None, "2026-01-05", "2026-01-05T15:00:00", "invalid"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    PointInTimeContext(NAMES, [snapshot(**{name: value})])
        for changes in (
            {"published_at": (AVAILABLE + timedelta(seconds=1)).isoformat()},
            {"expires_at": AVAILABLE.isoformat()},
            {"expires_at": (AVAILABLE - timedelta(seconds=1)).isoformat()},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PointInTimeContext(NAMES, [snapshot(**changes)])
        self.assertTrue(PointInTimeContext(NAMES, [snapshot(published_at=AVAILABLE.isoformat())])
                        .lookup(AVAILABLE)["available"])
        context = PointInTimeContext(NAMES, [])
        for value in (None, "2026-01-05T15:00:00Z", -1, datetime(2026, 1, 5)):
            with self.subTest(lookup=value), self.assertRaises(ValueError):
                context.lookup(value)

    def test_document_loading_and_exact_version_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps(document()), encoding="utf-8")
            context = PointInTimeContext.from_path(path)
            self.assertEqual(context.feature_names, NAMES)
            self.assertTrue(context.lookup(AVAILABLE)["available"])
            invalid = [document(schema_version=True), document(schema_version=1.0),
                       document(schema_version=2), document(symbol="SPY"), document(extra=1),
                       document(feature_names="news_sentiment"), [], {}]
            for value in invalid:
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    PointInTimeContext.from_path(path)
            for text in ('{"symbol":"QQQ","symbol":"SPY"}',
                         json.dumps(document()).replace('"news_sentiment": 0', '"news_sentiment": NaN')):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ValueError):
                    PointInTimeContext.from_path(path)


if __name__ == "__main__":
    unittest.main()
