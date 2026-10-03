"""Connection profiles and the public UI never grant account access."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.connection_catalog import PLATFORMS, build_profile
from deploy.huggingface import platforms


class PlatformConnectionTests(unittest.TestCase):
    def test_profiles_have_no_credentials_and_accept_only_fixed_choices(self):
        self.assertEqual(set(PLATFORMS), {'tradingview', 'alpaca', 'ibkr', 'schwab', 'coinbase', 'binance', 'kraken', 'polymarket', 'databento', 'massive'})
        for platform in PLATFORMS:
            for feed in ('sip', 'iex'):
                profile = build_profile(platform, feed)
                self.assertIs(profile['execution_enabled'], False)
                self.assertEqual(profile['mode'], 'research_read_only')
                self.assertNotIn('url', profile)
                self.assertEqual(profile['credential_env_names'], {
                    'alpaca': ['APCA_API_KEY_ID', 'APCA_API_SECRET_KEY'],
                    'databento': ['DATABENTO_API_KEY'], 'massive': ['MASSIVE_API_KEY'],
                }.get(platform, []))
                json.dumps(profile, allow_nan=False)
        for platform in (None, [], {}, '../private', 'https://example.invalid', 'ALPACA'):
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                build_profile(platform)
        for feed in (None, [], 'custom', 'SIP'):
            with self.assertRaises(ValueError):
                build_profile('alpaca', feed)

    def test_public_api_cannot_dispatch_any_private_platform(self):
        with patch.object(platforms, 'check_connection') as check:
            for platform in ('alpaca', 'tradingview', 'ibkr', 'schwab', 'databento', 'massive'):
                result = platforms.public_check(platform)
                self.assertFalse(result['capabilities']['account_read'])
                self.assertFalse(result['capabilities']['order_execution'])
            check.assert_not_called()
            for platform in platforms.PUBLIC_PLATFORMS:
                platforms.public_check(platform)
                check.assert_called_with(platform, environ={})

    def test_setup_download_round_trip_and_invalid_inputs(self):
        for platform in PLATFORMS:
            instructions, profile, download = platforms.setup(platform, 'iex')
            self.assertEqual(json.loads(profile), build_profile(platform, 'iex'))
            self.assertEqual(json.loads(Path(download).read_text()), json.loads(profile))
            self.assertIn('dwight connection-check --profile', instructions)
        with patch.object(platforms, 'check_connection') as check:
            for value in ('/etc/passwd', 'APCA_API_KEY_ID=secret', '<script>alert(1)</script>', ''):
                with self.assertRaises(ValueError):
                    platforms.public_check(value)
                with self.assertRaises(ValueError):
                    platforms.setup(value)
            check.assert_not_called()

    def cli(self, *arguments):
        from dwight.__main__ import main
        output = io.StringIO()
        with patch('sys.argv', ['dwight', *arguments]), contextlib.redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_exported_profile_drives_same_check_without_loading_public_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'coinbase.json'
            with patch('dwight.__main__.load_env') as load_env, \
                 patch('dwight.connection_checks.check_connection', return_value={'status': 'public_data_available'}) as check:
                self.cli('connection-profile', 'coinbase', '--output', str(path))
                result = self.cli('connection-check', '--profile', str(path))
                self.assertEqual(result['status'], 'public_data_available')
                check.assert_called_once_with('coinbase', feed='sip')
                load_env.assert_not_called()

    def test_tampered_profile_rejected_before_network_or_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'profile.json'
            for field, value in [('execution_enabled', True), ('url', 'http://127.0.0.1'),
                                 ('credential_env_names', ['CUSTOM_SECRET'])]:
                profile = build_profile('alpaca')
                profile[field] = value
                path.write_text(json.dumps(profile))
                with patch('dwight.connection_checks.check_connection') as check, \
                     patch('dwight.__main__.load_env') as load_env, \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.cli('connection-check', '--profile', str(path))
                check.assert_not_called()
                load_env.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec('gradio'), 'Runs in Space environment')
    def test_public_callbacks_have_only_fixed_selectors_and_no_credential_input(self):
        from deploy.huggingface.app import build_app
        app = build_app()
        try:
            config = app.get_config_file()
            components = {c['id']: c for c in config['components']}
            dependencies = {d.get('api_name'): d for d in config['dependencies']}
            for name, count in [('platform_setup', 2), ('public_connection_check', 1)]:
                inputs = dependencies[name]['inputs']
                self.assertEqual(len(inputs), count)
                self.assertTrue(all(components[i]['type'] == 'dropdown' for i in inputs))
            self.assertIn('downloadbutton', [c['type'] for c in config['components']])
            self.assertNotIn('file', [c['type'] for c in config['components']])
        finally:
            app.close()

    def test_private_provider_profile_loads_local_environment_only_at_check(self):
        for platform in ('databento', 'massive'):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'profile.json'
                with patch('dwight.__main__.load_env') as load_env, \
                     patch('dwight.connection_checks.check_connection', return_value={'status': 'checked'}) as check:
                    self.cli('connection-profile', platform, '--output', str(path))
                    load_env.assert_not_called()
                    self.cli('connection-check', '--profile', str(path))
                    load_env.assert_called_once_with()
                    check.assert_called_once_with(platform, feed='sip')

    def test_history_cli_routes_without_including_key_arguments(self):
        with patch('dwight.__main__.load_env') as load_env, \
             patch('dwight.vendor_history.estimate_history', return_value={'retrieval_started': False}) as estimate:
            result = self.cli('history-estimate', '--start', '2025-11-28', '--end', '2025-11-28', '--dataset', 'XNAS.ITCH')
            self.assertFalse(result['retrieval_started'])
            estimate.assert_called_once_with('2025-11-28', '2025-11-28', dataset='XNAS.ITCH')
            load_env.assert_called_once_with()
        with patch('dwight.__main__.load_env'), \
             patch('dwight.vendor_history.download_history', return_value={'source': 'databento'}) as download:
            self.cli('download-history', '--provider', 'databento', '--start', '2025-11-28', '--end', '2025-11-28',
                     '--dataset', 'XNAS.ITCH', '--max-cost-usd', '0.50')
            download.assert_called_once_with('databento', Path('private-data'), '2025-11-28', '2025-11-28',
                                             dataset='XNAS.ITCH', max_cost_usd='0.50')
