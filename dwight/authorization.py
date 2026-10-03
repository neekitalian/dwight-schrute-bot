"""Private, local operator authorization records for the paper entry library.

No network, credential loading or automatic approval. These records are not
authenticated web consent, signatures or Mastercard Verifiable Intent. The
caller supplies a private SQLite connection, shared with its execution ledger.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


class AuthorizationError(ValueError):
    """A missing, changed or out-of-scope local authorization blocks entries."""


POLICY_FIELDS = {
    "allowed_symbols", "max_order_notional", "max_position_notional",
    "max_gross_notional", "max_daily_loss", "max_stop_risk", "max_spread_bps",
    "max_quote_age_seconds", "expected_feed",
}
SPEC_FIELDS = {
    "authorization_id", "granted_by", "provider", "account_mode", "account_id",
    "strategy_id", "strategy_revision", "symbols", "side", "allocation_usd",
    "risk_policy", "expires_at",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise AuthorizationError("Authorization times require an explicit UTC offset") from None


def amount(value):
    try:
        result = Decimal(str(value))
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)) or not result.is_finite() or result <= 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise AuthorizationError("Authorization limits must be finite positive amounts") from None


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise AuthorizationError("Authorization identifiers must be bounded plain references")


def _validated(spec):
    if not isinstance(spec, dict) or set(spec) != SPEC_FIELDS:
        raise AuthorizationError("Authorization requires the complete fixed scope")
    for field in ("authorization_id", "granted_by", "account_id", "strategy_id"):
        _identifier(spec[field])
    if (spec["provider"] != "alpaca" or spec["account_mode"] != "paper" or
            spec["symbols"] != ["QQQ"] or spec["side"] != "buy"):
        raise AuthorizationError("Only Alpaca paper QQQ long-entry authorization is supported")
    if not isinstance(spec["strategy_revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", spec["strategy_revision"]):
        raise AuthorizationError("Strategy revision must be an explicit SHA256 fingerprint")
    allocation = amount(spec["allocation_usd"])
    policy = spec["risk_policy"]
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        raise AuthorizationError("Authorization requires the exact risk policy snapshot")
    if (policy["allowed_symbols"] != ["QQQ"] or not isinstance(policy["expected_feed"], str)
            or policy["expected_feed"] not in {"sip", "iex"}):
        raise AuthorizationError("Authorization policy must select QQQ and a fixed feed")
    for field in POLICY_FIELDS - {"allowed_symbols", "expected_feed"}:
        # Match RiskPolicy's numeric contract, rather than accepting string limits.
        if isinstance(policy[field], bool) or not isinstance(policy[field], (int, float)):
            raise AuthorizationError("Risk policy limits must be numeric")
        amount(policy[field])
    try:
        snapshot = json.loads(canonical(spec))
    except (ValueError, TypeError):
        raise AuthorizationError("Authorization scope must contain finite JSON values") from None
    snapshot["allocation_usd"] = str(allocation)
    snapshot["expires_at"] = utc(spec["expires_at"]).isoformat()
    return snapshot


class AuthorizationLedger:
    """Append-only local grants and controls on a caller-owned private database.

    approve/set_state are explicit operator actions, not provider authentication.
    validate_entry must run inside the transaction that commits the new intent.
    Pause and expiry cannot retract a committed or already submitted order.
    """

    def __init__(self, db):
        if db.in_transaction:
            raise AuthorizationError("Initialize the authorization ledger outside a caller transaction")
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS paper_authorizations(
                authorization_id TEXT PRIMARY KEY, approved_at TEXT NOT NULL,
                scope TEXT NOT NULL, scope_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_authorization_events(
                sequence INTEGER PRIMARY KEY, authorization_id TEXT NOT NULL,
                created_at TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS authorization_no_update
                BEFORE UPDATE ON paper_authorizations BEGIN
                SELECT RAISE(ABORT, 'Authorization scopes are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS authorization_no_delete
                BEFORE DELETE ON paper_authorizations BEGIN
                SELECT RAISE(ABORT, 'Authorization scopes are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS authorization_events_no_update
                BEFORE UPDATE ON paper_authorization_events BEGIN
                SELECT RAISE(ABORT, 'Authorization events are append only'); END;
            CREATE TRIGGER IF NOT EXISTS authorization_events_no_delete
                BEFORE DELETE ON paper_authorization_events BEGIN
                SELECT RAISE(ABORT, 'Authorization events are append only'); END;
        """)

    def approve(self, spec, *, now=None):
        if self.db.in_transaction:
            raise AuthorizationError("Approval requires a fresh transaction")
        scope = _validated(spec)
        approved_at = utc(now if now is not None else datetime.now(timezone.utc))
        if utc(scope["expires_at"]) <= approved_at:
            raise AuthorizationError("Authorization must expire after approval")
        record = {"schema_version": 1, "approved_at": approved_at.isoformat(), "scope": scope}
        digest = fingerprint(record)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM paper_authorizations WHERE authorization_id=?",
                               (scope["authorization_id"],)).fetchone():
                raise AuthorizationError("Authorization reference already exists; approve a new scope separately")
            self.db.execute("INSERT INTO paper_authorizations VALUES(?,?,?,?)",
                            (scope["authorization_id"], approved_at.isoformat(), canonical(scope), digest))
            self.db.execute("INSERT INTO paper_authorization_events(authorization_id,created_at,state) VALUES(?,?,?)",
                            (scope["authorization_id"], approved_at.isoformat(), "active"))
        return self.get(scope["authorization_id"])

    def get(self, authorization_id):
        _identifier(authorization_id)
        row = self.db.execute("SELECT approved_at,scope,scope_hash FROM paper_authorizations WHERE authorization_id=?",
                              (authorization_id,)).fetchone()
        if row is None:
            raise AuthorizationError("No recorded authorization for this entry")
        try:
            record = {"schema_version": 1, "approved_at": row[0], "scope": _validated(json.loads(row[1]))}
            if fingerprint(record) != row[2]:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise AuthorizationError("Authorization scope integrity check failed") from None
        event = self.db.execute("SELECT created_at,state FROM paper_authorization_events WHERE authorization_id=? ORDER BY sequence DESC LIMIT 1",
                                (authorization_id,)).fetchone()
        if event is None or event[1] not in {"active", "paused", "revoked"}:
            raise AuthorizationError("Authorization control state is unknown")
        return {**record, "scope_hash": row[2], "state": event[1], "state_at": event[0]}

    def set_state(self, authorization_id, state, *, now=None):
        if self.db.in_transaction:
            raise AuthorizationError("Authorization controls require a fresh transaction")
        if not isinstance(state, str) or state not in {"active", "paused", "revoked"}:
            raise AuthorizationError("Choose active, paused or revoked")
        timestamp = utc(now if now is not None else datetime.now(timezone.utc))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            record = self.get(authorization_id)
            if timestamp < utc(record["state_at"]):
                raise AuthorizationError("Authorization controls cannot go backwards in time")
            if record["state"] == "revoked" and state != "revoked":
                raise AuthorizationError("Revoked authorization cannot be resumed")
            if state == "active" and timestamp >= utc(record["scope"]["expires_at"]):
                raise AuthorizationError("Expired authorization cannot be resumed")
            if record["state"] != state:
                self.db.execute("INSERT INTO paper_authorization_events(authorization_id,created_at,state) VALUES(?,?,?)",
                                (authorization_id, timestamp.isoformat(), state))
        return self.get(authorization_id)

    def validate_entry(self, authorization_id, *, account_id, strategy_id, strategy_revision,
                       symbol, policy, gross_notional, now):
        if not self.db.in_transaction:
            raise AuthorizationError("Authorization and intent require one transaction")
        record = self.get(authorization_id)
        scope, timestamp = record["scope"], utc(now)
        if record["state"] != "active":
            raise AuthorizationError("Authorization is paused or revoked; new entries are blocked")
        if not utc(record["approved_at"]) <= timestamp < utc(scope["expires_at"]):
            raise AuthorizationError("Authorization is future dated or expired")
        if utc(record["state_at"]) > timestamp:
            raise AuthorizationError("Authorization control event is future dated")
        if account_id != scope["account_id"]:
            raise AuthorizationError("Fresh broker account differs from the authorized account")
        if symbol != "QQQ" or strategy_id != scope["strategy_id"] or strategy_revision != scope["strategy_revision"]:
            raise AuthorizationError("Entry differs from the authorized strategy version or instrument")
        if fingerprint(policy) != fingerprint(scope["risk_policy"]):
            raise AuthorizationError("Risk policy differs from the authorized snapshot")
        if amount(gross_notional) > amount(scope["allocation_usd"]):
            raise AuthorizationError("Account exposure exceeds the authorized allocation")
        return record
