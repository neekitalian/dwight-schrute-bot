from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import uuid
from vwap_bot.engine import Bot, Config
from .connectors.csv import read_bars
from .store import Store

STRATEGIES = {"vwap_pullback": Bot}
UPSTREAM = "532618ed5b415c974bb5be3da3330cd638c38931"


def replay(data: Path, symbol: str, config: dict, output: Path, strategy="vwap_pullback"):
    if strategy not in STRATEGIES:
        raise ValueError("unknown strategy")
    if not symbol.strip():
        raise ValueError("symbol must not be empty")
    settings = Config(**config)
    # Snapshot input bytes once so the manifest exactly identifies replayed data.
    raw = data.read_bytes()
    run_id = uuid.uuid4().hex
    run_dir = output / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    snapshot = run_dir / "input.csv"
    snapshot.write_bytes(raw)
    bot = STRATEGIES[strategy](settings)
    count = 0
    try:
        for bar in read_bars(snapshot):
            bot.feed(bar)
            count += 1
        if not count:
            raise ValueError("input contains no bars")
        bot.finish()
        sources = sorted(Path(__file__).parent.glob("**/*.py"))
        sources += sorted((Path(__file__).parent.parent / "vwap_bot").glob("*.py"))
        code_hash = hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()
        manifest = {"schema_version": 1, "run_id": run_id, "mode": "historical_replay",
                    "strategy": strategy, "symbol": symbol, "config": asdict(settings),
                    "input_sha256": hashlib.sha256(raw).hexdigest(), "bars": count,
                    "code_sha256": code_hash, "vwap_upstream_commit": UPSTREAM}
        summary = bot.stats()
        events = [("trade_closed", t) for t in bot.trades]
        events += [("realized_equity", point) for point in bot.curve]
        store = Store(output / "journal.sqlite3")
        try:
            store.save(run_id, manifest, summary, events)
        finally:
            store.close()
        for name, value in (("manifest", manifest), ("summary", summary), ("trades", bot.trades)):
            (run_dir / f"{name}.json").write_text(json.dumps(value, indent=2, allow_nan=False)+"\n")
        return {"run_id": run_id, "directory": str(run_dir), **summary}
    except Exception as exc:
        (run_dir / "failed.json").write_text(json.dumps({"error_type": type(exc).__name__}))
        raise
