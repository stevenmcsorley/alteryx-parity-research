"""Database Input / Database Output (Flowbench extensions; not claimed as reference catalogue entries).

Database Input reads live records from a saved connection (PostgreSQL or SQL Server) on every run; there is
no result cache. Reads are read-only by construction (SQL guard + read-only session / rollback). Database
Output writes only when the connection allows writes AND the run grants ``allow_database_writes``.
"""
from __future__ import annotations

import datetime as _dt
import time

from ..errors import ConfigError, DataError, EngineError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import CastFailure, Field, FieldType, Schema, cast_value
from ._common import require_fields

TEXT = 2**30 - 1


def _store():
    from ..db.connections import ConnectionStore
    return ConnectionStore()


def open_dialect(conn_id: str):
    from ..db.dialects import dialect_for
    store = _store()
    cfg = store.get(conn_id)
    return cfg, dialect_for(cfg, store.secret(conn_id))


def source_sql(dialect, cfg) -> tuple[str, tuple]:
    """SQL + parameters for a Database Input configuration (no values interpolated)."""
    if cfg['mode'] == 'table':
        cols = '*' if not cfg.get('columns') else ', '.join(dialect.quote(c) for c in cfg['columns'])
        sql = f"SELECT {cols} FROM {dialect.qualified(cfg.get('schema'), cfg['table'])}"
        params = ()
    else:
        sql, params = cfg['query'].strip().rstrip(';'), tuple(cfg.get('params') or ())
    if cfg.get('order_by'):
        order = ', '.join(f"{dialect.quote(o['field'])} {'DESC' if o.get('order') == 'desc' else 'ASC'}"
                          for o in cfg['order_by'])
        sql = f'SELECT * FROM ({sql}) flowbench_src ORDER BY {order}'
    return sql, params


class DatabaseInput(Tool):
    manifest = ToolManifest(
        tool_id='core.db_input', version='1.0.0', display_name='Database Input', catalog_ids=(),
        inputs=(), outputs=(Anchor('output', 'O', description='Live records read from the database'),),
        side_effects='network', deterministic=False,
        description='Read a table/view or a read-only parameterised SQL query from a saved PostgreSQL or SQL Server '
                    'connection. Data is re-read on every run (no cache). Without order_by, row order is whatever the '
                    'database returns. Eligible downstream Select/Filter/Summarize/Join steps may run inside the '
                    'database (see the run plan).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['connection_id', 'mode'], 'properties': {
            'connection_id': {'type': 'string', 'minLength': 1},
            'connection_name': {'type': 'string', 'description': 'Display only'},
            'mode': {'enum': ['table', 'query']},
            'schema': {'type': ['string', 'null'], 'default': None},
            'table': {'type': 'string'},
            'columns': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None},
            'query': {'type': 'string', 'description': 'Read-only SELECT; use %s placeholders for parameters'},
            'params': {'type': 'array', 'items': {'type': ['string', 'number', 'boolean', 'null']}, 'default': []},
            'order_by': {'type': 'array', 'default': [], 'items': {
                'type': 'object', 'additionalProperties': False, 'required': ['field'],
                'properties': {'field': {'type': 'string'}, 'order': {'enum': ['asc', 'desc'], 'default': 'asc'}}}},
            'max_rows': {'type': ['integer', 'null'], 'minimum': 0, 'default': None,
                         'description': 'Bounded fetch: stop after this many rows (null = all)'},
            'batch_rows': {'type': 'integer', 'minimum': 1, 'default': 10000},
            'refresh': {'enum': ['every_run'], 'default': 'every_run',
                        'description': 'Database data is always re-read when the workflow runs'},
        }})

    def check_config(self, cfg):
        if cfg['mode'] == 'table' and not cfg.get('table'):
            raise ConfigError('Table mode needs a table')
        if cfg['mode'] == 'query':
            if not cfg.get('query'):
                raise ConfigError('Query mode needs a SQL query')
            from ..db.dialects import ReadOnlyViolation, check_read_only_sql
            try:
                check_read_only_sql(cfg['query'])
            except ReadOnlyViolation as e:
                raise ConfigError(e.message) from None
            if cfg['query'].count('%s') != len(cfg.get('params') or []):
                raise ConfigError(f"Query has {cfg['query'].count('%s')} %s placeholder(s) but "
                                  f"{len(cfg.get('params') or [])} parameter(s)")

    def describe(self, cfg):
        from ..db.dialects import Query
        conn_cfg, d = open_dialect(cfg['connection_id'])
        sql, params = source_sql(d, cfg)
        with Query(d, sql, params) as q:
            return Schema(q.describe())

    def infer(self, ctx, cfg, inputs):
        try:
            return {'output': self.describe(cfg)}
        except EngineError:
            return None  # connection problems are reported when the node runs

    def iter_batches(self, ctx, cfg, batch_rows, sql=None, params=None, fields=None):
        from ..db.dialects import Query
        conn_cfg, d = open_dialect(cfg['connection_id'])
        if sql is None:
            sql, params = source_sql(d, cfg)
        t0 = time.perf_counter()
        queried_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')
        with Query(d, sql, params, cancel_token=ctx.cancel_token) as q:
            schema = Schema(fields) if fields is not None else Schema(q.describe())
            for rows in q.batches(list(schema), batch_rows, cfg.get('max_rows')):
                yield Table.from_rows(schema, [_cast_row(r, schema) for r in rows])
            ctx.emit('database-read', message=f'read {q.rows_fetched} row(s) from {conn_cfg.name} ({d.name})',
                     connection=conn_cfg.name, dialect=d.name, sql=sql, parameters=len(params or ()),
                     rows=q.rows_fetched, duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                     first_row_ms=None if q.first_row_ms is None else round(q.first_row_ms, 1), queried_at=queried_at)
            if q.rows_fetched == 0:
                yield Table.empty(schema)

    def run(self, ctx, cfg, inputs, *, sql=None, params=None, fields=None):
        parts = list(self.iter_batches(ctx, cfg, cfg['batch_rows'], sql, params, fields))
        schema = parts[0].schema
        cols = [[] for _ in schema]
        for p in parts:
            for c, pc in zip(cols, p.columns):
                c.extend(pc)
        return {'output': Table(schema, cols)}


def _cast_row(row, schema):
    out = []
    for v, f in zip(row, schema):
        if v is None:
            out.append(None)
            continue
        try:
            out.append(cast_value(v, f)[0])
        except CastFailure as e:
            raise DataError(f'Database value for {f.name!r} cannot be represented as {f.type.value}: {e}') from None
    return tuple(out)


_SQL_TYPES = {
    'postgresql': {'bool': 'boolean', 'byte': 'smallint', 'int16': 'smallint', 'int32': 'integer', 'int64': 'bigint',
                   'float': 'real', 'double': 'double precision', 'date': 'date', 'time': 'time',
                   'datetime': 'timestamp', 'blob': 'bytea'},
    'sqlserver': {'bool': 'bit', 'byte': 'tinyint', 'int16': 'smallint', 'int32': 'int', 'int64': 'bigint',
                  'float': 'real', 'double': 'float', 'date': 'date', 'time': 'time', 'datetime': 'datetime2',
                  'blob': 'varbinary(max)'},
}


def sql_type(dialect_name: str, f: Field) -> str:
    t = f.type.value
    if t in _SQL_TYPES[dialect_name]:
        return _SQL_TYPES[dialect_name][t]
    if t == 'fixed_decimal':
        return f'numeric({min(f.size, 38)},{f.scale})'
    n = f.size or 254
    if dialect_name == 'postgresql':
        return 'text' if n > 10485760 else f'varchar({n})'
    return 'nvarchar(max)' if n > 4000 else f'nvarchar({n})'


class DatabaseOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.db_output', version='1.0.0', display_name='Database Output', catalog_ids=(),
        inputs=(Anchor('input', 'I'),), outputs=(), side_effects='network', deterministic=True,
        description='Write records to a database table (create or append) in one transaction. Requires a connection '
                    'with writes enabled AND an explicit per-run grant (allow_database_writes).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['connection_id', 'table', 'mode'], 'properties': {
            'connection_id': {'type': 'string', 'minLength': 1},
            'connection_name': {'type': 'string'},
            'schema': {'type': ['string', 'null'], 'default': None},
            'table': {'type': 'string', 'minLength': 1},
            'mode': {'enum': ['create', 'append']},
            'batch_rows': {'type': 'integer', 'minimum': 1, 'default': 1000}}})

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        conn_cfg, d = open_dialect(cfg['connection_id'])
        if not conn_cfg.allow_writes:
            raise EngineError(f'Connection {conn_cfg.name!r} is read-only; enable writes on the connection first',
                              code='permission-denied')
        if not ctx.limits.get('allow_database_writes'):
            raise EngineError('Database writes are disabled for this run; grant them explicitly '
                              '(CLI --allow-database-writes or confirm in the desktop app)', code='permission-denied')
        from ..db.dialects import DatabaseError, _ms_message
        target = d.qualified(cfg.get('schema'), cfg['table'])
        conn = d.connect(read_only=False)
        try:
            cur = conn.cursor()
            if cfg['mode'] == 'create':
                cols = ', '.join(f'{d.quote(f.name)} {sql_type(d.name, f)}' for f in t.schema)
                cur.execute(f'CREATE TABLE {target} ({cols})')
            placeholders = ', '.join(['%s'] * len(t.schema))
            names = ', '.join(d.quote(n) for n in t.schema.names)
            sql = f'INSERT INTO {target} ({names}) VALUES ({placeholders})'
            rows = list(t.rows())
            for i in range(0, len(rows), cfg['batch_rows']):
                ctx.check_cancelled()
                cur.executemany(sql, rows[i:i + cfg['batch_rows']])
            conn.commit()
        except EngineError:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise DatabaseError(d._scrub(_ms_message(e) if d.name == 'sqlserver' else str(e))) from None
        finally:
            conn.close()
        ctx.emit('database-write', message=f'wrote {len(t)} row(s) to {target} on {conn_cfg.name}',
                 connection=conn_cfg.name, rows=len(t), table=cfg['table'])
        return {}


TOOLS = [DatabaseInput, DatabaseOutput]
