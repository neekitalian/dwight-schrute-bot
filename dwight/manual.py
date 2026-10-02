"""Private records for human-operated Paper Trading by TradingView.

No broker transport, UI automation, account access, or order submission exists
here. A confirmation records a human decision, not a broker acknowledgement.
Imported fills are user-supplied evidence, never a verified account statement.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import csv
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4


ACCOUNT = "tradingview_native_paper"
UTC = timezone.utc
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")
PROPOSAL_FIELDS = {
    "proposal_id", "account", "symbol", "side", "quantity", "entry", "stop",
    "target", "source", "model", "version", "signal_at", "available_at", "expires_at",
}
FILL_FIELDS = {
    "fill_id", "filled_at", "sequence", "symbol", "side", "quantity", "price", "fee", "data_kind",
}
FILL_OPTIONAL_FIELDS = {"proposal_id"}
STATUSES = {"pending", "confirmed", "skipped", "expired"}
ZERO = Decimal("0")


class ManualPaperError(ValueError):
    """Invalid input or inconsistent local evidence; no account was changed."""


def _stamp(value, field):
    if not isinstance(value, str):
        raise ManualPaperError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone missing")
        return result.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise ManualPaperError(f"{field} must be a timezone-aware ISO timestamp") from exc


def _clock(now=None):
    value = now if now is not None else datetime.now(UTC)
    if not isinstance(value, datetime):
        raise ManualPaperError("now must be a timezone-aware datetime")
    return _stamp(value.isoformat(), "now")


def _identifier(value, field):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ManualPaperError(f"{field} must be a stable identifier of 1 to 160 characters")
    return value


def _decimal(value, field, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ManualPaperError(f"{field} must be a finite decimal")
    # Bound both magnitude and fractional precision so exact money arithmetic
    # cannot be exhausted by attacker-controlled exponents or enormous inputs.
    if len(str(value)) > 80:
        raise ManualPaperError(f"{field} is too long")
    try:
        result = Decimal(str(value))
        if (not result.is_finite() or result < ZERO or (not zero and result == ZERO)
                or result > Decimal("1000000000000")
                or (result != ZERO and result.as_tuple().exponent < -8)):
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError) as exc:
        qualifier = "nonnegative" if zero else "positive"
        raise ManualPaperError(f"{field} must be {qualifier}, finite, at most 10^12 and use at most 8 decimal places") from exc


def _text(value):
    if value == ZERO:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _money(value):
    return _text(value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _proposal(payload):
    if not isinstance(payload, dict) or set(payload) != PROPOSAL_FIELDS:
        raise ManualPaperError("proposal fields must exactly match the documented schema")
    result = dict(payload)
    _identifier(result["proposal_id"], "proposal_id")
    if (result["account"] != ACCOUNT or result["symbol"] != "QQQ"
            or result["side"] != "buy"):
        raise ManualPaperError("manual proposals are QQQ long entries for tradingview_native_paper only")
    for field in ("source", "model", "version"):
        value = result[field]
        if (not isinstance(value, str) or not value.strip() or len(value) > 256
                or any(ord(c) < 32 for c in value)):
            raise ManualPaperError(f"{field} must be nonempty text of at most 256 characters")
    for field in ("signal_at", "available_at", "expires_at"):
        result[field] = _stamp(result[field], field)
    if not result["signal_at"] <= result["available_at"] < result["expires_at"]:
        raise ManualPaperError("proposal requires signal_at <= available_at < expires_at")
    values = {field: _decimal(result[field], field) for field in ("quantity", "entry", "stop", "target")}
    if not values["stop"] < values["entry"] < values["target"]:
        raise ManualPaperError("long entry requires stop < entry < target")
    result.update({field: _text(value) for field, value in values.items()})
    return result


def _fill(payload):
    if (not isinstance(payload, dict) or not FILL_FIELDS <= set(payload)
            or set(payload) - FILL_FIELDS - FILL_OPTIONAL_FIELDS):
        raise ManualPaperError("fill fields must match the documented normalized CSV schema")
    result = dict(payload)
    _identifier(result["fill_id"], "fill_id")
    result["filled_at"] = _stamp(result["filled_at"], "filled_at")
    seq = result["sequence"]
    if not isinstance(seq, (str, int)) or isinstance(seq, bool) or not re.fullmatch(r"[0-9]{1,9}", str(seq)):
        raise ManualPaperError("sequence must be a nonnegative integer of at most 9 digits")
    result["sequence"] = int(seq)
    if result["symbol"] != "QQQ" or result["side"] not in ("buy", "sell"):
        raise ManualPaperError("fills must describe QQQ buy or sell executions")
    if result["data_kind"] not in ("synthetic", "paper_export"):
        raise ManualPaperError("data_kind must be synthetic or paper_export")
    for field in ("quantity", "price", "fee"):
        result[field] = _text(_decimal(result[field], field, zero=field == "fee"))
    result["proposal_id"] = result.get("proposal_id") or None
    if result["proposal_id"] is not None:
        _identifier(result["proposal_id"], "proposal_id")
    return result


def _accounting(fills):
    """Recompute full chronological FIFO history; reject incomplete inventory.

    Partial exits allocate each remaining entry fee proportionally, with the
    final exit taking the exact remainder. Cash flow is not account equity.
    """
    with localcontext() as context:
        context.prec = 80
        lots, segments, exits, curve = [], [], [], []
        cumulative, cashflow, all_fees = ZERO, ZERO, ZERO
        for fill in sorted(fills, key=lambda f: (f["filled_at"], f["sequence"])):
            quantity, price, fee = (Decimal(fill[k]) for k in ("quantity", "price", "fee"))
            all_fees += fee
            if fill["side"] == "buy":
                cashflow -= quantity * price + fee
                lots.append({"fill_id": fill["fill_id"], "filled_at": fill["filled_at"],
                             "quantity": quantity, "price": price, "fee": fee})
                continue
            if sum((lot["quantity"] for lot in lots), ZERO) < quantity:
                raise ManualPaperError("sell exceeds recorded long inventory; import complete opening fills first")
            cashflow += quantity * price - fee
            remaining, exit_fee, exit_pnl = quantity, fee, ZERO
            for lot in lots:
                if remaining == ZERO:
                    break
                if lot["quantity"] == ZERO:
                    continue
                take = min(remaining, lot["quantity"])
                buy_fee = lot["fee"] if take == lot["quantity"] else lot["fee"] * take / lot["quantity"]
                sell_fee = exit_fee if take == remaining else exit_fee * take / remaining
                pnl = take * (price - lot["price"]) - buy_fee - sell_fee
                segments.append({"entry_fill_id": lot["fill_id"], "exit_fill_id": fill["fill_id"],
                                 "quantity": _text(take), "entry_price": _text(lot["price"]),
                                 "exit_price": _text(price), "entry_fee": _money(buy_fee),
                                 "exit_fee": _money(sell_fee), "realized_pnl": _money(pnl)})
                lot["quantity"] -= take
                lot["fee"] -= buy_fee
                remaining -= take
                exit_fee -= sell_fee
                exit_pnl += pnl
            cumulative += exit_pnl
            exits.append({"fill_id": fill["fill_id"], "filled_at": fill["filled_at"],
                          "quantity": _text(quantity), "realized_pnl": _money(exit_pnl)})
            curve.append({"filled_at": fill["filled_at"], "sequence": fill["sequence"],
                          "fill_id": fill["fill_id"], "cumulative_realized_pnl": _money(cumulative)})
        open_lots = [{"entry_fill_id": lot["fill_id"], "quantity": _text(lot["quantity"]),
                      "entry_price": _text(lot["price"]), "remaining_entry_fee": _money(lot["fee"])}
                     for lot in lots if lot["quantity"] > ZERO]
        quantity = sum((lot["quantity"] for lot in lots), ZERO)
        cost = sum((lot["quantity"] * lot["price"] + lot["fee"] for lot in lots), ZERO)
        return {"net_realized_pnl": _money(cumulative), "fees_imported": _money(all_fees),
                "net_trade_cash_flow": _money(cashflow), "closed_exit_count": len(exits),
                "fifo_matches": segments, "closed_exits": exits, "realized_pnl_curve": curve,
                "open_position": {"symbol": "QQQ", "quantity": _text(quantity),
                                  "cost_including_remaining_entry_fees": _money(cost),
                                  "lots": open_lots, "mark_price": None, "unrealized_pnl": None}}


class ManualPaperJournal:
    """Transactional local evidence ledger; path is a private SQLite filename.

    The caller must keep this file outside public Space bundles and Git. Schema
    version 1 uses a single account and zero opening inventory. Concurrent
    writers serialize with SQLite BEGIN IMMEDIATE. Reimports are idempotent.
    """

    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ManualPaperError("journal cannot be a symbolic link")
        created = False
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise ManualPaperError("journal must be a regular file")
        else:
            os.close(descriptor)
            created = True
        os.chmod(self.path, 0o600)
        with self._db() as connection:
            # The connection transaction itself serializes racing initializers.
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables and created:
                connection.execute("CREATE TABLE metadata (version INTEGER NOT NULL, account TEXT NOT NULL)")
                connection.execute("INSERT INTO metadata VALUES (1, ?)", (ACCOUNT,))
                connection.execute("CREATE TABLE proposals (proposal_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, status_at TEXT NOT NULL)")
                connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id), status TEXT NOT NULL, changed_at TEXT NOT NULL)")
                connection.execute("CREATE TABLE fills (fill_id TEXT PRIMARY KEY, filled_at TEXT NOT NULL, sequence INTEGER NOT NULL, proposal_id TEXT REFERENCES proposals(proposal_id), payload TEXT NOT NULL, UNIQUE(filled_at, sequence))")
            if connection.execute("SELECT version, account FROM metadata").fetchall() != [(1, ACCOUNT)]:
                raise ManualPaperError("unsupported journal schema or account")
            # Additive v1 migration: the stable instance identity distinguishes
            # a new journal recreated at the same path from its predecessor.
            if "journal_identity" not in tables:
                connection.execute("CREATE TABLE journal_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), instance_id TEXT NOT NULL)")
                connection.execute("INSERT INTO journal_identity VALUES (1, ?)", (uuid4().hex,))
            _journal_identity(connection)
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ManualPaperError("journal integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ManualPaperError("journal relationship check failed")
            # Validate stored evidence as well as SQLite pages. A modified but
            # syntactically valid payload must never quietly change a report.
            self._read_proposals(connection)
            _accounting(self._read_fills(connection))

    @contextmanager
    def _db(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=15)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.rollback()
            raise ManualPaperError("manual journal database is invalid or unavailable") from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _read_proposals(connection):
        result = []
        for proposal_id, text, status, created_at, status_at in connection.execute(
                "SELECT proposal_id, payload, status, created_at, status_at FROM proposals ORDER BY created_at, proposal_id"):
            try:
                proposal = _proposal(json.loads(text))
            except (ValueError, TypeError) as exc:
                raise ManualPaperError("stored proposal is invalid") from exc
            if (proposal["proposal_id"] != proposal_id or _json(proposal) != text
                    or status not in STATUSES or _stamp(created_at, "created_at") != created_at
                    or _stamp(status_at, "status_at") != status_at or status_at < created_at):
                raise ManualPaperError("stored proposal metadata is inconsistent")
            with localcontext() as context:
                context.prec = 80
                quantity, entry, stop, target = (Decimal(proposal[k]) for k in ("quantity", "entry", "stop", "target"))
                risk = quantity * (entry - stop)
                reward = quantity * (target - entry)
                result.append({**proposal, "status": status, "created_at": created_at,
                               "status_at": status_at, "planned_notional": _money(quantity * entry),
                               "planned_stop_risk_before_costs": _money(risk),
                               "planned_reward_before_costs": _money(reward),
                               "reward_risk_ratio_before_costs": _money(reward / risk),
                               "submits_orders": False})
        return result

    @staticmethod
    def _read_fills(connection):
        result = []
        for fill_id, filled_at, sequence, proposal_id, text in connection.execute(
                "SELECT fill_id, filled_at, sequence, proposal_id, payload FROM fills ORDER BY filled_at, sequence"):
            try:
                fill = _fill(json.loads(text))
            except (ValueError, TypeError) as exc:
                raise ManualPaperError("stored fill is invalid") from exc
            if ((fill["fill_id"], fill["filled_at"], fill["sequence"], fill["proposal_id"]) !=
                    (fill_id, filled_at, sequence, proposal_id) or _json(fill) != text):
                raise ManualPaperError("stored fill metadata is inconsistent")
            result.append(fill)
        if len({fill["data_kind"] for fill in result}) > 1:
            raise ManualPaperError("synthetic and paper-export evidence cannot share a journal")
        return result

    def _expire(self, connection, now):
        for proposal in self._read_proposals(connection):
            if now < proposal["status_at"]:
                raise ManualPaperError("journal clock cannot move before recorded status changes")
            if proposal["status"] == "pending" and now >= proposal["expires_at"]:
                connection.execute("UPDATE proposals SET status='expired', status_at=? WHERE proposal_id=?",
                                   (now, proposal["proposal_id"]))
                connection.execute("INSERT INTO events(proposal_id,status,changed_at) VALUES (?, 'expired', ?)",
                                   (proposal["proposal_id"], now))

    def add_proposal(self, payload, now=None, *, create_before=None):
        proposal = _proposal(payload)
        deadline = _stamp(create_before, 'create_before') if create_before is not None else None
        with self._db() as connection:
            # A journal import can hold the SQLite writer lock. Live creation
            # must use the time AFTER that wait, never backdate availability.
            now = _clock(now)
            existing = connection.execute("SELECT payload FROM proposals WHERE proposal_id=?", (proposal["proposal_id"],)).fetchone()
            if existing:
                if existing[0] != _json(proposal):
                    raise ManualPaperError("proposal ID conflicts with existing content")
                self._expire(connection, now)
            else:
                if not proposal["available_at"] <= now < proposal["expires_at"]:
                    raise ManualPaperError("new proposal must be available and unexpired")
                if deadline is not None and now >= deadline:
                    raise ManualPaperError("new proposal reference deadline passed while awaiting journal delivery")
                connection.execute("INSERT INTO proposals VALUES (?, ?, 'pending', ?, ?)",
                                   (proposal["proposal_id"], _json(proposal), now, now))
                connection.execute("INSERT INTO events(proposal_id,status,changed_at) VALUES (?, 'pending', ?)",
                                   (proposal["proposal_id"], now))
            return next(row for row in self._read_proposals(connection) if row["proposal_id"] == proposal["proposal_id"])

    def list_proposals(self, now=None):
        with self._db() as connection:
            self._expire(connection, _clock(now))
            return self._read_proposals(connection)

    def set_status(self, proposal_id, status, now=None):
        _identifier(proposal_id, "proposal_id")
        if status not in {"confirmed", "skipped", "expired"}:
            raise ManualPaperError("manual status must be confirmed, skipped, or expired")
        now = _clock(now)
        rejection = None
        result = None
        with self._db() as connection:
            # Expiration persists even if an attempted late confirmation fails.
            self._expire(connection, now)
            proposal = next((p for p in self._read_proposals(connection) if p["proposal_id"] == proposal_id), None)
            if proposal is None:
                rejection = "unknown proposal ID"
            elif proposal["status"] == status:
                result = proposal
            elif proposal["status"] != "pending":
                rejection = "terminal proposal status cannot be changed"
            elif status == "expired":
                rejection = "a proposal cannot expire before expires_at"
            else:
                connection.execute("UPDATE proposals SET status=?, status_at=? WHERE proposal_id=?", (status, now, proposal_id))
                connection.execute("INSERT INTO events(proposal_id,status,changed_at) VALUES (?, ?, ?)", (proposal_id, status, now))
                result = next(p for p in self._read_proposals(connection) if p["proposal_id"] == proposal_id)
        if rejection:
            raise ManualPaperError(rejection)
        return result

    def import_fills(self, csv_path):
        path = Path(csv_path)
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            raise ManualPaperError("fill CSV must be a file of at most 64 MiB")
        rows = []
        try:
            with path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream, strict=True)
                fields = reader.fieldnames or []
                if (len(fields) != len(set(fields)) or not FILL_FIELDS <= set(fields)
                        or set(fields) - FILL_FIELDS - FILL_OPTIONAL_FIELDS):
                    raise ManualPaperError("CSV headers must match the normalized fill schema")
                for line, row in enumerate(reader, start=2):
                    if len(rows) >= 100000:
                        raise ManualPaperError("CSV exceeds 100000 fills")
                    if None in row or any(value is None for value in row.values()):
                        raise ManualPaperError(f"CSV line {line}: missing or extra cells")
                    try:
                        normalized = _fill(row)
                        if normalized["filled_at"] > _clock():
                            raise ManualPaperError("fill timestamp is in the future")
                        rows.append(normalized)
                    except ManualPaperError as exc:
                        raise ManualPaperError(f"CSV line {line}: {exc}") from exc
        except (csv.Error, UnicodeError) as exc:
            raise ManualPaperError("fill CSV cannot be decoded") from exc
        if not rows:
            raise ManualPaperError("fill CSV contains no executions")
        inserted, duplicates = 0, 0
        with self._db() as connection:
            self._read_proposals(connection)
            self._read_fills(connection)
            for fill in rows:
                existing = connection.execute("SELECT payload FROM fills WHERE fill_id=?", (fill["fill_id"],)).fetchone()
                if existing:
                    if existing[0] != _json(fill):
                        raise ManualPaperError("fill ID conflicts with existing content; no rows imported")
                    duplicates += 1
                    continue
                if fill["proposal_id"] and not connection.execute(
                        "SELECT 1 FROM proposals WHERE proposal_id=?", (fill["proposal_id"],)).fetchone():
                    raise ManualPaperError("fill references an unknown proposal; no rows imported")
                if connection.execute("SELECT 1 FROM fills WHERE filled_at=? AND sequence=?",
                                      (fill["filled_at"], fill["sequence"])).fetchone():
                    raise ManualPaperError("execution timestamp and sequence conflict; no rows imported")
                connection.execute("INSERT INTO fills VALUES (?, ?, ?, ?, ?)",
                                   (fill["fill_id"], fill["filled_at"], fill["sequence"], fill["proposal_id"], _json(fill)))
                inserted += 1
            history = self._read_fills(connection)
            accounting = _accounting(history)
        return {"account": ACCOUNT, "inserted": inserted, "duplicates_skipped": duplicates,
                "total_fills": len(history), "data_kind": history[0]["data_kind"],
                "net_realized_pnl": accounting["net_realized_pnl"], "submits_orders": False}

    def report(self, now=None):
        now = _clock(now)
        with self._db() as connection:
            instance_id = _journal_identity(connection)
            self._expire(connection, now)
            proposals = self._read_proposals(connection)
            fills = self._read_fills(connection)
            if fills and fills[-1]["filled_at"] > now:
                raise ManualPaperError("report clock cannot precede imported fills")
            events = [dict(zip(("proposal_id", "status", "changed_at"), row)) for row in connection.execute(
                "SELECT proposal_id, status, changed_at FROM events ORDER BY id")]
            # Preserve rollback of expiration if accounting or audit fails.
            result = _assemble_report(proposals, fills, events, now)
            result["journal_instance_id"] = instance_id
        return result

    @contextmanager
    def _snapshot_db(self):
        """Read one existing database version without reserving a writer lock."""
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            # BEGIN is deferred: this first read actually acquires the SQLite
            # snapshot. Capture time is sampled by the caller only afterward.
            if connection.execute("SELECT version, account FROM metadata").fetchall() != [(1, ACCOUNT)]:
                raise ManualPaperError("unsupported journal schema or account")
            yield connection
        except sqlite3.DatabaseError as exc:
            raise ManualPaperError("manual journal snapshot is invalid or unavailable") from exc
        finally:
            if connection is not None:
                connection.rollback()
                connection.close()

    def snapshot_at(self, cutoff: datetime, now=None):
        """Capture imported executions through an inclusive event-time cutoff.

        This does not expire proposals or modify journal state. All source rows
        come from one SQLite read snapshot; proposal status is reconstructed
        from its recorded history through the cutoff. The v1 journal has no
        import timestamps, so this is evidence captured at generation time,
        never a claim about which fills were known at the historical cutoff.
        Save the returned snapshot to freeze a milestone; later imports can
        legitimately change a newly generated snapshot for the same cutoff.
        """
        if not isinstance(cutoff, datetime):
            raise ManualPaperError("cutoff must be a timezone-aware datetime")
        cutoff = _clock(cutoff)
        with self._snapshot_db() as connection:
            captured_at = _clock(now)
            if cutoff > captured_at:
                raise ManualPaperError("snapshot cutoff cannot follow capture time")
            instance_id = _journal_identity(connection)
            current = self._read_proposals(connection)
            source_fills = self._read_fills(connection)
            source_events = connection.execute(
                "SELECT id, proposal_id, status, changed_at FROM events ORDER BY id").fetchall()
            proposals, events = _proposals_at(current, source_events, cutoff)
            fills = [fill for fill in source_fills if fill["filled_at"] <= cutoff]
            available_ids = {proposal["proposal_id"] for proposal in proposals}
            for fill in fills:
                if fill["proposal_id"] is not None and fill["proposal_id"] not in available_ids:
                    raise ManualPaperError(
                        "snapshot fill references a proposal unavailable at cutoff; original evidence link is retained")
        result = _assemble_report(proposals, fills, events, cutoff)
        result.update({
            "cutoff": cutoff, "captured_at": captured_at, "journal_instance_id": instance_id,
            "snapshot_basis": "executions_through_cutoff_evidence_captured_at_generation",
            "import_provenance": "not_recorded", "knowledge_at_cutoff_unknown": True,
            "evidence_status": "imported_fills_through_cutoff" if fills else "no_imported_fills_through_cutoff",
        })
        result["limitations"].extend([
            "Executions and proposal events after the inclusive cutoff are excluded; all earlier imported opening fills remain in FIFO history.",
            "Import timestamps were not recorded. Evidence captured at generation may include late imports; knowledge at the historical cutoff is unknown.",
            "No imported fills through the cutoff does not establish that no trading occurred or that the account was flat.",
        ])
        return result


def _journal_identity(connection):
    """Read the additive v1 identity; never repair it from a read-only view."""
    try:
        rows = connection.execute("SELECT singleton, instance_id FROM journal_identity").fetchall()
    except sqlite3.DatabaseError as exc:
        raise ManualPaperError("journal instance identity is missing or invalid") from exc
    if (len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], str)
            or re.fullmatch(r"[0-9a-f]{32}", rows[0][1]) is None):
        raise ManualPaperError("journal instance identity is missing or invalid")
    return rows[0][1]


def _proposals_at(proposals, source_events, cutoff):
    """Validate recorded transitions, then reconstruct the cutoff status view."""
    by_id = {proposal["proposal_id"]: proposal for proposal in proposals}
    histories = {identifier: [] for identifier in by_id}
    for event_id, identifier, status, changed_at in source_events:
        if (type(event_id) is not int or event_id < 1 or identifier not in by_id
                or status not in STATUSES or _stamp(changed_at, "event changed_at") != changed_at):
            raise ManualPaperError("stored proposal event history is inconsistent")
        proposal, history = by_id[identifier], histories[identifier]
        if not history:
            if status != "pending" or changed_at != proposal["created_at"]:
                raise ManualPaperError("stored proposal event history has no valid creation event")
        elif (history[-1]["status"] != "pending" or status == "pending"
              or changed_at < history[-1]["changed_at"]):
            raise ManualPaperError("stored proposal event history has an invalid transition")
        if (not proposal["available_at"] <= proposal["created_at"] < proposal["expires_at"]
                or (status == "expired" and changed_at < proposal["expires_at"])
                or (status in {"confirmed", "skipped"} and changed_at >= proposal["expires_at"])):
            raise ManualPaperError("stored proposal event history violates availability or expiry")
        history.append({"proposal_id": identifier, "status": status, "changed_at": changed_at})
    selected = []
    for proposal in proposals:
        history = histories[proposal["proposal_id"]]
        if (not history or history[-1]["status"] != proposal["status"]
                or history[-1]["changed_at"] != proposal["status_at"]):
            raise ManualPaperError("stored proposal event history disagrees with current status")
        if proposal["created_at"] > cutoff:
            continue
        previous = [event for event in history if event["changed_at"] <= cutoff][-1]
        view = {**proposal, "status": previous["status"], "status_at": previous["changed_at"],
                "status_basis": "recorded_event"}
        if view["status"] == "pending" and proposal["expires_at"] <= cutoff:
            # This is a derived view only, not a fabricated persisted event.
            view.update(status="expired", status_at=proposal["expires_at"], status_basis="derived_expiry")
        selected.append(view)
    events = [{"proposal_id": identifier, "status": status, "changed_at": changed_at}
              for _, identifier, status, changed_at in source_events if changed_at <= cutoff]
    return selected, events


def _assemble_report(proposals, fills, events, as_of):
    """Pure accounting and audit assembly shared by live and cutoff reports."""
    accounting = _accounting(fills)
    by_id = {p["proposal_id"]: p for p in proposals}
    audit = []
    linked_entry_quantities = {}
    for fill in fills:
        proposal = by_id.get(fill["proposal_id"])
        issues = []
        confirmation_at = confirmation_timing = None
        proposed_quantity = linked_quantity = excess_quantity = None
        if not proposal:
            issues.append("no_proposal_link")
        elif fill["side"] == "buy":
            if fill["filled_at"] < proposal["available_at"]:
                issues.append("entry_before_proposal_available")
            if fill["filled_at"] >= proposal["expires_at"]:
                issues.append("entry_after_proposal_expired")
            if proposal["status"] != "confirmed":
                issues.append("proposal_not_manually_confirmed")
                confirmation_timing = "not_confirmed"
            else:
                # Confirmation is a human acknowledgment, not required
                # pre-trade approval. Expose timing without inventing an
                # execution-policy violation for an acknowledgment later
                # than the imported fill. Confirmed status is terminal, so
                # status_at is its original recorded confirmation time.
                confirmation_at = proposal["status_at"]
                confirmation_timing = (
                    "before_fill" if confirmation_at < fill["filled_at"] else
                    "after_fill" if confirmation_at > fill["filled_at"] else
                    "at_fill"
                )
            # Partial buys share a proposal budget. Selling shares does
            # not authorize reusing the same entry proposal. Reimports
            # cannot inflate this sum because fill identities are unique.
            with localcontext() as context:
                context.prec = 80
                total = linked_entry_quantities.get(proposal["proposal_id"], ZERO) + Decimal(fill["quantity"])
                linked_entry_quantities[proposal["proposal_id"]] = total
                proposed = Decimal(proposal["quantity"])
                proposed_quantity, linked_quantity = _text(proposed), _text(total)
                excess_quantity = _text(max(ZERO, total-proposed))
                if total > proposed:
                    issues.append("entry_quantity_exceeds_proposal")
        audit.append({"fill_id": fill["fill_id"], "proposal_id": fill["proposal_id"],
                      "issues": issues,
                      "human_confirmation_at": confirmation_at,
                      "confirmation_timing": confirmation_timing,
                      "proposed_entry_quantity": proposed_quantity,
                      "linked_entry_quantity_to_date": linked_quantity,
                      "entry_quantity_excess": excess_quantity,
                      "entry_price_difference":
                      _money(Decimal(fill["price"]) - Decimal(proposal["entry"]))
                      if proposal and fill["side"] == "buy" else None})
    is_open = Decimal(accounting["open_position"]["quantity"]) > ZERO
    return {"schema_version": 1, "account": ACCOUNT, "as_of": as_of,
            "mode": "manual_evidence", "submits_orders": False, "broker_verified": False,
            "data_kind": fills[0]["data_kind"] if fills else "no_fills",
            "performance_scope": "imported_fills_only", "currency": "USD",
            "reconciliation_status": "unknown_open_positions" if is_open else "unverified_no_account_snapshot",
            "account_equity": None, "account_return_pct": None, "unrealized_pnl": None,
            "proposals": proposals, "proposal_events": events, "fills": fills,
            "fill_audit": audit, **accounting,
            "limitations": [
                "Human confirmation is not a broker order acknowledgement.",
                "Confirmation timing describes recorded evidence; it is not proof of pre-trade approval or execution.",
                "Quantity deviations compare cumulative linked buys with a proposal; unlinked fills cannot be attributed.",
                "CSV is a documented normalized format, not a native TradingView export adapter.",
                "Imported evidence is user supplied; completeness and account balance are unverified.",
                "FIFO starts with zero inventory and excludes deposits, withdrawals, dividends and interest.",
                "Realized PnL is not account equity, total return, or marked-to-market performance.",
                "Missing quotes leave open-position marks and unrealized PnL unknown.",
                "Planned stops are instructions for the human; this journal cannot enforce them.",
            ]}
