"""Preparation kernels, wave 2: Auto Field, Multi-Field Formula, Tile, Rank, Random % Sample,
Select Records, Imputation, Multi-Field Binning, Create Samples, Oversample Field."""
from __future__ import annotations

import math
import random
import re
import statistics

from ..errors import ConfigError, DataError, ExpressionError, SchemaError
from ..formula import EvalContext, compile_expression, field_type_for_kind
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import CastFailure, Field, FieldType, Schema, cast_value, format_value
from ._common import ON_ERROR, TYPE_ENUM, cast_column, field_from_spec, require_fields

TEXT = 2**30 - 1


def _group_index(t: Table, group_by):
    require_fields(t.schema, group_by)
    gi = [t.schema.index(g) for g in group_by]
    groups: dict = {}
    for i, row in enumerate(t.rows()):
        groups.setdefault(tuple(row[j] for j in gi), []).append(i)
    return groups


# =========================================================================== Auto Field

_INT = re.compile(r'^[+-]?\d+$')
_NUM = re.compile(r'^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_DT = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
_TIME = re.compile(r'^\d{2}:\d{2}:\d{2}$')


def autofield_type(values) -> Field | None:
    """Smallest type holding every non-null, non-empty text value (engine policy, see ENGINE_SEMANTICS)."""
    vals = [v.strip() for v in values if v is not None and v.strip() != '']
    if not vals:
        return None
    if all(_INT.match(v) for v in vals):
        nums = [int(v) for v in vals]
        lo, hi = min(nums), max(nums)
        for t, (a, b) in ((FieldType.BYTE, (0, 255)), (FieldType.INT16, (-2**15, 2**15 - 1)),
                          (FieldType.INT32, (-2**31, 2**31 - 1)), (FieldType.INT64, (-2**63, 2**63 - 1))):
            if a <= lo and hi <= b:
                return Field('x', t)
        return Field('x', FieldType.DOUBLE)
    if all(_NUM.match(v) for v in vals):
        return Field('x', FieldType.DOUBLE)
    for pattern, t in ((_DATE, FieldType.DATE), (_DT, FieldType.DATETIME), (_TIME, FieldType.TIME)):
        if all(pattern.match(v) for v in vals):
            try:
                for v in vals:
                    cast_value(v, Field('x', t))
                return Field('x', t)
            except CastFailure:
                break
    longest = max(len(v) for v in values if v is not None)
    ascii_only = all(v is None or v.isascii() for v in values)
    return Field('x', FieldType.V_STRING if ascii_only else FieldType.V_WSTRING, max(longest, 1))


class AutoField(Tool):
    streaming = None
    manifest = ToolManifest(
        tool_id='core.auto_field', version='1.0.0', display_name='Auto Field', catalog_ids=('preparation:auto-field',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Convert text fields to the smallest type that holds all their values. Output schema is '
                    'data-dependent. Empty text becomes null in converted non-text fields.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'fields': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None,
                       'description': 'Text fields to examine (null = every text field)'}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        names = cfg['fields'] if cfg['fields'] is not None else [f.name for f in t.schema if f.type.is_string]
        require_fields(t.schema, names)
        out = t
        for n in names:
            f = t.schema.field(n)
            if not f.type.is_string:
                continue
            col = t.column(n)
            best = autofield_type(col)
            if best is None:
                continue
            target = Field(n, best.type, best.size, best.scale)
            if target.type.is_string:
                out = out.with_column(target, col)
            else:
                out = out.with_column(target, [None if v is None or v.strip() == '' else cast_value(v.strip(), target)[0]
                                               for v in col])
        return {'output': out}


# =========================================================================== Multi-Field Formula

class MultiFieldFormula(Tool):
    streaming = 'row-local'
    manifest = ToolManifest(
        tool_id='core.multi_field_formula', version='1.0.0', display_name='Multi-Field Formula',
        catalog_ids=('preparation:multi-field-formula',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Apply one expression to several fields; [_CurrentField_] is the field value and '
                    '[_CurrentFieldName_] its name. Optionally write to new prefixed/suffixed fields or change type.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields', 'expression'], 'properties': {
            'fields': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
            'expression': {'type': 'string', 'minLength': 1},
            'copy_output': {'type': 'boolean', 'default': False},
            'prefix': {'type': 'string', 'default': ''},
            'suffix': {'type': 'string', 'default': '_out'},
            'output_type': {'enum': [None] + TYPE_ENUM, 'default': None,
                            'description': 'null = keep each field\'s own type'},
            'output_size': {'type': 'integer', 'minimum': 1},
            'on_conversion_error': ON_ERROR,
        }})

    def check_config(self, cfg):
        if cfg['copy_output'] and not (cfg['prefix'] or cfg['suffix']):
            raise ConfigError('Copying output needs a prefix or suffix')

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, cfg['fields'])
        out = t
        ec = EvalContext(now=ctx.clock, warn=lambda m: ctx.warn(m))
        for name in cfg['fields']:
            src = t.schema.field(name)
            from ..formula import kind_of_field
            expr_src = cfg['expression'].replace('[_CurrentField_]', '[__current__]')
            sch = Schema(list(t.schema) + [Field('__current__', src.type, src.size, src.scale),
                                           Field('__current_name__', FieldType.V_WSTRING)])
            expr_src = expr_src.replace('[_CurrentFieldName_]', '[__current_name__]')
            try:
                expr = compile_expression(expr_src, sch)
            except ExpressionError as e:
                raise ExpressionError(f'{name}: {e.message}', node_id=ctx.node_id) from None
            col = t.column(name)
            values = [expr.evaluate(row + (v, name), ec) for row, v in zip(t.rows(), col)]
            if cfg['output_type']:
                target = field_from_spec({'name': name, 'type': cfg['output_type'],
                                          **({'size': cfg['output_size']} if 'output_size' in cfg else {})})
            else:
                target = src
            if cfg['copy_output']:
                target = target.renamed(cfg['prefix'] + name + cfg['suffix'])
                if target.name in out.schema and target.name not in cfg['fields']:
                    raise SchemaError(f'Output field {target.name!r} already exists')
            out = out.with_column(target, cast_column(values, target, ctx, cfg['on_conversion_error']))
        return {'output': out}


# =========================================================================== Tile

class Tile(Tool):
    manifest = ToolManifest(
        tool_id='core.tile', version='1.0.0', display_name='Tile', catalog_ids=('preparation:tile',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Assign tile numbers by equal record counts, equal sums, manual cut-offs or unique values. '
                    'Adds Tile_Num and Tile_SequenceNum. Input order is preserved.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['method'], 'properties': {
            'method': {'enum': ['equal_records', 'equal_sum', 'manual', 'unique_value']},
            'tiles': {'type': 'integer', 'minimum': 1, 'default': 4},
            'field': {'type': 'string', 'description': 'Field for equal_sum / manual / unique_value'},
            'cutoffs': {'type': 'array', 'items': {'type': 'number'}, 'default': [],
                        'description': 'Manual: ascending upper bounds of each tile'},
            'group_by': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
        }})

    def check_config(self, cfg):
        if cfg['method'] != 'equal_records' and not cfg.get('field'):
            raise ConfigError(f"Tile method {cfg['method']} needs a field")
        if cfg['method'] == 'manual' and (not cfg['cutoffs'] or cfg['cutoffs'] != sorted(cfg['cutoffs'])):
            raise ConfigError('Manual tiling needs ascending cut-offs')

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        n_rows = len(t)
        tile = [None] * n_rows
        seq = [None] * n_rows
        col = t.column(cfg['field']) if cfg.get('field') else None
        if col is not None:
            require_fields(t.schema, [cfg['field']])
        for members in _group_index(t, cfg['group_by']).values():
            m = cfg['method']
            if m == 'equal_records':
                k = cfg['tiles']
                n = len(members)
                for pos, i in enumerate(members):
                    tile[i] = pos * k // n + 1 if n else None
            elif m == 'equal_sum':
                vals = [col[i] or 0 for i in members]
                total = sum(vals)
                k = cfg['tiles']
                run = 0
                for i, v in zip(members, vals):
                    tile[i] = min(k, int(run * k // total) + 1) if total else 1
                    run += v
            elif m == 'manual':
                cuts = cfg['cutoffs']
                for i in members:
                    v = col[i]
                    tile[i] = None if v is None else next((j + 1 for j, c in enumerate(cuts) if v <= c), len(cuts) + 1)
            else:
                ids: dict = {}
                for i in members:
                    tile[i] = ids.setdefault(col[i], len(ids) + 1)
            counts: dict = {}
            for i in members:
                if tile[i] is not None:
                    counts[tile[i]] = counts.get(tile[i], 0) + 1
                    seq[i] = counts[tile[i]]
        out = t.with_column(Field('Tile_Num', FieldType.INT32), tile)
        return {'output': out.with_column(Field('Tile_SequenceNum', FieldType.INT32), seq)}


# =========================================================================== Rank

class Rank(Tool):
    manifest = ToolManifest(
        tool_id='core.rank', version='1.0.0', display_name='Rank', catalog_ids=('preparation:rank',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Rank records by a field (ascending or descending) with a choice of tie handling. '
                    'Nulls rank last. Input order is preserved.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {
            'field': {'type': 'string'},
            'order': {'enum': ['asc', 'desc'], 'default': 'desc'},
            'ties': {'enum': ['min', 'dense', 'ordinal', 'average', 'max'], 'default': 'min'},
            'output_field': {'type': 'string', 'minLength': 1, 'default': 'Rank'},
            'group_by': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
        }})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        col = t.column(cfg['field'])
        ranks = [None] * len(t)
        desc = cfg['order'] == 'desc'
        for members in _group_index(t, cfg['group_by']).values():
            present = [i for i in members if col[i] is not None]
            try:
                present.sort(key=lambda i: col[i], reverse=desc)
            except TypeError:
                raise DataError(f"Cannot order values of {cfg['field']!r}") from None
            pos = 0
            dense = 0
            while pos < len(present):
                end = pos
                while end + 1 < len(present) and col[present[end + 1]] == col[present[pos]]:
                    end += 1
                dense += 1
                for k, i in enumerate(present[pos:end + 1]):
                    ranks[i] = {'min': pos + 1, 'max': end + 1, 'dense': dense, 'ordinal': pos + k + 1,
                                'average': (pos + end + 2) / 2}[cfg['ties']]
                pos = end + 1
        ftype = FieldType.DOUBLE if cfg['ties'] == 'average' else FieldType.INT64
        return {'output': t.with_column(Field(cfg['output_field'], ftype), ranks)}


# =========================================================================== Random % Sample / Create Samples / Oversample

class RandomSample(Tool):
    deterministic = True
    manifest = ToolManifest(
        tool_id='core.random_sample', version='1.0.0', display_name='Random % Sample',
        catalog_ids=('preparation:random-sample',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Random sample of N records or N percent without replacement, input order preserved. '
                    'Uses a seeded PRNG (Python Mersenne Twister): reproducible here, but the selected records are '
                    'not expected to equal another product\'s for the same seed.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['mode', 'n'], 'properties': {
            'mode': {'enum': ['count', 'percent']}, 'n': {'type': 'number', 'minimum': 0},
            'seed': {'type': 'integer', 'default': 1}}})

    def check_config(self, cfg):
        if cfg['mode'] == 'percent' and cfg['n'] > 100:
            raise ConfigError('Percent must be between 0 and 100')

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        n = len(t)
        k = min(n, int(cfg['n'])) if cfg['mode'] == 'count' else int(round(n * cfg['n'] / 100))
        idx = sorted(random.Random(cfg['seed']).sample(range(n), k))
        return {'output': t.take(idx)}


class CreateSamples(Tool):
    manifest = ToolManifest(
        tool_id='core.create_samples', version='1.0.0', display_name='Create Samples',
        catalog_ids=('preparation:create-samples',),
        inputs=(Anchor('input', 'I'),),
        outputs=(Anchor('estimation', 'E'), Anchor('validation', 'V'), Anchor('holdout', 'H')),
        description='Randomly split records into estimation, validation and holdout samples by percentage '
                    '(seeded, reproducible; order preserved within each output).',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'estimation_pct': {'type': 'number', 'minimum': 0, 'maximum': 100, 'default': 50},
            'validation_pct': {'type': 'number', 'minimum': 0, 'maximum': 100, 'default': 50},
            'seed': {'type': 'integer', 'default': 1}}})

    def check_config(self, cfg):
        if cfg['estimation_pct'] + cfg['validation_pct'] > 100:
            raise ConfigError('Estimation + validation percentages exceed 100')

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        rng = random.Random(cfg['seed'])
        e, v, h = [], [], []
        pe, pv = cfg['estimation_pct'] / 100, cfg['validation_pct'] / 100
        for i in range(len(t)):
            u = rng.random()
            (e if u < pe else v if u < pe + pv else h).append(i)
        return {'estimation': t.take(e), 'validation': t.take(v), 'holdout': t.take(h)}


class OversampleField(Tool):
    manifest = ToolManifest(
        tool_id='core.oversample_field', version='1.0.0', display_name='Oversample Field',
        catalog_ids=('preparation:oversample-field',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Keep every record whose field equals the target value and randomly drop other records so the '
                    'target makes up the desired percentage (seeded, order preserved).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field', 'value'], 'properties': {
            'field': {'type': 'string'}, 'value': {'type': ['string', 'number', 'boolean']},
            'desired_pct': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 100, 'default': 50},
            'seed': {'type': 'integer', 'default': 1}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        col = t.column(cfg['field'])
        target = [i for i, v in enumerate(col) if v is not None and format_value(v) == format_value(cfg['value'])]
        others = [i for i, v in enumerate(col) if i not in set(target)]
        pct = cfg['desired_pct'] / 100
        want_others = len(others) if pct >= 1 else int(round(len(target) * (1 - pct) / pct))
        if want_others >= len(others):
            if target:
                ctx.warn('Target value already makes up at least the desired percentage; no records removed')
            keep = others
        else:
            keep = random.Random(cfg['seed']).sample(others, want_others)
        return {'output': t.take(sorted(set(target) | set(keep)))}


# =========================================================================== Select Records

class SelectRecords(Tool):
    streaming = None
    manifest = ToolManifest(
        tool_id='core.select_records', version='1.0.0', display_name='Select Records',
        catalog_ids=('preparation:select-records',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Keep records by 1-based position: one entry per line/comma: "n", "a-b", "-b" (1..b), "a+" (a..end).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['ranges'], 'properties': {
            'ranges': {'type': 'string', 'minLength': 1}}})

    @staticmethod
    def parse(text):
        out = []
        for part in re.split(r'[\n,;]+', text):
            p = part.strip()
            if not p:
                continue
            m = re.fullmatch(r'(\d+)?\s*-\s*(\d+)|(\d+)\+|(\d+)', p)
            if not m:
                raise ConfigError(f'Invalid record range {p!r}')
            if m.group(2):
                out.append((int(m.group(1) or 1), int(m.group(2))))
            elif m.group(3):
                out.append((int(m.group(3)), None))
            else:
                out.append((int(m.group(4)), int(m.group(4))))
        return out

    def check_config(self, cfg):
        self.parse(cfg['ranges'])

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        ranges = self.parse(cfg['ranges'])
        keep = [i for i in range(len(t)) if any(a <= i + 1 and (b is None or i + 1 <= b) for a, b in ranges)]
        return {'output': t.take(keep)}


# =========================================================================== Imputation

class Imputation(Tool):
    manifest = ToolManifest(
        tool_id='core.imputation', version='1.0.0', display_name='Imputation', catalog_ids=('preparation:imputation',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Replace nulls (or a specific value) in numeric fields with the mean, median, mode or a value; '
                    'optionally add a 0/1 indicator field per imputed field.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'], 'properties': {
            'fields': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
            'replace': {'enum': ['null', 'value'], 'default': 'null'},
            'replace_value': {'type': 'number', 'default': 0},
            'with': {'enum': ['mean', 'median', 'mode', 'value'], 'default': 'mean'},
            'with_value': {'type': 'number', 'default': 0},
            'indicator': {'type': 'boolean', 'default': False},
        }})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, cfg['fields'])
        out = t
        for name in cfg['fields']:
            f = t.schema.field(name)
            if not f.type.is_numeric:
                raise SchemaError(f'Imputation requires numeric fields; {name!r} is {f.type.value}')
            col = t.column(name)
            hit = [v is None for v in col] if cfg['replace'] == 'null' else [v == cfg['replace_value'] for v in col]
            base = [float(v) for v, h in zip(col, hit) if v is not None and not h]
            how = cfg['with']
            # output type is static (never data-dependent): floating fields keep their type, mean/median of
            # other numeric fields produce double, mode/value keep the field's own type
            if f.type in (FieldType.DOUBLE, FieldType.FLOAT) or how in ('mode', 'value'):
                target = f
            else:
                target = Field(name, FieldType.DOUBLE)
            fill = None
            if how == 'value':
                fill = cfg['with_value']
            elif not base:
                if len(col):
                    ctx.warn(f'No values available to compute the {how} of {name!r}; left unchanged')
            elif how == 'mean':
                fill = math.fsum(base) / len(base)
            elif how == 'median':
                fill = statistics.median(base)
            else:
                counts: dict = {}
                for v in base:
                    counts[v] = counts.get(v, 0) + 1
                fill = min(v for v, c in counts.items() if c == max(counts.values()))
            new = [fill if h and fill is not None else v for v, h in zip(col, hit)]
            out = out.with_column(target, cast_column(new, target, ctx, 'error'))
            if cfg['indicator']:
                out = out.with_column(Field(f'{name}_Indicator', FieldType.INT32), [1 if h else 0 for h in hit])
        return {'output': out}


# =========================================================================== Multi-Field Binning

class MultiFieldBinning(Tool):
    manifest = ToolManifest(
        tool_id='core.multi_field_binning', version='1.0.0', display_name='Multi-Field Binning',
        catalog_ids=('preparation:multi-field-binning',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Bin numeric fields into N bins by equal record counts (by sorted value) or equal value '
                    'intervals; adds <field>_Tile_Num. Nulls get a null bin.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'], 'properties': {
            'fields': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
            'method': {'enum': ['equal_records', 'equal_interval'], 'default': 'equal_records'},
            'bins': {'type': 'integer', 'minimum': 1, 'default': 5}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, cfg['fields'])
        out = t
        k = cfg['bins']
        for name in cfg['fields']:
            if not t.schema.field(name).type.is_numeric:
                raise SchemaError(f'Binning requires numeric fields; {name!r} is not')
            col = t.column(name)
            present = [i for i, v in enumerate(col) if v is not None]
            bins = [None] * len(col)
            if cfg['method'] == 'equal_records':
                order = sorted(present, key=lambda i: col[i])
                for pos, i in enumerate(order):
                    bins[i] = pos * k // len(order) + 1
            elif present:
                lo, hi = min(col[i] for i in present), max(col[i] for i in present)
                width = (hi - lo) / k if hi != lo else 1
                for i in present:
                    bins[i] = min(k, int((col[i] - lo) / width) + 1) if hi != lo else 1
            out = out.with_column(Field(f'{name}_Tile_Num', FieldType.INT32), bins)
        return {'output': out}


TOOLS = [AutoField, MultiFieldFormula, Tile, Rank, RandomSample, CreateSamples, OversampleField, SelectRecords,
         Imputation, MultiFieldBinning]
