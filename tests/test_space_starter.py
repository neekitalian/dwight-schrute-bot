"""Public onboarding accepts a fixed route, never a credential or local path."""
import importlib.util
import unittest

from deploy.huggingface import starter


class StarterTests(unittest.TestCase):
    def test_supported_routes_are_static_and_unknown_inputs_rejected(self):
        for route in starter.CHOICES:
            self.assertIsInstance(starter.plan(route), str)
        for value in ('/etc/passwd', 'APCA_API_KEY_ID=secret', '<script>alert(1)</script>', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                starter.plan(value)

    @unittest.skipUnless(importlib.util.find_spec('gradio'), 'Runs in Space environment')
    def test_starter_is_first_tab_and_callback_has_only_the_fixed_choice(self):
        from deploy.huggingface.app import build_app
        app = build_app()
        try:
            config = app.get_config_file()
            tabs = [c['props']['label'] for c in config['components'] if c['type'] == 'tabitem']
            self.assertEqual(tabs[0], 'Start here')
            callback = next(d for d in config['dependencies'] if d.get('api_name') == 'starter_plan')
            self.assertEqual(len(callback['inputs']), 1)
            selector = next(c for c in config['components'] if c['id'] == callback['inputs'][0])
            self.assertEqual(selector['type'], 'dropdown')
            self.assertEqual([c[1] for c in selector['props']['choices']], list(starter.CHOICES))
            self.assertNotIn('file', [c['type'] for c in config['components']])
        finally:
            app.close()


if __name__ == '__main__':
    unittest.main()
