"""In-Database tools: build SQL queries step by step and run them in PostgreSQL or SQL Server.

An In-DB stream is a one-row table (``IDB_SCHEMA``) holding the connection id, the SQL text, its bound
parameters and the fields it returns. Each In-DB tool wraps the upstream query in a new SELECT, so
nothing is executed until Data Stream Out or Browse In-DB fetches rows, or Write Data In-DB writes them.
Expressions are translated by the pushdown translator (``flowengine.db.pushdown``), which keeps
Flowbench semantics: two-valued logic, null-safe equality, case-insensitive text comparison and
division by zero as NULL. An expression without an exact SQL equivalent is rejected with the reason.
Values are always bound parameters, and identifiers are quoted by the dialect.

These are Flowbench semantics. Alteryx In-DB XML and its vendor-specific SQL generation are not
reproduced.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
from decimal import Decimal

from ..errors import ConfigError, DataError, EngineError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema
from ._common import require_fields

TEXT = 2**30 - 1
IDB_SCHEMA = Schema([Field('Connection', FieldType.V_WSTRING, 256), Field('SQL', FieldType.V_WSTRING, TEXT),
                     Field('Params', FieldType.V_WSTRING, TEXT), Field('Fields', FieldType.V_WSTRING, TEXT),
                     Field('Steps', FieldType.V_WSTRING, TEXT)])


# =========================================================================== stream encoding

def _enc(v):
    if isinstance(v, _dt.datetime):
        return {'t': 'datetime', 'v': v.isoformat()}
    if isinstance(v, _dt.date):
        return {'t': 'date', 'v': v.isoformat()}
    if isinstance(v, _dt.time):
        return {'t': 'time', 'v': v.isoformat()}
    if isinstance(v, Decimal):
        return {'t': 'decimal', 'v': str(v)}
    if isinstance(v, bytes):
        return {'t': 'bytes', 'v': base64.b64encode(v).decode('ascii')}
    return v


def _dec(v):
    if isinstance(v, dict) and 't' in v:
        return {'datetime': _dt.datetime.fromisoformat, 'date': _dt.date.fromisoformat, 'time': _dt.time.fromisoformat,
                'decimal': Decimal, 'bytes': base64.b64decode}[v['t']](v['v'])
    return v


class Stream:
    def __init__(self, conn_id, sql, params, fields, steps):
        self.conn_id, self.sql, self.params, self.fields, self.steps = conn_id, sql, list(params), list(fields), list(steps)

    @property
    def schema(self):
        return Schema(self.fields)

    def table(self):
        return Table.from_rows(IDB_SCHEMA, [(self.conn_id, self.sql, json.dumps([_enc(p) for p in self.params]),
                                             json.dumps(self.schema.to_list()), json.dumps(self.steps))])

    @classmethod
    def read(cls, table, what='input'):
        if tuple(table.schema.names) != tuple(IDB_SCHEMA.names) or len(table) != 1:
            raise DataError(f'{what} is not an In-DB stream; connect Connect In-DB, Data Stream In or another In-DB tool')
        conn_id, sql, params, fields, steps = table.row(0)
        return cls(conn_id, sql, [_dec(p) for p in json.loads(params)], [Field.from_dict(f) for f in json.loads(fields)],
                   json.loads(steps))

    def dialect(self):
        from .database import open_dialect
        return open_dialect(self.conn_id)

    def wrap(self, sql, extra_params, fields, step):
        return Stream(self.conn_id, sql, self.params + list(extra_params), fields, self.steps + [step])


def _expect_stream(schema, what='input'):
    if tuple(schema.names) != tuple(IDB_SCHEMA.names):
        raise SchemaError(f'{what} must be an In-DB stream (from Connect In-DB, Data Stream In or another In-DB tool)')


def _alias(stream, suffix=''):
    """Deterministic subquery alias: each wrap adds a step, so nested aliases never collide."""
    return f'fb{len(stream.steps)}{suffix}'


def _translator(dialect, fields, alias):
    from ..db.pushdown import _Expr
    return _Expr(dialect, fields, alias)


def _translate(fn, *args):
    from ..db.pushdown import NotPushable
    try:
        return fn(*args)
    except NotPushable as e:
        raise ConfigError(f'Expression cannot run in the database: {e}') from None


def _parse(expression):
    from ..formula import parse
    return parse(expression)


class _InDB(Tool):
    """Base: one stream input, one stream output."""

    def infer(self, ctx, cfg, inputs):
        for name, schema in inputs.items():
            _expect_stream(schema if not isinstance(schema, list) else schema[0], name)
        return {a.name: IDB_SCHEMA for a in self.manifest.outputs}


_CONN = {'type': 'string', 'minLength': 1, 'description': 'Saved database connection (toolbar → Databases)'}


# =========================================================================== sources and sinks

class ConnectInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.connect_indb', version='1.0.0', display_name='Connect In-DB', catalog_ids=('in-database:connect-in-db',),
        inputs=(), outputs=(Anchor('output', 'In-DB'),), side_effects='network', deterministic=False,
        description='Start an In-DB stream from a table or view, or from a read-only parameterised SELECT, on a '
                    'saved PostgreSQL or SQL Server connection. Nothing is read until Data Stream Out, Browse '
                    'In-DB or Write Data In-DB. Use Sample In-DB to order rows.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['connection_id', 'mode'], 'properties': {
            'connection_id': _CONN, 'mode': {'enum': ['table', 'query']},
            'schema': {'type': ['string', 'null'], 'default': None}, 'table': {'type': 'string'},
            'columns': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None},
            'query': {'type': 'string'},
            'params': {'type': 'array', 'items': {'type': ['string', 'number', 'boolean', 'null']}, 'default': []}}})

    def check_config(self, cfg):
        from .database import DatabaseInput
        DatabaseInput().check_config({**cfg, 'order_by': []})

    def run(self, ctx, cfg, inputs):
        from ..db.dialects import Query
        from .database import open_dialect, source_sql
        conn_cfg, d = open_dialect(cfg['connection_id'])
        sql, params = source_sql(d, {**cfg, 'order_by': []})
        with Query(d, sql, params) as q:
            fields = q.describe()
        what = f"{cfg.get('schema') + '.' if cfg.get('schema') else ''}{cfg['table']}" if cfg['mode'] == 'table' else 'query'
        return {'output': Stream(cfg['connection_id'], sql, params, fields, [f'connect {conn_cfg.name}: {what}']).table()}


def _fetch(ctx, stream, max_rows=None, batch_rows=10000):
    from ..db.dialects import Query
    from .database import _cast_row
    _, d = stream.dialect()
    schema = stream.schema
    rows = []
    with Query(d, stream.sql, stream.params, cancel_token=ctx.cancel_token) as q:
        for batch in q.batches(list(schema), batch_rows, max_rows):
            rows.extend(_cast_row(r, schema) for r in batch)
    return Table.from_rows(schema, rows)


class DataStreamOut(Tool):
    manifest = ToolManifest(
        tool_id='core.data_stream_out', version='1.0.0', display_name='Data Stream Out',
        catalog_ids=('in-database:data-stream-out',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('output', 'Output', description='Records fetched from the database'),),
        side_effects='network', deterministic=False,
        description='Run the In-DB stream\'s query and bring its records into the workflow (optionally the first '
                    'max_rows). The run log shows the SQL that was executed.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'max_rows': {'type': ['integer', 'null'], 'minimum': 0, 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        _expect_stream(inputs['input'])
        return None  # the record schema is known when the stream is built

    def run(self, ctx, cfg, inputs):
        stream = Stream.read(inputs['input'])
        table = _fetch(ctx, stream, cfg['max_rows'])
        ctx.info(f'Data Stream Out read {len(table)} record(s)', sql=stream.sql, steps=stream.steps)
        return {'output': table}


class BrowseInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.browse_indb', version='1.0.0', display_name='Browse In-DB', catalog_ids=('in-database:browse-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('output', 'In-DB', description='The same stream, unchanged'),),
        side_effects='network', deterministic=False,
        description='Preview the first preview_rows records of an In-DB stream in the results panel and pass the '
                    'stream on unchanged. Only the preview rows are fetched.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'preview_rows': {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 100}}})

    def run(self, ctx, cfg, inputs):
        stream = Stream.read(inputs['input'])
        preview = _fetch(ctx, stream, cfg['preview_rows'])
        if ctx.browse_sink:
            ctx.browse_sink(ctx.node_id, preview)
        return {'output': inputs['input']}


class DataStreamIn(Tool):
    manifest = ToolManifest(
        tool_id='core.data_stream_in', version='1.0.0', display_name='Data Stream In', catalog_ids=('in-database:data-stream-in',),
        inputs=(Anchor('input', 'Input', description='Local records'),), outputs=(Anchor('output', 'In-DB'),),
        side_effects='network', deterministic=True,
        description='Send local records into the database as an In-DB stream. inline mode embeds up to '
                    'inline_max_rows records as a VALUES list with bound parameters, needing no write access. '
                    'table mode creates (or replaces) a table, which needs a writable connection and the per-run '
                    'database-write permission.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['connection_id'], 'properties': {
            'connection_id': _CONN, 'mode': {'enum': ['inline', 'table'], 'default': 'inline'},
            'inline_max_rows': {'type': 'integer', 'minimum': 1, 'maximum': 50000, 'default': 5000},
            'schema': {'type': ['string', 'null'], 'default': None}, 'table': {'type': ['string', 'null'], 'default': None},
            'if_exists': {'enum': ['error', 'replace'], 'default': 'error'}}})

    def check_config(self, cfg):
        if cfg['mode'] == 'table' and not cfg['table']:
            raise ConfigError('Data Stream In table mode needs a table name')

    def infer(self, ctx, cfg, inputs):
        return {'output': IDB_SCHEMA}

    def run(self, ctx, cfg, inputs):
        from .database import DatabaseOutput, open_dialect, sql_type
        table = inputs['input']
        conn_cfg, d = open_dialect(cfg['connection_id'])
        if cfg['mode'] == 'inline':
            if len(table) > cfg['inline_max_rows']:
                raise DataError(f"Data Stream In inline mode allows {cfg['inline_max_rows']} records; got {len(table)}. "
                                'Use table mode, which needs write access.')
            casts = [f'CAST(%s AS {sql_type(d.name, f)})' for f in table.schema]
            names = ', '.join(d.quote(n) for n in table.schema.names)
            if len(table):
                rows_sql = ', '.join('(' + ', '.join(casts) + ')' for _ in range(len(table)))
                sql = f'SELECT * FROM (VALUES {rows_sql}) fbvalues ({names})'
                params = [v for row in table.rows() for v in row]
                limit = 2000 if d.name == 'sqlserver' else 60000  # driver/server bound-parameter limits
                if len(params) > limit:
                    raise DataError(f'Data Stream In inline mode needs {len(params)} parameters ({d.name} allows '
                                    f'{limit}); send fewer records or columns, or use table mode')
            else:  # an empty stream with the right columns
                cols = ', '.join(f'CAST(NULL AS {sql_type(d.name, f)}) AS {d.quote(f.name)}' for f in table.schema)
                sql = f'SELECT {cols} WHERE 1 = 0'
                params = []
            return {'output': Stream(cfg['connection_id'], sql, params, list(table.schema),
                                     [f'stream in {len(table)} record(s) inline']).table()}
        target = d.qualified(cfg['schema'], cfg['table'])
        if cfg['if_exists'] == 'replace':
            _execute_write(ctx, conn_cfg, d, [f'DROP TABLE IF EXISTS {target}'])
        DatabaseOutput().run(ctx, {'connection_id': cfg['connection_id'], 'schema': cfg['schema'], 'table': cfg['table'],
                                   'mode': 'create', 'batch_rows': 1000}, {'input': table})
        sql = f"SELECT {', '.join(d.quote(n) for n in table.schema.names)} FROM {target}"
        return {'output': Stream(cfg['connection_id'], sql, [], list(table.schema), [f'stream in to {target}']).table()}


def _execute_write(ctx, conn_cfg, d, statements, params=()):
    if not conn_cfg.allow_writes:
        raise EngineError(f'Connection {conn_cfg.name!r} is read-only; enable writes on the connection first',
                          code='permission-denied')
    if not ctx.limits.get('allow_database_writes'):
        raise EngineError('Database writes are disabled for this run; grant them explicitly '
                          '(CLI --allow-database-writes or confirm in the desktop app)', code='permission-denied')
    from ..db.dialects import DatabaseError, _ms_message
    conn = d.connect(read_only=False)
    try:
        cur = conn.cursor()
        for i, statement in enumerate(statements):
            cur.execute(statement, tuple(params) if i == len(statements) - 1 and params else None)
        conn.commit()
    except Exception as e:
        conn.rollback()
        if isinstance(e, EngineError):
            raise
        raise DatabaseError(d._scrub(_ms_message(e) if d.name == 'sqlserver' else str(e))) from None
    finally:
        conn.close()


class WriteDataInDB(Tool):
    manifest = ToolManifest(
        tool_id='core.write_indb', version='1.0.0', display_name='Write Data In-DB', catalog_ids=('in-database:write-data-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(), side_effects='network', deterministic=True,
        description='Write the result of an In-DB stream to a table inside the database, without bringing it into '
                    'the workflow: create a new table, replace an existing one, or append to one. Needs a writable '
                    'connection and the per-run database-write permission. Runs in one transaction.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['table', 'mode'], 'properties': {
            'schema': {'type': ['string', 'null'], 'default': None}, 'table': {'type': 'string', 'minLength': 1},
            'mode': {'enum': ['create', 'replace', 'append']}}})

    def infer(self, ctx, cfg, inputs):
        _expect_stream(inputs['input'])
        return {}

    def run(self, ctx, cfg, inputs):
        stream = Stream.read(inputs['input'])
        conn_cfg, d = stream.dialect()
        target = d.qualified(cfg['schema'], cfg['table'])
        names = ', '.join(d.quote(f.name) for f in stream.fields)
        src = f'SELECT {names} FROM ({stream.sql}) fbwrite'
        if cfg['mode'] == 'append':
            statements = [f'INSERT INTO {target} ({names}) {src}']
        else:
            create = (f'CREATE TABLE {target} AS {src}' if d.name == 'postgresql'
                      else f'SELECT {names} INTO {target} FROM ({stream.sql}) fbwrite')
            statements = ([f'DROP TABLE IF EXISTS {target}'] if cfg['mode'] == 'replace' else []) + [create]
        _execute_write(ctx, conn_cfg, d, statements, stream.params)
        ctx.emit('database-write', message=f"wrote In-DB result to {target} on {conn_cfg.name} ({cfg['mode']})",
                 connection=conn_cfg.name, table=cfg['table'], sql=statements[-1])
        return {}


# =========================================================================== transforms

class SelectInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.select_indb', version='1.0.0', display_name='Select In-DB', catalog_ids=('in-database:select-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('output', 'In-DB'),),
        description='Choose, order and rename the columns of an In-DB stream. Listed fields are kept in the listed '
                    'order, or every field when none are listed; renames map old names to new ones. Types are not '
                    'changed.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'fields': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}, 'default': []},
            'renames': {'type': 'object', 'additionalProperties': {'type': 'string', 'minLength': 1}, 'default': {}}}})

    def run(self, ctx, cfg, inputs):
        s = Stream.read(inputs['input'])
        _, d = s.dialect()
        names = cfg['fields'] or s.schema.names
        require_fields(s.schema, names + list(cfg['renames']))
        out = [s.schema.field(n).renamed(cfg['renames'].get(n, n)) for n in names]
        if len({f.name for f in out}) != len(out):
            raise ConfigError('Select In-DB would produce duplicate field names')
        a = _alias(s)
        cols = ', '.join(f'{a}.{d.quote(n)} AS {d.quote(f.name)}' for n, f in zip(names, out))
        return {'output': s.wrap(f'SELECT {cols} FROM ({s.sql}) {a}', [], out, 'select ' + ', '.join(f.name for f in out)).table()}


class FilterInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.filter_indb', version='1.0.0', display_name='Filter In-DB', catalog_ids=('in-database:filter-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('true', 'True'), Anchor('false', 'False')),
        description='Split an In-DB stream with a Flowbench expression evaluated in the database. Comparisons, '
                    'AND/OR/NOT, IN, IsNull, IsEmpty, Contains, StartsWith, EndsWith and arithmetic keep Flowbench '
                    'semantics. A null comparison is false, so the record goes to False. Other functions are '
                    'rejected with the reason.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['expression'], 'properties': {
            'expression': {'type': 'string', 'minLength': 1}}})

    def check_config(self, cfg):
        _parse(cfg['expression'])

    def run(self, ctx, cfg, inputs):
        s = Stream.read(inputs['input'])
        _, d = s.dialect()
        a = _alias(s)
        e = _translator(d, s.fields, a)
        pred = _translate(e.pred, _parse(cfg['expression']))
        base = f'SELECT * FROM ({s.sql}) {a}'
        return {'true': s.wrap(f'{base} WHERE {pred}', e.params, s.fields, f"filter {cfg['expression']}").table(),
                'false': s.wrap(f'{base} WHERE NOT {pred}', e.params, s.fields, f"filter NOT ({cfg['expression']})").table()}


class FormulaInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.formula_indb', version='1.0.0', display_name='Formula In-DB', catalog_ids=('in-database:formula-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('output', 'In-DB'),),
        description='Add or replace columns of an In-DB stream with expressions evaluated in the database. Numeric '
                    'results are doubles (division by zero gives null), text results are text, and conditions are '
                    'booleans. Formulas are applied in order, so later ones can use earlier results. Functions '
                    'without an exact SQL equivalent are rejected.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['formulas'], 'properties': {
            'formulas': {'type': 'array', 'minItems': 1, 'items': {
                'type': 'object', 'additionalProperties': False, 'required': ['field', 'expression'],
                'properties': {'field': {'type': 'string', 'minLength': 1}, 'expression': {'type': 'string', 'minLength': 1}}}}}})

    def check_config(self, cfg):
        for f in cfg['formulas']:
            _parse(f['expression'])

    def run(self, ctx, cfg, inputs):
        from ..db.pushdown import NotPushable
        s = Stream.read(inputs['input'])
        _, d = s.dialect()
        pg = d.name == 'postgresql'
        for formula in cfg['formulas']:
            a, b = _alias(s, 'i'), _alias(s, 'o')
            e = _translator(d, s.fields, a)
            node = _parse(formula['expression'])
            name = formula['field']
            from ..formula import FieldRef
            try:
                sql, kind = e.value(node)
            except NotPushable:
                sql, kind = _translate(e.pred, node), 'condition'
            if kind == 'condition':
                sql = f'CASE WHEN {sql} THEN TRUE ELSE FALSE END' if pg else f'CAST(CASE WHEN {sql} THEN 1 ELSE 0 END AS bit)'
                field = Field(name, FieldType.BOOL)
            elif isinstance(node, FieldRef):  # a copy keeps the source field's type
                field = s.schema.field(node.name).renamed(name)
            elif kind == 'number':
                sql = f"CAST({sql} AS {'double precision' if pg else 'float'})"
                field = Field(name, FieldType.DOUBLE)
            elif kind == 'string':
                field = Field(name, FieldType.V_WSTRING, TEXT)
            elif kind == 'bool':
                field = Field(name, FieldType.BOOL)
            else:
                raise ConfigError(f'Formula In-DB {name!r}: the expression has no SQL type (for example a bare null)')
            kept = [f for f in s.fields if f.name != name]
            cols = ', '.join([f'{a}.{d.quote(f.name)}' for f in kept] + [f'{sql} AS {d.quote(name)}'])
            position = s.schema.names.index(name) if name in s.schema.names else len(kept)
            fields = kept[:position] + [field] + kept[position:]
            order = ', '.join(d.quote(f.name) for f in fields)
            # The expression sits in the SELECT list, before the wrapped query: its parameters come first.
            s = Stream(s.conn_id, f'SELECT {order} FROM (SELECT {cols} FROM ({s.sql}) {a}) {b}', e.params + s.params,
                       fields, s.steps + [f"formula {name} = {formula['expression']}"])
        return {'output': s.table()}


_AGG = ('group_by', 'count', 'count_non_null', 'count_distinct', 'sum', 'avg', 'min', 'max')


class SummarizeInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.summarize_indb', version='1.0.0', display_name='Summarize In-DB',
        catalog_ids=('in-database:summarize-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('output', 'In-DB'),),
        description='Group and aggregate an In-DB stream in the database: group_by, count (records), '
                    'count_non_null, count_distinct, sum, avg, min and max. Counts are int64, sum and avg are '
                    'doubles, and min and max keep the field type. Text grouping uses a binary (code-point) '
                    'collation, as the local engine does.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['actions'], 'properties': {
            'actions': {'type': 'array', 'minItems': 1, 'items': {
                'type': 'object', 'additionalProperties': False, 'required': ['field', 'op'],
                'properties': {'field': {'type': 'string'}, 'op': {'enum': list(_AGG)},
                               'output': {'type': ['string', 'null'], 'default': None}}}}}})

    def run(self, ctx, cfg, inputs):
        s = Stream.read(inputs['input'])
        _, d = s.dialect()
        require_fields(s.schema, [x['field'] for x in cfg['actions']])
        a = _alias(s)
        fl = 'double precision' if d.name == 'postgresql' else 'float'
        big = 'bigint'
        sel, groups, fields = [], [], []
        for x in cfg['actions']:
            f = s.schema.field(x['field'])
            col = f'{a}.{d.quote(f.name)}'
            name = x['output'] or (f.name if x['op'] == 'group_by' else f"{x['op'].title().replace('_', '')}_{f.name}")
            op = x['op']
            if op in ('sum', 'avg') and not f.type.is_numeric:
                raise SchemaError(f'Summarize In-DB {op} needs a numeric field: {f.name!r}')
            if op == 'group_by':
                expr, out = (f'{col} {d.binary_collation}' if f.type.is_string else col), f.renamed(name)
                groups.append(expr)
            elif op == 'count':
                expr, out = f'CAST(COUNT(*) AS {big})', Field(name, FieldType.INT64)
            elif op == 'count_non_null':
                expr, out = f'CAST(COUNT({col}) AS {big})', Field(name, FieldType.INT64)
            elif op == 'count_distinct':
                expr, out = f'CAST(COUNT(DISTINCT {col}) AS {big})', Field(name, FieldType.INT64)
            elif op in ('sum', 'avg'):
                expr, out = f'CAST({op.upper()}(CAST({col} AS {fl})) AS {fl})', Field(name, FieldType.DOUBLE)
            else:
                expr, out = f'{op.upper()}({col})', f.renamed(name)
            sel.append(f'{expr} AS {d.quote(name)}')
            fields.append(out)
        if len({f.name for f in fields}) != len(fields):
            raise ConfigError('Summarize In-DB output names must be unique; set output on the clashing actions')
        sql = f'SELECT {", ".join(sel)} FROM ({s.sql}) {a}' + (f' GROUP BY {", ".join(groups)}' if groups else '')
        return {'output': s.wrap(sql, [], fields, 'summarize ' + ', '.join(f"{x['op']}({x['field']})" for x in cfg['actions'])).table()}


class JoinInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.join_indb', version='1.0.0', display_name='Join In-DB', catalog_ids=('in-database:join-in-db',),
        inputs=(Anchor('left', 'Left', description='In-DB stream'), Anchor('right', 'Right', description='In-DB stream')),
        outputs=(Anchor('output', 'In-DB'),),
        description='Join two In-DB streams on the same connection by key fields: inner, left, right or full '
                    'outer. Keys are compared null-safely and case-sensitively, as the local Join does. Right '
                    'fields that clash with left names get the prefix Right_.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['keys'], 'properties': {
            'keys': {'type': 'array', 'minItems': 1, 'items': {
                'type': 'object', 'additionalProperties': False, 'required': ['left', 'right'],
                'properties': {'left': {'type': 'string'}, 'right': {'type': 'string'}}}},
            'join_type': {'enum': ['inner', 'left', 'right', 'full'], 'default': 'inner'}}})

    def run(self, ctx, cfg, inputs):
        left, right = Stream.read(inputs['left'], 'left'), Stream.read(inputs['right'], 'right')
        if left.conn_id != right.conn_id:
            raise DataError('Join In-DB inputs use different connections; use Data Stream Out and the Join tool')
        _, d = left.dialect()
        require_fields(left.schema, [k['left'] for k in cfg['keys']])
        require_fields(right.schema, [k['right'] for k in cfg['keys']])
        depth = len(left.steps) + len(right.steps)
        la, ra = f'fb{depth}l', f'fb{depth}r'
        conds = []
        for k in cfg['keys']:
            lf, rf = left.schema.field(k['left']), right.schema.field(k['right'])
            if lf.type.is_string != rf.type.is_string or lf.type.is_numeric != rf.type.is_numeric:
                raise SchemaError(f"Join In-DB key types differ: {lf.name} ({lf.type.value}) and {rf.name} ({rf.type.value})")
            lc, rc = f'{la}.{d.quote(lf.name)}', f'{ra}.{d.quote(rf.name)}'
            if lf.type.is_string:
                lc, rc = f'{lc} {d.binary_collation}', f'{rc} {d.binary_collation}'
            conds.append(f'({lc} = {rc} OR ({lc} IS NULL AND {rc} IS NULL))')
        names = set(left.schema.names)
        fields, cols = list(left.fields), [f'{la}.{d.quote(f.name)} AS {d.quote(f.name)}' for f in left.fields]
        for f in right.fields:
            name = f.name
            while name in names:
                name = 'Right_' + name
            names.add(name)
            fields.append(f.renamed(name))
            cols.append(f'{ra}.{d.quote(f.name)} AS {d.quote(name)}')
        kind = {'inner': 'INNER', 'left': 'LEFT OUTER', 'right': 'RIGHT OUTER', 'full': 'FULL OUTER'}[cfg['join_type']]
        sql = f'SELECT {", ".join(cols)} FROM ({left.sql}) {la} {kind} JOIN ({right.sql}) {ra} ON {" AND ".join(conds)}'
        out = Stream(left.conn_id, sql, left.params + right.params, fields,
                     left.steps + right.steps + [f"{cfg['join_type']} join on " + ', '.join(f"{k['left']}={k['right']}" for k in cfg['keys'])])
        return {'output': out.table()}


class UnionInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.union_indb', version='1.0.0', display_name='Union In-DB', catalog_ids=('in-database:union-in-db',),
        inputs=(Anchor('inputs', 'In-DB', multiple=True),), outputs=(Anchor('output', 'In-DB'),),
        description='Stack In-DB streams on the same connection by field name (UNION ALL), in connection order. '
                    'Fields missing from some inputs are filled with typed nulls when missing_fields is include. '
                    'Matching fields must have compatible types.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'missing_fields': {'enum': ['include', 'error'], 'default': 'include'}}})

    def infer(self, ctx, cfg, inputs):
        for schema in inputs['inputs']:
            _expect_stream(schema)
        return {'output': IDB_SCHEMA}

    def run(self, ctx, cfg, inputs):
        from .database import sql_type
        streams = [Stream.read(t) for t in inputs['inputs']]
        if len({s.conn_id for s in streams}) > 1:
            raise DataError('Union In-DB inputs use different connections')
        _, d = streams[0].dialect()
        order, proto = [], {}
        for s in streams:
            for f in s.fields:
                if f.name not in proto:
                    order.append(f.name)
                    proto[f.name] = f
                elif proto[f.name].type.is_string != f.type.is_string or proto[f.name].type.is_numeric != f.type.is_numeric:
                    raise SchemaError(f'Union In-DB field {f.name!r} has incompatible types')
        parts, params = [], []
        for s in streams:
            have = set(s.schema.names)
            missing = [n for n in order if n not in have]
            if missing and cfg['missing_fields'] == 'error':
                raise SchemaError(f'Union In-DB input is missing fields {missing}')
            a = f'fbu{len(parts)}'
            cols = ', '.join(f'{a}.{d.quote(n)} AS {d.quote(n)}' if n in have
                             else f'CAST(NULL AS {sql_type(d.name, proto[n])}) AS {d.quote(n)}' for n in order)
            parts.append(f'SELECT {cols} FROM ({s.sql}) {a}')
            params += s.params
        sql = f'SELECT * FROM ({" UNION ALL ".join(parts)}) fbunion'
        steps = [step for s in streams for step in s.steps] + [f'union of {len(streams)} streams']
        return {'output': Stream(streams[0].conn_id, sql, params, [proto[n] for n in order], steps).table()}


class SampleInDB(_InDB):
    manifest = ToolManifest(
        tool_id='core.sample_indb', version='1.0.0', display_name='Sample In-DB', catalog_ids=('in-database:sample-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('output', 'In-DB'),),
        description='Keep the first N records in order_by order (required, because databases have no natural order), '
                    'or a random N records or random percent of records using the database random function '
                    '(not repeatable).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['method'], 'properties': {
            'method': {'enum': ['first_n', 'random_n', 'random_percent']},
            'n': {'type': 'integer', 'minimum': 0, 'default': 100},
            'percent': {'type': 'number', 'minimum': 0, 'maximum': 100, 'default': 10},
            'order_by': {'type': 'array', 'default': [], 'items': {
                'type': 'object', 'additionalProperties': False, 'required': ['field'],
                'properties': {'field': {'type': 'string'}, 'order': {'enum': ['asc', 'desc'], 'default': 'asc'}}}}}})

    def check_config(self, cfg):
        if cfg['method'] == 'first_n' and not cfg['order_by']:
            raise ConfigError('Sample In-DB first_n needs order_by; database rows have no natural order')

    def run(self, ctx, cfg, inputs):
        s = Stream.read(inputs['input'])
        _, d = s.dialect()
        a = _alias(s)
        pg = d.name == 'postgresql'
        if cfg['method'] == 'random_percent':
            # No SQL "%" operator: drivers use %s markers. RAND(seed) gives one value per row on SQL Server.
            cond = 'random() < %s' if pg else 'RAND(CHECKSUM(NEWID())) < %s'
            return {'output': s.wrap(f'SELECT * FROM ({s.sql}) {a} WHERE {cond}', [cfg['percent'] / 100], s.fields,
                                     f"sample {cfg['percent']}% at random").table()}
        if cfg['method'] == 'random_n':
            order = 'random()' if pg else 'NEWID()'
            step = f"sample {cfg['n']} at random"
        else:
            require_fields(s.schema, [o['field'] for o in cfg['order_by']])
            nulls = ' NULLS FIRST' if pg else ''
            order = ', '.join(f"{a}.{d.quote(o['field'])} {'DESC' if o['order'] == 'desc' else 'ASC'}{nulls if o['order'] != 'desc' else ''}"
                              for o in cfg['order_by'])
            step = f"first {cfg['n']} by " + ', '.join(o['field'] for o in cfg['order_by'])
        sql = (f'SELECT * FROM ({s.sql}) {a} ORDER BY {order} LIMIT %s' if pg
               else f'SELECT TOP (%s) * FROM ({s.sql}) {a} ORDER BY {order}')
        params = s.params + [cfg['n']] if pg else [cfg['n']] + s.params
        return {'output': Stream(s.conn_id, sql, params, s.fields, s.steps + [step]).table()}


class DynamicInputInDB(Tool):
    manifest = ToolManifest(
        tool_id='core.dynamic_input_indb', version='1.0.0', display_name='Dynamic Input In-DB',
        catalog_ids=('in-database:dynamic-input-in-db',),
        inputs=(Anchor('input', 'Input', description='Records holding read-only queries or table names'),),
        outputs=(Anchor('output', 'In-DB'),), side_effects='network', deterministic=False,
        description='Build an In-DB stream from a query (or table name) in each input record, stacked by field name '
                    'with UNION ALL. Every query is checked to be read-only and must return the same field names. '
                    'Values are not substituted into SQL; write complete queries.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['connection_id', 'source_field'], 'properties': {
            'connection_id': _CONN, 'source_field': {'type': 'string', 'minLength': 1},
            'source_type': {'enum': ['query', 'table'], 'default': 'query'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['source_field']])
        return {'output': IDB_SCHEMA}

    def run(self, ctx, cfg, inputs):
        from ..db.dialects import Query, ReadOnlyViolation, check_read_only_sql
        from .database import open_dialect
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        _, d = open_dialect(cfg['connection_id'])
        parts, fields = [], None
        for number, value in enumerate(table.column(cfg['source_field']), start=1):
            if value is None or not str(value).strip():
                raise DataError(f'Dynamic Input In-DB record {number} has no {cfg["source_type"]}')
            if cfg['source_type'] == 'table':
                schema_name, _, name = str(value).rpartition('.')
                sql = f'SELECT * FROM {d.qualified(schema_name or None, name)}'
            else:
                sql = str(value).strip().rstrip(';')
                try:
                    check_read_only_sql(sql)
                except ReadOnlyViolation as e:
                    raise DataError(f'Dynamic Input In-DB record {number}: {e.message}') from None
            with Query(d, sql) as q:
                these = q.describe()
            if fields is None:
                fields = these
            elif [f.name for f in these] != [f.name for f in fields]:
                raise DataError(f'Dynamic Input In-DB record {number} returns fields {[f.name for f in these]}, '
                                f'expected {[f.name for f in fields]}')
            parts.append(f'SELECT * FROM ({sql}) fbdyn{number}')
        if not parts:
            raise DataError('Dynamic Input In-DB received no records')
        return {'output': Stream(cfg['connection_id'], ' UNION ALL '.join(parts), [], fields,
                                 [f'dynamic input of {len(parts)} source(s)']).table()}


class DynamicOutputInDB(Tool):
    SCHEMA = Schema([Field('Connection', FieldType.V_WSTRING, 256), Field('Query', FieldType.V_WSTRING, TEXT),
                     Field('Parameters', FieldType.V_WSTRING, TEXT), Field('Steps', FieldType.V_WSTRING, TEXT)])
    manifest = ToolManifest(
        tool_id='core.dynamic_output_indb', version='1.0.0', display_name='Dynamic Output In-DB',
        catalog_ids=('in-database:dynamic-output-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('output', 'Output', description='The generated query as a record'),),
        description='Output the SQL of an In-DB stream as an ordinary record (connection, query with %s parameter '
                    'markers, the parameters as JSON, and the steps that built it), for logging or for use by '
                    'other tools. Nothing is executed.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        _expect_stream(inputs['input'])
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        s = Stream.read(inputs['input'])
        return {'output': Table.from_rows(self.SCHEMA, [(s.conn_id, s.sql, json.dumps([_enc(p) for p in s.params]),
                                                         '\n'.join(s.steps))])}


class MacroInputInDB(Tool):
    manifest = ToolManifest(
        tool_id='core.macro_input_indb', version='1.0.0', display_name='Macro Input In-DB',
        catalog_ids=('in-database:macro-input-in-db',),
        inputs=(), outputs=(Anchor('output', 'In-DB'),),
        description='Inside a macro, expose an In-DB stream from the parent workflow by name, so in-database steps '
                    'can be packaged as macros. The parent binding must be an In-DB stream.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['name'],
                       'properties': {'name': {'type': 'string', 'minLength': 1}}})

    def infer(self, ctx, cfg, inputs):
        return {'output': IDB_SCHEMA}

    def run(self, ctx, cfg, inputs):
        table = ctx.macro_inputs.get(cfg['name'])
        if not isinstance(table, Table):
            raise DataError(f"Macro input {cfg['name']!r} is not bound to a parent In-DB stream", node_id=ctx.node_id)
        Stream.read(table, f"macro input {cfg['name']!r}")
        return {'output': table}


class MacroOutputInDB(Tool):
    manifest = ToolManifest(
        tool_id='core.macro_output_indb', version='1.0.0', display_name='Macro Output In-DB',
        catalog_ids=('in-database:macro-output-in-db',),
        inputs=(Anchor('input', 'In-DB'),), outputs=(Anchor('output', 'In-DB'),),
        description='Inside a macro, return an In-DB stream to the parent workflow as a named macro result.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'properties': {'name': {'type': 'string', 'default': 'Output'}}})

    def infer(self, ctx, cfg, inputs):
        _expect_stream(inputs['input'])
        return {'output': IDB_SCHEMA}

    def run(self, ctx, cfg, inputs):
        Stream.read(inputs['input'])
        return {'output': inputs['input']}


TOOLS = [ConnectInDB, DataStreamOut, BrowseInDB, DataStreamIn, WriteDataInDB, SelectInDB, FilterInDB, FormulaInDB,
         SummarizeInDB, JoinInDB, UnionInDB, SampleInDB, DynamicInputInDB, DynamicOutputInDB, MacroInputInDB, MacroOutputInDB]
