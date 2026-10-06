# Paper order supervision

Status: conservative library verified with deterministic broker fixtures. No real broker lifecycle, fills or trading results were verified by these tests. It supports one dedicated Alpaca paper account, whole-share QQQ long entries and owned exit orders.

Use `SupervisedPaperExecutor` from `dwight.paper_supervision` for the automatic worker. It extends `PaperExecutor` with a private recovery journal and includes additional close-order fills in reconciliation. Do not reopen its ledger with the base executor, which cannot account for these separate sell orders.

## Worker interface

`SupervisedPaperExecutor(client, db_path, policy, close_buffer_seconds=300, entry_timeout_seconds=120)` retains the original exclusive ledger lock and fixed paper endpoint.

Call `supervise()` on every worker pulse before considering new entries. Continue these calls when market data fails, strategy evaluation fails, the operator pauses entries or entry authorization expires. Supervision does not require an active entry authorization.

The pulse returns session state, recovery reasons, pending jobs and whether the order lifecycle permits a new entry. `entries_allowed` is only a lifecycle gate. Valid authorization, fresh signals, cash, quotes and all entry risk checks are still required. Incoherent account snapshots or unknown submission outcomes raise `ReconciliationRequired`.

New entries use `submit(proposal, quote, authorization_id=..., signal_expires_at=...)`. The automatic worker must provide the fixed signal expiry and use the trusted current clock. Explicit `now` values are for deterministic tests. The subclass rechecks expiry after broker reads, requires cash sufficient for the whole-share limit order and refuses entries during the closing buffer.

## Recovery sequence

1. Reconcile the account identity, all owned parent orders and bracket legs, separately persisted close orders, cumulative fills, open orders and positions.
2. Record a recovery job for partial entries, stale unfilled entries, missing protection, an executable exit remaining after the position closes, overnight exposure or the exchange closing window.
3. Persist one cancellation attempt before sending it. A cancellation acknowledgment is not terminal status. Later pulses read the actual order outcome and any concurrent fills.
4. Before a close, require a terminal parent and terminal bracket legs. Refresh account ownership and fills after clock checks. The verified remaining quantity must match the dedicated account position.
5. Persist a stable QQQ sell market order with `day` time in force, then submit it once. Reconcile the actual response and subsequent fills.

There is one close intent per parent entry. A timeout triggers a lookup using its persisted client identifier. An ambiguous POST is never retried. A partial close remains under observation; a rejected or canceled close with residual exposure requires operator reconciliation rather than an automatic replacement order.

A cancellation with an unknown outcome is disclosed immediately. An unconfirmed cancellation older than 60 seconds sets `cancellation_stalled` and requires operator attention. Neither case repeats the cancellation blindly or submits an overlapping close.

Unknown orders, unexplained positions, fractional or short quantities, replaced orders, backwards cumulative fills or conflicting snapshots halt new entries and prevent recovery mutations. Supervision never cancels unrelated orders and does not automatically buy to cover a short position.

## Session boundaries

The daily broker calendar supplies the actual close, including early closes. The default worker starts canceling owned entries and bracket exits five minutes before that close. Each mutation refreshes clock, ownership and fill evidence. A slow scan that makes the clock stale fails closed.

After hours, supervision preserves confirmed protective bracket exits instead of removing them when it cannot execute a closing sell. It can cancel an unfilled entry to prevent later entry. Unresolved exposure remains visible and blocks new entries; an overnight recovery job is handled when the market is open. No market order is intentionally queued after the regular session closes.

Closing before the bell is a target, not a guarantee. A disconnected worker, trading halt, delayed cancellation or rejected exit can leave exposure. Report the unresolved state; do not label a cancellation request or a submitted sell as a flat account.

## Broker validation still required

The fixture tests exercise cancellation races, partial fills, durable close intents, timeout lookup, restart recovery, protection overlap, early closes, account conflicts and cash or expiry rejection. A private pilot must additionally verify actual bracket status transitions, account fields, broker clock and calendar responses, cancellation latency, rejected exits and fill accounting.

REST reads are separate snapshots and cannot eliminate concurrent external activity. Use a dedicated account with one execution process. Terminal order evidence and coherent positions reduce known races; they are not an atomic broker-side reduce-only guarantee. Market exit prices and stop execution prices are not guaranteed. Polling every historical intent also needs an operational latency review before expanding the trial.

Alpaca documents activation of bracket exits after the parent is fully filled, group cancellation and possible competing exit fills during fast markets. Its calendar provides per-day session boundaries. The implementation follows those documented constraints and treats cancellation as asynchronous confirmation. [Alpaca order lifecycle](https://docs.alpaca.markets/us/docs/orders-at-alpaca), [Alpaca calendar](https://docs.alpaca.markets/us/v1.1/reference/getcalendar-1)
