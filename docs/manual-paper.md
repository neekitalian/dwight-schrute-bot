# Native TradingView paper workflow

Dwight can journal proposals and import normalized fill evidence for **Paper Trading by TradingView**. This is the account selected for this project. It is separate from an Alpaca paper account.

This module never sends an order, controls a browser, logs into TradingView, or changes an account. TradingView's [Pine strategy FAQ](https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account) says Pine strategies cannot place orders in the built-in Paper Trading account. An alert or webhook does not remove this limitation.

## User flow

1. A reviewed strategy produces a QQQ long-entry proposal with prices, quantity, model version, and an expiry time. The current journal accepts an explicit proposal JSON file; it is not yet wired to the shadow worker to generate proposals automatically.
2. Review the proposal in the private local queue. Compare current prices, account holdings, costs and risk before acting. A historical replay candidate must not be presented as a fresh opportunity.
3. Place the paper order yourself in TradingView. Configure any protective exits there. Dwight's journal cannot enforce a stop or guarantee its execution price.
4. Mark the proposal `confirmed` to record your decision, or `skipped`. Confirmation is **not** evidence of a submitted or filled order. Pending proposals expire at `expires_at`; late confirmation is rejected and expiration remains recorded.
5. Obtain execution records from the account, preserve the original evidence privately, and convert them to the normalized CSV below. Only import executions, not order requests or summary profit rows.
6. Import fills into the same private journal and review FIFO realized PnL, remaining inventory, and differences from linked proposals. Compare the journal's inventory with the account manually.

The initial scope is QQQ long inventory in USD. Fractional quantities up to eight decimal places are accepted for accurately recording supplied fills. This does not promise fractional support for any account or strategy. The journal begins with zero inventory; include all opening buys before importing sales. Short selling, position adjustments, stock splits, transfers, and other instruments are not supported.

## Command line

Use a reviewed, current proposal JSON matching the schema below:

```bash
python -m dwight manual-propose private-data/proposal.json \
  --state runs/manual-paper/account.sqlite3
python -m dwight manual-list --state runs/manual-paper/account.sqlite3
python -m dwight manual-status YOUR-PROPOSAL-ID confirmed \
  --state runs/manual-paper/account.sqlite3
```

A human must place the order in TradingView. `confirmed` only records the human
acknowledgment. Use `skipped` when declining a proposal. The CLI requires current
available/expiry times; the documentation's invented prices are not a signal.

After converting actual execution evidence to Dwight's documented CSV schema:

```bash
python -m dwight manual-import private-data/normalized-paper-fills.csv \
  --paper-export --state runs/manual-paper/account.sqlite3
python -m dwight manual-report --state runs/manual-paper/account.sqlite3 \
  --output runs/manual-paper/report-001.json
```

`manual-report` without `--output` prints the full private JSON report to stdout.
With `--output`, it creates a new mode-0600 JSON file and prints only its location
and evidence labels. Existing paths are never overwritten. Select a new filename
for each snapshot. Do not redirect private stdout into a public log.

For a private visual report, use a new output directory:

```bash
python -m dwight manual-report --state runs/manual-paper/account.sqlite3 \
  --html-output runs/manual-paper/visual-report-001
```

Open the returned HTML locally. It contains an inline realized-PnL chart, costs,
remaining inventory, imported fills and proposal discrepancies. It loads no
external scripts or fonts. Its frozen JSON snapshot and checksum let you identify
the exact evidence used. Both reports contain private execution data; share only
with authorized recipients. The chart is cumulative realized PnL after sell
executions, not account equity or drawdown. Account return and unrealized PnL
remain unknown. An empty journal means no imported evidence, not verified zero
account activity. Paper-export labels do not establish authenticity or a complete
account history, and these checks do not prove full strategy adherence.

`--html-output` and `--output` are mutually exclusive. The HTML directory must
have an existing parent, must not already exist and must not traverse a symlink. New report directories use
mode 0700 and files mode 0600. This command snapshots current journal evidence;
it does not schedule milestones, place an order or send email.

Run the invented accounting fixture in a separate journal:

```bash
python -m dwight manual-import examples/manual-fills.csv --synthetic \
  --state runs/manual-demo/account.sqlite3
python -m dwight manual-report --state runs/manual-demo/account.sqlite3
```

Every import requires either `--synthetic` or `--paper-export`. Every CSV row must
have the matching `data_kind`. The CLI validates and imports the same frozen
input bytes. This prevents a sample fixture from being silently labelled as paper
results; `--paper-export` still does not independently verify an account export.
JSON inputs must be objects, at most 1 MiB, with no duplicate keys or non-finite
numbers. Validation failures return a nonzero exit status without a traceback.

## Private journal API

```python
from datetime import datetime, timedelta, timezone
from dwight.manual import ManualPaperJournal

now = datetime.now(timezone.utc)
journal = ManualPaperJournal("runs/manual-paper/account.sqlite3")
proposal = journal.add_proposal({
    "proposal_id": "QQQ-20261005T143000-vwap-model1",
    "account": "tradingview_native_paper",
    "symbol": "QQQ",
    "side": "buy",
    "quantity": "2",
    "entry": "500.00",
    "stop": "498.00",
    "target": "504.00",
    "source": "vwap_pullback",
    "model": "reviewed-model-name",
    "version": "immutable-model-and-code-version",
    "signal_at": now.isoformat(),
    "available_at": now.isoformat(),
    "expires_at": (now + timedelta(minutes=5)).isoformat(),
}, now=now)
# The numbers above are invented schema examples, not a trading signal.
journal.list_proposals(now=now)
journal.set_status(proposal["proposal_id"], "skipped", now=now)
# With a separately prepared private file:
# journal.import_fills("private-data/normalized-paper-fills.csv")
report = journal.report()
```

Public methods:

- `ManualPaperJournal(path)` opens or creates one private SQLite file.
- `add_proposal(payload, now=None)` returns the canonical proposal and planned risk arithmetic. Reusing an ID with identical normalized content is idempotent; changed content is rejected.
- `list_proposals(now=None)` returns all proposals and expires pending entries that are no longer current.
- `set_status(proposal_id, status, now=None)` records `confirmed`, `skipped`, or `expired`. Terminal status cannot be changed. Explicit expiration is accepted only at or after expiry.
- `import_fills(csv_path)` atomically imports a normalized CSV and returns inserted/duplicate counts.
- `report(now=None)` returns JSON-serializable evidence, fills, FIFO matches, realized PnL, and the remaining position. It also expires pending proposals.

`now` is an optional timezone-aware Python datetime for deterministic tests. Production defaults to the current UTC time. Journal status time cannot go backwards, and a report cannot use a time before its latest imported fill. This is a current evidence report, not a historical as-of query engine.

All proposal fields shown above are required; unknown fields are rejected. `signal_at <= available_at < expires_at` is required and a new proposal must already be available and not expired. Long prices require `0 < stop < entry < target`. Quantity and prices must be positive finite decimals with at most eight decimal places and magnitude at most 10^12. Calculated stop risk and reward exclude costs and gap/slippage risk; these are explanatory calculations, not portfolio risk enforcement.

Keep source/model/version values specific enough to recover the reviewed decision. The journal records these strings; it does not independently verify an artifact checksum, data source, or strategy eligibility. Those checks belong to the upstream strategy and release process.

## Normalized fill CSV

**This is not a tested adapter for native TradingView exports.** No actual export sample has been provided. Column names and semantics below are Dwight's interchange contract. Preserve the original export and mapping procedure privately. A future native adapter should be tested against an actual export, including partial executions and fees.

Required header:

```csv
fill_id,filled_at,sequence,symbol,side,quantity,price,fee,data_kind,proposal_id
```

`proposal_id` is the only optional column. When present it may be empty.

| Field | Meaning |
|---|---|
| `fill_id` | Stable execution identity. A partial fill needs its own ID; an order ID alone is insufficient. Use the same ID when reimporting the same execution. |
| `filled_at` | Actual execution timestamp, ISO 8601 with an explicit timezone. Converted to UTC. Future timestamps are rejected. |
| `sequence` | Nonnegative integer specifying actual order among executions at the same timestamp. Use 0 for a unique timestamp. Timestamp plus sequence must be unique. Do not guess an ambiguous execution order. |
| `symbol` | Exactly `QQQ`. |
| `side` | `buy` to add long inventory, `sell` to reduce it. |
| `quantity` | Positive executed quantity, not requested order size. Up to eight decimal places. |
| `price` | Positive execution price in USD. |
| `fee` | Nonnegative fee in USD for this execution. Zero is explicit; missing fees are rejected. Negative rebates are not supported. |
| `data_kind` | `synthetic` for invented fixtures or `paper_export` for user-supplied paper execution evidence. A label does not verify authenticity. |
| `proposal_id` | Optional link to an existing local proposal for audit. Unlinked real manual fills can still be recorded. |

Text identifiers start with an ASCII letter or digit, followed by letters, digits, `_`, `.`, `:`, `/` or `-`, up to 160 characters. Numbers must be finite and bounded as above. Headers must not repeat; missing cells, unexpected columns, malformed timestamps and invalid fees are rejected. CSVs are bounded to 64 MiB and 100,000 rows per import.

The entire import is one transaction. Exact duplicates are skipped after normalization. A conflicting ID, unknown proposal, ambiguous execution sequence, mixed synthetic/paper evidence, or any chronological oversell rolls back every new row in the batch. File row order does not determine FIFO; timestamps and explicit sequences do. Earlier fills can be added later and will recalculate the entire FIFO history, so previous reports may change when evidence is supplemented. Imported fills cannot be edited or deleted through this API; conflicting records need investigation against source evidence rather than silent replacement.

Synthetic and paper-export fills must use **separate journals**. The committed `examples/manual-fills.csv` is entirely invented and tests accounting only. Its result is $49.40 realized PnL, 2 QQQ shares remaining, and $220.20 remaining cost including allocated entry fees. It is not Dwight performance or a TradingView statement.

## Accounting and reconciliation

For a closed FIFO quantity:

`realized PnL = quantity × (exit price − entry price) − allocated entry fees − allocated exit fees`

Each exit consumes the oldest recorded buys. Partial exits receive proportional fees; the final portion takes the exact remaining fee. Decimal arithmetic is used internally, and report money is rounded to eight decimal places. Individual rounded matched segments can differ from their aggregate by rounding fractions. The total is calculated before display rounding.

The report deliberately distinguishes:

- `realized_pnl_curve`: cumulative net realized PnL after each exit. It is not an equity curve.
- `net_trade_cash_flow`: cash movement from the imported trades and fees. Open inventory makes this different from profit.
- `open_position`: remaining quantity, FIFO lots and cost including remaining entry fees.
- `account_equity`, `account_return_pct`, `mark_price` and `unrealized_pnl`: unknown because neither account balances nor executable quotes have been verified.
- `closed_exit_count`: number of sell executions, not independent strategy trades. Partial exits must not inflate a claimed win rate.

Every report is `broker_verified=false` and `performance_scope=imported_fills_only`. Reconciliation is `unknown_open_positions` when inventory remains or `unverified_no_account_snapshot` when flat. Flat local inventory alone does not prove complete account history. Deposits, withdrawals, dividends, interest and other holdings are not included.

Linked entry fills are checked for availability, expiry, manual confirmation, planned quantity and price differences. Human acknowledgment timing is recorded separately because acknowledging a fill afterward is legitimate. An actual late or different-price execution is preserved and flagged, rather than rewritten to match the proposal. The audit records `human_confirmation_at` and `confirmation_timing` as
`before_fill`, `at_fill`, `after_fill` or `not_confirmed`; timing is descriptive,
not a violation. Each linked buy also reports `proposed_entry_quantity`,
`linked_entry_quantity_to_date` and `entry_quantity_excess`. Cumulative buys over
the proposal quantity receive `entry_quantity_exceeds_proposal`. Exits do not
reset that proposal's entry budget, duplicate reimports do not increase it, and
unlinked fills are not attributed to a proposal. These checks are an audit trail,
not a claim that all strategy and risk rules were followed.

## Storage and operation

New journal directories use mode 0700 and database files mode 0600. SQLite transactions use full synchronization and serialize writers. Initialization checks database structure and validates stored evidence; corrupt files are rejected rather than reinitialized. This is not encryption or a tamper-proof ledger. Protect the host and its backups.

Keep journal files, original exports, reports and proposal JSON under private paths such as ignored `runs/` or `private-data/`. Do not include them in the public Hugging Face Space, GitHub, logs or screenshots. This module has no email transport. Exported report files must receive private file permissions from the caller.

Local use and CSV reconciliation do not require DigitalOcean. An always-on server becomes useful for live data observation and scheduled report generation. It does not automate the native TradingView paper account, and this journal is not yet connected to the milestone scheduler or public dashboard.
