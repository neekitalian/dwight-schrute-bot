"""CLI boundaries for read-only observations and explicit manual proposals."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.__main__ import main


class ManualSignalCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def invoke(self, *args, exit_code=None):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['dwight', *map(str, args)]), \
                patch('dwight.__main__.load_env'), redirect_stdout(out), redirect_stderr(err):
            if exit_code is None:
                main()
            else:
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, exit_code)
        return out.getvalue(), err.getvalue()

    def store(self):
        mocked = patch('dwight.manual_signals.ManualSignalStore')
        cls = mocked.start()
        self.addCleanup(mocked.stop)
        store = cls.return_value.__enter__.return_value
        store.state_dir = self.root
        return cls, store

    def test_observation_logs_counts_not_prices_or_features(self):
        cls, store = self.store()
        store.observe.return_value = {
            'status': 'observed', 'signals': [{'stop': 99, 'features': {'private': 42}}],
            'provider_response': 'private', 'observed_at': 'test-clock',
        }
        out, err = self.invoke('manual-observe', '--signals', self.root, '--feed', 'sip', '--once')
        result = json.loads(out)
        self.assertEqual(result['signal_count'], 1)
        self.assertFalse(result['submits_orders'])
        self.assertNotIn('private', out)
        self.assertNotIn('stop', out)
        self.assertEqual(err, '')
        cls.assert_called_once_with(self.root, feed='sip')

    def test_provider_error_is_sanitized_and_nonzero(self):
        _, store = self.store()
        store.observe.side_effect = RuntimeError('secret-provider-response')
        out, err = self.invoke('manual-observe', '--feed', 'iex', '--once', exit_code=1)
        self.assertEqual(json.loads(out)['status'], 'error_abstain')
        self.assertNotIn('secret-provider-response', out + err)

    def test_stop_marker_prevents_network_observation(self):
        _, store = self.store()
        (self.root / 'STOP').touch()
        out, _ = self.invoke('manual-observe', '--feed', 'sip', '--once')
        self.assertEqual(json.loads(out)['status'], 'stopped')
        store.observe.assert_not_called()

    def test_returned_provider_error_keeps_safe_type_and_revision_halt_is_terminal(self):
        _, store = self.store()
        store.observe.return_value = {'status': 'error_abstain', 'error_type': 'TimeoutError'}
        out, _ = self.invoke('manual-observe', '--feed', 'sip', '--once', exit_code=1)
        self.assertEqual(json.loads(out)['error_type'], 'TimeoutError')
        store.observe.return_value = {'status': 'data_revision_requires_review', 'signals': []}
        with patch('dwight.__main__.time.sleep') as sleep:
            out, _ = self.invoke('manual-observe', '--feed', 'sip', exit_code=3)
        sleep.assert_not_called()
        self.assertEqual(json.loads(out)['status'], 'data_revision_requires_review')

    def test_continuous_polling_stops_on_marker_and_logs_no_signal_terms(self):
        _, store = self.store()
        store.observe.return_value = {'status': 'observed', 'signals': [{'stop': 99}]}
        with patch('dwight.__main__.time.sleep', side_effect=lambda _: (self.root / 'STOP').touch()) as sleep:
            out, _ = self.invoke('manual-observe', '--feed', 'sip')
        store.observe.assert_called_once()
        sleep.assert_called_once_with(30)
        self.assertIn('"status": "stopped"', out)
        self.assertNotIn('"stop":', out)

    def test_manual_reference_is_explicit_and_passed_without_float_conversion(self):
        _, store = self.store()
        store.prepare.return_value = {'submits_orders': False, 'account_verified': False}
        journal = self.root / 'manual.sqlite3'
        out, _ = self.invoke('manual-prepare', 'signal-1', '--feed', 'sip',
                             '--entry', '430.78', '--quantity', '2',
                             '--price-observed-at', '2026-10-05T15:00:00+00:00', '--state', journal)
        store.prepare.assert_called_once_with(
            'signal-1', entry='430.78', quantity='2',
            price_observed_at='2026-10-05T15:00:00+00:00', journal_path=journal)
        self.assertFalse(json.loads(out)['submits_orders'])

    def test_missing_human_price_reference_never_opens_store(self):
        cls, _ = self.store()
        _, err = self.invoke('manual-prepare', 'signal-1', '--feed', 'sip', exit_code=2)
        self.assertIn('--price-observed-at', err)
        cls.assert_not_called()

    def test_inspection_has_no_account_verification_claim(self):
        _, store = self.store()
        store.list_signals.return_value = [{'signal_id': 'test', 'eligible': False}]
        out, _ = self.invoke('manual-signals', '--feed', 'sip', '--limit', 5)
        result = json.loads(out)
        self.assertFalse(result['account_verified'])
        self.assertFalse(result['portfolio_gates_applied'])
        store.list_signals.assert_called_once_with(limit=5)
