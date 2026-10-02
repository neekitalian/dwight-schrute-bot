"""Private, observation-only TradingView alert inbox. No order transport exists.

Possession of the URL capability permits observations, never approvals or fills.
The HTTP adapter deliberately has no inbox-read or manual-journal endpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat


UTC = timezone.utc
MAX_BODY_BYTES = 8192
CAPABILITY_ENV = "DWIGHT_TRADINGVIEW_CAPABILITY"
MAX_AGE_SECONDS = 300
FIELDS = {"schema_version", "event_id", "kind", "symbol", "exchange", "timeframe",
          "bar_open_at", "bar_close_at", "sent_at", "bar_closed", "open", "high",
          "low", "close", "volume", "source", "strategy_version"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


class TradingViewError(ValueError):
    """A rejected observation; no approval, fill, or order was created."""


class DuplicateConflict(TradingViewError):
    """An event identity was reused with different content."""


class StaleEvent(TradingViewError):
    """A new observation is old or has implausible timing."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _timestamp(value, field):
    if not isinstance(value, str) or len(value) > 40:
        raise TradingViewError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone missing")
        return result.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise TradingViewError(f"{field} must be a timezone-aware ISO timestamp") from exc


def _clock(now):
    if now is None:
        return datetime.now(UTC)
    if not isinstance(now, datetime):
        raise TradingViewError("now must be a timezone-aware datetime")
    return _timestamp(now.isoformat(), "now")


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise TradingViewError(f"{field} must be a finite number")
    if len(str(value)) > 64:
        raise TradingViewError(f"{field} is too long")
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number < 0 or number > Decimal("1e12")
                or (field != "volume" and number == 0)
                or (number != 0 and number.as_tuple().exponent < -8)):
            raise InvalidOperation
    except (ValueError, InvalidOperation) as exc:
        raise TradingViewError(f"{field} must be finite, bounded and use at most 8 decimal places") from exc
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def validate_event(payload):
    """Validate and canonicalize a QQQ five-minute closed-bar observation."""
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise TradingViewError("event fields must exactly match the documented schema")
    event = dict(payload)
    if (type(event["schema_version"]) is not int or event["schema_version"] != 1
            or event["kind"] != "bar_observation" or event["symbol"] != "QQQ"
            or event["exchange"] != "NASDAQ" or event["timeframe"] != "5"
            or event["bar_closed"] is not True):
        raise TradingViewError("only version 1 NASDAQ QQQ five-minute closed-bar observations are accepted")
    for field in ("event_id", "source", "strategy_version"):
        if not isinstance(event[field], str) or not _IDENTIFIER.fullmatch(event[field]):
            raise TradingViewError(f"{field} must be a stable ASCII identifier of at most 160 characters")
    stamps = {field: _timestamp(event[field], field)
              for field in ("bar_open_at", "bar_close_at", "sent_at")}
    start, end = stamps["bar_open_at"], stamps["bar_close_at"]
    if (end - start != timedelta(minutes=5) or start.minute % 5
            or start.second or start.microsecond or stamps["sent_at"] < end):
        raise TradingViewError("event requires an aligned five-minute bar and sent_at >= bar_close_at")
    event.update({field: value.isoformat(timespec="microseconds") for field, value in stamps.items()})
    for field in ("open", "high", "low", "close", "volume"):
        event[field] = _number(event[field], field)
    low, high = Decimal(event["low"]), Decimal(event["high"])
    if not (low <= Decimal(event["open"]) <= high and low <= Decimal(event["close"]) <= high):
        raise TradingViewError("OHLC prices are inconsistent")
    return event


def decode_event(raw):
    """Bounded UTF-8 JSON; reject duplicate keys and non-finite JSON tokens."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY_BYTES:
        raise TradingViewError("alert body exceeds the 8192-byte limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TradingViewError("duplicate JSON fields are not allowed")
            result[key] = value
        return result

    def constant(_):
        raise TradingViewError("non-finite JSON values are not allowed")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise TradingViewError("invalid alert JSON") from exc
    return validate_event(value)


def _safe_path(path):
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise TradingViewError("parent traversal is not allowed in inbox paths")
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TradingViewError("inbox paths must not contain symbolic links")
    return path


class TradingViewInbox:
    """Persistent unreviewed observations, deliberately separate from manual.py."""

    def __init__(self, path):
        self.path = _safe_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_path(self.path)
        created = False
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise TradingViewError("inbox must be a regular file")
            # Inspect unknown existing files read-only. Even chmod is a mutation
            # and must wait until we know this is our dedicated inbox database.
            connection = None
            try:
                # immutable also prevents SQLite from creating -wal/-shm files
                # merely while inspecting an unrelated WAL-mode database. This
                # initial identity check reads the main file only; the normal
                # locked transaction below rechecks a recognized inbox.
                connection = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0.25)
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA query_only=ON")
                self._validate_database(connection)
            except sqlite3.DatabaseError as exc:
                raise TradingViewError("inbox database is unavailable or invalid") from exc
            finally:
                if connection is not None:
                    connection.close()
        else:
            os.close(descriptor)
            created = True
        _safe_path(self.path)
        os.chmod(self.path, 0o600)
        with self._db() as connection:
            if created:
                connection.execute("CREATE TABLE inbox_metadata (version INTEGER NOT NULL, mode TEXT NOT NULL)")
                connection.execute("INSERT INTO inbox_metadata VALUES (1, 'observation_only')")
                connection.execute("CREATE TABLE alert_observations (event_id TEXT PRIMARY KEY, source TEXT NOT NULL, strategy_version TEXT NOT NULL, bar_close_at TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(source, strategy_version, bar_close_at))")
            self._validate_database(connection)

    @staticmethod
    def _validate_database(connection):
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"inbox_metadata", "alert_observations"} or connection.execute(
                "SELECT version, mode FROM inbox_metadata").fetchall() != [(1, "observation_only")]:
            raise TradingViewError("unsupported inbox schema; use a dedicated inbox file")
        expected_columns = {"inbox_metadata": ["version", "mode"],
                            "alert_observations": ["event_id", "source", "strategy_version",
                                                   "bar_close_at", "received_at", "payload"]}
        for table, expected in expected_columns.items():
            if [row[1] for row in connection.execute(f"PRAGMA table_info({table})")] != expected:
                raise TradingViewError("unsupported inbox table schema")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise TradingViewError("inbox integrity check failed")

    @contextmanager
    def _db(self):
        connection = None
        try:
            _safe_path(self.path)
            connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.25)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.rollback()
            raise TradingViewError("inbox database is unavailable or invalid") from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def accept(self, payload, now=None):
        """Persist once; retries stay unreviewed and never extend freshness."""
        event, received = validate_event(payload), _clock(now)
        text = _json(event)
        with self._db() as connection:
            existing = connection.execute("SELECT payload FROM alert_observations WHERE event_id=? OR (source=? AND strategy_version=? AND bar_close_at=?)",
                                          (event["event_id"], event["source"], event["strategy_version"], event["bar_close_at"])).fetchall()
            if existing:
                if len(existing) != 1 or existing[0][0] != text:
                    raise DuplicateConflict("event identity conflicts with stored observation")
                inserted = False
            else:
                age = (received - _timestamp(event["bar_close_at"], "bar_close_at")).total_seconds()
                sent = _timestamp(event["sent_at"], "sent_at")
                if not 0 <= age <= MAX_AGE_SECONDS or sent > received + timedelta(seconds=5):
                    raise StaleEvent("new observations must arrive within 300 seconds of bar close, with no future bar")
                connection.execute("INSERT INTO alert_observations VALUES (?, ?, ?, ?, ?, ?)",
                                   (event["event_id"], event["source"], event["strategy_version"],
                                    event["bar_close_at"], received.isoformat(timespec="microseconds"), text))
                inserted = True
        return {"event_id": event["event_id"], "inserted": inserted, "duplicate": not inserted,
                "status": "unreviewed", "submits_orders": False, "creates_fills": False}

    def list_events(self, limit=100):
        """Local review only; this method is intentionally not exposed over HTTP."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise TradingViewError("limit must be an integer from 1 to 1000")
        with self._db() as connection:
            rows = connection.execute("SELECT payload, received_at FROM alert_observations ORDER BY received_at DESC, event_id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for text, received_at in rows:
            try:
                event = validate_event(json.loads(text))
                if _json(event) != text or _timestamp(received_at, "received_at").isoformat(timespec="microseconds") != received_at:
                    raise ValueError("noncanonical stored event")
            except (ValueError, TypeError) as exc:
                raise TradingViewError("stored inbox observation is invalid") from exc
            result.append({"event": event, "received_at": received_at, "status": "unreviewed",
                           "source_verified": False, "submits_orders": False, "creates_fills": False})
        return result


def capability_from_env(environ=None):
    """Load an independent URL capability. Never falls back to broker keys."""
    environment = os.environ if environ is None else environ
    capability = environment.get(CAPABILITY_ENV)
    if not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability):
        raise TradingViewError(f"{CAPABILITY_ENV} must contain 32 to 128 random URL-safe characters")
    return capability


class _InboxServer(HTTPServer):
    # Single bounded request at a time: no attacker-created unbounded threads.
    # A production TLS reverse proxy must enforce connection/rate limits.
    allow_reuse_address = True

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(1.0)
        return request, address


def make_server(inbox, host="127.0.0.1", port=8765, *, capability=None, clock=None):
    """Construct, but do not start, a loopback-only HTTP alert receiver.

    A TLS reverse proxy may forward to loopback; this function never publishes
    a URL, configures a proxy or logs the capability. Call serve_forever yourself.
    """
    if host not in {"127.0.0.1", "localhost"}:
        raise TradingViewError("the inbox binds to IPv4 loopback only; use a separately configured TLS proxy")
    if type(port) is not int or not 0 <= port <= 65535:
        raise TradingViewError("port must be an integer from 0 to 65535")
    if capability is None:
        capability = capability_from_env()
    elif not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability):
        raise TradingViewError("invalid capability configuration")
    expected_path = ("/alerts/" + capability).encode("ascii")

    class Handler(BaseHTTPRequestHandler):
        server_version = "DwightInbox"
        sys_version = ""

        def log_message(self, *_args):
            # BaseHTTPRequestHandler logs the full request URI, including its
            # credential. Disable all request/error logging at this boundary.
            pass

        def _reply(self, code, value):
            body = (_json(value) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            self._reply(code, {"error": "invalid_request"})

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, {"status": "ok", "mode": "observation_only",
                                  "submits_orders": False, "creates_fills": False,
                                  "broker_connected": False})
            else:
                self._reply(404, {"error": "not_found"})

        def do_POST(self):
            if not hmac.compare_digest(self.path.encode("utf-8"), expected_path):
                self._reply(401, {"error": "unauthorized"})
                return
            if (self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Content-Encoding")
                    or len(self.headers.get_all("Content-Type", [])) != 1
                    or self.headers.get_content_type() != "application/json"
                    or self.headers.get_content_charset("utf-8").lower() != "utf-8"):
                self._reply(415, {"error": "requires_uncompressed_utf8_json"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]):
                self._reply(411, {"error": "requires_content_length"})
                return
            length = int(lengths[0])
            if not 1 <= length <= MAX_BODY_BYTES:
                self._reply(413, {"error": "body_size_limit"})
                return
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise TradingViewError("incomplete body")
                event = decode_event(raw)
            except TimeoutError:
                self._reply(408, {"error": "request_timeout"})
                return
            except TradingViewError:
                self._reply(400, {"error": "invalid_event"})
                return
            try:
                result = inbox.accept(event, now=clock() if clock else None)
            except DuplicateConflict:
                self._reply(409, {"error": "duplicate_conflict"})
            except StaleEvent:
                self._reply(422, {"error": "stale_or_future_event"})
            except TradingViewError:
                self._reply(503, {"error": "inbox_unavailable"})
            else:
                self._reply(202 if result["inserted"] else 200, result)

    return _InboxServer((host, port), Handler)
