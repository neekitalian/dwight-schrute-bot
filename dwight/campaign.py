"""Private milestone ledger. No mail transport and no order capability.

Milestones begin with a verified real shadow observation, never during setup.
Delivery claims persist before a connector is invoked. An ambiguous send stays
claimed until its provider receipt is recovered; it is never retried blindly.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from .ops import sha256, verify_release

UTC = timezone.utc
NY = ZoneInfo('America/New_York')
HOURS = (12, 24, 48, 168)


def clock(now=None):
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError('campaign clock must include a timezone')
    return value.astimezone(UTC)


def parse(value):
    return clock(datetime.fromisoformat(value))


def save(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


@contextmanager
def ledger(directory):
    directory = Path(directory).resolve()
    path = directory/'campaign.json'
    if not path.is_file():
        raise ValueError('campaign does not exist')
    with (directory/'campaign.lock').open('a') as lock:
        os.chmod(directory/'campaign.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        yield state
        save(path, state)


def initialize(directory, recipients, now=None):
    recipients = list(dict.fromkeys(x.strip() for x in recipients))
    if not recipients or any(not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', x) for x in recipients):
        raise ValueError('provide valid report recipients')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    state = {
        'schema_version': 1, 'id': uuid.uuid4().hex, 'symbol': 'QQQ',
        'created_at': clock(now).isoformat(), 'started_at': None,
        'mode': 'shadow', 'status': 'awaiting_preparation',
        'recipients': recipients, 'experiment_dir': None, 'shadow_state': None,
        'release_dir': None, 'release_id': None,
        'blockers': ['Alpaca credentials and matching real time feed',
                     'Real historical experiment and reviewed frozen model',
                     'Linux server with a healthy shadow worker',
                     'Connected email sender'],
        'milestones': [{
            'hours': hour, 'due_at': None, 'report': None,
            'delivery': {email: {'status': 'pending'} for email in recipients},
        } for hour in HOURS],
    }
    save(directory/'campaign.json', state)
    return {'directory': str(directory), 'status': state['status'],
            'milestone_hours': list(HOURS), 'started_at': None}


def start(directory, release_dir, experiment_dir, shadow_state, now=None):
    now = clock(now)
    release_dir, experiment_dir, shadow_state = [Path(p).resolve() for p in (release_dir, experiment_dir, shadow_state)]
    manifest = verify_release(release_dir)
    if manifest.get('synthetic') or manifest.get('symbol') != 'QQQ':
        raise ValueError('campaign requires a real QQQ release')
    if sha256(experiment_dir/'report.json') != sha256(release_dir/'report.json'):
        raise ValueError('experiment does not match frozen release')
    identity = sha256(release_dir/'release.json')
    beat = json.loads((shadow_state/'heartbeat.json').read_text())
    age = (now-parse(beat['observed_at'])).total_seconds()
    if (beat.get('release_id') != identity or beat.get('status') != 'observed'
            or not 0 <= age <= 180 or not beat.get('timely_bar_closes')
            or beat.get('submits_orders') is not False):
        raise ValueError('start after a fresh real shadow observation with timely bars')
    # A matching heartbeat must also be durable in the observation ledger.
    with sqlite3.connect(f'file:{shadow_state / "shadow.sqlite3"}?mode=ro', uri=True) as db:
        row = db.execute('SELECT payload FROM observations WHERE observed_at=? ORDER BY sequence DESC LIMIT 1',
                         (beat['observed_at'],)).fetchone()
    if not row or json.loads(row[0]) != beat:
        raise ValueError('heartbeat is not confirmed in the observation ledger')
    with ledger(directory) as state:
        if state['started_at']:
            raise ValueError('campaign already started; milestones are immutable')
        state.update(status='observing', started_at=now.isoformat(), blockers=[],
                     release_dir=str(release_dir), release_id=identity,
                     experiment_dir=str(experiment_dir), shadow_state=str(shadow_state))
        for milestone in state['milestones']:
            milestone['due_at'] = (now+timedelta(hours=milestone['hours'])).isoformat()
    return status(directory, now)


def observations(state, until):
    """Count only timely, first observed bars. Warmup/catchup does not count."""
    since = parse(state['started_at'])
    until = min(clock(until), since+timedelta(hours=168))
    timely, sessions, decisions, takes, stale, errors = set(), set(), 0, 0, 0, 0
    latest = latest_at = None
    dbpath = Path(state['shadow_state'])/'shadow.sqlite3'
    with sqlite3.connect(f'file:{dbpath}?mode=ro', uri=True) as db:
        rows = db.execute('SELECT payload FROM observations WHERE observed_at>=? AND observed_at<=? ORDER BY sequence',
                          (since.isoformat(), until.isoformat()))
        for (raw,) in rows:
            item = json.loads(raw)
            if item.get('release_id') != state['release_id']:
                continue
            latest = item['status']
            latest_at = parse(item['observed_at'])
            errors += item['status'] in {'late_bar','error_abstain','data_revision_requires_review'}
            for stamp in item.get('timely_bar_closes', []):
                close = parse(stamp)
                if since <= close <= until:
                    timely.add(close.isoformat())
                    sessions.add(close.astimezone(NY).date().isoformat())
        for (raw,) in db.execute('SELECT payload FROM decisions WHERE release_id=?', (state['release_id'],)):
            item = json.loads(raw)
            # A catchup signal predating campaign start is never a forward candidate.
            if not since <= parse(item['available_at']) <= until or not since <= parse(item['observed_at']) <= until:
                continue
            decisions += 1
            takes += item.get('shadow_take') is True
            stale += item.get('stale_or_catchup') is True
    from .data import exchange_sessions
    expected = 0
    excluded_sessions = 0
    for session in exchange_sessions(since.astimezone(NY).date(), until.astimezone(NY).date()):
        if session.close-session.open != timedelta(minutes=390):
            excluded_sessions += 1
            continue
        close = session.open+timedelta(minutes=5)
        while close <= session.close:
            expected += since <= close <= until
            close += timedelta(minutes=5)
    return {
        'elapsed_hours': max(0, (until-since).total_seconds()/3600),
        'observed_sessions': len(sessions), 'observed_market_minutes': len(timely)*5,
        'expected_market_minutes': expected*5,
        'missing_market_minutes': max(0, expected-len(timely))*5,
        'excluded_early_close_sessions': excluded_sessions,
        'shadow_decisions': decisions, 'shadow_takes': takes,
        'stale_decisions': stale, 'error_observations': errors,
        'latest_status': latest, 'broker_paper_fills': None,
        'last_observation_age_seconds': (until-latest_at).total_seconds() if latest_at else None,
        'broker_paper_pnl': None,
        'execution_note': 'Shadow observations are not broker orders or fills',
    }


def status(directory, now=None):
    now = clock(now)
    with ledger(directory) as state:
        snapshot = json.loads(json.dumps(state))
    snapshot['due_hours'] = [m['hours'] for m in snapshot['milestones']
                             if m['due_at'] and parse(m['due_at']) <= now
                             and any(x['status'] != 'sent' for x in m['delivery'].values())]
    if snapshot['started_at']:
        snapshot.update(observations(snapshot, now))
    return snapshot


def report(directory, hours, now=None):
    now = clock(now)
    if hours not in HOURS:
        raise ValueError('unknown milestone')
    with ledger(directory) as state:
        milestone = next(m for m in state['milestones'] if m['hours'] == hours)
        if not milestone['due_at'] or parse(milestone['due_at']) > now:
            raise ValueError('milestone is not due')
        if milestone['report']:
            return milestone['report']
        from .audit import audit_experiment
        from .reporting import generate_report
        verify_release(Path(state['release_dir']))
        campaign_status = dict(status=state['status'], blockers=[],
                               **observations(state, parse(milestone['due_at'])))
        campaign_status['window_start'] = state['started_at']
        campaign_status['window_end'] = milestone['due_at']
        artifact = generate_report(Path(state['experiment_dir']),
                                   Path(directory)/f'report-{hours}h-{uuid.uuid4().hex}',
                                   milestone_label='One week' if hours == 168 else f'{hours} hours',
                                   campaign_status=campaign_status,
                                   audit=audit_experiment(Path(state['experiment_dir'])))
        milestone['report'] = artifact
        milestone['report_sha256'] = sha256(Path(artifact['html']))
        milestone['email_sha256'] = sha256(Path(artifact['email']))
        milestone['artifact_sha256'] = {key: sha256(Path(value)) for key, value in artifact.items()
                                        if key in {'html','email','summary','price_chart','performance_chart'}}
        milestone['generated_at'] = now.isoformat()
    return artifact


def claim_delivery(directory, hours, recipient, now=None):
    with ledger(directory) as state:
        milestone = next((m for m in state['milestones'] if m['hours'] == hours), None)
        if not milestone or not milestone['report']:
            raise ValueError('generate the milestone report before sending')
        record = milestone['delivery'].get(recipient)
        if record is None or record['status'] != 'pending':
            raise ValueError('recipient is unknown, already sent or awaiting delivery reconciliation')
        if sha256(Path(milestone['report']['email'])) != milestone['email_sha256']:
            raise ValueError('report body changed after generation')
        if sha256(Path(milestone['report']['html'])) != milestone['report_sha256']:
            raise ValueError('report HTML changed after generation')
        for key, checksum in milestone.get('artifact_sha256', {}).items():
            if sha256(Path(milestone['report'][key])) != checksum:
                raise ValueError('report artifact changed after generation')
        token = uuid.uuid4().hex
        record.update(status='sending', claim=token, claimed_at=clock(now).isoformat())
    return {'claim': token, 'recipient': recipient, 'email': milestone['report']['email'],
            'html': milestone['report']['html'], 'status': 'sending'}


def report_due(directory, now=None):
    now = clock(now)
    snapshot = status(directory, now)
    generated = []
    for milestone in snapshot['milestones']:
        if milestone['hours'] in snapshot['due_hours'] and not milestone['report']:
            generated.append({'hours': milestone['hours'], 'artifacts': report(directory, milestone['hours'], now)})
    return {'status': snapshot['status'], 'reports_generated': generated, 'sends_email': False}


def confirm_delivery(directory, hours, recipient, claim, receipt, now=None):
    if not receipt or not receipt.strip():
        raise ValueError('provider delivery receipt is required')
    with ledger(directory) as state:
        milestone = next((m for m in state['milestones'] if m['hours'] == hours), None)
        record = milestone['delivery'].get(recipient) if milestone else None
        if not record or record.get('claim') != claim or record['status'] != 'sending':
            raise ValueError('delivery claim does not match')
        record.update(status='sent', receipt=receipt, confirmed_at=clock(now).isoformat())
        if all(r['status'] == 'sent' for m in state['milestones'] for r in m['delivery'].values()):
            state['status'] = 'completed'
    return {'status': 'sent', 'hours': hours, 'recipient': recipient}
