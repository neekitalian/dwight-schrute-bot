"""Native-paper milestone CLI routing; no account, market-data or mail calls."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.__main__ import main


class ManualCampaignCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.campaign = self.root / 'campaign'

    def invoke(self, *arguments, exit_code=None):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['dwight', *map(str, arguments)]), \
                patch('dwight.__main__.load_env') as environment, \
                redirect_stdout(out), redirect_stderr(err):
            if exit_code is None:
                main()
            else:
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, exit_code)
            environment.assert_not_called()
        return out.getvalue(), err.getvalue()

    def test_initialize_preserves_decimal_and_repeated_recipient_arguments(self):
        signals, journal = self.root / 'signals', self.root / 'journal.sqlite3'
        with patch('dwight.manual_campaign.initialize', return_value={
                'status': 'prepared', 'started_at': None, 'sends_email': False}) as initialize:
            out, err = self.invoke('manual-campaign-init', self.campaign,
                                   '--signals', signals, '--state', journal,
                                   '--recipient', 'first@example.com', '--recipient', 'second@example.com',
                                   '--allocation', '1234.56000001')
        initialize.assert_called_once_with(self.campaign, signals, journal,
                                           ['first@example.com', 'second@example.com'], '1234.56000001')
        self.assertIsNone(json.loads(out)['started_at'])
        self.assertEqual(err, '')

    def test_campaign_commands_reach_the_campaign_api_before_generic_manual_handler(self):
        evidence = self.root / 'account-evidence.json'
        cases = [
            ('record_account', ['manual-campaign-account', evidence], (self.campaign, evidence)),
            ('start', ['manual-campaign-start', 'snapshot-1'], (self.campaign, 'snapshot-1')),
            ('status', ['manual-campaign-status'], (self.campaign,)),
            ('report_due', ['manual-campaign-report-due'], (self.campaign,)),
            ('report', ['manual-campaign-report', '--hours', '24'], (self.campaign, 24)),
            ('claim_delivery', ['manual-campaign-claim-mail', '--hours', '48',
                                '--recipient', 'first@example.com'],
             (self.campaign, 48, 'first@example.com')),
            ('confirm_delivery', ['manual-campaign-confirm-mail', '--hours', '168',
                                  '--recipient', 'first@example.com', '--claim', 'claim-1',
                                  '--receipt', 'provider-message-1'],
             (self.campaign, 168, 'first@example.com', 'claim-1', 'provider-message-1')),
        ]
        for function, arguments, expected in cases:
            with self.subTest(function=function), \
                    patch('dwight.manual_campaign.' + function,
                          return_value={'submits_orders': False, 'sends_email': False}) as api, \
                    patch('dwight.manual.ManualPaperJournal') as generic_journal:
                out, err = self.invoke(*arguments, '--campaign', self.campaign)
                api.assert_called_once_with(*expected)
                generic_journal.assert_not_called()
                self.assertFalse(json.loads(out)['sends_email'])
                self.assertEqual(err, '')

    def test_invalid_milestone_never_invokes_api(self):
        with patch('dwight.manual_campaign.report') as report:
            _, err = self.invoke('manual-campaign-report', '--campaign', self.campaign,
                                 '--hours', '36', exit_code=2)
        report.assert_not_called()
        self.assertIn('invalid choice', err)

    def test_missing_allocation_never_initializes_or_starts_clock(self):
        with patch('dwight.manual_campaign.initialize') as initialize:
            _, err = self.invoke('manual-campaign-init', self.campaign,
                                 '--signals', self.root / 'signals', '--state', self.root / 'journal.sqlite3',
                                 '--recipient', 'first@example.com', exit_code=2)
        initialize.assert_not_called()
        self.assertIn('--allocation', err)

    def test_api_validation_error_is_nonzero(self):
        with patch('dwight.manual_campaign.start', side_effect=ValueError('fresh observation required')):
            out, err = self.invoke('manual-campaign-start', 'snapshot-1',
                                 '--campaign', self.campaign, exit_code=2)
        self.assertEqual(out, '')
        self.assertIn('fresh observation required', err)


if __name__ == '__main__':
    unittest.main()
