import argparse
import json
from pathlib import Path
from .runner import replay, STRATEGIES
from .connectors.polymarket import discover
from .ops import doctor, load_env, release


def main():
    parser = argparse.ArgumentParser(description="Dwight: paper-first research framework")
    commands = parser.add_subparsers(dest="command", required=True)
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
    p = commands.add_parser('experiment', help='Chronological VWAP classifier experiment')
    p.add_argument('data', type=Path)
    p.add_argument('--symbol', choices=['QQQ'], default='QQQ')
    p.add_argument('--config', type=Path)
    p.add_argument('--synthetic', action='store_true', help='Smoke test only; cannot deploy to live shadow')
    p.add_argument('--tracking-uri', help='Local SQLite MLflow URI')
    p.add_argument('--output', type=Path, default=Path('runs/experiments'))
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
        load_env()
        if args.command == "strategies":
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
        elif args.command == 'experiment':
            from .experiments import experiment
            config = json.loads(args.config.read_text()) if args.config else {}
            if args.tracking_uri:
                config['tracking_uri'] = args.tracking_uri
            result = experiment(args.data,args.symbol,args.output,args.synthetic,config)
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
