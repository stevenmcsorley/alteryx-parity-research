"""Calgary Loader, Input, Join, Cross Count and Cross Count Append on an open indexed store; no vendor code.

Functional source: the public Calgary tool help. Calgary is the reference's proprietary indexed database format
(.cydb); Flowbench cannot read or write it. These tools give the same workflow (load once with indexes, then query,
join and count quickly) on an open format instead: a SQLite database file (``.fbcal``) holding one table of records,
B-tree indexes on the fields you choose, and the Flowbench schema so values come back with their types. Linked
(household/individual) files are not supported.

Queries are lists of conditions on fields: ``{"field": "Region", "op": "in", "values": ["North", "South"]}``,
with ops ``=``, ``!=``, ``<``, ``<=``, ``>``, ``>=``, ``in``, ``between``, ``starts_with``, ``is_null`` and
``not_null``; all conditions must hold. Indexed fields make them fast; any field may be used.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, cast_value, format_value
from ._common import require_fields

FORMAT = 'flowbench.calgary.v1'
OPS = ('=', '!=', '<', '<=', '>', '>=', 'in', 'between', 'starts_with', 'is_null', 'not_null')
MAX_CROSS_VALUES = 1000
_CONDITION = {'type': 'object', 'additionalProperties': False, 'required': ['field', 'op'], 'properties': {
    'field': {'type': 'string', 'minLength': 1}, 'op': {'enum': list(OPS)},
    'value': {}, 'values': {'type': 'array'}}}


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _sql_value(v):
    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    return format_value(v)


def _open(ctx, path_text, must_exist=True):
    path = ctx.resolve_path(path_text)
    if path.suffix.lower() != '.fbcal':
        raise ConfigError('Flowbench indexed databases end in .fbcal (the reference .cydb format cannot be read)')
    if must_exist and not path.is_file():
        raise DataError(f'Indexed database not found: {path}')
    return path


def _load(path):
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        meta = dict(conn.execute('SELECT key, value FROM calgary_meta').fetchall())
    except sqlite3.DatabaseError:
        conn.close()
        raise DataError(f'{path.name} is not a Flowbench indexed database') from None
    if meta.get('format') != FORMAT:
        conn.close()
        raise DataError(f'{path.name} is not a Flowbench indexed database')
    schema = Schema([Field.from_dict(d) for d in json.loads(meta['schema'])])
    return conn, schema, json.loads(meta.get('indexes', '[]'))


def _where(conditions, schema):
    clauses, params = [], []
    for c in conditions:
        if c['field'] not in schema:
            raise ConfigError(f"Query field {c['field']!r} is not in the database")
        f, op = _q(c['field']), c['op']
        if op in ('is_null', 'not_null'):
            clauses.append(f"{f} IS {'NOT ' if op == 'not_null' else ''}NULL")
        elif op == 'in':
            vals = c.get('values') or []
            if not vals:
                raise ConfigError(f"Condition on {c['field']} needs values")
            clauses.append(f"{f} IN ({','.join('?' * len(vals))})")
            params += [_sql_value(v) for v in vals]
        elif op == 'between':
            vals = c.get('values') or []
            if len(vals) != 2:
                raise ConfigError(f"between on {c['field']} needs two values")
            clauses.append(f'{f} BETWEEN ? AND ?')
            params += [_sql_value(v) for v in vals]
        elif op == 'starts_with':
            clauses.append(f"{f} LIKE ? ESCAPE '\\'")
            params.append(str(c.get('value', '')).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        else:
            if 'value' not in c:
                raise ConfigError(f"Condition {op} on {c['field']} needs a value")
            clauses.append(f'{f} {"<>" if op == "!=" else op} ?')
            params.append(_sql_value(c['value']))
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', params


def _to_table(schema, rows):
    return Table(schema, [[cast_value(v, f)[0] for v in col] for f, col in zip(schema, zip(*rows))] if rows else [[] for _ in schema])


class CalgaryLoader(Tool):
    manifest = ToolManifest(
        tool_id='core.calgary_loader', version='1.0.0', display_name='Calgary Loader', catalog_ids=('calgary:calgary-loader',),
        inputs=(Anchor('input', 'I', description='Records to load'),), outputs=(Anchor('output', 'O', description='One summary record'),),
        side_effects='writes-files',
        description='Load records into a Flowbench indexed database (.fbcal, SQLite) with indexes on chosen fields, for fast '
                    'queries, joins and counts with the Calgary tools. Replaces the file. Not the reference .cydb format.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path', 'index_fields'], 'properties': {
            'path': {'type': 'string', 'minLength': 1, 'description': 'For example data/customers.fbcal'},
            'index_fields': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True, 'minItems': 1}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], cfg['index_fields'])
        return {'output': Schema([Field('Path', FieldType.V_WSTRING, 2**30 - 1), Field('Records', FieldType.INT64),
                                  Field('Indexes', FieldType.V_WSTRING, 2**30 - 1)])}

    def run(self, ctx, cfg, inputs):
        table: Table = inputs['input']
        out_schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        path = _open(ctx, cfg['path'], must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.fbcal.tmp')
        tmp.unlink(missing_ok=True)
        with closing(sqlite3.connect(tmp)) as conn:
            cols = ', '.join(_q(f.name) for f in table.schema)
            conn.execute(f'CREATE TABLE records ({cols})')
            conn.executemany(f'INSERT INTO records VALUES ({",".join("?" * len(table.schema))})',
                             ([_sql_value(v) for v in row] for row in table.rows()))
            for i, name in enumerate(cfg['index_fields']):
                conn.execute(f'CREATE INDEX idx_{i} ON records ({_q(name)} COLLATE NOCASE)')
                conn.execute(f'CREATE INDEX idx_{i}_exact ON records ({_q(name)})')
            conn.execute('CREATE TABLE calgary_meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.executemany('INSERT INTO calgary_meta VALUES (?, ?)', [
                ('format', FORMAT), ('schema', json.dumps(table.schema.to_list())), ('indexes', json.dumps(cfg['index_fields'])),
                ('records', str(len(table)))])
            conn.commit()
        tmp.replace(path)
        ctx.info(f'loaded {len(table):,} records into {path.name} with {len(cfg["index_fields"])} index(es)')
        return {'output': Table.from_rows(out_schema, [(str(path), len(table), ', '.join(cfg['index_fields']))])}


_DB = {'path': {'type': 'string', 'minLength': 1, 'description': 'The .fbcal database written by Calgary Loader'}}


class CalgaryInput(Tool):
    manifest = ToolManifest(
        tool_id='core.calgary_input', version='1.0.0', display_name='Calgary Input', catalog_ids=('calgary:calgary-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Records matching the query'),), side_effects='reads-files',
        description='Query a Flowbench indexed database (.fbcal): records meeting every condition, chosen fields, optional limit.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path'], 'properties': {**_DB,
            'query': {'type': 'array', 'items': _CONDITION, 'default': []},
            'fields': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True, 'default': []},
            'limit': {'type': 'integer', 'minimum': 0, 'default': 0}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        conn, schema, _ = _load(_open(ctx, cfg['path']))
        with closing(conn):
            names = cfg['fields'] or schema.names
            missing = [n for n in names if n not in schema]
            if missing:
                raise ConfigError(f'Field(s) {missing} are not in the database')
            where, params = _where(cfg['query'], schema)
            sql = f"SELECT {', '.join(_q(n) for n in names)} FROM records{where}" + (f" LIMIT {int(cfg['limit'])}" if cfg['limit'] else '')
            rows = conn.execute(sql, params).fetchall()
        return {'output': _to_table(Schema([schema.field(n) for n in names]), rows)}


class CalgaryJoin(Tool):
    manifest = ToolManifest(
        tool_id='core.calgary_join', version='1.0.0', display_name='Calgary Join', catalog_ids=('calgary:calgary-join',),
        inputs=(Anchor('input', 'I', description='Records whose fields are looked up'),),
        outputs=(Anchor('joined', 'J', description='Each input record with every matching database record'),
                 Anchor('unjoined', 'U', description='Input records with no match')), side_effects='reads-files',
        description='Join input records to a Flowbench indexed database on field pairs (input field = database field), '
                    'with an optional extra query; returns joined and unmatched records.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path', 'on'], 'properties': {**_DB,
            'on': {'type': 'array', 'minItems': 1, 'items': {'type': 'object', 'additionalProperties': False, 'required': ['input', 'database'],
                                                              'properties': {'input': {'type': 'string'}, 'database': {'type': 'string'}}}},
            'query': {'type': 'array', 'items': _CONDITION, 'default': []},
            'fields': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True, 'default': []},
            'prefix': {'type': 'string', 'default': 'DB_'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [p['input'] for p in cfg['on']])
        return None

    def run(self, ctx, cfg, inputs):
        table: Table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        conn, schema, _ = _load(_open(ctx, cfg['path']))
        with closing(conn):
            names = cfg['fields'] or schema.names
            where, params = _where(cfg['query'], schema)
            keys = ' AND '.join(f"{_q(p['database'])} = ?" for p in cfg['on'])
            for p in cfg['on']:
                if p['database'] not in schema:
                    raise ConfigError(f"Database field {p['database']!r} does not exist")
            sql = (f"SELECT {', '.join(_q(n) for n in names)} FROM records"
                   + (f'{where} AND {keys}' if where else f' WHERE {keys}'))
            db_fields = [Field(cfg['prefix'] + n, schema.field(n).type, schema.field(n).size, schema.field(n).scale) for n in names]
            clash = [f.name for f in db_fields if f.name in table.schema]
            if clash:
                raise ConfigError(f'Field(s) {clash} already exist; set a prefix')
            joined, unjoined = [], []
            idx = [table.schema.names.index(p['input']) for p in cfg['on']]
            for row in table.rows():
                ctx.check_cancelled()
                matches = conn.execute(sql, params + [_sql_value(row[i]) for i in idx]).fetchall()
                if matches:
                    joined += [list(row) + [cast_value(v, f)[0] for v, f in zip(m, db_fields)] for m in matches]
                else:
                    unjoined.append(row)
        return {'joined': Table.from_rows(Schema([*table.schema, *db_fields]), joined), 'unjoined': Table.from_rows(table.schema, unjoined)}


class CalgaryCrossCount(Tool):
    manifest = ToolManifest(
        tool_id='core.calgary_cross_count', version='1.0.0', display_name='Calgary Cross Count', catalog_ids=('calgary:calgary-cross-count',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per combination with its count'),), side_effects='reads-files',
        description='Count the records of a Flowbench indexed database for every combination of the cross-count fields '
                    '(optionally after a query).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path', 'fields'], 'properties': {**_DB,
            'fields': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True, 'minItems': 1},
            'query': {'type': 'array', 'items': _CONDITION, 'default': []}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        conn, schema, _ = _load(_open(ctx, cfg['path']))
        with closing(conn):
            missing = [n for n in cfg['fields'] if n not in schema]
            if missing:
                raise ConfigError(f'Field(s) {missing} are not in the database')
            where, params = _where(cfg['query'], schema)
            cols = ', '.join(_q(n) for n in cfg['fields'])
            rows = conn.execute(f'SELECT {cols}, COUNT(*) FROM records{where} GROUP BY {cols} ORDER BY {cols}', params).fetchall()
        out = Schema([*[schema.field(n) for n in cfg['fields']], Field('Count', FieldType.INT64)])
        return {'output': _to_table(out, rows)}


class CalgaryCrossCountAppend(Tool):
    manifest = ToolManifest(
        tool_id='core.calgary_cross_count_append', version='1.0.0', display_name='Calgary Cross Count Append',
        catalog_ids=('calgary:calgary-cross-count-append',),
        inputs=(Anchor('input', 'I', description='Records whose fields select database records'),),
        outputs=(Anchor('output', 'O', description='Each input record with counts appended'),), side_effects='reads-files',
        description='For each input record, count the database records matching it on field pairs, split into one count field '
                    'per value of an optional cross-count field (up to 1,000 values), plus the total.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path', 'on'], 'properties': {**_DB,
            'on': {'type': 'array', 'minItems': 1, 'items': {'type': 'object', 'additionalProperties': False, 'required': ['input', 'database'],
                                                              'properties': {'input': {'type': 'string'}, 'database': {'type': 'string'}}}},
            'cross_field': {'type': ['string', 'null'], 'default': None},
            'query': {'type': 'array', 'items': _CONDITION, 'default': []},
            'prefix': {'type': 'string', 'default': 'Count_'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [p['input'] for p in cfg['on']])
        return None

    def run(self, ctx, cfg, inputs):
        table: Table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        conn, schema, _ = _load(_open(ctx, cfg['path']))
        with closing(conn):
            for p in cfg['on']:
                if p['database'] not in schema:
                    raise ConfigError(f"Database field {p['database']!r} does not exist")
            where, params = _where(cfg['query'], schema)
            keys = ' AND '.join(f"{_q(p['database'])} = ?" for p in cfg['on'])
            cond = f'{where} AND {keys}' if where else f' WHERE {keys}'
            values = []
            if cfg['cross_field']:
                if cfg['cross_field'] not in schema:
                    raise ConfigError(f"Cross-count field {cfg['cross_field']!r} does not exist")
                values = [v for (v,) in conn.execute(f"SELECT DISTINCT {_q(cfg['cross_field'])} FROM records{where} "
                                                     f"ORDER BY 1", params).fetchall()]
                if len(values) > MAX_CROSS_VALUES:
                    raise ConfigError(f"{cfg['cross_field']} has {len(values):,} values; cross counts allow up to {MAX_CROSS_VALUES:,}")
            names = [f"{cfg['prefix']}{'Null' if v is None else format_value(v)}" for v in values] + [f"{cfg['prefix']}Total"]
            clash = [n for n in names if n in table.schema] + ([n for n in names if names.count(n) > 1][:1])
            if clash:
                raise ConfigError(f'Count field(s) {sorted(set(clash))} clash; set a prefix')
            idx = [table.schema.names.index(p['input']) for p in cfg['on']]
            rows = []
            for row in table.rows():
                ctx.check_cancelled()
                args = params + [_sql_value(row[i]) for i in idx]
                if cfg['cross_field']:
                    counts = dict(conn.execute(f"SELECT {_q(cfg['cross_field'])}, COUNT(*) FROM records{cond} GROUP BY 1", args).fetchall())
                    split = [counts.get(v, 0) for v in values]
                    rows.append([*row, *split, sum(split)])
                else:
                    rows.append([*row, conn.execute(f'SELECT COUNT(*) FROM records{cond}', args).fetchone()[0]])
        return {'output': Table.from_rows(Schema([*table.schema, *[Field(n, FieldType.INT64) for n in names]]), rows)}


TOOLS = [CalgaryLoader, CalgaryInput, CalgaryJoin, CalgaryCrossCount, CalgaryCrossCountAppend]
