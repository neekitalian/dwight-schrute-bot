"""Installed-command boundaries: local onboarding and observation only."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.__main__ import main


class ToolkitCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def invoke(self, *args):
        output = io.StringIO()
        with patch('sys.argv', ['dwight', *map(str, args)]), redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_onboarding_never_loads_unrelated_current_directory_secrets(self):
        unrelated = self.root / 'unrelated'
        unrelated.mkdir()
        (unrelated / '.env').write_text('DWIGHT_UNRELATED_TEST=must-not-be-loaded\n')
        original = Path.cwd()
        try:
            os.chdir(unrelated)
            with patch.dict(os.environ, {}, clear=True), patch('dwight.__main__.load_env') as loader:
                made = self.invoke('init-workspace', self.root / 'workspace')
                result = self.invoke('toolkit-status', self.root / 'workspace')
                loader.assert_not_called()
                self.assertNotIn('DWIGHT_UNRELATED_TEST', os.environ)
            self.assertFalse(made['submits_orders'])
            self.assertFalse(result['network_checked'])
            self.assertFalse(result['paper_account_connected'])
        finally:
            os.chdir(original)

    def test_inbox_read_does_not_load_credentials_or_create_trades(self):
        with patch('dwight.__main__.load_env') as loader:
            result = self.invoke('tradingview-list', '--state', self.root / 'inbox.sqlite3')
            loader.assert_not_called()
        self.assertEqual(result, {'mode': 'unreviewed_observations', 'submits_orders': False, 'events': []})

    def test_serve_missing_capability_rejects_before_database_creation(self):
        path = self.root / 'must-not-exist' / 'inbox.sqlite3'
        errors = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch('dwight.__main__.load_env'), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                self.invoke('tradingview-serve', '--state', path)
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(path.parent.exists())
        self.assertIn('DWIGHT_TRADINGVIEW_CAPABILITY', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
