# From QQQ observations to a manual paper proposal

The baseline observer reads QQQ market data from Alpaca. It preserves the VWAP,
EMA, confirmed-swing, impulse and rejection rules used in the research engine,
without simulating positions, fills or account equity. It stores completed-bar
observations privately. A human supplies a proposed entry price and quantity
before a fresh long signal can enter the existing manual paper journal.

This is an unfiltered baseline. It does not use a trained classifier, transformer
or FinRL model. No provider account is bundled. The observer does not connect to
Paper Trading by TradingView, send orders, or verify account holdings and risk.

## Prepare the data connection

Install the data dependencies from the toolkit requirements. Put the existing
Alpaca market-data credentials in a private `.env`, or the server environment
file. Do not put them in a TradingView message, public Space or Git repository.
Use the same `sip` or `iex` feed for every command sharing an observation store.
Feed entitlements still apply; a paper account alone does not imply real-time
SIP access. VWAP changes when its volume feed changes.

```sh
dwight manual-observe --feed sip --signals ./my-dwight/runs/manual-signals --once
```

Omit `--once` to poll every 30 seconds. Closed markets and unsupported early
closes produce no eligible observations. The first usable five-minute interval
needs a further 60 seconds to settle. The strategy also needs enough completed
bars and confirmed swings to form a setup; many sessions may produce no signal.

The worker reads from regular-session open and requires a complete minute-data
prefix. It never fills missing minutes. A vendor correction or disappearance of
already recorded bars halts the store for review. Keep the evidence, stop the
worker and investigate the source; do not automatically delete state to resume.
Source/configuration identity is pinned to the store. A changed strategy needs
a deliberately new state directory and experiment identity.

Continuous logs contain status and counts only. To inspect the private records,
use another terminal:

```sh
dwight manual-signals --feed sip --signals ./my-dwight/runs/manual-signals
```

This command prints private prices and feature evidence. Keep its output out of
public logs. Observations include rejected shorts and stale catch-up signals
for audit. An observation is not a recommendation or an account risk check.

## Build and review one proposal

Only a timely accepted long signal on the latest settled bar can be prepared.
Its expiry is fixed at bar close plus 120 seconds, capped at session close.
Polling again does not extend it. The human price reference must be no older
than 30 seconds and cannot be in the future.

Read the current account and market in TradingView. Review current holdings,
open orders, available cash, the spread, intended stop, daily losses and planned
quantity. Dwight has no verified account snapshot and cannot do these checks
for you. Supply the actual proposed price, whole-share quantity and timestamp:

```sh
dwight manual-prepare SIGNAL_ID --feed sip \
  --signals ./my-dwight/runs/manual-signals \
  --entry PROPOSED_ENTRY_PRICE --quantity WHOLE_SHARES \
  --price-observed-at TIME_WITH_TIMEZONE \
  --state ./my-dwight/runs/manual-paper/account.sqlite3
```

Replace all capitalized placeholders. There are deliberately no invented
current prices in this command. The supplied entry is a proposed price, never a
verified quote or fill. The stop comes from the observed strategy setup. The
target is calculated from this entry reference using the strategy's reward
multiple and tick rule. Quantity is human supplied, not sized using simulated
capital. Planned risk excludes costs, gaps and execution uncertainty.

The exact proposal and its richer observation/reference audit are saved before
delivery to `ManualPaperJournal`. Repeating identical preparation after a crash
recovers the same proposal; changed parameters for the same signal are rejected.
Expired observations are not renewed. A recovered old acknowledgment does not
make the proposal actionable again. This delivery is local journal insertion,
not a broker submission.

Continue with [manual proposal review and fill import](manual-paper.md): place
any paper order yourself in TradingView, set protective exits there, acknowledge
your decision, then preserve and normalize actual execution evidence. A
`confirmed` status is a human acknowledgment, not an execution record.

## Strategy and evidence boundaries

Confirmed pivots retain their right-side confirmation delay. Signal features
are frozen from completed data and never receive future outcome labels. The
observer preserves the setup formula, but can emit a different sequence from a
portfolio replay because simulated holdings and losses do not suppress signals.
Do not compare their trade counts as though they were identical policies.

The TradingView webhook inbox remains a separate collection path. Its alert
payloads do not supply this observer's Alpaca session history. Neither path
automatically starts a paper experiment clock or sends milestone emails.
The separate [native-paper milestone ledger](manual-milestones.md) can use the
observer's durable records after its own preparation, account-evidence and
explicit start checks. It preserves the distinction between observation
coverage, imported executions and unknown account results.

## Existing Linux host

The reviewed installer includes `dwight-manual-observer.service` and leaves it
inactive. Its defaults are SIP data, `/etc/dwight/paper.env` and private state at
`/var/lib/dwight/manual-signals`. Verify your entitlement, install a pinned
release, run the bounded observation command as the service user, and inspect
its result before enabling the service. Keep one active observer per state
directory. Manual inspection and proposal preparation can use the same private
store while it runs.

A `STOP` file inside the signal-state directory prevents new observations and
new preparation. Existing orders or protective exits in TradingView are not
changed. Removing the marker does not clear a data-revision halt.

The public Hugging Face Space remains a synthetic research demo and does not
host these credentials, observations, proposals or account reports.
