# Native TradingView paper experiment milestones

The `manual-campaign-*` commands track a private, manually operated QQQ paper
experiment at 12, 24, 48 and 168 elapsed hours. They read the baseline observer's
stored evidence and the native-paper journal. They do not connect to an account,
place orders, send email or start the separate model-shadow campaign.

Preparation does not start the clock. An account evidence file is a human
statement supported by archived source material, not authenticated broker access.
Synthetic fills and setup observations cannot stand in for a forward experiment.

## Prepare a separate experiment

Set up the [baseline observer](manual-observer.md) and a dedicated
[manual paper journal](manual-paper.md). Keep the same feed and frozen strategy
identity throughout the experiment. Do not mix synthetic fixtures, unrelated
account activity or the shadow campaign into these files.

Initialize a new private campaign directory with the explicitly reviewed USD
allocation and recipients:

```sh
dwight manual-list --state ./my-dwight/runs/manual-paper/account.sqlite3
dwight manual-campaign-init ./my-dwight/runs/manual-campaign \
  --signals ./my-dwight/runs/manual-signals \
  --state ./my-dwight/runs/manual-paper/account.sqlite3 \
  --recipient first@example.com --recipient second@example.com \
  --allocation ALLOCATION_USD
```

Replace the allocation placeholder and example recipients. The allocation is a
human declaration, not an account balance, deposit or enforced position limit.
The prepared campaign retains its experiment identity, paths, recipient list
and milestone schedule. The application version, baseline identity, feed and
observation timing policy are pinned too. It has no start timestamp or due deadlines yet.
Initialization requires an existing observation store and an empty dedicated
journal with no proposals or fills. The campaign's parent directory must exist;
the campaign directory itself must be new.

## Record account evidence and start

Inspect Paper Trading by TradingView directly. Preserve the original account
evidence privately, and record its capture time, paper-account identity and
reconciliation facts. Do not substitute an order request, a chart alert, a
replay fill or an invented balance for account evidence. The evidence format is
Dwight's own explicit schema; it is not a native TradingView export adapter.

The JSON object must contain exactly these fields. This is a template, not an
account statement: replace every uppercase placeholder, use the frozen
observation feed, and record the actual quantities and order count. Set each
review checkbox to `true` only after completing that review.

```json
{
  "schema_version": 1,
  "account": "tradingview_native_paper",
  "account_alias": "PRIVATE_OPAQUE_ALIAS",
  "observed_at": "ACCOUNT_CAPTURE_TIME_WITH_TIMEZONE",
  "equity_usd": "OBSERVED_EQUITY_USD",
  "cash_usd": "OBSERVED_CASH_USD",
  "qqq_quantity": "OBSERVED_QQQ_QUANTITY",
  "qqq_open_orders": 0,
  "chart_symbol": "QQQ",
  "chart_timeframe": "5Min",
  "chart_feed": "ACTUAL_CHART_FEED_DESCRIPTION",
  "chart_realtime": false,
  "observation_feed": "sip",
  "feed_comparison_note": "ACTUAL_CHART_AND_OBSERVER_FEED_COMPARISON",
  "checks": {
    "paper_account_label_reviewed": false,
    "holdings_and_orders_reviewed": false,
    "risk_policy_reviewed": false,
    "manual_exits_ready": false,
    "fill_mapping_reviewed": false
  },
  "evidence_files": ["ACCOUNT_SOURCE.png"]
}
```

Amounts and quantities are nonnegative finite decimals under the journal's
decimal limits; the open-order count is a nonnegative integer. The alias is at
most 80 characters, chart-feed text at most 160, and comparison note at most
600, with no control characters. Use an opaque alias, not login credentials.
The capture timestamp must include a timezone and cannot be in the future.
`chart_realtime` and every checkbox must be JSON booleans.

Attach one to eight nonempty source files, each at most 10,000,000 bytes, with
extension `.png`, `.jpg`, `.jpeg`, `.pdf`, `.csv`, `.json` or `.txt`. Paths are
absolute or relative to the evidence JSON's directory; symbolic links and parent
traversal are rejected. The command archives the files privately under new names
and records their hashes. An allowed extension does not prove a source's
authenticity or establish a native export format.

```sh
dwight manual-campaign-account ./my-dwight/private-data/account-evidence.json \
  --campaign ./my-dwight/runs/manual-campaign
```

Use the returned snapshot ID when the prerequisites and current observation
checks are satisfied:

```sh
dwight manual-campaign-start SNAPSHOT_ID \
  --campaign ./my-dwight/runs/manual-campaign
dwight manual-campaign-status --campaign ./my-dwight/runs/manual-campaign
```

Starting requires fresh, durable observation evidence from the matching baseline
and feed. The account snapshot must be no more than five minutes old, show zero
QQQ quantity and zero QQQ open orders, identify a real-time chart, and include
all five completed reviews. The declared allocation must not exceed either its
reported cash or equity. The dedicated journal must still contain no proposals
or fills. The observer must have no STOP, revision halt or unresolved data error,
and its latest bar must have first arrived between 60 and 120 seconds after
close and still be less than 120 seconds old.

These checks validate recorded evidence and timing; they do not authenticate
the account or enforce its risk limits. Starting does not require a signal or
force a trade. The start timestamp and
all four deadlines are fixed together. Restarting a worker or running a command
again cannot extend them. No command resets the account or closes existing
positions as preparation.

Keep order entry, protective exits, daily-loss checks, cash/position review and
session-close handling in the human execution workflow. Import only actual
executions using the documented normalized `paper_export` schema. Preserve the
original source and its mapping; do not guess missing fees or ambiguous fill
ordering. Human confirmation of a proposal is separate from execution evidence.

## Fixed-cutoff reports

```sh
dwight manual-campaign-report-due --campaign ./my-dwight/runs/manual-campaign
dwight manual-campaign-report --campaign ./my-dwight/runs/manual-campaign --hours 12
```

Reports are cumulative from the fixed start to the selected deadline. A delayed
run keeps that deadline as its cutoff and records its later generation time.
Elapsed hours include weekends and holidays; observed exchange-session coverage
is reported separately. Repeated generation returns the frozen report, rather
than rewriting already reported results with a longer window.

The report distinguishes execution time from when the evidence was captured.
Its accounting covers imported executions through the cutoff using the evidence
available at report generation. The journal does not record each fill's import
time, so the report cannot claim that all those fills were known at the cutoff.
Later imports do not silently rewrite a frozen milestone. Subsequent reports
identify additional executions discovered for previously reported windows.
Removing or changing already reported journal records prevents a new report.
Preserve corrections separately and explain their effect in subsequent reporting.

Proposal states are reconstructed at the cutoff from their event history.
Later confirmations, skips, fills and expirations do not become earlier facts.
FIFO realized PnL and linked-fill checks are recomputed for the cutoff evidence.
This snapshot process does not roll the live journal backward.

An empty execution snapshot means no imported fills, not verified zero account
activity. FIFO realized PnL, imported fees and remaining journal inventory are
different from account equity, total return and drawdown. Unsupported account
metrics remain unknown. Human-supplied account snapshots retain their own
timestamps and provenance; recording one does not make the journal broker
verified. A sell-execution count is not an independent trade count or win rate.

The report includes an HTML summary, linked fill charts, JSON evidence snapshots,
a plain text email draft and a ZIP containing those report files. The ZIP contains
account source references and hashes, but excludes the archived account screenshots
and original source attachments. Its strategy checks flag proposals without a
matching eligible observer signal and fill discrepancies; these checks do not
establish complete strategy adherence or account risk compliance.

All report files and account evidence are private. Do not publish them in the
Hugging Face demo, GitHub or a public website. Review the report and redact
sensitive identifiers before separately sharing it.

## Delivery claims and receipts

Each recipient has a separate delivery record for each milestone. These commands
only maintain that record; a connected, explicitly authorized sender must send
the frozen report separately.

```sh
dwight manual-campaign-claim-mail --campaign ./my-dwight/runs/manual-campaign \
  --hours 12 --recipient first@example.com
```

Claim before calling the mail provider. Use the returned report paths and claim
token. After the provider supplies its message receipt, record that receipt:

```sh
dwight manual-campaign-confirm-mail --campaign ./my-dwight/runs/manual-campaign \
  --hours 12 --recipient first@example.com \
  --claim CLAIM_TOKEN --receipt PROVIDER_MESSAGE_ID
```

An interrupted or uncertain send remains claimed. Reconcile it against provider
records before doing anything that could send a duplicate; never clear a claim
merely to retry. A saved receipt is evidence of the recorded provider operation,
not proof that the recipient read the message. Keep one authoritative ledger and
do not claim independently on local and server copies.

## Linux report timer

The installer includes `dwight-manual-reports.service` and
`dwight-manual-reports.timer` and leaves both inactive. The service runs only
`manual-campaign-report-due`, using `/var/lib/dwight/manual-campaign`. It needs no
market-data credentials and has no email transport. Its five-minute timer is
separate from `dwight-reports.timer`, which belongs to the model-shadow campaign.

Prepare and start the campaign on that authoritative host as the `dwight` user.
Check the bounded report command and its private output, then enable the native
paper timer when ready:

```sh
sudo systemctl enable --now dwight-manual-reports.timer
systemctl list-timers dwight-manual-reports.timer
```

The persistent timer can catch up after a restart, retaining every original
cutoff. It does not automate TradingView, obtain account snapshots, import fills
or deliver email. Stop the timer and any running report service before replacing
the installed source. Copy an active SQLite journal with a coherent backup or
snapshot; copying only its main file can omit active transactions.
