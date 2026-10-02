"""Paper-only Alpaca transport and conservative, durable execution.

No imports perform network I/O. This initial execution policy permits whole-share,
long-only SPY/QQQ limit brackets during regular hours. It deliberately stops new
entries on unknown orders, ambiguous submissions, partial parent fills, or an
unexplained position. A dedicated paper account and one persistent database are
required. Never delete the database to work around a reconciliation failure.

Broker brackets are not guaranteed stop prices. Alpaca activates bracket exits
after the entry is fully filled, so partial fills require operator attention in
this initial implementation. Daily loss uses equity versus broker last_equity;
deposits/resetting the paper account invalidate that reference.

References:
https://docs.alpaca.markets/us/docs/working-with-orders
https://docs.alpaca.markets/us/docs/paper-trading
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote as urlquote
from urllib.request import Request, HTTPRedirectHandler, build_opener

PAPER_URL = "https://paper-api.alpaca.markets"
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
ACTIVE_PROTECTION = {"new", "accepted", "partially_filled", "accepted_for_bidding", "stopped"}


class PreparationRequired(RuntimeError):
    """The user must configure an account or credentials outside source control."""


class PaperError(RuntimeError):
    pass


class BrokerHTTPError(PaperError):
    def __init__(self, status):
        self.status = status
        # Never include server bodies, headers, or credentials in error output.
        super().__init__(f"Alpaca paper request failed with HTTP {status}")


class RiskRejected(PaperError):
    pass


class ReconciliationRequired(PaperError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PaperError("Paper API redirects are forbidden")


class AlpacaPaperClient:
    """Minimal authenticated REST client with an immutable paper origin.

    Credentials are supplied explicitly or loaded by from_env(), never at import.
    Environment endpoint overrides are rejected rather than followed.
    """

    def __init__(self, key: str, secret: str, *, base_url=PAPER_URL, timeout=15):
        if base_url != PAPER_URL:
            raise ValueError("Only the exact Alpaca paper endpoint is allowed")
        if not key or not secret:
            raise PreparationRequired("Prepare Alpaca paper API credentials outside GitHub")
        if any("\r" in value or "\n" in value for value in (key, secret)):
            raise PreparationRequired("Paper API credentials contain an invalid newline")
        if timeout <= 0 or timeout > 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        self._key, self._secret, self.timeout = key, secret, timeout
        self._opener = build_opener(_NoRedirect())

    @classmethod
    def from_env(cls):
        for name in ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL"):
            if os.environ.get(name, PAPER_URL) != PAPER_URL:
                raise PreparationRequired(f"{name} must be the Alpaca paper endpoint")
        key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise PreparationRequired(
                "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY to paper credentials in the "
                "local environment (or ALPACA_API_KEY and ALPACA_SECRET_KEY); "
                "do not paste their values into chat or commit them")
        return cls(key, secret)

    def _request(self, method, path, *, params=None, payload=None):
        get_paths = {"/v2/account", "/v2/positions", "/v2/orders", "/v2/clock",
                     "/v2/calendar", "/v2/orders:by_client_order_id"}
        order_path = re.fullmatch(r"/v2/orders/[a-zA-Z0-9-]+", path)
        if not ((method == "GET" and (path in get_paths or order_path)) or
                (method == "POST" and path == "/v2/orders") or
                (method == "DELETE" and order_path)):
            raise ValueError("Unsupported paper API operation")
        url = PAPER_URL + path + (("?" + urlencode(params)) if params else "")
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request = Request(url, data=body, method=method, headers={
            "APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret,
            "Content-Type": "application/json", "User-Agent": "Dwight-Paper/0.1"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = response.read()
                return json.loads(data) if data else None
        except HTTPError as exc:
            raise BrokerHTTPError(exc.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise PaperError("Alpaca paper transport failed; mutation outcome may be unknown") from None

    def get_account(self):
        return self._request("GET", "/v2/account")

    def get_positions(self):
        return self._request("GET", "/v2/positions")

    def get_orders(self):
        orders = self._request("GET", "/v2/orders", params={
            "status": "open", "limit": 500, "nested": "true"})
        if not isinstance(orders, list) or len(orders) >= 500:
            raise ReconciliationRequired("Open-order snapshot is invalid or potentially truncated")
        return orders

    def get_clock(self):
        return self._request("GET", "/v2/clock")

    def get_calendar(self, start, end):
        return self._request("GET", "/v2/calendar", params={"start": start, "end": end})

    def get_order_by_client_id(self, client_order_id):
        try:
            order = self._request("GET", "/v2/orders:by_client_order_id", params={
                "client_order_id": client_order_id})
        except BrokerHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        # Only the order-by-ID endpoint documents the nested parameter.
        return self.get_order(order["id"]) if order.get("order_class") == "bracket" else order

    def get_order(self, order_id):
        return self._request("GET", "/v2/orders/" + urlquote(order_id, safe=""),
                             params={"nested": "true"})

    def submit_order(self, payload):
        return self._request("POST", "/v2/orders", payload=payload)

    def cancel_order(self, order_id):
        return self._request("DELETE", "/v2/orders/" + urlquote(order_id, safe=""))


def _decimal(value, field, *, positive=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RiskRejected(f"Invalid {field}") from None
    if not number.is_finite() or (positive and number <= 0):
        raise RiskRejected(f"Invalid {field}")
    return number


def _time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise RiskRejected("Timestamps must be timezone aware")
    return result.astimezone(timezone.utc)


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, default=str)


@dataclass(frozen=True)
class RiskPolicy:
    allowed_symbols: tuple[str, ...] = ("SPY", "QQQ")
    max_order_notional: float = 1000
    max_position_notional: float = 2000
    max_gross_notional: float = 3000
    max_daily_loss: float = 100
    max_stop_risk: float = 25
    max_spread_bps: float = 10
    max_quote_age_seconds: float = 10
    expected_feed: str = "sip"

    def __post_init__(self):
        if not self.allowed_symbols or not set(self.allowed_symbols) <= {"SPY", "QQQ"}:
            raise ValueError("Initial paper policy permits only SPY and QQQ")
        if self.expected_feed not in {"sip", "iex"}:
            raise ValueError("Expected feed must be sip or iex")
        for key, value in asdict(self).items():
            if key not in {"allowed_symbols", "expected_feed"}:
                _decimal(value, key, positive=True)


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    feed: str


@dataclass(frozen=True)
class OrderProposal:
    signal_id: str
    symbol: str
    qty: int
    limit_price: float
    stop_price: float
    take_profit_price: float


class PaperExecutor:
    """A single process owns a persistent paper-account ledger.

    reconcile() performs GETs only and returns the broker snapshot. submit()
    reconciles first, enforces risk checks, journals intent, then submits once.
    An intent with an unknown outcome is never automatically submitted again.
    All local intents are refreshed, including their bracket legs, to reconstruct
    expected positions from cumulative broker fills rather than optimistic state.
    """

    def __init__(self, client, db_path: Path, policy: RiskPolicy | None = None):
        self.client, self.policy = client, policy or RiskPolicy()
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = path.with_suffix(path.suffix + ".lock").open("a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ReconciliationRequired("Another execution process owns this ledger") from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS paper_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_intents(
                client_order_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, broker_order TEXT);
            CREATE TABLE IF NOT EXISTS paper_audit(
                sequence INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                event TEXT NOT NULL, detail TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()
        self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _audit(self, event, detail):
        self.db.execute("INSERT INTO paper_audit(created_at,event,detail) VALUES(?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), event, _json(detail)))

    def _block(self, reason):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO paper_meta VALUES('blocked',?)", (reason,))
            self._audit("entries_blocked", {"reason": reason})
        raise ReconciliationRequired(reason)

    @staticmethod
    def _flatten(order):
        yield order
        for leg in order.get("legs") or []:
            yield from PaperExecutor._flatten(leg)

    def _record_order(self, cid, order):
        row = self.db.execute("SELECT payload FROM paper_intents WHERE client_order_id=?", (cid,)).fetchone()
        payload = json.loads(row["payload"])
        if not isinstance(order, dict) or not order.get("id") or order.get("client_order_id") != cid:
            self._block("Broker response does not match the persisted order intent")
        if (order.get("symbol") != payload["symbol"] or order.get("side") != "buy" or
                _decimal(order.get("qty"), "broker qty") != _decimal(payload["qty"], "intent qty") or
                order.get("order_class") != "bracket"):
            self._block("Broker order fields differ from persisted intent")
        with self.db:
            self.db.execute("UPDATE paper_intents SET state=?,broker_order=? WHERE client_order_id=?",
                            (order.get("status", "unknown"), _json(order), cid))
            self._audit("broker_order", {"client_order_id": cid, "order": order})

    def reconcile(self):
        """Read-only at the broker. Mismatches persist a block; later clean reconciliation clears it."""
        try:
            account = self.client.get_account()
            if not account.get("id"):
                self._block("Broker account identity is missing")
            account_id = str(account["id"])
            previous = self.db.execute("SELECT value FROM paper_meta WHERE key='account_id'").fetchone()
            if previous and previous[0] != account_id:
                self._block("The execution ledger belongs to a different paper account")
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO paper_meta VALUES('account_id',?)", (account_id,))
            intents = self.db.execute("SELECT * FROM paper_intents").fetchall()
            known_ids, expected, incomplete = set(), {}, []
            for row in intents:
                order = self.client.get_order_by_client_id(row["client_order_id"])
                if order is None:
                    self._block("An intent has no confirmed broker outcome; do not resubmit it")
                self._record_order(row["client_order_id"], order)
                nodes = list(self._flatten(order))
                for item in nodes:
                    if not item.get("id"):
                        self._block("Broker order or bracket leg is missing an identifier")
                    known_ids.add(item["id"])
                    symbol = item.get("symbol")
                    side = item.get("side")
                    if symbol != order["symbol"] or side not in {"buy", "sell"}:
                        self._block("Invalid bracket leg identity")
                    filled = _decimal(item.get("filled_qty", "0"), "filled quantity")
                    quantity = _decimal(item.get("qty"), "order quantity", positive=True)
                    if filled < 0 or filled > quantity:
                        self._block("Broker fill quantity is inconsistent")
                    expected[symbol] = expected.get(symbol, Decimal(0)) + (filled if side == "buy" else -filled)
                if order.get("status") == "partially_filled":
                    incomplete.append(order["symbol"])
                net = sum((_decimal(x.get("filled_qty", "0"), "fill") *
                           (1 if x.get("side") == "buy" else -1) for x in nodes), Decimal(0))
                if net > 0 and order.get("status") != "partially_filled":
                    stops = [x for x in nodes[1:] if x.get("type") in {"stop", "stop_limit"}
                             and x.get("status") in ACTIVE_PROTECTION and x.get("side") == "sell"]
                    if not stops or sum((_decimal(x.get("qty"), "stop qty") -
                                         _decimal(x.get("filled_qty", "0"), "stop filled")
                                         for x in stops), Decimal(0)) < net:
                        self._block("A filled position has no confirmed protective stop")
            open_orders = self.client.get_orders()
            for top in open_orders:
                for item in self._flatten(top):
                    if item.get("id") not in known_ids:
                        self._block("Unknown broker order: use a dedicated paper account")
            positions = self.client.get_positions()
            actual = {}
            for position in positions:
                symbol = position.get("symbol")
                quantity = _decimal(position.get("qty"), "position quantity")
                if symbol in actual or quantity < 0:
                    self._block("Short or duplicate broker position is unsupported")
                actual[symbol] = quantity
            if ({s: q for s, q in expected.items() if q} != {s: q for s, q in actual.items() if q}):
                self._block("Broker positions differ from recorded order fills")
            if incomplete:
                self._block("Partially filled entry requires review; bracket exits may not yet be active")
            with self.db:
                self.db.execute("DELETE FROM paper_meta WHERE key='blocked'")
                self._audit("reconciled", {"positions": positions, "open_orders": open_orders})
            return {"account": account, "positions": positions, "open_orders": open_orders}
        except (PaperError, OSError, ValueError, KeyError, TypeError) as exc:
            # Restrict messages to known exceptions; provider payloads may be sensitive.
            if isinstance(exc, ReconciliationRequired):
                raise
            self._block("Broker reconciliation failed; no new entries are allowed")

    def _validate(self, proposal, quote, snapshot, clock, now):
        p, policy = proposal, self.policy
        if p.symbol not in policy.allowed_symbols or quote.symbol != p.symbol:
            raise RiskRejected("Symbol is outside the paper allowlist or mismatches the quote")
        if not p.signal_id or len(p.signal_id) > 256:
            raise RiskRejected("A stable signal identifier is required")
        if isinstance(p.qty, bool) or not isinstance(p.qty, int) or p.qty <= 0:
            raise RiskRejected("Only positive whole-share long entries are supported")
        now = _time(now)
        if quote.feed != policy.expected_feed:
            raise RiskRejected("Quote feed differs from the approved feature feed")
        age = (now - _time(quote.timestamp)).total_seconds()
        if not 0 <= age <= policy.max_quote_age_seconds:
            raise RiskRejected("Quote is stale or future dated")
        if clock.get("is_open") is not True:
            raise RiskRejected("Regular trading session is closed")
        clock_age = (now - _time(clock["timestamp"])).total_seconds()
        if not -1 <= clock_age <= policy.max_quote_age_seconds:
            raise RiskRejected("Broker clock is stale")
        bid = _decimal(quote.bid, "bid", positive=True)
        ask = _decimal(quote.ask, "ask", positive=True)
        if bid > ask or (ask - bid) / ((ask + bid) / 2) * 10000 > _decimal(policy.max_spread_bps, "spread limit"):
            raise RiskRejected("Quote spread exceeds the approved limit")
        limit = _decimal(p.limit_price, "limit price", positive=True)
        stop = _decimal(p.stop_price, "stop price", positive=True)
        target = _decimal(p.take_profit_price, "take profit", positive=True)
        if any(value != value.quantize(Decimal("0.01")) for value in (limit, stop, target)):
            raise RiskRejected("Equity prices must use penny increments")
        if not stop < bid <= ask <= limit < target:
            raise RiskRejected("Long bracket requires stop below bid and target above executable limit")
        if stop > bid - Decimal("0.01"):
            raise RiskRejected("Bracket stop must be at least one cent below the fresh bid")
        if limit > ask * Decimal("1.001"):
            raise RiskRejected("Entry limit is more than 10 bps above the fresh ask")
        notional = limit * p.qty
        if notional > _decimal(policy.max_order_notional, "max order"):
            raise RiskRejected("Order notional exceeds the approved limit")
        if (limit - stop) * p.qty > _decimal(policy.max_stop_risk, "max stop risk"):
            raise RiskRejected("Planned stop risk exceeds the approved limit")
        account = snapshot["account"]
        if (account.get("status") != "ACTIVE" or account.get("trading_blocked") is not False or
                account.get("account_blocked") is not False):
            raise RiskRejected("Paper account is not active and unblocked")
        equity = _decimal(account.get("equity"), "equity", positive=True)
        last_equity = _decimal(account.get("last_equity"), "last equity", positive=True)
        if last_equity - equity >= _decimal(policy.max_daily_loss, "daily loss"):
            raise RiskRejected("Daily equity loss limit has been reached")
        if notional > _decimal(account.get("buying_power"), "buying power", positive=True):
            raise RiskRejected("Insufficient paper buying power")
        # No pyramiding or second order per symbol in the initial deployment.
        symbol_exposure, gross = Decimal(0), Decimal(0)
        for position in snapshot["positions"]:
            value = abs(_decimal(position.get("market_value"), "position market value"))
            gross += value
            if position["symbol"] == p.symbol:
                symbol_exposure += value
        for top in snapshot["open_orders"]:
            for order in self._flatten(top):
                if order.get("side") == "buy" and order.get("status") not in TERMINAL:
                    remaining = _decimal(order["qty"], "order qty") - _decimal(order.get("filled_qty", 0), "filled qty")
                    value = remaining * _decimal(order.get("limit_price"), "pending limit", positive=True)
                    gross += value
                    if order["symbol"] == p.symbol:
                        symbol_exposure += value
        if symbol_exposure:
            raise RiskRejected("An entry or position already exists for this symbol")
        if symbol_exposure + notional > _decimal(policy.max_position_notional, "position limit"):
            raise RiskRejected("Position notional exceeds the approved limit")
        if gross + notional > _decimal(policy.max_gross_notional, "gross limit"):
            raise RiskRejected("Account gross exposure exceeds the approved limit")

    def submit(self, proposal: OrderProposal, quote: Quote, *, now=None):
        cid = "dw-" + hashlib.sha256(proposal.signal_id.encode()).hexdigest()[:40]
        payload = {"symbol": proposal.symbol, "qty": str(proposal.qty), "side": "buy",
                   "type": "limit", "limit_price": str(proposal.limit_price),
                   "time_in_force": "gtc", "order_class": "bracket", "extended_hours": False,
                   "client_order_id": cid, "take_profit": {"limit_price": str(proposal.take_profit_price)},
                   "stop_loss": {"stop_price": str(proposal.stop_price)}}
        existing = self.db.execute("SELECT * FROM paper_intents WHERE signal_id=?", (proposal.signal_id,)).fetchone()
        if existing and existing["payload"] != _json(payload):
            self._block("A signal identifier was reused with different order fields")
        snapshot = self.reconcile()
        if existing:
            # Return the refreshed record, even when rejected/canceled; never resend.
            return json.loads(self.db.execute("SELECT broker_order FROM paper_intents WHERE client_order_id=?", (cid,)).fetchone()[0])
        clock = self.client.get_clock()
        self._validate(proposal, quote, snapshot, clock, now or datetime.now(timezone.utc))
        with self.db:
            self.db.execute("INSERT INTO paper_intents VALUES(?,?,?,?,?,?,NULL)",
                            (cid, proposal.signal_id, _json(payload), _json(asdict(self.policy)),
                             "submitting", datetime.now(timezone.utc).isoformat()))
            self._audit("intent_persisted", {"client_order_id": cid, "payload": payload})
            self._audit("entry_risk_snapshot", {"client_order_id": cid,
                        "quote": asdict(quote), "account": snapshot["account"], "clock": clock})
        try:
            order = self.client.submit_order(payload)
        except (PaperError, TimeoutError, OSError):
            with self.db:
                self.db.execute("UPDATE paper_intents SET state='ambiguous' WHERE client_order_id=?", (cid,))
                self._audit("submission_ambiguous", {"client_order_id": cid})
            try:
                order = self.client.get_order_by_client_id(cid)
            except (PaperError, TimeoutError, OSError):
                self._block("Submission outcome is unknown; reconcile before further activity")
            if order is None:
                self._block("Submission not found after failure; do not resubmit this intent")
        self._record_order(cid, order)
        return order

    def cancel_pending(self, client_order_id):
        """Cancel only our unfilled entry; never cancel a protective bracket leg."""
        self.reconcile()
        row = self.db.execute("SELECT broker_order FROM paper_intents WHERE client_order_id=?", (client_order_id,)).fetchone()
        if not row:
            raise RiskRejected("Cannot cancel an unknown order")
        order = json.loads(row[0])
        if _decimal(order.get("filled_qty", 0), "filled qty") != 0:
            raise RiskRejected("Cannot cancel an entry with fills or its protective exits")
        if order["status"] in TERMINAL:
            return order
        # Cancellation itself can race a fill; never assume success or flatten.
        with self.db:
            self._audit("cancel_requested", {"client_order_id": client_order_id})
        try:
            self.client.cancel_order(order["id"])
        except (PaperError, TimeoutError, OSError):
            self._block("Cancellation outcome is unknown; reconcile required")
        return self.reconcile()
