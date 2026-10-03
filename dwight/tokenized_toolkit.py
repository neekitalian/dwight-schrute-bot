"""Private collection and offline comparison entry points. No trading imports."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path


def _read(path, limit=1024*1024):
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Research inputs cannot traverse symbolic links')
    with path.open('rb') as stream:
        raw = stream.read(limit+1)
    if len(raw) > limit:
        raise ValueError('Research input exceeds its size bound')
    return raw


def _object(raw):
    def unique(pairs):
        values = {}
        for key,value in pairs:
            if key in values:
                raise ValueError('Duplicate research JSON field')
            values[key] = value
        return values
    def constant(_):
        raise ValueError('Non-finite research JSON value')
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('Research JSON must contain an object')
    return value


def _new_directory(path):
    directory = Path(path).expanduser().absolute()
    if '..' in directory.parts or any(part.is_symlink() for part in (directory,*directory.parents)):
        raise ValueError('Private output cannot traverse symbolic links or parent paths')
    directory.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory.mkdir(mode=0o700)  # Refuses files, existing reports and symlinks.
    return directory


def _write(path, content):
    with path.open('xb') as stream:
        os.chmod(path,0o600)
        stream.write(content)


def _json(value):
    return (json.dumps(value,indent=2,allow_nan=False)+'\n').encode()


def collect_tokenized(output, *, transport=None, clock=None):
    """Five public GET requests. Explicitly selected; no environment loading."""
    from .tokenized_data import collect_qqqx_snapshot
    # Refuse an existing destination before any network request.
    directory = _new_directory(output)
    try:
        snapshot = collect_qqqx_snapshot(transport=transport,clock=clock)
        raw = _json(snapshot)
        _write(directory/'snapshot.json',raw)
    except Exception as exc:
        _write(directory/'failed.json',_json({'status':'collection_failed',
                                             'error_type':type(exc).__name__,
                                             'execution_enabled':False}))
        raise
    return {'status':'collected','snapshot':str(directory/'snapshot.json'),
            'snapshot_sha256':hashlib.sha256(raw).hexdigest(),
            'completed_bars':len(snapshot['bars']),'data_kind':snapshot['data_kind'],
            'execution_enabled':False}


def _qqq_input(manifest_path):
    """Verify a known raw QQQ dataset, its evidence hashes and actual calendar."""
    from .data import exchange_sessions
    from .experiments import _provenance
    path = Path(manifest_path).expanduser().absolute()
    manifest_raw = _read(path)
    manifest = _object(manifest_raw)
    if (manifest.get('schema_version') not in {'vendor-rth-bars-v1','alpaca-rth-bars-v1'}
            or manifest.get('adjustment') != 'raw' or manifest.get('symbols') != ['QQQ']
            or manifest.get('timestamp_convention') != 'interval_start'
            or manifest.get('availability') != 'interval_end'
            or manifest.get('calendar') != 'XNYS'
            or manifest.get('session_policy') != 'complete_regular_sessions_only'
            or manifest.get('synthetic',False) is not False):
        raise ValueError('Comparison requires a known, raw QQQ regular-session dataset manifest')
    entries = manifest.get('files')
    if not isinstance(entries,list) or not entries or len(entries)>5000:
        raise ValueError('Dataset requires bounded evidence files')
    files, names = {}, set()
    for entry in entries:
        if not isinstance(entry,dict) or set(entry) != {'path','sha256'}:
            raise ValueError('Invalid dataset evidence entry')
        name = entry['path']
        if not isinstance(name,str) or name in names:
            raise ValueError('Invalid or duplicated dataset evidence path')
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Dataset evidence path must stay inside the dataset')
        names.add(name)
        raw = _read(path.parent/relative,64*1024*1024)
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Dataset evidence checksum mismatch')
        if name in {'QQQ-5Min.csv','sessions.json'}:
            files[name] = raw
    expected = manifest.get('bars',{}).get('QQQ',{}).get('5Min')
    if expected != 'QQQ-5Min.csv' or manifest.get('sessions') != 'sessions.json' or not {'QQQ-5Min.csv','sessions.json'} <= files.keys():
        raise ValueError('Dataset requires QQQ five-minute bars and exchange sessions')
    provenance, checked = _provenance(path.parent/expected,'QQQ',files[expected],False,path)
    if checked != manifest:
        raise ValueError('Dataset changed during comparison preparation')
    sessions = exchange_sessions(manifest['start'],manifest['end'])
    actual = json.loads(files['sessions.json'])
    if actual != [session.as_dict() for session in sessions]:
        raise ValueError('Dataset sessions do not match the exchange calendar')
    reader = csv.DictReader(io.StringIO(files[expected].decode('utf-8-sig')),strict=True)
    required = {'timestamp','open','high','low','close','volume'}
    if (not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames))
            or not required <= set(reader.fieldnames)):
        raise ValueError('Dataset lacks five-minute OHLCV columns')
    rows = []
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError('Malformed five-minute CSV row')
            rows.append({'timestamp':row['timestamp'],**{key:float(row[key]) for key in required-{'timestamp'}}})
    except csv.Error:
        raise ValueError('Malformed five-minute CSV') from None
    if len(rows)>200_000:
        raise ValueError('Comparison supports at most 200000 QQQ bars')
    if manifest.get('counts',{}).get('QQQ',{}).get('5Min') != len(rows):
        raise ValueError('Dataset bar count mismatch')
    # A verified complete-session manifest must contain every regular bar.
    expected_times = set()
    from datetime import timedelta
    for session in sessions:
        stamp = session.open
        while stamp < session.close:
            expected_times.add(stamp)
            stamp += timedelta(minutes=5)
    if {datetime.fromisoformat(row['timestamp']) for row in rows} != expected_times or len(rows)!=len(expected_times):
        raise ValueError('QQQ dataset has missing or unexpected regular-session bars')
    source = {'symbol':'QQQ','asset_class':'equity_etf','currency':'USD',
              'provider':provenance['source'],'feed':provenance['feed'],
              'adjustment':'raw','interval_minutes':5,'timestamp_label':'start',
              'data_kind':'historical_real','collected_at':manifest['retrieved_at'],
              'dataset_sha256':manifest['dataset_sha256'],
              'manifest_sha256':hashlib.sha256(manifest_raw).hexdigest(),
              'input_sha256':hashlib.sha256(files[expected]).hexdigest(),
              'exchange_sessions':actual,
              'integrity_statement':'Local hashes and calendar checked; no cryptographic provider attestation'}
    return rows,source


def compare_tokenized(qqq_manifest, token_snapshot, output):
    """Offline comparison. Existing output is never replaced; inputs frozen."""
    from .tokenized_research import compare_market_data
    from .tokenized_reporting import render_comparison
    rows,source = _qqq_input(qqq_manifest)
    token_raw = _read(token_snapshot,2*1024*1024)
    snapshot = _object(token_raw)
    if snapshot.get('data_kind') != 'observed_public_market':
        raise ValueError('Manifest comparison requires an observed public token snapshot')
    report = compare_market_data(rows,snapshot,qqq_source=source)
    report['input_integrity'] = {'qqq_manifest_sha256':source['manifest_sha256'],
                                'token_snapshot_sha256':hashlib.sha256(token_raw).hexdigest()}
    report['generated_at'] = datetime.now(timezone.utc).isoformat()
    raw = _json(report)
    html = render_comparison(report).encode()
    directory = _new_directory(output)
    _write(directory/'report.json',raw)
    _write(directory/'report.html',html)
    _write(directory/'token-snapshot.json',token_raw)
    _write(directory/'qqq-source.json',_json(source))
    _write(directory/'checksums.json',_json({name:hashlib.sha256(content).hexdigest() for name,content in [
        ('report.json',raw),('report.html',html),('token-snapshot.json',token_raw),('qqq-source.json',_json(source))]}))
    return {'status':report['status'],'directory':str(directory),
            'report':str(directory/'report.html'),'common_bar_count':report['coverage']['common_bar_count'],
            'data_kind':report['data_kind'],'execution_enabled':False}
