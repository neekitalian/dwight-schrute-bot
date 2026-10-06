"""Private single-operator QQQ paper pilot, separate from the public portal.

Only the broker worker writes execution state. Market-data observation runs in
a separate thread so provider retries cannot suspend order supervision. Starting
the process is read-only until an explicit console authorization and start.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import sqlite3
import threading
from uuid import uuid4

from .authorization import AuthorizationError, canonical, utc
from .manual_signals import ManualSignalStore
from .paper import OrderProposal, PaperError, RiskPolicy, _decimal
from .signals import strategy_identity
from vwap_bot.engine import Config

STRATEGY_ID = "qqq_vwap_5m"
AUTHORIZE_FIELDS = {"account_id", "allocation_usd", "max_order_notional_usd",
                    "daily_loss_limit_usd", "max_stop_risk_usd", "expires_at", "consent"}


def clock():
    return datetime.now(timezone.utc)


def private_directory(value):
    path = Path(value).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Paper workspace must not contain symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_private(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Private state must not be a symbolic link")
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def init_paper_workspace(directory, feed="sip"):
    if feed not in {"sip", "iex"}:
        raise ValueError("Select sip or iex explicitly")
    root = private_directory(directory)
    config = {"schema_version": 1, "provider": "alpaca", "mode": "paper",
              "symbol": "QQQ", "feed": feed, "strategy_id": STRATEGY_ID,
              "strategy_revision": strategy_identity(Config())}
    path = root / "config.json"
    if path.exists():
        if path.is_symlink() or json.loads(path.read_text()) != config:
            raise ValueError("Workspace belongs to another configuration; do not overwrite it")
    else:
        write_private(path, config)
    with ControlLedger(root / "controls.sqlite3"):
        pass
    return {"status": "prepared", "directory": str(root), "feed": feed,
            "mode": "paper", "entries_enabled": False, "account_connected": False}


class ControlLedger:
    """Persist authenticated local console requests; queuing is not execution."""

    def __init__(self, path):
        path = Path(path)
        private_directory(path.parent)
        if path.is_symlink():
            raise ValueError("Control ledger cannot be a symbolic link")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=3)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,action TEXT NOT NULL,
              payload TEXT NOT NULL,created_at TEXT NOT NULL,state TEXT NOT NULL,result TEXT);
            CREATE TABLE IF NOT EXISTS runtime(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(signal_id TEXT PRIMARY KEY,
              observed_at TEXT NOT NULL,state TEXT NOT NULL,detail TEXT NOT NULL);
        """)
        self.db.commit()

    def __enter__(self): return self
    def __exit__(self, *_): self.db.close()

    def enqueue(self, action, payload, now=None):
        if not isinstance(payload, dict):
            raise ValueError("Command payload must be an object")
        if action == "authorize":
            if set(payload) != AUTHORIZE_FIELDS or payload["consent"] is not True:
                raise ValueError("Explicit complete paper scope consent is required")
            if not isinstance(payload["account_id"], str) or not 1 <= len(payload["account_id"]) <= 128:
                raise ValueError("A verified paper account identifier is required")
            for field in AUTHORIZE_FIELDS - {"account_id", "expires_at", "consent"}:
                if type(payload[field]) not in (int, float) or not math.isfinite(payload[field]) or payload[field] <= 0:
                    raise ValueError("Limits must be finite positive numbers")
            if payload["max_order_notional_usd"] > payload["allocation_usd"]:
                raise ValueError("Order limit must not exceed allocation")
            stamp = utc(now or clock())
            expiry = utc(payload["expires_at"])
            if not stamp < expiry <= stamp + timedelta(days=7):
                raise ValueError("Authorization must expire within seven days")
        elif action == "start":
            if set(payload) != {"authorization_id"} or not isinstance(payload["authorization_id"], str):
                raise ValueError("Start requires a recorded authorization")
        elif action == "pause":
            if payload:
                raise ValueError("Pause has no settings")
        else:
            raise ValueError("Unsupported paper command")
        cid = uuid4().hex
        with self.db:
            pending = self.db.execute("SELECT count(*) FROM commands WHERE state='queued'").fetchone()[0]
            if pending >= 100:
                raise ValueError("Too many pending commands")
            self.db.execute("INSERT INTO commands VALUES(?,?,?,?,?,NULL)",
                            (cid, action, canonical(payload), (now or clock()).isoformat(), "queued"))
        return {"command_id": cid, "status": "queued"}

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime VALUES(?,?)", (key, canonical(value)))

    def decide(self, sid, state, detail, now):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?,?,?)",
                            (sid, now.isoformat(), state, canonical(detail)))


class ObservationThread(threading.Thread):
    def __init__(self, root, feed):
        super().__init__(daemon=True, name="dwight-market-observer")
        self.root, self.feed = root, feed
        self.stop_event = threading.Event()
        self.latest = queue.Queue(maxsize=1)

    def run(self):
        try:
            with ManualSignalStore(self.root / "observations", self.feed) as observer:
                while not self.stop_event.is_set():
                    status = observer.observe()
                    item = {"received_at": clock().isoformat(), "status": status,
                            "signals": observer.list_signals()}
                    try: self.latest.get_nowait()
                    except queue.Empty: pass
                    self.latest.put_nowait(item)
                    self.stop_event.wait(10)
        except Exception:
            # Provider exception text and payloads can contain private material.
            try: self.latest.get_nowait()
            except queue.Empty: pass
            self.latest.put_nowait({"received_at": clock().isoformat(),
                                   "status": {"status": "observer_failed"}, "signals": []})


def proposal_for(record, quote, snapshot, policy, allocation, revision, now):
    """Convert one causal observation into a cash-funded, scoped candidate."""
    if (record.get("symbol") != "QQQ" or record.get("feed") != policy.expected_feed
            or record.get("strategy_identity") != revision or record.get("direction") != 1
            or record.get("eligible") is not True or record.get("initially_eligible") is not True
            or not utc(record["available_at"]) <= now < utc(record["expires_at"])):
        raise ValueError("Candidate is ineligible, expired or has different provenance")
    entry = Decimal(str(quote.ask)).quantize(Decimal(".01"), rounding=ROUND_CEILING)
    stop = Decimal(str(record["stop"])).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
    distance = entry - stop
    if distance <= Decimal(".01"):
        raise ValueError("Candidate has no valid stop distance")
    cash = _decimal(snapshot["account"].get("cash"), "cash", positive=True)
    cap = min(cash, _decimal(allocation, "allocation", positive=True), Decimal(str(policy.max_order_notional)),
              Decimal(str(policy.max_position_notional)), Decimal(str(policy.max_gross_notional)))
    qty = int(min(cap / entry, Decimal(str(policy.max_stop_risk)) / distance).to_integral_value(rounding=ROUND_FLOOR))
    if qty < 1:
        raise ValueError("Limits do not permit a whole QQQ share")
    target = (entry + Decimal(str(Config().reward_r)) * distance).quantize(Decimal(".01"), rounding=ROUND_CEILING)
    return OrderProposal(record["signal_id"], "QQQ", qty, float(entry), float(stop), float(target), STRATEGY_ID, revision)


class PaperWorker:
    def __init__(self, root, executor, market, controls, config):
        self.root, self.executor, self.market, self.controls, self.config = root, executor, market, controls, config
        self.observation = None
        self.snapshot = None

    def _policy(self, payload):
        allocation = float(payload["allocation_usd"])
        return RiskPolicy(max_order_notional=float(payload["max_order_notional_usd"]),
                          max_position_notional=allocation, max_gross_notional=allocation,
                          max_daily_loss=float(payload["daily_loss_limit_usd"]),
                          max_stop_risk=float(payload["max_stop_risk_usd"]), expected_feed=self.config["feed"])

    def _commands(self, now, ready):
        # Requests interrupted mid-processing are uncertain; never activate them on restart.
        with self.controls.db:
            self.controls.db.execute("UPDATE commands SET state='failed',result=? WHERE state='processing'",
                                     (canonical({"error": "Interrupted command; review status before retry"}),))
        rows = self.controls.db.execute("SELECT * FROM commands WHERE state='queued' ORDER BY created_at,id").fetchall()
        for row in rows:
            with self.controls.db:
                self.controls.db.execute("UPDATE commands SET state='processing' WHERE id=?", (row["id"],))
            try:
                payload = json.loads(row["payload"])
                action = row["action"]
                if action == "pause":
                    self.controls.set("enabled", False)
                    aid = self.controls.get("authorization_id")
                    if aid:
                        self.executor.authorizations.set_state(aid, "paused", now=now)
                    result = {"state": "paused", "existing_exposure_supervised": True}
                elif action == "authorize":
                    if not ready or self.snapshot["account"]["id"] != payload["account_id"]:
                        raise ValueError("Verify the selected paper account before authorizing")
                    if utc(payload["expires_at"]) <= now:
                        raise ValueError("Requested authorization has expired")
                    if self.snapshot["positions"] or self.snapshot["open_orders"]:
                        raise ValueError("Change authorization while flat with no open orders")
                    policy = self._policy(payload)
                    aid = "console-" + row["id"]
                    spec = {"authorization_id": aid, "granted_by": "local-console", "provider": "alpaca",
                            "account_mode": "paper", "account_id": payload["account_id"],
                            "strategy_id": STRATEGY_ID, "strategy_revision": self.config["strategy_revision"],
                            "symbols": ["QQQ"], "side": "buy", "allocation_usd": payload["allocation_usd"],
                            "risk_policy": json.loads(canonical(asdict(policy))), "expires_at": payload["expires_at"]}
                    old = self.controls.get("authorization_id")
                    if old:
                        self.executor.authorizations.set_state(old, "revoked", now=now)
                    self.controls.set("enabled", False)
                    self.executor.authorizations.approve(spec, now=now)
                    self.controls.set("authorization_id", aid)
                    self.executor.policy = policy
                    result = {"authorization_id": aid, "state": "authorized", "entries_enabled": False}
                elif action == "start":
                    aid = self.controls.get("authorization_id")
                    if not ready or payload["authorization_id"] != aid:
                        raise ValueError("Fresh preflight and the current authorization are required")
                    grant = self.executor.authorizations.get(aid)
                    if utc(grant["scope"]["expires_at"]) <= now:
                        raise ValueError("Authorization has expired")
                    if grant["state"] == "paused":
                        self.executor.authorizations.set_state(aid, "active", now=now)
                    elif grant["state"] != "active":
                        raise ValueError("Authorization is not active")
                    self.controls.set("enabled", True)
                    self.controls.set("started_at", now.isoformat())
                    result = {"state": "running", "mode": "paper"}
                else:
                    raise ValueError("Unsupported command")
                state = "completed"
            except (ValueError, PaperError, KeyError, TypeError):
                # Reject without leaking provider errors or user-supplied strings.
                state, result = "failed", {"error": "Command rejected; check account readiness and authorization"}
            with self.controls.db:
                self.controls.db.execute("UPDATE commands SET state=?,result=? WHERE id=?", (state, canonical(result), row["id"]))

    def tick(self, now=None, *, read_only=False):
        fixture_clock = now is not None
        now = now or clock()
        failures, supervision = [], None
        try:
            supervision = ({"entries_allowed": False, "mode": "read_only_preflight"}
                           if read_only else self.executor.supervise(**({"now": now} if fixture_clock else {})))
            self.snapshot = self.executor.reconcile()
            asset = self.executor.client.get_asset()
            if asset.get("symbol") != "QQQ" or asset.get("class") != "us_equity" or asset.get("tradable") is not True or asset.get("status") != "active":
                raise ValueError("QQQ trading permission failed")
            account = self.snapshot["account"]
            if account.get("status") != "ACTIVE" or account.get("trading_blocked") is not False or account.get("account_blocked") is not False:
                raise ValueError("Account is blocked")
            ready = True
        except (PaperError, ValueError, KeyError, TypeError, OSError):
            ready = False
            failures.append("Broker state is not ready; entries blocked")
        feed_access = False
        if ready:
            try:
                self.market.latest_quote(self.config["feed"])
                feed_access = True
            except (PaperError, ValueError, KeyError, TypeError, OSError):
                failures.append("Selected quote feed is unavailable; entries blocked")
        if not fixture_clock:
            now = clock()
        if not read_only:
            self._commands(now, ready and feed_access)
        aid = self.controls.get("authorization_id")
        grant = None
        if aid:
            try:
                grant = self.executor.authorizations.get(aid)
                policy_values = dict(grant["scope"]["risk_policy"])
                policy_values["allowed_symbols"] = tuple(policy_values["allowed_symbols"])
                self.executor.policy = RiskPolicy(**policy_values)
                if not utc(grant["approved_at"]) <= now < utc(grant["scope"]["expires_at"]):
                    failures.append("Authorization expired; entries blocked")
                    self.controls.set("enabled", False)
            except (ValueError, KeyError, TypeError):
                failures.append("Authorization is invalid; entries blocked")
                self.controls.set("enabled", False)
        observation = self.observation
        data_ready = False
        if observation:
            data_ready = bool(0 <= (now - utc(observation["received_at"])).total_seconds() <= 45
                              and observation["status"].get("status") in {"ready", "observed", "waiting_for_bar", "market_closed", "warming_up"})
        if not data_ready:
            failures.append("Market observation is not fresh; entries blocked")
        enabled = self.controls.get("enabled", False)
        if not read_only and ready and feed_access and data_ready and enabled and grant and grant["state"] == "active" and supervision.get("entries_allowed", False):
            for record in observation["signals"]:
                if record.get("eligible") is not True:
                    continue
                sid = record["signal_id"]
                if self.controls.db.execute("SELECT 1 FROM decisions WHERE signal_id=?", (sid,)).fetchone():
                    continue
                checked_at = now if now is not None else clock()
                try:
                    quote = self.market.latest_quote(self.config["feed"])
                    checked_at = clock() if not getattr(self.market, "fixture_clock", False) else now
                    proposal = proposal_for(record, quote, self.snapshot, self.executor.policy,
                                            grant["scope"]["allocation_usd"], self.config["strategy_revision"], checked_at)
                    # Account/risk checks are repeated inside the durable executor.
                    submission_clock = {"now": checked_at} if getattr(self.market, "fixture_clock", False) else {}
                    order = self.executor.submit(proposal, quote, authorization_id=aid,
                                                 signal_expires_at=record["expires_at"], **submission_clock)
                    self.controls.decide(sid, "submitted", {"broker_order_id": order.get("id")}, checked_at)
                except (PaperError, ValueError, KeyError, TypeError, OSError):
                    self.controls.decide(sid, "skipped", {"reason": "Freshness, risk or broker validation failed"}, checked_at)
        scope = grant["scope"] if grant else None
        authorization = None if not grant else {"id": aid, "state": grant["state"],
                            "allocation_usd": scope["allocation_usd"], "expires_at": scope["expires_at"],
                            "risk_policy": scope["risk_policy"]}
        account = self.snapshot["account"] if ready else {}
        state = "preflight" if read_only else "paused" if not enabled else "blocked" if failures else "waiting_for_session" if not supervision.get("market_open", True) else "running"
        status = {"updated_at": now.isoformat(), "state": state,
                  "account": {"provider": "alpaca", "mode": "paper", "id": account.get("id")},
                  "strategy": {"id": STRATEGY_ID, "revision": self.config["strategy_revision"], "symbol": "QQQ", "timeframe": "5m", "feed": self.config["feed"]},
                  "readiness": {"ready": ready and feed_access and data_ready and not failures, "failures": failures},
                  "authorization": authorization, "risk_policy": json.loads(canonical(asdict(self.executor.policy))),
                  "equity_usd": account.get("equity"), "pnl_usd": None,
                  "positions": self.snapshot["positions"] if ready else [],
                  "orders": self.snapshot["open_orders"] if ready else [],
                  "health": {"worker": state, "broker": "connected" if ready else "unknown", "data": "current" if data_ready else "waiting", "feed_access": feed_access,
                             "supervision": supervision, "verification": "paper pilot; real fills must be verified"},
                  "commands": [dict(row) for row in self.controls.db.execute("SELECT id,action,state,result FROM commands ORDER BY created_at DESC LIMIT 10")]}
        write_private(self.root / "status.json", status)
        return status


def run_paper_worker(directory, *, once=False):
    from .paper_market import HardenedAlpacaPaperClient, PaperMarket
    from .paper_supervision import SupervisedPaperExecutor
    root = private_directory(directory)
    for name in ("config.json", "execution.sqlite3", "execution.sqlite3.lock", "status.json"):
        if (root / name).is_symlink():
            raise ValueError("Paper workspace files cannot be symbolic links")
    config = json.loads((root / "config.json").read_text())
    expected = {"schema_version": 1, "provider": "alpaca", "mode": "paper", "symbol": "QQQ",
                "feed": config.get("feed"), "strategy_id": STRATEGY_ID,
                "strategy_revision": strategy_identity(Config())}
    if config != expected or config["feed"] not in {"sip", "iex"}:
        raise ValueError("Frozen workspace configuration does not match this worker")
    old_umask = os.umask(0o077)
    account_lock = None
    observer = None
    try:
        client = HardenedAlpacaPaperClient.from_env()
        account = client.get_account()
        account_id = account.get("id")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("Paper account identity could not be verified")
        locks = private_directory(os.environ.get("DWIGHT_ACCOUNT_LOCK_DIR", str(Path.home() / ".dwight" / "paper-account-locks")))
        lock_name = hashlib.sha256(("alpaca-paper:" + account_id).encode()).hexdigest() + ".lock"
        account_lock = (locks / lock_name).open("a+")
        fcntl.flock(account_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with SupervisedPaperExecutor(client, root / "execution.sqlite3", RiskPolicy(expected_feed=config["feed"])) as executor, ControlLedger(root / "controls.sqlite3") as controls:
            worker = PaperWorker(root, executor, PaperMarket.from_env(), controls, config)
            if not once:
                observer = ObservationThread(root, config["feed"])
                observer.start()
            while True:
                if observer:
                    try: worker.observation = observer.latest.get_nowait()
                    except queue.Empty: pass
                worker.tick(read_only=once)
                if once: break
                threading.Event().wait(5)
        return {"status": "checked", "directory": str(root), "mode": "paper"}
    finally:
        if observer: observer.stop_event.set()
        if account_lock: account_lock.close()
        os.umask(old_umask)
