# Paper execution authorization

Status: current source on `main`, tested with broker fixtures. These additions are not in the existing 0.7.1 release archive. There is no authorization CLI, authenticated control console or running execution service. The public setup page cannot create a grant or submit an order.

An account connection establishes access. A Dwight execution authorization records the narrower scope permitted for new paper entries. The first scope is a dedicated Alpaca paper account and whole-share, long-only QQQ orders.

## Grant scope

| Field | Binding |
| --- | --- |
| Provider and account | Fixed Alpaca paper endpoint and the exact broker account identity |
| Instrument and direction | QQQ buy entries only |
| Strategy | Explicit strategy identifier and a pinned 64-character hexadecimal revision |
| Allocation | Positive USD exposure allowance, in addition to the entry risk checks |
| Risk policy | Exact approved `RiskPolicy` snapshot, including feed, spread, freshness, exposure and loss limits |
| Validity | Server-recorded approval time and explicit UTC expiry |
| Operator | Local `granted_by` record; not authenticated web-user identity |

The approved specification is immutable and has a stored hash. A changed strategy, allocation, risk policy or expiry requires a new grant. State transitions are recorded separately; they do not edit the approved specification.

The strategy fingerprint is supplied by the private caller. The eventual worker must derive it from its frozen strategy code and configuration, rather than trusting an alert's claim about its version. The ledger checks that the supplied fingerprint matches the approved scope; it does not independently inspect the strategy that produced a signal.

## Library use

`PaperExecutor.authorizations` exposes the local authorization ledger. `approve(spec, now=...)` records a grant. `set_state(id, 'paused'|'active'|'revoked', now=...)` records a state transition. Revocation cannot be resumed. Explicit timestamps support deterministic tests; an operating service must use its trusted current UTC time.

Each new `PaperExecutor.submit` requires the authorization identifier and matching strategy lineage. It reconciles the broker, validates the entry, checks the current grant and persists the authorization snapshot with the order intent and risk evidence before attempting a submission. Authorization records share the executor's SQLite transaction and durable storage.

Construct the ledger, approve a scope and change its state outside any existing caller transaction. These operations reject a pending transaction without committing or rolling it back. Entry validation belongs inside the transaction that persists the new intent.

The broker response is associated with the same client order identifier. Submission timeouts trigger lookup and reconciliation, never a blind second submission. Reusing a signal with changed order fields or authorization lineage is rejected. Historical intents keep their original evidence and cannot be silently attached to a new grant.

## Pause and recovery

Pause, revocation and expiry block new intents. An order already committed for submission can remain in flight or fill afterwards. These controls cannot recall a broker request or guarantee that the account is flat.

Reconciliation and cancellation of owned, unfilled entries do not require an active grant. They must remain available to recover previously accepted orders. Existing protective orders must not be canceled merely because authorization ended. Closing a position requires a separate, explicit exit policy and confirmed broker evidence.

## Remaining execution work

This library is not the complete automatic trading product. Partial-entry protection, cancellation races, continuous exit supervision, session-close handling, authenticated customer consent and broker-derived reporting still need implementation or verification. The current executor blocks new entries on a partial bracket fill; that does not protect the already filled quantity.

Allocation and loss limits are entry checks, not guaranteed maximum losses. Stops can fill at worse prices, and account equity or broker state can change after a snapshot. A private operator must verify the account and lifecycle before any automatic paper pilot.

The ledger does not implement Mastercard protocols or claim cryptographic proof of customer intent. Licensed data, credentials, grants and account evidence stay private. See [the automatic paper product specification](automatic-paper-product.md) for the full readiness gates.
