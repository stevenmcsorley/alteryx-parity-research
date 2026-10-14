"""Transform category kernels: Summarize, Count Records, Running Total, Transpose, Cross Tab."""
from __future__ import annotations

import math
import statistics
from decimal import Decimal

from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value, cast_value, CastFailure
from ._common import require_fields

TEXT = 2**30 - 1

_AGGS = ['group_by', 'sum', 'count', 'count_non_null', 'count_null', 'count_distinct', 'count_distinct_non_null',
         'min', 'max', 'avg', 'median', 'mode', 'std_dev', 'variance', 'first', 'last', 'concat',
         'longest', 'shortest', 'count_blank', 'count_non_blank']
_NUMERIC_ONLY = {'sum', 'avg', 'median', 'std_dev', 'variance'}


def _numeric(values, name, op):
    out = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
            raise DataError(f'{op} on {name!r} requires numeric values')
        out.append(v)
    return out


def _aggregate(op, values, field: Field, spec):
    if op == 'count':
        return len(values)
    if op == 'count_non_null':
        return sum(v is not None for v in values)
    if op == 'count_null':
        return sum(v is None for v in values)
    if op == 'count_blank':
        return sum(v is None or v == '' for v in values)
    if op == 'count_non_blank':
        return sum(not (v is None or v == '') for v in values)
    if op == 'count_distinct':
        return len(set(values))
    if op == 'count_distinct_non_null':
        return len({v for v in values if v is not None})
    if op == 'first':
        return values[0] if values else None
    if op == 'last':
        return values[-1] if values else None
    nonnull = [v for v in values if v is not None]
    if op in ('min', 'max'):
        if not nonnull:
            return None
        try:
            return min(nonnull) if op == 'min' else max(nonnull)
        except TypeError:
            raise DataError(f'{op} on {field.name!r}: values are not mutually comparable') from None
    if op in ('longest', 'shortest'):
        if not nonnull:
            return None
        texts = [v if isinstance(v, str) else format_value(v) for v in nonnull]
        pick = max if op == 'longest' else min
        return pick(texts, key=len)  # first of equal length wins
    if op == 'concat':
        sep = spec.get('separator', ',')
        return sep.join(v if isinstance(v, str) else format_value(v) for v in nonnull) if nonnull else None
    if op == 'mode':
        if not nonnull:
            return None
        counts = {}
        for v in nonnull:
            counts[v] = counts.get(v, 0) + 1
        best = max(counts.values())
        return next(v for v in nonnull if counts[v] == best)  # earliest among ties
    nums = _numeric(values, field.name, op)
    if not nums:
        return None
    if op == 'sum':
        if any(isinstance(v, float) for v in nums):
            return math.fsum(float(v) for v in nums)
        return sum(nums)
    floats = [float(v) for v in nums]
    if op == 'avg':
        return math.fsum(floats) / len(floats)
    if op == 'median':
        return float(statistics.median(floats))
    if op in ('std_dev', 'variance'):
        if len(floats) < 2:
            return 0.0  # single value: 0, not null [ref:summarize-statistics]
        return statistics.stdev(floats) if op == 'std_dev' else statistics.variance(floats)  # sample (n-1)
    raise ConfigError(f'Unsupported aggregation {op}')  # pragma: no cover


def _agg_field(op, src: Field, out_name: str) -> Field:
    if op in ('count', 'count_non_null', 'count_null', 'count_distinct', 'count_distinct_non_null',
              'count_blank', 'count_non_blank'):
        return Field(out_name, FieldType.INT64)
    if op in ('avg', 'median', 'std_dev', 'variance'):
        return Field(out_name, FieldType.DOUBLE)
    if op == 'sum':
        if src.type.is_integer or src.type is FieldType.BOOL:
            return Field(out_name, FieldType.INT64)
        if src.type is FieldType.FIXED_DECIMAL:
            return Field(out_name, FieldType.FIXED_DECIMAL, 38, src.scale)
        if src.type.is_numeric:
            return Field(out_name, FieldType.DOUBLE)
        raise SchemaError(f'sum requires a numeric field; {src.name!r} is {src.type.value}')
    if op in ('concat', 'longest', 'shortest'):
        return Field(out_name, FieldType.V_WSTRING, TEXT)
    if op in _NUMERIC_ONLY and not src.type.is_numeric:
        raise SchemaError(f'{op} requires a numeric field; {src.name!r} is {src.type.value}')
    return src.renamed(out_name)  # group_by, min, max, first, last, mode


class Summarize(Tool):
    manifest = ToolManifest(
        tool_id='core.summarize', version='1.0.0', display_name='Summarize',
        catalog_ids=('transform:summarize',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Group and aggregate. Groups are output in ascending key order by default. '
                    'Std dev/variance are sample (n-1) estimates (0 for a single value).',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['actions'],
            'properties': {
                'actions': {'type': 'array', 'minItems': 1, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['field', 'op'],
                    'properties': {
                        'field': {'type': 'string'}, 'op': {'enum': _AGGS},
                        'output': {'type': 'string', 'minLength': 1},
                        'separator': {'type': 'string'},
                    }}},
                'sort_groups': {'type': 'boolean', 'default': True,
                                'description': 'Order output groups ascending by group-by values (nulls first). '
                                               'True matches the reference [ref:summarize-basic]; False keeps '
                                               'first-occurrence order'},
            },
        })

    def check_config(self, cfg):
        names = [self._out_name(a) for a in cfg['actions']]
        if len(set(names)) != len(names):
            raise ConfigError(f'Duplicate output field names {sorted({n for n in names if names.count(n) > 1})}; '
                              f'set "output" to disambiguate')

    @staticmethod
    def _out_name(a):
        if 'output' in a:
            return a['output']
        if a['op'] == 'group_by':
            return a['field']
        return f"{''.join(p.capitalize() for p in a['op'].split('_'))}_{a['field']}"

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        acts = cfg['actions']
        require_fields(t.schema, [a['field'] for a in acts])
        gb = [a for a in acts if a['op'] == 'group_by']
        gi = [t.schema.index(a['field']) for a in gb]
        out_fields = [_agg_field(a['op'], t.schema.field(a['field']), self._out_name(a)) for a in acts]
        Schema(out_fields)
        groups: dict = {}
        for i, row in enumerate(t.rows()):
            groups.setdefault(tuple(row[j] for j in gi), []).append(i)
        if not gb and not groups:
            groups[()] = []  # whole-table aggregate of empty input yields one record
        keys = list(groups)
        if cfg['sort_groups']:
            from .preparation import sort_key_fn
            kschema = Schema([t.schema.field(a['field']) for a in gb])
            kf = sort_key_fn(kschema, [{'field': a['field']} for a in gb])
            keys.sort(key=kf)
        cols = [[] for _ in acts]
        for k in keys:
            members = groups[k]
            for c, a in enumerate(acts):
                if a['op'] == 'group_by':
                    cols[c].append(k[gb.index(a)])
                    continue
                values = [t.columns[t.schema.index(a['field'])][i] for i in members]
                v = _aggregate(a['op'], values, t.schema.field(a['field']), a)
                try:
                    v, _ = cast_value(v, out_fields[c])
                except CastFailure as e:
                    raise DataError(f'{a["op"]} result for {a["field"]!r}: {e}') from None
                cols[c].append(v)
        return {'output': Table(out_fields, cols)}


class CountRecords(Tool):
    manifest = ToolManifest(
        tool_id='core.count_records', version='1.0.0', display_name='Count Records',
        catalog_ids=('transform:count-records',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='One record with the number of incoming records (0 for empty input).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'properties': {'field_name': {'type': 'string', 'minLength': 1, 'default': 'Count'}}})

    def run(self, ctx, cfg, inputs):
        f = Field(cfg['field_name'], FieldType.INT64)
        if ctx.dry_run:
            return {'output': Table.empty([f])}
        return {'output': Table([f], [[len(inputs['input'])]])}


class RunningTotal(Tool):
    manifest = ToolManifest(
        tool_id='core.running_total', version='1.0.0', display_name='Running Total',
        catalog_ids=('transform:running-total',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Cumulative sums in record order, optionally restarting per group. Nulls add nothing.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['fields'],
            'properties': {
                'fields': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
                'group_by': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
                'prefix': {'type': 'string', 'default': 'RunTot_'},
            },
        })

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, cfg['fields'] + cfg['group_by'])
        gi = [t.schema.index(g) for g in cfg['group_by']]
        if gi:  # grouped output is ordered by group key, stable [ref:running-total-grouped]
            from .preparation import sort_key_fn
            key = sort_key_fn(t.schema, [{'field': g} for g in cfg['group_by']])
            rows = list(t.rows())
            t = t.take(sorted(range(len(rows)), key=lambda i: key(rows[i])))
        out = t
        for name in cfg['fields']:
            src = t.schema.field(name)
            if not src.type.is_numeric:
                raise SchemaError(f'Running total requires numeric fields; {name!r} is {src.type.value}')
            ftype = FieldType.INT64 if src.type.is_integer else (
                FieldType.FIXED_DECIMAL if src.type is FieldType.FIXED_DECIMAL else FieldType.DOUBLE)
            field = Field(cfg['prefix'] + name, ftype, 38 if ftype is FieldType.FIXED_DECIMAL else None, src.scale if ftype is FieldType.FIXED_DECIMAL else None)
            totals: dict = {}
            col = t.column(name)
            vals = []
            for row, v in zip(t.rows(), col):
                k = tuple(row[j] for j in gi)
                totals[k] = totals.get(k, 0) + (v or 0)
                vals.append(float(totals[k]) if ftype is FieldType.DOUBLE else totals[k])
            out = out.with_column(field, vals)
        return {'output': out}


class Transpose(Tool):
    manifest = ToolManifest(
        tool_id='core.transpose', version='1.0.0', display_name='Transpose',
        catalog_ids=('transform:transpose',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Pivot data fields into Name/Value records, repeating key fields.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['key_fields'],
            'properties': {
                'key_fields': {'type': 'array', 'items': {'type': 'string'}},
                'data_fields': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None,
                                'description': 'null = every non-key field'},
                'name_field': {'type': 'string', 'default': 'Name'},
                'value_field': {'type': 'string', 'default': 'Value'},
            },
        })

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        keys = cfg['key_fields']
        data = cfg['data_fields'] if cfg['data_fields'] is not None else [n for n in t.schema.names if n not in keys]
        require_fields(t.schema, keys + data)
        if set(keys) & set(data):
            raise ConfigError('A field cannot be both a key and a data field')
        fields = [t.schema.field(k) for k in keys] + [Field(cfg['name_field'], FieldType.V_WSTRING, TEXT),
                                                     Field(cfg['value_field'], FieldType.V_WSTRING, TEXT)]
        ki = [t.schema.index(k) for k in keys]
        di = [(n, t.schema.index(n)) for n in data]
        rows = []
        for row in t.rows():
            base = tuple(row[i] for i in ki)
            for n, i in di:
                rows.append(base + (n, format_value(row[i])))
        return {'output': Table.from_rows(fields, rows)}


class CrossTab(Tool):
    manifest = ToolManifest(
        tool_id='core.cross_tab', version='1.0.0', display_name='Cross Tab',
        catalog_ids=('transform:cross-tab',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Pivot distinct header-field values into columns. New column names are the header values, '
                    'sorted ascending; groups in ascending key order. Output schema is data-dependent.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['group_by', 'header_field', 'value_field'],
            'properties': {
                'group_by': {'type': 'array', 'items': {'type': 'string'}},
                'header_field': {'type': 'string'}, 'value_field': {'type': 'string'},
                'method': {'enum': ['sum', 'count', 'avg', 'min', 'max', 'first', 'last', 'concat'], 'default': 'sum'},
                'separator': {'type': 'string', 'default': ','},
                'null_header_name': {'type': 'string', 'default': '_Null_'},
            },
        })

    def infer(self, ctx, cfg, inputs):
        return None  # columns depend on data values

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, cfg['group_by'] + [cfg['header_field'], cfg['value_field']])
        gi = [t.schema.index(g) for g in cfg['group_by']]
        hi, vi = t.schema.index(cfg['header_field']), t.schema.index(cfg['value_field'])
        vfield = t.schema[vi]
        groups: dict = {}
        headers = set()
        for row in t.rows():
            h = cfg['null_header_name'] if row[hi] is None else format_value(row[hi])
            headers.add(h)
            groups.setdefault(tuple(row[j] for j in gi), {}).setdefault(h, []).append(row[vi])
        cols_h = sorted(headers)
        clash = set(cols_h) & set(cfg['group_by'])
        if clash:
            raise SchemaError(f'Header values collide with group-by field names: {sorted(clash)}')
        method = cfg['method']
        out_fields = [t.schema[j] for j in gi] + [_agg_field(method, vfield.renamed(h), h) for h in cols_h]
        from .preparation import sort_key_fn
        gkey = sort_key_fn(Schema([t.schema[j] for j in gi]), [{'field': t.schema[j].name} for j in gi])
        rows = []
        for k, cells in sorted(groups.items(), key=lambda kv: gkey(kv[0])):  # [ref:crosstab-sum]
            vals = []
            for h, f in zip(cols_h, out_fields[len(gi):]):
                if h not in cells:
                    vals.append(None)
                    continue
                v = _aggregate(method, cells[h], vfield, {'separator': cfg['separator']})
                vals.append(cast_value(v, f)[0])
            rows.append(tuple(k) + tuple(vals))
        return {'output': Table.from_rows(out_fields, rows)}


TOOLS = [Summarize, CountRecords, RunningTotal, Transpose, CrossTab]
