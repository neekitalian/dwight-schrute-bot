"""Public fixed walk-forward UI: actual evidence, no private-account surface."""
import ast
from copy import deepcopy
from datetime import datetime, timedelta
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.huggingface import walkforward_view as view
from dwight import walkforward as engine


HAS_GRADIO = importlib.util.find_spec("gradio") is not None


class SpaceWalkForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        view._cached_walkforward_json.cache_clear()
        cls.temporary_paths, cls.replayed_dates = [], set()
        original_temp, original_replay = tempfile.TemporaryDirectory, engine._replay

        def capture_directory(*args, **kwargs):
            directory = original_temp(*args, **kwargs)
            cls.temporary_paths.append(Path(directory.name))
            return directory

        def capture_replay(bars, *args, **kwargs):
            cls.replayed_dates.update(bar.timestamp.date().isoformat() for bar in bars)
            return original_replay(bars, *args, **kwargs)

        with patch.object(view, 'TemporaryDirectory', side_effect=capture_directory), patch.object(engine, '_replay', side_effect=capture_replay):
            cls.report = view.run_public_walkforward()

    def test_actual_fixed_experiment_has_three_complete_long_only_windows(self):
        report = self.report
        self.assertEqual(report['status'], 'completed_synthetic_smoke')
        self.assertEqual(report['window_counts'], {'planned': 3, 'completed': 3, 'insufficient': 0})
        self.assertEqual(report['complete_sessions'], 358)
        self.assertEqual(report['space_demo']['seed'], 42)
        self.assertEqual(report['space_demo']['calendar_days'], 500)
        self.assertTrue(report['walkforward_settings']['long_only'])
        self.assertEqual([len(w['train']) for w in report['windows']], [80, 120, 160])
        self.assertEqual([len(w['validation']) for w in report['windows']], [40, 40, 40])
        self.assertEqual([len(w['test']) for w in report['windows']], [40, 40, 40])
        self.assertFalse(report['promotion_eligible'])
        self.assertEqual(report['filtered_ahead_windows'], 1)

    def test_reserved_final_holdout_and_unused_dates_never_replayed(self):
        final = self.report['final_holdout']
        self.assertEqual(len(final['sessions']), 40)
        self.assertFalse(final['consumed'])
        self.assertIsNone(final['evaluation'])
        self.assertTrue(self.replayed_dates)
        self.assertTrue(set(final['sessions']).isdisjoint(self.replayed_dates))
        self.assertTrue(set(self.report['plan']['unused_development_sessions']).isdisjoint(self.replayed_dates))
        self.assertTrue(all(not w['selection_uses_test'] for w in self.report['windows']))
        seen = set()
        for window in self.report['windows']:
            self.assertTrue(seen.isdisjoint(window['test']))
            seen.update(window['test'])

    def test_paths_and_temporary_artifacts_are_not_exposed_or_retained(self):
        encoded = json.dumps(self.report, allow_nan=False)
        self.assertTrue(self.temporary_paths)
        for path in self.temporary_paths:
            self.assertFalse(path.exists())
            self.assertNotIn(str(path), encoded)
        self.assertNotIn('directory', self.report)
        self.assertNotIn('dataset_manifest', self.report['walkforward_settings'])
        self.assertTrue(all('directory' not in window for window in self.report['windows']))
        self.assertFalse(self.report['space_demo']['broker_connected'])
        self.assertFalse(self.report['space_demo']['private_data_read'])
        self.assertFalse(self.report['space_demo']['finrl_evaluated'])
        self.assertFalse(self.report['space_demo']['transformer_evaluated'])
        self.assertEqual(self.report['space_demo']['broker_orders_submitted'], 0)

    def test_cache_is_bounded_detached_and_does_not_retrain(self):
        changed = view.run_public_walkforward()
        changed['synthetic'] = False
        changed['windows'][0]['test_evaluation']['baseline']['net_pnl'] = 999999
        with patch.object(view, 'run_walkforward', side_effect=AssertionError('must use cache')):
            self.assertEqual(view.run_public_walkforward(), self.report)
        info = view._cached_walkforward_json.cache_info()
        self.assertEqual(info.maxsize, 1)
        self.assertEqual(info.currsize, 1)

    def test_public_boundary_rejects_real_promoted_or_consumed_holdout(self):
        for changes in ({'synthetic': False}, {'source': 'alpaca'}, {'symbol': 'SPY'}, {'promotion_eligible': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                view._public({**self.report, **changes})
        bad = deepcopy(self.report)
        bad['final_holdout']['consumed'] = True
        with self.assertRaisesRegex(ValueError, 'unscored'):
            view._public(bad)
        bad = deepcopy(self.report)
        bad['windows'][0]['test'].append(bad['final_holdout']['sessions'][0])
        with self.assertRaisesRegex(ValueError, 'Final holdout'):
            view._public(bad)

    def test_timeline_shows_actual_dates_counts_and_separate_reserved_data(self):
        figure = view.timeline_figure(self.report)
        self.assertEqual([trace.name for trace in figure.data], ['Train', 'Validate', 'Test', 'Unused dev.', 'Final holdout'])
        for trace, part in zip(figure.data[:3], ('train', 'validation', 'test')):
            for index, window in enumerate(self.report['windows']):
                dates = window[part]
                self.assertEqual(trace.base[index], dates[0])
                expected = (datetime.fromisoformat(dates[-1]) + timedelta(days=1) - datetime.fromisoformat(dates[0])).total_seconds() * 1000
                self.assertEqual(trace.x[index], expected)
                self.assertEqual(list(trace.customdata[index])[:3], [dates[0], dates[-1], len(dates)])
        final_trace = figure.data[-1]
        self.assertEqual(list(final_trace.y), ['Untouched data'])
        self.assertEqual(final_trace.customdata[0][2], 40)
        self.assertIn('not evaluated', final_trace.customdata[0][3])

    def test_pnl_bars_show_every_policy_per_window_without_fake_account_curve(self):
        figure = view.pnl_figure(self.report)
        self.assertEqual(len(figure.data), 3)
        for trace, variant in zip(figure.data, view.VARIANTS):
            self.assertEqual(trace.type, 'bar')
            self.assertEqual(list(trace.x), ['Window 1', 'Window 2', 'Window 3'])
            self.assertEqual(list(trace.y), [window['test_evaluation'][variant]['net_pnl'] for window in self.report['windows']])
        self.assertIn('Independent', figure.layout.xaxis.title.text)
        self.assertIn('Synthetic', figure.layout.title.text)
        self.assertEqual(figure.layout.paper_bgcolor, '#101318')
        self.assertTrue(figure.to_json())

    def test_insufficient_window_stays_visible_without_invented_zero_pnl(self):
        report = deepcopy(self.report)
        window = report['windows'][1]
        window.update(status='insufficient_test_samples', test_evaluation={}, blocking_reasons=['test needs more labels'])
        figure = view.pnl_figure(report)
        self.assertTrue(all(trace.y[1] is None for trace in figure.data))
        html = view.window_table(report)
        self.assertIn('insufficient_test_samples', html)
        self.assertIn('Not scored', html)
        self.assertIn('test needs more labels', html)
        self.assertIn(window['test'][0], html)

    def test_explanation_keeps_manual_account_and_finrl_boundaries_clear(self):
        text = view.WORKFLOW_NOTE
        self.assertIn('place\norders manually', text)
        self.assertIn('no order-submission', text)
        self.assertIn('not yet a tested native TradingView export adapter', text)
        self.assertIn('FinRL is not trained or evaluated', text)
        self.assertIn('logistic regression', text)
        note = view.evidence_note(self.report)
        self.assertIn('not a continuous account equity curve', note)
        self.assertIn('reduced', (view.INTRO + note).lower())
        self.assertIn('No result is eligible for deployment', note)

    def test_failed_generation_cleans_up(self):
        paths = []

        def fail(path, **kwargs):
            paths.append(path.parent)
            path.write_text('incomplete')
            raise ValueError('private test path must not leak')

        with patch.object(view, 'generate', side_effect=fail):
            with self.assertRaises(ValueError):
                view._cached_walkforward_json.__wrapped__()
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))

    @unittest.skipUnless(HAS_GRADIO, "Gradio UI checks run in the dedicated Space environment")
    def test_callback_returns_no_private_error(self):
        from deploy.huggingface.app import evaluate_walkforward
        with patch.object(view, 'run_public_walkforward', side_effect=ValueError('private secret')):
            with self.assertRaises(Exception) as error:
                evaluate_walkforward()
        self.assertNotIn('private secret', str(error.exception))
        self.assertIn('No account', str(error.exception))

    @unittest.skipUnless(HAS_GRADIO, "Gradio UI checks run in the dedicated Space environment")
    def test_public_tab_is_inputless_click_only_without_upload_or_order_surface(self):
        from deploy.huggingface.app import build_app
        # Constructing the page must not eagerly run this new experiment.
        with patch.object(view, 'run_public_walkforward', side_effect=AssertionError('no initial work')):
            app = build_app()
        try:
            config = app.get_config_file()
            component_types = {component['type'] for component in config['components']}
            self.assertTrue(component_types.isdisjoint({'file', 'uploadbutton', 'fileexplorer', 'textbox'}))
            callback = next(dep for dep in config['dependencies'] if dep['api_name'] == 'walkforward')
            self.assertEqual(callback['inputs'], [])
            self.assertTrue(all(event == 'click' for _, event in callback['targets']))
            self.assertEqual(len(callback['outputs']), 6)
            for dependency in config['dependencies']:
                if any(event == 'load' for _, event in dependency['targets']):
                    self.assertTrue(set(callback['outputs']).isdisjoint(dependency['outputs']))
            button = next(c for c in config['components'] if c['type'] == 'button' and c['props']['value'] == 'Evaluate walk-forward')
            self.assertEqual(callback['targets'][0][0], button['id'])
        finally:
            app.close()
        source = Path(view.__file__).read_text()
        imports = [node.module or '' for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
        self.assertTrue(all(not module.startswith(('dwight.paper', 'dwight.manual', 'requests', 'urllib')) for module in imports))


if __name__ == '__main__':
    unittest.main()
