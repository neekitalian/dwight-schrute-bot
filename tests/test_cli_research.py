"""Offline CLI integration: explicit evidence modes and private report output."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.__main__ import main
from dwight.manual import ACCOUNT, ManualPaperJournal


class ResearchCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'private' / 'account.sqlite3'
        self.fixture = Path(__file__).resolve().parents[1] / 'examples' / 'manual-fills.csv'

    def invoke(self, *args, error=None):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['dwight', *map(str, args)]), patch('dwight.__main__.load_env'), redirect_stdout(out), redirect_stderr(err):
            if error is None:
                main()
            else:
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(error, err.getvalue())
                self.assertEqual(out.getvalue(), '')
                return err.getvalue()
        self.assertEqual(err.getvalue(), '')
        return json.loads(out.getvalue())

    def write_proposal(self):
        now = datetime.now(timezone.utc)
        payload = {'proposal_id': 'QQQ-CLI-DEMO-1', 'account': ACCOUNT,
                   'symbol': 'QQQ', 'side': 'buy', 'quantity': '2', 'entry': '100',
                   'stop': '99', 'target': '102', 'source': 'synthetic_cli_test',
                   'model': 'fixture', 'version': 'test-only',
                   'signal_at': now.isoformat(), 'available_at': now.isoformat(),
                   'expires_at': (now + timedelta(minutes=10)).isoformat()}
        path = self.root / 'proposal.json'
        path.write_text(json.dumps(payload))
        return path, payload

    def test_manual_proposal_review_confirmation_does_not_create_fills(self):
        path, payload = self.write_proposal()
        result = self.invoke('manual-propose', path, '--state', self.state)
        self.assertEqual(result['status'], 'pending')
        self.assertFalse(result['submits_orders'])
        listed = self.invoke('manual-list', '--state', self.state)
        self.assertEqual(listed['proposals'][0]['proposal_id'], payload['proposal_id'])
        confirmed = self.invoke('manual-status', payload['proposal_id'], 'confirmed', '--state', self.state)
        self.assertEqual(confirmed['status'], 'confirmed')
        report = self.invoke('manual-report', '--state', self.state)
        self.assertEqual(report['fills'], [])
        self.assertFalse(report['broker_verified'])
        self.assertIsNone(report['account_equity'])

    def test_manual_import_requires_mode_matching_csv_before_inserting(self):
        self.invoke('manual-import', self.fixture, '--state', self.state, error='required')
        self.invoke('manual-import', self.fixture, '--state', self.state, '--paper-export', error='does not match')
        self.assertEqual(ManualPaperJournal(self.state).report()['fills'], [])
        first = self.invoke('manual-import', self.fixture, '--state', self.state, '--synthetic')
        second = self.invoke('manual-import', self.fixture, '--state', self.state, '--synthetic')
        self.assertEqual(first['inserted'], 3)
        self.assertEqual(second['inserted'], 0)
        self.assertEqual(second['duplicates_skipped'], 3)
        report = self.invoke('manual-report', '--state', self.state)
        self.assertEqual(report['data_kind'], 'synthetic')
        self.assertEqual(report['net_realized_pnl'], '49.4')
        self.assertEqual(report['open_position']['quantity'], '2')

    def test_paper_export_mode_is_explicit_but_never_verified(self):
        path = self.root / 'claimed-export.csv'
        path.write_text(self.fixture.read_text().replace(',synthetic,', ',paper_export,'))
        self.invoke('manual-import', path, '--state', self.state, '--paper-export')
        report = self.invoke('manual-report', '--state', self.state)
        self.assertEqual(report['data_kind'], 'paper_export')
        self.assertFalse(report['broker_verified'])
        self.assertEqual(report['performance_scope'], 'imported_fills_only')

    def test_report_file_is_private_and_stdout_does_not_repeat_private_fills(self):
        self.invoke('manual-import', self.fixture, '--state', self.state, '--synthetic')
        output = self.root / 'reports' / 'manual.json'
        result = self.invoke('manual-report', '--state', self.state, '--output', output)
        self.assertEqual(result['output'], str(output))
        self.assertNotIn('fills', result)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(output.read_text())['net_realized_pnl'], '49.4')
        self.assertEqual(list(output.parent.glob('.dwight-report-*')), [])

    def test_report_refuses_existing_files_and_journal_path(self):
        self.invoke('manual-import', self.fixture, '--state', self.state, '--synthetic')
        output = self.root / 'existing.json'
        output.write_text('keep me')
        self.invoke('manual-report', '--state', self.state, '--output', output, error='File exists')
        self.assertEqual(output.read_text(), 'keep me')
        self.invoke('manual-report', '--state', self.state, '--output', self.state, error='File exists')
        self.assertEqual(len(ManualPaperJournal(self.state).report()['fills']), 3)
        self.assertEqual(list(self.root.rglob('.dwight-report-*')), [])

    def test_strict_proposal_json_rejects_duplicates_nan_and_wrong_shape(self):
        path, _ = self.write_proposal()
        for content, expected in [('[]', 'must contain an object'),
                                  ('{"quantity":1,"quantity":2}', 'duplicate'),
                                  ('{"quantity":NaN}', 'non-finite')]:
            with self.subTest(content=content):
                path.write_text(content)
                self.invoke('manual-propose', path, '--state', self.state, error=expected)
        self.assertEqual(ManualPaperJournal(self.state).list_proposals(), [])

    def test_walkforward_requires_explicit_real_or_synthetic_mode(self):
        with patch('dwight.walkforward.run_walkforward') as runner:
            self.invoke('walkforward', '--data', 'bars.csv', '--out', self.root, error='required')
            self.invoke('walkforward', '--data', 'bars.csv', '--out', self.root,
                        '--synthetic', '--real-data', error='not allowed')
            runner.assert_not_called()

    def test_enrichment_requires_evidence_mode_and_rejects_ambiguous_config(self):
        path = self.root / 'enrichment.json'
        path.write_text('{"max_windows":1,"max_windows":2}')
        with patch('dwight.enrichment.run_enrichment') as runner:
            self.invoke('enrichment', '--data', 'bars.csv', '--out', self.root, error='required')
            self.invoke('enrichment', '--data', 'bars.csv', '--out', self.root,
                        '--synthetic', '--real-data', error='not allowed')
            self.invoke('enrichment', '--data', 'bars.csv', '--out', self.root,
                        '--real-data', '--config', path, error='duplicate')
            runner.assert_not_called()

    def test_enrichment_preserves_real_data_flag_without_promotion(self):
        result = {'status': 'insufficient_data', 'promotion_eligible': False}
        with patch('dwight.enrichment.run_enrichment', return_value=result) as runner:
            actual = self.invoke('enrichment', '--data', 'bars.csv', '--out', self.root, '--real-data')
            runner.assert_called_once_with(Path('bars.csv'), self.root, synthetic=False, config={})
        self.assertEqual(actual, result)

    def test_walkforward_dispatches_strict_config_and_synthetic_flag(self):
        path = self.root / 'walkforward.json'
        config = {'long_only': True, 'max_windows': 2, 'holdout_sessions': 40}
        path.write_text(json.dumps(config))
        result = {'status': 'completed_synthetic_smoke', 'synthetic': True,
                  'final_holdout': {'consumed': False}, 'promotion_eligible': False}
        with patch('dwight.walkforward.run_walkforward', return_value=result) as runner:
            actual = self.invoke('walkforward', '--data', 'bars.csv', '--out', self.root,
                                 '--synthetic', '--config', path)
            runner.assert_called_once_with(Path('bars.csv'), self.root, synthetic=True, config=config)
        self.assertEqual(actual, result)

    def test_walkforward_real_flag_does_not_imply_source_verification(self):
        result = {'status': 'insufficient_data', 'synthetic': False, 'promotion_eligible': False,
                  'source': 'unverified_csv'}
        with patch('dwight.walkforward.run_walkforward', return_value=result) as runner:
            actual = self.invoke('walkforward', '--data', 'bars.csv', '--out', self.root, '--real-data')
            runner.assert_called_once_with(Path('bars.csv'), self.root, synthetic=False, config={})
        self.assertFalse(actual['promotion_eligible'])
        self.assertEqual(actual['source'], 'unverified_csv')

    def test_real_walkforward_rejects_reduced_samples_before_reading_data(self):
        path = self.root / 'invalid.json'
        path.write_text(json.dumps({'min_train_samples': 1}))
        self.invoke('walkforward', '--data', 'not-read.csv', '--out', self.root / 'runs',
                    '--real-data', '--config', path, error='only for synthetic')
        self.assertFalse((self.root / 'runs').exists())

    def test_walkforward_actual_insufficient_fixture_preserves_holdout(self):
        from examples.make_experiment_demo import generate
        data = self.root / 'fixture.csv'
        generate(data, days=10, seed=42)
        report = self.invoke('walkforward', '--data', data, '--out', self.root / 'runs', '--synthetic')
        self.assertTrue(report['synthetic'])
        self.assertFalse(report['promotion_eligible'])
        self.assertFalse(report['final_holdout']['consumed'])
        self.assertEqual(report['window_counts']['completed'], 0)
        self.assertEqual(report['aggregate'], {})
        self.assertTrue((Path(report['directory']) / 'report.json').is_file())

    def test_missing_input_returns_clean_cli_error(self):
        error = self.invoke('manual-propose', self.root / 'missing.json', '--state', self.state,
                            error='No such file')
        self.assertNotIn('Traceback', error)


if __name__ == '__main__':
    unittest.main()
