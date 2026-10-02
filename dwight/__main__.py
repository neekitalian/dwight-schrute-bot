import argparse
import json
from pathlib import Path
from .runner import replay, STRATEGIES
from .connectors.polymarket import discover


def main():
    parser = argparse.ArgumentParser(description="Dwight: paper-first research framework")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("strategies", help="List registered strategies")
    p = commands.add_parser("replay", help="Historical CSV replay with simulated fills")
    p.add_argument("data", type=Path)
    p.add_argument("--symbol", required=True)
    p.add_argument("--strategy", choices=STRATEGIES, default="vwap_pullback")
    p.add_argument("--config", type=Path)
    p.add_argument("--output", type=Path, default=Path("runs"))
    p = commands.add_parser("polymarket-discover", help="Read public markets; never submits orders")
    p.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    try:
        if args.command == "strategies":
            result = {key: {"mode": "historical_replay", "asset_class": "equities"} for key in STRATEGIES}
        elif args.command == "replay":
            config = json.loads(args.config.read_text()) if args.config else {}
            result = replay(args.data, args.symbol, config, args.output, args.strategy)
        else:
            result = discover(args.limit)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
