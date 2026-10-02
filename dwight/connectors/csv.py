import csv
from datetime import datetime
from pathlib import Path
from vwap_bot.engine import Bar


def read_bars(path: Path):
    with path.open(newline="") as stream:
        for line, row in enumerate(csv.DictReader(stream), 2):
            try:
                yield Bar(datetime.fromisoformat(row["timestamp"]),
                          *(float(row[k]) for k in ("open", "high", "low", "close", "volume")))
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"CSV line {line}: {exc}") from exc
