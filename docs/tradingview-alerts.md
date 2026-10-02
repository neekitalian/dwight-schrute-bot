# TradingView observation inbox

Dwight can receive and privately journal a closed QQQ five-minute bar from a
TradingView alert. This first version is an **observation bridge**: it does not
run a model, propose or approve trades, send orders, manufacture fills, or change
the native TradingView Paper Trading account. Every accepted event remains
`unreviewed` and `source_verified=false`.

The inbox and [manual paper journal](manual-paper.md) are separate databases and
APIs. An alert dictionary cannot become an approved manual proposal. Any future
strategy adapter needs a separately reviewed model, market-data compatibility
checks, risk checks, and an explicit human review boundary. Reading an inbox bar
does not establish eligibility for a trade.

## Local setup

The receiver requires a dedicated `DWIGHT_TRADINGVIEW_CAPABILITY` environment
variable: 32–128 URL-safe ASCII letters, digits, `_` or `-`. Provision a random
value with at least 32 random bytes using a private credential manager or local
environment file. It is independent of all broker credentials. Missing or
malformed configuration fails closed; no key is generated automatically.

The CLI reads the ignored local `.env` file through the existing safe `KEY=value`
loader. Restrict that file to its owner (mode 0600). Do not commit it, use a
broker key as the capability, or include the capability in source code, Pine,
alert message JSON, screenshots, shell history, support reports, or public logs.

```bash
python -m dwight tradingview-serve \
  --state runs/tradingview/inbox.sqlite3 --host 127.0.0.1 --port 8765
```

This starts a foreground process only when the operator runs it. There is no
automatic startup or deployment. The service binds to IPv4 loopback only.
`GET http://127.0.0.1:8765/healthz` reports process liveness and the
observation-only mode, without credentials, event data, or account information.
Health is not a model, data freshness, broker, or delivery check.

Review accepted observations privately:

```bash
python -m dwight tradingview-list --state runs/tradingview/inbox.sqlite3
```

The Python API is dependency-free:

```python
from dwight.tradingview import TradingViewInbox, make_server

inbox = TradingViewInbox("runs/tradingview/inbox.sqlite3")
server = make_server(inbox)  # Reads DWIGHT_TRADINGVIEW_CAPABILITY; does not start.
try:
    server.serve_forever()
finally:
    server.server_close()
```

`inbox.accept(payload, now=None)` validates and persists one observation.
`inbox.list_events(limit=100)` returns up to 1,000 observations for local review.
An aware `now` datetime is accepted for deterministic tests; production uses UTC
system time. There is no HTTP endpoint to read events or change review status.

## Alert message contract

Send UTF-8 JSON using `POST /alerts/<CAPABILITY>`. The body is at most 8,192 bytes;
`Content-Length` and `application/json` are required. Compressed or chunked
bodies, duplicate JSON fields, unknown fields and non-finite values are rejected.

This invented example uses fixed timestamps to describe the schema. It is not
a signal, account record, or current market data; unchanged timestamps will
normally fail the freshness check:

```json
{
  "schema_version": 1,
  "event_id": "QQQ-5-1791210900000-observer-v1",
  "kind": "bar_observation",
  "symbol": "QQQ",
  "exchange": "NASDAQ",
  "timeframe": "5",
  "bar_open_at": "2026-10-05T14:30:00Z",
  "bar_close_at": "2026-10-05T14:35:00Z",
  "sent_at": "2026-10-05T14:35:01Z",
  "bar_closed": true,
  "open": 100,
  "high": 102,
  "low": 99,
  "close": 101,
  "volume": 12345,
  "source": "dwight_qqq_observer",
  "strategy_version": "observer-v1"
}
```

All shown fields are required. Version, kind, symbol, exchange, timeframe and
closed flag must match exactly. `strategy_version` versions the observer
contract; it is not a model approval. IDs/source/version start with an ASCII
letter or digit and contain only letters, digits, `_`, `.`, `:`, `-`, up to 160
characters. No instructions, account, quantity, side, status, or fill fields are
allowed. Text fields remain untrusted metadata.

Timestamps must have explicit timezones and are normalized to UTC. The bar must
last exactly five minutes and open on a five-minute boundary. `sent_at` must be
at or after its close. A new event must arrive within 300 seconds of the close;
future bars and send times more than five seconds ahead are rejected. The
receiver's clock must be accurate. OHLC prices must be positive, finite, within
the bar's low/high, at most 10^12, and use at most eight fractional digits.
Volume has the same bounds but may be zero. Numbers or numeric strings are
normalized identically. The inbox does not verify that a claimed bar was an
actual exchange session or that its prices are authentic.

SQLite enforces unique event IDs and a second identity of source + version + bar
close. Identical normalized retries return the original event without refreshing
its receipt time, even after the freshness window. A reused identity with any
different normalized content returns 409. Send the same body when retrying;
changing `sent_at` produces a conflict. A second ID cannot turn the same observer
bar into a second signal. Restarting the process preserves these guarantees.

| Response | Meaning |
|---|---|
| 202 | New observation stored, still unreviewed. |
| 200 | Identical event already stored; no new action. |
| 400 | Invalid JSON or schema. |
| 401 | Wrong capability path. |
| 409 | Event identity conflicts with prior evidence. |
| 411 / 413 / 415 | Missing/invalid length, body too large, or unsupported encoding/media. |
| 422 | Stale event or future timestamp. |
| 503 | Inbox unavailable; investigate storage/lock health. |

## Pine template and chart connection

[`examples/tradingview/qqq_observer.pine`](../examples/tradingview/qqq_observer.pine)
is a Pine v6 **uncompiled template**. It has not been compiled in TradingView or
tested against a real alert delivery. It emits only for NASDAQ QQQ, a standard
five-minute chart, regular-market session bars, confirmed realtime bars, and
present OHLCV. It is not a replica of the VWAP strategy and its marker is not a
trade recommendation or evidence of successful webhook delivery.

After validating the template in Pine Editor, add it to the intended chart and
create an alert for its `alert()` calls. Configure the private HTTPS webhook URL
in TradingView's webhook field, never in the JSON message. Recreate the alert
after changing the script, symbol, timeframe, or inputs: TradingView runs a
snapshot saved at alert creation. A script alone does not create a running
alert. These behaviors are documented in [TradingView's Pine alert guide](https://www.tradingview.com/pine-script-docs/concepts/alerts/).

No TradingView account has been connected or changed by this module. The
[user-provided chart](https://www.tradingview.com/chart/d5qUHtf0/) remains an
unverified layout reference. Its access, contents, data subscriptions and current
alert settings must be verified during the final connection test. TradingView
chart data is not assumed equivalent to Alpaca IEX/SIP or any research dataset;
this inbox does not promote data into the model pipeline.

## HTTPS and URL capability constraint

TradingView documents JSON HTTP POST webhooks, requires 2FA, accepts ports 80 and
443, and cancels requests after three seconds. Deliveries can fail; monitor the
alert log's webhook status. [Official webhook guide](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/).

TradingView cannot reach localhost. A later deployment needs a separately
configured HTTPS reverse proxy on port 443 forwarding to the loopback receiver.
The app does not publish, configure or activate that proxy. Use a public CA
certificate; restrict requests to the documented method/path, buffer and limit
the body to 8 KiB, apply connection/rate limits, and use short upstream timeouts.
The stdlib server handles one bounded request at a time and is not an Internet
edge server. SQLite has a short lock timeout; a slow or locked inbox may return
503 and requires operational monitoring. Host protection, storage capacity,
backups, and process supervision remain operator responsibilities.

The socket has a one-second inactivity timeout for request headers and body;
this is not an absolute deadline against a continuously trickling client. Python
also bounds individual header lines and their count. The proxy must finish and
buffer the request before forwarding it. The reviewed starting templates are
[`deploy/linux/tradingview-nginx.conf.example`](../deploy/linux/tradingview-nginx.conf.example)
and [`deploy/linux/dwight-alert-inbox.service`](../deploy/linux/dwight-alert-inbox.service).
They have not been installed or tested as a public endpoint. Supply a real domain,
certificate, private environment file and service account before enabling them.

**The URL is a credential.** This design fits TradingView's configured URL/body
interface without requiring custom authorization headers or embedding secrets
in the body. It does not cryptographically prove TradingView was the sender.
Anyone holding the full capability URL can submit valid-looking observations.
Disable or redact full request URI, query string, Referer and request-body logging
at every proxy, CDN, monitoring agent and error tracker. Application request/error
logs are disabled to avoid leaking the path. TLS hides it in transit, but the
TradingView configuration and endpoint infrastructure still hold it. Do not put
the URL in browser links or public health endpoints. Rotate the private value
and update the TradingView webhook URL if it is exposed. A proxy allowlist using
TradingView's currently documented sender addresses can provide another control;
check the official list during deployment rather than trusting a copied list.

New inbox directories use 0700 and database files 0600. This is private local
storage, not encryption or a tamper-proof ledger. Keep the inbox under ignored
`runs/` or `private-data/`, separate from the manual journal and public artifacts.
There is no automatic retention policy; preserve idempotency evidence when
archiving. The code contains no order adapter, brokerage login, or fill generator.

TradingView's [strategy FAQ](https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account)
states Pine cannot directly place orders in native Paper Trading or integrated
brokers. Alpaca separately documents [paper and live connections to TradingView](https://alpaca.markets/learn/how-to-trade-options-on-tradingview-with-alpaca-trading-api-account).
That optional broker connection is separate from this inbox and the selected
native Paper Trading workflow. No broker execution is implemented here.
