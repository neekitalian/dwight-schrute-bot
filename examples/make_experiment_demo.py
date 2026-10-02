"""Reproducible synthetic bars for the ML pipeline smoke test, never evidence.

Uses weekdays, including exchange holidays, deliberately without external data
or a calendar dependency. Every session has 78 five-minute bars. The default
500-calendar-day span contains 358 synthetic sessions. Run experiments with
--synthetic; these fixtures cannot qualify a model for broker deployment.
"""
import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path
import random
from zoneinfo import ZoneInfo


def generate(output: Path, days=500, seed=42):
    if days < 1:
        raise ValueError("days must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    start = datetime(2023, 1, 2, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    sessions = 0
    with output.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for offset in range(days):
            day = start+timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            sessions += 1
            price = 100
            for i in range(78):
                close = price+rng.gauss(.025, .3)
                writer.writerow([
                    (day+timedelta(minutes=5*i)).isoformat(), price,
                    max(price, close)+rng.uniform(.02, .2),
                    min(price, close)-rng.uniform(.02, .2), close,
                    rng.randrange(1000, 20000),
                ])
                price = close
    return sessions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("private-data/synthetic-vwap-5Min.csv"))
    parser.add_argument("--days", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    count = generate(args.output, args.days, args.seed)
    print(f"{args.output.resolve()} ({count} synthetic sessions; use --synthetic)")


if __name__ == "__main__":
    main()
