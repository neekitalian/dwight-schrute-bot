"""Public onboarding directions only. No workspace, account or credential access."""
REPOSITORY = 'https://github.com/neekitalian/dwight-schrute-bot'
RELEASE = REPOSITORY + '/releases/tag/v0.7.1'
CHOICES = (
    'Explore the research demo',
    'Research my own QQQ history',
    'Connect TradingView alerts',
    'Record TradingView paper trades',
)
HOME_TITLE = 'Test your next trading idea'
HOME_SUBTITLE = 'Replay QQQ. Compare models. Review every trade.'
INTRO = 'A public QQQ research demo using synthetic prices.'
FLOW = '''<div class="dw-flow">
<div class="dw-node"><span>01</span><b>Create a workspace</b><small>Private data, settings and journals.</small></div>
<div class="dw-node"><span>02</span><b>Replay QQQ</b><small>Compare models against a fixed baseline.</small></div>
<div class="dw-node"><span>03</span><b>Review alerts</b><small>Review observations before any manual paper trade.</small></div>
<div class="dw-node"><span>04</span><b>Keep evidence</b><small>Record fills, costs and model results.</small></div></div>'''
DOWNLOAD = f'[Download toolkit]({RELEASE}) · [Setup guide]({REPOSITORY}/blob/main/docs/quickstart.md) · [Source]({REPOSITORY})'
PLANS = {
    CHOICES[0]: '''### Explore without an account

1. Open **Performance** to compare the baseline and classifier on fixed synthetic cases.
2. Open **Walk-forward** to inspect training, validation and later test windows.
3. Open **Transformer connections** to see where price and news models could contribute.

Displayed returns belong to invented prices. They test the process and do not estimate future QQQ returns.
No signup, wallet or trading credentials are needed for this demo.''',
    CHOICES[1]: f'''### Keep historical research private

Download and extract the toolkit, then follow the included setup guide. After installing its requirements:

```sh
dwight init-workspace ./my-dwight
dwight toolkit-status ./my-dwight
dwight download-qqq-sample --output ./my-dwight/private-data/firstrate
```

Run `scripts/review_qqq_sample.py` with the downloaded manifest to create the private report.
The available official sample is useful for checking the complete process; it is too short to train a deployment model.
For model research, collect substantially more QQQ history and preserve the same feed definition for future observation.
Databento and Massive history adapters are available privately. Run `dwight prepare-data-keys`,
fill your local `.env`, then follow the [market data guide]({REPOSITORY}/blob/main/docs/data-providers.md).

[Complete private research steps]({REPOSITORY}/blob/main/docs/quickstart.md)''',
    CHOICES[2]: f'''### TradingView to a private observation inbox

A TradingView chart sends an alert after a five minute bar closes. Your HTTPS endpoint delivers it
to the private Dwight inbox. You review the observation there.

The toolkit includes a QQQ Pine observer template and a local receiver. After preparing the private endpoint capability:

```sh
dwight tradingview-serve --state ./my-dwight/runs/tradingview/inbox.sqlite3
dwight tradingview-list --state ./my-dwight/runs/tradingview/inbox.sqlite3
```

Run the list command in a separate terminal. External TradingView delivery requires your own verified HTTPS proxy,
TradingView 2FA and an alert configured in your account. The Pine template still needs compilation and delivery
verification in TradingView. No webhook is connected through this public Space.

Received alerts remain unreviewed observations. They are not broker orders, fills, authenticated market-data entitlements,
or proof that the VWAP strategy should enter a position.

[Connection guide]({REPOSITORY}/blob/main/docs/tradingview-alerts.md)''',
    CHOICES[3]: f'''### Use your native TradingView paper account

Dwight uses completed Alpaca bars to record a baseline setup. You supply a proposed price and quantity,
then review the proposal. You enter the order in **Paper Trading by TradingView** yourself.
Afterward, normalize and import execution evidence so Dwight can report the imported fills.

Pine alerts cannot automatically place orders in TradingView's built-in paper account. That simulator is separate
from an Alpaca paper account. The toolkit does not silently switch accounts or automate the browser.

The private journal tracks imported executions; it does not claim to know total account equity. A native TradingView
CSV export adapter has not yet been verified, so follow the explicit normalization schema.

With your own data credentials, the local baseline observer records completed QQQ setups without simulated
positions or a trained model. Inspect a signal privately, then supply a current proposed entry and whole-share
quantity. Stale or revised observations cannot become a new proposal. Account holdings, cash and risk require
human review. This public Space does not run that worker.

[Baseline observation and preparation guide]({REPOSITORY}/blob/main/docs/manual-observer.md)

After importing evidence into your local journal, create a private visual snapshot:

```sh
dwight manual-report --state ./my-dwight/runs/manual-paper/account.sqlite3 \\
  --html-output ./my-dwight/runs/manual-paper/report-001
```

Choose a new report directory. The self-contained HTML shows realized P&L, costs, inventory and proposal differences.
It includes a frozen JSON snapshot. Keep these account reports private; this public Space never reads them.

[Manual paper workflow]({REPOSITORY}/blob/main/docs/manual-paper.md)

For a forward experiment, prepare a separate campaign and record the reviewed account evidence before starting
the clock. Its 12, 24, 48 and 168 hour reports preserve fixed cutoffs, count missing observations, compare
proposal evidence and retain later corrections. Reports include private charts, a ZIP and an email draft.
A separate connected sender delivers email; the public Space does not run your campaign.

[Milestone setup and delivery guide]({REPOSITORY}/blob/main/docs/manual-milestones.md)''',
}
BOUNDARY = '''**Model status** The original classifier and the additional session features are research components.
A short real QQQ sample has been replayed privately, but it did not meet the training gates. Transformer and FinRL
performance remain unmeasured. No profitable model is bundled or promised.

**License and scope** The reusable core is Apache 2.0. This release is a research preview.
Managed execution is not implemented. Market-data rights and third-party model licenses remain separate.'''


def plan(choice):
    if choice not in PLANS:
        raise ValueError('Choose one of the supported toolkit paths')
    return PLANS[choice]
