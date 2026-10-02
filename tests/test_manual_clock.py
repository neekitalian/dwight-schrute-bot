"""Real SQLite contention cannot backdate a newly delivered proposal."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from dwight.manual import ManualPaperError, ManualPaperJournal, _clock
from tests.test_manual import NOW, proposal


class JournalClockTests(unittest.TestCase):
    def test_writer_wait_cannot_bypass_reference_deadline_or_proposal_expiry(self):
        for mode in ('reference', 'expiry'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                journal = ManualPaperJournal(Path(temporary) / 'journal.sqlite3')
                payload = proposal(expires_at=(NOW+timedelta(seconds=1 if mode == 'expiry' else 300)).isoformat())
                options = {'create_before': (NOW+timedelta(seconds=1)).isoformat()} if mode == 'reference' else {}
                clock = [NOW]
                waiting = threading.Event()
                original_db = journal._db

                @contextmanager
                def observe_lock_wait():
                    waiting.set()
                    with original_db() as connection:
                        yield connection

                def current(now=None):
                    return _clock(clock[0] if now is None else now)

                with sqlite3.connect(journal.path) as blocker, ThreadPoolExecutor(max_workers=1) as executor:
                    blocker.execute('BEGIN IMMEDIATE')
                    with patch.object(journal, '_db', observe_lock_wait), patch('dwight.manual._clock', side_effect=current):
                        pending = executor.submit(journal.add_proposal, payload, **options)
                        self.assertTrue(waiting.wait(2), 'worker did not reach journal transaction')
                        clock[0] = NOW+timedelta(seconds=2)
                        blocker.commit()
                        with self.assertRaises(ManualPaperError):
                            pending.result(timeout=3)
                self.assertEqual(journal.list_proposals(now=clock[0]), [])

    def test_existing_proposal_can_recover_after_reference_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = ManualPaperJournal(Path(temporary) / 'journal.sqlite3')
            payload = proposal()
            deadline = (NOW+timedelta(seconds=1)).isoformat()
            journal.add_proposal(payload, now=NOW, create_before=deadline)
            recovered = journal.add_proposal(payload, now=NOW+timedelta(minutes=10), create_before=deadline)
            self.assertEqual(recovered['status'], 'expired')
            self.assertEqual(len(journal.list_proposals(now=NOW+timedelta(minutes=10))), 1)
