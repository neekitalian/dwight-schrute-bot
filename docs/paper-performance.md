# Private paper execution performance

Dwight reports owned QQQ executions from Alpaca's paper-account `FILL`
activities. Each ledger record keeps the provider activity ID, broker order ID,
execution timestamp, side, quantity, price and original response. An order's
`filled_qty`, `cum_qty` or `filled_avg_price` is cumulative evidence; none is
expanded into individual executions. Alpaca documents partial and full fills
in its [account activity schema](https://docs.alpaca.markets/us/docs/account-activities).

## Collect and freeze a report

Use the initialized private worker workspace and its locally configured paper
credentials:

```sh
.venv/bin/dwight paper-report --directory runs/automatic-paper \
  --html-output runs/paper-reports/session-review
```

The output directory must be new. It contains a self-contained HTML report,
frozen JSON, and a manifest with SHA-256 checksums. The manifest is published
after both artifacts finish. Files use mode `0600`; the directory uses `0700`.
Keep these account records outside public sites, source control and shared
bundles. The HTML escapes broker strings and loads no remote assets.

The worker also polls reporting independently of order supervision. Reporting
uses broker GETs and a separate `reporting.sqlite3`; it never writes the
execution ledger or submits, cancels or closes an order. A failed reporting
request cannot suspend the supervisor. The console shows a fresh, matching
account summary only when quantity reconciliation succeeds.

## What the numbers mean

An execution belongs to Dwight only when its broker order ID is evidenced by
an owned persisted entry, its bracket legs, or a persisted supervisor close
linked to that entry. Fresh read-only order snapshots discover new bracket leg
IDs and check persisted identities. Matching a symbol or client-ID prefix is
insufficient. Unrelated activities remain separately marked in the private
ledger and are excluded from Dwight metrics.

Gross realized FIFO P&L matches each owned sell execution against the oldest
remaining owned buy quantities: matched quantity times sell price minus buy
price. Partial fills retain their actual separate prices. No disposal means
realized P&L is unavailable; an evidenced breakeven disposal can equal zero.
The offline curve accumulates realized gross P&L only at actual disposal times.
It is not an account equity or net-return curve.

Fees, net realized P&L and account returns remain unavailable. `FILL` activities
do not establish all charges, cash deposits or withdrawals, account resets,
corporate actions or adjusted basis. No zero fees, estimated net result, or
return percentage is substituted for missing evidence. Paper executions are
broker simulations and do not establish live performance.

## Coverage and reconciliation

Each collection freezes one cutoff and re-fetches the full reporting window
from just before the earliest owned intent. The lower bound cannot silently
move forward. With no owned intents, there is no invented start date, activity
window or performance. The activities endpoint uses the last activity ID as
its pagination token; a short nonempty page still requires another request.
See Alpaca's [activity request and pagination reference](https://docs.alpaca.markets/us/reference/getaccountactivities-2).

An empty final page proves only that the requested provider window was fetched.
Provider completeness remains unverified. Late-booked fills and corrections
can change later responses. Identical duplicate IDs are deduplicated; conflicting
IDs, missing previously recorded activities, malformed or future executions,
unsupported replacements, ambiguous equal execution timestamps, and oversold
inventory reject collection. Prior immutable evidence and reports are preserved.

The report compares execution quantities with each owned order's cumulative
filled quantity and the current broker QQQ position. These snapshots are not
atomic with the frozen activity cutoff. A concurrent execution or booking lag
can therefore produce a retryable mismatch. The source activities are retained,
but gross P&L and the curve are withheld until quantities reconcile. A position
snapshot alone never supplies missing execution prices or opening basis.

## Bounded private state

The default collection allows 20 pages of at most 100 rows, including the empty
final page. The library permits at most 200 pages and 20,000 unique activities.
Source and ownership evidence are bounded to 16 MiB; execution ownership is
limited to 1,000 intents. Reporting state stops safely at 32 MiB or 20,000 compact
immutable report snapshots. The count limit is approximately thirteen days of
once-per-minute reports; the byte or source-window limit may arrive sooner.

Compact snapshots reference the immutable activity ledger by checksum rather
than repeating execution history each minute. Initial state is published from
a completed private temporary database; later source records and snapshots
commit together. Hardlink aliases, symlinks and account/scope changes are rejected.

At a limit, reporting becomes unavailable and supervision continues. Preserve
the old private ledger and frozen reports before arranging a new reporting
ledger. A new ledger still needs the complete owned execution history within
the source bounds; it does not justify discarding earlier fills or inventing
opening basis. Never edit the immutable records to bypass a discrepancy.

Fixtures use separate reporting ledgers, an explicit fixture marker, and a
visible `FIXTURE` label on every rendered preview. Synthetic examples must
never be mixed with broker responses or presented as actual paper results.
