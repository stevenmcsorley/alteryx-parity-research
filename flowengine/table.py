"""Columnar in-memory record batch with an explicit typed schema.

The reference kernels operate on :class:`Table`. The type is intentionally
small and immutable so an Arrow-backed implementation can replace it behind
the same interface (see docs/ARCHITECTURE.md, decision ADR-002).
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Iterator, Sequence

from .errors import SchemaError
from .types import Field, Schema, format_value


class Table:
    __slots__ = ('schema', '_columns', '_num_rows')

    def __init__(self, schema: Iterable[Field], columns: Sequence[Sequence]):
        self.schema = schema if isinstance(schema, Schema) else Schema(schema)
        cols = [list(c) for c in columns]
        if len(cols) != len(self.schema):
            raise SchemaError(f'{len(cols)} columns supplied for {len(self.schema)} fields')
        lengths = {len(c) for c in cols}
        if len(lengths) > 1:
            raise SchemaError(f'Ragged columns: lengths {sorted(lengths)}')
        self._columns = cols
        self._num_rows = lengths.pop() if lengths else 0

    # ------------------------------------------------------------------ constructors
    @classmethod
    def empty(cls, schema: Iterable[Field]) -> 'Table':
        schema = schema if isinstance(schema, Schema) else Schema(schema)
        return cls(schema, [[] for _ in schema])

    @classmethod
    def from_rows(cls, schema: Iterable[Field], rows: Iterable[Sequence]) -> 'Table':
        schema = schema if isinstance(schema, Schema) else Schema(schema)
        cols = [[] for _ in schema]
        width = len(schema)
        for i, row in enumerate(rows):
            if len(row) != width:
                raise SchemaError(f'Row {i} has {len(row)} values; schema has {width} fields')
            for c, v in zip(cols, row):
                c.append(v)
        return cls(schema, cols)

    # ------------------------------------------------------------------ access
    @property
    def num_rows(self) -> int:
        return self._num_rows

    def __len__(self) -> int:
        return self._num_rows

    @property
    def columns(self) -> list[list]:
        return self._columns

    def column(self, name: str) -> list:
        return self._columns[self.schema.index(name)]

    def rows(self) -> Iterator[tuple]:
        return zip(*self._columns) if self._columns else iter(() for _ in range(self._num_rows))

    def row(self, i: int) -> tuple:
        return tuple(c[i] for c in self._columns)

    def to_dicts(self) -> list[dict]:
        names = self.schema.names
        return [dict(zip(names, r)) for r in self.rows()]

    # ------------------------------------------------------------------ transforms
    def take(self, indices: Sequence[int]) -> 'Table':
        return Table(self.schema, [[c[i] for i in indices] for c in self._columns])

    def select(self, names: Sequence[str]) -> 'Table':
        idx = [self.schema.index(n) for n in names]
        return Table([self.schema[i] for i in idx], [self._columns[i] for i in idx])

    def with_column(self, field: Field, values: Sequence, position: int | None = None) -> 'Table':
        """Append (or replace by name) a column."""
        if len(values) != self._num_rows:
            raise SchemaError(f'Column {field.name!r} has {len(values)} values for {self._num_rows} rows')
        fields = list(self.schema)
        cols = list(self._columns)
        if field.name in self.schema:
            i = self.schema.index(field.name)
            fields[i] = field
            cols[i] = list(values)
        else:
            pos = len(fields) if position is None else position
            fields.insert(pos, field)
            cols.insert(pos, list(values))
        return Table(fields, cols)

    # ------------------------------------------------------------------ identity
    def canonical_digest(self) -> str:
        """Order-sensitive SHA-256 over schema and canonical value text."""
        h = hashlib.sha256()
        h.update(json.dumps(self.schema.to_list(), sort_keys=True).encode())
        for row in self.rows():
            h.update(json.dumps([format_value(v) for v in row], ensure_ascii=False).encode())
            h.update(b'\n')
        return h.hexdigest()

    def __eq__(self, other) -> bool:
        return isinstance(other, Table) and self.schema == other.schema and self._columns == other._columns

    def __repr__(self) -> str:
        return f'Table({self._num_rows} rows x {len(self.schema)} fields: {self.schema.names})'
