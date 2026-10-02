import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def encode(value):
    return json.dumps(value, allow_nan=False, sort_keys=True)


class Store:
    """Single-run transactions: failed runs never leave partial successful records."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, manifest TEXT NOT NULL,
            summary TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(run_id, sequence));
        """)

    def save(self, run_id, manifest, summary, events):
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?, ?, ?, ?)",
                (run_id, datetime.now(timezone.utc).isoformat(), encode(manifest), encode(summary)))
            for seq, (kind, payload) in enumerate(events):
                self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                                (run_id, seq, kind, encode(payload)))

    def close(self):
        self.db.close()
