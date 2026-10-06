"""Loopback-only controls for a separately running private paper worker.

This module has no broker client, credentials, order transport or public hosting
route. The command handler owns its private queue and validates every command
again before acting. A queued command is not an authorization or an order fill.

Create the server with ``make_console_server(status_path, command_handler)`` and
open ``server.startup_url`` locally. Its generated capability is passed in the
URL fragment, removed from browser history and retained only for that tab's
session. It is never included in HTTP access logs. Do not forward this port or
publish the bootstrap URL. The CLI owns worker and console startup:

    python -m dwight paper-init --directory runs/automatic-paper --feed sip
    python -m dwight paper-worker --directory runs/automatic-paper
    python -m dwight paper-console --directory runs/automatic-paper --open

Configure paper keys in the private environment before starting the worker.
Run worker and console in separate terminals from that environment. The host
must remain on for the worker to supervise exposure. On a private server, use
SSH forwarding to its loopback port; this server has no public binding option.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import secrets


MAX_BODY_BYTES = 8192
MAX_STATUS_BYTES = 262144
MAX_STATUS_AGE_SECONDS = 30
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_AMOUNT_FIELDS = {"allocation_usd", "max_order_notional_usd",
                  "daily_loss_limit_usd", "max_stop_risk_usd"}
_AUTHORIZE_FIELDS = _AMOUNT_FIELDS | {"account_id", "expires_at", "consent"}


class ConsoleError(ValueError):
    """An invalid private console request was rejected."""


def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) else None


def _scalar(value):
    if isinstance(value, str):
        return value[:128]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    return None


def _fields(value, names):
    if not isinstance(value, dict):
        return {}
    return {key: _scalar(value[key]) for key in names if key in value}


def _time(value):
    if not isinstance(value, str) or len(value) > 48:
        raise ConsoleError("Use an expiry with an explicit time zone")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ConsoleError("Use an expiry with an explicit time zone") from None


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ConsoleError("Duplicate JSON fields are not allowed")
            result[key] = value
        return result

    def constant(_):
        raise ConsoleError("Use finite JSON numbers")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ConsoleError("Invalid command JSON") from None


def validate_command(value):
    """Validate the small control protocol without granting permission."""
    if not isinstance(value, dict) or set(value) != {"action", "payload"}:
        raise ConsoleError("Commands require action and payload")
    action, payload = value["action"], value["payload"]
    if not isinstance(action, str) or action not in {"authorize", "start", "pause"}:
        raise ConsoleError("Choose authorize, start or pause")
    if not isinstance(payload, dict):
        raise ConsoleError("Command payload must be an object")
    payload = dict(payload)
    if action == "pause":
        if payload:
            raise ConsoleError("Pause takes no settings")
    elif action == "start":
        if (set(payload) != {"authorization_id"}
                or not isinstance(payload["authorization_id"], str)
                or not _IDENTIFIER.fullmatch(payload["authorization_id"])):
            raise ConsoleError("Start requires a recorded authorization reference")
    else:
        if set(payload) != _AUTHORIZE_FIELDS or payload["consent"] is not True:
            raise ConsoleError("Approve the complete paper session scope")
        if not isinstance(payload["account_id"], str) or not _IDENTIFIER.fullmatch(payload["account_id"]):
            raise ConsoleError("Select the verified paper account")
        for key in _AMOUNT_FIELDS:
            number = payload[key]
            if (isinstance(number, bool) or not isinstance(number, (str, int, float))
                    or len(str(number)) > 32):
                raise ConsoleError("Session limits must be finite positive amounts")
            try:
                converted = float(number)
                if not math.isfinite(converted) or not 0 < converted <= 1e12:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise ConsoleError("Session limits must be finite positive amounts") from None
            payload[key] = converted
        if payload["max_order_notional_usd"] > payload["allocation_usd"]:
            raise ConsoleError("Maximum order must not exceed the allocation")
        _time(payload["expires_at"])
    return action, payload


def sanitize_status(value, *, now=None):
    """Export a fixed UI shape; never return raw worker, account or broker JSON."""
    value = value if isinstance(value, dict) else {}
    result = {
        "updated_at": _text(value.get("updated_at"), 48),
        "state": _text(value.get("state"), 64) or "unavailable",
        "account": _fields(value.get("account"), ("provider", "mode", "id")),
        "strategy": _fields(value.get("strategy"), ("id", "revision", "symbol", "timeframe", "feed")),
        "authorization": _fields(value.get("authorization"), ("id", "state", "allocation_usd", "expires_at")),
        "health": {}, "positions": [], "orders": [], "commands": [],
        "equity_usd": _scalar(value.get("equity_usd")),
        # The worker does not yet have reconciled PnL accounting.
        "pnl_usd": None,
    }
    policy_fields = ("max_order_notional", "max_position_notional", "max_gross_notional",
                     "max_daily_loss", "max_stop_risk", "max_spread_bps",
                     "max_quote_age_seconds", "expected_feed")
    authorization = value.get("authorization")
    policy = value.get("risk_policy")
    if not isinstance(policy, dict) and isinstance(authorization, dict):
        policy = authorization.get("risk_policy")
    result["risk_policy"] = _fields(policy, policy_fields)
    health = value.get("health")
    if isinstance(health, dict):
        for key in ("worker", "data", "broker"):
            item = health.get(key)
            result["health"][key] = (_fields(item, ("state", "checked_at", "message"))
                                     if isinstance(item, dict) else _text(item, 128))
    for key, fields in (
        ("positions", ("symbol", "qty", "side", "avg_entry_price", "market_value")),
        ("orders", ("id", "client_order_id", "symbol", "side", "qty", "filled_qty",
                    "status", "type", "limit_price", "stop_price")),
    ):
        rows = value.get(key)
        if isinstance(rows, list):
            result[key] = [_fields(row, fields) for row in rows[:100] if isinstance(row, dict)]
    commands = value.get("commands")
    if isinstance(commands, list):
        for command in commands[:10]:
            if not isinstance(command, dict):
                continue
            item = _fields(command, ("id", "action", "state"))
            outcome = command.get("result")
            if isinstance(outcome, str) and len(outcome) <= MAX_BODY_BYTES:
                try:
                    outcome = _decode(outcome.encode())
                except ConsoleError:
                    outcome = {}
            item["result"] = _fields(outcome, ("message", "error", "authorization_id"))
            result["commands"].append(item)
    readiness = value.get("readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    failures = readiness.get("failures")
    failures = [_text(item) for item in failures[:30] if isinstance(item, str)] if isinstance(failures, list) else []
    current = now or datetime.now(timezone.utc)
    try:
        age = (current - _time(result["updated_at"])).total_seconds()
        stale = not 0 <= age <= MAX_STATUS_AGE_SECONDS
    except (ConsoleError, TypeError):
        stale = True
    result["stale"] = stale
    if stale:
        failures.append("Worker status is missing or stale; new entries remain blocked")
    result["readiness"] = {"ready": readiness.get("ready") is True and not stale,
                           "failures": failures}
    return result


def read_status(path):
    """Read only the designated bounded local snapshot, with a closed fallback."""
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return sanitize_status({})
        with path.open("rb") as source:
            raw = source.read(MAX_STATUS_BYTES + 1)
        if len(raw) > MAX_STATUS_BYTES:
            return sanitize_status({})
        return sanitize_status(_decode(raw))
    except (OSError, ConsoleError):
        return sanitize_status({})


_STYLE = """
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#101114;color:#e8e9ed;font-size:14px}*{box-sizing:border-box}body{margin:0}main{max-width:1120px;margin:auto;padding:36px 28px 56px}header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:28px}.brand{font-size:21px;font-weight:650;letter-spacing:-.5px}h1{font-size:28px;margin:6px 0 8px;font-weight:550;letter-spacing:-.8px}h2{font-size:16px;font-weight:600;margin:0 0 18px}.muted,small{color:#9296a3}.label{font-size:11px;text-transform:uppercase;letter-spacing:1.2px;color:#9296a3}.badge{border:1px solid #3a3e49;border-radius:20px;padding:6px 11px;font-size:12px}.statusline{display:flex;justify-content:space-between;gap:20px;margin-bottom:24px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.panel{background:#191b20;border:1px solid #2b2e37;border-radius:12px;padding:24px}.wide{grid-column:1/-1}.facts{display:grid;grid-template-columns:repeat(4,1fr);gap:20px}.fact strong{display:block;font-size:17px;font-weight:500;margin-top:7px;overflow-wrap:anywhere}.step{display:inline-block;color:#9a9eab;font-size:12px;margin-right:9px;font-variant-numeric:tabular-nums}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:block;color:#b6b9c3;font-size:12px;margin-bottom:6px}input{width:100%;background:#111216;border:1px solid #363a45;border-radius:7px;color:#e8e9ed;padding:11px;font:inherit}input:focus{outline:2px solid #828bff;outline-offset:1px}.consent{display:flex;align-items:flex-start;gap:9px;font-size:12px;line-height:1.6;margin:18px 0}.consent input{width:16px;min-width:16px;margin:4px 0;accent-color:#a7adff}.actions{display:flex;gap:10px;flex-wrap:wrap}button{padding:11px 16px;border:1px solid #444957;border-radius:7px;background:#242730;color:#e8e9ed;font:inherit;font-weight:550;cursor:pointer}button.primary{background:#d5d8e3;color:#17191f;border-color:#d5d8e3}button:disabled{opacity:.36;cursor:not-allowed}button:focus-visible{outline:2px solid #828bff;outline-offset:3px}p{line-height:1.6}.notice{font-size:12px;color:#b6b9c3;min-height:20px;margin:14px 0 0}.failures{padding-left:18px;color:#d8b799;font-size:12px;line-height:1.7;margin:12px 0}.health{display:flex;flex-wrap:wrap;gap:18px;font-size:12px;color:#b6b9c3;margin:18px 0 0}.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;text-align:left;font-size:12px}th{font-weight:500;color:#9296a3}th,td{padding:11px 8px;border-bottom:1px solid #2b2e37}td{overflow-wrap:anywhere}.empty{color:#9296a3;font-size:12px;margin:0}.scope{font-size:12px;color:#b6b9c3;line-height:1.7;margin-bottom:18px;overflow-wrap:anywhere}.footer{font-size:11px;color:#737886;margin-top:22px}@media(max-width:720px){main{padding:24px 16px}.grid{grid-template-columns:1fr}.facts{grid-template-columns:1fr 1fr}.formgrid{grid-template-columns:1fr}.panel{padding:20px}.statusline{flex-direction:column;gap:8px}h1{font-size:25px}}
"""

_SCRIPT = r"""
const $ = id => document.getElementById(id);
let capability = '', latest = null, busy = false, initialized = false, pendingId = null;
const fragment = new URLSearchParams(location.hash.slice(1));
const incoming = fragment.get('session');
if (incoming) { sessionStorage.setItem('dwight-console-session', incoming); history.replaceState(null, '', '/'); }
capability = incoming || sessionStorage.getItem('dwight-console-session') || '';
const text = (id, value) => { $(id).textContent = value == null || value === '' ? 'Awaiting worker' : String(value); };
const money = value => value == null ? 'Not available' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(Number(value));
const localTime = value => value ? new Date(value).toLocaleString() : 'Not recorded';
function health(value) { return typeof value === 'object' && value ? value.state || 'unknown' : value || 'unknown'; }
function rows(id, values, columns, empty) {
  const target = $(id); target.replaceChildren();
  if (!values.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = empty; target.appendChild(p); return; }
  const table = document.createElement('table'), head = document.createElement('tr');
  columns.forEach(([key, label]) => { const th = document.createElement('th'); th.textContent = label; head.appendChild(th); });
  const thead = document.createElement('thead'); thead.appendChild(head); table.appendChild(thead);
  const tbody = document.createElement('tbody'); values.forEach(row => { const tr = document.createElement('tr'); columns.forEach(([key]) => { const td = document.createElement('td'); td.textContent = row[key] == null ? 'Unknown' : String(row[key]); tr.appendChild(td); }); tbody.appendChild(tr); });
  table.appendChild(tbody); target.appendChild(table);
}
function controls() {
  const active = latest && ['active','paused'].includes(latest.authorization.state) && new Date(latest.authorization.expires_at) > new Date();
  const paper = latest && latest.account.mode === 'paper' && latest.account.provider === 'alpaca' && latest.account.id && health(latest.health.broker) === 'connected';
  $('approve').disabled = busy || !capability || !paper || latest.stale || !$('consent').checked;
  $('start').disabled = busy || !paper || !active || !latest.readiness.ready || latest.stale;
  $('pause').disabled = busy || !capability || !latest;
}
function render(value) {
  latest = value;
  const brokerKnown = !value.stale && health(value.health.broker) === 'connected';
  text('mode', brokerKnown && value.account.mode === 'paper' ? 'PAPER ACCOUNT' : 'ACCOUNT UNVERIFIED');
  text('state', value.stale ? 'Worker unavailable' : value.state);
  $('setup').hidden = brokerKnown && Boolean(value.account.id);
  text('account', brokerKnown ? value.account.id : null);
  text('provider', brokerKnown && value.account.provider === 'alpaca' ? 'Alpaca Paper' : 'Account unverified');
  text('strategy', value.strategy.symbol === 'QQQ' ? 'QQQ VWAP · 5 min' : 'QQQ VWAP · pending');
  text('feed', value.strategy.feed ? value.strategy.feed.toUpperCase() + ' feed' : 'Feed unverified');
  text('revision', value.strategy.revision);
  text('equity', money(brokerKnown ? value.equity_usd : null));
  text('updated', 'Updated ' + localTime(value.updated_at));
  text('worker-health', 'Worker: ' + health(value.health.worker));
  text('data-health', 'Data: ' + health(value.health.data));
  text('broker-health', 'Broker: ' + health(value.health.broker));
  const failures = $('failures'); failures.replaceChildren();
  value.readiness.failures.forEach(reason => { const li = document.createElement('li'); li.textContent = reason; failures.appendChild(li); });
  text('readiness', value.readiness.ready ? 'Ready for authorized paper entries' : 'Entries blocked');
  const auth = value.authorization;
  text('authorization', auth.id ? `${auth.state || 'unknown'} · ${money(auth.allocation_usd)} · expires ${localTime(auth.expires_at)}` : 'No session authorization recorded');
  text('fixed-policy', `Position and gross exposure capped at your allocation. Maximum spread: ${value.risk_policy.max_spread_bps ?? 'unverified'} bps. Maximum quote age: ${value.risk_policy.max_quote_age_seconds ?? 'unverified'} seconds. Feed: ${value.strategy.feed || 'unverified'}.`);
  if (!initialized && value.account.id) {
    const policy = value.risk_policy;
    if (auth.allocation_usd != null) $('allocation').value = auth.allocation_usd;
    if (policy.max_order_notional != null) $('max-order').value = policy.max_order_notional;
    if (policy.max_daily_loss != null) $('daily-loss').value = policy.max_daily_loss;
    if (policy.max_stop_risk != null) $('stop-risk').value = policy.max_stop_risk;
    initialized = true;
  }
  rows('positions', brokerKnown ? value.positions : [], [['symbol','Symbol'],['qty','Shares'],['side','Side'],['avg_entry_price','Avg entry']], brokerKnown ? 'No reported positions' : 'Awaiting broker evidence');
  rows('orders', brokerKnown ? value.orders : [], [['symbol','Symbol'],['side','Side'],['qty','Shares'],['filled_qty','Filled'],['status','Status']], brokerKnown ? 'No reported open orders' : 'Awaiting broker evidence');
  if (pendingId) {
    const command = value.commands.find(item => item.id === pendingId);
    if (command && command.state !== 'queued' && command.state !== 'processing') {
      const result = command.result || {};
      text('message', command.state === 'failed' ? result.error || result.message || 'Worker rejected the command' : result.message || `Worker processed ${command.action} request`);
      pendingId = null;
    }
  }
  controls();
}
async function api(path, options = {}) {
  const response = await fetch(path, {...options, credentials:'omit', cache:'no-store', headers:{'X-Dwight-Session':capability, 'Content-Type':'application/json', ...(options.headers || {})}});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Console request failed');
  return value;
}
async function refresh() {
  if (!capability) { text('message','Launch the private console with --open to unlock this tab'); controls(); return; }
  try { render(await api('/api/status')); }
  catch (error) { text('message', error.message); if (latest) { latest.stale = true; latest.readiness.ready = false; } text('state','Worker unavailable'); controls(); }
}
async function command(action, payload) {
  busy = true; controls(); text('message','Sending control request');
  try { const receipt = await api('/api/commands', {method:'POST',body:JSON.stringify({action,payload})}); pendingId = receipt.command_id; text('message',`Queued ${action} request; awaiting worker confirmation`); if (action === 'authorize') $('consent').checked = false; await refresh(); }
  catch (error) { text('message',error.message); }
  finally { busy = false; controls(); }
}
$('scope-form').addEventListener('submit', event => {
  event.preventDefault(); if (!latest || !$('scope-form').reportValidity() || !$('consent').checked) return;
  const expires = new Date($('expiry').value); if (!Number.isFinite(expires.valueOf()) || expires <= new Date()) { text('message','Choose a future expiry'); return; }
  if (expires > new Date(Date.now() + 7 * 24 * 60 * 60 * 1000)) { text('message','Choose an expiry within seven days'); return; }
  if (Number($('max-order').value) > Number($('allocation').value)) { text('message','Maximum order must not exceed the allocation'); return; }
  command('authorize', {account_id:latest.account.id, allocation_usd:Number($('allocation').value), max_order_notional_usd:Number($('max-order').value), daily_loss_limit_usd:Number($('daily-loss').value), max_stop_risk_usd:Number($('stop-risk').value), expires_at:expires.toISOString(), consent:true});
});
$('consent').addEventListener('change',controls);
$('start').addEventListener('click',() => { if (latest && ! $('start').disabled) command('start',{authorization_id:latest.authorization.id}); });
$('pause').addEventListener('click',() => command('pause',{}));
refresh(); setInterval(refresh,5000);
"""

_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dwight · Paper console</title><style>""" + _STYLE + """</style></head>
<body><main><header><div class="brand">Dwight</div><span class="badge" id="mode">ACCOUNT UNVERIFIED</span></header>
<div class="statusline"><div><div class="label">Private paper console</div><h1>Run a paper session</h1><div class="muted" id="state">Awaiting worker</div></div><small id="updated">No worker snapshot</small></div>
<div class="grid"><section class="panel wide"><h2><span class="step">1</span>Verify the account</h2><div class="facts"><div class="fact"><div class="label">Broker</div><strong id="provider">Account unverified</strong></div><div class="fact"><div class="label">Account</div><strong id="account">Awaiting worker</strong></div><div class="fact"><div class="label">Strategy</div><strong id="strategy">QQQ VWAP · 5 min</strong><small id="feed">Feed unverified</small></div><div class="fact"><div class="label">Broker equity</div><strong id="equity">Not available</strong></div></div><div class="health"><span id="worker-health">Worker: unknown</span><span id="data-health">Data: unknown</span><span id="broker-health">Broker: unknown</span></div><ul class="failures" id="failures"></ul><details><summary class="muted">Pinned strategy revision</summary><p class="scope" id="revision">Awaiting worker</p></details><p class="notice" id="setup">Start the private worker to verify the account and readiness. Paper API keys belong in your private .env file.</p></section>
<section class="panel"><h2><span class="step">2</span>Authorize the session</h2><p class="scope">Authorize QQQ long entries on completed five-minute bars in the verified Alpaca paper account.</p><form id="scope-form"><div class="formgrid"><div><label for="allocation">Allocation · USD</label><input id="allocation" type="number" min="0.01" step="0.01" required placeholder="Enter amount"></div><div><label for="max-order">Maximum order · USD</label><input id="max-order" type="number" min="0.01" step="0.01" required placeholder="Enter amount"></div><div><label for="daily-loss">Daily loss limit · USD</label><input id="daily-loss" type="number" min="0.01" step="0.01" required placeholder="Enter amount"></div><div><label for="stop-risk">Planned risk per trade · USD</label><input id="stop-risk" type="number" min="0.01" step="0.01" required placeholder="Enter amount"></div><div class="wide"><label for="expiry">Authorization expiry · your local time</label><input id="expiry" type="datetime-local" required></div></div><p class="notice" id="fixed-policy">Position and gross exposure capped at your allocation. Awaiting fixed policy and feed.</p><p class="notice">The worker cancels unfilled entry remainders after 120 seconds. It reconciles then cancels or closes owned partial or unprotected exposure when safe. Session closing begins five minutes before market close. Stop prices and fills are not guaranteed.</p><label class="consent"><input id="consent" type="checkbox" required><span>I authorize the displayed paper account and pinned QQQ strategy, selected feed, allocation, complete risk and supervision policy, and expiry for paper entries.</span></label><button class="primary" id="approve" type="submit" disabled>Approve paper session</button></form></section>
<section class="panel"><h2><span class="step">3</span>Control paper entries</h2><p class="scope" id="authorization">No session authorization recorded</p><p id="readiness">Entries blocked</p><div class="actions"><button class="primary" id="start" type="button" disabled>Start paper trading</button><button id="pause" type="button" disabled>Pause new entries</button></div><p class="notice">Pause blocks new entries. Keep the worker running to supervise existing orders and positions.</p><p class="notice" id="message" role="status" aria-live="polite"></p></section>
<section class="panel"><h2>Positions</h2><div class="tablewrap" id="positions"><p class="empty">Awaiting broker evidence</p></div></section><section class="panel"><h2>Open orders</h2><div class="tablewrap" id="orders"><p class="empty">Awaiting broker evidence</p></div></section></div>
<p class="footer">Local operator access · Paper pilot · Actual fills require verification · Controls await worker confirmation</p></main><script>""" + _SCRIPT + """</script></body></html>"""


def _hash(value):
    return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()


def make_console_server(status_path, command_handler, *, host="127.0.0.1", port=0, capability=None):
    """Bind only IPv4 loopback; return a server with its private bootstrap URL.

    ``command_handler(action, payload)`` must durably queue the command and return
    ``{command_id: ..., status: 'queued'}``. It must not share broker credentials
    with this server. The worker independently validates scope and readiness.
    """
    if host != "127.0.0.1":
        raise ConsoleError("The private console binds only to 127.0.0.1")
    if not callable(command_handler):
        raise ConsoleError("A private worker command handler is required")
    capability = capability if capability is not None else secrets.token_urlsafe(32)
    if not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability):
        raise ConsoleError("Use a private capability of 32 to 128 URL-safe characters")
    status_path = Path(status_path).expanduser().absolute()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            # Request paths, headers, control bodies and capabilities stay private.
            pass

        def _reply(self, code, value, *, html=False):
            body = value.encode() if html else json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; "
                             f"script-src 'sha256-{_hash(_SCRIPT)}'; style-src 'sha256-{_hash(_STYLE)}'; "
                             "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def _host(self):
            return self.headers.get_all("Host", []) == [self.server.expected_host]

        def _authenticated(self):
            values = self.headers.get_all("X-Dwight-Session", [])
            return (len(values) == 1 and _CAPABILITY.fullmatch(values[0])
                    and hmac.compare_digest(values[0], capability))

        def do_GET(self):
            if not self._host():
                self._reply(403, {"error": "Invalid console host"})
            elif self.path == "/":
                self._reply(200, _HTML, html=True)
            elif self.path == "/api/status":
                if not self._authenticated():
                    self._reply(401, {"error": "Open the private console bootstrap URL"})
                else:
                    self._reply(200, read_status(status_path))
            else:
                self._reply(404, {"error": "Route not found"})

        def do_POST(self):
            if not self._host() or self.headers.get_all("Origin", []) != [self.server.expected_origin]:
                self._reply(403, {"error": "Console commands require the local console origin"})
                return
            if not self._authenticated():
                self._reply(401, {"error": "Open the private console bootstrap URL"})
                return
            if self.path != "/api/commands":
                self._reply(404, {"error": "Route not found"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or content_type != "application/json":
                self._reply(400, {"error": "Use a bounded JSON command body"})
                return
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]):
                self._reply(411, {"error": "A single content length is required"})
                return
            size = int(lengths[0])
            if not 0 < size <= MAX_BODY_BYTES:
                self._reply(413, {"error": "Command body exceeds the limit"})
                return
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ConsoleError("Incomplete command body")
                action, payload = validate_command(_decode(raw))
                result = command_handler(action, payload)
                if (not isinstance(result, dict) or result.get("status") != "queued"
                        or not isinstance(result.get("command_id"), str)
                        or not _IDENTIFIER.fullmatch(result["command_id"])):
                    self._reply(503, {"error": "Worker command receipt is unavailable"})
                    return
                self._reply(202, {"command_id": result["command_id"], "status": "queued"})
            except ConsoleError as exc:
                self._reply(400, {"error": str(exc)})
            except ValueError:
                self._reply(409, {"error": "Worker rejected the command; verify settings and readiness"})
            except Exception:
                self._reply(503, {"error": "Private worker controls are unavailable"})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.expected_host = f"127.0.0.1:{server.server_port}"
    server.expected_origin = f"http://{server.expected_host}"
    server.console_url = f"{server.expected_origin}/#session={capability}"
    server.startup_url = server.console_url
    return server
