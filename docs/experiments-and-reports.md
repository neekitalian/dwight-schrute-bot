# QQQ experiments and milestone reports

The campaign reports at 12, 24, 48 and 168 elapsed hours after a verified real shadow observation. Preparation time does not count. Milestones are cumulative and immutable. Weekends and exchange closures count as elapsed time but add no market observations. All timestamps are stored in UTC; exchange sessions use New York time.

## Current boundary

The first report is a synthetic software exercise. It is not QQQ market performance. Real historical evaluation requires Alpaca credentials and licensed data. Forward observation additionally requires the same real time feed and an always on server. The running worker observes decisions only. The broker paper execution lifecycle remains unfinished, so broker fills and broker profit remain unmeasured.

## Experimental protocol

Compare the unchanged VWAP baseline, a fixed relative volume rule, and the logistic take or skip filter. Train only on earlier full sessions, choose thresholds on validation, then evaluate on a later holdout. Keep costs, feed, model, code and risk settings recorded. Do not repeatedly tune to the final holdout. During the observation week, keep the deployed model fixed; proposed improvements belong in separate offline experiments and require a fresh later evaluation period.

The initial robustness batch repeats the software experiment with three fixed synthetic seeds and a separate doubled cost sensitivity case. These runs can expose implementation or sensitivity issues. They cannot establish market edge and are never eligible for live shadow or orders.

## Report contents

Each report contains a readable candlestick chart with session VWAP, EMA, volume and simulated trade markers, plus separate equity and drawdown curves for all three historical test portfolios. These charts are rendered locally from the experiment input; they do not use TradingView content. The chosen single chart session is explicitly labelled. The equity curves cover the complete recorded test period.

The forward section separately shows observed sessions, timely market minutes, expected market minutes, missing coverage, candidate decisions, takes, stale decisions and errors. It never converts shadow decisions or historical trades into broker fills. Reports after a delayed wakeup retain their original deadline cutoff. A report produced after a crash may be late; the report does not extend its observation window to hide this.

Audit checks cover artifact identity, chronological splits, reference engine parity, entry timing, sizing, cost accounting, stop and target exits, and losing trade limits. Missing evidence is unverified. The strategy allows shorts in replay while the current paper library accepts long entries only. EMA20 is one possible pullback touch level, not a mandatory trend filter. Runtime agent skill invocations are currently absent and are reported as such. A passing audit verifies recorded software behavior, not expected profitability or broker readiness.

Email prose uses plain paragraphs without dash punctuation or marketing language. Losses are written as amounts followed by the word loss. Scientific charts and metric tables preserve precise numeric meaning.

## Prepare and start

```sh
dwight campaign-init --directory runs/campaigns/qqq-week-one \
  --recipients first@example.com second@example.com
```

The private directory has mode 0700 and the campaign ledger has mode 0600. Actual recipients belong only in that ignored directory. Do not upload it to GitHub or a public Space.

After the real data experiment and reviewed release exist, start the Linux shadow worker using [the server runbook](server-experiment.md). Wait for a current `observed` heartbeat that includes timely bars, then run:

```sh
dwight campaign-start --campaign /var/lib/dwight/campaign \
  --release /var/lib/dwight/releases/candidate \
  --experiment /var/lib/dwight/experiments/RUN \
  --shadow-state /var/lib/dwight/shadow
```

The command verifies real QQQ provenance, release identity, matching experiment and durable fresh observation. A synthetic model, stale heartbeat, mismatch or already started campaign fails. The first meaningful stage is shadow observation; there is no switch that silently enables broker orders.

```sh
dwight campaign-status --campaign /var/lib/dwight/campaign
dwight campaign-report-due --campaign /var/lib/dwight/campaign
dwight audit /var/lib/dwight/experiments/RUN
dwight report /var/lib/dwight/experiments/RUN --output runs/initial-report
```

The server timer generates due reports every five minutes and sends no email. It requires no trading credentials. Reports are cumulative snapshots through their deadline, stored privately. Repeating a successful report command returns the same artifact. Inspect failed service logs if generation cannot verify the underlying experiment.

## Email delivery

The Codex heartbeat follows this task and checks every fifteen minutes. It uses the connected email provider after the user connects it. The Linux worker and report generation continue without Codex, but this connector delivery route requires the Codex host to be available. Uninterrupted server based mail delivery would require a separate SMTP or transactional email sender; none is configured.

Run delivery commands on the authoritative campaign host. If reading remotely, copy a coherent snapshot using SQLite backup or stop the writer briefly; copying only a live SQLite main file can miss WAL transactions. Do not independently claim delivery on both a local copy and the server.

For each recipient, claim the generated report before calling the provider, then confirm only after receiving its message receipt:

```sh
dwight campaign-claim-mail --campaign /var/lib/dwight/campaign --hours 12 \
  --recipient first@example.com
dwight campaign-confirm-mail --campaign /var/lib/dwight/campaign --hours 12 \
  --recipient first@example.com --claim CLAIM_FROM_PREVIOUS_COMMAND \
  --receipt PROVIDER_MESSAGE_ID
```

These commands do not send mail. A pending delivery is claimable once. An interrupted or uncertain provider call remains in `sending` and needs reconciliation against sent mail before any retry. Do not mark a message sent without evidence or reset a claim to bypass this check. Track recipients independently. The heartbeat pauses after all four reports have confirmed delivery to both recipients.

## What the user prepares

Create the Linux server and provide its SSH host or configured alias plus login user. Keep private keys local. Configure Alpaca paper keys in a private environment file and verify real time entitlement for the selected feed. Install and connect Outlook Email for report delivery. None of these credentials belong in chat, source code, reports or Hugging Face.
