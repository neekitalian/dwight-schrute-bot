"""Causal QQQ baseline signals, without a simulated portfolio or order access.

The observer uses the pinned engine's indicators, confirmed pivots and signal
rules. Its feed deliberately omits portfolio gates: a historical simulated
position, loss count or account equity cannot describe the user's account.
Only completed bars belong here; ``inspect_session`` additionally checks the
calendar session and settlement clock before calling the observer.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path

from vwap_bot import engine
from vwap_bot.engine import Bar, Bot, Config
from .data import NY, Session
from . import experiments

INTERVAL = timedelta(minutes=5)


def _config(config: Config) -> Config:
    """Reject permissive Python booleans and malformed Config instances."""
    if type(config) is not Config:
        raise ValueError("config must be a Config instance")
    values = asdict(config)
    for name, value in values.items():
        if name == "use_ema":
            if type(value) is not bool:
                raise ValueError("use_ema must be a boolean")
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number, not a boolean")
    # Revalidate even if a caller bypassed frozen-dataclass protections.
    return Config(**values)


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _bar(bar):
    if type(bar) is not Bar:
        raise ValueError("bars must contain Bar instances")
    _aware(bar.timestamp, "bar timestamp")
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("OHLCV must be finite numbers, not booleans")
    # Check again at this boundary, including instances mutated by external code.
    Bar(bar.timestamp, *values)


class SignalObserver(Bot):
    """Observe immutable signal-time records using baseline strategy rules.

    ``signals`` returns detached JSON-safe list snapshots. The underlying
    records are immutable JSON strings; callers and later bars cannot add
    outcomes or alter previously captured features. There is no model and no
    equity, positions, fills, trades, or portfolio risk gate in this class.
    """

    def __init__(self, config=Config()):
        self.c = _config(config)
        self.pending = None
        self.last_timestamp = None
        self.day = None
        self._records = ()
        self._reset()

    def _reset(self):
        super()._reset()
        # Bot's indicator reset also creates a simulated loss counter.
        del self.losses

    @property
    def signals(self):
        return [json.loads(record) for record in self._records]

    def feed(self, b):
        _bar(b)
        local = b.timestamp.astimezone(NY)
        if local.weekday() >= 5 or not time(9, 30) <= local.time() <= time(15, 55):
            raise ValueError("Input must contain regular-session bars only")
        if local.minute % 5 or local.second or local.microsecond:
            raise ValueError("Expected start-labelled 5-minute bars")
        if self.last_timestamp and b.timestamp <= self.last_timestamp:
            raise ValueError("Bars must be strictly increasing")
        if local.date() != self.day:
            if local.time() != time(9, 30):
                raise ValueError("Observer requires the full session prefix from its open")
            self._reset()
            self.day = local.date()
        elif b.timestamp - self.last_timestamp != INTERVAL:
            raise ValueError("Missing intraday bars")
        self.last_timestamp = b.timestamp
        self.pending = None
        # These are exactly Bot.feed's completed-bar indicator updates.
        prev = self.bars[-1] if self.bars else b
        self.trs.append(max(b.high-b.low, abs(b.high-prev.close), abs(b.low-prev.close)))
        self.bars.append(b)
        self.pv += (b.high+b.low+b.close) / 3 * b.volume
        self.vol += b.volume
        self.ema = b.close if self.ema is None else self.ema + 2/(self.c.ema_period+1)*(b.close-self.ema)
        if not all(math.isfinite(value) for value in (self.trs[-1], self.pv, self.vol, self.ema)):
            raise ValueError("Nonfinite indicator bookkeeping; refusing a signal")
        self._pivots()
        try:
            if local.time() == time(15, 55):
                self.setup = None
            else:
                # Directly retain the engine's signal rules, without account gates.
                super()._signal(b, prev)
                if self.pending is not None:
                    self._capture(b)
        finally:
            # A signal is never carried into next bar's historical opening price.
            self.pending = None

    def _capture(self, bar):
        pending = self.pending
        direction, stop = pending.get("direction"), pending.get("stop")
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError("Engine signal direction must be -1 or 1")
        if (type(stop) not in (int, float) or not math.isfinite(stop) or stop <= 0
                or direction*(bar.close-stop) <= 0):
            raise ValueError("Engine signal stop must be finite, positive and protective")
        if pending.get("signal_time") != bar.timestamp.isoformat():
            raise ValueError("Engine signal timestamp does not match its observed bar")
        if not isinstance(pending.get("reason"), str) or not pending["reason"].strip():
            raise ValueError("Engine signal must include its reason")
        features = experiments.extract_features(self, pending)
        record = {
            "direction": direction, "stop": stop, "reason": pending["reason"],
            "signal_time": pending["signal_time"],
            "available_at": (bar.timestamp+INTERVAL).isoformat(),
            "feature_version": experiments.FEATURE_VERSION, "features": features,
            "accepted": direction == 1,
            "rejection_reason": None if direction == 1 else "long_only_policy",
        }
        self._records += (json.dumps(record, sort_keys=True, allow_nan=False),)

    def _enter(self, *args, **kwargs):
        raise RuntimeError("SignalObserver cannot enter a simulated or real position")

    def _exit(self, *args, **kwargs):
        raise RuntimeError("SignalObserver cannot exit a simulated or real position")

    def finish(self):
        self.pending = None

    def stats(self):
        raise RuntimeError("SignalObserver has no portfolio statistics")


def _session(session):
    if type(session) is not Session:
        raise ValueError("session must be a calendar Session")
    _aware(session.open, "session open")
    _aware(session.close, "session close")
    Session(session.date, session.open, session.close)
    opened, closed = session.open.astimezone(NY), session.close.astimezone(NY)
    if (opened.weekday() >= 5 or opened.time() != time(9, 30)
            or closed.date() != opened.date() or closed.time() > time(16)
            or closed.second or closed.microsecond):
        raise ValueError("Unsupported regular-session calendar bounds")


def inspect_session(bars, session: Session, *, now: datetime, config=Config(),
                    settle_seconds=60, max_age_seconds=120) -> dict:
    """Inspect a contiguous prefix against a caller-supplied exchange session.

    The caller must obtain the session from the exchange calendar; this pure
    function does not read a CSV, fetch data or persist observations. A prefix
    may lag the clock for historical catch-up, but only the latest theoretically
    completed and settled bar can be eligible. Expiry always starts at bar close,
    never at poll time. Invalid input raises ValueError and emits no result.
    """
    config = _config(config)
    _aware(now, "now")
    _session(session)
    if type(settle_seconds) is not int or not 0 <= settle_seconds <= 300:
        raise ValueError("settle_seconds must be an integer from 0 to 300")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ValueError("max_age_seconds must be an integer from 1 to 3600")
    result = {"status": "ready", "latest_bar_close": None, "signals": []}
    if not session.open <= now < session.close:
        result["status"] = "market_closed"
        return result
    if session.close-session.open != timedelta(minutes=390):
        result["status"] = "unsupported_early_close"
        return result
    bars = list(bars)
    if not bars:
        raise ValueError("A nonempty full session prefix is required")
    cutoff = now-timedelta(seconds=settle_seconds)
    expected = session.open
    for bar in bars:
        _bar(bar)
        if bar.timestamp != expected:
            raise ValueError("Bars must be a continuous full session prefix without gaps or duplicates")
        if not session.open <= bar.timestamp < session.close:
            raise ValueError("Bar is outside the supplied session")
        if bar.timestamp+INTERVAL > cutoff:
            raise ValueError("Bar is unclosed, future or not yet settled")
        expected += INTERVAL
    latest_close = bars[-1].timestamp+INTERVAL
    result["latest_bar_close"] = latest_close.isoformat()
    # Distinguish the most recent supplied row from the bar due by this clock.
    completed_count = max(0, int((cutoff-session.open).total_seconds() // 300))
    due_close = session.open+completed_count*INTERVAL
    observer = SignalObserver(config)
    for bar in bars:
        observer.feed(bar)
    for signal in observer.signals:
        available = datetime.fromisoformat(signal["available_at"])
        expires = min(available+timedelta(seconds=max_age_seconds), session.close)
        timely = available == latest_close == due_close and now < expires
        result["signals"].append({
            **signal, "expires_at": expires.isoformat(),
            "eligible": signal["accepted"] and timely,
            "stale_or_catchup": not timely,
            "signal_age_seconds": (now-available).total_seconds(),
        })
    return result


def strategy_identity(config=Config()) -> str:
    """Hash local source bytes, feature version and all Config values.

    Scope is engine.py, signals.py and experiments.py (which owns feature
    extraction), plus FEATURE_VERSION and serialized Config. It intentionally
    excludes market data, clocks, package version, source/feed and account
    evidence; callers record those separately. Source edits, even comments,
    change identity. There is no model artifact in this baseline identity.
    """
    config = _config(config)
    files = {"vwap_bot/engine.py": Path(engine.__file__),
             "dwight/signals.py": Path(__file__),
             "dwight/experiments.py": Path(experiments.__file__)}
    payload = {
        "scope": "qqq-long-only-signal-observer-v1",
        "source_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for name, path in files.items()},
        "feature_version": experiments.FEATURE_VERSION,
        "config": asdict(config),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()
