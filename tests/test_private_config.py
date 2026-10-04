import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.private_config import (DATA_KEY_NAMES, MAX_ENV_BYTES, NEWS_CONFIG_NAMES,
                                  prepare_data_keys, prepare_news_config)


class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS's temporary directory can traverse /var, a symlink to
        # /private/var. Use its real path, as the entry point requires.
        self.parent = Path(temporary.name).resolve()
        self.path = self.parent / ".env"

    def test_new_file_has_empty_fields_and_no_secret_return_or_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = prepare_data_keys(self.path)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["file"], str(self.path))
        self.assertEqual(result["added_fields"], list(DATA_KEY_NAMES))
        self.assertFalse(result["values_reported"])
        self.assertFalse(result["network_checked"])
        self.assertFalse(result["submits_orders"])
        entries = [line for line in self.path.read_text().splitlines()
                   if line and not line.startswith("#")]
        self.assertEqual(entries, [name + "=" for name in DATA_KEY_NAMES])

    def test_existing_private_bytes_are_preserved_and_only_missing_fields_added(self):
        sentinel = "fake-local-secret-that-must-not-be-returned"
        original = ("# Keep original comments and CRLF\r\n"
                    f'APCA_API_KEY_ID="{sentinel}"\r\n'
                    f"APCA_API_SECRET_KEY={sentinel}\r\n"
                    "DWIGHT_DATA_FEED=sip").encode()
        self.path.write_bytes(original)
        result = prepare_data_keys(str(self.path))
        written = self.path.read_bytes()
        self.assertTrue(written.startswith(original + b"\n"))
        self.assertEqual(result["added_fields"], ["DATABENTO_API_KEY", "MASSIVE_API_KEY"])
        self.assertEqual(written.count(b"APCA_API_KEY_ID="), 1)
        self.assertEqual(written.count(b"DATABENTO_API_KEY=\n"), 1)
        self.assertEqual(written.count(b"MASSIVE_API_KEY=\n"), 1)
        self.assertNotIn(sentinel, json.dumps(result))

    def test_preparation_is_idempotent_and_preserves_populated_provider_keys(self):
        prepare_data_keys(self.path)
        original = self.path.read_bytes().replace(
            b"DATABENTO_API_KEY=\n", b"DATABENTO_API_KEY=fake-databento-value\n"
        ).replace(b"MASSIVE_API_KEY=\n", b"MASSIVE_API_KEY=fake-massive-value\n")
        self.path.write_bytes(original)
        result = prepare_data_keys(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(result["added_fields"], [])
        self.assertNotIn("fake-databento-value", json.dumps(result))
        self.assertNotIn("fake-massive-value", json.dumps(result))

    @unittest.skipUnless(os.name == "posix", "POSIX permission validation")
    def test_new_and_existing_files_are_private_even_with_permissive_umask(self):
        previous_umask = os.umask(0)
        try:
            prepare_data_keys(self.path)
        finally:
            os.umask(previous_umask)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.chmod(0o666)
        prepare_data_keys(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_does_not_load_private_values_or_modify_process_environment(self):
        self.path.write_text("DATABENTO_API_KEY=file-only-secret\n")
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "existing-environment-value"}):
            before = dict(os.environ)
            result = prepare_data_keys(self.path)
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn("file-only-secret", json.dumps(result))
            self.assertNotIn("existing-environment-value", json.dumps(result))

    def test_target_symlink_is_rejected_without_touching_destination(self):
        destination = self.parent / "private-source"
        original = b"DATABENTO_API_KEY=fake-private-secret\n"
        destination.write_bytes(original)
        self.path.symlink_to(destination)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            prepare_data_keys(self.path)
        self.assertEqual(destination.read_bytes(), original)
        self.assertTrue(self.path.is_symlink())

    def test_parent_symlink_and_missing_parent_are_rejected(self):
        alias = self.parent / "alias"
        alias.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            prepare_data_keys(alias / ".env")
        self.assertFalse(self.path.exists())
        missing = self.parent / "not-created" / ".env"
        with self.assertRaisesRegex(ValueError, "parent must already exist"):
            prepare_data_keys(missing)
        self.assertFalse(missing.parent.exists())

    def test_duplicate_entries_reject_without_printing_values_or_changing_bytes(self):
        originals = [
            b"DATABENTO_API_KEY=fake-first-secret\nDATABENTO_API_KEY=fake-second-secret\n",
            b"CUSTOM_KEY=fake-first-secret\n  CUSTOM_KEY =fake-second-secret\n",
        ]
        for original in originals:
            with self.subTest(original=original):
                self.path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "Duplicate key entries") as caught:
                    prepare_data_keys(self.path)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertNotIn("fake-first-secret", str(caught.exception))
                self.assertNotIn("fake-second-secret", str(caught.exception))

    def test_malformed_entries_reject_without_changing_bytes(self):
        originals = [
            b"export DATABENTO_API_KEY=fake-private-secret\n",
            b"MASSIVE_API_KEY fake-private-secret\n",
            b"1INVALID=fake-private-secret\n",
            b"BAD-KEY=fake-private-secret\n",
            b"=fake-private-secret\n",
        ]
        for original in originals:
            with self.subTest(original=original):
                self.path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "simple KEY=value") as caught:
                    prepare_data_keys(self.path)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertNotIn("fake-private-secret", str(caught.exception))

    def test_invalid_utf8_is_rejected_without_changing_bytes(self):
        original = b"DATABENTO_API_KEY=\xff\n"
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            prepare_data_keys(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_oversized_file_is_rejected_without_changing_bytes(self):
        original = b"#" + b"a" * MAX_ENV_BYTES
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "no larger than 64 KiB"):
            prepare_data_keys(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_append_cannot_exceed_size_limit(self):
        original = b"#" + b"a" * (MAX_ENV_BYTES - 1)
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "would exceed its size limit"):
            prepare_data_keys(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_news_preparation_preserves_market_keys_and_adds_no_values(self):
        original = b"MASSIVE_API_KEY=fake-market-key\nCUSTOM_SETTING=keep\n"
        self.path.write_bytes(original)
        result = prepare_news_config(self.path)
        self.assertTrue(self.path.read_bytes().startswith(original))
        self.assertEqual(result["added_fields"], list(NEWS_CONFIG_NAMES))
        for name in NEWS_CONFIG_NAMES:
            self.assertIn(name + "=\n", self.path.read_text())
        self.assertNotIn("fake-market-key", json.dumps(result))
        self.assertFalse(result["network_checked"])
        self.assertFalse(result["submits_orders"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_news_preparation_preserves_existing_private_addresses_and_key(self):
        original = (b"BENZINGA_RELAY_REST=https://private.example/v1/news\n"
                    b"BENZINGA_RELAY_WS=wss://private.example/v1/news/ws\n"
                    b"BENZINGA_RELAY_KEY=fake-relay-key\n")
        self.path.write_bytes(original)
        result = prepare_news_config(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(result["added_fields"], [])
        self.assertNotIn("private.example", json.dumps(result))
        self.assertNotIn("fake-relay-key", json.dumps(result))

    def test_news_preparation_refuses_ambiguous_and_symlink_files(self):
        original = b"BENZINGA_RELAY_KEY=fake-a\nBENZINGA_RELAY_KEY=fake-b\n"
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "Duplicate key entries"):
            prepare_news_config(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        alias = self.parent / "alias.env"
        alias.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            prepare_news_config(alias)
        self.assertEqual(self.path.read_bytes(), original)

    def test_news_cli_does_not_load_environment_or_connect(self):
        from dwight.__main__ import main
        output = io.StringIO()
        with patch("sys.argv", ["dwight", "prepare-news-config", "--file", str(self.path)]), \
                patch("dwight.__main__.load_env") as load, \
                patch("socket.create_connection") as connect, \
                contextlib.redirect_stdout(output):
            main()
        load.assert_not_called()
        connect.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["added_fields"], list(NEWS_CONFIG_NAMES))
        self.assertFalse(result["values_reported"])


if __name__ == "__main__":
    unittest.main()
