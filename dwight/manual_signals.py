"""Private, read-only QQQ signal observations and durable manual proposals.

Alpaca is used only for market-data reads. Entry price and share count are human
references, never fills, executable quotes, or account-aware sizing. This module
has no broker account or order transport, trained model, or replay portfolio.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_EVEN, localcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from zoneinfo import ZoneInfo

from vwap_bot.engine import Bar, Config
from .data import exchange_sessions, fetch_alpaca_bars, normalize_minutes
from .manual import (ACCOUNT, PROPOSAL_FIELDS, ManualPaperError, ManualPaperJournal, _decimal,
                     _json, _proposal, _stamp, _text)
from . import signals


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
SETTLE_SECONDS = 60
MAX_AGE_SECONDS = 120
PRICE_MAX_AGE_SECONDS = 30
SOURCE = "vwap_pullback_signal_observer"
MODEL = "none-baseline"
SAFETY = {
    "submits_orders": False,
    "human_review_required": True,
    "account_risk_verified": False,
    "portfolio_risk_enforced": False,
    "portfolio_gates_applied": False,
    "risk_notice": "Human review required: account and portfolio risk are not verified or enforced; no orders are submitted.",
}
SIGNAL_FIELDS = {
    "direction", "stop", "reason", "signal_time", "available_at", "feature_version",
    "features", "accepted", "rejection_reason", "expires_at", "eligible",
    "stale_or_catchup", "signal_age_seconds",
}


class ManualSignalError(ValueError):
    """Sanitized signal-observation or proposal error; no order was submitted."""


def _clock(value=None):
    value = datetime.now(UTC) if value is None else value
    if not isinstance(value, datetime):
        raise ManualSignalError("now must be a timezone-aware datetime")
    return datetime.fromisoformat(_stamp(value.isoformat(), "now"))


def _timestamp(value, field):
    if isinstance(value, datetime):
        value = value.isoformat()
    return _stamp(value, field)


def _safe_path(value):
    """Reject traversal and symbolic links, including existing ancestors."""
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise ManualSignalError("private paths cannot contain parent traversal")
    path = path.absolute()
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise ManualSignalError("private paths cannot contain symbolic links")
        if component.exists() and component != path and not component.is_dir():
            raise ManualSignalError("private path parent must be a directory")
    return path


def _private_directory(path, *, tighten=True):
    path = _safe_path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if not path.is_dir():
        raise ManualSignalError("state directory must be a directory")
    if tighten:
        os.chmod(path, 0o700)
    return path


def _private_file(path):
    _safe_path(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        os.close(fd)
        raise ManualSignalError("private state files must be regular files without hard links")
    os.fchmod(fd, 0o600)
    return fd


def _bar_payload(bar):
    return _json({"timestamp": _timestamp(bar.timestamp, "bar timestamp"),
                  **{key: float(getattr(bar, key))
                     for key in ("open", "high", "low", "close", "volume")}})


def _canonical_stop(raw, tick):
    """Remove float multiplication noise without changing the engine tick level."""
    if isinstance(raw, bool) or len(str(raw)) > 80:
        raise ManualSignalError("signal stop is invalid")
    try:
        value = Decimal(str(raw))
        if not value.is_finite() or not 0 < value <= Decimal("1e12"):
            raise ValueError
        with localcontext() as context:
            context.prec = 80
            rounded = (value / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick
            tolerance = min(tick / 1000, max(Decimal("1e-10"), abs(value) * Decimal("1e-14")))
            if abs(rounded - value) > tolerance:
                raise ValueError
        return _decimal(rounded, "stop")
    except (ValueError, ArithmeticError):
        raise ManualSignalError("signal stop is not on the baseline tick grid") from None


class ManualSignalStore:
    """One locked writer, frozen completed bars, and an idempotent local outbox.

    A state directory is permanently tied to its feed and baseline identity.
    Every signal's initial observation and expiry remain fixed across restarts.
    A data revision halts new proposals permanently until separately reviewed.
    """

    def __init__(self, state_dir, feed="sip", config=Config()):
        if feed not in {"sip", "iex"}:
            raise ManualSignalError("feed must be sip or iex; symbol is QQQ")
        if not isinstance(config, Config) or config != Config():
            raise ManualSignalError("manual signal observation supports default baseline rules only")
        self.feed, self.config, self.symbol = feed, config, "QQQ"
        self.identity = signals.strategy_identity(config)
        self.db = self.lock_fd = None
        try:
            self.state_dir = _private_directory(state_dir)
            self.lock_fd = _private_file(self.state_dir / "manual-signals.lock")
            self.path = self.state_dir / "manual-signals.sqlite3"
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = _safe_path(str(self.path) + suffix)
                if sidecar.exists():
                    os.close(_private_file(sidecar))
            os.close(_private_file(self.path))
            self.db = sqlite3.connect(self.path, timeout=5)
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            schema = """
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bars(
                    kind TEXT NOT NULL, timestamp TEXT NOT NULL, session TEXT NOT NULL,
                    payload TEXT NOT NULL, first_observed_at TEXT NOT NULL,
                    PRIMARY KEY(kind,timestamp));
                CREATE TABLE IF NOT EXISTS signals(
                    signal_id TEXT PRIMARY KEY, signal_time TEXT NOT NULL,
                    payload TEXT NOT NULL, first_observed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(
                    sequence INTEGER PRIMARY KEY, observed_at TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                    signal_id TEXT PRIMARY KEY REFERENCES signals(signal_id),
                    destination TEXT NOT NULL, payload TEXT NOT NULL,
                    reference TEXT NOT NULL, planned_at TEXT NOT NULL,
                    delivered_at TEXT, acknowledgement TEXT);
            """
            expected = _json({"schema": 1, "feed": feed, "symbol": self.symbol,
                              "identity": self.identity, "config": asdict(config)})
            with self.db:
                # SQLite serializes this brief initialization transaction; the
                # observer may hold its operation lock while fetching data.
                self.db.execute("BEGIN IMMEDIATE")
                for statement in schema.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                old = self._meta("identity")
                if old is not None and old != expected:
                    raise ManualSignalError("state directory belongs to a different feed or strategy identity")
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES('identity',?)", (expected,))
            if self.db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ManualSignalError("private signal state integrity check failed")
        except ManualSignalError:
            self.close()
            raise
        except (OSError, sqlite3.DatabaseError):
            self.close()
            raise ManualSignalError("private signal state could not be opened securely") from None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @contextmanager
    def _writer(self):
        if self.lock_fd is None:
            raise ManualSignalError("manual signal store is closed")
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ManualSignalError("another manual signal operation is active; retry after the current observation or preparation finishes") from None
        try:
            yield
        finally:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def _check_identity(self):
        if signals.strategy_identity(self.config) != self.identity:
            raise ManualSignalError("baseline source identity changed; restart and review with a separate state directory")

    def _advance_clock(self, now):
        if self.db is None:
            raise ManualSignalError("manual signal store is closed")
        stamp = _timestamp(now, "now")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self._meta("last_clock")
            if previous is not None and stamp < previous:
                raise ManualSignalError("observation clock cannot move backwards")
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES('last_clock',?)", (stamp,))

    def _status(self, status, now, **details):
        result = {"mode": "manual_signal_observer", "status": status,
                  "observed_at": _timestamp(now, "now"), "strategy_identity": self.identity,
                  "symbol": self.symbol, "feed": self.feed, "model": MODEL,
                  "signals": [], **details, **SAFETY}
        with self.db:
            if status == "error_abstain":
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES('data_error','1')")
            self.db.execute("INSERT INTO observations(observed_at,status,payload) VALUES(?,?,?)",
                            (result["observed_at"], status, _json(result)))
        return result

    def _halt(self, now, stamp, reason):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('halt',?)",
                            (_json({"bar_timestamp": stamp, "reason": reason,
                                    "detected_at": _timestamp(now, "now")}),))
        return self._status("data_revision_requires_review", now,
                            revision=json.loads(self._meta("halt")))

    def _check_previous_minutes(self, rows, session, now):
        old = self.db.execute("SELECT timestamp,payload FROM bars WHERE kind='1m' AND session=?",
                              (session.date,)).fetchall()
        if not old:
            return None
        by_stamp = {}
        for row in rows:
            stamp = _timestamp(row["t"].replace("Z", "+00:00"), "bar timestamp")
            if stamp in by_stamp:
                raise ManualSignalError("duplicate market-data minute; observation abstained")
            by_stamp[stamp] = row
        for stamp, payload in old:
            if stamp not in by_stamp:
                return self._halt(now, stamp, "previously_observed_minute_disappeared")
            row = by_stamp[stamp]
            try:
                if any(isinstance(row[key], bool) for key in ("o", "h", "l", "c", "v")):
                    raise ValueError
                bar = Bar(datetime.fromisoformat(stamp),
                          *(float(row[key]) for key in ("o", "h", "l", "c", "v")))
                current = _bar_payload(bar)
            except (ValueError, TypeError, KeyError, OverflowError):
                return self._halt(now, stamp, "previously_observed_minute_revised")
            if current != payload:
                return self._halt(now, stamp, "previously_observed_minute_revised")
        return None

    def _freshness(self, record, now):
        result = dict(record)
        available = datetime.fromisoformat(record["available_at"])
        expires = datetime.fromisoformat(record["expires_at"])
        age = (now - available).total_seconds()
        halted = self._meta("halt") is not None
        stopped = (self.state_dir / "STOP").exists()
        data_error = self._meta("data_error") is not None
        eligible = (record["initially_eligible"] and record["accepted"]
                    and record["direction"] == 1 and not halted and not stopped and not data_error
                    and SETTLE_SECONDS <= age <= MAX_AGE_SECONDS and now < expires)
        result.update({"eligible": bool(eligible), "actionable": bool(eligible),
                       "signal_age_seconds": age, "stale_or_catchup": not eligible,
                       "expired": now >= expires,
                       "data_revision_requires_review": halted, "stopped": stopped,
                       "latest_observation_abstained": data_error, **SAFETY})
        return result

    def observe(self, now=None):
        with self._writer():
            self._check_identity()
            return self._observe(now)

    def _observe(self, now=None):
        live_clock = now is None
        now = _clock(now)
        self._advance_clock(now)
        if (self.state_dir / "STOP").exists():
            return self._status("stopped", now)
        if self._meta("halt"):
            return self._status("data_revision_requires_review", now,
                                revision=json.loads(self._meta("halt")))
        try:
            day = now.astimezone(NY).date()
            sessions = exchange_sessions(day, day)
            if not sessions or now < sessions[0].open or now >= sessions[0].close:
                return self._status("market_closed", now)
            session = sessions[0]
            if session.close - session.open != timedelta(minutes=390):
                return self._status("unsupported_early_close", now)
            as_of = now - timedelta(seconds=SETTLE_SECONDS)
            if as_of < session.open + timedelta(minutes=5):
                return self._status("warming_up", now)
            rows = fetch_alpaca_bars(session.open, now, (self.symbol,), self.feed)
            if live_clock:
                # Network retries can take minutes. First observation means
                # receipt time, never the earlier request-start timestamp.
                now = _clock()
                self._advance_clock(now)
                as_of = now - timedelta(seconds=SETTLE_SECONDS)
            if not isinstance(rows, dict) or set(rows) != {self.symbol}:
                raise ManualSignalError("unexpected market-data symbol")
            revision = self._check_previous_minutes(rows[self.symbol], session, now)
            if revision:
                return revision
            minutes, bars = normalize_minutes(rows[self.symbol], sessions,
                                             require_complete_session=False, as_of=as_of)
            fresh = []
            for kind, values in (("1m", minutes), ("5m", bars)):
                existing = dict(self.db.execute("SELECT timestamp,payload FROM bars WHERE kind=? AND session=?",
                                                (kind, session.date)).fetchall())
                stamps = set()
                for bar in values:
                    stamp, payload = _timestamp(bar.timestamp, "bar timestamp"), _bar_payload(bar)
                    stamps.add(stamp)
                    if stamp in existing and existing[stamp] != payload:
                        return self._halt(now, stamp, "previously_observed_bar_revised")
                    if stamp not in existing:
                        fresh.append((kind, stamp, session.date, payload, _timestamp(now, "now")))
                missing = set(existing) - stamps
                if missing:
                    return self._halt(now, min(missing), "previously_observed_bar_disappeared")
            inspection = signals.inspect_session(bars, session, now=now, config=self.config,
                                                 settle_seconds=SETTLE_SECONDS,
                                                 max_age_seconds=MAX_AGE_SECONDS)
            new_signals = []
            with self.db:
                self.db.executemany("INSERT INTO bars VALUES(?,?,?,?,?)", fresh)
                for candidate in inspection["signals"]:
                    record = {key: candidate[key] for key in SIGNAL_FIELDS}
                    for field in ("signal_time", "available_at", "expires_at"):
                        record[field] = _timestamp(record[field], field)
                    signal_id = hashlib.sha256(_json([self.identity, self.feed, self.symbol,
                                                     record["signal_time"]]).encode()).hexdigest()
                    if self.db.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone():
                        continue
                    record.update({"signal_id": signal_id, "symbol": self.symbol,
                                   "feed": self.feed, "source": SOURCE, "model": MODEL,
                                   "strategy_identity": self.identity,
                                   "first_observed_at": _timestamp(now, "now"),
                                   "initially_eligible": bool(record["eligible"]),
                                   "first_signal_age_seconds": record["signal_age_seconds"],
                                   "session": session.as_dict(), "strategy": asdict(self.config),
                                   "provenance": {"provider": "alpaca", "feed": self.feed,
                                                  "symbol": self.symbol, "adjustment": "raw",
                                                  "bar_interval": "5m", "timestamp_label": "start",
                                                  "settle_seconds": SETTLE_SECONDS,
                                                  "max_age_seconds": MAX_AGE_SECONDS,
                                                  "baseline_only": True}, **SAFETY})
                    self.db.execute("INSERT INTO signals VALUES(?,?,?,?)",
                                    (signal_id, record["signal_time"], _json(record), record["first_observed_at"]))
                    new_signals.append(signal_id)
                self.db.execute("DELETE FROM metadata WHERE key='data_error'")
            status = inspection["status"]
            if not fresh and status in {"observed", "ready"}:
                status = "waiting_for_bar"
            return self._status(status, now, latest_bar_close=inspection.get("latest_bar_close"),
                                new_bars=sum(row[0] == "5m" for row in fresh),
                                new_signals=new_signals,
                                signals=self._list(now, limit=100))
        except (ValueError, RuntimeError, OSError, TypeError, KeyError, OverflowError, sqlite3.DatabaseError) as exc:
            # Never copy exception messages: provider responses may contain private text.
            return self._status("error_abstain", now, error_type=type(exc).__name__,
                                error="Market data or signal validation failed; no proposal was created.")

    def _list(self, now, limit):
        rows = self.db.execute("SELECT payload FROM signals ORDER BY signal_time DESC,signal_id LIMIT ?",
                               (limit,)).fetchall()
        return [self._freshness(json.loads(row[0]), now) for row in rows]

    def list_signals(self, now=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ManualSignalError("limit must be an integer from 1 to 10000")
        now = _clock(now)
        self._advance_clock(now)
        return self._list(now, limit)

    def _acknowledge(self, signal_id, proposal, now):
        """Separate durable acknowledgement boundary, also useful for crash recovery."""
        with self.db:
            self.db.execute("UPDATE outbox SET delivered_at=COALESCE(delivered_at,?),acknowledgement=? WHERE signal_id=?",
                            (_timestamp(now, "now"), _json(proposal), signal_id))

    def prepare(self, signal_id, *, entry, quantity, price_observed_at, journal_path, now=None):
        """Plan once, then deliver the identical payload to the manual journal.

        Stale references can only recover an acknowledgement for an exact
        proposal already in the journal, never create a first delivery. The
        signal and reference freshness flags are recomputed on every retry.
        """
        try:
            with self._writer():
                self._check_identity()
                return self._prepare(signal_id, entry=entry, quantity=quantity,
                                     price_observed_at=price_observed_at,
                                     journal_path=journal_path, now=now)
        except ManualSignalError:
            raise
        except ManualPaperError as exc:
            # Manual validation messages contain field names, never provider data.
            raise ManualSignalError(str(exc)) from None
        except (OSError, sqlite3.DatabaseError):
            raise ManualSignalError("manual proposal delivery failed; retry the identical parameters and destination") from None

    def _prepare(self, signal_id, *, entry, quantity, price_observed_at, journal_path, now):
        live_clock = now is None
        now = _clock(now)
        self._advance_clock(now)
        if (self.state_dir / "STOP").exists():
            raise ManualSignalError("manual signal preparation is stopped; human review required")
        if self._meta("halt"):
            raise ManualSignalError("data_revision_requires_review: manual signal preparation is halted")
        if self._meta("data_error"):
            raise ManualSignalError("latest market-data observation abstained; a successful data recheck and human review are required")
        if not isinstance(signal_id, str) or len(signal_id) != 64:
            raise ManualSignalError("unknown signal ID")
        row = self.db.execute("SELECT payload FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
        if row is None:
            raise ManualSignalError("unknown signal ID")
        record = json.loads(row[0])
        entry, quantity = _decimal(entry, "entry"), _decimal(quantity, "quantity")
        if quantity != quantity.to_integral_value():
            raise ManualSignalError("quantity must be a positive whole number of shares")
        observed = _timestamp(price_observed_at, "price_observed_at")
        price_age = (now - datetime.fromisoformat(observed)).total_seconds()
        price_fresh = 0 <= price_age < PRICE_MAX_AGE_SECONDS
        destination = _safe_path(journal_path)
        same_parent = (destination.parent == self.state_dir
                       or (destination.parent.exists()
                           and os.path.samefile(destination.parent, self.state_dir)))
        reserved = {self.path.name.casefold(), "manual-signals.lock", "stop",
                    *(self.path.name.casefold() + suffix for suffix in ("-journal", "-wal", "-shm"))}
        if same_parent and destination.name.casefold() in reserved:
            raise ManualSignalError("journal destination cannot be a reserved signal state, SQLite sidecar, lock, or STOP file")
        reference = {"entry": _text(entry), "quantity": _text(quantity),
                     "price_observed_at": observed, "price_basis": "human_supplied_reference",
                     "quote_verified": False, "quantity_basis": "human_supplied_whole_shares"}
        outbox = self.db.execute("SELECT destination,payload,reference,planned_at,delivered_at FROM outbox WHERE signal_id=?",
                                 (signal_id,)).fetchone()
        if outbox:
            if outbox[0] != str(destination) or outbox[2] != _json(reference):
                raise ManualSignalError("prepared signal conflicts with the original entry reference, quantity, timestamp, or journal destination")
            payload, planned_at = json.loads(outbox[1]), outbox[3]
        else:
            current = self._freshness(record, now)
            if record["direction"] != 1:
                raise ManualSignalError("manual proposals require a long signal")
            if not current["eligible"]:
                raise ManualSignalError("signal is unavailable, rejected, stale, expired, or halted; human review required")
            if not price_fresh:
                raise ManualSignalError("human entry reference must be less than 30 seconds old and not in the future")
            tick, reward = Decimal(str(self.config.tick)), Decimal(str(self.config.reward_r))
            stop = _canonical_stop(record["stop"], tick)
            if entry - stop <= tick:
                raise ManualSignalError("long entry must exceed the signal stop by more than one tick")
            with localcontext() as context:
                context.prec = 80
                target = ((entry + reward * (entry - stop)) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            payload = _proposal({"proposal_id": "signal-" + signal_id, "account": ACCOUNT,
                                 "symbol": self.symbol, "side": "buy", "quantity": _text(quantity),
                                 "entry": _text(entry), "stop": _text(stop), "target": _text(target),
                                 "source": SOURCE, "model": MODEL, "version": self.identity,
                                 "signal_at": record["signal_time"], "available_at": record["available_at"],
                                 "expires_at": record["expires_at"]})
            planned_at = _timestamp(now, "now")
            # Commit intent before crossing the journal's separate transaction.
            with self.db:
                self.db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,NULL,NULL)",
                                (signal_id, str(destination), _json(payload), _json(reference), planned_at))
        # Recheck destination safety on every retry, including parent components.
        if not price_fresh and not destination.exists():
            raise ManualSignalError("stale entry reference cannot make a first delivery; only an existing matching journal proposal can be recovered")
        _private_directory(destination.parent, tighten=False)
        _safe_path(destination)
        if destination.exists() and (not destination.is_file() or destination.stat().st_nlink != 1):
            raise ManualSignalError("journal destination must be a regular file without hard links")
        for suffix in ("-journal", "-wal", "-shm"):
            _safe_path(str(destination) + suffix)
        journal = ManualPaperJournal(destination)
        if live_clock:
            # Opening a busy journal can wait for another SQLite writer. Never
            # reuse the request-start clock to validate a post-wait reference.
            now = _clock()
            self._advance_clock(now)
            price_age = (now - datetime.fromisoformat(observed)).total_seconds()
            price_fresh = 0 <= price_age < PRICE_MAX_AGE_SECONDS
        if price_fresh:
            deadline = min(datetime.fromisoformat(payload["expires_at"]),
                           datetime.fromisoformat(observed) + timedelta(seconds=PRICE_MAX_AGE_SECONDS))
            # The journal samples a live clock after obtaining its transaction
            # lock and enforces this deadline on first insertion only.
            proposal = journal.add_proposal(payload, now=None if live_clock else now,
                                            create_before=deadline.isoformat())
        else:
            proposal = next((item for item in journal.list_proposals(now=None if live_clock else now)
                             if item["proposal_id"] == payload["proposal_id"]), None)
            if proposal is None:
                raise ManualSignalError("stale entry reference cannot make a first delivery; only an existing matching journal proposal can be recovered")
            if _proposal({key: proposal[key] for key in PROPOSAL_FIELDS}) != payload:
                raise ManualSignalError("proposal ID conflicts with existing journal content")
        if live_clock:
            now = _clock()
            self._advance_clock(now)
        self._acknowledge(signal_id, proposal, now)
        if live_clock:
            # Include time spent committing the durable acknowledgement in
            # returned actionability, without renewing the original deadline.
            now = _clock()
        price_age = (now - datetime.fromisoformat(observed)).total_seconds()
        price_fresh = 0 <= price_age < PRICE_MAX_AGE_SECONDS
        current = self._freshness(record, now)
        return {"status": "delivered", "signal_id": signal_id, "proposal": proposal,
                "journal_path": str(destination), "prepared_at": planned_at,
                "reference": {**reference, "canonical_stop": payload["stop"],
                              "original_signal_stop": record["stop"]}, "provenance": record,
                "actionable": bool(current["eligible"] and price_fresh and proposal["status"] == "pending"),
                "price_reference_fresh": price_fresh, "price_reference_age_seconds": price_age,
                "expired": current["expired"], "idempotent_retry": outbox is not None, **SAFETY}
