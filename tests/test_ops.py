import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from dataclasses import asdict
from vwap_bot.engine import Config
from unittest.mock import patch
from dwight.ops import load_env, doctor, release, verify_release, sha256
from dwight.experiments import FEATURE_NAMES, FEATURE_VERSION
from dwight.recorder import record_books

class OpsTests(unittest.TestCase):
    def make_candidate(self, root):
        exp=root/'exp';exp.mkdir()
        source=Path(__file__).resolve().parents[1]
        code_hash=hashlib.sha256((source/'dwight/experiments.py').read_bytes()+(source/'vwap_bot/engine.py').read_bytes()).hexdigest()
        model={'schema_version':1,'kind':'logistic_regression','feature_version':FEATURE_VERSION,
               'feature_names':list(FEATURE_NAMES),'mean':[0]*len(FEATURE_NAMES),
               'scale':[1]*len(FEATURE_NAMES),'coefficients':[0]*len(FEATURE_NAMES),
               'intercept':0,'threshold':.5,'symbol':'QQQ','synthetic':True,
               'source':'synthetic','feed':'synthetic','input_sha256':'fixture',
               'strategy':asdict(Config()),'code_sha256':code_hash}
        (exp/'model.json').write_text(json.dumps(model))
        report={**model,'status':'completed_synthetic_smoke','selected_threshold':.5,
                'model_sha256':sha256(exp/'model.json')}
        (exp/'report.json').write_text(json.dumps(report))
        policy=root/'policy.json'
        policy.write_text(json.dumps({'mode':'shadow','feed':'synthetic','allowed_symbols':['QQQ'],
                                     'poll_seconds':30,'bar_settle_seconds':60,'max_bar_delay_seconds':120}))
        return exp,policy

    def test_dotenv_not_executed_or_overridden(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'ALREADY':'original'}, clear=True):
            p=Path(root)/'.env';p.write_text('ALREADY=new\nSECRET="$(do-not-execute)"\n')
            load_env(p)
            self.assertEqual(os.environ['ALREADY'],'original')
            self.assertEqual(os.environ['SECRET'],'$(do-not-execute)')
            self.assertNotIn('$(do-not-execute)',str(doctor()))

    def test_freeze_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);exp,policy=self.make_candidate(root)
            release(exp,root/'release',feed='synthetic',symbol='QQQ',policy_path=policy)
            self.assertFalse(verify_release(root/'release','synthetic')['paper_approved'])
            with self.assertRaises(ValueError): verify_release(root/'release','iex')
            with patch('dwight.ops.source_sha256',return_value='changed-code'):
                with self.assertRaises(ValueError): verify_release(root/'release')
            (root/'release/model.json').write_text('tampered')
            with self.assertRaises(ValueError): verify_release(root/'release')

    def test_release_rejects_feed_provenance_and_report_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);exp,policy=self.make_candidate(root)
            with self.assertRaises(ValueError):
                release(exp,root/'release',feed='sip',symbol='QQQ',policy_path=policy)
            report=json.loads((exp/'report.json').read_text());report['selected_threshold']=.9
            (exp/'report.json').write_text(json.dumps(report))
            with self.assertRaises(ValueError):
                release(exp,root/'release',feed='synthetic',symbol='QQQ',policy_path=policy)

    def test_release_rejects_other_symbols_and_widened_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);exp,policy=self.make_candidate(root)
            with self.assertRaises(ValueError):
                release(exp,root/'release',feed='synthetic',symbol='SPY',policy_path=policy)
            settings=json.loads(policy.read_text());settings['allowed_symbols']=['QQQ','SPY']
            policy.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, 'QQQ only'):
                release(exp,root/'release',feed='synthetic',symbol='QQQ',policy_path=policy)
            self.assertFalse((root/'release').exists())

    def test_records_each_outcome_and_errors(self):
        data=[{'id':'m','clobTokenIds':'["123", "456"]','closed':False}]
        book={'asset_id':'123','bids':[],'asks':[]}
        with tempfile.TemporaryDirectory() as root,patch('dwight.recorder.get_json',side_effect=[data,book,OSError()]):
            result=record_books(root,1)
            self.assertEqual(result['recorded'],1)
            self.assertEqual(result['errors'],1)
