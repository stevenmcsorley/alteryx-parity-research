"""Persistent run history and provenance (SQLite).

Each run stores its manifest (engine/profile/workflow digest/per-anchor output
digests/warnings/error), the full event stream, and the workflow document
itself so any historical run can be inspected or re-executed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_utc TEXT NOT NULL,
  finished_utc TEXT NOT NULL,
  status TEXT NOT NULL,
  workflow_name TEXT,
  workflow_digest TEXT NOT NULL,
  profile TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  duration_ms REAL,
  manifest TEXT NOT NULL,
  workflow TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  node_id TEXT,
  body TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS runs_by_digest ON runs(workflow_digest, started_utc);
PRAGMA user_version = 1;
"""


class RunHistory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        """Connection that commits on success and is always closed (Windows file locks)."""
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            c.execute('PRAGMA foreign_keys = ON')
            with c:
                yield c

    def record(self, doc: dict, result) -> None:
        ev = result.events
        m = result.manifest
        started, finished = ev[0]['ts'], ev[-1]['ts']
        with self._lock, self._connect() as c:
            c.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (result.run_id, started, finished, result.status, doc.get('name'), m['workflow_digest'],
                       m['profile']['name'], m['engine_version'], m.get('stats', {}).get('wall_ms'),
                       json.dumps(m, default=str), json.dumps(doc)))
            c.executemany('INSERT INTO events VALUES (?,?,?,?,?,?)',
                          [(result.run_id, e['seq'], e['ts'], e['type'], e.get('node_id'),
                            json.dumps(e, default=str)) for e in ev])

    def list(self, limit: int = 50, workflow_digest: str | None = None) -> list[dict]:
        q = ('SELECT run_id, started_utc, finished_utc, status, workflow_name, workflow_digest, profile, '
             'engine_version, duration_ms FROM runs')
        args: tuple = ()
        if workflow_digest:
            q += ' WHERE workflow_digest = ?'
            args = (workflow_digest,)
        q += ' ORDER BY started_utc DESC LIMIT ?'
        with self._connect() as c:
            cols = ['run_id', 'started_utc', 'finished_utc', 'status', 'workflow_name', 'workflow_digest', 'profile',
                    'engine_version', 'duration_ms']
            return [dict(zip(cols, r)) for r in c.execute(q, args + (limit,))]

    def get(self, run_id: str) -> dict | None:
        with self._connect() as c:
            row = c.execute('SELECT manifest, workflow FROM runs WHERE run_id = ?', (run_id,)).fetchone()
            if not row:
                return None
            events = [json.loads(b) for (b,) in c.execute('SELECT body FROM events WHERE run_id = ? ORDER BY seq',
                                                           (run_id,))]
        return {'manifest': json.loads(row[0]), 'workflow': json.loads(row[1]), 'events': events}
