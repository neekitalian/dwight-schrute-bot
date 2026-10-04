import argparse
import csv
import json
import os
import tempfile
import time
from pathlib import Path
from .runner import replay, STRATEGIES
from .connectors.polymarket import discover
from .ops import doctor, load_env, release


def _read_object(path):
    """Read bounded strict JSON without silently replacing duplicate fields."""
    with Path(path).open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("JSON input must be at most 1 MiB")

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON fields are not allowed")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-finite JSON values are not allowed")

    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_fields,
                       parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON input must contain an object")
    return value


def _write_private_report(path, result):
    """Publish a complete private JSON file, refusing existing output paths."""
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".dwight-report-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # link publishes complete bytes atomically and fails rather than
        # overwriting an existing report, database, or symlink destination.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(path)


def _import_manual_snapshot(journal, path, kind):
    """Validate and import the same frozen CSV bytes under an explicit label."""
    with Path(path).open("rb") as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("manual fill CSV must be at most 64 MiB")
    with tempfile.TemporaryDirectory(prefix="dwight-manual-import-") as temporary:
        snapshot = Path(temporary) / "fills.csv"
        with snapshot.open("xb") as stream:
            os.chmod(snapshot, 0o600)
            stream.write(raw)
        try:
            with snapshot.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream, strict=True)
                if not reader.fieldnames or "data_kind" not in reader.fieldnames:
                    raise ValueError("manual CSV requires an explicit data_kind column")
                for row in reader:
                    if row.get("data_kind") != kind:
                        raise ValueError("CSV data_kind does not match the selected import flag")
        except csv.Error as exc:
            raise ValueError("manual fill CSV cannot be decoded") from exc
        return journal.import_fills(snapshot)


def _observe_manual(store, once):
    """Keep service logs free of prices, features and private proposal terms."""
    while True:
        if (store.state_dir / 'STOP').exists():
            return {'status': 'stopped', 'submits_orders': False}
        try:
            observation = store.observe()
            result = {key: observation[key] for key in
                      ('status', 'observed_at', 'latest_bar_close', 'error_type') if key in observation}
            result.update(submits_orders=False,
                          signal_count=len(observation.get('signals', [])))
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            result = {'status': 'error_abstain', 'error_type': type(exc).__name__,
                      'submits_orders': False}
        if once or result['status'] == 'data_revision_requires_review':
            return result
        print(json.dumps(result, allow_nan=False), flush=True)
        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Dwight: QQQ research and TradingView alert toolkit")
    from . import __version__
    parser.add_argument("--version", action="version", version=f"Dwight {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    from .connection_catalog import PLATFORMS
    commands.add_parser('connections', help='List supported connection setup paths and their implementation status')
    p = commands.add_parser('collect-tokenized', help='Collect public QQQx bars and book into a new private directory; no account or orders')
    p.add_argument('--out', type=Path, required=True)
    p = commands.add_parser('compare-tokenized', help='Compare private raw QQQ history with an observed QQQx snapshot offline')
    p.add_argument('--qqq-manifest', type=Path, required=True)
    p.add_argument('--token-snapshot', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p = commands.add_parser('prepare-data-keys', help='Prepare private empty key fields without overwriting existing secrets')
    p.add_argument('--file', type=Path, default=Path('.env'))
    p = commands.add_parser('prepare-news-config', help='Prepare private news relay addresses and key; no connection or orders')
    p.add_argument('--file', type=Path, default=Path('.env'))
    p = commands.add_parser('connection-profile', help='Export a credential-free read-only setup profile')
    p.add_argument('platform', choices=PLATFORMS)
    p.add_argument('--feed', choices=['sip', 'iex'], default='sip')
    p.add_argument('--output', type=Path, required=True)
    p = commands.add_parser('connection-check', help='Check fixed public feeds or private data-provider access; never orders')
    connection_source = p.add_mutually_exclusive_group(required=True)
    connection_source.add_argument('--platform', choices=PLATFORMS)
    connection_source.add_argument('--profile', type=Path)
    p.add_argument('--feed', choices=['sip', 'iex'], default=None, help='Direct platform checks only; profile selects its own feed')
    p = commands.add_parser('init-workspace', help='Create a private QQQ research workspace without credentials or orders')
    p.add_argument('directory', type=Path)
    p = commands.add_parser('toolkit-status', help='Check private workspace preparation without network or credential values')
    p.add_argument('directory', type=Path)
    p = commands.add_parser('tradingview-serve', help='Serve a local authenticated observation inbox; cannot place orders')
    p.add_argument('--state', type=Path, default=Path('runs/tradingview/inbox.sqlite3'))
    p.add_argument('--host', default='127.0.0.1', help='Loopback only; use a reviewed TLS proxy for external delivery')
    p.add_argument('--port', type=int, default=8765)
    p = commands.add_parser('tradingview-list', help='List unreviewed TradingView observations; these are not trades')
    p.add_argument('--state', type=Path, default=Path('runs/tradingview/inbox.sqlite3'))
    p.add_argument('--limit', type=int, default=100)
    commands.add_parser("strategies", help="List registered strategies")
    commands.add_parser("doctor", help="Check preparation; never prints credential values")
    p = commands.add_parser("replay", help="Historical CSV replay with simulated fills")
    p.add_argument("data", type=Path)
    p.add_argument("--symbol", choices=['QQQ','SYNTHETIC'], default='QQQ')
    p.add_argument("--strategy", choices=STRATEGIES, default="vwap_pullback")
    p.add_argument("--config", type=Path)
    p.add_argument("--output", type=Path, default=Path("runs"))
    p = commands.add_parser("polymarket-discover", help="Read public markets; never submits orders")
    p.add_argument("--limit", type=int, default=10)
    p = commands.add_parser('download-data', help='Download and fingerprint Alpaca one-minute data')
    p.add_argument('--start', required=True, help='YYYY-MM-DD inclusive session date')
    p.add_argument('--end', required=True, help='YYYY-MM-DD inclusive session date')
    p.add_argument('--symbols', nargs='+', choices=['QQQ'], default=['QQQ'])
    p.add_argument('--feed', choices=['sip','iex'], required=True)
    p.add_argument('--output', type=Path, default=Path('private-data'))
    p = commands.add_parser('history-estimate', help='Check Databento cost/count metadata; does not download billable bars')
    p.add_argument('--start', required=True, help='YYYY-MM-DD inclusive session date')
    p.add_argument('--end', required=True, help='YYYY-MM-DD inclusive session date')
    p.add_argument('--dataset', required=True, help='Explicit Databento dataset ID')
    p = commands.add_parser('download-history', help='Download private QQQ research history from Databento or Massive')
    p.add_argument('--provider', choices=['databento', 'massive'], required=True)
    p.add_argument('--start', required=True, help='YYYY-MM-DD inclusive session date')
    p.add_argument('--end', required=True, help='YYYY-MM-DD inclusive session date')
    p.add_argument('--dataset', help='Databento only: explicit dataset ID')
    p.add_argument('--max-cost-usd', help='Databento only: estimated-cost allowance; not a provider billing cap')
    p.add_argument('--output', type=Path, default=Path('private-data'))
    p = commands.add_parser('download-qqq-sample', help='Download the official FirstRate QQQ sample for private research; no broker credentials')
    p.add_argument('--output', type=Path, default=Path('private-data/firstrate'))
    p = commands.add_parser('experiment', help='Chronological VWAP classifier experiment')
    p.add_argument('data', type=Path)
    p.add_argument('--symbol', choices=['QQQ'], default='QQQ')
    p.add_argument('--config', type=Path)
    p.add_argument('--synthetic', action='store_true', help='Smoke test only; cannot deploy to live shadow')
    p.add_argument('--tracking-uri', help='Local SQLite MLflow URI')
    p.add_argument('--output', type=Path, default=Path('runs/experiments'))
    p = commands.add_parser('walkforward', help='Offline QQQ walk-forward comparison with an unscored final holdout')
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--config', type=Path, help='Strict JSON configuration file')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--synthetic', action='store_true', help='Invented fixtures; never deployment evidence')
    mode.add_argument('--real-data', action='store_true', help='Observed QQQ CSV; source verification still requires provenance')
    p = commands.add_parser('enrichment', help='Preregistered offline v1 versus enriched-feature comparison; never promotes a model')
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--config', type=Path, help='Strict JSON configuration file')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--synthetic', action='store_true', help='Invented fixtures for software validation only')
    mode.add_argument('--real-data', action='store_true', help='Observed QQQ CSV; source verification still requires provenance')
    p = commands.add_parser('manual-campaign-init', help='Freeze private native-paper milestone preparation; does not start its clock')
    p.add_argument('directory', type=Path)
    p.add_argument('--signals', type=Path, required=True)
    p.add_argument('--state', type=Path, required=True, help='Dedicated native-paper journal')
    p.add_argument('--recipient', action='append', required=True, help='Private report recipient; repeat for each address')
    p.add_argument('--allocation', required=True, help='Explicit human-declared USD allocation; not verified account equity')
    p = commands.add_parser('manual-campaign-account', help='Archive human-supplied account evidence; no account connection or clock start')
    p.add_argument('evidence_json', type=Path)
    p.add_argument('--campaign', type=Path, required=True)
    p = commands.add_parser('manual-campaign-start', help='Start native-paper milestones after account evidence and fresh observation checks')
    p.add_argument('snapshot_id')
    p.add_argument('--campaign', type=Path, required=True)
    p = commands.add_parser('manual-campaign-status', help='Inspect private native-paper milestones and evidence status')
    p.add_argument('--campaign', type=Path, required=True)
    p = commands.add_parser('manual-campaign-report-due', help='Generate due native-paper reports at fixed cutoffs; sends no email')
    p.add_argument('--campaign', type=Path, required=True)
    p = commands.add_parser('manual-campaign-report', help='Freeze one due native-paper milestone report')
    p.add_argument('--campaign', type=Path, required=True)
    p.add_argument('--hours', type=int, choices=[12, 24, 48, 168], required=True)
    p = commands.add_parser('manual-campaign-claim-mail', help='Claim one native-paper report recipient before a separate mail-provider call')
    p.add_argument('--campaign', type=Path, required=True)
    p.add_argument('--hours', type=int, choices=[12, 24, 48, 168], required=True)
    p.add_argument('--recipient', required=True)
    p = commands.add_parser('manual-campaign-confirm-mail', help='Record provider receipt evidence; does not send email')
    p.add_argument('--campaign', type=Path, required=True)
    p.add_argument('--hours', type=int, choices=[12, 24, 48, 168], required=True)
    p.add_argument('--recipient', required=True)
    p.add_argument('--claim', required=True)
    p.add_argument('--receipt', required=True)
    p = commands.add_parser('manual-observe', help='Read Alpaca QQQ data and retain baseline observations; no simulated or broker orders')
    p.add_argument('--signals', type=Path, default=Path('runs/manual-signals'))
    p.add_argument('--feed', choices=['sip', 'iex'], required=True)
    p.add_argument('--once', action='store_true', help='Run one bounded read; otherwise poll every 30 seconds')
    p = commands.add_parser('manual-signals', help='Inspect private signal observations; no account risk checks or orders')
    p.add_argument('--signals', type=Path, default=Path('runs/manual-signals'))
    p.add_argument('--feed', choices=['sip', 'iex'], required=True)
    p.add_argument('--limit', type=int, default=100)
    p = commands.add_parser('manual-prepare', help='Build a review proposal from an observed signal and explicit human price/size')
    p.add_argument('signal_id')
    p.add_argument('--signals', type=Path, default=Path('runs/manual-signals'))
    p.add_argument('--feed', choices=['sip', 'iex'], required=True)
    p.add_argument('--entry', required=True, help='Human-supplied proposed entry price, never a verified fill')
    p.add_argument('--quantity', required=True, help='Explicit planned whole shares; account sizing is not automated')
    p.add_argument('--price-observed-at', required=True, help='Timezone-aware timestamp of the human price reference')
    p.add_argument('--state', type=Path, default=Path('runs/manual-paper/account.sqlite3'))
    p = commands.add_parser('manual-propose', help='Journal a QQQ proposal for human review; does not submit an order')
    p.add_argument('proposal', type=Path, help='Strict JSON proposal file')
    p.add_argument('--state', type=Path, default=Path('runs/manual-paper/account.sqlite3'))
    p = commands.add_parser('manual-list', help='List private proposals and expire stale pending proposals')
    p.add_argument('--state', type=Path, default=Path('runs/manual-paper/account.sqlite3'))
    p = commands.add_parser('manual-status', help='Record a human decision; not a broker acknowledgement')
    p.add_argument('proposal_id')
    p.add_argument('status', choices=['confirmed', 'skipped', 'expired'])
    p.add_argument('--state', type=Path, default=Path('runs/manual-paper/account.sqlite3'))
    p = commands.add_parser('manual-import', help='Import normalized execution evidence; not a native TradingView CSV adapter')
    p.add_argument('fills', type=Path, help='Normalized CSV execution file')
    p.add_argument('--state', type=Path, default=Path('runs/manual-paper/account.sqlite3'))
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--synthetic', action='store_true', help='Require data_kind=synthetic in every row')
    mode.add_argument('--paper-export', action='store_true', help='Require data_kind=paper_export; authenticity is user supplied')
    p = commands.add_parser('manual-report', help='Report private imported-fill accounting, not account equity')
    p.add_argument('--state', type=Path, default=Path('runs/manual-paper/account.sqlite3'))
    output = p.add_mutually_exclusive_group()
    output.add_argument('--output', type=Path, help='New private JSON file; existing paths are never overwritten')
    output.add_argument('--html-output', type=Path, help='New private directory containing a self-contained visual report and frozen JSON')
    p = commands.add_parser('freeze-release', help='Freeze evaluated artifact for shadow only')
    p.add_argument('experiment', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--symbol', choices=['QQQ'], default='QQQ')
    p.add_argument('--feed', choices=['sip','iex','synthetic'], required=True)
    p.add_argument('--policy', type=Path, default=Path('configs/shadow-policy.json'))
    p = commands.add_parser('shadow', help='Poll live bars and journal decisions; cannot submit orders')
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--state', type=Path, default=Path('runs/shadow'))
    p.add_argument('--once', action='store_true')
    p = commands.add_parser('health', help='Check the shadow heartbeat')
    p.add_argument('--state', type=Path, default=Path('runs/shadow'))
    p = commands.add_parser('paper-check', help='Read-only paper account reconciliation; no orders')
    p.add_argument('--state', type=Path, default=Path('runs/paper/account.sqlite3'))
    p.add_argument('--feed', choices=['sip','iex'], default='sip')
    p = commands.add_parser('record-polymarket', help='One bounded public order-book snapshot; no wallet')
    p.add_argument('--limit', type=int, default=3)
    p.add_argument('--output', type=Path, default=Path('runs/polymarket'))
    p = commands.add_parser('audit', help='Audit saved strategy and experiment evidence')
    p.add_argument('experiment', type=Path)
    p = commands.add_parser('report', help='Create a private visual research report; sends no email')
    p.add_argument('experiment', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--label', default='Initial review')
    p = commands.add_parser('campaign-init', help='Prepare private 12h, 24h, 48h and one week milestones')
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--recipients', nargs='+', required=True)
    p = commands.add_parser('campaign-start', help='Start milestone clock after real shadow observation')
    p.add_argument('--campaign', type=Path, required=True)
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--experiment', type=Path, required=True)
    p.add_argument('--shadow-state', type=Path, required=True)
    p = commands.add_parser('campaign-status', help='Read private campaign and due milestones')
    p.add_argument('--campaign', type=Path, required=True)
    p = commands.add_parser('campaign-report-due', help='Generate due reports on the server; sends no email')
    p.add_argument('--campaign', type=Path, required=True)
    p = commands.add_parser('campaign-report', help='Freeze a due milestone report at its deadline')
    p.add_argument('--campaign', type=Path, required=True)
    p.add_argument('--hours', type=int, choices=[12,24,48,168], required=True)
    p = commands.add_parser('campaign-claim-mail', help='Persist a delivery claim before calling a mail provider')
    p.add_argument('--campaign', type=Path, required=True)
    p.add_argument('--hours', type=int, choices=[12,24,48,168], required=True)
    p.add_argument('--recipient', required=True)
    p = commands.add_parser('campaign-confirm-mail', help='Record a confirmed provider receipt')
    p.add_argument('--campaign', type=Path, required=True)
    p.add_argument('--hours', type=int, choices=[12,24,48,168], required=True)
    p.add_argument('--recipient', required=True)
    p.add_argument('--claim', required=True)
    p.add_argument('--receipt', required=True)
    args = parser.parse_args()
    try:
        # Onboarding checks only the selected workspace, without importing an
        # unrelated current-directory .env into this process.
        if (args.command not in {'init-workspace', 'toolkit-status', 'tradingview-list',
                                'connections', 'connection-profile', 'connection-check', 'prepare-data-keys', 'prepare-news-config',
                                'collect-tokenized', 'compare-tokenized'}
                and not args.command.startswith('manual-campaign-')):
            load_env()
        if args.command == 'connections':
            result = {'platforms': list(PLATFORMS.values()), 'order_execution': False}
        elif args.command == 'collect-tokenized':
            from .tokenized_toolkit import collect_tokenized
            result = collect_tokenized(args.out)
        elif args.command == 'compare-tokenized':
            from .tokenized_toolkit import compare_tokenized
            result = compare_tokenized(args.qqq_manifest,args.token_snapshot,args.out)
        elif args.command == 'prepare-data-keys':
            from .private_config import prepare_data_keys
            result = prepare_data_keys(args.file)
        elif args.command == 'prepare-news-config':
            from .private_config import prepare_news_config
            result = prepare_news_config(args.file)
        elif args.command == 'connection-profile':
            from .connection_catalog import build_profile
            result = build_profile(args.platform, feed=args.feed)
            _write_private_report(args.output, result)
        elif args.command == 'connection-check':
            from .connection_catalog import build_profile
            from .connection_checks import check_connection
            platform, feed = args.platform, args.feed or 'sip'
            if args.profile:
                if args.feed is not None:
                    raise ValueError('The profile selects its feed; omit --feed')
                profile = _read_object(args.profile)
                platform, feed = profile.get('platform'), profile.get('feed', 'sip')
                if profile != build_profile(platform, feed=feed):
                    raise ValueError('Use an unchanged Dwight setup profile; custom endpoints and credentials are not accepted')
            if platform in {'alpaca', 'databento', 'massive'}:
                load_env()
            result = check_connection(platform, feed=feed)
        elif args.command == 'init-workspace':
            from .toolkit import init_workspace
            result = init_workspace(args.directory)
        elif args.command == 'toolkit-status':
            from .toolkit import toolkit_status
            result = toolkit_status(args.directory)
        elif args.command == 'tradingview-serve':
            from .tradingview import TradingViewInbox, capability_from_env, make_server
            capability = capability_from_env()
            inbox = TradingViewInbox(args.state)
            with make_server(inbox, host=args.host, port=args.port, capability=capability) as server:
                print(json.dumps({'status': 'listening', 'host': server.server_address[0],
                                  'port': server.server_address[1], 'mode': 'unreviewed_observations',
                                  'submits_orders': False}), flush=True)
                try:
                    server.serve_forever(poll_interval=0.2)
                except KeyboardInterrupt:
                    pass
            return
        elif args.command == 'tradingview-list':
            from .tradingview import TradingViewInbox
            result = {'mode': 'unreviewed_observations', 'submits_orders': False,
                      'events': TradingViewInbox(args.state).list_events(limit=args.limit)}
        elif args.command == "strategies":
            result = {key: {"mode": "historical_replay", "asset_class": "equities", "allowed_symbols": ['QQQ']} for key in STRATEGIES}
        elif args.command == "replay":
            config = json.loads(args.config.read_text()) if args.config else {}
            result = replay(args.data, args.symbol, config, args.output, args.strategy)
        elif args.command == 'polymarket-discover':
            result = discover(args.limit)
        elif args.command == 'doctor':
            result = doctor()
        elif args.command == 'download-data':
            from .data import download_alpaca_dataset
            result = download_alpaca_dataset(args.output,args.start,args.end,args.symbols,args.feed)
        elif args.command == 'history-estimate':
            from .vendor_history import estimate_history
            result = estimate_history(args.start, args.end, dataset=args.dataset)
        elif args.command == 'download-history':
            from .vendor_history import download_history
            result = download_history(args.provider, args.output, args.start, args.end,
                                      dataset=args.dataset, max_cost_usd=args.max_cost_usd)
        elif args.command == 'download-qqq-sample':
            from .firstrate import download_firstrate_sample
            result = download_firstrate_sample(args.output)
        elif args.command == 'experiment':
            from .experiments import experiment
            config = json.loads(args.config.read_text()) if args.config else {}
            if args.tracking_uri:
                config['tracking_uri'] = args.tracking_uri
            result = experiment(args.data,args.symbol,args.output,args.synthetic,config)
        elif args.command == 'walkforward':
            from .walkforward import run_walkforward
            config = _read_object(args.config) if args.config else {}
            result = run_walkforward(args.data, args.out, synthetic=args.synthetic, config=config)
        elif args.command == 'enrichment':
            from .enrichment import run_enrichment
            config = _read_object(args.config) if args.config else {}
            result = run_enrichment(args.data, args.out, synthetic=args.synthetic, config=config)
        elif args.command in {'manual-observe', 'manual-signals', 'manual-prepare'}:
            from .manual_signals import ManualSignalStore
            with ManualSignalStore(args.signals, feed=args.feed) as store:
                if args.command == 'manual-observe':
                    result = _observe_manual(store, args.once)
                elif args.command == 'manual-signals':
                    result = {'signals': store.list_signals(limit=args.limit),
                              'submits_orders': False, 'account_verified': False,
                              'portfolio_gates_applied': False}
                else:
                    result = store.prepare(args.signal_id, entry=args.entry,
                                           quantity=args.quantity,
                                           price_observed_at=args.price_observed_at,
                                           journal_path=args.state)
        elif args.command.startswith('manual-campaign-'):
            from . import manual_campaign
            if args.command == 'manual-campaign-init':
                result = manual_campaign.initialize(args.directory, args.signals, args.state,
                                                    args.recipient, args.allocation)
            elif args.command == 'manual-campaign-account':
                result = manual_campaign.record_account(args.campaign, args.evidence_json)
            elif args.command == 'manual-campaign-start':
                result = manual_campaign.start(args.campaign, args.snapshot_id)
            elif args.command == 'manual-campaign-status':
                result = manual_campaign.status(args.campaign)
            elif args.command == 'manual-campaign-report':
                result = manual_campaign.report(args.campaign, args.hours)
            elif args.command == 'manual-campaign-report-due':
                result = manual_campaign.report_due(args.campaign)
            elif args.command == 'manual-campaign-claim-mail':
                result = manual_campaign.claim_delivery(args.campaign, args.hours, args.recipient)
            else:
                result = manual_campaign.confirm_delivery(args.campaign, args.hours, args.recipient,
                                                          args.claim, args.receipt)
        elif args.command.startswith('manual-'):
            from .manual import ManualPaperJournal
            journal = ManualPaperJournal(args.state)
            if args.command == 'manual-propose':
                result = journal.add_proposal(_read_object(args.proposal))
            elif args.command == 'manual-list':
                result = {'account': 'tradingview_native_paper', 'submits_orders': False,
                          'proposals': journal.list_proposals()}
            elif args.command == 'manual-status':
                result = journal.set_status(args.proposal_id, args.status)
            elif args.command == 'manual-import':
                kind = 'synthetic' if args.synthetic else 'paper_export'
                result = _import_manual_snapshot(journal, args.fills, kind)
            else:
                result = journal.report()
                if args.html_output:
                    from .manual_reporting import render_manual_report
                    result = render_manual_report(result, args.html_output)
                elif args.output:
                    output = _write_private_report(args.output, result)
                    result = {'output': output, 'account': result['account'],
                              'data_kind': result['data_kind'], 'broker_verified': False,
                              'performance_scope': result['performance_scope'], 'submits_orders': False}
        elif args.command == 'freeze-release':
            result = release(args.experiment,args.output,feed=args.feed,symbol=args.symbol,policy_path=args.policy)
        elif args.command == 'shadow':
            from .shadow import ShadowMonitor
            with ShadowMonitor(args.release,args.state) as monitor:
                result = monitor.run(args.once)
        elif args.command == 'health':
            from .shadow import health
            result = health(args.state)
        elif args.command == 'paper-check':
            from .paper import AlpacaPaperClient, PaperExecutor, RiskPolicy
            client = AlpacaPaperClient.from_env()
            with PaperExecutor(client,args.state,RiskPolicy(expected_feed=args.feed)) as executor:
                snapshot = executor.reconcile()
            result = {'paper_endpoint':True,'account_status':snapshot['account'].get('status'),
                      'positions':len(snapshot['positions']),'open_orders':len(snapshot['open_orders']),
                      'submits_orders':False}
        elif args.command == 'audit':
            from .audit import audit_experiment
            result = audit_experiment(args.experiment)
        elif args.command == 'report':
            from .audit import audit_experiment
            from .reporting import generate_report
            result = generate_report(args.experiment,args.output,milestone_label=args.label,
                                     audit=audit_experiment(args.experiment))
        elif args.command.startswith('campaign-'):
            from . import campaign
            if args.command == 'campaign-init':
                result = campaign.initialize(args.directory,args.recipients)
            elif args.command == 'campaign-start':
                result = campaign.start(args.campaign,args.release,args.experiment,args.shadow_state)
            elif args.command == 'campaign-status':
                result = campaign.status(args.campaign)
            elif args.command == 'campaign-report-due':
                result = campaign.report_due(args.campaign)
            elif args.command == 'campaign-report':
                result = campaign.report(args.campaign,args.hours)
            elif args.command == 'campaign-claim-mail':
                result = campaign.claim_delivery(args.campaign,args.hours,args.recipient)
            else:
                result = campaign.confirm_delivery(args.campaign,args.hours,args.recipient,args.claim,args.receipt)
        else:
            from .recorder import record_books
            result = record_books(args.output,args.limit)
        print(json.dumps(result, indent=2, allow_nan=False))
        if args.command == 'manual-observe' and result['status'] == 'data_revision_requires_review':
            raise SystemExit(3)
        if (args.command == 'health' and not result['healthy'] or
                args.command in {'shadow', 'manual-observe'} and
                result['status'] in {'error_abstain', 'data_revision_requires_review'}):
            raise SystemExit(1)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
