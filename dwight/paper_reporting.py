"""Private, read-only broker reporting from genuine Alpaca paper FILL activities.

Order snapshots establish ownership and reconcile quantities. They are never
expanded into executions. No broker mutations or execution-ledger writes occur.
"""
from __future__ import annotations

from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote
from uuid import uuid4

from .paper import PAPER_URL, PaperError

MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_REPORTS = 20000
MAX_ACTIVITIES = 20000


class PaperReportError(PaperError):
    """Reporting evidence is incomplete, inconsistent, or outside its scope."""


LIMITATIONS = [
    "An empty final page proves only that the requested provider window was fetched; provider completeness is unverified.",
    "Late booking, broker corrections and deleted activities can require operator reconciliation; immutable evidence is never overwritten.",
    "Order and position snapshots are read separately from the fixed activity cutoff; concurrent fills can prevent reconciliation.",
    "Gross FIFO P&L uses owned QQQ FILL executions only. Corporate actions and adjusted basis are not supported.",
    "Fees, net P&L and account returns are unavailable: FILL records do not establish fees, cash flows or reconciled account equity returns.",
    "Alpaca paper executions are simulated broker executions, not live trading results.",
]


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise PaperReportError("Broker evidence must be finite JSON data") from None


def _text(value, label, maximum=512):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(ord(x) < 32 for x in value):
        raise PaperReportError(f"Missing or malformed {label}")
    return value


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise PaperReportError(f"Malformed {label}")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PaperReportError(f"Malformed {label}") from None
    if not result.is_finite() or result < 0 or (positive and result == 0) or result.adjusted() > 15 or result.as_tuple().exponent < -12:
        raise PaperReportError(f"Invalid {label}")
    return result


def _decimal_text(value):
    return format(value, "f")


def _timestamp(value, label="execution timestamp"):
    """Keep nanosecond ordering; datetime.fromisoformat alone truncates it."""
    _text(value, label, 64)
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise PaperReportError(f"{label} must be an aware RFC3339 timestamp")
    try:
        base = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise PaperReportError(f"Malformed {label}") from None
    fraction = match[2] or ""
    key = Decimal(int((base - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()))
    if fraction:
        key += Decimal("0." + fraction)
    normalized = base.strftime("%Y-%m-%dT%H:%M:%S") + ("." + fraction.rstrip("0") if fraction.rstrip("0") else "") + "+00:00"
    return normalized, key


def _now(value):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PaperReportError("Report cutoff must be an aware datetime")
    return value.astimezone(timezone.utc)


def _path(value):
    path = Path(value).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise PaperReportError("Private reporting paths must not contain symbolic links")
    return path


def _private_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Do not chmod an arbitrary existing parent (for example the user's home).
    if path.parent.stat().st_mode & 0o077:
        raise PaperReportError("Reporting requires a private directory (mode 0700)")


def _read_db(path):
    if not path.is_file():
        raise PaperReportError("The execution ledger does not exist")
    db = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    return db


def _broker(call, label, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception:
        # Broker exceptions can contain URLs, headers or account payloads.
        raise PaperReportError(f"{label} failed; the previous report is unchanged") from None


def _execution_scope(path):
    try:
        db = _read_db(path)
        try:
            row = db.execute("SELECT value FROM paper_meta WHERE key='account_id'").fetchone()
            if row is None:
                raise PaperReportError("Execution ledger has no verified paper account binding")
            account_id = _text(row[0], "execution account identity", 128)
            tables = {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            entry_size = db.execute("SELECT sum(length(payload)+length(client_order_id)+length(created_at)+coalesce(length(broker_order),0)) FROM (SELECT * FROM paper_intents LIMIT 1001)").fetchone()[0] or 0
            exit_size = (db.execute("SELECT sum(length(payload)+length(client_order_id)+length(created_at)+coalesce(length(broker_order),0)) FROM (SELECT * FROM paper_exit_intents LIMIT 1001)").fetchone()[0] or 0) if "paper_exit_intents" in tables else 0
            audit_size = (db.execute("SELECT sum(length(detail)) FROM (SELECT detail FROM paper_audit WHERE event IN ('broker_order','supervision_exit_receipt') LIMIT 100001)").fetchone()[0] or 0) if "paper_audit" in tables else 0
            if entry_size + exit_size + audit_size > MAX_EVIDENCE_BYTES:
                raise PaperReportError("Execution ownership evidence exceeds the report byte bound")
            entries = [dict(x) for x in db.execute("SELECT client_order_id,payload,created_at,broker_order FROM paper_intents LIMIT 1001")]
            exits = ([dict(x) for x in db.execute("SELECT client_order_id,parent_client_order_id,payload,created_at,broker_order FROM paper_exit_intents LIMIT 1001")]
                     if "paper_exit_intents" in tables else [])
            audits = ([dict(x) for x in db.execute("SELECT event,detail FROM paper_audit WHERE event IN ('broker_order','supervision_exit_receipt') LIMIT 100001")]
                      if "paper_audit" in tables else [])
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        raise PaperReportError("Execution ledger cannot be read consistently") from None
    if len(entries) + len(exits) > 1000 or len(audits) > 100000:
        raise PaperReportError("Execution ownership history exceeds the report bound")
    if sum(len(_json(row)) for row in entries + exits + audits) > MAX_EVIDENCE_BYTES:
        raise PaperReportError("Execution ownership evidence exceeds the report byte bound")
    return account_id, entries, exits, audits


def _ownership(client, entries, exits, audits):
    entries_by_cid = {row["client_order_id"]: row for row in entries}
    if len(entries_by_cid) != len(entries):
        raise PaperReportError("Duplicate entry ownership")
    known, snapshots, missing = {}, {}, set()
    all_rows = [(row, row["client_order_id"], "entry") for row in entries]
    for row in exits:
        if row["parent_client_order_id"] not in entries_by_cid:
            raise PaperReportError("Supervisor exit has no owned parent entry")
        all_rows.append((row, row["parent_client_order_id"], "supervisor_exit"))
    audit_by_cid = {}
    for audit in audits:
        try:
            detail = json.loads(audit["detail"])
            cid, order = detail["client_order_id"], detail["order"]
        except (TypeError, ValueError, KeyError):
            raise PaperReportError("Malformed persisted order evidence") from None
        audit_by_cid.setdefault(cid, []).append(order)

    def node(order, cid, parent, side, role, *, current):
        if not isinstance(order, dict):
            raise PaperReportError("Malformed owned order snapshot")
        oid = _text(order.get("id"), "owned broker order id")
        if order.get("symbol") != "QQQ" or order.get("side") != side or order.get("replaces") or order.get("replaced_by"):
            raise PaperReportError("Unsupported owned order identity or replacement")
        quantity = _number(order.get("qty"), "owned order quantity", positive=True)
        filled = _number(order.get("filled_qty", "0"), "owned cumulative filled quantity")
        if filled > quantity:
            raise PaperReportError("Owned cumulative fill exceeds order quantity")
        identity = {"broker_order_id": oid, "client_order_id": cid, "parent_client_order_id": parent,
                    "symbol": "QQQ", "side": side, "role": role}
        if oid in known and known[oid] != identity:
            raise PaperReportError("Broker order id is shared by different owned orders")
        if current and oid in snapshots:
            raise PaperReportError("Broker order id appears twice in a current ownership snapshot")
        known[oid] = identity
        if current:
            snapshots[oid] = filled
        return oid

    for row, parent, role in all_rows:
        cid = _text(row["client_order_id"], "persisted client order id")
        try:
            payload = json.loads(row["payload"])
            saved = json.loads(row["broker_order"]) if row["broker_order"] else None
        except (TypeError, ValueError):
            raise PaperReportError("Malformed persisted order intent") from None
        if not isinstance(payload, dict) or payload.get("symbol") != "QQQ" or payload.get("side") != ("buy" if role == "entry" else "sell"):
            raise PaperReportError("Reporting supports owned long-only QQQ intents")
        expected_qty = _number(payload.get("qty"), "intent quantity", positive=True)
        current = _broker(client.get_order_by_client_id, "Owned order snapshot", cid)
        if current is None:
            raise PaperReportError("Owned intent has no confirmed broker outcome")
        history = audit_by_cid.get(cid, []) + ([saved] if saved is not None else [])
        parent_id = None
        for order, is_current in [(x, False) for x in history] + [(current, True)]:
            if (not isinstance(order, dict) or order.get("client_order_id") != cid or
                    _number(order.get("qty"), "broker order quantity", positive=True) != expected_qty):
                raise PaperReportError("Owned broker order differs from its persisted intent")
            if role == "entry" and order.get("order_class") != "bracket":
                raise PaperReportError("Owned entry is not a bracket order")
            if role == "supervisor_exit" and (order.get("type") != "market" or order.get("time_in_force") != "day" or order.get("order_class") not in {None, "", "simple"}):
                raise PaperReportError("Owned supervisor close differs from its intent")
            oid = node(order, cid, parent, "buy" if role == "entry" else "sell", role, current=is_current)
            if parent_id is not None and parent_id != oid:
                raise PaperReportError("Persisted owned broker order identity changed")
            parent_id = oid
            legs = order.get("legs") or []
            if not isinstance(legs, list) or len(legs) > 2 or (role == "supervisor_exit" and legs):
                raise PaperReportError("Unsupported owned bracket structure")
            for leg in legs:
                if not isinstance(leg, dict) or leg.get("legs") or leg.get("type") not in {"limit", "stop", "stop_limit"}:
                    raise PaperReportError("Unsupported owned bracket leg")
                node(leg, cid, parent, "sell", "bracket_exit", current=is_current)
    missing.update(set(known) - set(snapshots))
    return known, snapshots, missing


def _activity(raw, account_id, cutoff_key):
    if not isinstance(raw, dict) or raw.get("activity_type") != "FILL" or raw.get("type") not in {"fill", "partial_fill"}:
        raise PaperReportError("Malformed broker FILL activity")
    if "account_id" in raw and raw["account_id"] != account_id:
        raise PaperReportError("FILL activity belongs to another paper account")
    if raw.get("side") not in {"buy", "sell"}:
        raise PaperReportError("Unsupported FILL side")
    timestamp, key = _timestamp(raw.get("transaction_time"))
    if key > cutoff_key:
        raise PaperReportError("Broker activity has a future execution timestamp")
    record = {"activity_id": _text(raw.get("id"), "broker activity id"), "account_id": account_id,
              "broker_order_id": _text(raw.get("order_id"), "activity broker order id"),
              "transaction_time": timestamp, "symbol": _text(raw.get("symbol"), "activity symbol", 64),
              "side": raw["side"], "quantity": _decimal_text(_number(raw.get("qty"), "execution quantity", positive=True)),
              "price": _decimal_text(_number(raw.get("price"), "execution price", positive=True)),
              "activity_type": "FILL", "execution_type": raw["type"], "raw": raw}
    if len(_json(record)) > 65536:
        raise PaperReportError("Broker activity exceeds the evidence size bound")
    return record, key


def _fifo(records):
    ordered = sorted(records, key=lambda row: _timestamp(row["transaction_time"])[1])
    for previous, row in zip(ordered, ordered[1:]):
        if _timestamp(previous["transaction_time"])[1] == _timestamp(row["transaction_time"])[1]:
            # No documented causal ordering exists for equal activity timestamps.
            raise PaperReportError("Same-timestamp owned executions have ambiguous FIFO ordering")
    lots, matches, family_quantities = deque(), [], {}
    bought = sold = realized = gross = Decimal(0)
    for row in ordered:
        qty, price = Decimal(row["quantity"]), Decimal(row["price"])
        parent = row["parent_client_order_id"]
        family_quantities[parent] = family_quantities.get(parent, Decimal(0)) + (qty if row["side"] == "buy" else -qty)
        if family_quantities[parent] < 0:
            raise PaperReportError("Owned FILL activities oversell an owned order family")
        if row["side"] == "buy":
            bought += qty
            lots.append([row, qty])
            continue
        sold += qty
        while qty:
            if not lots:
                raise PaperReportError("Owned FILL activities oversell known FIFO inventory")
            buy, remaining = lots[0]
            matched = min(qty, remaining)
            pnl = matched * (price - Decimal(buy["price"]))
            gross += pnl
            realized += matched
            matches.append({"buy_activity_id": buy["activity_id"], "sell_activity_id": row["activity_id"],
                            "quantity": _decimal_text(matched), "buy_price": buy["price"], "sell_price": row["price"],
                            "gross_realized_pnl_usd": _decimal_text(pnl)})
            qty -= matched
            lots[0][1] -= matched
            if lots[0][1] == 0:
                lots.popleft()
    return {"buy_quantity": _decimal_text(bought), "sell_quantity": _decimal_text(sold),
            "open_quantity": _decimal_text(bought - sold), "realized_quantity": _decimal_text(realized),
            "matched_disposals": len({x["sell_activity_id"] for x in matches}),
            "gross_realized_pnl_usd": _decimal_text(gross) if realized else None,
            "fees_usd": None, "net_realized_pnl_usd": None, "account_return_pct": None}, matches


def _schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS report_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fill_activities(activity_id TEXT PRIMARY KEY,record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS owned_orders(broker_order_id TEXT PRIMARY KEY,identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS paper_reports(sequence INTEGER PRIMARY KEY,report TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_fill_update BEFORE UPDATE ON fill_activities BEGIN SELECT RAISE(ABORT,'immutable FILL evidence'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_fill_delete BEFORE DELETE ON fill_activities BEGIN SELECT RAISE(ABORT,'immutable FILL evidence'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_owner_update BEFORE UPDATE ON owned_orders BEGIN SELECT RAISE(ABORT,'immutable order ownership'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_owner_delete BEFORE DELETE ON owned_orders BEGIN SELECT RAISE(ABORT,'immutable order ownership'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_report_update BEFORE UPDATE ON paper_reports BEGIN SELECT RAISE(ABORT,'immutable report'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_report_delete BEFORE DELETE ON paper_reports BEGIN SELECT RAISE(ABORT,'immutable report'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_meta_update BEFORE UPDATE ON report_meta BEGIN SELECT RAISE(ABORT,'immutable report scope'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_meta_delete BEFORE DELETE ON report_meta BEGIN SELECT RAISE(ABORT,'immutable report scope'); END;
    """)


def collect_paper_report(client, execution_db: Path, state_db: Path, *, now=None, max_pages=20) -> dict:
    """Fetch a fixed, bounded complete window before atomically journaling a report.

    The client must implement get_account(), get_positions(),
    get_order_by_client_id(cid), and get_fill_activities(after, until,
    page_token=None, page_size=100). All these operations are broker GETs.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= 200:
        raise PaperReportError("max_pages must be an integer from 1 through 200")
    if getattr(client, "base_url", None) != PAPER_URL:
        raise PaperReportError("Reporting requires the fixed Alpaca paper endpoint")
    execution_db, state_db = _path(execution_db), _path(state_db)
    if execution_db == state_db or (execution_db.exists() and state_db.exists() and execution_db.samefile(state_db)):
        raise PaperReportError("Reporting state must be separate from the execution ledger")
    if state_db.exists() and (not state_db.is_file() or state_db.stat().st_nlink != 1):
        raise PaperReportError("Reporting state must be a regular file with no hardlink aliases")
    _private_parent(state_db)
    cutoff = _now(now)
    until, cutoff_key = _timestamp(cutoff.isoformat())
    lock_path = state_db.with_suffix(state_db.suffix + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PaperReportError("Another reporting process owns this reporting ledger") from None
        with localcontext() as context:
            context.prec = 80
            return _collect_locked(client, execution_db, state_db, cutoff, until, cutoff_key, max_pages)
    finally:
        os.close(fd)


def _collect_locked(client, execution_db, state_db, cutoff, until, cutoff_key, max_pages):
    account_id, entries, exits, audits = _execution_scope(execution_db)
    account = _broker(client.get_account, "Paper account snapshot")
    if not isinstance(account, dict) or account.get("id") != account_id:
        raise PaperReportError("Broker and execution ledger belong to different paper accounts")
    previous_records, previous_owners, meta = {}, {}, {}
    if state_db.exists():
        try:
            if state_db.stat().st_size >= MAX_STATE_BYTES:
                raise PaperReportError("Reporting ledger reached its 32 MiB bound; archive it privately before continuing")
            with closing(_read_db(state_db)) as prior:
                meta = dict(prior.execute("SELECT key,value FROM report_meta"))
                if prior.execute("SELECT count(*) FROM paper_reports").fetchone()[0] >= MAX_REPORTS:
                    raise PaperReportError("Reporting ledger reached its 20000 report bound; archive it privately before continuing")
                previous_records = dict(prior.execute("SELECT activity_id,record FROM fill_activities LIMIT 20001"))
                previous_owners = dict(prior.execute("SELECT broker_order_id,identity FROM owned_orders LIMIT 4001"))
                if len(previous_records) > MAX_ACTIVITIES or len(previous_owners) > 4000:
                    raise PaperReportError("Persisted reporting evidence exceeds the row bound")
        except sqlite3.Error:
            raise PaperReportError("Reporting ledger cannot be read") from None
        if meta.get("account_id") != account_id or meta.get("execution_db") != str(execution_db):
            raise PaperReportError("Reporting ledger account or execution scope mismatch")
    created = [_timestamp(row["created_at"], "intent creation timestamp")[1] for row in entries + exits]
    if any(value > cutoff_key for value in created):
        raise PaperReportError("Execution intent is later than the reporting cutoff")
    after = None
    if created:
        # Floor to the preceding second without converting Decimal through a
        # float; the provider's after boundary is exclusive.
        after = (datetime.fromtimestamp(int(min(created)), timezone.utc) - timedelta(seconds=1)).isoformat()
    if meta.get("after"):
        # The initial lower bound is immutable. An earlier newly imported intent
        # needs a new report ledger rather than silently changing its coverage.
        if after is not None and _timestamp(after)[1] < _timestamp(meta["after"])[1]:
            raise PaperReportError("Execution history predates the immutable reporting window")
        after = meta["after"]
    known, snapshots, missing = _ownership(client, entries, exits, audits)
    for oid, identity_json in previous_owners.items():
        identity = json.loads(identity_json)
        if oid in known and known[oid] != identity:
            raise PaperReportError("Previously evidenced order ownership changed")
        if oid not in known:
            raise PaperReportError("Previously evidenced owned order disappeared from execution history")
    records, page_token, seen_tokens, rows, pages, evidence_bytes = {}, None, set(), 0, 0, 0
    for pages in range(1, max_pages + 1) if after is not None else []:
        page = _broker(client.get_fill_activities, "FILL activity page", after, until, page_token=page_token, page_size=100)
        if not isinstance(page, list) or len(page) > 100:
            raise PaperReportError("Invalid or oversized FILL activity page")
        if not page:
            break
        rows += len(page)
        for raw in page:
            record, _ = _activity(raw, account_id, cutoff_key)
            aid = record["activity_id"]
            if aid in records and _json(records[aid]) != _json(record):
                raise PaperReportError("Conflicting duplicate broker activity id")
            if aid not in records:
                evidence_bytes += len(_json(record))
                if evidence_bytes > MAX_EVIDENCE_BYTES or len(records) >= MAX_ACTIVITIES:
                    raise PaperReportError("FILL evidence exceeds the report row or byte bound")
            records[aid] = record
        page_token = page[-1]["id"]
        if page_token in seen_tokens:
            raise PaperReportError("FILL pagination token did not advance")
        seen_tokens.add(page_token)
    else:
        if after is not None:
            raise PaperReportError("FILL page bound reached before an empty final page")
    for aid, persisted in previous_records.items():
        old = json.loads(persisted)
        if _timestamp(old["transaction_time"])[1] > cutoff_key:
            raise PaperReportError("Reporting cutoff predates immutable activity evidence")
        if aid not in records:
            raise PaperReportError("A previously recorded broker activity is missing from the fetched window")
        if _json(records[aid]) != persisted:
            raise PaperReportError("Broker activity conflicts with immutable recorded evidence")
    owned, excluded, totals = [], [], {}
    for record in records.values():
        owner = known.get(record["broker_order_id"])
        if owner:
            if record["symbol"] != owner["symbol"] or record["side"] != owner["side"]:
                raise PaperReportError("Activity conflicts with evidenced owned order identity")
            owned.append({**{k: v for k, v in record.items() if k != "raw"}, "owned": True,
                          "parent_client_order_id": owner["parent_client_order_id"], "order_role": owner["role"]})
            oid = record["broker_order_id"]
            totals[oid] = totals.get(oid, Decimal(0)) + Decimal(record["quantity"])
        else:
            excluded.append({**{k: v for k, v in record.items() if k != "raw"}, "owned": False})
    metrics, matches = _fifo(owned)
    reasons = []
    if missing:
        reasons.append("Previously evidenced bracket orders are absent from the current nested snapshot")
    for oid, quantity in snapshots.items():
        if totals.get(oid, Decimal(0)) != quantity:
            reasons.append("Execution quantities differ from current owned cumulative order snapshots")
            break
    positions = _broker(client.get_positions, "Paper position snapshot")
    if not isinstance(positions, list) or len(positions) > 1000:
        raise PaperReportError("Malformed broker positions")
    actual = Decimal(0)
    seen_symbols = set()
    for position in positions:
        if not isinstance(position, dict):
            raise PaperReportError("Malformed broker position")
        symbol = _text(position.get("symbol"), "position symbol", 64)
        if symbol in seen_symbols:
            raise PaperReportError("Duplicate broker position symbol")
        seen_symbols.add(symbol)
        if symbol == "QQQ":
            actual = _number(position.get("qty"), "broker QQQ position quantity")
    if actual != Decimal(metrics["open_quantity"]):
        reasons.append("Broker QQQ position differs from owned execution inventory")
    final_account = _broker(client.get_account, "Final paper account snapshot")
    if not isinstance(final_account, dict) or final_account.get("id") != account_id:
        raise PaperReportError("Paper account identity changed during collection")
    if not created:
        reasons.append("No owned order intents establish a broker activity reporting window")
    reconciled = not reasons
    if not reconciled:
        metrics["gross_realized_pnl_usd"] = None
    ordered_records = sorted(records.values(), key=lambda row: row["activity_id"])
    checksum = hashlib.sha256(_json(ordered_records).encode()).hexdigest()
    reconciliation = {"status": "matched" if reconciled else "mismatch" if created else "unverified", "reasons": reasons,
                      "owned_activity_quantity": metrics["open_quantity"],
                      "owned_order_snapshot_quantity": _decimal_text(sum((qty if known[oid]["side"] == "buy" else -qty for oid, qty in snapshots.items()), Decimal(0))),
                      "broker_qqq_position_quantity": _decimal_text(actual)}
    fixture = getattr(client, "fixture_clock", False) is True
    if meta and meta.get("fixture") != str(fixture).lower():
        raise PaperReportError("Fixture and broker reporting evidence must use separate ledgers")
    summary = {"status": "no_evidence" if not owned else "ready" if reconciled else "reconciliation_required",
               "account_id": account_id, "as_of": until, "fixture": fixture,
               "gross_realized_pnl_usd": metrics["gross_realized_pnl_usd"], "open_quantity": metrics["open_quantity"] if owned else None,
               "fees_usd": None, "net_realized_pnl_usd": None, "account_return_pct": None,
               "owned_fill_count": len(owned), "excluded_fill_count": len(excluded), "reconciliation": reconciliation["status"]}
    if not created:
        for key in ("buy_quantity", "sell_quantity", "open_quantity", "realized_quantity"):
            metrics[key] = None
        reconciliation["owned_activity_quantity"] = None
        reconciliation["owned_order_snapshot_quantity"] = None
    report = {"schema_version": 1, "source": "alpaca_paper_fill_activities", "fixture": fixture,
              "generated_at": until, "account_id": account_id, "symbol": "QQQ",
              "window": {"after": after, "until": until, "pages": pages, "rows": rows, "empty_page_observed": after is not None,
                         "provider_completeness": "unverified"},
              "ledger": {"path": str(state_db), "activity_count": len(records), "owned_activity_count": len(owned),
                         "excluded_activity_count": len(excluded), "checksum_sha256": checksum},
              "metrics": metrics, "reconciliation": reconciliation, "limitations": LIMITATIONS[:],
              "console_summary": summary,
              "owned_executions": sorted(owned, key=lambda row: _timestamp(row["transaction_time"])[1]),
              "excluded_executions": sorted(excluded, key=lambda row: _timestamp(row["transaction_time"])[1]),
              "fifo_matches": matches if reconciled else []}
    # Only this private reporting DB is writable. A single transaction publishes
    # activities, ownership and report together after the entire window succeeds.
    # Compact immutable snapshots reference source records by ledger checksum.
    # Repeating the entire execution/FIFO history every minute is unnecessary.
    compact = {key: value for key, value in report.items() if key not in {"owned_executions", "excluded_executions", "fifo_matches", "limitations"}}
    added_bytes = sum(len(_json(record)) for record in ordered_records if record["activity_id"] not in previous_records)
    added_bytes += sum(len(_json(identity)) for oid, identity in known.items() if oid not in previous_owners)
    current_bytes = state_db.stat().st_size if state_db.exists() else 0
    if current_bytes + 2 * (added_bytes + len(_json(compact))) + 65536 >= MAX_STATE_BYTES:
        raise PaperReportError("Reporting ledger would exceed its 32 MiB bound; archive it privately before continuing")
    new_state = not state_db.exists()
    work_path = state_db.with_name(state_db.name + "." + uuid4().hex + ".tmp") if new_state else state_db
    try:
        state_fd = os.open(work_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(state_fd, 0o600)
        os.close(state_fd)
        with closing(sqlite3.connect(work_path, timeout=3)) as db:
            db.execute("PRAGMA synchronous=FULL")
            _schema(db)
            with db:
                bindings = {"account_id": account_id, "execution_db": str(execution_db), "fixture": str(fixture).lower()}
                if after is not None:
                    bindings["after"] = after
                for key, value in bindings.items():
                    db.execute("INSERT OR IGNORE INTO report_meta VALUES(?,?)", (key, value))
                for record in ordered_records:
                    db.execute("INSERT OR IGNORE INTO fill_activities VALUES(?,?)", (record["activity_id"], _json(record)))
                for oid, identity in known.items():
                    db.execute("INSERT OR IGNORE INTO owned_orders VALUES(?,?)", (oid, _json(identity)))
                db.execute("INSERT INTO paper_reports(report) VALUES(?)", (_json(compact),))
        if new_state:
            os.replace(work_path, state_db)
    except (sqlite3.Error, OSError):
        raise PaperReportError("Private reporting publication failed; the previous report is unchanged") from None
    finally:
        if new_state:
            work_path.unlink(missing_ok=True)
    return report


def _atomic_private(path, data):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _realized_svg(report):
    """Draw observed cumulative gross disposals only, with no invented origin."""
    if report["reconciliation"]["status"] != "matched" or not report["fifo_matches"]:
        return '<p class="muted">Cumulative realized gross P&amp;L unavailable</p>'
    by_disposal = {}
    for match in report["fifo_matches"]:
        aid = match["sell_activity_id"]
        by_disposal[aid] = by_disposal.get(aid, Decimal(0)) + Decimal(match["gross_realized_pnl_usd"])
    points, cumulative = [], Decimal(0)
    for execution in report["owned_executions"]:
        if execution["activity_id"] in by_disposal:
            cumulative += by_disposal[execution["activity_id"]]
            points.append((execution["transaction_time"], _timestamp(execution["transaction_time"])[1], cumulative))
    first, last = points[0][1], points[-1][1]
    low, high = min(point[2] for point in points), max(point[2] for point in points)
    if high == low:
        low, high = low - 1, high + 1
    coordinates = []
    for _, time_key, value in points:
        x = Decimal(440) if first == last else Decimal(90) + (time_key - first) / (last - first) * 700
        y = Decimal(190) - (value - low) / (high - low) * 140
        coordinates.append(f"{x:.2f},{y:.2f}")
    circles = "".join(f'<circle cx="{xy.split(",")[0]}" cy="{xy.split(",")[1]}" r="4" fill="#eeeeee"/>' for xy in coordinates)
    return f'''<h2>Cumulative realized gross P&amp;L</h2>
<p class="muted">Owned matched disposals only · USD · Before fees</p>
<svg viewBox="0 0 880 250" role="img" aria-label="Cumulative realized gross FIFO profit and loss in USD">
<line x1="90" y1="50" x2="790" y2="50" stroke="#444"/><line x1="90" y1="190" x2="790" y2="190" stroke="#444"/>
<text x="10" y="55" fill="#aaa">{html.escape(_decimal_text(high))}</text><text x="10" y="195" fill="#aaa">{html.escape(_decimal_text(low))}</text>
<polyline points="{' '.join(coordinates)}" fill="none" stroke="#eee" stroke-width="2"/>{circles}
<text x="90" y="230" fill="#aaa" font-size="11">{html.escape(points[0][0])}</text>
<text x="790" y="247" text-anchor="end" fill="#aaa" font-size="11">{html.escape(points[-1][0])}</text></svg>'''


def render_paper_report(report, output: Path) -> dict:
    """Write private, self-contained artifacts; publish the manifest last."""
    if not isinstance(report, dict) or report.get("source") != "alpaca_paper_fill_activities" or type(report.get("fixture")) is not bool:
        raise PaperReportError("Renderer requires an explicitly labelled paper FILL report")
    encoded = (_json(report) + "\n").encode()
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise PaperReportError("Frozen report exceeds its byte bound")
    # Validate derived claims before displaying a caller-provided snapshot.
    try:
        with localcontext() as context:
            context.prec = 80
            expected, matches = _fifo(report["owned_executions"])
        reconciliation = report["reconciliation"]
        if reconciliation["status"] not in {"matched", "mismatch", "unverified"}:
            raise PaperReportError("Malformed report reconciliation status")
        if reconciliation["status"] != "matched":
            expected["gross_realized_pnl_usd"] = None
            matches = []
        if report["window"]["after"] is None:
            if report["owned_executions"]:
                raise PaperReportError("Owned executions have no established collection window")
            for key in ("buy_quantity", "sell_quantity", "open_quantity", "realized_quantity"):
                expected[key] = None
        if report["metrics"] != expected or report["fifo_matches"] != matches:
            raise PaperReportError("Frozen report metrics do not match execution evidence")
        if reconciliation["status"] == "matched" and any(reconciliation[key] != expected["open_quantity"] for key in
                ("owned_activity_quantity", "owned_order_snapshot_quantity", "broker_qqq_position_quantity")):
            raise PaperReportError("Frozen report quantities do not reconcile")
        if any(row["account_id"] != report["account_id"] for row in report["owned_executions"] + report["excluded_executions"]):
            raise PaperReportError("Frozen report execution account mismatch")
    except (KeyError, TypeError, ValueError):
        raise PaperReportError("Malformed frozen report") from None
    output = _path(output)
    if output.exists():
        raise PaperReportError("Report output must be a new directory; existing reports are immutable")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    if not output.is_dir() or output.stat().st_mode & 0o077:
        raise PaperReportError("Report output must be a private directory (mode 0700)")
    digest = hashlib.sha256(encoded).hexdigest()
    json_path = output / f"paper-report-{digest}.json"
    html_path = output / f"paper-report-{digest}.html"
    escape = lambda value: html.escape(str(value), quote=True)
    def displayed(value, *, currency=False):
        return "Unavailable" if value is None else ("$" if currency else "") + escape(value)
    metrics, reconciliation = report["metrics"], report["reconciliation"]
    label = ("FIXTURE — synthetic test executions; not actual broker results." if report["fixture"] else
             "Genuine Alpaca paper FILL activities. Paper executions are simulated.")
    rows = "".join("<tr>" + "".join("<td>" + escape(row[key]) + "</td>" for key in
                     ("transaction_time", "activity_id", "broker_order_id", "side", "quantity", "price")) + "</tr>"
                   for row in report["owned_executions"])
    limitations = "".join("<li>" + escape(item) + "</li>" for item in report["limitations"])
    reasons = "".join("<li>" + escape(item) + "</li>" for item in reconciliation["reasons"])
    curve = _realized_svg(report)
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Private Dwight paper execution report</title><style>
body{{font:16px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#eee;background:#111}}
table{{border-collapse:collapse;width:100%;font-size:12px}}td,th{{padding:8px;border:1px solid #444;text-align:left;overflow-wrap:anywhere}}
.notice{{padding:16px;border:1px solid #555;background:#1c1c1c}}dt{{font-weight:600;color:#aaa}}dd{{margin:6px 0 20px}}
.table{{overflow:auto}}.muted{{color:#aaa;font-size:14px}}details{{border-top:1px solid #444;margin:24px 0;padding-top:18px}}summary{{cursor:pointer}}svg{{max-width:100%;font-family:system-ui,sans-serif}}
</style><h1>Private Dwight paper execution report</h1>
<p class="notice">{escape(label)} This file contains private account evidence; keep it outside public sites and source control.</p>
<p>Account: {escape(report['account_id'])} · QQQ · Cutoff: {escape(report['window']['until'])}</p>
<p>Quantity reconciliation: <strong>{escape(reconciliation['status'])}</strong>. Provider completeness: unverified.</p><ul>{reasons}</ul>
<dl><dt>Gross realized FIFO P&amp;L</dt><dd>{displayed(metrics['gross_realized_pnl_usd'], currency=True)}</dd>
<dt>Fees / net realized P&amp;L / account return</dt><dd>Unavailable / Unavailable / Unavailable</dd>
<dt>Owned open quantity</dt><dd>{displayed(metrics['open_quantity'])}</dd>
<dt>Owned executions / excluded unrelated executions</dt><dd>{len(report['owned_executions'])} / {len(report['excluded_executions'])}</dd></dl>
{curve}<details><summary>Owned execution evidence</summary><div class="table"><table><thead><tr><th>UTC execution time</th><th>Broker activity id</th><th>Broker order id</th><th>Side</th><th>Quantity</th><th>Price (USD)</th></tr></thead><tbody>{rows}</tbody></table></div></details>
<details><summary>Limits and ledger checksum</summary><ul>{limitations}</ul><p>Ledger SHA-256: {escape(report['ledger']['checksum_sha256'])}</p></details></html>"""
    html_bytes = document.encode()
    for path in (json_path, html_path, output / "paper-report-manifest.json"):
        _path(path)
    _atomic_private(json_path, encoded)
    _atomic_private(html_path, html_bytes)
    manifest = {"schema_version": 1, "source": report["source"], "fixture": report["fixture"],
                "generated_at": report["generated_at"], "json": json_path.name, "html": html_path.name,
                "checksum_sha256": digest, "html_checksum_sha256": hashlib.sha256(html_bytes).hexdigest()}
    manifest_path = output / "paper-report-manifest.json"
    _atomic_private(manifest_path, (_json(manifest) + "\n").encode())
    return {"status": "written", "directory": str(output), "json": str(json_path), "html": str(html_path),
            "manifest": str(manifest_path), "checksum_sha256": digest}
