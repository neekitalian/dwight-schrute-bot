"""Invented inputs exercise private workflows, integrity and browser safety."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from dwight.data import Session
from dwight.tokenized_research import compare_market_data, QUANTITY_UNIT
from dwight.tokenized_reporting import render_comparison
from dwight.tokenized_toolkit import _qqq_input, collect_tokenized, compare_tokenized
from dwight.vendor_history import fingerprint

UTC = timezone.utc
OPEN = datetime(2026,10,2,13,30,tzinfo=UTC)
FINISH = datetime(2026,10,3,10,tzinfo=UTC)


def token(kind='observed_public_market'):
    return {'schema_version':1,'data_kind':kind,'execution_enabled':False,
            'interval_minutes':5,'timestamp_label':'start',
            'instrument':{'symbol':'QQQx','base':'QQQx','quote':'USD',
                          'venue':'kraken','asset_class':'tokenized_equity','pair':'QQQxUSD'},
            'collection':{'started_at':(FINISH-timedelta(seconds=5)).isoformat(),
                          'finished_at':FINISH.isoformat()},
            'bars':[{'timestamp':OPEN.isoformat(),'open':101,'high':102,'low':100,'close':101,'volume':1}],
            'orderbook':{'observed_at':FINISH.isoformat(),'bids':[[100,10]],'asks':[[102,10]],
                         'quantity_unit':QUANTITY_UNIT}}


class TokenizedToolkitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.dataset = self.root/'dataset'
        self.dataset.mkdir()
        self.session = Session('2026-10-02',OPEN,OPEN+timedelta(hours=6.5))
        # Imports inside the workflow use the public data calendar helper.
        self.calendar = patch('dwight.data.exchange_sessions',return_value=[self.session])
        self.calendar.start()
        self.addCleanup(self.calendar.stop)
        self.comparison_calendar = patch('dwight.tokenized_research.exchange_sessions',return_value=[self.session])
        self.comparison_calendar.start()
        self.addCleanup(self.comparison_calendar.stop)
        sessions = [self.session.as_dict()]
        (self.dataset/'sessions.json').write_text(json.dumps(sessions))
        for minutes in [1,5]:
            lines = ['timestamp,open,high,low,close,volume']
            for i in range(390//minutes):
                lines.append(f'{(OPEN+timedelta(minutes=i*minutes)).isoformat()},100,101,99,100,{minutes*10}')
            (self.dataset/f'QQQ-{minutes}Min.csv').write_text('\n'.join(lines)+'\n')
        (self.dataset/'raw').mkdir()
        (self.dataset/'raw/page-000001.json').write_text('{"fixture":"invented software test"}')
        self.manifest = {'schema_version':'vendor-rth-bars-v1','source':'massive',
                         'retrieved_at':FINISH.isoformat(),'start':'2026-10-02','end':'2026-10-02',
                         'symbols':['QQQ'],'feed':'massive_stocks_aggregates','adjustment':'raw',
                         'provider_metadata':{'source':'massive','feed':'massive_stocks_aggregates','adjustment':'raw'},
                         'research_only':True,'live_feed':False,'synthetic':False,
                         'source_acquisition':'https_download','timestamp_convention':'interval_start',
                         'availability':'interval_end','calendar':'XNYS','session_policy':'complete_regular_sessions_only',
                         'gap_policy':'reject_no_forward_fill','volume_definition':'fixture_volume',
                         'counts':{'QQQ':{'1Min':390,'5Min':78}},
                         'bars':{'QQQ':{'1Min':'QQQ-1Min.csv','5Min':'QQQ-5Min.csv'}},
                         'sessions':'sessions.json'}
        self.refresh_manifest()
        self.snapshot = self.root/'token.json'
        self.snapshot.write_text(json.dumps(token()))

    def refresh_manifest(self):
        names = ['sessions.json','QQQ-1Min.csv','QQQ-5Min.csv','raw/page-000001.json']
        self.manifest['files'] = [{'path':name,'sha256':hashlib.sha256((self.dataset/name).read_bytes()).hexdigest()} for name in names]
        self.manifest['raw_pages'] = [self.manifest['files'][-1]]
        self.manifest['dataset_sha256'] = fingerprint(self.manifest)
        (self.dataset/'manifest.json').write_text(json.dumps(self.manifest))

    def test_complete_offline_cli_flow_no_credentials_and_private_integrity(self):
        from dwight.__main__ import main
        out = self.root/'report'
        stream = io.StringIO()
        with patch('dwight.__main__.load_env') as env, patch('sys.argv',[
            'dwight','compare-tokenized','--qqq-manifest',str(self.dataset/'manifest.json'),
            '--token-snapshot',str(self.snapshot),'--out',str(out)]),redirect_stdout(stream):
            main()
            env.assert_not_called()
        result = json.loads(stream.getvalue())
        self.assertEqual(result['common_bar_count'],1)
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode),0o700)
        checks = json.loads((out/'checksums.json').read_bytes())
        for name,expected in checks.items():
            self.assertEqual(hashlib.sha256((out/name).read_bytes()).hexdigest(),expected)
            self.assertEqual(stat.S_IMODE((out/name).stat().st_mode),0o600)
        self.assertEqual((out/'token-snapshot.json').read_bytes(),self.snapshot.read_bytes())
        report = json.loads((out/'report.json').read_bytes())
        self.assertEqual(report['sources']['qqq']['input_sha256'],self.manifest['files'][2]['sha256'])

    def test_existing_collection_output_blocks_network(self):
        with patch('dwight.tokenized_data.collect_qqqx_snapshot') as fetch:
            with self.assertRaises(FileExistsError):
                collect_tokenized(self.dataset)
            fetch.assert_not_called()

    def test_collection_failure_is_sanitized_and_cannot_be_overwritten(self):
        out = self.root/'failed'
        with patch('dwight.tokenized_data.collect_qqqx_snapshot',side_effect=RuntimeError('PRIVATE BODY')):
            with self.assertRaises(RuntimeError):
                collect_tokenized(out)
        self.assertNotIn('PRIVATE BODY',(out/'failed.json').read_text())
        with self.assertRaises(FileExistsError):
            collect_tokenized(out)

    def test_changed_data_fails_before_report_creation(self):
        (self.dataset/'QQQ-5Min.csv').write_text('changed bytes')
        with self.assertRaisesRegex(ValueError,'checksum'):
            compare_tokenized(self.dataset/'manifest.json',self.snapshot,self.root/'report')
        self.assertFalse((self.root/'report').exists())

    def test_valid_hashes_do_not_permit_fabricated_calendar(self):
        sessions = [dict(self.session.as_dict(),close=(self.session.close-timedelta(minutes=5)).isoformat())]
        (self.dataset/'sessions.json').write_text(json.dumps(sessions))
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError,'exchange calendar'):
            _qqq_input(self.dataset/'manifest.json')

    def test_adjusted_history_and_synthetic_snapshot_cannot_be_mixed(self):
        self.snapshot.write_text(json.dumps(token('synthetic')))
        with self.assertRaisesRegex(ValueError,'observed public'):
            compare_tokenized(self.dataset/'manifest.json',self.snapshot,self.root/'report')
        self.manifest['adjustment']='split'
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError,'raw QQQ'):
            _qqq_input(self.dataset/'manifest.json')

    def test_missing_bar_rejected_even_with_updated_file_hash(self):
        p=self.dataset/'QQQ-5Min.csv'
        p.write_text('\n'.join(p.read_text().splitlines()[:-1])+'\n')
        self.manifest['counts']['QQQ']['5Min']=77
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError,'missing or unexpected'):
            _qqq_input(self.dataset/'manifest.json')

    def test_duplicate_csv_header_and_extra_cells_rejected(self):
        p=self.dataset/'QQQ-5Min.csv'
        original=p.read_text()
        for content in [original.replace('volume\n','volume,volume\n',1),original.replace(',50\n',',50,extra\n',1)]:
            p.write_text(content)
            self.refresh_manifest()
            with self.assertRaises(ValueError):
                _qqq_input(self.dataset/'manifest.json')

    def test_source_and_output_symlinks_rejected(self):
        link=self.root/'link'
        link.symlink_to(self.dataset,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symbolic'):
            _qqq_input(link/'manifest.json')
        with self.assertRaisesRegex(ValueError,'symbolic'):
            collect_tokenized(link/'new')

    def test_renderer_escapes_data_has_correct_venue_and_masks_partial_vwap(self):
        rows,source = _qqq_input(self.dataset/'manifest.json')
        source['provider']='</script><script>bad()</script>'
        result=compare_market_data(rows,token(),qqq_source=source)
        result['aligned_series'][0]['qqqx_vwap_prefix_complete']=False
        html=render_comparison(result)
        self.assertNotIn('<script>bad()</script>',html)
        self.assertIn('&lt;/script&gt;',html)
        self.assertIn('<td>kraken</td>',html)
        self.assertIn('<td>QQQxUSD</td>',html)
        self.assertIn('"qqqx_vwap": null',html)
        self.assertNotIn('src="http',html)
        self.assertNotIn('fetch(',html)


if __name__=='__main__':
    unittest.main()
