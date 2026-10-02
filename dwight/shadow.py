"""Read-only live-data rehearsal of a frozen model. No broker imports or orders.

This is a polled research monitor: the replay engine's portfolio remains
simulated. It records the model's candidate decisions, never treats simulated
fills as broker fills, and cannot be switched to paper execution with a flag.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

from vwap_bot.engine import Config
from .data import exchange_sessions, fetch_alpaca_bars, normalize_minutes
from .experiments import CandidateBot, JSONModel
from .ops import verify_release, sha256

UTC = timezone.utc
NY = ZoneInfo('America/New_York')


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, default=str)


class ShadowMonitor:
    def __init__(self, release_dir, state_dir):
        self.release_dir, self.state_dir = Path(release_dir), Path(state_dir)
        self.manifest = verify_release(self.release_dir)
        if self.manifest.get('synthetic'):
            raise ValueError('Synthetic models are for offline smoke tests only')
        self.model = JSONModel.load(self.release_dir/'model.json')
        self.policy = json.loads((self.release_dir/'policy.json').read_text())
        self.symbol, self.feed = self.manifest['symbol'], self.manifest['feed']
        self.identity = sha256(self.release_dir/'release.json')
        self.config = Config(**self.model.artifact['strategy'])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock = (self.state_dir/'shadow.lock').open('a+')
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError('Another shadow monitor owns this state directory') from None
        self.db = sqlite3.connect(self.state_dir/'shadow.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS bars(
                release_id TEXT, timestamp TEXT, payload TEXT,
                PRIMARY KEY(release_id,timestamp));
            CREATE TABLE IF NOT EXISTS decisions(
                signal_id TEXT PRIMARY KEY, release_id TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS observations(
                sequence INTEGER PRIMARY KEY, observed_at TEXT, status TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS shadow_halts(release_id TEXT PRIMARY KEY, reason TEXT);
        ''')

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def _status(self, status, now, **details):
        result = dict(mode='shadow', status=status, observed_at=now.isoformat(),
                      release_id=self.identity, symbol=self.symbol, feed=self.feed,
                      submits_orders=False, **details)
        with self.db:
            self.db.execute('INSERT INTO observations(observed_at,status,payload) VALUES(?,?,?)',
                            (now.isoformat(), status, encoded(result)))
        temp = self.state_dir/'heartbeat.tmp'
        temp.write_text(encoded(result)+'\n')
        temp.replace(self.state_dir/'heartbeat.json')
        return result

    def step(self, now=None):
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError('Shadow clock must be timezone aware')
        # Recheck release and code each iteration; fail on edits during a run.
        verify_release(self.release_dir, self.feed)
        if sha256(self.release_dir/'release.json') != self.identity:
            raise ValueError('Release changed while the monitor was running')
        if Path(self.policy.get('stop_file', 'runs/STOP')).exists():
            return self._status('stopped', now)
        halted = self.db.execute('SELECT reason FROM shadow_halts WHERE release_id=?', (self.identity,)).fetchone()
        if halted:
            return self._status('data_revision_requires_review',now,revised_bar=halted[0])
        day = now.astimezone(NY).date()
        sessions = exchange_sessions(day, day)
        if (not sessions or now < sessions[0].open or
                now >= sessions[0].close+timedelta(seconds=self.policy['max_bar_delay_seconds']+self.policy['poll_seconds'])):
            return self._status('market_closed', now)
        session = sessions[0]
        # The pinned strategy has 16:00 exits. Skip short sessions explicitly.
        if session.close-session.open != timedelta(minutes=390):
            return self._status('unsupported_early_close', now)
        delay = self.policy.get('bar_settle_seconds', 60)
        as_of = now-timedelta(seconds=delay)
        if as_of < session.open+timedelta(minutes=5):
            return self._status('warming_up', now)
        rows = fetch_alpaca_bars(session.open, now, (self.symbol,), self.feed)
        _, bars = normalize_minutes(rows[self.symbol], sessions,
                                    require_complete_session=False, as_of=as_of)
        if not bars:
            return self._status('warming_up', now)
        # Keep the raw observed slice: historical vendors can revise bars later.
        raw_path = self.state_dir/'observed'
        raw_path.mkdir(exist_ok=True)
        stamp = now.strftime('%Y%m%dT%H%M%S%fZ')
        (raw_path/f'{stamp}.json').write_text(encoded(rows)+'\n')
        last_close = bars[-1].timestamp+timedelta(minutes=5)
        # A 5m bar naturally ages between boundaries. Only enforce the delivery
        # deadline when processing a newly completed bar, not on every poll.
        fresh_bars = []
        for bar in bars:
            payload = encoded(asdict(bar))
            old = self.db.execute('SELECT payload FROM bars WHERE release_id=? AND timestamp=?',
                                  (self.identity, bar.timestamp.isoformat())).fetchone()
            if old and old[0] != payload:
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO shadow_halts VALUES(?,?)',
                                    (self.identity,bar.timestamp.isoformat()))
                return self._status('data_revision_requires_review', now,
                                    revised_bar=bar.timestamp.isoformat())
            if not old:
                fresh_bars.append((bar, payload))
        if not fresh_bars:
            return self._status('waiting_for_bar', now, last_bar_close=last_close.isoformat())
        bot = CandidateBot(self.config, self.model, self.model.artifact['threshold'])
        for bar in bars:
            bot.feed(bar)
        decisions = []
        new_stamps = {bar.timestamp.isoformat() for bar, _ in fresh_bars}
        for candidate in bot.candidates:
            if candidate['signal_time'] not in new_stamps:
                continue
            available = datetime.fromisoformat(candidate['available_at'])
            age = (now-available).total_seconds()
            actionable = 0 <= age <= self.policy['max_bar_delay_seconds']
            signal_id = hashlib.sha256((self.identity+'|'+self.symbol+'|'+candidate['signal_time']).encode()).hexdigest()
            decisions.append({**candidate, 'signal_id':signal_id, 'symbol':self.symbol,
                              'observed_at':now.isoformat(), 'signal_age_seconds':age,
                              'shadow_take':bool(candidate['taken'] and actionable),
                              'stale_or_catchup':not actionable, 'submits_orders':False})
        with self.db:
            for bar, payload in fresh_bars:
                self.db.execute('INSERT INTO bars VALUES(?,?,?)',
                                (self.identity, bar.timestamp.isoformat(), payload))
            for decision in decisions:
                self.db.execute('INSERT OR IGNORE INTO decisions VALUES(?,?,?)',
                                (decision['signal_id'], self.identity, encoded(decision)))
        status = 'observed' if 0 <= (now-last_close).total_seconds() <= self.policy['max_bar_delay_seconds'] else 'late_bar'
        return self._status(status, now, last_bar_close=last_close.isoformat(),
                            new_bars=len(fresh_bars), decisions=decisions)

    def run(self, once=False):
        while True:
            try:
                result = self.step()
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                # Never print provider payloads/credentials or retry any order.
                result = self._status('error_abstain', datetime.now(UTC), error_type=type(exc).__name__)
                if once:
                    return result
            print(encoded(result), flush=True)
            if once or result['status'] == 'stopped':
                return result
            time.sleep(self.policy['poll_seconds'])


def health(state_dir, max_age_seconds=180):
    value = json.loads((Path(state_dir)/'heartbeat.json').read_text())
    age = (datetime.now(UTC)-datetime.fromisoformat(value['observed_at'])).total_seconds()
    normal = {'observed','waiting_for_bar','warming_up','market_closed','unsupported_early_close'}
    return {'healthy':0 <= age <= max_age_seconds and value['status'] in normal,
            'heartbeat_age_seconds':age, 'status':value['status']}
