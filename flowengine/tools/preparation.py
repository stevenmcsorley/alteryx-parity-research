"""Preparation category kernels."""
from __future__ import annotations

import functools
import re
import unicodedata
from decimal import Decimal

from ..errors import ConfigError, DataError, ExpressionError, SchemaError
from ..formula import EvalContext, compile_expression, field_type_for_kind, truthy
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import CastFailure, Field, FieldType, Schema, cast_value
from ._common import FIELD_SPEC, ON_ERROR, TYPE_ENUM, cast_column, field_from_spec, require_fields


def _eval_ctx(ctx):
    return EvalContext(now=ctx.clock, warn=lambda m: ctx.warn(m))


def _compile(ctx, expr, schema, **kw):
    try:
        return compile_expression(expr, schema, **kw)
    except ExpressionError as e:
        raise ExpressionError(e.message, node_id=ctx.node_id) from None


# =========================================================================== Filter

_BASIC_OPS = ['=', '!=', '<', '<=', '>', '>=', 'is_null', 'is_not_null', 'is_empty', 'is_not_empty',
              'contains', 'does_not_contain', 'is_true', 'is_false']


def _quote(v):
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    if isinstance(v, (int, float)):
        return repr(v)
    return '"' + str(v).replace('"', '""') + '"'


def basic_filter_expression(field: str, op: str, value) -> str:
    """Translate a basic (field/operator/value) filter into the expression language."""
    f = f'[{field}]'
    if op in ('=', '!=', '<', '<=', '>', '>='):
        if value is None:
            raise ConfigError(f'Operator {op!r} requires a value')
        return f'{f} {op} {_quote(value)}'
    return {'is_null': f'IsNull({f})', 'is_not_null': f'!IsNull({f})', 'is_empty': f'IsEmpty({f})',
            'is_not_empty': f'!IsEmpty({f})', 'contains': f'Contains({f}, {_quote(value)})',
            'does_not_contain': f'!Contains({f}, {_quote(value)})', 'is_true': f'{f} = TRUE',
            'is_false': f'{f} = FALSE'}[op]


class Filter(Tool):
    streaming = 'row-local'
    manifest = ToolManifest(
        tool_id='core.filter', version='1.0.0', display_name='Filter',
        catalog_ids=('preparation:filter',),
        inputs=(Anchor('input', 'I'),),
        outputs=(Anchor('true', 'T', description='Records meeting the condition'),
                 Anchor('false', 'F', description='Records not meeting the condition (null counts as not met)')),
        description='Split records by a basic comparison or a boolean expression.',
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'oneOf': [{'required': ['expression']}, {'required': ['field', 'operator']}],
            'properties': {
                'expression': {'type': 'string', 'minLength': 1},
                'field': {'type': 'string'},
                'operator': {'enum': _BASIC_OPS},
                'value': {'type': ['string', 'number', 'boolean', 'null']},
            },
        })

    def check_config(self, cfg):
        if 'field' in cfg:
            cfg['expression'] = basic_filter_expression(cfg['field'], cfg['operator'], cfg.get('value'))

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        expr = _compile(ctx, cfg['expression'], t.schema)
        if expr.kind not in ('bool', 'number', 'null', 'any'):
            raise ExpressionError(f'Filter expression must be boolean, got {expr.kind}', node_id=ctx.node_id)
        ec = _eval_ctx(ctx)
        yes, no = [], []
        for i, row in enumerate(t.rows()):
            (yes if truthy(expr.evaluate(row, ec)) else no).append(i)
        return {'true': t.take(yes), 'false': t.take(no)}


# =========================================================================== Formula

class Formula(Tool):
    streaming = 'row-local'
    manifest = ToolManifest(
        tool_id='core.formula', version='1.0.0', display_name='Formula',
        catalog_ids=('preparation:formula',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Create or update fields with expressions, evaluated in order (later formulas see earlier results).',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['formulas'],
            'properties': {
                'formulas': {'type': 'array', 'minItems': 1, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['field', 'expression'],
                    'properties': {
                        'field': {'type': 'string', 'minLength': 1},
                        'expression': {'type': 'string', 'minLength': 1},
                        'type': {'enum': TYPE_ENUM}, 'size': {'type': 'integer', 'minimum': 1},
                        'scale': {'type': 'integer', 'minimum': 0},
                    }}},
                'on_conversion_error': ON_ERROR,
            },
        })

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        ec = _eval_ctx(ctx)
        for spec in cfg['formulas']:
            expr = _compile(ctx, spec['expression'], t.schema)
            if spec['field'] in t.schema and 'type' not in spec:
                target = t.schema.field(spec['field'])  # existing field keeps its type
            elif 'type' in spec:
                target = field_from_spec({'name': spec['field'], **{k: spec[k] for k in ('type', 'size', 'scale') if k in spec}})
            else:
                target = Field(spec['field'], field_type_for_kind(expr.kind),
                               (2**30 - 1) if expr.kind in ('string', 'any', 'null') else None)
            values = []
            for i, row in enumerate(t.rows()):
                try:
                    values.append(expr.evaluate(row, ec))
                except ExpressionError as e:
                    raise ExpressionError(f'Formula for {spec["field"]!r}, record {i + 1}: {e.message}',
                                          node_id=ctx.node_id) from None
                if i % 50000 == 0:
                    ctx.check_cancelled()
            t = t.with_column(target, cast_column(values, target, ctx, cfg['on_conversion_error']))
        return {'output': t}


# =========================================================================== Select

class Select(Tool):
    streaming = 'row-local'
    manifest = ToolManifest(
        tool_id='core.select', version='1.0.0', display_name='Select',
        catalog_ids=('preparation:select',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Include/exclude, rename, retype and reorder fields.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['fields'],
            'properties': {
                'fields': {'type': 'array', 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['field'],
                    'properties': {
                        'field': {'type': 'string', 'minLength': 1},
                        'selected': {'type': 'boolean', 'default': True},
                        'rename': {'type': 'string', 'minLength': 1},
                        'type': {'enum': TYPE_ENUM}, 'size': {'type': 'integer', 'minimum': 1},
                        'scale': {'type': 'integer', 'minimum': 0},
                    }}},
                'unknown_fields': {'enum': ['include', 'exclude'], 'default': 'include',
                                   'description': 'Fields not listed (e.g. new upstream fields) are appended or dropped'},
                'missing_fields': {'enum': ['error', 'ignore'], 'default': 'error'},
                'order': {'enum': ['listed', 'input'], 'default': 'listed',
                          'description': 'listed = output in the order configured; input = keep incoming field order'},
                'on_conversion_error': ON_ERROR,
            },
        })

    def check_config(self, cfg):
        names = [f['field'] for f in cfg['fields']]
        if len(names) != len(set(names)):
            raise ConfigError('A field is listed more than once')

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        listed = [f for f in cfg['fields']]
        missing = [f['field'] for f in listed if f['field'] not in t.schema]
        if missing and cfg['missing_fields'] == 'error':
            raise SchemaError(f'Configured fields not present in input: {missing}')
        if missing:
            ctx.warn(f'Ignoring configured fields not present in input: {missing}')
        listed = [f for f in listed if f['field'] in t.schema]
        if cfg['unknown_fields'] == 'include':
            known = {f['field'] for f in listed}
            listed += [{'field': n, 'selected': True} for n in t.schema.names if n not in known]
        if cfg['order'] == 'input':  # [ref:holdout Select sample, OrderChanged=False]
            pos = {n: i for i, n in enumerate(t.schema.names)}
            listed.sort(key=lambda spec: pos[spec['field']])
        fields, cols = [], []
        for spec in listed:
            if not spec.get('selected', True):
                continue
            src = t.schema.field(spec['field'])
            col = t.column(spec['field'])
            name = spec.get('rename', src.name)
            if 'type' in spec:
                target = field_from_spec({'name': name, 'type': spec['type'],
                                          **{k: spec[k] for k in ('size', 'scale') if k in spec}})
                col = cast_column(col, target, ctx, cfg['on_conversion_error'])
            elif 'size' in spec or 'scale' in spec:
                target = Field(name, src.type, spec.get('size', src.size), spec.get('scale', src.scale))
                col = cast_column(col, target, ctx, cfg['on_conversion_error'])
            else:
                target = src.renamed(name)
            fields.append(target)
            cols.append(col)
        return {'output': Table(Schema(fields), cols)}


# =========================================================================== Sort

def sort_key_fn(schema, keys, case_insensitive=False):
    """Build a comparison for (row-index, row) pairs: nulls first ascending, stable."""
    idx = [(schema.index(k['field']), k.get('order', 'asc') == 'desc') for k in keys]

    def norm(v):
        if case_insensitive and isinstance(v, str):
            return v.casefold()
        return v

    def cmp(a, b):
        for i, desc in idx:
            x, y = norm(a[i]), norm(b[i])
            if x == y:
                continue
            if x is None:
                r = -1
            elif y is None:
                r = 1
            else:
                try:
                    r = -1 if x < y else 1
                except TypeError:
                    raise DataError(f'Cannot order values of different types in {schema[i].name!r}') from None
            return -r if desc else r
        return 0
    return functools.cmp_to_key(cmp)


class Sort(Tool):
    manifest = ToolManifest(
        tool_id='core.sort', version='1.0.0', display_name='Sort',
        catalog_ids=('preparation:sort',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Stable multi-key sort. Nulls sort before all values ascending (after, descending).',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['keys'],
            'properties': {
                'keys': {'type': 'array', 'minItems': 1, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['field'],
                    'properties': {'field': {'type': 'string'}, 'order': {'enum': ['asc', 'desc'], 'default': 'asc'}}}},
                'case_insensitive': {'type': 'boolean', 'default': False},
                'collation': {'enum': ['ordinal', 'locale'], 'default': 'ordinal',
                              'description': 'ordinal = code-point order; locale = culture-aware order '
                                             '(source documents with a non-zero sort locale)'},
            },
        })

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [k['field'] for k in cfg['keys']])
        rows = list(t.rows())
        if cfg['collation'] == 'locale':
            key = locale_sort_key(t.schema, cfg['keys'])
            ctx.info('Culture-aware collation is the platform collation (Windows: en-US, digits as numbers); '
                     'it is known not to match the reference order exactly (probe-sort-locale-1033)')
        else:
            key = sort_key_fn(t.schema, cfg['keys'], cfg['case_insensitive'])
        order = sorted(range(len(rows)), key=lambda i: key(rows[i]))
        return {'output': t.take(order)}


def _culture_compare():
    """Platform culture-aware string comparison (Windows CompareStringEx, en-US, digits as numbers);
    elsewhere a case-insensitive natural-number approximation. Engine policy, not reference-equal."""
    import os
    import re as _re
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32')
        f = k.CompareStringEx
        f.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int,
                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        return lambda a, b: f('en-US', 0x8, a, len(a), b, len(b), None, None, None) - 2

    def key(s):
        return [(0, int(p)) if p.isdigit() else (1, p.casefold()) for p in _re.split(r'(\d+)', s) if p]
    return lambda a, b: (key(a) > key(b)) - (key(a) < key(b))


def locale_sort_key(schema, keys):
    cmp_text = _culture_compare()
    idx = [(schema.index(k['field']), k.get('order', 'asc') == 'desc') for k in keys]

    def cmp(a, b):
        for i, desc in idx:
            x, y = a[i], b[i]
            if x == y:
                continue
            if x is None:
                r = -1
            elif y is None:
                r = 1
            elif isinstance(x, str) and isinstance(y, str):
                r = cmp_text(x, y)
                if r == 0:
                    continue
            else:
                r = -1 if x < y else 1
            return -r if desc else r
        return 0
    return functools.cmp_to_key(cmp)


# =========================================================================== Record ID

class RecordID(Tool):
    streaming = 'stateful'


    def open_stream(self, ctx, cfg):
        tool = self

        class _State:
            def __init__(self):
                self.counters = {}

            def process(self, batch):
                return tool._number(ctx, cfg, batch, self.counters)
        return _State()

    manifest = ToolManifest(
        tool_id='core.record_id', version='1.0.0', display_name='Record ID',
        catalog_ids=('preparation:record-id',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Add a sequential identifier field.',
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'field_name': {'type': 'string', 'minLength': 1, 'default': 'RecordID'},
                'start': {'type': 'integer', 'default': 1},
                'type': {'enum': ['int16', 'int32', 'int64', 'v_string'], 'default': 'int32'},
                'size': {'type': 'integer', 'minimum': 1, 'default': 6,
                         'description': 'Zero-padded width when type is v_string'},
                'position': {'enum': ['first', 'last'], 'default': 'first'},
                'group_by': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            },
        })

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        if cfg['field_name'] in t.schema:
            raise SchemaError(f"Field {cfg['field_name']!r} already exists")
        return self._number(ctx, cfg, t, {})

    def _number(self, ctx, cfg, t, counters):
        if cfg['field_name'] in t.schema:
            raise SchemaError(f"Field {cfg['field_name']!r} already exists")
        require_fields(t.schema, cfg['group_by'])
        gi = [t.schema.index(g) for g in cfg['group_by']]
        ids = []
        for row in t.rows():
            k = tuple(row[i] for i in gi)
            n = counters.get(k, cfg['start'])
            counters[k] = n + 1
            ids.append(n)
        ftype = FieldType(cfg['type'])
        if ftype is FieldType.V_STRING:
            field = Field(cfg['field_name'], ftype, max(cfg['size'], 1))
            ids = [str(n).zfill(cfg['size']) for n in ids]
        else:
            field = Field(cfg['field_name'], ftype)
        ids = cast_column(ids, field, ctx, 'error')
        out = t.with_column(field, ids, 0 if cfg['position'] == 'first' else None)
        return {'output': out}


# =========================================================================== Sample

class Sample(Tool):
    manifest = ToolManifest(
        tool_id='core.sample', version='1.0.0', display_name='Sample',
        catalog_ids=('preparation:sample',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Deterministic positional sampling (first/last/skip N, every Nth, first N percent), optionally per group.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['mode', 'n'],
            'properties': {
                'mode': {'enum': ['first', 'last', 'skip', 'every_nth', 'first_percent']},
                'n': {'type': 'integer', 'minimum': 0},
                'group_by': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            },
        })

    def check_config(self, cfg):
        if cfg['mode'] == 'every_nth' and cfg['n'] < 1:
            raise ConfigError('every_nth requires n >= 1')
        if cfg['mode'] == 'first_percent' and cfg['n'] > 100:
            raise ConfigError('first_percent requires 0 <= n <= 100')

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, cfg['group_by'])
        gi = [t.schema.index(g) for g in cfg['group_by']]
        groups: dict = {}
        for i, row in enumerate(t.rows()):
            groups.setdefault(tuple(row[j] for j in gi), []).append(i)
        n, mode = cfg['n'], cfg['mode']
        keep = []
        key_fn = sort_key_fn(Schema([t.schema[j] for j in gi]), [{'field': t.schema[j].name} for j in gi])
        key_of = [key_fn(k) for k in groups]
        for members in groups.values():
            if mode == 'first':
                sel = members[:n]
            elif mode == 'last':
                sel = members[-n:] if n else []
            elif mode == 'skip':
                sel = members[n:]
            elif mode == 'every_nth':
                sel = members[::n]  # first record of each block of n [ref:sample-every-nth]
            else:
                sel = members[:(len(members) * n) // 100]
            keep.append(sel)
        if gi:  # grouped output is ordered by group key [ref:sample-first-grouped]
            order = sorted(range(len(keep)), key=lambda g: key_of[g])
            keep = [keep[g] for g in order]
        return {'output': t.take([i for sel in keep for i in sel])}


# =========================================================================== Unique

class Unique(Tool):
    manifest = ToolManifest(
        tool_id='core.unique', version='1.0.0', display_name='Unique',
        catalog_ids=('preparation:unique',),
        inputs=(Anchor('input', 'I'),),
        outputs=(Anchor('unique', 'U', description='First record of each key'),
                 Anchor('duplicates', 'D', description='Subsequent records of each key')),
        description='Split first occurrences from duplicates on the selected key fields (input order preserved).',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['fields'],
            'properties': {
                'fields': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
                'case_insensitive': {'type': 'boolean', 'default': False},
            },
        })

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, cfg['fields'])
        idx = [t.schema.index(f) for f in cfg['fields']]
        ci = cfg['case_insensitive']
        seen, uniq, dup = set(), [], []
        rows = list(t.rows())
        for i, row in enumerate(rows):
            k = tuple((row[j].casefold() if ci and isinstance(row[j], str) else row[j]) for j in idx)
            (dup if k in seen else uniq).append(i)
            seen.add(k)
        # both outputs are ordered by key (stable, nulls first) [ref:unique-case-null, probe-unique-order]
        key = sort_key_fn(t.schema, [{'field': f} for f in cfg['fields']], ci)
        uniq.sort(key=lambda i: key(rows[i]))
        dup.sort(key=lambda i: key(rows[i]))
        return {'unique': t.take(uniq), 'duplicates': t.take(dup)}


# =========================================================================== Generate Rows

class GenerateRows(Tool):
    manifest = ToolManifest(
        tool_id='core.generate_rows', version='1.0.0', display_name='Generate Rows',
        catalog_ids=('preparation:generate-rows',),
        inputs=(Anchor('input', 'I', required=False,
                       description='Optional: a loop is run for every incoming record'),),
        outputs=(Anchor('output', 'O'),),
        description='Generate records with an initialise / condition / loop expression triple.',
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'required': ['field', 'type', 'init', 'condition', 'loop'],
            'properties': {
                'field': {'type': 'string', 'minLength': 1},
                'type': {'enum': TYPE_ENUM}, 'size': {'type': 'integer', 'minimum': 1},
                'init': {'type': 'string'}, 'condition': {'type': 'string'}, 'loop': {'type': 'string'},
                'max_rows_per_record': {'type': 'integer', 'minimum': 1, 'default': 1_000_000},
            },
        })

    def run(self, ctx, cfg, inputs):
        src: Table | None = inputs.get('input')
        base = src if src is not None else Table([], [])
        if src is None:
            base_rows = [()]
        else:
            base_rows = list(src.rows())
        target = field_from_spec({'name': cfg['field'], 'type': cfg['type'], **({'size': cfg['size']} if 'size' in cfg else {})})
        replace = cfg['field'] in base.schema
        schema_for_expr = base.schema
        kind = 'number' if target.type.is_numeric else ('string' if target.type.is_string else
                                                       {'date': 'date', 'datetime': 'datetime', 'time': 'time',
                                                        'bool': 'bool'}.get(target.type.value, 'any'))
        if replace:
            pos = base.schema.index(cfg['field'])
            out_fields = [target if f.name == cfg['field'] else f for f in base.schema]
            extra = None
        else:
            pos = len(base.schema)
            out_fields = list(base.schema) + [target]
            extra = {cfg['field']: kind}
        init = _compile(ctx, cfg['init'], schema_for_expr, extra_kinds=None)
        cond = _compile(ctx, cfg['condition'], schema_for_expr, extra_kinds=extra)
        loop = _compile(ctx, cfg['loop'], schema_for_expr, extra_kinds=extra)
        ec = _eval_ctx(ctx)
        limit = cfg['max_rows_per_record']
        out_rows = []
        if ctx.dry_run:  # schema inference only: never generate data (a source with no input would loop)
            base_rows = []
        for row in base_rows:
            value, _ = _cast(init.evaluate(row, ec), target)
            count = 0
            while True:
                probe = _with_value(row, pos, value, replace)
                if not truthy(cond.evaluate(probe, ec)):
                    break
                out_rows.append(probe)
                count += 1
                if count >= limit:
                    raise DataError(f'Generate Rows exceeded {limit} records for one input record; '
                                    f'check the condition/loop expressions (raise max_rows_per_record if intended)')
                if count % 10000 == 0:
                    ctx.check_cancelled()
                value, _ = _cast(loop.evaluate(probe, ec), target)
        return {'output': Table.from_rows(out_fields, out_rows)}


def _cast(v, field):
    try:
        return cast_value(v, field)
    except CastFailure as e:
        raise DataError(f'Generated value for {field.name!r}: {e}') from None


def _with_value(row, pos, value, replace):
    if replace:
        return row[:pos] + (value,) + row[pos + 1:]
    return row + (value,)


# =========================================================================== Multi-Row Formula

class MultiRowFormula(Tool):
    manifest = ToolManifest(
        tool_id='core.multi_row_formula', version='1.0.0', display_name='Multi-Row Formula',
        catalog_ids=('preparation:multi-row-formula',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Expression with [Row-n:Field]/[Row+n:Field] references to neighbouring records. '
                    'Updating an existing field makes earlier rows see already-computed values.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['field', 'expression'],
            'properties': {
                'field': {'type': 'string', 'minLength': 1},
                'expression': {'type': 'string', 'minLength': 1},
                'type': {'enum': TYPE_ENUM}, 'size': {'type': 'integer', 'minimum': 1},
                'nonexistent_rows': {'enum': ['null', 'zero_or_empty'], 'default': 'null'},
                'group_by': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
                'on_conversion_error': ON_ERROR,
            },
        })

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, cfg['group_by'])
        name = cfg['field']
        exists = name in t.schema
        if exists and 'type' not in cfg:
            target = t.schema.field(name)
        else:
            target = field_from_spec({'name': name, 'type': cfg.get('type', 'double'),
                                      **({'size': cfg['size']} if 'size' in cfg else {})})
        kind = 'number' if target.type.is_numeric else ('string' if target.type.is_string else 'any')
        if exists:
            work = t
            extra = None
        else:
            work = t.with_column(target, [None] * len(t))
            extra = None
        expr = _compile(ctx, cfg['expression'], work.schema, allow_row_refs=True, extra_kinds=extra)
        fi = work.schema.index(name)
        gi = [work.schema.index(g) for g in cfg['group_by']]
        rows = [list(r) for r in work.rows()]
        groups: dict = {}
        for i, r in enumerate(rows):
            groups.setdefault(tuple(r[j] for j in gi), []).append(i)
        position = {}
        for members in groups.values():
            for p, i in enumerate(members):
                position[i] = (members, p)
        default_missing = cfg['nonexistent_rows']
        ec = _eval_ctx(ctx)

        def missing(col):
            if default_missing == 'null':
                return None
            f = work.schema[col]
            return 0 if f.type.is_numeric else ('' if f.type.is_string else None)

        for i in range(len(rows)):
            members, p = position[i]

            def accessor(offset, col, members=members, p=p):
                q = p + offset
                if 0 <= q < len(members):
                    return rows[members[q]][col]
                return missing(col)
            ec.row_accessor = accessor
            v = expr.evaluate(tuple(rows[i]), ec)
            try:
                rows[i][fi], _ = cast_value(v, target)
            except CastFailure as e:
                if cfg['on_conversion_error'] == 'error':
                    raise DataError(f'Record {i + 1}: {e}') from None
                rows[i][fi] = None
                ctx.warn(f'value for {name!r} not convertible; set to null')
        out_fields = [target if f.name == name else f for f in work.schema]
        return {'output': Table.from_rows(out_fields, rows)}


# =========================================================================== Data Cleansing

class DataCleansing(Tool):
    streaming = 'row-local'

    def can_stream(self, cfg):
        return not cfg['remove_null_columns']  # column removal needs the whole table

    manifest = ToolManifest(
        tool_id='core.data_cleansing', version='1.0.0', display_name='Data Cleansing',
        catalog_ids=('preparation:data-cleansing',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Remove null rows/columns, replace nulls, strip unwanted characters and change case.',
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'fields': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None,
                           'description': 'Fields to cleanse (null = all string and numeric fields)'},
                'remove_null_rows': {'type': 'boolean', 'default': False},
                'remove_null_columns': {'type': 'boolean', 'default': False},
                'replace_null_strings': {'type': 'boolean', 'default': False},
                'replace_null_numbers': {'type': 'boolean', 'default': False},
                'trim_whitespace': {'type': 'boolean', 'default': False},
                'collapse_whitespace': {'type': 'boolean', 'default': False},
                'remove_tabs_linebreaks': {'type': 'boolean', 'default': False},
                'remove_all_whitespace': {'type': 'boolean', 'default': False},
                'remove_letters': {'type': 'boolean', 'default': False},
                'remove_numbers': {'type': 'boolean', 'default': False},
                'remove_punctuation': {'type': 'boolean', 'default': False},
                'case': {'enum': ['none', 'upper', 'lower', 'title'], 'default': 'none'},
            },
        })

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        fields = cfg['fields'] if cfg['fields'] is not None else [f.name for f in t.schema
                                                                  if f.type.is_string or f.type.is_numeric]
        require_fields(t.schema, fields)
        if cfg['remove_null_rows']:
            keep = [i for i, r in enumerate(t.rows()) if not all(v is None or v == '' for v in r)]
            t = t.take(keep)
        if cfg['remove_null_columns']:
            drop = {f.name for f, c in zip(t.schema, t.columns) if all(v is None or v == '' for v in c)}
            t = t.select([n for n in t.schema.names if n not in drop])
            fields = [f for f in fields if f not in drop]
        for name in fields:
            f = t.schema.field(name)
            col = t.column(name)
            if f.type.is_string:
                col = [self._clean_text(v, cfg) for v in col]
            elif f.type.is_numeric and cfg['replace_null_numbers']:
                col = [(Decimal(0) if f.type is FieldType.FIXED_DECIMAL else (0.0 if f.type in (FieldType.FLOAT, FieldType.DOUBLE) else 0))
                       if v is None else v for v in col]
            t = t.with_column(f, col)
        return {'output': t}

    @staticmethod
    def _clean_text(v, cfg):
        if v is None:
            return '' if cfg['replace_null_strings'] else None
        if cfg['remove_tabs_linebreaks']:
            v = re.sub(r'[\t\r\n]', '', v)
        if cfg['remove_all_whitespace']:
            v = re.sub(r'\s+', '', v)
        if cfg['collapse_whitespace']:
            v = re.sub(r'\s{2,}', ' ', v)
        if cfg['trim_whitespace']:
            v = v.strip()
        if cfg['remove_letters']:
            v = ''.join(ch for ch in v if not ch.isalpha())
        if cfg['remove_numbers']:
            v = ''.join(ch for ch in v if not ch.isdigit())
        if cfg['remove_punctuation']:
            v = ''.join(ch for ch in v if ch.isalnum() or ch.isspace())
        case = cfg['case']
        if case == 'upper':
            v = v.upper()
        elif case == 'lower':
            v = v.lower()
        elif case == 'title':
            v = v.title()
        return v


# ======================================================================== Data Cleanse Pro

class DataCleansePro(Tool):
    """Independent Unicode-aware implementation of the documented AMP cleanse options.

    This is a distinct tool from the legacy Data Cleansing macro. Unverified
    ICU edge cases are intentionally not described as reference-equivalent.
    """
    streaming = 'row-local'

    def can_stream(self, cfg):
        return not (cfg['remove_null_rows'] or cfg['remove_null_columns'])

    def infer(self, ctx, cfg, inputs):
        # Column removal depends on values and cannot be known from empty-schema inference.
        if cfg['remove_null_columns']:
            return None
        return super().infer(ctx, cfg, inputs)

    manifest = ToolManifest(
        tool_id='core.data_cleanse_pro', version='1.0.0', display_name='Data Cleanse Pro',
        catalog_ids=('preparation:data-cleanse-pro',), inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Output'),),
        description='AMP-style cleansing for selected fields: null handling, Unicode character removal, whitespace, HTML tags and case.',
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'fields': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None,
                           'description': 'Selected columns; null selects every input column.'},
                'remove_null_rows': {'type': 'boolean', 'default': False},
                'remove_null_columns': {'type': 'boolean', 'default': False},
                'remove_leading_and_trailing_whitespace': {'type': 'boolean', 'default': True},
                'remove_tabs_line_breaks_and_duplicates': {'type': 'boolean', 'default': False},
                'remove_all_whitespace': {'type': 'boolean', 'default': False},
                'remove_html_tags': {'type': 'boolean', 'default': False},
                'remove_invisible_characters': {'type': 'boolean', 'default': False},
                'remove_letters': {'type': 'boolean', 'default': False},
                'letters_except': {'type': 'string', 'default': ''},
                'remove_numbers': {'type': 'boolean', 'default': False},
                'numbers_except': {'type': 'string', 'default': ''},
                'remove_punctuation': {'type': 'boolean', 'default': False},
                'punctuation_except': {'type': 'string', 'default': ''},
                'replace_string_columns': {'type': 'boolean', 'default': False},
                'string_nulls': {'enum': ['preserve', 'blank', 'nullify_blank'], 'default': 'blank'},
                'replace_numeric_columns': {'type': 'boolean', 'default': False},
                'numeric_nulls': {'enum': ['preserve', 'zero', 'nullify_zero'], 'default': 'zero'},
                'modify_case': {'type': 'boolean', 'default': False},
                'case': {'enum': ['upper', 'lower', 'title'], 'default': 'upper'},
            },
        })

    def run(self, ctx, cfg, inputs):
        table: Table = inputs['input']
        selected = set(table.schema.names if cfg['fields'] is None else cfg['fields'])
        require_fields(table.schema, list(selected))
        fields = list(table.schema)
        cols = [list(c) for c in table.columns]
        for fi, field in enumerate(fields):
            if field.name not in selected:
                continue
            values = cols[fi]
            if field.type.is_string:
                values = [self._clean_string(v, cfg) for v in values]
                if cfg['replace_string_columns']:
                    if cfg['string_nulls'] == 'blank':
                        values = ['' if v is None else v for v in values]
                    elif cfg['string_nulls'] == 'nullify_blank':
                        values = [None if v == '' else v for v in values]
            elif field.type.is_numeric and cfg['replace_numeric_columns']:
                if cfg['numeric_nulls'] == 'zero':
                    zero = (Decimal(0).quantize(Decimal(1).scaleb(-field.scale)) if field.type is FieldType.FIXED_DECIMAL
                            else (0.0 if field.type in (FieldType.FLOAT, FieldType.DOUBLE) else 0))
                    values = [zero if v is None else v for v in values]
                elif cfg['numeric_nulls'] == 'nullify_zero':
                    values = [None if v == 0 else v for v in values]
            cols[fi] = values
        # The documented null row/column removals occur after all other steps.
        result = Table(fields, cols)
        if cfg['remove_null_rows']:
            keep = [i for i, row in enumerate(result.rows()) if not all(v is None for v in row)]
            result = result.take(keep)
        if cfg['remove_null_columns']:
            keep_fields = [f.name for f, values in zip(result.schema, result.columns)
                           if not all(v is None for v in values)]
            result = result.select(keep_fields)
        return {'output': result}

    @staticmethod
    def _clean_string(value, cfg):
        if value is None:
            return None
        value = str(value)
        if cfg['remove_html_tags']:
            value = re.sub(r'<[^>]*>', '', value)
        if cfg['remove_invisible_characters']:
            value = ''.join(ch for ch in value if unicodedata.category(ch) != 'Cf')
        if cfg['remove_letters']:
            exceptions = cfg['letters_except']
            value = ''.join(ch for ch in value if not (unicodedata.category(ch).startswith('L') and ch not in exceptions))
        if cfg['remove_numbers']:
            exceptions = cfg['numbers_except']
            value = ''.join(ch for ch in value if not (unicodedata.category(ch).startswith('N') and ch not in exceptions))
        if cfg['remove_punctuation']:
            exceptions = cfg['punctuation_except']
            value = ''.join(ch for ch in value if not (unicodedata.category(ch)[0] in ('P', 'S') and ch not in exceptions))
        if cfg['remove_all_whitespace']:
            value = ''.join(ch for ch in value if not ch.isspace())
        elif cfg['remove_tabs_line_breaks_and_duplicates']:
            value = re.sub(r'\s+', ' ', value)
        if cfg['remove_leading_and_trailing_whitespace']:
            value = value.strip()
        if cfg['modify_case']:
            if cfg['case'] == 'upper':
                value = value.upper()
            elif cfg['case'] == 'lower':
                value = value.lower()
            else:
                value = value.title()
        return value


TOOLS = [Filter, Formula, Select, Sort, RecordID, Sample, Unique, GenerateRows, MultiRowFormula, DataCleansing,
         DataCleansePro]
