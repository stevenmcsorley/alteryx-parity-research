"""Memory-bounded intermediate table storage with disk spilling.

The executor keeps every table that still has unconsumed downstream edges in a
:class:`TableStore`. When the estimated resident size exceeds the budget, the
largest resident tables are serialised to a private spill directory and
reloaded transparently on next access. Serialisation uses pickle of the
engine's own value types (exact round trip of Decimal/date/bytes/None); spill
files never leave the run's temporary directory and are deleted at the end.
"""
from __future__ import annotations

import os
import pickle
import shutil
import tempfile
import threading
from pathlib import Path

from .table import Table

_BYTES_PER_VALUE = 48  # conservative CPython estimate for small scalars incl. list slot


def estimate_bytes(t: Table) -> int:
    n = len(t) * max(len(t.schema), 1)
    text = 0
    for f, col in zip(t.schema, t.columns):
        if f.type.is_string and col:
            sample = col[:: max(1, len(col) // 64)]
            avg = sum(len(v) for v in sample if isinstance(v, str)) / max(len(sample), 1)
            text += int(avg * len(col))
    return n * _BYTES_PER_VALUE + text


_CHUNK = 20_000


def _write_spill(path, table: Table):
    """Schema, then column slices of _CHUNK rows: each pickle call has a small memo, so writing a spill does
    not transiently duplicate the whole table in memory."""
    with open(path, 'wb') as fh:
        pickle.dump((tuple(table.schema), len(table)), fh, protocol=pickle.HIGHEST_PROTOCOL)
        for i in range(0, len(table), _CHUNK):
            pickle.dump([c[i:i + _CHUNK] for c in table.columns], fh, protocol=pickle.HIGHEST_PROTOCOL)


def _read_spill(path) -> Table:
    with open(path, 'rb') as fh:
        schema, n = pickle.load(fh)
        cols = [[] for _ in schema]
        read = 0
        while read < n:
            part = pickle.load(fh)
            for c, p in zip(cols, part):
                c.extend(p)
            read += len(part[0]) if part else n
    return Table(schema, cols) if schema else Table([], [])


class TableStore:
    def __init__(self, budget_bytes: int | None, emit=None, spill_root: Path | None = None):
        self.budget = budget_bytes
        self._emit = emit or (lambda *a, **k: None)
        self._resident: dict = {}
        self._sizes: dict = {}
        self._spilled: dict = {}
        self._dir: Path | None = None
        self._root = spill_root
        self._lock = threading.Lock()
        self.spill_count = 0
        self.spilled_bytes = 0
        self.peak_resident = 0

    def _resident_total(self) -> int:
        return sum(self._sizes[k] for k in self._resident)

    def put(self, key, table: Table, protect=()):
        with self._lock:
            self._resident[key] = table
            self._sizes[key] = estimate_bytes(table)
            self._enforce(protect=set(protect) | {key})
            self.peak_resident = max(self.peak_resident, self._resident_total())

    def get(self, key) -> Table:
        with self._lock:
            if key in self._resident:
                return self._resident[key]
            path = self._spilled.pop(key)
            table = _read_spill(path)
            os.unlink(path)
            self._resident[key] = table
            self._emit('info', message=f'reloaded spilled table {key[0]}.{key[1]}', spill='reload')
            self._enforce(protect={key})
            self.peak_resident = max(self.peak_resident, self._resident_total())
            return table

    def pop(self, key):
        with self._lock:
            self._resident.pop(key, None)
            self._sizes.pop(key, None)
            path = self._spilled.pop(key, None)
            if path and os.path.exists(path):
                os.unlink(path)

    def __contains__(self, key):
        return key in self._resident or key in self._spilled

    def _enforce(self, protect):
        if self.budget is None:
            return
        while self._resident_total() > self.budget:
            candidates = [k for k in self._resident if k not in protect]
            if not candidates:
                return  # protected tables alone exceed the budget; nothing further can be spilled
            victim = max(candidates, key=lambda k: self._sizes[k])
            if self._dir is None:
                self._dir = Path(tempfile.mkdtemp(prefix='flowengine-spill-', dir=self._root))
            path = self._dir / f'{len(self._spilled)}-{self.spill_count}.pkl'
            _write_spill(path, self._resident.pop(victim))
            self._spilled[victim] = path
            self.spill_count += 1
            self.spilled_bytes += self._sizes[victim]
            self._emit('info', message=f'spilled table {victim[0]}.{victim[1]} (~{self._sizes[victim]} bytes) to disk',
                       spill='write')

    def close(self):
        if self._dir and self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
