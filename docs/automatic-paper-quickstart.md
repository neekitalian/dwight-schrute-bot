# Automatic QQQ paper pilot

Current `main` includes a private console and continuous Alpaca paper worker.
The execution lifecycle has been tested with broker fixtures, not a completed
real broker pilot. The public Vercel and Hugging Face pages remain demonstrations.
They cannot authorize this worker or receive account keys.

## What the user does

1. Run the private worker and open its local console
2. Verify the displayed Alpaca paper account, QQQ strategy and selected feed
3. Enter allocation, maximum order size, daily entry-loss limit, per-trade stop
   risk and an expiry within seven days
4. Review the complete scope and approve the session
5. Click **Start paper trading**, then monitor positions and broker orders

Approval alone does not start entries. **Pause new entries** keeps order
supervision running. Pausing does not flatten the account. A worker restart
reconciles existing exposure before accepting another candidate. Closing the
browser does not stop the worker; powering off its host does.

Starting entries is not an experiment milestone start. The existing shadow and
native TradingView campaign clocks remain separate and unchanged.

## Install locally

Use Python 3.11 or later. Install current reviewed source with the data extra;
these additions are not in the older 0.7.1 release archive:

```sh
python -m venv .venv
.venv/bin/python -m pip install '.[data]'
.venv/bin/python -m dwight prepare-data-keys
```

Fill only your **Alpaca paper** credentials in `.env`. Keep that file private.
Select the feed explicitly. SIP requires the relevant entitlement. IEX is a
different volume feed and must not be silently substituted for SIP research.
Neither credentials nor successful data access grant permission to trade.

Prepare a new private workspace and run an initial broker-read-only preflight:

```sh
.venv/bin/dwight paper-init --directory runs/automatic-paper --feed sip
.venv/bin/dwight paper-worker --directory runs/automatic-paper --once
```

Use `--feed iex` at initialization only if you deliberately choose IEX. A
workspace cannot change its feed or strategy in place. The initial preflight
does not cancel or submit broker orders, process control commands or start a
campaign. It writes private status and ledger evidence.

Run these in separate terminals from the directory containing your `.env`:

```sh
.venv/bin/dwight paper-worker --directory runs/automatic-paper
```

```sh
.venv/bin/dwight paper-console --directory runs/automatic-paper --open
```

The console binds to `127.0.0.1:8787`. `--open` launches a tab with a private
session capability. The URL fragment is removed after loading. The capability
is also saved in the private `console-session.json` so a remote operator can
open it through SSH. Never publish that file or URL. Console restarts invalidate
old session capabilities.

The worker initially observes and reconciles without entries. Authorize and
start in the console only after confirming the exact account and limits. No
orders are forced to test connectivity. Wait for a valid forward QQQ setup.

## Always-on Linux deployment

Use one dedicated Alpaca paper account, one server and one persistent workspace.
The account lock prevents another worker on the same host from using the same
account. It is not a distributed lease; do not run this account on another host.
All workers on this server must share `DWIGHT_ACCOUNT_LOCK_DIR`.
Upgrade while flat with no pending orders. A changed strategy fingerprint is
rejected by the frozen workspace; it cannot be silently adopted on restart.

The service templates are `deploy/linux/dwight-paper-worker.service` and
`deploy/linux/dwight-paper-console.service`. They assume the reviewed installation
at `/opt/dwight`, a `dwight` service user, private credentials in
`/etc/dwight/paper.env`, and writable state in `/var/lib/dwight`. Initialize the
workspace as that user before starting services. Keep both services disabled
until the installation and preflight have been checked.

Reach the console through an SSH tunnel, keeping port 8787 closed to the public
internet. Replace `YOUR_SERVER_ALIAS` with your configured SSH destination:

```sh
ssh -N -L 8787:127.0.0.1:8787 YOUR_SERVER_ALIAS
```

In another local terminal, open the private authenticated session without
printing its capability:

```sh
ssh YOUR_SERVER_ALIAS 'cat /var/lib/dwight/automatic-paper/console-session.json' |
  python3 -c 'import json,sys,webbrowser; webbrowser.open(json.load(sys.stdin)["url"])'
```

The browser, Vercel and Hugging Face are not always-on execution hosts. The
Linux worker must stay running to handle partial fills and session exits. If
broker access fails, exposure may remain unresolved; the console never claims
flatness from missing evidence. No remote service is installed or activated by
these source files.

## Execution and evidence

The baseline consumes complete five-minute QQQ bars through the existing causal
observer. A separate thread handles market-data retries. One execution writer
checks current account state, quotes, cash, authorization and signal expiry.
Each stable signal gets at most one durable entry intent. An unknown submission
outcome is reconciled by identifier, never blindly retried.

Normal entries use long-only whole-share limit brackets. The worker cancels
stale entry remainders after 120 seconds, and handles partial or unprotected
owned exposure through the [supervision state machine](paper-supervision.md).
Session-close recovery starts five minutes before the calendar close, including
early closes. The baseline observer currently abstains on early-close sessions;
existing order supervision still runs. Stops, timing and loss limits do not
guarantee execution prices or maximum losses.

The console displays broker equity, positions and orders only from verified
current snapshots. It does not invent a return curve or realized PnL. Automatic
fill-level reporting, costs and reconciled equity-return accounting are remaining
work. Historical replay, strategy observations and actual broker fills stay
separate. News, transformers and FinRL do not alter this baseline.

TradingView remains an optional chart and observation source. Its native paper
account is separate from Alpaca and requires manual orders. No TradingView
webhook directly activates this worker in this first pilot. Other catalogued
brokers do not inherit Alpaca execution readiness.
