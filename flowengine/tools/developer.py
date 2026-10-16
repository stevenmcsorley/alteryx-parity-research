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
import re
import os
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
from ._common import FIELD_SPEC, TYPE_ENUM, cast_column, require_fields

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

class JSONSchemaTool(Tool):
    manifest = ToolManifest(
        tool_id='core.json_schema', version='1.0.0', display_name='JSON Schema',
        catalog_ids=('developer:json-schema',),
        inputs=(Anchor('input', 'Input', required=False),), outputs=(Anchor('output', 'Output'),),
        description='Generate a JSON Schema object and append its serialized form to each input row.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['title', 'fields'], 'properties': {
            'title': {'type': 'string', 'minLength': 1}, 'description': {'type': 'string', 'default': ''},
            'output_field': {'type': 'string', 'minLength': 1, 'default': 'JSON Schema'},
            'fields': {'type': 'array', 'minItems': 1, 'items': {'type': 'object', 'additionalProperties': False,
                'required': ['name', 'type'], 'properties': {
                    'name': {'type': 'string', 'minLength': 1},
                    'type': {'enum': ['string', 'integer', 'number', 'boolean', 'date', 'datetime', 'enum']},
                    'description': {'type': 'string', 'default': ''},
                    'enum_values': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
                    'default_value': {}, 'required': {'type': 'boolean', 'default': False},
                    'minimum': {'type': 'number'}, 'maximum': {'type': 'number'},
                    'min_length': {'type': 'integer', 'minimum': 0}, 'max_length': {'type': 'integer', 'minimum': 0},
                    'min_date': {'type': 'string'}, 'max_date': {'type': 'string'},
                }}}
        }})

    def check_config(self, cfg):
        names = [f['name'] for f in cfg['fields']]
        if len(names) != len(set(names)):
            raise ConfigError('JSON Schema field names must be unique')
        for f in cfg['fields']:
            if f['type'] == 'enum' and not f['enum_values']:
                raise ConfigError(f"Enum field {f['name']!r} requires at least one enum value")
            if f.get('minimum') is not None and f.get('maximum') is not None and f['minimum'] > f['maximum']:
                raise ConfigError(f"Minimum exceeds maximum for {f['name']!r}")
            if f.get('min_length') is not None and f.get('max_length') is not None and f['min_length'] > f['max_length']:
                raise ConfigError(f"Minimum length exceeds maximum length for {f['name']!r}")
            if f.get('min_date') and f.get('max_date') and f['min_date'] > f['max_date']:
                raise ConfigError(f"Minimum date exceeds maximum date for {f['name']!r}")

    def run(self, ctx, cfg, inputs):
        schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'title': cfg['title'], 'type': 'object', 'properties': {}}
        if cfg['description']:
            schema['description'] = cfg['description']
        required = []
        for f in cfg['fields']:
            kind = f['type']
            prop = {'type': 'string' if kind in ('date', 'datetime', 'enum') else kind}
            if f.get('description'):
                prop['description'] = f['description']
            if kind == 'enum':
                prop['enum'] = f['enum_values']
            if kind == 'date':
                prop['format'] = 'date'
            elif kind == 'datetime':
                prop['format'] = 'date-time'
            for source, dest in (('minimum', 'minimum'), ('maximum', 'maximum'),
                                 ('min_length', 'minLength'), ('max_length', 'maxLength')):
                if f.get(source) is not None:
                    prop[dest] = f[source]
            if f.get('min_date'):
                prop['x-flowbench-min-date'] = f['min_date']
            if f.get('max_date'):
                prop['x-flowbench-max-date'] = f['max_date']
            if 'default_value' in f:
                prop['default'] = f['default_value']
            if f.get('required'):
                required.append(f['name'])
            schema['properties'][f['name']] = prop
        if required:
            schema['required'] = required
        serialized = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        field = Field(cfg['output_field'], FieldType.V_WSTRING, max(1, len(serialized)))
        incoming = inputs.get('input')
        if incoming is None:
            return {'output': Table.from_rows([field], [[serialized]])}
        if cfg['output_field'] in incoming.schema:
            raise ConfigError(f"Output field {cfg['output_field']!r} already exists in the input")
        return {'output': incoming.with_column(field, [serialized] * len(incoming))}

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


def _lenient_json(text):
    """Strict JSON first; on failure retry with trailing commas removed (accepted by the reference
    [ref:holdout JSON_Parse sample])."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        relaxed = re.sub(r',(\s*[}\]])', lambda m: m.group(1), text)
        if relaxed == text:
            raise
        return json.loads(relaxed)


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
                doc = _lenient_json(text)
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


_JSON_NUMBER = re.compile(r'-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?')


class JSONBuild(Tool):
    """Inverse of JSON Parse: JSON_Name paths and values back into JSON documents."""
    manifest = ToolManifest(
        tool_id='core.json_build', version='1.0.0', display_name='JSON Build', catalog_ids=('laboratory:json-build',),
        inputs=(Anchor('input', 'Input', description='JSON_Name / value records, e.g. from JSON Parse'),),
        outputs=(Anchor('output', 'Output', description='One JSON document per group'),),
        description='Rebuild JSON from name/value records. Dotted names (a.b.c) make objects and numeric segments '
                    '(items.0) make arrays, in record order. Values come from JSON_ValueInt/Float/Bool when present, '
                    'otherwise JSON_ValueString, which is typed as number/true/false/null when value_types is infer. '
                    'group_by builds one document per group. A path used as both a value and a container is an error.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'name_field': {'type': 'string', 'default': 'JSON_Name'},
            'value_field': {'type': 'string', 'default': 'JSON_ValueString'},
            'group_by': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}, 'default': []},
            'value_types': {'enum': ['infer', 'string'], 'default': 'infer'},
            'pretty': {'type': 'boolean', 'default': False}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['name_field'], cfg['value_field'], *cfg['group_by']])
        return {'output': Schema([*[schema.field(g) for g in cfg['group_by']], Field('JSON', FieldType.V_WSTRING, TEXT)])}

    @staticmethod
    def _typed(text, mode):
        """Strict JSON literals only: numbers (no leading zeros, so 007 stays text), true, false, null."""
        if text is None or mode == 'string':
            return text
        if text in ('true', 'false'):
            return text == 'true'
        if text == 'null':
            return None
        if _JSON_NUMBER.fullmatch(text):
            return json.loads(text)
        return text

    @staticmethod
    def _arrays(node):
        """Turn objects whose keys are exactly 0..n-1 into lists, recursively."""
        if isinstance(node, dict):
            node = {k: JSONBuild._arrays(v) for k, v in node.items()}
            if node and all(k.isdigit() for k in node) and sorted(int(k) for k in node) == list(range(len(node))):
                return [node[str(i)] for i in range(len(node))]
        return node

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        names = set(table.schema.names)
        typed = [n for n in ('JSON_ValueInt', 'JSON_ValueFloat', 'JSON_ValueBool') if n in names]
        docs = {}
        for number, rec in enumerate(table.to_dicts(), start=1):
            path = rec[cfg['name_field']]
            if path is None or path == '':
                raise DataError(f'JSON Build record {number} has an empty name')
            value = next((rec[n] for n in typed if rec[n] is not None), None)
            if value is None:
                value = self._typed(rec[cfg['value_field']], cfg['value_types'])
            key = tuple(rec[g] for g in cfg['group_by'])
            node = docs.setdefault(key, {})
            parts = str(path).split('.')
            for depth, part in enumerate(parts[:-1]):
                child = node.setdefault(part, {})
                if not isinstance(child, dict):
                    raise DataError(f'JSON Build record {number}: {".".join(parts[:depth + 1])!r} is a value and a container')
                node = child
            if parts[-1] in node and isinstance(node[parts[-1]], dict):
                raise DataError(f'JSON Build record {number}: {path!r} is a container and a value')
            node[parts[-1]] = value
        rows = [(*key, json.dumps(self._arrays(doc), ensure_ascii=False, indent=2 if cfg['pretty'] else None,
                                  separators=None if cfg['pretty'] else (',', ':'))) for key, doc in docs.items()]
        return {'output': Table.from_rows(schema, rows)}


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


class DynamicReplace(Tool):
    """Apply ordered, data-driven replacement rules to selected columns."""
    manifest = ToolManifest(
        tool_id='core.dynamic_replace', version='1.0.0', display_name='Dynamic Replace',
        catalog_ids=('developer:dynamic-replace',),
        inputs=(Anchor('data', 'D'), Anchor('replacements', 'R')),
        outputs=(Anchor('output', 'O'), Anchor('summary', 'S', required=False)),
        description='Apply replacement rules in input order. Each rule names a data field, a Boolean expression evaluated against the data row, and a static or formula replacement value. First matching rule wins.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'field_name_field': {'type': 'string', 'default': 'Field'},
            'condition_field': {'type': 'string', 'default': 'Condition'},
            'value_field': {'type': 'string', 'default': 'Value'},
            'value_is_expression': {'type': 'boolean', 'default': False},
            'type_mode': {'enum': ['keep_original', 'auto', 'explicit'], 'default': 'keep_original'},
            'output_type': {'enum': TYPE_ENUM},
            'output_size': {'type': 'integer', 'minimum': 1, 'maximum': TEXT},
            'output_scale': {'type': 'integer', 'minimum': 0, 'maximum': 49},
            'unmatched_behavior': {'enum': ['allow', 'warn', 'error'], 'default': 'error'},
        }})

    def check_config(self, cfg):
        if cfg['type_mode'] == 'explicit' and not cfg.get('output_type'):
            raise ConfigError('explicit type_mode requires output_type')
        if cfg['type_mode'] == 'explicit' and cfg['output_type'] == 'fixed_decimal':
            if cfg.get('output_size') is None or cfg.get('output_scale') is None or cfg['output_scale'] >= cfg['output_size']:
                raise ConfigError('explicit fixed_decimal requires output_size > output_scale')

    def infer(self, ctx, cfg, inputs):
        # The replacement input is row-driven, so affected columns are not known at plan time.
        return None

    def run(self, ctx, cfg, inputs):
        data, rules = inputs['data'], inputs['replacements']
        names = (cfg['field_name_field'], cfg['condition_field'], cfg['value_field'])
        require_fields(rules.schema, names)
        fi, ci, vi = (rules.schema.index(n) for n in names)
        parsed = []
        selected = set()
        for rule_no, rule in enumerate(rules.rows(), 1):
            field_name, condition, value = rule[fi], rule[ci], rule[vi]
            if field_name is None or str(field_name) not in data.schema.names:
                msg = f'Replacement rule {rule_no} refers to an unknown data field'
                if cfg['unmatched_behavior'] == 'error':
                    raise ConfigError(msg)
                if cfg['unmatched_behavior'] == 'warn':
                    ctx.warn(msg)
                continue
            if condition is None or not str(condition).strip():
                raise ConfigError(f'Replacement rule {rule_no} has an empty condition')
            try:
                predicate = compile_expression(str(condition), data.schema)
                replacement = compile_expression(str(value), data.schema) if cfg['value_is_expression'] and value is not None else None
            except Exception as e:
                raise ConfigError(f'Replacement rule {rule_no} has an invalid expression: {e}') from None
            parsed.append((str(field_name), predicate, value, replacement))
            selected.add(str(field_name))

        original = list(data.schema)
        row_cache = list(data.rows())
        ec = EvalContext(now=ctx.clock)
        columns = [list(data.column(f.name)) for f in data.schema]
        hit_counts = {}
        matched_rows = {name: set() for name in selected}
        truncations = {}
        for field_name, predicate, static_value, replacement in parsed:
            col_i = data.schema.index(field_name)
            for row_i, row in enumerate(row_cache):
                ctx.check_cancelled()
                if row_i not in matched_rows[field_name] and predicate.evaluate(row, ec):
                    raw = replacement.evaluate(row, ec) if replacement else static_value
                    try:
                        converted, warning = cast_value(raw, original[col_i])
                        columns[col_i][row_i] = converted
                        if warning:
                            truncations[field_name] = truncations.get(field_name, 0) + 1
                    except (CastFailure, ValueError, TypeError, OverflowError):
                        raise DataError(f'Replacement rule for field {field_name!r} produced a value incompatible with its type') from None
                    hit_counts[(field_name, str(raw) if raw is not None else None)] = hit_counts.get((field_name, str(raw) if raw is not None else None), 0) + 1
                    matched_rows[field_name].add(row_i)
        for field_name, count in truncations.items():
            ctx.warn(f'{count} replacement value(s) truncated to the existing field size',
                     field=field_name, count=count)
        output_fields = original
        if cfg['type_mode'] == 'explicit':
            target_type = FieldType(cfg['output_type'])
            target_size = cfg.get('output_size') if target_type.is_string or target_type is FieldType.FIXED_DECIMAL else None
            target_scale = cfg.get('output_scale') if target_type is FieldType.FIXED_DECIMAL else None
            output_fields = [Field(f.name, target_type, target_size, target_scale, f.description)
                             if f.name in selected else f for f in original]
            for col_i, f in enumerate(output_fields):
                if f.name in selected:
                    try:
                        columns[col_i] = cast_column(columns[col_i], f, ctx)
                    except (CastFailure, ValueError, TypeError, OverflowError):
                        raise DataError(f'Result values for field {f.name!r} cannot be converted to {f.type.value}') from None
        elif cfg['type_mode'] == 'auto':
            # Infer from the complete post-replacement column and widen conservatively.
            from ..types import infer_type, unify_fields
            output_fields = list(original)
            for col_i, f in enumerate(original):
                if f.name not in selected:
                    continue
                kinds = [infer_type(v) for v in columns[col_i] if v is not None]
                if not kinds:
                    continue
                target = Field(f.name, kinds[0])
                if target.type.is_string:
                    target = Field(f.name, target.type, max(1, max((len(format_value(v)) for v in columns[col_i] if v is not None), default=1)))
                try:
                    for kind in kinds[1:]:
                        target = unify_fields(target, Field(f.name, kind))
                    columns[col_i] = [cast_value(v, target)[0] for v in columns[col_i]]
                except (SchemaError, CastFailure, ValueError, TypeError, OverflowError):
                    # A mixed column that cannot be represented exactly remains text.
                    target = Field(f.name, FieldType.V_WSTRING, max(1, max((len(format_value(v)) for v in columns[col_i] if v is not None), default=1)))
                    columns[col_i] = [None if v is None else format_value(v) for v in columns[col_i]]
                output_fields[col_i] = target
        out = Table(Schema(output_fields), columns)
        summary_schema = Schema([Field('Field', FieldType.V_WSTRING, TEXT), Field('ReplacementValue', FieldType.V_WSTRING, TEXT), Field('Count', FieldType.INT64)])
        summary_rows = [(field, value, count) for (field, value), count in sorted(hit_counts.items(), key=lambda x: (x[0][0], x[0][1] or ''))]
        return {'output': out, 'summary': Table.from_rows(summary_schema, summary_rows)}


# =========================================================================== Blob Convert / Base64

class BlobInput(Tool):
    """Read binary files as typed BLOB records, optionally in bounded chunks."""

    manifest = ToolManifest(
        tool_id='core.blob_input', version='1.0.0', display_name='Blob Input',
        catalog_ids=('developer:blob-input',),
        inputs=(Anchor('input', 'Input', required=False),), outputs=(Anchor('output', 'Output'),),
        side_effects='reads-files', deterministic=True,
        description='Read one binary file, or read a file path from each input row. Optional chunking emits bounded binary rows.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['mode'], 'properties': {
            'mode': {'enum': ['file', 'pass_through'], 'default': 'file'},
            'path': {'type': ['string', 'null'], 'default': None},
            'filename_field': {'type': ['string', 'null'], 'default': None},
            'blob_field': {'type': 'string', 'minLength': 1, 'default': 'Blob'},
            'split_blobs': {'type': 'boolean', 'default': False},
            'chunk_size_kb': {'type': 'integer', 'minimum': 1, 'maximum': 102400, 'default': 1024},
            'max_blob_size_mb': {'type': 'integer', 'minimum': 1, 'maximum': 4096, 'default': 512},
        }})

    def check_config(self, cfg):
        if cfg['mode'] == 'file' and not cfg.get('path'):
            raise ConfigError('Blob Input file mode requires path')
        if cfg['mode'] == 'pass_through' and not cfg.get('filename_field'):
            raise ConfigError('Blob Input pass-through mode requires filename_field')

    def infer(self, ctx, cfg, inputs):
        from ..types import Schema
        source = inputs.get('input') if cfg['mode'] == 'pass_through' else None
        if cfg['mode'] == 'pass_through' and source is None:
            return None
        fields = list(source) if source is not None else []
        names = {f.name for f in fields}
        if cfg['blob_field'] in names:
            return None
        fields.append(Field(cfg['blob_field'], FieldType.BLOB))
        if cfg['split_blobs']:
            if 'BlobNumber' in {f.name for f in fields}:
                return None
            fields.append(Field('BlobNumber', FieldType.INT64))
        return {'output': Schema(fields)}

    def run(self, ctx, cfg, inputs):
        upstream = inputs.get('input') if cfg['mode'] == 'pass_through' else None
        if cfg['mode'] == 'pass_through' and upstream is None:
            raise ConfigError('Blob Input pass-through mode requires an incoming file list')
        if upstream is not None:
            require_fields(upstream.schema, [cfg['filename_field']])
            if cfg['blob_field'] in upstream.schema.names or (cfg['split_blobs'] and 'BlobNumber' in upstream.schema.names):
                raise ConfigError('Blob Input output field collides with an incoming field')
            fields = list(upstream.schema)
        else:
            fields = []
        fields.append(Field(cfg['blob_field'], FieldType.BLOB))
        if cfg['split_blobs']:
            fields.append(Field('BlobNumber', FieldType.INT64))
        max_bytes = cfg['max_blob_size_mb'] * 1024 * 1024
        chunk_bytes = cfg['chunk_size_kb'] * 1024
        rows = []
        inputs_iter = upstream.rows() if upstream is not None else [()]
        for input_row in inputs_iter:
            ctx.check_cancelled()
            raw_path = input_row[upstream.schema.index(cfg['filename_field'])] if upstream is not None else cfg['path']
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise DataError('Blob Input received an empty or non-text file path')
            path = ctx.resolve_path(raw_path)
            try:
                if not path.is_file():
                    raise FileNotFoundError
                if cfg['split_blobs']:
                    with path.open('rb') as stream:
                        index = 0
                        while True:
                            ctx.check_cancelled()
                            blob = stream.read(chunk_bytes)
                            if not blob:
                                break
                            index += 1
                            prefix = tuple(input_row)
                            rows.append(prefix + (blob, index))
                            if len(rows) > 1_000_000:
                                raise DataError('Blob Input exceeded the one-million-chunk execution safety limit')
                        if index == 0:
                            rows.append(tuple(input_row) + (b'', 1))
                else:
                    if path.stat().st_size > max_bytes:
                        raise DataError(f'Blob Input file exceeds configured maximum ({cfg["max_blob_size_mb"]} MB)')
                    rows.append(tuple(input_row) + (path.read_bytes(),))
            except FileNotFoundError:
                raise DataError('Blob Input cannot access a configured file') from None
        ctx.info(f'read {len(rows)} blob record(s)')
        return {'output': Table.from_rows(fields, rows)}


class BlobOutput(Tool):
    """Write each BLOB row to its own file using safe filename-only substitutions."""

    manifest = ToolManifest(
        tool_id='core.blob_output', version='1.0.0', display_name='Blob Output',
        catalog_ids=('developer:blob-output',), inputs=(Anchor('input', 'Input'),), outputs=(),
        side_effects='writes-files', deterministic=True,
        description='Write each binary field value to its own file. Dynamic filename operations affect only the filename component.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path', 'blob_field'], 'properties': {
            'path': {'type': 'string', 'minLength': 1}, 'blob_field': {'type': 'string', 'minLength': 1},
            'modify_method': {'enum': ['none', 'append', 'prepend', 'replace_name'], 'default': 'none'},
            'filename_field': {'type': ['string', 'null'], 'default': None},
            'if_exists': {'enum': ['error', 'overwrite'], 'default': 'error'},
        }})

    def check_config(self, cfg):
        if cfg['modify_method'] != 'none' and not cfg.get('filename_field'):
            raise ConfigError('Blob Output filename_field is required for dynamic filename modification')

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        if not ctx.limits.get('allow_file_writes'):
            raise EngineError('Blob Output writes files and is disabled for this run. Allow file writes explicitly.',
                              code='permission-denied', node_id=ctx.node_id)
        table: Table = inputs['input']
        fields = [cfg['blob_field']] + ([cfg['filename_field']] if cfg['modify_method'] != 'none' else [])
        require_fields(table.schema, fields)
        base = ctx.resolve_path(cfg['path'])
        written = []
        for row_number, record in enumerate(table.rows(), 1):
            ctx.check_cancelled()
            blob = record[table.schema.index(cfg['blob_field'])]
            if blob is None:
                raise DataError(f'Blob Output row {row_number} has a null binary value')
            if not isinstance(blob, (bytes, bytearray, memoryview)):
                raise DataError(f'Blob Output field {cfg["blob_field"]!r} is not binary')
            destination = base
            if cfg['modify_method'] != 'none':
                value = record[table.schema.index(cfg['filename_field'])]
                if not isinstance(value, str) or not value.strip() or value in ('.', '..') or '/' in value or '\\' in value or ':' in value:
                    raise DataError(f'Blob Output row {row_number} has an unsafe filename value')
                stem, suffix = base.stem, base.suffix
                if cfg['modify_method'] == 'append': stem += value
                elif cfg['modify_method'] == 'prepend': stem = value + stem
                else: stem = value
                destination = base.with_name(stem + suffix)
            if destination in written:
                raise DataError(f'Blob Output rows resolve to the same destination at row {row_number}')
            if destination.exists() and cfg['if_exists'] == 'error':
                raise DataError(f'Blob Output destination already exists for row {row_number}')
            written.append(destination)
        staged = []
        backups = []
        installed = []
        remove_backups = True
        try:
            for row_number, (record, destination) in enumerate(zip(table.rows(), written), 1):
                ctx.check_cancelled()
                destination.parent.mkdir(parents=True, exist_ok=True)
                data = record[table.schema.index(cfg['blob_field'])]
                handle, temp_path = tempfile.mkstemp(prefix='.flowbench-blob-', dir=destination.parent)
                try:
                    with os.fdopen(handle, 'wb') as stream:
                        stream.write(bytes(data))
                        stream.flush()
                        os.fsync(stream.fileno())
                    staged.append((Path(temp_path), destination))
                finally:
                    if os.path.exists(temp_path) and all(Path(temp_path) != item[0] for item in staged):
                        os.unlink(temp_path)
            for temp_path, destination in staged:
                ctx.check_cancelled()
                if destination.exists():
                    if cfg['if_exists'] == 'error':
                        raise DataError(f'Blob Output destination appeared during write at row {len(installed) + 1}')
                    handle, backup_name = tempfile.mkstemp(prefix='.flowbench-blob-backup-', dir=destination.parent)
                    os.close(handle)
                    os.unlink(backup_name)
                    backup = Path(backup_name)
                    os.replace(destination, backup)
                    backups.append((backup, destination))
                os.replace(temp_path, destination)
                installed.append(destination)
        except BaseException:
            for path in installed:
                try: path.unlink(missing_ok=True)
                except OSError: pass
            for backup, destination in backups:
                try: os.replace(backup, destination)
                except OSError: remove_backups = False
            raise
        finally:
            for temp_path, _ in staged:
                try: temp_path.unlink(missing_ok=True)
                except OSError: pass
            if remove_backups:
                for backup, _ in backups:
                    try: backup.unlink(missing_ok=True)
                    except OSError: pass
        ctx.info(f'wrote {len(installed)} blob file(s)')
        return {}

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


CONTROL_LOG_SCHEMA = Schema([Field('Container', FieldType.V_WSTRING), Field('NodeID', FieldType.V_WSTRING),
                             Field('Tool', FieldType.V_WSTRING), Field('Event', FieldType.V_WSTRING),
                             Field('Message', FieldType.V_WSTRING), Field('Records', FieldType.INT64)])


class ControlContainer(Tool):
    """Native execution gate for a group of nodes; the executor owns the scheduling.

    Children wait for the optional control input to finish and run only if it
    delivered at least one record. The log lists child events once they finish;
    an inactive container emits no log records, so chained containers stop too.
    """
    container = True
    manifest = ToolManifest(
        tool_id='core.control_container', version='1.0.0', display_name='Control Container',
        catalog_ids=('developer:control-container',),
        inputs=(Anchor('control', 'Control', required=False),), outputs=(Anchor('log', 'Log'),),
        description='Run the contained nodes only after the optional Control input completes with at least one '
                    'record (always, when unconnected). Log lists contained node events; it is empty when the '
                    'container did not run. Disabling the container skips all contained nodes.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['children'], 'properties': {
            'caption': {'type': 'string', 'maxLength': 200, 'default': 'Control Container'},
            'children': {'type': 'array', 'uniqueItems': True, 'maxItems': 10000,
                         'items': {'type': 'string', 'minLength': 1}}}})

    def infer(self, ctx, config, inputs):
        return {'log': CONTROL_LOG_SCHEMA}

    def run(self, ctx, cfg, inputs):
        rows = [(ctx.node_id, *entry) for entry in (ctx.container_log or [])]
        return {'log': Table.from_rows(CONTROL_LOG_SCHEMA, rows)}


RUNNER_LOG_SCHEMA = Schema([Field('Workflow', FieldType.V_WSTRING), Field('Status', FieldType.V_WSTRING),
                            Field('Sequence', FieldType.INT64), Field('Event', FieldType.V_WSTRING),
                            Field('NodeID', FieldType.V_WSTRING), Field('Message', FieldType.V_WSTRING)])
RUNNER_EVENTS = ('node-completed', 'node-skipped', 'node-failed', 'warning', 'info', 'run-completed', 'run-cancelled')


class Runner(Tool):
    """Run the workflow file named by each record, in order, and route its log by outcome.

    Child runs inherit this run's permission flags and cancellation; a failed child
    is reported on Failure and does not fail the parent.
    """
    manifest = ToolManifest(
        tool_id='core.runner', version='1.0.0', display_name='Runner', catalog_ids=('developer:runner',),
        inputs=(Anchor('input', 'Input', description='Records whose path field names the workflows to run'),),
        outputs=(Anchor('success', 'Success', description='Event log of workflows that succeeded'),
                 Anchor('failure', 'Failure', description='Event log of workflows that failed, were invalid or were not run')),
        side_effects='process', deterministic=True,
        description='For each input record, run the Flowbench or Alteryx workflow file named in path_field, one '
                    'at a time and in record order, with the same permissions as this run. Logs go to Success or '
                    'Failure. on_failure stop skips the remaining workflows. Requires permission to run processes.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path_field'], 'properties': {
            'path_field': {'type': 'string', 'minLength': 1},
            'on_failure': {'enum': ['continue', 'stop'], 'default': 'continue'},
            'max_depth': {'type': 'integer', 'minimum': 1, 'maximum': 64, 'default': 8}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        if cfg['path_field'] not in schema.names or not schema.field(cfg['path_field']).type.is_string:
            raise SchemaError(f"Runner path field {cfg['path_field']!r} must be a text field in the input")
        return {'success': RUNNER_LOG_SCHEMA, 'failure': RUNNER_LOG_SCHEMA}

    def _run_one(self, ctx, cfg, path):
        """Return (status, [(event, node_id, message)]) for one child workflow."""
        from ..errors import CancelledError
        from ..executor import run_workflow, workflow_digest
        from .macros import WorkflowMacro
        try:
            doc, child_base = WorkflowMacro()._definition(ctx, {'definition_path': path})
        except EngineError as e:
            return 'invalid', [('load-failed', None, e.message)]
        identity = workflow_digest(doc)
        if identity in ctx.macro_stack:
            return 'invalid', [('recursion', None, 'Workflow is already running in this call stack')]
        if len(ctx.macro_stack) >= cfg['max_depth']:
            return 'invalid', [('depth-limit', None, f"Nested runs exceed max_depth {cfg['max_depth']}")]
        try:
            result = run_workflow(doc, base_dir=child_base, clock=ctx.clock, cancel_token=ctx.cancel_token,
                                  limits=ctx.limits, profile=ctx.profile.name if ctx.profile else None,
                                  retain='none', _macro_stack=(*ctx.macro_stack, identity))
        except CancelledError:
            raise
        except EngineError as e:
            problems = getattr(e, 'problems', None) or [{'node_id': e.node_id, 'message': e.message}]
            return 'invalid', [('validation-failed', p.get('node_id'), p.get('message')) for p in problems]
        if result.status == 'cancelled':
            raise CancelledError('Run cancelled while running a child workflow', node_id=ctx.node_id)
        return result.status, [(ev['type'], ev.get('node_id'), ev.get('message') or ev.get('status'))
                               for ev in result.events if ev['type'] in RUNNER_EVENTS]

    def run(self, ctx, cfg, inputs):
        _require_processes(ctx, 'Runner')
        source: Table = inputs['input']
        self.infer(ctx, cfg, {'input': source.schema})
        rows = {'success': [], 'failure': []}
        stopped = False
        for number, path in enumerate(source.column(cfg['path_field']), start=1):
            ctx.check_cancelled()
            if stopped:
                rows['failure'].append((path, 'not-run', 1, 'not-run', None, 'An earlier workflow failed'))
                continue
            if path is None or not str(path).strip():
                status, events = 'invalid', [('load-failed', None, f'Record {number} has an empty path')]
            else:
                status, events = self._run_one(ctx, cfg, str(path))
            ctx.info(f'Runner: {path} {status}')
            target = 'success' if status == 'succeeded' else 'failure'
            rows[target].extend((path, status, i, event, node, message)
                                for i, (event, node, message) in enumerate(events, start=1))
            stopped = target == 'failure' and cfg['on_failure'] == 'stop'
        return {k: Table.from_rows(RUNNER_LOG_SCHEMA, v) for k, v in rows.items()}


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


TOOLS = [FieldInfo, JSONSchemaTool, JSONParse, JSONBuild, DynamicRename, DynamicSelect, DynamicReplace, BlobInput, BlobOutput, BlobConvert, Base64Encoder, Message, TestTool,
         BlockUntilDone, ControlContainer, Runner, Detour, DetourEnd, ExpectEquals, Throttle, PythonTool, RTool, RunCommand]
