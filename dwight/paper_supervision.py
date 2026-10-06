"""Fixture-tested QQQ paper order supervision, independent of strategy inference.

This is a conservative single-account library, not verified broker operation.
Partial entries are canceled, observed terminal, then closed only after all
bracket exits are observed terminal. Every close is persisted before one POST.
The fixed paper client and original executor's exclusive ledger lock are reused.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .paper import (
    ACTIVE_PROTECTION, PAPER_URL, PaperError, PaperExecutor,
    ReconciliationRequired, RiskRejected, _decimal, _json, _time as _paper_time,
)

# Replaced orders can have executable successors we do not own or understand.
FINAL = {"filled", "canceled", "expired", "rejected"}
KNOWN_STATUS = FINAL | {"new", "accepted", "pending_new", "accepted_for_bidding",
                        "partially_filled", "pending_cancel", "held", "stopped",
                        "done_for_day", "suspended", "calculated"}
NEW_YORK = ZoneInfo("America/New_York")


def _time(value):
    try:
        return _paper_time(value)
    except (AttributeError, ValueError, TypeError, OverflowError):
        raise RiskRejected("Supervision timestamps require timezone-aware values") from None


def _quantity(value, label, *, positive=False):
    result = _decimal(value, label, positive=positive)
    if result < 0 or result != result.to_integral_value():
        raise ReconciliationRequired("Supervision requires nonnegative whole-share quantities")
    return result


class SupervisedPaperExecutor(PaperExecutor):
    """Adds owned close intents and restart-safe lifecycle recovery to PaperExecutor.

    Call supervise() on every worker pulse even when entries are paused, data is
    missing or authorization has expired. It may cancel/close owned paper orders;
    reconcile() remains read-only at the broker. Never use the base executor on a
    ledger containing these additional exits, because it cannot account for them.
    """

    def __init__(self, client, db_path, policy=None, *, close_buffer_seconds=300,
                 entry_timeout_seconds=120):
        for value, minimum, maximum in ((close_buffer_seconds, 30, 1800),
                                         (entry_timeout_seconds, 5, 3600)):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError("Supervision intervals must be bounded integer seconds")
        super().__init__(client, db_path, policy)
        self.close_buffer_seconds = close_buffer_seconds
        self.entry_timeout_seconds = entry_timeout_seconds
        self._entry_session = None
        self._entry_expiry = None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS paper_supervision_jobs(
                parent_client_order_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_cancel_attempts(
                broker_order_id TEXT PRIMARY KEY, parent_client_order_id TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_exit_intents(
                client_order_id TEXT PRIMARY KEY,
                parent_client_order_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, broker_order TEXT);
            CREATE TABLE IF NOT EXISTS paper_supervision_fills(
                broker_order_id TEXT PRIMARY KEY, filled_qty TEXT NOT NULL,
                status TEXT NOT NULL);
        """)

    def _mark_blocked(self, reason):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO paper_meta VALUES('blocked',?)", (reason,))
            self._audit("supervision_entries_blocked", {"reason": reason})

    def _guard_client(self):
        if getattr(self.client, "base_url", None) != PAPER_URL:
            self._block("Supervision requires the fixed Alpaca paper endpoint")

    def _session(self, now=None):
        """Validate current broker clock against the explicit daily calendar."""
        self._guard_client()
        clock = self.client.get_clock()
        timestamp = _time(now if now is not None else datetime.now(timezone.utc))
        if not isinstance(clock, dict) or not isinstance(clock.get("is_open"), bool):
            self._block("Supervision broker clock is invalid")
        age = (timestamp - _time(clock["timestamp"])).total_seconds()
        if not -1 <= age <= 10:
            self._block("Supervision broker clock is stale")
        day = timestamp.astimezone(NEW_YORK).date().isoformat()
        calendar = self.client.get_calendar(day, day)
        if now is None:
            timestamp = datetime.now(timezone.utc)
            if not -1 <= (timestamp - _time(clock["timestamp"])).total_seconds() <= 10:
                self._block("Supervision clock became stale while fetching its calendar")
        if not isinstance(calendar, list) or len(calendar) > 1:
            self._block("Supervision calendar snapshot is invalid")
        if not calendar:
            if clock["is_open"]:
                self._block("Open broker clock contradicts the exchange calendar")
            return {"now": timestamp, "clock": clock, "open": False,
                    "close_at": None, "entry_cutoff": None}
        row = calendar[0]
        if (not isinstance(row, dict) or row.get("date") != day or
                any(not isinstance(row.get(field), str) or not re.fullmatch(r"\d{2}:\d{2}", row[field])
                    for field in ("open", "close"))):
            self._block("Supervision calendar date is inconsistent")
        opened = datetime.fromisoformat(day + "T" + row["open"]).replace(tzinfo=NEW_YORK).astimezone(timezone.utc)
        closed = datetime.fromisoformat(day + "T" + row["close"]).replace(tzinfo=NEW_YORK).astimezone(timezone.utc)
        if not opened < closed or clock["is_open"] != (opened <= timestamp < closed):
            self._block("Broker clock contradicts calendar session boundaries")
        if clock["is_open"] and _time(clock.get("next_close")) != closed:
            self._block("Broker close differs from the daily exchange calendar")
        return {"now": timestamp, "clock": clock, "open": clock["is_open"],
                "close_at": closed,
                "entry_cutoff": closed - timedelta(seconds=self.close_buffer_seconds)}

    def _node(self, order, *, side, known_ids):
        if (not isinstance(order, dict) or not isinstance(order.get("id"), str) or not order["id"]
                or order.get("symbol") != "QQQ" or order.get("side") != side
                or order.get("status") not in KNOWN_STATUS
                or order.get("replaced_by") or order.get("replaces")):
            self._block("Owned broker order identity or lifecycle is unsupported")
        if order["id"] in known_ids:
            self._block("Broker order identity appears in more than one owned family")
        quantity = _quantity(order.get("qty"), "order quantity", positive=True)
        filled = _quantity(order.get("filled_qty", 0), "filled quantity")
        if filled > quantity:
            self._block("Broker fill exceeds its order quantity")
        previous = self.db.execute("SELECT filled_qty,status FROM paper_supervision_fills WHERE broker_order_id=?",
                                   (order["id"],)).fetchone()
        if previous and filled < _quantity(previous[0], "previous filled quantity"):
            self._block("Cumulative broker fills went backwards")
        if previous and previous[1] in FINAL and previous[1] != order["status"]:
            self._block("A terminal broker order changed lifecycle state")
        known_ids[order["id"]] = order
        return filled

    def _record_exit(self, row, order):
        payload = json.loads(row["payload"])
        if (not isinstance(order, dict) or order.get("client_order_id") != row["client_order_id"]
                or order.get("symbol") != "QQQ" or order.get("side") != "sell"
                or order.get("type") != "market" or order.get("time_in_force") != "day"
                or _quantity(order.get("qty"), "close qty", positive=True) != _quantity(payload["qty"], "intent qty", positive=True)
                or order.get("order_class") not in {None, "", "simple"}
                or order.get("legs")):
            self._block("Broker close differs from the persisted owned exit intent")
        with self.db:
            self.db.execute("UPDATE paper_exit_intents SET state=?,broker_order=? WHERE client_order_id=?",
                            (order.get("status", "unknown"), _json(order), row["client_order_id"]))
            self._audit("supervision_exit_receipt", {"client_order_id": row["client_order_id"], "order": order})

    def _snapshot(self):
        """Strict ownership and fill accounting, including additional close sells."""
        self._guard_client()
        account = self.client.get_account()
        if not isinstance(account, dict) or not account.get("id"):
            self._block("Broker account identity is missing")
        previous = self.db.execute("SELECT value FROM paper_meta WHERE key='account_id'").fetchone()
        account_id = str(account["id"])
        if previous and previous[0] != account_id:
            self._block("The execution ledger belongs to a different paper account")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO paper_meta VALUES('account_id',?)", (account_id,))
        known, families = {}, {}
        for row in self.db.execute("SELECT * FROM paper_intents").fetchall():
            cid = row["client_order_id"]
            order = self.client.get_order_by_client_id(cid)
            if order is None:
                self._block("An entry intent has no confirmed broker outcome; do not resubmit it")
            self._record_order(cid, order)
            entry_filled = self._node(order, side="buy", known_ids=known)
            legs = order.get("legs") or []
            if not isinstance(legs, list) or len(legs) > 2:
                self._block("Bracket legs are invalid or incomplete")
            sold = Decimal(0)
            for leg in legs:
                if leg.get("legs") or leg.get("type") not in {"limit", "stop", "stop_limit"}:
                    self._block("Bracket exit structure is unsupported")
                sold += self._node(leg, side="sell", known_ids=known)
            families[cid] = {"order": order, "legs": legs, "net": entry_filled - sold,
                             "entry_filled": entry_filled, "created_at": row["created_at"], "exit": None}
        for row in self.db.execute("SELECT * FROM paper_exit_intents").fetchall():
            family = families.get(row["parent_client_order_id"])
            if family is None:
                self._block("Owned close has no recorded parent entry")
            order = self.client.get_order_by_client_id(row["client_order_id"])
            if order is None:
                self._block("An exit intent has no confirmed broker outcome; never resubmit it")
            self._record_exit(row, order)
            family["net"] -= self._node(order, side="sell", known_ids=known)
            family["exit"] = order
        if any(family["net"] < 0 for family in families.values()):
            self._block("Owned fills indicate an oversold or short position")
        open_orders = self.client.get_orders()
        if not isinstance(open_orders, list) or len(open_orders) >= 500:
            self._block("Open-order snapshot is invalid or potentially truncated")
        for top in open_orders:
            for item in self._flatten(top):
                observed = known.get(item.get("id"))
                if observed is None:
                    self._block("Unknown broker order: no supervision mutations are permitted")
                fields = ("symbol", "side", "qty", "filled_qty", "status")
                if any(str(item.get(field, "0" if field == "filled_qty" else None)) !=
                       str(observed.get(field, "0" if field == "filled_qty" else None)) for field in fields):
                    self._block("Order snapshots changed during reconciliation; poll again")
        positions = self.client.get_positions()
        if not isinstance(positions, list):
            self._block("Broker position snapshot is invalid")
        actual = Decimal(0)
        if len(positions) > 1:
            self._block("Unknown or duplicate account positions block supervision")
        for position in positions:
            if position.get("symbol") != "QQQ":
                self._block("Unknown account position blocks supervision")
            actual += _quantity(position.get("qty"), "position quantity")
        expected = sum((family["net"] for family in families.values()), Decimal(0))
        if actual != expected:
            self._block("Broker positions differ from cumulative owned fills")
        if sum(family["net"] > 0 for family in families.values()) > 1:
            self._block("Multiple exposed parent entries require operator reconciliation")
        with self.db:
            for order_id, order in known.items():
                self.db.execute("INSERT OR REPLACE INTO paper_supervision_fills VALUES(?,?,?)",
                                (order_id, str(_quantity(order.get("filled_qty", 0), "filled quantity")), order["status"]))
                if order["status"] in FINAL:
                    self.db.execute("UPDATE paper_cancel_attempts SET state='confirmed' WHERE broker_order_id=?",
                                    (order_id,))
            self._audit("supervision_snapshot", {"positions": positions, "open_orders": open_orders,
                                                "owned_quantity": str(expected)})
        return {"account": account, "positions": positions, "open_orders": open_orders,
                "families": families, "quantity": expected}

    @staticmethod
    def _protected(family):
        net = family["net"]
        stops = sum((_quantity(leg["qty"], "stop quantity") - _quantity(leg.get("filled_qty", 0), "stop fills")
                     for leg in family["legs"] if leg.get("type") in {"stop", "stop_limit"}
                     and leg.get("status") in ACTIVE_PROTECTION), Decimal(0))
        if net == 0:
            return family["entry_filled"] == 0 or all(leg["status"] in FINAL for leg in family["legs"])
        return family["order"]["status"] == "filled" and stops >= net

    def reconcile(self):
        """Read-only broker reconciliation; recovery continues through supervise()."""
        try:
            snapshot = self._snapshot()
            pending = self.db.execute("SELECT 1 FROM paper_supervision_jobs WHERE state!='done' LIMIT 1").fetchone()
            if pending or any(not self._protected(family) for family in snapshot["families"].values()):
                self._block("Owned exposure is undergoing lifecycle recovery; new entries are blocked")
            with self.db:
                self.db.execute("DELETE FROM paper_meta WHERE key='blocked'")
            return {key: snapshot[key] for key in ("account", "positions", "open_orders")}
        except ReconciliationRequired:
            raise
        except (PaperError, OSError, ValueError, KeyError, TypeError):
            self._block("Supervised reconciliation failed; no new entries are allowed")

    def submit(self, proposal, quote, *, authorization_id=None, signal_expires_at=None, now=None):
        # Clock/calendar checks are separate from strategy and cannot be bypassed
        # by valid authorization. Existing intent lookup stays read-only in base.
        existing = self.db.execute("SELECT 1 FROM paper_intents WHERE signal_id=?", (proposal.signal_id,)).fetchone()
        if existing:
            return super().submit(proposal, quote, authorization_id=authorization_id, now=now)
        try:
            self._entry_expiry = _time(signal_expires_at) if signal_expires_at is not None else None
            self._entry_session = self._session(now)
            if not self._entry_session["open"] or self._entry_session["now"] >= self._entry_session["entry_cutoff"]:
                raise RiskRejected("New entries are disabled outside the permitted session window")
            return super().submit(proposal, quote, authorization_id=authorization_id, now=now)
        finally:
            self._entry_session = None
            self._entry_expiry = None

    def _validate(self, proposal, quote, snapshot, clock, now):
        if self._entry_session is None or not self._entry_session["open"] or _time(now) >= self._entry_session["entry_cutoff"]:
            raise RiskRejected("New entry reached the exchange closing cutoff")
        if self._entry_expiry is not None and _time(now) >= self._entry_expiry:
            raise RiskRejected("Signal expired while reconciling the paper account")
        gross = super()._validate(proposal, quote, snapshot, clock, now)
        cash = _decimal(snapshot["account"].get("cash"), "account cash", positive=True)
        if _decimal(proposal.limit_price, "limit", positive=True) * proposal.qty > cash:
            raise RiskRejected("Whole-share entry exceeds available cash; leverage is disabled")
        return gross

    def _job(self, cid, reason, now):
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO paper_supervision_jobs VALUES(?,?,?,?)",
                                       (cid, reason, "recovering", _time(now).isoformat()))
            if inserted.rowcount:
                self._audit("supervision_recovery_requested", {"parent_client_order_id": cid, "reason": reason})

    def _cancel(self, cid, order, now):
        if self.db.execute("SELECT 1 FROM paper_cancel_attempts WHERE broker_order_id=?", (order["id"],)).fetchone():
            return
        with self.db:
            self.db.execute("INSERT INTO paper_cancel_attempts VALUES(?,?,?,?)",
                            (order["id"], cid, "requesting", _time(now).isoformat()))
            self._audit("supervision_cancel_requested", {"parent_client_order_id": cid, "broker_order_id": order["id"]})
        try:
            self.client.cancel_order(order["id"])
        except (PaperError, TimeoutError, OSError):
            with self.db:
                self.db.execute("UPDATE paper_cancel_attempts SET state='unknown' WHERE broker_order_id=?", (order["id"],))
            self._mark_blocked("Cancellation outcome is unknown; await broker confirmation")
            return
        with self.db:
            self.db.execute("UPDATE paper_cancel_attempts SET state='awaiting_confirmation' WHERE broker_order_id=?", (order["id"],))

    def _close_owned(self, cid, family, snapshot, now):
        quantity = family["net"]
        if quantity <= 0 or quantity != snapshot["quantity"]:
            self._block("Close quantity is not the entire verified owned exposure")
        if family["order"]["status"] not in FINAL or any(leg["status"] not in FINAL for leg in family["legs"]):
            self._block("Cannot overlap a close with an executable bracket order")
        close_id = "dw-close-" + hashlib.sha256(cid.encode()).hexdigest()[:38]
        payload = {"symbol": "QQQ", "qty": str(int(quantity)), "side": "sell", "type": "market",
                   "time_in_force": "day", "extended_hours": False, "client_order_id": close_id}
        with self.db:
            self.db.execute("INSERT INTO paper_exit_intents VALUES(?,?,?,?,?,NULL)",
                            (close_id, cid, _json(payload), "submitting", _time(now).isoformat()))
            self._audit("supervision_exit_persisted", {"parent_client_order_id": cid, "payload": payload,
                        "account_id": snapshot["account"]["id"], "positions": snapshot["positions"]})
        row = self.db.execute("SELECT * FROM paper_exit_intents WHERE client_order_id=?", (close_id,)).fetchone()
        try:
            order = self.client.submit_order(payload)
        except (PaperError, TimeoutError, OSError):
            with self.db:
                self.db.execute("UPDATE paper_exit_intents SET state='ambiguous' WHERE client_order_id=?", (close_id,))
                self._audit("supervision_exit_ambiguous", {"client_order_id": close_id})
            try:
                order = self.client.get_order_by_client_id(close_id)
            except (PaperError, TimeoutError, OSError):
                self._block("Owned exit outcome is unknown; never resubmit it")
            if order is None:
                self._block("Owned exit was not found after an ambiguous submission; never resubmit it")
        self._record_exit(row, order)

    def _mutation_snapshot(self, now=None):
        """Refresh account ownership after clock reads, then recheck time locally.

        Separate REST snapshots cannot eliminate concurrent external activity.
        The dedicated account requirement remains essential. Terminal entry and
        leg status plus fresh fill/position agreement removes known owned races.
        """
        session = self._session(now)
        snapshot = self._snapshot()
        timestamp = _time(now if now is not None else datetime.now(timezone.utc))
        if not -1 <= (timestamp - _time(session["clock"]["timestamp"])).total_seconds() <= 10:
            self._block("Broker clock became stale during mutation reconciliation")
        session["now"] = timestamp
        session["open"] = session["open"] and timestamp < session["close_at"]
        return session, snapshot

    def supervise(self, *, now=None):
        """One bounded pulse of independent order recovery and session close.

        Mutations require a coherent dedicated-account snapshot. A cancellation
        acknowledgment is never interpreted as terminal status. Each subsequent
        pulse polls first, so fill races change the eventual verified close qty.
        """
        try:
            session = self._session(now)
            timestamp = session["now"]
            snapshot = self._snapshot()
            closing = session["entry_cutoff"] is not None and session["now"] >= session["entry_cutoff"]
            for cid, family in snapshot["families"].items():
                order = family["order"]
                age = (session["now"] - _time(family["created_at"])).total_seconds()
                if age < 0:
                    self._block("Owned entry timestamp is future dated")
                active_parent = order["status"] not in FINAL
                created_day = _time(family["created_at"]).astimezone(NEW_YORK).date()
                today = session["now"].astimezone(NEW_YORK).date()
                if active_parent and family["entry_filled"] > 0:
                    self._job(cid, "partial_entry", timestamp)
                elif created_day < today and (active_parent or family["net"] > 0):
                    self._job(cid, "overnight_exposure", timestamp)
                elif active_parent and age >= self.entry_timeout_seconds:
                    self._job(cid, "stale_entry", timestamp)
                elif family["net"] > 0 and not self._protected(family) and family["exit"] is None:
                    self._job(cid, "missing_protection", timestamp)
                elif family["net"] == 0 and family["entry_filled"] > 0 and any(leg["status"] not in FINAL for leg in family["legs"]):
                    self._job(cid, "orphan_exit", timestamp)
                elif closing and (active_parent or family["net"] > 0):
                    self._job(cid, "session_close", timestamp)
            jobs = self.db.execute("SELECT * FROM paper_supervision_jobs WHERE state!='done' ORDER BY created_at,parent_client_order_id").fetchall()
            reasons = []
            for job in jobs:
                cid, family = job["parent_client_order_id"], snapshot["families"][job["parent_client_order_id"]]
                reasons.append(job["reason"])
                if job["state"] == "failed":
                    continue
                if family["exit"] is not None:
                    if family["exit"]["status"] in FINAL:
                        with self.db:
                            state = "done" if family["net"] == 0 else "failed"
                            self.db.execute("UPDATE paper_supervision_jobs SET state=? WHERE parent_client_order_id=?", (state, cid))
                        if state == "failed":
                            self._mark_blocked("Owned close ended with residual exposure; operator reconciliation is required")
                    continue
                parent = family["order"]
                if parent["status"] not in FINAL:
                    # Canceling an unfilled/partial parent prevents future entry.
                    # A later full fill is read before any subsequent exit action.
                    fresh, latest = self._mutation_snapshot(now)
                    current = latest["families"][cid]
                    if current["order"]["status"] in FINAL:
                        continue
                    if current["entry_filled"] == 0 or fresh["open"] or not any(leg["status"] in ACTIVE_PROTECTION for leg in current["legs"]):
                        self._cancel(cid, current["order"], fresh["now"])
                    continue
                remaining_legs = [leg for leg in family["legs"] if leg["status"] not in FINAL]
                if remaining_legs:
                    # Do not strip protection after hours when we cannot execute
                    # a closing sell. Waiting remains explicit unresolved state.
                    fresh, latest = self._mutation_snapshot(now)
                    current = latest["families"][cid]
                    active_legs = [leg for leg in current["legs"] if leg["status"] not in FINAL]
                    if current["order"]["status"] not in FINAL:
                        self._block("Entry lifecycle changed before protective cancellation")
                    if fresh["open"] and active_legs:
                        self._cancel(cid, active_legs[0], fresh["now"])
                    continue
                if family["net"] == 0:
                    with self.db:
                        self.db.execute("UPDATE paper_supervision_jobs SET state='done' WHERE parent_client_order_id=?", (cid,))
                    continue
                fresh, latest = self._mutation_snapshot(now)
                current = latest["families"][cid]
                if current["exit"] is not None:
                    continue
                if current["net"] == 0:
                    with self.db:
                        self.db.execute("UPDATE paper_supervision_jobs SET state='done' WHERE parent_client_order_id=?", (cid,))
                elif fresh["open"]:
                    self._close_owned(cid, current, latest, fresh["now"])
            pending = self.db.execute("SELECT reason,state FROM paper_supervision_jobs WHERE state!='done'").fetchall()
            entries_allowed = session["open"] and not closing and not pending
            if not entries_allowed:
                self._mark_blocked("New entries are disabled while lifecycle recovery or session restrictions apply")
            elif all(self._protected(family) for family in snapshot["families"].values()):
                with self.db:
                    self.db.execute("DELETE FROM paper_meta WHERE key='blocked'")
            uncertain_cancel = self.db.execute("SELECT 1 FROM paper_cancel_attempts WHERE state IN ('requesting','unknown') LIMIT 1").fetchone()
            stalled_cancel = any((session["now"] - _time(row[0])).total_seconds() >= 60
                                 for row in self.db.execute("SELECT created_at FROM paper_cancel_attempts WHERE state!='confirmed'"))
            return {"paper_only": True, "broker_verified": False, "entries_allowed": bool(entries_allowed),
                    "pending_jobs": len(pending), "recovery_reasons": sorted({row["reason"] for row in pending}),
                    "operator_required": bool(uncertain_cancel) or stalled_cancel or any(row["state"] == "failed" for row in pending),
                    "cancellation_stalled": stalled_cancel,
                    "market_open": session["open"],
                    "session_close": session["close_at"].isoformat() if session["close_at"] else None,
                    "owned_quantity_at_snapshot": str(snapshot["quantity"])}
        except ReconciliationRequired:
            raise
        except (PaperError, OSError, ValueError, KeyError, TypeError):
            self._block("Paper supervision failed; exposure and mutation outcomes require reconciliation")
