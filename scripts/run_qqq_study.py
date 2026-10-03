"""Run one fixed, private QQQ study; no broker orders or automatic deployment."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dwight.ops import load_env
from dwight import study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "collect", "recover", "evaluate", "status"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--source-attempt", type=Path)
    parser.add_argument("--source-protocol", type=Path)
    args = parser.parse_args()
    if args.stage == "prepare":
        if not args.config:
            parser.error("prepare requires --config")
        result = study.prepare(args.workspace, json.loads(args.config.read_text()))
    elif args.stage == "recover":
        if args.config or not args.source_attempt or not args.source_protocol:
            parser.error("recover requires --source-attempt and --source-protocol; omit --config")
        result = study.recover(args.workspace, args.source_attempt, args.source_protocol)
    else:
        if args.config:
            parser.error("The prepared recipe is frozen; omit --config")
        if args.stage == "collect":
            load_env(args.env_file)
        result = getattr(study, args.stage)(args.workspace)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
