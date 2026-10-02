from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dwight import campaign
from dwight.ops import sha256

START = datetime(2026, 10, 2, 19, 1, tzinfo=timezone.utc)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root/'campaign'
        campaign.initialize(self.directory, ['one@example.com','two@example.com'], START)

    def fixture(self):
        release = self.root/'release'
        release.mkdir()
        experiment = self.root/'experiment'
        experiment.mkdir()
        (release/'release.json').write_text('{}')
        (release/'report.json').write_text('{}')
        (experiment/'report.json').write_text('{}')
        shadow = self.root/'shadow'
        shadow.mkdir()
        beat = dict(release_id=sha256(release/'release.json'), observed_at=START.isoformat(),
                    status='observed', timely_bar_closes=[(START-timedelta(minutes=1)).isoformat()],
                    submits_orders=False)
        (shadow/'heartbeat.json').write_text(json.dumps(beat))
        with sqlite3.connect(shadow/'shadow.sqlite3') as db:
            db.executescript('CREATE TABLE observations(sequence INTEGER PRIMARY KEY,observed_at TEXT,status TEXT,payload TEXT);'
                             'CREATE TABLE decisions(signal_id TEXT,release_id TEXT,payload TEXT);')
            db.execute('INSERT INTO observations(observed_at,status,payload) VALUES(?,?,?)',
                       (START.isoformat(), 'observed', json.dumps(beat)))
        return release, experiment, shadow

    def begin(self):
        paths = self.fixture()
        with patch('dwight.campaign.verify_release', return_value={'synthetic':False,'symbol':'QQQ'}):
            campaign.start(self.directory, *paths, START)
        return paths

    def test_setup_has_no_due_milestones_and_keeps_contacts_private(self):
        value = campaign.status(self.directory, START+timedelta(days=3))
        self.assertIsNone(value['started_at'])
        self.assertEqual(value['due_hours'], [])
        self.assertEqual((self.directory/'campaign.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    def test_rejects_synthetic_and_stale_heartbeat(self):
        paths = self.fixture()
        with patch('dwight.campaign.verify_release', return_value={'synthetic':True,'symbol':'QQQ'}):
            with self.assertRaisesRegex(ValueError, 'real QQQ'):
                campaign.start(self.directory, *paths, START)
        with patch('dwight.campaign.verify_release', return_value={'synthetic':False,'symbol':'QQQ'}):
            with self.assertRaisesRegex(ValueError, 'fresh real shadow'):
                campaign.start(self.directory, *paths, START+timedelta(hours=1))

    def test_start_freezes_deadlines_and_refuses_restart(self):
        paths = self.begin()
        value = campaign.status(self.directory, START+timedelta(hours=24))
        self.assertEqual(value['due_hours'], [12,24])
        self.assertEqual([m['due_at'] for m in value['milestones']],
                         [(START+timedelta(hours=h)).isoformat() for h in campaign.HOURS])
        with patch('dwight.campaign.verify_release', return_value={'synthetic':False,'symbol':'QQQ'}):
            with self.assertRaisesRegex(ValueError, 'already started'):
                campaign.start(self.directory, *paths, START)

    def test_forward_counts_exclude_catchup_other_release_and_future(self):
        release, _, shadow = self.begin()
        identity = sha256(release/'release.json')
        with sqlite3.connect(shadow/'shadow.sqlite3') as db:
            for offset, rid in [(5,identity),(10,identity),(15,'other'),(24*60,identity)]:
                at = START+timedelta(minutes=offset)
                beat = dict(release_id=rid,status='observed',observed_at=at.isoformat(),timely_bar_closes=[
                    (at-timedelta(minutes=1)).isoformat(),(START-timedelta(minutes=10)).isoformat()])
                db.execute('INSERT INTO observations(observed_at,status,payload) VALUES(?,?,?)',
                           (at.isoformat(),'observed',json.dumps(beat)))
            for i, delta in enumerate([-10,5,24*60]):
                at = START+timedelta(minutes=delta)
                decision = dict(available_at=at.isoformat(),observed_at=at.isoformat(),
                                shadow_take=True,stale_or_catchup=False)
                db.execute('INSERT INTO decisions VALUES(?,?,?)',(str(i),identity,json.dumps(decision)))
        value = campaign.status(self.directory,START+timedelta(hours=12))
        self.assertEqual(value['observed_market_minutes'],10)
        self.assertEqual(value['observed_sessions'],1)
        self.assertEqual(value['shadow_decisions'],1)
        self.assertEqual(value['shadow_takes'],1)
        self.assertIsNone(value['broker_paper_fills'])

    def test_weekend_clock_does_not_invent_market_observations(self):
        self.begin()
        early = campaign.status(self.directory,START+timedelta(hours=12))
        later = campaign.status(self.directory,START+timedelta(hours=48))
        self.assertEqual(early['expected_market_minutes'],later['expected_market_minutes'])
        self.assertEqual(later['observed_market_minutes'],0)

    def test_due_report_and_delivery_are_idempotent_and_recipient_specific(self):
        self.begin()
        with self.assertRaisesRegex(ValueError,'not due'):
            campaign.report(self.directory,12,START+timedelta(hours=1))
        output = self.root/'report'
        output.mkdir()
        body = output/'email.txt'
        body.write_text('A report with observed facts.\n')
        html = output/'report.html'
        html.write_text('<p>A report</p>')
        result = {'html':str(html),'email':str(body)}
        with patch('dwight.campaign.verify_release'), patch('dwight.audit.audit_experiment',return_value={}), \
                patch('dwight.reporting.generate_report',return_value=result) as generate:
            first = campaign.report(self.directory,12,START+timedelta(hours=13))
            again = campaign.report(self.directory,12,START+timedelta(hours=25))
            self.assertEqual(first,again)
            generate.assert_called_once()
            self.assertEqual(generate.call_args.kwargs['campaign_status']['elapsed_hours'],12)
        claim = campaign.claim_delivery(self.directory,12,'one@example.com',START)
        with self.assertRaisesRegex(ValueError,'awaiting delivery'):
            campaign.claim_delivery(self.directory,12,'one@example.com',START)
        with self.assertRaisesRegex(ValueError,'receipt'):
            campaign.confirm_delivery(self.directory,12,'one@example.com',claim['claim'],'',START)
        campaign.confirm_delivery(self.directory,12,'one@example.com',claim['claim'],'provider-message-1',START)
        value = campaign.status(self.directory,START+timedelta(hours=12))
        self.assertEqual(value['due_hours'],[12])  # second recipient still due
        claim2 = campaign.claim_delivery(self.directory,12,'two@example.com',START)
        campaign.confirm_delivery(self.directory,12,'two@example.com',claim2['claim'],'provider-message-2',START)
        self.assertEqual(campaign.status(self.directory,START+timedelta(hours=12))['due_hours'],[])

    def test_partial_render_can_retry_and_modified_attachment_cannot_send(self):
        self.begin()
        destinations = []
        def render(experiment, output, **kwargs):
            destinations.append(output)
            output.mkdir()
            if len(destinations) == 1:
                (output/'partial.png').write_bytes(b'partial')
                raise RuntimeError('renderer interrupted')
            (output/'email.txt').write_text('Observed results.\n')
            (output/'report.html').write_text('<p>Observed results</p>')
            return {'email':str(output/'email.txt'),'html':str(output/'report.html')}
        with patch('dwight.campaign.verify_release'), patch('dwight.audit.audit_experiment',return_value={}), \
                patch('dwight.reporting.generate_report',side_effect=render):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                campaign.report(self.directory,12,START+timedelta(hours=12))
            result = campaign.report(self.directory,12,START+timedelta(hours=12))
        self.assertNotEqual(destinations[0],destinations[1])
        Path(result['html']).write_text('<p>Unverified replacement</p>')
        with self.assertRaisesRegex(ValueError,'HTML changed'):
            campaign.claim_delivery(self.directory,12,'one@example.com')


if __name__ == '__main__':
    unittest.main()
