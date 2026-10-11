# Private research comparison tickets

A research ticket freezes the scope of one historical comparison before a
replay is run. Its inclusive session-date window is the ticket's time horizon.
All selected strategies share the same verified five-minute QQQ data, capital,
cost settings and simulator implementation. A ticket is research metadata only:
freezing or verifying it does not run a replay, connect an account, create a
paper session or place an order.

The first implementation supports the existing VWAP baseline and its fixed
prior-relative-volume comparator. It records a named cohort, not an open-ended
strategy marketplace. Strategy-specific parameter editing is intentionally
outside this first contract. The data source, feed, adjustment policy, dataset
and bar-file hashes, strategy-engine hash, coverage, shared simulator settings,
and user-declared evaluation role are stored in the ticket. Market data itself
is not copied into the ticket.

## Freeze a ticket

The public app's `/research` page gathers the title, date window, dataset
manifest path, strategy cohort, and shared settings. It runs in the browser and
downloads a JSON configuration file. It does not send the form to the server,
read market data, or freeze a ticket. Save the file inside the private workspace
and use the local command below to verify it and create the frozen record.

Create a private JSON definition inside an initialized workspace. For example:

```json
{
  "title": "QQQ opening window comparison",
  "window": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
  "dataset_manifest": "private-data/your-dataset/manifest.json",
  "strategies": ["vwap_baseline", "relative_volume_filter"],
  "engine_config": {
    "capital": 10000,
    "commission": 0.005,
    "slippage": 0.01
  },
  "evaluation_role": "exploratory",
  "cost_stress_multiplier": 2
}
```

Use actual dates contained by the selected manifest. The manifest, its bar file,
and the configuration must be private files inside the private workspace. The
ticket directory is created with mode 0700 and each ticket with mode 0600. A
new UUID path is used every time; existing tickets are never replaced.

```sh
chmod 600 /path/to/workspace/configs/research-ticket.json
dwight research-ticket-freeze \
  --workspace /path/to/workspace \
  --config /path/to/workspace/configs/research-ticket.json
```

The command checks the data manifest and file hashes, restricts paths to the
workspace, verifies the requested market sessions, pins the current strategy
engine, and writes a frozen definition with no results. Missing full exchange
sessions are listed and block data readiness. `data_ready` means only that the
dataset has complete supported sessions for the requested window. The cohort
runner has not been implemented, so `runner_available` remains false.
Unsupported partial or early close sessions are disclosed and excluded. An
intraday gap fails closed.

## Verify a ticket

```sh
dwight research-ticket-verify \
  /path/to/workspace/tickets/TICKET_ID.json \
  --workspace /path/to/workspace
```

Verification checks the ticket's definition and artifact checksums, current
strategy-engine identity, and the referenced data files. Checksums identify
local bytes; they are not a digital signature, provider attestation, or data
redistribution license. Licensing remains a separate review.

## Current limits

This is the pre-registration foundation, not yet the complete visual lab. The
ticket core does not execute the cohort, render synchronized charts, record a
viewed final holdout in a cross-ticket ledger, or automatically prevent a later
ticket from reusing overlapping dates. The role value is a declaration, not an
enforced split. The cost stress multiplier is recorded for the next runner
stage; this command does not calculate stress results. Replay execution and
holdout-consumption controls must be implemented before describing tickets as
a complete anti-overfitting system.

Historical simulations remain distinct from live shadow decisions, broker
paper-account fills and real-account fills. This ticket creates none of those
observations.
