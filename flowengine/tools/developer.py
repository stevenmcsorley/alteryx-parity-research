"""Developer category kernels: Field Info, JSON Parse, Dynamic Rename, Dynamic Select, Blob Convert,
Base64 Encoder, Message, Test, Block Until Done, Detour, Detour End, Expect Equals, Throttle,
Python, R, Run Command.

Tools that execute user code or external programs (Python, R, Run Command) run in a separate
process with a scrubbed environment and a timeout, and only when the run grants the
``allow_processes`` permission (CLI --allow-processes; desktop asks the user).
"""
from __future__ import annotations

import base64
import csv
import datetime as _dt
import json
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from ..errors import ConfigError, DataError, EngineError, SchemaError
from ..formula import EvalContext, compile_expression, truthy
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import CastFailure, Field, FieldType, Schema, cast_value, format_value
from ._common import FIELD_SPEC, TYPE_ENUM, require_fields

TEXT = 2**30 - 1


def _require_processes(ctx, tool):
    if not ctx.limits.get('allow_processes'):
        raise EngineError(f'{tool} runs code or programs outside the engine and is disabled for this run. '
                          f'Allow it explicitly (CLI --allow-processes, or confirm in the desktop app).',
                          code='permission-denied', node_id=ctx.node_id)


# =========================================================================== Field Info

class FieldInfo(Tool):
    manifest = ToolManifest(
        tool_id='core.field_info', version='1.0.0', display_name='Field Info', catalog_ids=('developer:field-info',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='One record per incoming field: Name, Type, Size, Scale, Source, Description. Source is null: '
                    'flowengine does not yet track field provenance.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    SCHEMA = Schema([Field('Name', FieldType.V_WSTRING, TEXT), Field('Type', FieldType.V_STRING, 32),
                     Field('Size', FieldType.INT32), Field('Scale', FieldType.INT16),
                     Field('Source', FieldType.V_WSTRING, TEXT), Field('Description', FieldType.V_WSTRING, TEXT)])

    # Type vocabulary and storage sizes as documented for the reference product's data types; Field Info
    # output is data that downstream tools compare against, so it uses that vocabulary.
    REF_NAMES = {'bool': 'Bool', 'byte': 'Byte', 'int16': 'Int16', 'int32': 'Int32', 'int64': 'Int64',
                 'fixed_decimal': 'FixedDecimal', 'float': 'Float', 'double': 'Double', 'string': 'String',
                 'wstring': 'WString', 'v_string': 'V_String', 'v_wstring': 'V_WString', 'date': 'Date',
                 'time': 'Time', 'datetime': 'DateTime', 'blob': 'Blob'}
    FIXED_SIZES = {'bool': 1, 'byte': 1, 'int16': 2, 'int32': 4, 'int64': 8, 'float': 4, 'double': 8,
                   'date': 10, 'time': 8, 'datetime': 19}

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        rows = []
        for f in t.schema:
            size = f.size if f.size is not None else self.FIXED_SIZES.get(f.type.value)
            scale = f.scale if f.type is FieldType.FIXED_DECIMAL else None  # [ref:field-info-basic]
            rows.append((f.name, self.REF_NAMES[f.type.value], size, scale, None, f.description))
        return {'output': Table.from_rows(self.SCHEMA, rows)}


# =========================================================================== JSON Parse

def _flatten(value, prefix, out):
    # empty arrays/objects produce no record [ref:probe-json-false-null]
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(v, f'{prefix}.{k}' if prefix else str(k), out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten(v, f'{prefix}.{i}' if prefix else str(i), out)
    else:
        out.append((prefix, value))


class JSONParse(Tool):
    streaming = 'row-local'
    manifest = ToolManifest(
        tool_id='core.json_parse', version='1.0.0', display_name='JSON Parse', catalog_ids=('developer:json-parse',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Flatten JSON text into one record per leaf value: JSON_Name (dotted path, array indexes as '
                    'numbers) and JSON_ValueString, or typed value columns.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {
            'field': {'type': 'string'}, 'include_input_fields': {'type': 'boolean', 'default': False},
            'single_field': {'type': 'boolean', 'default': True,
                             'description': 'Values in one text field; otherwise String/Int/Float/Bool columns'},
            'ignore_errors': {'type': 'boolean', 'default': False}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        fi = t.schema.index(cfg['field'])
        keep = list(t.schema) if cfg['include_input_fields'] else []
        if cfg['single_field']:
            new = [Field('JSON_Name', FieldType.V_WSTRING, TEXT), Field('JSON_ValueString', FieldType.V_WSTRING, TEXT)]
        else:
            new = [Field('JSON_Name', FieldType.V_WSTRING, TEXT), Field('JSON_ValueString', FieldType.V_WSTRING, TEXT),
                   Field('JSON_ValueInt', FieldType.INT64), Field('JSON_ValueFloat', FieldType.DOUBLE),
                   Field('JSON_ValueBool', FieldType.BOOL)]
        rows = []
        for n, row in enumerate(t.rows()):
            text = row[fi]
            if text is None:
                continue
            try:
                doc = json.loads(text)
            except (json.JSONDecodeError, TypeError) as e:
                if cfg['ignore_errors']:
                    ctx.warn(f'record {n + 1}: invalid JSON ignored')
                    continue
                raise DataError(f'record {n + 1}: invalid JSON: {e}') from None
            leaves = []
            _flatten(doc, '', leaves)
            base = row if keep else ()
            for name, v in leaves:
                if cfg['single_field']:
                    # booleans render as 1/0 in the single value field [ref:json-parse-single]
                    val = None if v is None else ('1' if v is True else '0' if v is False else
                                                  v if isinstance(v, str) else format_value(v))
                    rows.append(base + (name, val))
                else:
                    rows.append(base + (name, v if isinstance(v, str) else None,
                                        v if isinstance(v, int) and not isinstance(v, bool) else None,
                                        float(v) if isinstance(v, float) else None,
                                        v if isinstance(v, bool) else None))
        return {'output': Table.from_rows(keep + new, rows)}


# =========================================================================== Dynamic Rename / Select

class DynamicRename(Tool):
    manifest = ToolManifest(
        tool_id='core.dynamic_rename', version='1.0.0', display_name='Dynamic Rename',
        catalog_ids=('developer:dynamic-rename',),
        inputs=(Anchor('input', 'L'), Anchor('names', 'R', required=False,
                                             description='Rename map (right_input mode): old and new name fields')),
        outputs=(Anchor('output', 'O'),),
        description='Rename fields from the first data row, a prefix/suffix, removing a prefix/suffix, an expression '
                    'over [_CurrentField_] (the old name), or a mapping table on the R input.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['mode'], 'properties': {
            'mode': {'enum': ['first_row', 'add_prefix', 'add_suffix', 'remove_prefix', 'remove_suffix', 'formula',
                              'right_input']},
            'fields': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None,
                       'description': 'Fields to rename (null = all)'},
            'text': {'type': 'string', 'default': ''},
            'expression': {'type': 'string'},
            'old_name_field': {'type': 'string', 'default': 'Old'},
            'new_name_field': {'type': 'string', 'default': 'New'}}})

    def infer(self, ctx, cfg, inputs):
        if cfg['mode'] in ('first_row', 'right_input'):
            return None  # names come from data
        return super().infer(ctx, cfg, inputs)

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        names = cfg['fields'] if cfg['fields'] is not None else t.schema.names
        require_fields(t.schema, names)
        mode = cfg['mode']
        mapping: dict = {}
        out = t
        if mode == 'first_row':
            if not len(t):
                raise DataError('No first row to take field names from')
            first = t.row(0)
            for n in names:
                v = first[t.schema.index(n)]
                if v is not None and str(v).strip():
                    mapping[n] = format_value(v).strip()
            out = t.take(range(1, len(t)))
        elif mode in ('add_prefix', 'add_suffix', 'remove_prefix', 'remove_suffix'):
            x = cfg['text']
            for n in names:
                if mode == 'add_prefix':
                    mapping[n] = x + n
                elif mode == 'add_suffix':
                    mapping[n] = n + x
                elif mode == 'remove_prefix' and x and n.startswith(x):
                    mapping[n] = n[len(x):]
                elif mode == 'remove_suffix' and x and n.endswith(x):
                    mapping[n] = n[:-len(x)]
        elif mode == 'formula':
            if not cfg.get('expression'):
                raise ConfigError('Formula mode needs an expression')
            sch = Schema([Field('_CurrentField_', FieldType.V_WSTRING)])
            expr = compile_expression(cfg['expression'], sch)
            ec = EvalContext(now=ctx.clock)
            for n in names:
                v = expr.evaluate((n,), ec)
                if v is not None:
                    mapping[n] = format_value(v)
        else:
            m: Table | None = inputs.get('names')
            if m is None:
                raise ConfigError('right_input mode needs the R input connected')
            require_fields(m.schema, [cfg['old_name_field'], cfg['new_name_field']])
            for old, new in zip(m.column(cfg['old_name_field']), m.column(cfg['new_name_field'])):
                if old in names and new:
                    mapping[old] = format_value(new)
        fields = [f.renamed(mapping.get(f.name, f.name)) for f in out.schema]
        try:
            return {'output': Table(Schema(fields), out.columns)}
        except SchemaError as e:
            raise DataError(f'Renaming produced duplicate names: {e.message}') from None


class DynamicSelect(Tool):
    manifest = ToolManifest(
        tool_id='core.dynamic_select', version='1.0.0', display_name='Dynamic Select',
        catalog_ids=('developer:dynamic-select',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Keep fields by type, or by a boolean expression over [Name], [Type], [Size], [Position].',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['mode'], 'properties': {
            'mode': {'enum': ['types', 'formula']},
            'types': {'type': 'array', 'items': {'enum': TYPE_ENUM}, 'default': []},
            'expression': {'type': 'string'}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        if cfg['mode'] == 'types':
            keep = [f.name for f in t.schema if f.type.value in cfg['types']]
        else:
            if not cfg.get('expression'):
                raise ConfigError('Formula mode needs an expression')
            sch = Schema([Field('Name', FieldType.V_WSTRING), Field('Type', FieldType.V_WSTRING),
                          Field('Size', FieldType.INT64), Field('Position', FieldType.INT64)])
            expr = compile_expression(cfg['expression'], sch)
            ec = EvalContext(now=ctx.clock)
            keep = [f.name for i, f in enumerate(t.schema)
                    if truthy(expr.evaluate((f.name, f.type.value, f.size, i), ec))]
        return {'output': t.select(keep)}


# =========================================================================== Blob Convert / Base64

class BlobConvert(Tool):
    streaming = 'row-local'
    manifest = ToolManifest(
        tool_id='core.blob_convert', version='1.0.0', display_name='Blob Convert', catalog_ids=('developer:blob-convert',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Convert text to binary (UTF-8, UTF-16LE, Latin-1 or Base64-decoded) or binary to text.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['direction', 'field', 'output_field'], 'properties': {
            'direction': {'enum': ['to_blob', 'from_blob']}, 'field': {'type': 'string'},
            'output_field': {'type': 'string', 'minLength': 1},
            'encoding': {'enum': ['utf-8', 'utf-16-le', 'latin-1', 'base64'], 'default': 'utf-8'}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        enc = cfg['encoding']
        out = []
        for v in t.column(cfg['field']):
            if v is None:
                out.append(None)
                continue
            try:
                if cfg['direction'] == 'to_blob':
                    s = v if isinstance(v, str) else format_value(v)
                    out.append(base64.b64decode(s, validate=True) if enc == 'base64' else s.encode(enc))
                else:
                    if not isinstance(v, bytes):
                        raise DataError(f"{cfg['field']!r} is not a blob field")
                    out.append(base64.b64encode(v).decode('ascii') if enc == 'base64' else v.decode(enc))
            except (UnicodeError, ValueError) as e:
                raise DataError(f'Blob conversion failed: {e}') from None
        f = Field(cfg['output_field'], FieldType.BLOB if cfg['direction'] == 'to_blob' else FieldType.V_WSTRING,
                  None if cfg['direction'] == 'to_blob' else TEXT)
        return {'output': t.with_column(f, out)}


class Base64Encoder(Tool):
    streaming = 'row-local'
    manifest = ToolManifest(
        tool_id='core.base64_encoder', version='1.0.0', display_name='Base64 Encoder',
        catalog_ids=('developer:base64-encoder',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Base64-encode the UTF-8 bytes of a text field into a new field.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {
            'field': {'type': 'string'}, 'output_field': {'type': ['string', 'null'], 'default': None}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        name = cfg['output_field'] or f"{cfg['field']}_Base64_Encoded"
        vals = [None if v is None else base64.b64encode((v if isinstance(v, str) else format_value(v)).encode('utf-8')).decode('ascii')
                for v in t.column(cfg['field'])]
        return {'output': t.with_column(Field(name, FieldType.V_STRING, TEXT), vals)}


# =========================================================================== Message / Test / pass-throughs

class Message(Tool):
    manifest = ToolManifest(
        tool_id='core.message', version='1.0.0', display_name='Message', catalog_ids=('developer:message',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O', description='Records pass through unchanged'),),
        description='Emit a message, warning or error built from an expression for the first, last, every, or '
                    'expression-matching record; or after all records. An error stops the run.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['message'], 'properties': {
            'when': {'enum': ['first', 'last', 'every', 'expression', 'end'], 'default': 'first'},
            'condition': {'type': 'string', 'description': 'when=expression: boolean expression'},
            'message': {'type': 'string', 'minLength': 1, 'description': 'Expression producing the message text'},
            'level': {'enum': ['info', 'warning', 'error'], 'default': 'info'},
            'limit': {'type': 'integer', 'minimum': 1, 'default': 100}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        if ctx.dry_run:
            return {'output': t}
        msg = compile_expression(cfg['message'], t.schema)
        cond = compile_expression(cfg['condition'], t.schema) if cfg['when'] == 'expression' else None
        if cfg['when'] == 'expression' and cond is None:
            raise ConfigError('when=expression needs a condition')
        ec = EvalContext(now=ctx.clock)
        rows = list(t.rows())
        when = cfg['when']
        if when == 'first':
            picks = rows[:1]
        elif when == 'last':
            picks = rows[-1:]
        elif when == 'every':
            picks = rows
        elif when == 'end':
            picks = [rows[-1] if rows else tuple(None for _ in t.schema)]
        else:
            picks = [r for r in rows if truthy(cond.evaluate(r, ec))]
        for r in picks[:cfg['limit']]:
            text = format_value(msg.evaluate(r, ec)) or ''
            if cfg['level'] == 'error':
                raise DataError(text, code='message-error')
            ctx.warn(text) if cfg['level'] == 'warning' else ctx.info(text)
        return {'output': t}


class TestTool(Tool):
    manifest = ToolManifest(
        tool_id='core.test', version='1.0.0', display_name='Test', catalog_ids=('developer:test',),
        inputs=(Anchor('inputs', 'I', multiple=True, description='Connections #1, #2, ... in order'),), outputs=(),
        description='Assert record counts or that an expression holds for every record of a connection; a failing '
                    'test stops the run with an error.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['tests'], 'properties': {
            'tests': {'type': 'array', 'minItems': 1, 'items': {
                'type': 'object', 'additionalProperties': False, 'required': ['name', 'connection', 'type'],
                'properties': {'name': {'type': 'string'}, 'connection': {'type': 'integer', 'minimum': 1},
                               'type': {'enum': ['record_count_equals', 'record_count_zero', 'expression_true_all']},
                               'value': {'type': ['integer', 'string']}}}}}})

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        tables: list[Table] = inputs['inputs']
        for test in cfg['tests']:
            k = test['connection']
            if k > len(tables):
                raise ConfigError(f"Test {test['name']!r} refers to connection #{k}; only {len(tables)} connected")
            t = tables[k - 1]
            ok = True
            if test['type'] == 'record_count_equals':
                ok = len(t) == int(test.get('value', 0))
                detail = f'{len(t)} records, expected {test.get("value")}'
            elif test['type'] == 'record_count_zero':
                ok = len(t) == 0
                detail = f'{len(t)} records, expected none'
            else:
                expr = compile_expression(str(test.get('value') or ''), t.schema)
                ec = EvalContext(now=ctx.clock)
                bad = sum(1 for r in t.rows() if not truthy(expr.evaluate(r, ec)))
                ok = bad == 0
                detail = f'{bad} record(s) failed'
            if not ok:
                raise DataError(f"Test {test['name']!r} failed: {detail}", code='test-failed')
            ctx.info(f"Test {test['name']!r} passed")
        return {}


class BlockUntilDone(Tool):
    manifest = ToolManifest(
        tool_id='core.block_until_done', version='1.0.0', display_name='Block Until Done',
        catalog_ids=('developer:block-until-done',),
        inputs=(Anchor('input', 'I'),),
        outputs=(Anchor('output1', '1'), Anchor('output2', '2'), Anchor('output3', '3')),
        description='Pass all records to output 1, then 2, then 3, only after the input is complete. Downstream '
                    'branches run in that order (the executor orders by connection when ties exist).',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def run(self, ctx, cfg, inputs):
        t = inputs['input']
        return {'output1': t, 'output2': t, 'output3': t}


class Detour(Tool):
    manifest = ToolManifest(
        tool_id='core.detour', version='1.0.0', display_name='Detour', catalog_ids=('developer:detour',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('left', 'L'), Anchor('right', 'R')),
        description='Route every record to the left or right output (typically set by an app/macro question).',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'detour_right': {'type': 'boolean', 'default': False}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        empty = Table.empty(t.schema)
        return {'left': empty, 'right': t} if cfg['detour_right'] else {'left': t, 'right': empty}


class DetourEnd(Tool):
    manifest = ToolManifest(
        tool_id='core.detour_end', version='1.0.0', display_name='Detour End', catalog_ids=('developer:detour-end',),
        inputs=(Anchor('left', 'L', required=False), Anchor('right', 'R', required=False)),
        outputs=(Anchor('output', 'O'),),
        description='Re-join the two Detour branches: outputs whichever connected branch carries records '
                    '(left if both do).',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        return None  # schema depends on which branch was taken

    def run(self, ctx, cfg, inputs):
        left, right = inputs.get('left'), inputs.get('right')
        if left is None and right is None:
            raise ConfigError('Connect at least one Detour branch')
        if left is not None and (len(left) or right is None):
            return {'output': left}
        return {'output': right}


class ExpectEquals(Tool):
    manifest = ToolManifest(
        tool_id='core.expect_equals', version='1.0.0', display_name='Expect Equals',
        catalog_ids=('developer:expect-equals',),
        inputs=(Anchor('expected', 'E'), Anchor('actual', 'A')), outputs=(),
        description='Fail the run unless both inputs have identical field names, types and values in the same order.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'compare_types': {'type': 'boolean', 'default': True},
            'tolerance': {'type': ['number', 'null'], 'default': None,
                          'description': 'Absolute tolerance for numeric values (null = exact)'}}})

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        e, a = inputs['expected'], inputs['actual']
        if e.schema.names != a.schema.names:
            raise DataError(f'Field names differ: expected {e.schema.names}, got {a.schema.names}', code='test-failed')
        if cfg['compare_types'] and [f.type for f in e.schema] != [f.type for f in a.schema]:
            raise DataError('Field types differ', code='test-failed')
        if len(e) != len(a):
            raise DataError(f'Record counts differ: expected {len(e)}, got {len(a)}', code='test-failed')
        tol = cfg['tolerance']
        for i, (r1, r2) in enumerate(zip(e.rows(), a.rows())):
            for name, x, y in zip(e.schema.names, r1, r2):
                if x == y:
                    continue
                if tol is not None and isinstance(x, (int, float, Decimal)) and isinstance(y, (int, float, Decimal)) \
                        and abs(float(x) - float(y)) <= tol:
                    continue
                raise DataError(f'Record {i + 1} field {name!r}: expected {format_value(x)!r}, got {format_value(y)!r}',
                                code='test-failed')
        ctx.info(f'{len(e)} records equal')
        return {}


class Throttle(Tool):
    manifest = ToolManifest(
        tool_id='core.throttle', version='1.0.0', display_name='Throttle', catalog_ids=('developer:throttle',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Pass records through at no more than N records per minute (sleeps between batches; output is '
                    'identical to the input).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['records_per_minute'], 'properties': {
            'records_per_minute': {'type': 'integer', 'minimum': 1}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        if not ctx.dry_run and len(t):
            delay = 60.0 / cfg['records_per_minute']
            for _ in range(len(t) - 1):
                ctx.check_cancelled()
                time.sleep(delay)
        return {'output': t}


# =========================================================================== user code exchange format

_EXCHANGE_HELPER = r'''
"""flowbench_io: read/write typed tables for Flowbench code tools (no third-party dependencies)."""
import base64, datetime, decimal, json, os
_DIR = os.path.dirname(os.path.abspath(__file__))

def _dec(t, v):
    if v is None: return None
    if t == 'date': return datetime.date.fromisoformat(v)
    if t == 'datetime': return datetime.datetime.fromisoformat(v)
    if t == 'time': return datetime.time.fromisoformat(v)
    if t == 'fixed_decimal': return decimal.Decimal(v)
    if t == 'blob': return base64.b64decode(v)
    return v

def read_input(n=1, as_frame=False):
    """Records of input connection #n as a list of dicts (or a pandas DataFrame with as_frame=True)."""
    with open(os.path.join(_DIR, f'in{n}.jsonl'), encoding='utf-8') as f:
        schema = json.loads(f.readline())
        names = [x['name'] for x in schema]
        rows = [dict(zip(names, (_dec(x['type'], v) for x, v in zip(schema, json.loads(line))))) for line in f]
    if as_frame:
        import pandas as pd
        return pd.DataFrame(rows, columns=names)
    return rows

def input_count():
    return sum(1 for f in os.listdir(_DIR) if f.startswith('in') and f.endswith('.jsonl'))

def _enc(v):
    if v is None: return None
    if isinstance(v, bool) or isinstance(v, (int, float, str)): return v
    if isinstance(v, (datetime.date, datetime.time)): return v.isoformat()
    if isinstance(v, decimal.Decimal): return str(v)
    if isinstance(v, bytes): return base64.b64encode(v).decode('ascii')
    if hasattr(v, 'item'): return _enc(v.item())  # numpy scalars
    return str(v)

def write_output(n, data, schema=None):
    """Write output #n (1-5) from a list of dicts or a pandas DataFrame. schema: optional
    [{'name':..,'type':..}] using Flowbench type names; otherwise types are inferred."""
    if hasattr(data, 'to_dict'):
        cols = [str(c) for c in data.columns]
        recs = [dict(zip(cols, r)) for r in data.itertuples(index=False, name=None)]
    else:
        recs = list(data)
        cols = list(schema and [s['name'] for s in schema] or (recs[0].keys() if recs else []))
    with open(os.path.join(_DIR, f'out{n}.jsonl'), 'w', encoding='utf-8') as f:
        f.write(json.dumps(schema or [{'name': c} for c in cols]) + '\n')
        for r in recs:
            f.write(json.dumps([_enc(r.get(c)) for c in cols], ensure_ascii=False) + '\n')
'''


def _write_exchange(path: Path, t: Table):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(t.schema.to_list()) + '\n')
        for r in t.rows():
            f.write(json.dumps([_json_cell(v) for v in r], ensure_ascii=False) + '\n')


def _json_cell(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, bytes):
        return base64.b64encode(v).decode('ascii')
    return format_value(v)


def _read_exchange(path: Path, ctx) -> Table:
    with open(path, encoding='utf-8') as f:
        header = json.loads(f.readline() or '[]')
        rows = [json.loads(line) for line in f if line.strip()]
    if any('type' not in h for h in header):
        cols = list(zip(*rows)) if rows else [[] for _ in header]
        from ..types import type_for_values
        fields = []
        for h, c in zip(header, cols):
            t = type_for_values(c)
            fields.append(Field(h['name'], t, TEXT if t is FieldType.V_WSTRING else None))
    else:
        fields = [Field(h['name'], FieldType(h['type']), h.get('size'), h.get('scale')) for h in header]
    out_cols = [[] for _ in fields]
    for i, r in enumerate(rows):
        if len(r) != len(fields):
            raise DataError(f'{path.name}: row {i + 1} has {len(r)} values for {len(fields)} fields')
        for c, f, v in zip(out_cols, fields, r):
            try:
                if f.type is FieldType.BLOB and isinstance(v, str):
                    v = base64.b64decode(v)
                c.append(cast_value(v, f)[0])
            except CastFailure as e:
                raise DataError(f'{path.name}: row {i + 1} field {f.name!r}: {e}') from None
    return Table(Schema(fields), out_cols) if fields else Table([], [])


def _run_process(ctx, argv, cwd, timeout):
    from ..runtimes import clean_env
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8', errors='replace')
    except OSError as e:
        raise DataError(f'Could not start {argv[0]}: {e}', code='runtime-missing') from None
    deadline = time.monotonic() + timeout
    while p.poll() is None:
        if ctx.cancel_token.cancelled or time.monotonic() > deadline:
            p.kill()
            p.wait()
            if ctx.cancel_token.cancelled:
                ctx.check_cancelled()
            raise DataError(f'Process exceeded the {timeout} s time limit and was stopped', code='timeout')
        time.sleep(0.05)
    out, err = p.communicate()
    for line in out.splitlines()[-50:]:
        ctx.info(line)
    if p.returncode != 0:
        raise DataError(f'Process exited with code {p.returncode}: {err.strip()[-2000:]}', code='user-code-error')
    if err.strip():
        ctx.warn(err.strip()[-2000:])


class PythonTool(Tool):
    manifest = ToolManifest(
        tool_id='core.python', version='1.0.0', display_name='Python', catalog_ids=('developer:python',),
        inputs=(Anchor('inputs', 'I', required=False, multiple=True, description='Read with read_input(n)'),),
        outputs=tuple(Anchor(f'output{i}', str(i)) for i in range(1, 6)),
        side_effects='process', deterministic=False,
        description='Run Python code in an isolated process (named environment; Python 3.12 baseline). Use '
                    '"from flowbench_io import read_input, write_output". Outputs not written are empty.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['code'], 'properties': {
            'code': {'type': 'string', 'minLength': 1},
            'environment': {'type': 'string', 'default': 'default'},
            'timeout_seconds': {'type': 'integer', 'minimum': 1, 'default': 600}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _require_processes(ctx, 'The Python tool')
        from ..runtimes import interpreter
        exe = interpreter(cfg['environment'])
        with tempfile.TemporaryDirectory(prefix='flowbench-py-') as d:
            d = Path(d)
            (d / 'flowbench_io.py').write_text(_EXCHANGE_HELPER, encoding='utf-8')
            for k, t in enumerate(inputs.get('inputs') or []):
                _write_exchange(d / f'in{k + 1}.jsonl', t)
            (d / 'user_code.py').write_text(cfg['code'], encoding='utf-8')
            _run_process(ctx, [exe, '-I', '-X', 'utf8', '-c',
                               f'import sys; sys.path.insert(0, {str(d)!r}); '
                               f'exec(compile(open({str(d / "user_code.py")!r}, encoding="utf-8").read(), '
                               f'"user_code.py", "exec"), {{"__name__": "__main__"}})'],
                         d, cfg['timeout_seconds'])
            return {f'output{i}': (_read_exchange(d / f'out{i}.jsonl', ctx) if (d / f'out{i}.jsonl').is_file()
                                   else Table([], [])) for i in range(1, 6)}


_R_HELPER = r'''
read_input <- function(n = 1) {
  p <- file.path(Sys.getenv("FLOWBENCH_DIR"), paste0("in", n, ".csv"))
  utils::read.csv(p, stringsAsFactors = FALSE, na.strings = "\\N", check.names = FALSE)
}
write_output <- function(n, df) {
  p <- file.path(Sys.getenv("FLOWBENCH_DIR"), paste0("out", n, ".csv"))
  utils::write.csv(df, p, row.names = FALSE, na = "\\N")
}
'''


class RTool(Tool):
    manifest = ToolManifest(
        tool_id='core.r', version='1.0.0', display_name='R', catalog_ids=('developer:r',),
        inputs=(Anchor('inputs', 'I', required=False, multiple=True),),
        outputs=tuple(Anchor(f'output{i}', str(i)) for i in range(1, 6)),
        side_effects='process', deterministic=False,
        description='Run R code with a separately installed Rscript (CRAN). Declared packages are checked first; '
                    'read_input(n) / write_output(n, df) exchange CSV. Output fields are text.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['code'], 'properties': {
            'code': {'type': 'string', 'minLength': 1},
            'packages': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            'timeout_seconds': {'type': 'integer', 'minimum': 1, 'default': 600}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _require_processes(ctx, 'The R tool')
        from ..runtimes import require_rscript
        exe = require_rscript()
        with tempfile.TemporaryDirectory(prefix='flowbench-r-') as d:
            d = Path(d)
            for k, t in enumerate(inputs.get('inputs') or []):
                with open(d / f'in{k + 1}.csv', 'w', encoding='utf-8', newline='') as f:
                    w = csv.writer(f)
                    w.writerow(t.schema.names)
                    for r in t.rows():
                        w.writerow(['\\N' if v is None else format_value(v) for v in r])
            pkgs = ''.join(f'if (!requireNamespace("{p}", quietly=TRUE)) stop("R package {p} is not installed")\n'
                           f'cat("package {p}", as.character(utils::packageVersion("{p}")), "\\n")\n' for p in cfg['packages'])
            (d / 'main.R').write_text(f'Sys.setenv(FLOWBENCH_DIR={json.dumps(str(d))})\n{_R_HELPER}\n{pkgs}\n{cfg["code"]}\n',
                                      encoding='utf-8')
            _run_process(ctx, [exe, '--vanilla', str(d / 'main.R')], d, cfg['timeout_seconds'])
            outs = {}
            for i in range(1, 6):
                p = d / f'out{i}.csv'
                if not p.is_file():
                    outs[f'output{i}'] = Table([], [])
                    continue
                with open(p, encoding='utf-8', newline='') as f:
                    rows = list(csv.reader(f))
                names = rows[0] if rows else []
                fields = [Field(n, FieldType.V_WSTRING, TEXT) for n in names]
                outs[f'output{i}'] = Table.from_rows(fields, [[None if v == '\\N' else v for v in r] for r in rows[1:]])
            return outs


class RunCommand(Tool):
    manifest = ToolManifest(
        tool_id='core.run_command', version='1.0.0', display_name='Run Command', catalog_ids=('developer:run-command',),
        inputs=(Anchor('input', 'I', required=False, description='Optionally written to input_file first'),),
        outputs=(Anchor('output', 'O', description='Read from output_file after the program exits'),),
        side_effects='process', deterministic=False,
        description='Run an external program (no shell) with arguments; optionally write the input records to a CSV '
                    'file before and read a CSV file back afterwards.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['program'], 'properties': {
            'program': {'type': 'string', 'minLength': 1}, 'arguments': {'type': 'array', 'items': {'type': 'string'},
                                                                          'default': []},
            'working_dir': {'type': ['string', 'null'], 'default': None},
            'input_file': {'type': ['string', 'null'], 'default': None},
            'output_file': {'type': ['string', 'null'], 'default': None},
            'timeout_seconds': {'type': 'integer', 'minimum': 1, 'default': 600}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _require_processes(ctx, 'Run Command')
        cwd = ctx.resolve_path(cfg['working_dir']) if cfg['working_dir'] else Path(ctx.base_dir)
        t = inputs.get('input')
        if cfg['input_file'] and t is not None:
            with open(cwd / cfg['input_file'], 'w', encoding='utf-8', newline='') as f:
                w = csv.writer(f)
                w.writerow(t.schema.names)
                for r in t.rows():
                    w.writerow(['' if v is None else format_value(v) for v in r])
        _run_process(ctx, [cfg['program'], *cfg['arguments']], cwd, cfg['timeout_seconds'])
        if not cfg['output_file']:
            return {'output': Table([], [])}
        p = cwd / cfg['output_file']
        if not p.is_file():
            raise DataError(f'Program did not create {p}')
        with open(p, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
        names = rows[0] if rows else []
        return {'output': Table.from_rows([Field(n, FieldType.V_WSTRING, TEXT) for n in names], rows[1:])}


TOOLS = [FieldInfo, JSONParse, DynamicRename, DynamicSelect, BlobConvert, Base64Encoder, Message, TestTool,
         BlockUntilDone, Detour, DetourEnd, ExpectEquals, Throttle, PythonTool, RTool, RunCommand]
