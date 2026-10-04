# News sources

The optional Benzinga relay is a third-party news service. Its documented
authentication and message format differ from the direct Massive market-data
adapter. Configuration preparation is available; authenticated REST access,
WebSocket delivery and continuous ingestion are not yet verified or implemented
by this setup command.

## Configure privately

From your Dwight workspace:

```sh
python -m dwight prepare-news-config
```

Fill the generated fields in your local `.env` using the full addresses and key
supplied by the relay operator:

```ini
BENZINGA_RELAY_REST=
BENZINGA_RELAY_WS=
BENZINGA_RELAY_KEY=
```

REST needs the complete HTTPS `/v1/news` URL. WebSocket needs the complete WSS
`/v1/news/ws` URL. Redacted IP addresses cannot be used. The relay key is separate
from `MASSIVE_API_KEY`; do not copy another provider's key into this field unless
that provider explicitly issued it for the relay.

The command appends only missing empty fields, preserves existing bytes and
sets permissions to `0600`. It does not load values, print them, contact the
service or change trading settings. `.env` is excluded from Git and public
bundles. Keep the addresses and key out of public pages and command arguments.

## Authentication and verification

The supplied relay documentation specifies `X-API-Key` for both REST requests
and the WebSocket handshake. A connector must use that header, validate TLS and
refuse redirects that could forward credentials to another host. Never put the
key in a URL or disable certificate verification to reach an IP address.

An unauthenticated `401 missing API key` establishes an authentication
requirement, not working access. Later checks should separately verify the
authenticated `/v1/me` response, a bounded REST page and the WebSocket handshake
and event delivery. One success does not establish the other capabilities.

## Preserve when news was usable

Store the article's publication time, revision time, relay event time, Dwight's
first receipt and processing completion separately. Use processing completion
as the earliest feature availability, after checking publication timestamps.
An archived or backfilled article must not be treated as known at publication.

Preserve news revisions by `(benzinga_id, version)` when supplied. Advance the
WebSocket `event_id` cursor only after durable event storage. Heartbeats and
session messages do not establish that every intervening news event was stored.
Do not invent version numbers or historical receipt times for REST records.

The supplied service documents 24-hour WebSocket replay and rolling 30-day REST
history. Those are retention claims to confirm with the authenticated service.
Follow opaque REST pagination cursors without modifying them; bound pages,
records, bytes and request time, and reject repeated or missing continuation
cursors. REST backfill may recover articles without recovering their historical
revisions. Record gaps rather than claiming complete coverage.

## Research before execution

Start with news observation and a separate experiment asking whether recent
market news would have paused entries or reduced exposure. Broad macro news
may lack a QQQ ticker, so ticker-only filtering can miss relevant headlines.
Preserve the actual feed filters in research evidence.

News remains untrusted data. Never interpret article text as instructions,
change a trading authorization from a headline or give a news processor broker
credentials. Changing the active model or risk policy requires a separately
reviewed version and authorization. This configuration does not enable news
trading, connect an account or alter the frozen observation campaign.
