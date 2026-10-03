"""Offline fixtures only: recovery never queries a broker or evaluates trades."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwight.data import exchange_sessions, sha256_file
from dwight.experiments import _provenance
from dwight.history_recovery import audit_failed_download, recover_history, GAP_POLICY

UTC = timezone.utc
ACQUIRED = datetime(2026, 1, 1, tzinfo=UTC)
IMPORTED = ACQUIRED + timedelta(days=1)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'original' / 'datasets' / 'attempt'
        (self.source / 'raw').mkdir(parents=True, mode=0o700)
        self.protocol = self.root / 'original' / 'protocol.json'
        self.sessions = exchange_sessions('2025-11-03', '2025-12-05')
        self.protocol.write_text(json.dumps({'schema_version': 1, 'symbol': 'QQQ', 'start': '2025-11-03',
                                            'end': '2025-12-05', 'feed': 'sip', 'adjustment': 'raw'}))
        (self.source / 'sessions.json').write_text(json.dumps([s.as_dict() for s in self.sessions]))
        self.rows = [{'t': (s.open + timedelta(minutes=i)).isoformat(),
                      'o': 100, 'h': 102, 'l': 99, 'c': 101, 'v': 10}
                     for s in self.sessions for i in range(int((s.close-s.open).total_seconds()//60))]
        self.missing = self.rows.pop(18)['t']
        self.write_pages()

    def write_pages(self):
        split = len(self.rows)//2
        self.payloads = [{'bars': {'QQQ': self.rows[:split]}, 'next_page_token': 'opaque-next'},
                         {'bars': {'QQQ': self.rows[split:]}, 'next_page_token': None}]
        self.save_payloads()

    def save_payloads(self):
        entries = []
        for number, payload in enumerate(self.payloads, 1):
            path = f'raw/page-{number:06d}.json'
            (self.source / path).write_text(json.dumps(payload))
            entries.append({'path': path, 'sha256': sha256_file(self.source/path),
                            'retrieved_at': (ACQUIRED + timedelta(seconds=number)).isoformat()})
        (self.source/'failed.json').write_text(json.dumps({'status': 'failed', 'error_type': 'ValueError', 'raw_pages': entries}))

    def test_coverage_only_and_early_close_counts(self):
        # Invalid prices do not affect timestamp-only decisions; recovery below
        # separately validates OHLCV before creating a success manifest.
        self.payloads[0]['bars']['QQQ'][0]['o'] = 'not-a-price'
        self.save_payloads()
        report = audit_failed_download(self.source, self.protocol, report_path=self.root/'coverage.json')
        self.assertTrue(report['timestamp_only_audit'])
        self.assertFalse(report['outcomes_inspected'])
        self.assertEqual(report['excluded_sessions'], 1)
        self.assertEqual(report['missing_rth_minutes'], 1)
        self.assertEqual(report['complete_early_sessions'], 1)
        self.assertEqual(report['exclusions'][0]['missing_intervals'][0]['start'], self.missing)
        self.assertTrue(report['within_exclusion_cap'])
        if os.name == 'posix':
            self.assertEqual((self.root/'coverage.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            audit_failed_download(self.source, self.protocol, report_path=self.root/'coverage.json')

    def test_recovery_preserves_original_bytes_calendar_and_whole_session_exclusion(self):
        originals = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        result = recover_history(self.source, self.protocol, self.root/'recovered', now=IMPORTED)
        target = Path(result['directory'])
        self.assertTrue(result['research_only'])
        self.assertEqual(result['gap_policy'], GAP_POLICY)
        self.assertEqual(result['imported_at'], IMPORTED.isoformat())
        self.assertNotEqual(result['retrieved_at'], result['imported_at'])
        self.assertEqual(result['start'], '2025-11-03')
        self.assertEqual(result['end'], '2025-12-05')
        self.assertEqual(result['trusted_protocol_sha256'], sha256_file(self.protocol))
        self.assertEqual((target/'source-protocol.json').read_bytes(), self.protocol.read_bytes())
        self.assertEqual((target/'source-failed.json').read_bytes(), originals['failed.json'])
        self.assertEqual((target/'sessions.json').read_bytes(), originals['sessions.json'])
        for name, content in originals.items():
            self.assertEqual((self.source/name).read_bytes(), content)
            if name.startswith('raw/'):
                self.assertEqual((target/name).read_bytes(), content)
        exclusions = json.loads((target/result['exclusions']).read_bytes())
        self.assertEqual([row['session'] for row in exclusions], [self.sessions[0].date])
        expected_minutes = sum(int((s.close-s.open).total_seconds()/60) for s in self.sessions[1:])
        self.assertEqual(result['counts']['QQQ'], {'1Min': expected_minutes, '5Min': expected_minutes//5})
        csv_file = target/result['bars']['QQQ']['5Min']
        text = csv_file.read_text()
        self.assertNotIn(self.sessions[0].date, text)
        self.assertIn('2025-11-28', text)  # early close retained, never padded
        names = [entry['path'] for entry in result['files'] + result['raw_pages']]
        self.assertEqual(len(names), len(set(names)))
        for entry in result['files'] + result['raw_pages']:
            self.assertEqual(sha256_file(target/entry['path']), entry['sha256'])
        source, _ = _provenance(csv_file, 'QQQ', csv_file.read_bytes(), False, target/'manifest.json')
        self.assertEqual(source['source'], 'alpaca')
        self.assertEqual(source['dataset_sha256'], result['dataset_sha256'])
        if os.name == 'posix':
            for path in (target.parent, target, *target.rglob('*')):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_five_percent_cap_is_fixed_before_any_normalization(self):
        excluded_days = {s.date for s in self.sessions[:2]}
        self.rows = [row for row in self.rows if row['t'][:10] not in excluded_days]
        self.write_pages()
        report = audit_failed_download(self.source, self.protocol)
        self.assertFalse(report['within_exclusion_cap'])
        with patch('dwight.history_recovery.normalize_minutes') as normalize, self.assertRaisesRegex(ValueError, '5%'):
            recover_history(self.source, self.protocol, self.root/'recovered', now=IMPORTED)
        normalize.assert_not_called()
        self.assertFalse((self.root/'recovered').exists())

    def test_checksum_and_inventory_are_enforced(self):
        raw = self.source/'raw/page-000001.json'
        original = raw.read_bytes()
        raw.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            audit_failed_download(self.source, self.protocol)
        raw.write_bytes(original)
        (self.source/'raw/page-000003.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'inventory'):
            audit_failed_download(self.source, self.protocol)

    def test_pagination_incomplete_repeated_or_missing_rejected(self):
        for kind in ('final-token', 'missing-token', 'early-end', 'unexpected-symbol', 'out-of-order', 'duplicate'):
            self.write_pages()
            if kind == 'final-token':
                self.payloads[-1]['next_page_token'] = 'unfinished'
            elif kind == 'missing-token':
                self.payloads[-1].pop('next_page_token')
            elif kind == 'early-end':
                self.payloads[0]['next_page_token'] = None
            elif kind == 'unexpected-symbol':
                self.payloads[0]['bars']['SPY'] = []
            elif kind == 'out-of-order':
                self.payloads[0]['bars']['QQQ'][:2] = list(reversed(self.payloads[0]['bars']['QQQ'][:2]))
            else:
                self.payloads[0]['bars']['QQQ'][1] = self.payloads[0]['bars']['QQQ'][0]
            self.save_payloads()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                audit_failed_download(self.source, self.protocol)

    def test_calendar_protocol_and_page_numbering_must_match(self):
        calendar = self.source/'sessions.json'
        original = calendar.read_bytes()
        calendar.write_text('[]')
        with self.assertRaisesRegex(ValueError, 'calendar'):
            audit_failed_download(self.source, self.protocol)
        calendar.write_bytes(original)
        failure = self.source/'failed.json'
        value = json.loads(failure.read_bytes()); value['raw_pages'].reverse(); failure.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'contiguous'):
            audit_failed_download(self.source, self.protocol)

    def test_bad_ohlcv_in_excluded_session_leaves_only_private_failure(self):
        self.payloads[0]['bars']['QQQ'][0]['v'] = float('nan')
        self.save_payloads()
        with self.assertRaises(ValueError):
            recover_history(self.source, self.protocol, self.root/'recovered', now=IMPORTED)
        target = next((self.root/'recovered').iterdir())
        self.assertFalse((target/'manifest.json').exists())
        failure = json.loads((target/'recovery-failed.json').read_bytes())
        self.assertEqual(failure, {'status': 'failed', 'error_type': 'ValueError'})
        if os.name == 'posix':
            for path in (target, *target.rglob('*')):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_recovery_rejects_symlinks_and_existing_unsafe_output(self):
        link = self.root/'linked'
        link.symlink_to(self.root/'original', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            recover_history(link/'datasets/attempt', self.protocol, self.root/'recovered', now=IMPORTED)
        if os.name == 'posix':
            unsafe = self.root/'unsafe'; unsafe.mkdir(mode=0o755); unsafe.chmod(0o755)
            with self.assertRaisesRegex(ValueError, '0700'):
                recover_history(self.source, self.protocol, unsafe, now=IMPORTED)
            self.assertEqual(unsafe.stat().st_mode & 0o777, 0o755)


if __name__ == '__main__':
    unittest.main()
