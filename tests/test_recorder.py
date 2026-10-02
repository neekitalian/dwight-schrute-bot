from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.recorder import record_books


class RecorderTests(unittest.TestCase):
    def test_oversized_or_malformed_discovery_cannot_issue_book_requests(self):
        for response in ([{}, {}], ["bad"], {"markets": []}):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as temp, \
                    patch("dwight.recorder.get_json", return_value=response) as request:
                with self.assertRaisesRegex(ValueError, "discovery response"):
                    record_books(temp, limit=1)
                self.assertEqual(request.call_count, 1)
                self.assertFalse(list(Path(temp).glob("*/manifest.json")))

    def test_public_book_includes_received_timestamp_and_token_match(self):
        discovery = [{"id": "market", "conditionId": "condition", "clobTokenIds": '["123"]'}]
        book = {"asset_id": "123", "bids": [], "asks": []}
        with tempfile.TemporaryDirectory() as temp, patch("dwight.recorder.get_json", side_effect=[discovery, book]):
            manifest = record_books(temp, limit=1)
            row = json.loads((Path(manifest["directory"]) / "books.jsonl").read_text())
            self.assertGreaterEqual(datetime.fromisoformat(row["received_at"]),
                                    datetime.fromisoformat(row["observed_at"]))
            self.assertEqual(manifest["recorded"], 1)
            self.assertEqual(manifest["errors"], 0)
            self.assertEqual(row["token_id"], "123")

    def test_wrong_token_book_is_recorded_as_error(self):
        discovery = [{"id": "market", "clobTokenIds": ["123"]}]
        with tempfile.TemporaryDirectory() as temp, patch("dwight.recorder.get_json", side_effect=[
                discovery, {"asset_id": "999", "bids": [], "asks": []}]):
            manifest = record_books(temp, limit=1)
            self.assertEqual(manifest["recorded"], 0)
            self.assertEqual(manifest["errors"], 1)


if __name__ == "__main__":
    unittest.main()
