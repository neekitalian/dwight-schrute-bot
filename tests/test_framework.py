import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from dwight.runner import replay
from dwight.store import Store
from dwight.research import Assessment, classify
from dwight.connectors.polymarket import discover

DATA = 'timestamp,open,high,low,close,volume\n2026-09-28T09:30:00-04:00,100,101,99,100,1000\n'

class FrameworkTests(unittest.TestCase):
    def test_replay_durable_and_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); data=root/'bars.csv'; data.write_text(DATA)
            first=replay(data,'DEMO',{},root/'runs')
            second=replay(data,'DEMO',{},root/'runs')
            self.assertNotEqual(first['run_id'],second['run_id'])
            manifests=[json.loads((Path(r['directory'])/'manifest.json').read_text()) for r in (first,second)]
            self.assertEqual(manifests[0]['input_sha256'],manifests[1]['input_sha256'])
            with sqlite3.connect(root/'runs/journal.sqlite3') as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)

    def test_bad_input_never_creates_successful_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); data=root/'bars.csv';data.write_text(DATA+DATA.splitlines()[1]+'\n')
            with self.assertRaises(ValueError): replay(data,'DEMO',{},root/'runs')
            self.assertFalse((root/'runs/journal.sqlite3').exists())
            self.assertEqual(len(list((root/'runs').glob('*/failed.json'))),1)

    def test_journal_rolls_back_partial_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'journal.sqlite3')
            try:
                with self.assertRaises(ValueError):
                    store.save('x',{}, {}, [('ok',{}),('bad',{'value':float('nan')})])
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],0)
            finally: store.close()

    def test_uncertain_research_abstains(self):
        class Provider:
            def assess(self,text): return Assessment('buy',1,'model-v1','q-v1')
        self.assertEqual(classify('news',Provider())['category'],'review')

    def test_fallback_gets_original_evidence(self):
        class Fast:
            def assess(self,text): return Assessment('relevant',.5,'fast-v1','q-v1')
        class Fallback:
            def assess(self,text):
                self.text=text
                return Assessment('irrelevant',.99,'fallback-v1','q-v1')
        fallback=Fallback()
        result=classify('original news',Fast(),fallback)
        self.assertEqual(result['route'],'fallback')
        self.assertEqual(fallback.text,'original news')

    def test_discovery_validates_without_network(self):
        with patch('dwight.connectors.polymarket.urlopen') as request:
            for limit in (0,101,True):
                with self.assertRaises(ValueError): discover(limit)
            request.assert_not_called()

    def test_discovery_get_only(self):
        with patch('dwight.connectors.polymarket.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value=b'[{"id":"test"}]'
            self.assertEqual(discover(1),[{'id':'test'}])
            req=request.call_args.args[0]
            self.assertEqual(req.get_method(),'GET')
            self.assertNotIn('Authorization',req.headers)

if __name__=='__main__': unittest.main()
