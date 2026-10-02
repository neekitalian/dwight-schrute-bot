"""Private native-paper experiment deadlines, frozen evidence and mail receipts.

This ledger never places orders or sends mail. Account evidence is supplied and
reviewed by an operator, not authenticated through TradingView. It is separate
from the model-qualified shadow campaign in ``dwight.campaign``.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from zipfile import ZipFile, ZIP_DEFLATED
from zoneinfo import ZoneInfo

from . import __version__
from .data import exchange_sessions
from .manual import ACCOUNT, ManualPaperJournal, _decimal, _json, _stamp, _text
from .manual_reporting import render_manual_report
from .manual_signals import _safe_path, read_observation_snapshot

UTC = timezone.utc
NY = ZoneInfo('America/New_York')
HOURS = (12, 24, 48, 168)
CHECKS = {'paper_account_label_reviewed', 'holdings_and_orders_reviewed',
          'risk_policy_reviewed', 'manual_exits_ready', 'fill_mapping_reviewed'}
ACCOUNT_FIELDS = {'schema_version', 'account', 'account_alias', 'observed_at',
                  'equity_usd', 'cash_usd', 'qqq_quantity', 'qqq_open_orders',
                  'chart_symbol', 'chart_timeframe', 'chart_feed', 'chart_realtime',
                  'observation_feed', 'feed_comparison_note', 'checks', 'evidence_files'}
MAX_FILE_BYTES = 10_000_000
MAX_FILES = 8


def clock(now=None):
    value = datetime.now(UTC) if now is None else now
    if not isinstance(value, datetime):
        raise ValueError('clock must be a timezone-aware datetime')
    return datetime.fromisoformat(_stamp(value.isoformat(), 'clock'))


def stamp(now=None):
    return clock(now).isoformat(timespec='microseconds')


def parse(value):
    return datetime.fromisoformat(_stamp(value, 'timestamp'))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_bytes(path, limit=MAX_FILE_BYTES):
    path = _safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('evidence must be a bounded regular file without hard links')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('evidence file exceeds size limit')
        return raw
    finally:
        os.close(fd)


def _write_new(path, raw):
    path = _safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+'\n').encode()


def _directory(directory):
    root = _safe_path(directory)
    if not root.is_dir():
        raise ValueError('manual campaign does not exist')
    info = root.stat()
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('manual campaign directory must be private')
    return root


@contextmanager
def ledger(directory):
    root = _directory(directory)
    path = root/'manual-campaign.sqlite3'
    # Validate without creating absent files or following links.
    _read_bytes(path, 100_000_000)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('manual campaign database must be private')
    for suffix in ('-journal','-wal','-shm'):
        sidecar = _safe_path(str(path)+suffix)
        if sidecar.exists() and (not sidecar.is_file() or sidecar.stat().st_nlink != 1):
            raise ValueError('campaign SQLite sidecars must be regular private files')
    connection = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT payload, sha256 FROM plan').fetchone()
        if not row or digest(row['payload'].encode()) != row['sha256']:
            raise ValueError('frozen experiment specification changed')
        plan = json.loads(row['payload'])
        if plan.get('schema_version') != 1 or plan.get('account') != ACCOUNT:
            raise ValueError('unsupported native-paper experiment')
        if _read_bytes(root/'plan.json') != row['payload'].encode():
            raise ValueError('frozen plan file differs from experiment ledger')
        yield connection, plan, root
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _state(db):
    return json.loads(db.execute('SELECT payload FROM state').fetchone()[0])


def _save_state(db, value):
    db.execute('UPDATE state SET payload=?', (_json(value),))


def _journal(plan):
    path = _safe_path(plan['journal_path'])
    if not path.is_file():
        raise ValueError('the pinned manual journal is missing')
    return ManualPaperJournal(path)


def initialize(directory, signals_dir, journal_path, recipients, allocated_capital, now=None):
    """Prepare private immutable settings. Does not start the experiment clock."""
    now = clock(now)
    recipients = list(dict.fromkeys(recipients))
    if (not 1 <= len(recipients) <= 10 or any(not isinstance(x, str) or
            not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', x) for x in recipients)):
        raise ValueError('provide one to ten distinct valid report recipients')
    allocation = _text(_decimal(allocated_capital, 'allocated_capital'))
    source = read_observation_snapshot(signals_dir, cutoff=now, now=now)
    journal_path = _safe_path(journal_path)
    if not journal_path.is_file():
        raise ValueError('create a separate private manual journal before preparation')
    journal = ManualPaperJournal(journal_path)
    initial = journal.snapshot_at(now, now=now)
    if initial['fills'] or initial['proposals']:
        raise ValueError('first native-paper campaign requires an empty dedicated journal')
    root = _safe_path(directory)
    if root.exists() or not root.parent.is_dir():
        raise ValueError('choose a new campaign directory with an existing parent')
    plan = {'schema_version':1, 'id':uuid.uuid4().hex, 'account':ACCOUNT,
            'mode':'native_paper_manual', 'symbol':'QQQ', 'timeframe':'5Min',
            'model':'none-baseline', 'application_version':__version__,
            'strategy_identity':source['strategy_identity'], 'feed':source['feed'],
            'observation_policy':source['policy'],
            'store_instance_id':source['store_instance_id'],
            'journal_instance_id':initial['journal_instance_id'],
            'signals_dir':str(_safe_path(signals_dir)), 'journal_path':str(journal_path),
            'created_at':stamp(now), 'recipients':recipients, 'milestone_hours':list(HOURS),
            'allocated_capital_usd':allocation,
            'manual_risk_policy':{'risk_fraction':'0.0025', 'max_leverage':'1',
                                  'max_session_losses':2, 'one_qqq_position':True,
                                  'whole_shares':True, 'session_close_exit':True,
                                  'enforced_by_software':False},
            'opening_inventory_policy':'operator_attested_flat_qqq_and_no_qqq_orders',
            'broker_verified':False, 'submits_orders':False, 'sends_email':False}
    root.mkdir(mode=0o700)
    raw = _json_bytes(plan)
    _write_new(root/'plan.json', raw)
    _write_new(root/'manual-campaign.sqlite3', b'')
    with sqlite3.connect(root/'manual-campaign.sqlite3') as db:
        db.executescript('''CREATE TABLE plan(payload TEXT NOT NULL,sha256 TEXT NOT NULL);
            CREATE TABLE state(payload TEXT NOT NULL);
            CREATE TABLE accounts(id TEXT PRIMARY KEY,observed_at TEXT NOT NULL,captured_at TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE milestones(hours INTEGER PRIMARY KEY,due_at TEXT,report TEXT);
            CREATE TABLE deliveries(hours INTEGER NOT NULL,recipient TEXT NOT NULL,status TEXT NOT NULL,claim TEXT,claimed_at TEXT,receipt TEXT,confirmed_at TEXT,PRIMARY KEY(hours,recipient));''')
        db.execute('INSERT INTO plan VALUES(?,?)',(raw.decode(),digest(raw)))
        db.execute('INSERT INTO state VALUES(?)',(_json({'status':'prepared','started_at':None,
                   'account_snapshot_id':None,'start_anchor':None}),))
        db.executemany('INSERT INTO milestones(hours) VALUES(?)',[(h,) for h in HOURS])
        db.executemany('INSERT INTO deliveries(hours,recipient,status) VALUES(?,?,?)',
                       [(h,r,'pending') for h in HOURS for r in recipients])
    return {'id':plan['id'],'directory':str(root),'status':'prepared','started_at':None,
            'plan_sha256':digest(raw),'submits_orders':False,'sends_email':False}


def _account_payload(value, plan, now):
    if not isinstance(value, dict) or set(value) != ACCOUNT_FIELDS:
        raise ValueError('account evidence fields must exactly match the documented schema')
    value = dict(value)
    if (type(value['schema_version']) is not int or value['schema_version'] != 1 or
            value['account'] != ACCOUNT or value['chart_symbol'] != 'QQQ' or
            value['chart_timeframe'] != '5Min' or value['observation_feed'] != plan['feed']):
        raise ValueError('account evidence must match native paper, QQQ, five minutes and frozen feed')
    value['observed_at'] = _stamp(value['observed_at'], 'observed_at')
    if parse(value['observed_at']) > now:
        raise ValueError('account evidence cannot be future dated')
    for key in ('equity_usd','cash_usd','qqq_quantity'):
        value[key] = _text(_decimal(value[key], key, zero=True))
    if type(value['qqq_open_orders']) is not int or not 0 <= value['qqq_open_orders'] <= 100000:
        raise ValueError('QQQ open order count must be a nonnegative integer')
    for key, maximum in (('account_alias',80),('chart_feed',160),('feed_comparison_note',600)):
        if (not isinstance(value[key],str) or not value[key].strip() or len(value[key]) > maximum
                or any(ord(c)<32 for c in value[key])):
            raise ValueError('account alias and feed notes must be bounded nonempty text')
    if type(value['chart_realtime']) is not bool:
        raise ValueError('chart_realtime must be a boolean')
    if (not isinstance(value['checks'],dict) or set(value['checks']) != CHECKS
            or any(type(x) is not bool for x in value['checks'].values())):
        raise ValueError('provide every documented operator review checkbox explicitly')
    files = value['evidence_files']
    if not isinstance(files,list) or not 1 <= len(files) <= MAX_FILES or any(not isinstance(p,str) for p in files):
        raise ValueError('attach one to eight private account evidence files')
    return value


def record_account(directory, evidence_json_path, now=None):
    """Archive operator-supplied records; does not authenticate an account."""
    live_clock = now is None
    path = _safe_path(evidence_json_path)
    value = json.loads(_read_bytes(path,100_000))
    with ledger(directory) as (db, plan, root):
        now = clock(now)
        value = _account_payload(value,plan,now)
        state = _state(db)
        if now < parse(plan['created_at']):
            raise ValueError('capture cannot precede experiment preparation')
        if state.get('account_alias') and value['account_alias'] != state['account_alias']:
            raise ValueError('account alias differs from the frozen starting account')
        identifier = uuid.uuid4().hex
        evidence = []
        payloads = []
        for i, name in enumerate(value.pop('evidence_files')):
            source = Path(name)
            if not source.is_absolute():
                source = path.parent/source
            raw = _read_bytes(source)
            if not raw:
                raise ValueError('empty evidence file is not sufficient')
            extension = source.suffix.lower()
            if extension not in {'.png','.jpg','.jpeg','.pdf','.csv','.json','.txt'}:
                raise ValueError('unsupported private evidence file extension')
            destination = f'evidence-{identifier}-{i}{extension}'
            evidence.append({'file':destination,'sha256':digest(raw),'bytes':len(raw)})
            payloads.append((destination,raw))
        for name,raw in payloads:
            _write_new(root/name,raw)
        if live_clock:
            now = clock()
        result = {**value,'id':identifier,'captured_at':stamp(now),'files':evidence,
                  'source':'operator_supplied','broker_verified':False}
        db.execute('INSERT INTO accounts VALUES(?,?,?,?)',(identifier,result['observed_at'],
                                                         result['captured_at'],_json(result)))
    return {'snapshot_id':identifier,'observed_at':result['observed_at'],
            'source':'operator_supplied','broker_verified':False,'submits_orders':False}


def _verify_account(root, record):
    for evidence in record['files']:
        name = evidence['file']
        if Path(name).name != name or digest(_read_bytes(root/name)) != evidence['sha256']:
            raise ValueError('archived account evidence changed or disappeared')


def _source(plan, cutoff, now):
    snapshot = read_observation_snapshot(plan['signals_dir'], cutoff=cutoff, now=now)
    for key in ('store_instance_id','strategy_identity','feed'):
        if snapshot[key] != plan[key]:
            raise ValueError('observation source, feed or strategy changed; use a new experiment')
    if any(snapshot['integrity'].values()):
        raise ValueError('observation evidence contains incomplete or unlinked records')
    if snapshot['policy'] != plan['observation_policy']:
        raise ValueError('observation policy changed; use a new experiment')
    return snapshot


def _anchor(snapshot):
    return {key:snapshot[key] for key in ('cutoff','sequence_watermark','prefix_sha256')}


def _verify_anchor(plan, anchor, now):
    snapshot = _source(plan,parse(anchor['cutoff']),now)
    if _anchor(snapshot) != anchor:
        raise ValueError('previous observation history changed or was truncated')


def start(directory, snapshot_id, now=None):
    live_clock = now is None
    with ledger(directory) as (db, plan, root):
        now = clock(now)
        state = _state(db)
        if now < parse(plan['created_at']):
            raise ValueError('start cannot precede experiment preparation')
        if state['started_at'] is not None:
            raise ValueError('experiment already started; deadlines are immutable')
        record = db.execute('SELECT payload FROM accounts WHERE id=?',(snapshot_id,)).fetchone()
        if not record:
            raise ValueError('record the initial account evidence before starting')
        account = json.loads(record[0])
        _verify_account(root,account)
        if now < parse(account['captured_at']):
            raise ValueError('start cannot precede capture of required account evidence')
        age = (now-parse(account['observed_at'])).total_seconds()
        if not 0 <= age <= 300:
            raise ValueError('initial operator account evidence must be within five minutes')
        if (Decimal(account['qqq_quantity']) != 0 or account['qqq_open_orders'] != 0 or
                not account['chart_realtime'] or not all(account['checks'].values())):
            raise ValueError('start requires reviewed flat QQQ account, no QQQ orders, real-time chart and all manual checks')
        allocation = Decimal(plan['allocated_capital_usd'])
        if allocation > min(Decimal(account['equity_usd']),Decimal(account['cash_usd'])):
            raise ValueError('allocation exceeds operator-supplied cash or equity')
        current = _journal(plan).snapshot_at(now,now=now)
        if current['journal_instance_id'] != plan['journal_instance_id']:
            raise ValueError('dedicated journal identity changed')
        if current['fills'] or current['proposals']:
            raise ValueError('start requires the dedicated journal still to be empty')
        if live_clock:
            now = clock()
        source = _source(plan,now,now)
        if live_clock:
            now = clock()
        if now < parse(account['captured_at']):
            raise ValueError('start cannot precede capture of required account evidence')
        if not 0 <= (now-parse(account['observed_at'])).total_seconds() <= 300:
            raise ValueError('initial account evidence became stale during preparation')
        latest = source['latest_observation']
        if not latest or latest['status'] not in {'ready','waiting_for_bar'}:
            raise ValueError('start after a fresh durable completed-bar observation')
        current_status = source['current_status']
        if any(current_status.get(key) for key in ('halted','data_error','stopped')):
            raise ValueError('observer must not be stopped, halted or awaiting data recovery')
        available = parse(latest['latest_bar_close'])
        receipt_age = (now-parse(latest['observed_at'])).total_seconds()
        bar_age = (now-available).total_seconds()
        matching = [b for b in source['bars'] if parse(b['available_at']) == available]
        if (not 0 <= receipt_age <= 120 or not 60 <= bar_age < 120 or len(matching) != 1
                or not 60 <= (parse(matching[0]['first_seen_at'])-available).total_seconds() < 120):
            raise ValueError('start requires a timely bar first observed in its eligible window')
        state.update(status='observing',started_at=stamp(now),account_snapshot_id=snapshot_id,
                     account_alias=account['account_alias'],start_anchor=_anchor(source))
        _save_state(db,state)
        for hour in HOURS:
            db.execute('UPDATE milestones SET due_at=? WHERE hours=?',
                       (stamp(now+timedelta(hours=hour)),hour))
    return status(directory,now=now)


def _status(db, plan, now):
    state = _state(db)
    milestones = []
    for row in db.execute('SELECT * FROM milestones ORDER BY hours'):
        deliveries = {r['recipient']:{key:r[key] for key in ('status','claim','claimed_at','receipt','confirmed_at')}
                      for r in db.execute('SELECT * FROM deliveries WHERE hours=? ORDER BY recipient',(row['hours'],))}
        milestones.append({'hours':row['hours'],'due_at':row['due_at'],
                           'report':json.loads(row['report']) if row['report'] else None,
                           'deliveries':deliveries})
    return {'id':plan['id'],'account':ACCOUNT,'mode':'native_paper_manual',**state,
            'milestones':milestones,
            'due_hours':[m['hours'] for m in milestones if m['due_at'] and parse(m['due_at'])<=now and not m['report']],
            'pending_delivery_hours':[m['hours'] for m in milestones if m['report'] and
                                      any(d['status']!='sent' for d in m['deliveries'].values())],
            'submits_orders':False,'sends_email':False,'broker_verified':False}


def status(directory, now=None):
    with ledger(directory) as (db,plan,_):
        return _status(db,plan,clock(now))


def coverage(snapshot, since, until):
    """Calendar-aware data coverage; covered bar minutes are not worker uptime."""
    since,until = clock(since),clock(until)
    if since > until:
        raise ValueError('coverage start cannot follow cutoff')
    opportunities = set()
    exchange_seconds = supported_seconds = 0
    excluded_early = []
    excluded_closing = 0
    for session in exchange_sessions(since.astimezone(NY).date(),until.astimezone(NY).date()):
        overlap = max(0,(min(until,session.close)-max(since,session.open)).total_seconds())
        exchange_seconds += overlap
        if session.close-session.open != timedelta(minutes=390):
            if overlap:
                excluded_early.append(session.date)
            continue
        supported_seconds += overlap
        close = session.open+timedelta(minutes=5)
        # At 16:00 the current observer stops. Its final bar cannot settle.
        while close < session.close:
            if since <= close and close+timedelta(seconds=60) <= until:
                opportunities.add(stamp(close))
            close += timedelta(minutes=5)
        excluded_closing += since <= session.close <= until
    timely, late, catchup = set(),set(),set()
    for bar in snapshot['bars']:
        available,seen = parse(bar['available_at']),parse(bar['first_seen_at'])
        if not since <= seen <= until:
            continue
        if available < since:
            catchup.add(bar['timestamp'])
        elif stamp(available) in opportunities:
            delay = (seen-available).total_seconds()
            (timely if 60 <= delay < 120 else late).add(stamp(available))
    forward_signals = [s for s in snapshot['signals'] if since <= parse(s['available_at'])<=until
                       and since <= parse(s['first_seen_at'])<=until]
    errors = [o for o in snapshot['observations'] if since <= parse(o['observed_at'])<=until
              and o['status'] in {'error_abstain','data_revision_requires_review','stopped'}]
    return {'elapsed_hours':(until-since).total_seconds()/3600,
            'exchange_open_minutes':exchange_seconds/60,'supported_open_minutes':supported_seconds/60,
            'expected_observation_bars':len(opportunities),'timely_observed_bars':len(timely),
            'late_observed_bars':len(late),'catchup_bars':len(catchup),
            'missing_timely_bars':len(opportunities-timely),'covered_bar_minutes':len(timely)*5,
            'excluded_early_close_sessions':excluded_early,'excluded_session_closing_bars':excluded_closing,
            'forward_signal_count':len(forward_signals),
            'initially_eligible_long_signals':sum(s['initially_eligible'] and s['accepted'] and s['direction']==1
                                                 for s in forward_signals),
            'error_or_stop_observations':len(errors),'cutoff_status':snapshot['cutoff_status'],
            'coverage_note':'Covered bar minutes measure data coverage, not continuous service uptime. The closing five-minute bar is excluded by the current observation policy.'}


def _verify_artifact(root, record):
    directory = _safe_path(root/record['directory'])
    if directory.parent != root:
        raise ValueError('invalid report directory')
    for relative,expected in record['files'].items():
        path = _safe_path(directory/relative)
        if not path.is_relative_to(directory) or digest(_read_bytes(path)) != expected:
            raise ValueError('frozen report artifact changed or disappeared')
    return {**record,'html':str(directory/'index.html'),'email':str(directory/'email.txt'),
            'json':str(directory/'milestone.json'),'fill_html':str(directory/'fills/report.html'),
            'bundle':str(directory/'report.zip')}


def _verify_journal_history(plan, root, artifact, now):
    """Late evidence may extend an older cutoff, never erase its frozen rows."""
    paths = _verify_artifact(root,artifact)
    old = json.loads(_read_bytes(Path(paths['fill_html']).with_name('report.json')))
    current = _journal(plan).snapshot_at(parse(old['as_of']),now=now)
    if current['journal_instance_id'] != plan['journal_instance_id']:
        raise ValueError('dedicated journal identity changed')
    for collection,key in (('fills','fill_id'),('proposals','proposal_id')):
        present = {row[key]:row for row in current[collection]}
        if any(present.get(row[key]) != row for row in old[collection]):
            raise ValueError('previously reported journal evidence changed or disappeared')
    current_events = {_json(row) for row in current['proposal_events']}
    if any(_json(row) not in current_events for row in old['proposal_events']):
        raise ValueError('previously reported proposal history was truncated')
    return {'hours':artifact['hours'],
            'additional_execution_records_through_prior_cutoff':len(current['fills'])-len(old['fills']),
            'original_report_unchanged':True}


def _render_summary(value):
    c = value['coverage']
    facts = [('Elapsed hours',c['elapsed_hours']),('Exchange open minutes',c['exchange_open_minutes']),
             ('Timely observed bars',c['timely_observed_bars']),('Missing timely bars',c['missing_timely_bars'])]
    cards=''.join(f'<div class="card"><span>{escape(k)}</span><strong>{v:g}</strong></div>' for k,v in facts)
    possible=c['expected_observation_bars']
    pct=100*c['timely_observed_bars']/possible if possible else 0
    chart=(f'<svg viewBox="0 0 1000 35" role="img" aria-label="Timely observation coverage"><rect x="0" y="0" width="1000" height="30" fill="#283443"/><rect x="0" y="0" width="{10*pct}" height="30" fill="#2ac4b3"/></svg><p>{c["timely_observed_bars"]} of {possible} matured observation opportunities</p>'
           if possible else '<p>No supported observation opportunities matured in this window.</p>')
    f=value['imported_accounting']
    pnl='Unknown: no imported execution evidence' if f['fill_count']==0 else escape(f['net_realized_pnl'])+' USD'
    evidence=''.join(f'<li>{escape(a["observed_at"])}: operator supplied equity {escape(a["equity_usd"])} USD; QQQ quantity {escape(a["qqq_quantity"])}. Not independently authenticated.</li>' for a in value['account_snapshots']) or '<li>No eligible account snapshot supplied.</li>'
    times={key:parse(value[key]).strftime('%d %b %Y, %H:%M:%S UTC')
           for key in ('window_start','window_end','evidence_captured_at')}
    audit=value['strategy_audit']
    corrections=''.join(f'<li>After the {u["hours"]} hour report, {u["additional_execution_records_through_prior_cutoff"]} additional executions were supplied for its original window. Its saved copy remains unchanged.</li>'
                        for u in value['prior_report_evidence_updates'] if u['additional_execution_records_through_prior_cutoff']) or '<li>No additional executions found for an earlier saved window.</li>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>Dwight | {value['milestone_hours']} hour paper evidence</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#0b1016;color:#e8edf4;font:15px/1.6 system-ui,sans-serif}}main{{max-width:1200px;margin:auto;padding:32px 20px}}h1{{font-size:clamp(26px,4vw,40px);line-height:1.2}}h2{{font-size:20px}}.label,a{{color:#72d6ca}}.note,span{{color:#a5b3c4}}section,.card{{background:#111922;border:1px solid #283443;border-radius:10px;padding:20px;margin-top:18px}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}.card strong,.card span{{display:block}}.card strong{{font-size:25px}}svg{{width:100%}}code{{overflow-wrap:anywhere}}@media(max-width:700px){{.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}@media(max-width:400px){{.cards{{grid-template-columns:1fr}}}}</style></head><body><main>
<p class="label">DWIGHT / PRIVATE PAPER EXPERIMENT</p><h1>{value['milestone_hours']} hours of evidence</h1><p>QQQ · Five minute VWAP baseline · Manual native paper account</p><p class="note">Fixed window: {times['window_start']} to {times['window_end']}.<br>Evidence captured {times['evidence_captured_at']}.</p>
<section><strong>User supplied evidence, not an authenticated account statement.</strong><p>These deadlines measure elapsed time. Weekends and closed markets add no trading exposure. The observer cannot place orders or enforce account risk.</p></section><div class="cards">{cards}</div>
<section><h2>Observation coverage</h2>{chart}<p>{escape(c['coverage_note'])}</p><p>Late bars: {c['late_observed_bars']}. Catchup bars: {c['catchup_bars']}. Signals: {c['forward_signal_count']}. Initially eligible long setups: {c['initially_eligible_long_signals']}. Error or stop observations: {c['error_or_stop_observations']}.</p></section>
<section><h2>Imported executions through the deadline</h2><p>Imported fill count: {f['fill_count']}. Net realized PnL: <strong>{pnl}</strong>.</p><p>Account return, unrealized PnL and drawdown remain unknown. No imported fills does not prove no trades. These numbers use evidence available at report generation, which may have arrived after the deadline.</p><p><a href="fills/report.html">Open the private fill charts and proposal comparisons</a></p></section>
<section><h2>Operator account records</h2><ul>{evidence}</ul><p>Reported account values are shown separately from imported FIFO accounting. No return or account reconciliation is inferred from screenshots or labels.</p></section>
<section><h2>Strategy checks and later evidence</h2><p>Proposals with a different baseline label or without a matching eligible observer signal: {len(audit['proposals_not_matching_frozen_baseline'])}. Fill records with comparison flags: {audit['fill_records_with_flags']}.</p><p>These are evidence checks, not full strategy or portfolio risk verification. Protective exits and account limits remain the operator's responsibility.</p><ul>{corrections}</ul></section>
<section><h2>Frozen evidence</h2><p><a href="milestone.json">Milestone JSON</a> · <a href="fills/report.json">Fill snapshot JSON</a></p><p class="note">Later imports cannot silently replace this report. Preserve corrections separately and reconcile any sending attempt before retrying email.</p></section></main></body></html>'''


def _email(value):
    c=value['coverage']; f=value['imported_accounting']
    end=parse(value['window_end']).strftime('%d %B %Y at %H:%M UTC')
    performance=('No execution evidence was imported for this window, so account trading results remain unknown.'
                 if not f['fill_count'] else
                 f"The journal contains {f['fill_count']} imported executions through the deadline. Net realized profit or loss on those records is {f['net_realized_pnl']} USD, including allocated fees. These are user supplied records and have not been independently authenticated.")
    return f'''Hi,

This is Dwight's {value['milestone_hours']} hour QQQ paper experiment report. The fixed reporting window ends on {end}.

The window includes {c['exchange_open_minutes']:g} exchange open minutes. Dwight recorded {c['timely_observed_bars']} timely completed bars out of {c['expected_observation_bars']} eligible observation opportunities. It recorded {c['forward_signal_count']} forward strategy setups and {c['error_or_stop_observations']} error or stop observations. Closed markets do not count as trading exposure.

{performance}

Account return, unrealized profit or loss and drawdown remain unknown. A manual confirmation is not an order or a fill. The report includes source references and recorded proposal differences rather than a claim of complete strategy compliance.

The report uses executions through the fixed deadline and evidence captured when it was generated. Later corrections will not silently change this saved copy. The attached private report contains the charts and observation coverage.

Dwight has not sent or changed any account orders. Model improvements from transformers or FinRL have not been measured in this baseline experiment.
'''


def report(directory, hours, now=None):
    live_clock = now is None
    if type(hours) is not int or hours not in HOURS:
        raise ValueError('choose a 12, 24, 48 or 168 hour milestone')
    with ledger(directory) as (db,plan,root):
        now=clock(now)
        state=_state(db)
        row=db.execute('SELECT * FROM milestones WHERE hours=?',(hours,)).fetchone()
        if not state['started_at'] or not row['due_at'] or parse(row['due_at'])>now:
            raise ValueError('milestone is not due')
        if row['report']:
            return _verify_artifact(root,json.loads(row['report']))
        since,until=parse(state['started_at']),parse(row['due_at'])
        _verify_anchor(plan,state['start_anchor'],now)
        corrections=[]
        for previous in db.execute('SELECT report FROM milestones WHERE report IS NOT NULL'):
            previous=json.loads(previous[0])
            _verify_anchor(plan,previous['observation_anchor'],now)
            update=_verify_journal_history(plan,root,previous,now)
            if previous['hours'] < hours:
                corrections.append(update)
        source=_source(plan,until,now)
        journal=_journal(plan).snapshot_at(until,now=None if live_clock else now)
        if journal['journal_instance_id'] != plan['journal_instance_id']:
            raise ValueError('dedicated journal identity changed')
        if journal['data_kind']=='synthetic':
            raise ValueError('synthetic fills cannot enter a native paper campaign')
        if any(parse(f['filled_at'])<since for f in journal['fills']):
            raise ValueError('pre-start fills violate the frozen zero-opening-inventory policy')
        accounts=[]
        for record in db.execute('SELECT payload FROM accounts WHERE observed_at<=? ORDER BY observed_at,id',(stamp(until),)):
            account=json.loads(record[0]); _verify_account(root,account)
            if account['account_alias'] != state['account_alias']:
                continue
            if account['id']==state['account_snapshot_id'] or parse(account['observed_at'])>=since:
                accounts.append(account)
        accounting={'fill_count':len(journal['fills']), 'net_realized_pnl':journal['net_realized_pnl'] if journal['fills'] else None,
                    'fees_imported':journal['fees_imported'] if journal['fills'] else None,
                    'open_inventory':journal['open_position'] if journal['fills'] else None}
        eligible_ids={'signal-'+s['signal_id'] for s in source['signals']
                      if s['initially_eligible'] and s['accepted'] and s['direction']==1
                      and since<=parse(s['available_at'])<=until and since<=parse(s['first_seen_at'])<=until}
        mismatches=[p['proposal_id'] for p in journal['proposals'] if p['version']!=plan['strategy_identity']
                    or p['model']!='none-baseline' or p['source']!='vwap_pullback_signal_observer'
                    or p['proposal_id'] not in eligible_ids]
        if live_clock:
            now=clock()
        value={'schema_version':1,'experiment_id':plan['id'],'mode':'native_paper_manual',
               'milestone_hours':hours,'window_start':stamp(since),'window_end':stamp(until),
               'generated_at':stamp(now),'evidence_captured_at':journal['captured_at'],
               'strategy_identity':plan['strategy_identity'],'feed':plan['feed'],
               'plan_sha256':digest(_read_bytes(root/'plan.json')),
               'coverage':coverage(source,since,until),'observation_snapshot':source,
               'account_snapshots':accounts,'imported_accounting':accounting,
               'prior_report_evidence_updates':corrections,
               'strategy_audit':{'proposals_not_matching_frozen_baseline':mismatches,
                                 'fill_records_with_flags':sum(bool(a['issues']) for a in journal['fill_audit']),
                                 'full_strategy_compliance_verified':False,
                                 'portfolio_risk_verified':False},
               'account_equity':None,'account_return_pct':None,'unrealized_pnl':None,
               'broker_verified':False,'submits_orders':False,'sends_email':False,
               'evidence_complete':False,'evidence_basis':journal['snapshot_basis']}
        name=f'report-{hours}h-{uuid.uuid4().hex}'
        destination=root/name; destination.mkdir(mode=0o700)
        render_manual_report(journal,destination/'fills')
        _write_new(destination/'milestone.json',_json_bytes(value))
        _write_new(destination/'index.html',_render_summary(value).encode())
        _write_new(destination/'email.txt',_email(value).encode())
        archive=destination/'report.zip'
        _write_new(archive,b'')
        with ZipFile(archive,'w',compression=ZIP_DEFLATED) as bundle:
            for relative in ('index.html','milestone.json','email.txt','fills/report.html','fills/report.json'):
                bundle.writestr(relative,_read_bytes(destination/relative))
        files={p.relative_to(destination).as_posix():digest(_read_bytes(p))
               for p in sorted(destination.rglob('*')) if p.is_file()}
        artifact={'directory':name,'files':files,'hours':hours,'window_end':stamp(until),
                  'generated_at':stamp(now),'observation_anchor':_anchor(source)}
        db.execute('UPDATE milestones SET report=? WHERE hours=?',(_json(artifact),hours))
        return _verify_artifact(root,artifact)


def report_due(directory, now=None):
    live_clock = now is None
    now=clock(now)
    current=status(directory,now)
    generated=[{'hours':hour,'artifacts':report(directory,hour,None if live_clock else now)} for hour in current['due_hours']]
    return {'status':current['status'],'reports_generated':generated,'sends_email':False}


def claim_delivery(directory,hours,recipient,now=None):
    with ledger(directory) as (db,plan,root):
        now=clock(now)
        row=db.execute('SELECT report FROM milestones WHERE hours=?',(hours,)).fetchone()
        delivery=db.execute('SELECT * FROM deliveries WHERE hours=? AND recipient=?',(hours,recipient)).fetchone()
        if not row or not row[0] or not delivery or delivery['status']!='pending':
            raise ValueError('report or recipient unavailable, already sent, or awaiting receipt reconciliation')
        artifact=_verify_artifact(root,json.loads(row[0]))
        if now < parse(artifact['generated_at']):
            raise ValueError('mail claim cannot precede report generation')
        claim=uuid.uuid4().hex
        db.execute("UPDATE deliveries SET status='sending',claim=?,claimed_at=? WHERE hours=? AND recipient=?",
                   (claim,stamp(now),hours,recipient))
        return {'claim':claim,'recipient':recipient,'status':'sending','email':artifact['email'],
                'html':artifact['html'],'fill_html':artifact['fill_html'],'bundle':artifact['bundle'],
                'subject':f'Dwight QQQ paper experiment: {hours} hours','sends_email':False}


def confirm_delivery(directory,hours,recipient,claim,receipt,now=None):
    if (not isinstance(receipt,str) or not receipt.strip() or len(receipt)>500
            or any(ord(c)<32 for c in receipt)):
        raise ValueError('record a bounded provider message receipt')
    with ledger(directory) as (db,_,root):
        now=clock(now)
        row=db.execute('SELECT * FROM deliveries WHERE hours=? AND recipient=?',(hours,recipient)).fetchone()
        if not row or row['claim']!=claim or row['status'] not in {'sending','sent'}:
            raise ValueError('delivery claim does not match')
        if row['status']=='sent':
            if row['receipt']!=receipt:
                raise ValueError('conflicting provider receipt')
            return {'status':'sent','recipient':recipient,'idempotent':True,'sends_email':False}
        if now<parse(row['claimed_at']):
            raise ValueError('receipt confirmation cannot precede claim')
        artifact=json.loads(db.execute('SELECT report FROM milestones WHERE hours=?',(hours,)).fetchone()[0])
        _verify_artifact(root,artifact)
        db.execute("UPDATE deliveries SET status='sent',receipt=?,confirmed_at=? WHERE hours=? AND recipient=?",
                   (receipt,stamp(now),hours,recipient))
        if not db.execute("SELECT 1 FROM deliveries WHERE status!='sent'").fetchone():
            state=_state(db);state['status']='completed';_save_state(db,state)
        return {'status':'sent','recipient':recipient,'idempotent':False,'sends_email':False}
