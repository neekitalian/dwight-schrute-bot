import argparse
import csv
import json
import os
import tempfile
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


def main():
    parser = argparse.ArgumentParser(description="Dwight: QQQ research and TradingView alert toolkit")
    from . import __version__
    parser.add_argument("--version", action="version", version=f"Dwight {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
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
        if args.command not in {'init-workspace', 'toolkit-status', 'tradingview-list'}:
            load_env()
        if args.command == 'init-workspace':
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
        if (args.command == 'health' and not result['healthy'] or
                args.command == 'shadow' and result['status'] == 'error_abstain'):
            raise SystemExit(1)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
