"""Machine Learning tools: feature types, data health, feature building, transformation, algorithms, fitting,
prediction and assisted modelling.

Independent scikit-learn implementations of a standard machine-learning pipeline. They are not the
vendor's Intelligence Suite, which builds on its own featuretools/woodwork/EvalML stack. The pipeline
travels with the data in an ``ML_Pipeline`` field (see ``flowengine.mlpipeline``). The vendor's XGBoost
option is offered as scikit-learn gradient boosting, and its descriptions say so. Machine Learning
Predict and Machine Learning Send connect to the vendor's cloud service and are not implemented here.
"""
from __future__ import annotations

import datetime as _dt
import math

from .. import mlpipeline as ml, modeling, reports
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import MODEL_SCHEMA, expect_model_schema, missing, model_table, read_models, register_fitter
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
_FIELDS = {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1}, 'default': []}
_OPT_FIELDS = {'type': ['array', 'null'], 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1}, 'default': None}
STREAM_IN = Anchor('input', 'Input', description='Records, or the output of another Machine Learning tool')
STREAM_OUT = Anchor('output', 'Output', description='The records with the pipeline so far in ML_Pipeline')
FIT_SCHEMA = Schema([Field('Metric', FieldType.V_WSTRING, 64), Field('Value', FieldType.DOUBLE)])
IMPORTANCE_SCHEMA = Schema([Field('Field', FieldType.V_WSTRING, TEXT), Field('Importance', FieldType.DOUBLE),
                            Field('Rank', FieldType.INT32)])


def _data_schema(schema):
    return ml.data_schema(schema)


def _metrics_table(metrics):
    rows = [(k, float(v)) for k, v in metrics.items() if isinstance(v, (int, float)) and v is not None and math.isfinite(v)]
    return Table.from_rows(FIT_SCHEMA, rows)


# =========================================================================== Feature Types

class FeatureTypes(Tool):
    TYPES = Schema([Field('Name', FieldType.V_WSTRING, TEXT), Field('InputType', FieldType.V_WSTRING, 64),
                    Field('FeatureType', FieldType.V_WSTRING, 64), Field('Autodetected', FieldType.BOOL)])
    manifest = ToolManifest(
        tool_id='core.ml_feature_types', version='1.0.0', display_name='Feature Types',
        catalog_ids=('machine-learning:feature-types',),
        inputs=(STREAM_IN,), outputs=(STREAM_OUT, Anchor('types', 'Types', description='Name, input type and feature type')),
        description='Assign a machine-learning feature type to each field (Integer, Double, Boolean, Categorical, '
                    'Datetime, NaturalLanguage, Index or Unknown). Types are autodetected unless overridden. Feature '
                    'type is separate from the data type: values are unchanged and the types travel in ML_Pipeline '
                    'to Data Health, Build Features, Transformation, Assisted Modeling and Fit.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'overrides': {'type': 'object', 'default': {}, 'additionalProperties': {'enum': ['Autodetect', *ml.FEATURE_TYPES]}}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(_data_schema(inputs['input']), list(cfg['overrides']))
        return {'output': ml.stream_schema(inputs['input']), 'types': self.TYPES}

    def run(self, ctx, cfg, inputs):
        data, info = ml.read_stream(inputs['input'])
        require_fields(data.schema, list(cfg['overrides']))
        rows, assigned = [], {}
        for f in data.schema:
            chosen = cfg['overrides'].get(f.name, 'Autodetect')
            ftype = ml.detect_feature_type(f, data.column(f.name)) if chosen == 'Autodetect' else chosen
            assigned[f.name] = ftype
            rows.append((f.name, f.type.value, ftype, chosen == 'Autodetect'))
        info['feature_types'] = assigned
        return {'output': ml.write_stream(data, info), 'types': Table.from_rows(self.TYPES, rows)}


# =========================================================================== Data Health

def _health(values, numeric):
    """(missing, outliers, dominant share, outlier row indexes) for one column."""
    import numpy as np
    present_idx = [i for i, v in enumerate(values) if not (missing(v) or (isinstance(v, str) and not v.strip()))]
    n_missing = len(values) - len(present_idx)
    outlier_rows = []
    if numeric and len(present_idx) >= 4:
        x = np.array([float(values[i]) for i in present_idx])
        q1, q3 = np.percentile(x, [25, 75])
        iqr = q3 - q1
        if iqr > 0:
            lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            outlier_rows = [i for i, v in zip(present_idx, x) if v < lo or v > hi]
    counts = {}
    for i in present_idx:
        key = format_value(values[i])
        counts[key] = counts.get(key, 0) + 1
    dominant = max(counts.values()) / len(present_idx) if present_idx else 1.0
    return n_missing, outlier_rows, dominant, len(present_idx)


def _recommend(missing_share, outliers, dominant, present):
    notes = []
    if present == 0:
        return 'Every value is missing; drop this field'
    if missing_share >= 0.5:
        notes.append('More than half the values are missing; consider dropping the field')
    elif missing_share > 0:
        notes.append('Fill missing values (median for numeric, mode for categorical)')
    if outliers:
        notes.append(f'Review {outliers} outlier(s) outside 1.5×IQR; cap or remove them')
    if dominant >= 1.0 and present > 1:
        notes.append('Constant value; drop the field')
    elif dominant >= 0.9:
        notes.append('Nearly constant; consider dropping the field')
    return '; '.join(notes) or 'No action needed'


class DataHealth(Tool):
    manifest = ToolManifest(
        tool_id='core.data_health', version='1.0.0', display_name='Data Health', catalog_ids=('machine-learning:data-health',),
        inputs=(STREAM_IN,),
        outputs=(Anchor('scores', 'S', description='Health score per field'), Anchor('report', 'R', description='Health report'),
                 Anchor('outliers', 'O', description='Records holding an outlier, with RecordID and OutlierFields')),
        description='Check whether data is ready for machine learning (at least 30 records). Each field is scored on '
                    'three factors: the share of missing or blank values, the share of numeric outliers outside '
                    'Tukey\'s fences (1.5×IQR beyond the quartiles), and sparsity. Sparsity is the share held by '
                    'the single most common value; above 90% it reduces the score, reaching zero for a constant '
                    'field. Score = (1 − missing) × (1 − outliers) × (1 − sparsity penalty). The (Overall) row '
                    'averages the field scores.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'fields': _OPT_FIELDS, 'scale': {'enum': ['percent', 'unit'], 'default': 'percent'},
            'recommendations': {'type': 'boolean', 'default': False}}})

    def _scores_schema(self, cfg):
        fields = [Field('Field', FieldType.V_WSTRING, TEXT), Field('FeatureType', FieldType.V_WSTRING, 64),
                  Field('Missing', FieldType.DOUBLE), Field('Outliers', FieldType.DOUBLE), Field('Sparsity', FieldType.DOUBLE),
                  Field('Score', FieldType.DOUBLE)]
        if cfg['recommendations']:
            fields.append(Field('Recommendation', FieldType.V_WSTRING, TEXT))
        return Schema(fields)

    def infer(self, ctx, cfg, inputs):
        data = _data_schema(inputs['input'])
        require_fields(data, cfg['fields'] or [])
        out = Schema([*data, Field('RecordID', FieldType.INT64), Field('OutlierFields', FieldType.V_WSTRING, TEXT)])
        return {'scores': self._scores_schema(cfg), 'report': Schema([reports_field()]), 'outliers': out}

    def run(self, ctx, cfg, inputs):
        data, info = ml.read_stream(inputs['input'])
        if len(data) < 30:
            raise DataError(f'Data Health needs at least 30 records; the input has {len(data)}')
        names = cfg['fields'] or data.schema.names
        require_fields(data.schema, names)
        ftypes = ml.feature_types(data, info)
        k = 100.0 if cfg['scale'] == 'percent' else 1.0
        rows, scores, flagged = [], [], {}
        for name in names:
            f = data.schema.field(name)
            numeric = f.type.is_numeric and f.type is not FieldType.BOOL
            n_missing, outlier_rows, dominant, present = _health(data.column(name), numeric)
            m = n_missing / len(data)
            o = len(outlier_rows) / present if present else 0.0
            penalty = 1.0 if present == 0 else min(max((dominant - 0.9) / 0.1, 0.0), 1.0)
            score = (1 - m) * (1 - o) * (1 - penalty)
            scores.append(score)
            for i in outlier_rows:
                flagged.setdefault(i, []).append(name)
            row = [name, ftypes[name], m * k, o * k, dominant * k, score * k]
            if cfg['recommendations']:
                row.append(_recommend(m, len(outlier_rows), dominant, present))
            rows.append(tuple(row))
        overall = sum(scores) / len(scores)
        rows.append(('(Overall)', None, None, None, None, overall * k) + ((None,) if cfg['recommendations'] else ()))
        schema = self._scores_schema(cfg)
        scores_table = Table.from_rows(schema, rows)
        out_schema = Schema([*data.schema, Field('RecordID', FieldType.INT64), Field('OutlierFields', FieldType.V_WSTRING, TEXT)])
        outliers = Table.from_rows(out_schema, [(*data.row(i), i + 1, ', '.join(flagged[i])) for i in sorted(flagged)])
        fmt = (lambda v: '' if v is None else f'{v:.1f}%') if k == 100 else (lambda v: '' if v is None else f'{v:.3f}')
        columns = [{'label': c, 'align': 'left' if c in ('Field', 'Feature type', 'Recommendation') else 'right'}
                   for c in ['Field', 'Feature type', 'Missing', 'Outliers', 'Sparsity', 'Score']
                   + (['Recommendation'] if cfg['recommendations'] else [])]
        table_rows = [[r[0], r[1] or '', *[fmt(v) for v in r[2:6]], *r[6:]] for r in rows]
        report = [reports.element('text', text='Data health', style='title', align='left'),
                  reports.element('text', text=f'Overall score {fmt(overall * k)} across {len(names)} field(s) and '
                                               f'{len(data)} record(s); {len(flagged)} record(s) hold an outlier.',
                                  style='body', align='left'),
                  reports.element('table', title='Field health', group=None, columns=columns,
                                  rows=[[c if c is not None else '' for c in r] for r in table_rows], total_row=None)]
        return {'scores': scores_table, 'report': Table.from_rows(Schema([reports_field()]), [(e,) for e in report]),
                'outliers': outliers}


def reports_field():
    return Field('Report', FieldType.V_WSTRING, TEXT)


# =========================================================================== Build Features

AGGREGATIONS = ('count', 'sum', 'mean', 'min', 'max', 'std', 'skew', 'num_unique', 'mode', 'percent_true')
TRANSFORMS = ('year', 'month', 'day', 'weekday', 'hour', 'is_weekend', 'num_characters', 'num_words')
DEFAULT_PRIMITIVES = ['count', 'sum', 'mean', 'min', 'max', 'std', 'skew', 'num_unique', 'mode', 'percent_true',
                      'year', 'month', 'day', 'weekday', 'num_characters', 'num_words']
_KIND = {'Integer': 'numeric', 'Double': 'numeric', 'Boolean': 'boolean', 'Categorical': 'categorical',
         'Datetime': 'datetime', 'NaturalLanguage': 'text'}


def _as_datetime(v):
    if isinstance(v, _dt.datetime):
        return v
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day)
    try:
        return _dt.datetime.fromisoformat(str(v).strip())
    except ValueError:
        return None


def _skew(x):
    """Bias-corrected sample skewness (as pandas), None below three values or with zero spread."""
    n = len(x)
    if n < 3:
        return None
    mean = sum(x) / n
    m2 = sum((v - mean) ** 2 for v in x) / n
    if m2 == 0:
        return None
    m3 = sum((v - mean) ** 3 for v in x) / n
    return math.sqrt(n * (n - 1)) / (n - 2) * m3 / m2 ** 1.5


def _aggregate(primitive, values):
    present = [v for v in values if not missing(v)]
    if primitive == 'sum':
        return float(sum(present))
    if not present:
        return 0 if primitive == 'num_unique' else None
    if primitive == 'mean':
        return sum(present) / len(present)
    if primitive == 'min':
        return float(min(present))
    if primitive == 'max':
        return float(max(present))
    if primitive == 'std':
        if len(present) < 2:
            return None
        mean = sum(present) / len(present)
        return math.sqrt(sum((v - mean) ** 2 for v in present) / (len(present) - 1))
    if primitive == 'skew':
        return _skew(present)
    if primitive == 'num_unique':
        return len({format_value(v) for v in present})
    if primitive == 'mode':
        return ml._mode([format_value(v) for v in present])
    if primitive == 'percent_true':
        return sum(ml.to_bool(v) for v in present) / len(present)
    raise ConfigError(f'Unknown aggregation primitive {primitive!r}')


class _Feature:
    __slots__ = ('name', 'values', 'kind', 'field')

    def __init__(self, name, values, kind, field):
        self.name, self.values, self.kind, self.field = name, values, kind, field


class BuildFeatures(Tool):
    manifest = ToolManifest(
        tool_id='core.build_features', version='1.0.0', display_name='Build Features',
        catalog_ids=('machine-learning:build-features',),
        inputs=(Anchor('inputs', 'Input', multiple=True, description='One or more tables (#1, #2, ...) in connection order'),),
        outputs=(Anchor('output', 'Output', description='Target table records with the built features'),),
        description='Deep feature synthesis over related tables. Relationships link a parent key to a child key. '
                    'For the target table, the tool builds transform features of its own fields: YEAR, MONTH, DAY, '
                    'WEEKDAY, HOUR and IS_WEEKEND of dates, and NUM_CHARACTERS and NUM_WORDS of free text. It adds '
                    'aggregations of each child table: COUNT(child), and SUM, MEAN, MIN, MAX, STD and SKEW of numbers, '
                    'PERCENT_TRUE of Booleans, and MODE and NUM_UNIQUE of categories. It also adds direct features '
                    'from parent tables (parent.field). Depth limits how many primitives stack, for example '
                    'MEAN(sessions.SUM(transactions.amount)) at depth 2. Names follow the featuretools convention. '
                    'Key fields are not used as features.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'table_names': {'type': ['array', 'null'], 'items': {'type': 'string', 'pattern': '^[^.()]+$'}, 'default': None},
            'target_table': {'type': 'integer', 'minimum': 1, 'default': 1},
            'target_key': {'type': ['string', 'null'], 'default': None},
            'relationships': {'type': 'array', 'default': [], 'items': {
                'type': 'object', 'additionalProperties': False, 'required': ['parent', 'parent_key', 'child', 'child_key'],
                'properties': {'parent': {'type': 'integer', 'minimum': 1}, 'parent_key': {'type': 'string', 'minLength': 1},
                               'child': {'type': 'integer', 'minimum': 1}, 'child_key': {'type': 'string', 'minLength': 1}}}},
            'primitives': {'type': 'array', 'uniqueItems': True, 'items': {'enum': [*AGGREGATIONS, *TRANSFORMS]},
                           'default': DEFAULT_PRIMITIVES},
            'depth': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'default': 2}}})

    def infer(self, ctx, cfg, inputs):
        return None  # the built features depend on the data

    def run(self, ctx, cfg, inputs):
        streams = [ml.read_stream(t) for t in inputs['inputs']]
        n = len(streams)
        names = cfg['table_names'] or [f'T{i + 1}' for i in range(n)]
        if len(names) != n or len(set(names)) != n:
            raise ConfigError(f'table_names needs {n} distinct names, one per connected table')
        if cfg['target_table'] > n:
            raise ConfigError(f'Target table #{cfg["target_table"]} is not connected ({n} table(s))')
        target = cfg['target_table'] - 1
        keys = [set() for _ in range(n)]
        if cfg['target_key']:
            require_fields(streams[target][0].schema, [cfg['target_key']], 'target key')
            keys[target].add(cfg['target_key'])
        rels = []
        for r in cfg['relationships']:
            p, c = r['parent'] - 1, r['child'] - 1
            if p >= n or c >= n or p == c:
                raise ConfigError(f'Relationship #{r["parent"]} → #{r["child"]} must link two different connected tables')
            require_fields(streams[p][0].schema, [r['parent_key']], f'key of {names[p]}')
            require_fields(streams[c][0].schema, [r['child_key']], f'key of {names[c]}')
            keys[p].add(r['parent_key'])
            keys[c].add(r['child_key'])
            index = {}
            for i, v in enumerate(streams[p][0].column(r['parent_key'])):
                if missing(v):
                    continue
                k = format_value(v)
                if k in index:
                    raise DataError(f'Parent key {r["parent_key"]!r} of {names[p]} is not unique ({k!r} repeats)')
                index[k] = i
            rels.append((p, c, r['parent_key'], r['child_key'], index))
        prims = set(cfg['primitives'])
        ftypes = [ml.feature_types(d, info) for d, info in streams]

        def base(t):
            data = streams[t][0]
            return [_Feature(f.name, data.column(f.name), _KIND.get(ftypes[t][f.name]), f)
                    for f in data.schema if f.name not in keys[t] and _KIND.get(ftypes[t][f.name])]

        def transforms(feats):
            out = []
            for f in feats:
                if f.kind == 'datetime':
                    dts = [None if missing(v) else _as_datetime(v) for v in f.values]
                    parts = [('year', lambda d: d.year), ('month', lambda d: d.month), ('day', lambda d: d.day),
                             ('weekday', lambda d: d.weekday())]
                    if f.field.type is not FieldType.DATE:
                        parts.append(('hour', lambda d: d.hour))
                    for prim, fn in parts:
                        if prim in prims:
                            out.append(_Feature(f'{prim.upper()}({f.name})', [None if d is None else fn(d) for d in dts],
                                                'numeric', Field('_', FieldType.INT32)))
                    if 'is_weekend' in prims:
                        out.append(_Feature(f'IS_WEEKEND({f.name})', [None if d is None else d.weekday() >= 5 for d in dts],
                                            'boolean', Field('_', FieldType.BOOL)))
                elif f.kind == 'text':
                    if 'num_characters' in prims:
                        out.append(_Feature(f'NUM_CHARACTERS({f.name})', [None if missing(v) else len(str(v)) for v in f.values],
                                            'numeric', Field('_', FieldType.INT64)))
                    if 'num_words' in prims:
                        out.append(_Feature(f'NUM_WORDS({f.name})', [None if missing(v) else len(str(v).split()) for v in f.values],
                                            'numeric', Field('_', FieldType.INT64)))
            return out

        def features(t, depth, came_from):
            own = base(t)
            if depth == 0:
                return own
            out = own + transforms(own)
            rows_t = len(streams[t][0])
            for p, c, pkey, ckey, index in rels:
                if p == t and c != came_from:
                    groups = [[] for _ in range(rows_t)]
                    for i, v in enumerate(streams[c][0].column(ckey)):
                        j = None if missing(v) else index.get(format_value(v))
                        if j is not None:
                            groups[j].append(i)
                    if 'count' in prims:
                        out.append(_Feature(f'COUNT({names[c]})', [len(g) for g in groups], 'numeric', Field('_', FieldType.INT64)))
                    for f in features(c, depth - 1, t):
                        wanted = {'numeric': ('sum', 'mean', 'min', 'max', 'std', 'skew'), 'boolean': ('percent_true',),
                                  'categorical': ('mode', 'num_unique')}.get(f.kind, ())
                        for prim in wanted:
                            if prim not in prims:
                                continue
                            vals = [_aggregate(prim, [f.values[i] for i in g]) for g in groups]
                            kind, ftype = ('categorical', FieldType.V_WSTRING) if prim == 'mode' else \
                                ('numeric', FieldType.INT64 if prim == 'num_unique' else FieldType.DOUBLE)
                            out.append(_Feature(f'{prim.upper()}({names[c]}.{f.name})', vals, kind,
                                                Field('_', ftype, TEXT if ftype is FieldType.V_WSTRING else None)))
                if c == t and p != came_from:
                    lookup = [None if missing(v) else index.get(format_value(v)) for v in streams[c][0].column(ckey)]
                    for f in features(p, depth - 1, t):
                        out.append(_Feature(f'{names[p]}.{f.name}', [None if j is None else f.values[j] for j in lookup],
                                            f.kind, f.field))
            return out

        data = streams[target][0]
        built = [f for f in features(target, cfg['depth'], None) if f.name not in {b.name for b in base(target)}]
        out = data
        seen = set(data.schema.names)
        for f in built:
            if f.name in seen:
                ctx.warn(f'Feature {f.name!r} clashes with an existing field and was skipped')
                continue
            seen.add(f.name)
            ft = f.field.type
            field = Field(f.name, ft, f.field.size if ft.is_string or ft is FieldType.FIXED_DECIMAL else None, f.field.scale)
            out = out.with_column(field, f.values)
        if not built:
            ctx.warn('No features were built; check the relationships, primitives and feature types')
        return {'output': out}


# =========================================================================== Transformation

class Transformation(Tool):
    manifest = ToolManifest(
        tool_id='core.ml_transformation', version='1.0.0', display_name='Transformation',
        catalog_ids=('machine-learning:transformation',), inputs=(STREAM_IN,), outputs=(STREAM_OUT,),
        description='Add data-preparation steps to the pipeline. They are applied when Fit trains the model and again '
                    'when Predict scores new data. Set data types (Numeric, Categorical, Boolean or ID; ID fields '
                    'are not used). Clean up missing values with the training mean, median or mode, or a constant. '
                    'Leave features out of the model. One-hot encode features, with unknown categories at scoring '
                    'either ignored (all zeros) or treated as an error. Steps from several Transformation tools '
                    'combine; later settings win.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'numeric': _FIELDS, 'categorical': _FIELDS, 'boolean': _FIELDS, 'ids': _FIELDS,
            'impute_mean': _FIELDS, 'impute_median': _FIELDS, 'impute_mode': _FIELDS, 'impute_constant': _FIELDS,
            'constant_value': {'type': ['string', 'number', 'null'], 'default': None},
            'exclude': _FIELDS, 'one_hot': _FIELDS, 'unknown': {'enum': ['ignore', 'error'], 'default': 'ignore'}}})

    _TYPES = {'numeric': 'Numeric', 'categorical': 'Categorical', 'boolean': 'Boolean', 'ids': 'ID'}
    _IMPUTE = {'impute_mean': 'mean', 'impute_median': 'median', 'impute_mode': 'mode', 'impute_constant': 'constant'}

    def _check(self, cfg, schema):
        lists = [*self._TYPES, *self._IMPUTE, 'exclude', 'one_hot']
        require_fields(schema, sorted({n for k in lists for n in cfg[k]}))
        for group in (self._TYPES, self._IMPUTE):
            seen = {}
            for key in group:
                for name in cfg[key]:
                    if name in seen:
                        raise ConfigError(f'{name!r} is in both {seen[name]} and {key}')
                    seen[name] = key

    def infer(self, ctx, cfg, inputs):
        self._check(cfg, _data_schema(inputs['input']))
        return {'output': ml.stream_schema(inputs['input'])}

    def run(self, ctx, cfg, inputs):
        data, info = ml.read_stream(inputs['input'])
        self._check(cfg, data.schema)
        t = info['transform']
        for key, label in self._TYPES.items():
            for name in cfg[key]:
                t['types'][name] = label
        for key, method in self._IMPUTE.items():
            for name in cfg[key]:
                t['impute'][name] = method
                if method == 'constant':
                    if cfg['constant_value'] is None:
                        raise ConfigError('Set constant_value for the Replace with Constant clean-up')
                    t['constant'][name] = cfg['constant_value']
        t['exclude'] = sorted(set(t['exclude']) | set(cfg['exclude']))
        t['one_hot'] = sorted(set(t['one_hot']) | set(cfg['one_hot']))
        t['unknown'] = cfg['unknown']
        return {'output': ml.write_stream(data, info)}


# =========================================================================== Classification / Regression

_NUM = {'type': ['number', 'null'], 'default': None}
_INT = {'type': ['integer', 'null'], 'minimum': 1, 'default': None}
_COMMON_TREE = {
    'max_depth': {'type': ['integer', 'string', 'null'], 'default': None,
                  'description': "A whole number, 'unlimited', or null for the algorithm's default"},
    'max_features': {'type': ['string', 'number', 'null'], 'default': None,
                     'description': "auto, none, sqrt, log2, a whole number or a fraction"},
    'max_leaf_nodes': _INT, 'min_impurity_decrease': _NUM, 'min_samples_split': _NUM, 'min_weight_fraction_leaf': _NUM,
    'n_estimators': _INT, 'bootstrap': {'type': ['boolean', 'null'], 'default': None},
    'splitter': {'enum': ['best', 'random', None], 'default': None}}
_APPLIES = {
    'logistic_regression': {'C', 'penalty', 'solver', 'dual', 'tol', 'max_iter', 'fit_intercept', 'intercept_scaling',
                            'class_weight', 'multiclass'},
    'decision_tree': {'criterion', 'splitter', 'max_depth', 'max_features', 'max_leaf_nodes', 'min_impurity_decrease',
                      'min_samples_split', 'min_weight_fraction_leaf', 'class_weight'},
    'random_forest': {'criterion', 'max_depth', 'max_features', 'min_impurity_decrease', 'min_samples_split',
                      'min_weight_fraction_leaf', 'n_estimators', 'bootstrap', 'class_weight'},
    'gradient_boosting': {'learning_rate', 'max_depth', 'n_estimators', 'subsample', 'colsample'},
    'linear_regression': {'fit_intercept', 'normalize'},
}
_NOT_PARAMS = {'target_field', 'algorithm', 'features', 'seed'}


def _params(cfg, algorithm, warn):
    params = {k: v for k, v in cfg.items() if k not in _NOT_PARAMS and v is not None}
    ignored = sorted(set(params) - _APPLIES[algorithm])
    if ignored:
        warn(f'{ml.ALGORITHM_NAMES[algorithm]} does not use {ignored}; they were ignored')
    params = {k: v for k, v in params.items() if k in _APPLIES[algorithm]}
    if params.get('class_weight') == 'none':
        params['class_weight'] = None
    md = params.get('max_depth')
    if md is not None and md != 'unlimited' and not (isinstance(md, int) and md >= 1):
        raise ConfigError("max_depth must be a whole number of at least 1, or 'unlimited'")
    mf = params.get('max_features')
    if isinstance(mf, str) and mf not in ('auto', 'none', 'sqrt', 'log2'):
        raise ConfigError("max_features must be auto, none, sqrt, log2, a whole number or a fraction")
    params['seed'] = cfg['seed']
    return params


class _Algorithm(Tool):
    task = None

    def infer(self, ctx, cfg, inputs):
        data = _data_schema(inputs['input'])
        require_fields(data, [cfg['target_field'], *(cfg['features'] or [])])
        return {'output': ml.stream_schema(inputs['input'])}

    def run(self, ctx, cfg, inputs):
        data, info = ml.read_stream(inputs['input'])
        require_fields(data.schema, [cfg['target_field'], *(cfg['features'] or [])])
        if cfg['target_field'] in (cfg['features'] or []):
            raise ConfigError('The target field cannot also be a feature')
        info['estimator'] = {'task': self.task, 'algorithm': cfg['algorithm'], 'target': cfg['target_field'],
                             'features': cfg['features'], 'params': _params(cfg, cfg['algorithm'], ctx.warn)}
        return {'output': ml.write_stream(data, info)}


def _algorithm_manifest(tool_id, name, catalog, algorithms, description, extra):
    return ToolManifest(
        tool_id=tool_id, version='1.0.0', display_name=name, catalog_ids=(catalog,), inputs=(STREAM_IN,), outputs=(STREAM_OUT,),
        description=description,
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['target_field'], 'properties': {
            'target_field': {'type': 'string', 'minLength': 1}, 'algorithm': {'enum': list(algorithms), 'default': algorithms[0]},
            'features': _OPT_FIELDS, 'seed': {'type': 'integer', 'minimum': 0, 'default': 10}, **extra}})


class Classification(_Algorithm):
    task = 'classification'
    manifest = _algorithm_manifest(
        'core.ml_classification', 'Classification', 'machine-learning:classification', ml.CLASSIFIERS,
        'Choose the classification algorithm for the pipeline and its parameters. Fit trains it. The algorithms '
        'are logistic regression (liblinear by default, one-vs-rest for several classes), decision tree, random '
        'forest (depth 10 by default) and gradient boosting. Gradient boosting is scikit-learn gradient-boosted '
        'trees standing in for XGBoost: learning rate 0.05, depth 3, 100 estimators, plus subsample and column '
        'sample. Parameters left empty use the documented defaults, and parameters that do not apply to the '
        'chosen algorithm are reported and ignored.',
        {**_COMMON_TREE, 'C': {'type': ['number', 'null'], 'exclusiveMinimum': 0, 'default': None},
         'penalty': {'enum': ['l1', 'l2', None], 'default': None},
         'solver': {'enum': ['liblinear', 'sag', 'saga', 'lbfgs', 'newton-cg', None], 'default': None},
         'dual': {'type': ['boolean', 'null'], 'default': None}, 'tol': _NUM, 'max_iter': _INT,
         'fit_intercept': {'type': ['boolean', 'null'], 'default': None}, 'intercept_scaling': _NUM,
         'class_weight': {'enum': ['none', 'balanced', None], 'default': None},
         'multiclass': {'enum': ['auto', 'multinomial', 'ovr', None], 'default': None},
         'criterion': {'enum': ['gini', 'entropy', 'log_loss', None], 'default': None},
         'learning_rate': {'type': ['number', 'null'], 'exclusiveMinimum': 0, 'maximum': 1, 'default': None},
         'subsample': {'type': ['number', 'null'], 'exclusiveMinimum': 0, 'maximum': 1, 'default': None},
         'colsample': {'type': ['number', 'null'], 'exclusiveMinimum': 0, 'maximum': 1, 'default': None}})


class Regression(_Algorithm):
    task = 'regression'
    manifest = _algorithm_manifest(
        'core.ml_regression', 'Regression', 'machine-learning:regression', ml.REGRESSORS,
        'Choose the regression algorithm for the pipeline and its parameters. Fit trains it. The algorithms are '
        'linear regression (features standardised first when normalize is on, the default), decision tree and '
        'random forest. Trees use squared error, Friedman MSE or absolute error. Parameters left empty use the '
        'documented defaults.',
        {**_COMMON_TREE, 'fit_intercept': {'type': ['boolean', 'null'], 'default': None},
         'normalize': {'type': ['boolean', 'null'], 'default': None},
         'criterion': {'enum': ['squared_error', 'friedman_mse', 'absolute_error', None], 'default': None}})


# =========================================================================== Fit / Predict

MODEL = Anchor('model', 'Model', description='Trained pipeline for Predict (or Score)')


def _fit_outputs(name, model_info, estimator):
    ranked = sorted(model_info['importance'].items(), key=lambda kv: (-kv[1], kv[0]))
    return {'model': model_table(name, 'ml_pipeline', model_info['target'], model_info, modeling.sk_dumps(estimator)),
            'fit': _metrics_table(model_info['metrics']),
            'importance': Table.from_rows(IMPORTANCE_SCHEMA, [(k, v, i + 1) for i, (k, v) in enumerate(ranked)])}


def _fit(data, info, warn):
    try:
        return ml.fit(data, info, warn)
    except ValueError as e:  # scikit-learn parameter validation
        if isinstance(e, (ConfigError, DataError, SchemaError)):
            raise
        raise ConfigError(str(e)) from None


@register_fitter('core.ml_fit')
def _refit(ctx, cfg, table):
    data, _ = ml.read_stream(table)
    info, estimator = _fit(data, cfg['pipeline'], ctx.warn)
    return info, modeling.sk_dumps(estimator)


class Fit(Tool):
    manifest = ToolManifest(
        tool_id='core.ml_fit', version='1.0.0', display_name='Fit', catalog_ids=('machine-learning:fit',),
        inputs=(Anchor('input', 'Input', description='Output of Classification or Regression (directly or after sampling)'),),
        outputs=(MODEL, Anchor('fit', 'Fit', description='Training metrics'),
                 Anchor('importance', 'Importance', description='Share of importance per feature')),
        description='Train the pipeline. It applies the feature types and Transformation steps, fits the chosen '
                    'algorithm and outputs the model with its training metrics and feature importance. Importance '
                    'is impurity decrease for trees, or mean absolute coefficients for linear models. Records '
                    'with a missing target are left out and counted.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'model_name': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        if ml.PIPELINE_FIELD not in inputs['input']:
            raise SchemaError('Fit needs the output of a Classification or Regression tool')
        return {'model': MODEL_SCHEMA, 'fit': FIT_SCHEMA, 'importance': IMPORTANCE_SCHEMA}

    def run(self, ctx, cfg, inputs):
        if ml.PIPELINE_FIELD not in inputs['input'].schema:
            raise SchemaError('Fit needs the output of a Classification or Regression tool')
        data, info = ml.read_stream(inputs['input'])
        model_info, estimator = _fit(data, info, ctx.warn)
        name = cfg['model_name'] or ml.ALGORITHM_NAMES[model_info['algorithm']]
        return _fit_outputs(name, model_info, estimator)


class Predict(Tool):
    manifest = ToolManifest(
        tool_id='core.ml_predict', version='1.0.0', display_name='Predict', catalog_ids=('machine-learning:predict',),
        inputs=(Anchor('model', 'M', description='Model from Fit or Assisted Modeling (or any Flowbench model)'),
                Anchor('input', 'D', description='New records with the training fields')),
        outputs=(Anchor('output', 'Output'),),
        description='Apply a trained model to new records. Classification (including Text Classification) adds one '
                    'Probability_<class> field per class and Prediction (the most probable class). Regression adds '
                    'Prediction. Image Recognition models add Probability_<label> and Prediction. Topic Modeling models '
                    'add topic_1 … topic_k shares, and Named Entity Recognition models add entities and entity_diagram. The pipeline\'s '
                    'preparation is applied first. Records that cannot be prepared (a missing value with no '
                    'clean-up) get null predictions.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        expect_model_schema(inputs['model'])
        return None  # added fields depend on the model's classes

    def run(self, ctx, cfg, inputs):
        import numpy as np
        models = read_models(inputs['model'])
        if len(models) != 1:
            raise DataError(f'Model input holds {len(models)} models; connect one')
        _, info, payload = models[0]
        data, _ = ml.read_stream(inputs['input'])
        if info.get('family') == 'topic_model':
            return {'output': self._topics(info, payload, data)}
        if info.get('family') == 'ner':
            from .text_nlp import ner_apply
            return {'output': ner_apply(ctx, info, data, 'Predict')}
        if info.get('family') == 'image_classifier':
            from .vision_ai import image_predict
            res = image_predict(ctx, info, payload, data)
        else:
            res = modeling.predict(info, payload, data)
        if res['unseen']:
            ctx.warn(f"{res['unseen']} value(s) were categories the model did not see in training")
        if 'value' in res:
            added = [Field('Prediction', FieldType.DOUBLE)]
            values = [[float(v) if m and np.isfinite(v) else None] for v, m in zip(res['value'], res['mask'])]
        else:
            classes = info['classes']
            added = [*[Field(f'Probability_{c}', FieldType.DOUBLE) for c in classes], Field('Prediction', FieldType.V_WSTRING, TEXT)]
            values = []
            for p, m in zip(res['proba'], res['mask']):
                ok = m and np.all(np.isfinite(p))
                values.append([*(float(x) for x in p), classes[int(p.argmax())]] if ok else [None] * (len(classes) + 1))
        clash = [f.name for f in added if f.name in data.schema]
        if clash:
            raise SchemaError(f'Prediction would overwrite field(s) {clash}')
        schema = Schema([*data.schema, *added])
        return {'output': Table.from_rows(schema, [(*r, *v) for r, v in zip(data.rows(), values)])}

    def _topics(self, info, payload, data):
        from .text_mining import topic_matrix
        if info['text_field'] not in data.schema:
            raise SchemaError(f"Data has no text field {info['text_field']!r}")
        shares, mask = topic_matrix(info, modeling.sk_loads(payload), data)
        added = [Field(f'topic_{i + 1}', FieldType.DOUBLE) for i in range(info['topics'])]
        clash = [f.name for f in added if f.name in data.schema]
        if clash:
            raise SchemaError(f'Prediction would overwrite field(s) {clash}')
        rows = [(*r, *([float(x) for x in s] if m else [None] * len(added))) for r, s, m in zip(data.rows(), shares, mask)]
        return Table.from_rows(Schema([*data.schema, *added]), rows)


# =========================================================================== Assisted Modeling

_METRICS = {'accuracy': ('Accuracy', True), 'macro_f1': ('MacroF1', True), 'auc': ('AUC', True), 'log_loss': ('LogLoss', False),
            'rmse': ('RMSE', False), 'mae': ('MAE', False), 'r2': ('R2', True)}
_CLASS_METRICS = ('accuracy', 'macro_f1', 'auc', 'log_loss')


def _id_like(field, values):
    present = [v for v in values if not missing(v)]
    if len(present) < 10 or len({format_value(v) for v in present}) != len(present):
        return False
    if field.type.is_string:
        return True
    if field.type.is_integer:
        ints = sorted(int(v) for v in present)
        return ints[-1] - ints[0] == len(ints) - 1
    return False


class AssistedModeling(Tool):
    LEADERBOARD = Schema([Field('Rank', FieldType.INT32), Field('Algorithm', FieldType.V_WSTRING, 64), Field('Selected', FieldType.BOOL),
                          Field('Metric', FieldType.V_WSTRING, 32), Field('Score', FieldType.DOUBLE),
                          *[Field(n, FieldType.DOUBLE) for n in ('Accuracy', 'MacroF1', 'AUC', 'LogLoss', 'RMSE', 'MAE', 'R2')],
                          Field('Note', FieldType.V_WSTRING, TEXT)])
    PREPARATION = Schema([Field('Field', FieldType.V_WSTRING, TEXT), Field('FeatureType', FieldType.V_WSTRING, 64),
                          Field('Step', FieldType.V_WSTRING, TEXT)])
    manifest = ToolManifest(
        tool_id='core.assisted_modeling', version='1.0.0', display_name='Assisted Modeling',
        catalog_ids=('machine-learning:assisted-modeling',),
        inputs=(STREAM_IN,),
        outputs=(Anchor('model', 'Model', description='Best pipeline, trained on all records, for Predict'),
                 Anchor('leaderboard', 'Leaderboard', description='Cross-validated score of every algorithm'),
                 Anchor('preparation', 'Preparation', description='Automatic data-preparation steps'),
                 Anchor('pipeline', 'Pipeline', description='Records with the chosen pipeline, ready for Fit or editing')),
        description='Build a model automatically. Choose a target and the problem type (classification or regression, '
                    'autodetected from the target). Preparation is automatic for fields not already set by '
                    'Transformation: ID-like fields are dropped (all values distinct: text, or consecutive whole '
                    'numbers), as are dates, free text and categories with more than 50 values. Missing numbers '
                    'are filled with the median and missing categories with the mode. Every candidate algorithm '
                    'is then scored by k-fold cross-validation, stratified for classification, with preparation '
                    'refitted inside each fold. The best by the chosen metric is retrained on all records.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['target_field'], 'properties': {
            'target_field': {'type': 'string', 'minLength': 1}, 'task': {'enum': ['auto', 'classification', 'regression'], 'default': 'auto'},
            'algorithms': {'type': ['array', 'null'], 'uniqueItems': True, 'minItems': 1,
                           'items': {'enum': sorted(set(ml.CLASSIFIERS) | set(ml.REGRESSORS))}, 'default': None},
            'metric': {'enum': [*_METRICS, None], 'default': None}, 'features': _OPT_FIELDS,
            'folds': {'type': 'integer', 'minimum': 2, 'maximum': 20, 'default': 5},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 10}, 'model_name': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(_data_schema(inputs['input']), [cfg['target_field'], *(cfg['features'] or [])])
        return {'model': MODEL_SCHEMA, 'leaderboard': self.LEADERBOARD, 'preparation': self.PREPARATION,
                'pipeline': ml.stream_schema(inputs['input'])}

    def run(self, ctx, cfg, inputs):
        data, info = ml.read_stream(inputs['input'])
        target = cfg['target_field']
        require_fields(data.schema, [target, *(cfg['features'] or [])])
        task = ml.task_of(data, target, cfg['task'])
        candidates = cfg['algorithms'] or list(ml.CLASSIFIERS if task == 'classification' else ml.REGRESSORS)
        allowed = ml.CLASSIFIERS if task == 'classification' else ml.REGRESSORS
        wrong = [a for a in candidates if a not in allowed]
        if wrong:
            raise ConfigError(f'{wrong} are not {task} algorithms')
        metric = cfg['metric'] or ('accuracy' if task == 'classification' else 'rmse')
        if (metric in _CLASS_METRICS) != (task == 'classification'):
            raise ConfigError(f'Metric {metric!r} does not apply to {task}')
        predictors = cfg['features'] or [n for n in data.schema.names if n != target]
        if target in predictors:
            raise ConfigError('The target field cannot also be a feature')
        prep_rows = self._prepare(data, info, predictors)
        info['estimator'] = {'task': task, 'algorithm': None, 'target': target, 'features': cfg['features'], 'params': {'seed': cfg['seed']}}
        y, ymask, classes = ml._target(data, target, task)
        if task == 'classification':
            if len(classes) < 2:
                raise DataError('Classification needs at least two target classes')
            if metric == 'auc' and len(classes) != 2:
                raise ConfigError('AUC needs a two-class target')
        if int(ymask.sum()) < cfg['folds'] * 2:
            raise DataError(f'Not enough records with a target for {cfg["folds"]}-fold cross-validation')
        label, higher = _METRICS[metric]
        results = []
        for algorithm in candidates:
            trial = {**info, 'estimator': {**info['estimator'], 'algorithm': algorithm}}
            try:
                scores = ml.cross_validate(data, trial, cfg['folds'], cfg['seed'])
                results.append((algorithm, scores, None))
            except (ConfigError, DataError, ValueError) as e:
                results.append((algorithm, {}, str(e)))
        scored = [r for r in results if r[1].get(label) is not None]
        if not scored:
            raise DataError('No algorithm could be cross-validated: ' + '; '.join(f'{a}: {n}' for a, _, n in results if n))
        scored.sort(key=lambda r: (-r[1][label] if higher else r[1][label], candidates.index(r[0])))
        best = scored[0][0]
        order = [r[0] for r in scored] + [r[0] for r in results if r[1].get(label) is None]
        board = []
        for rank, algorithm in enumerate(order, 1):
            _, scores, note = next(r for r in results if r[0] == algorithm)
            board.append((rank, ml.ALGORITHM_NAMES[algorithm], algorithm == best, label, scores.get(label),
                          *[scores.get(n) for n in ('Accuracy', 'MacroF1', 'AUC', 'LogLoss', 'RMSE', 'MAE', 'R2')], note))
        info['estimator']['algorithm'] = best
        model_info, estimator = _fit(data, info, ctx.warn)
        name = cfg['model_name'] or f'Assisted {ml.ALGORITHM_NAMES[best]}'
        return {'model': _fit_outputs(name, model_info, estimator)['model'],
                'leaderboard': Table.from_rows(self.LEADERBOARD, board),
                'preparation': Table.from_rows(self.PREPARATION, prep_rows),
                'pipeline': ml.write_stream(data, info)}

    def _prepare(self, data, info, predictors):
        t = info['transform']
        ftypes = ml.feature_types(data, info)
        rows = []
        for name in predictors:
            if name in t['exclude']:
                rows.append((name, ftypes[name], 'Excluded by Transformation'))
                continue
            kind = ml.kind_of(name, ftypes, t)
            values = data.column(name)
            if name not in t['types'] and kind != 'id' and _id_like(data.schema.field(name), values):
                t['types'][name] = 'ID'
                kind = 'id'
            if kind == 'id':
                rows.append((name, ftypes[name], 'Dropped: identifier'))
                continue
            if kind in ('datetime', 'text') and name not in t['one_hot']:
                rows.append((name, ftypes[name], 'Dropped: dates and free text need Build Features first'))
                continue
            if kind == 'categorical' and name not in t['one_hot']:
                distinct = len({format_value(v) for v in values if not missing(v)})
                if distinct > 50:
                    t['exclude'] = sorted(set(t['exclude']) | {name})
                    rows.append((name, ftypes[name], f'Dropped: {distinct} categories (more than 50)'))
                    continue
            steps = []
            if any(missing(v) for v in values) and name not in t['impute']:
                method = 'median' if kind == 'numeric' else 'mode'
                t['impute'][name] = method
                steps.append(f'Fill missing values with the {method}')
            if kind == 'categorical' or name in t['one_hot']:
                if name not in t['one_hot']:
                    t['one_hot'] = sorted(set(t['one_hot']) | {name})
                steps.append('One-hot encode')
            rows.append((name, ftypes[name], '; '.join(steps) or 'Use as is'))
        return rows


TOOLS = [FeatureTypes, DataHealth, BuildFeatures, Transformation, Classification, Regression, Fit, Predict, AssistedModeling]
