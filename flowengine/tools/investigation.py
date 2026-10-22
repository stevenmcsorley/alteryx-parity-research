"""Independent Data Investigation kernels; no vendor code, report layouts or parity claims.

Functional sources: the public tool help linked in each catalogue contract. Statistics use numpy and
scipy. Sample standard deviation and variance use n-1, as Summarize does. Nulls and NaN are
"missing" throughout and are excluded from numeric statistics. Every output is a structured table
with a static schema, so downstream nodes validate before a run.
"""
from __future__ import annotations

import math
from collections import Counter

from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
_FIELDS = {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1}}


def _text(name):
    return Field(name, FieldType.V_WSTRING, TEXT)


def _int(name):
    return Field(name, FieldType.INT64)


def _dbl(name):
    return Field(name, FieldType.DOUBLE)


def _np():
    import numpy
    return numpy


def _finite(x):
    return None if x is None or not math.isfinite(x) else float(x)


def _selected(schema, cfg, numeric=False, what='field'):
    names = cfg.get('fields') or [f.name for f in schema if not numeric or f.type.is_numeric]
    require_fields(schema, names, what)
    if numeric:
        bad = [n for n in names if not schema.field(n).type.is_numeric]
        if bad:
            raise SchemaError(f'Numeric fields required; not numeric: {bad}')
    return names


def _missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _numbers(values):
    """Finite float array of the non-missing numeric values, plus the count of excluded non-finite values."""
    np = _np()
    arr = np.array([float(v) for v in values if not _missing(v)], dtype=float)
    finite = np.isfinite(arr)
    return arr[finite], int((~finite).sum())


def _label(v):
    return '[Null]' if _missing(v) else format_value(v)


# =========================================================================== shared field statistics

def _field_stats(field, values, top_n, distinct_limit):
    """Type-appropriate statistics for one column; returns a dict keyed by output column name."""
    np = _np()
    n = len(values)
    present = [v for v in values if not _missing(v)]
    distinct, capped = set(), False
    for v in present:
        if len(distinct) >= distinct_limit:
            capped = True
            break
        distinct.add(v)
    out = {'Field': field.name, 'Type': field.type.value, 'Records': n, 'Missing': n - len(present),
           'Present': len(present), 'CompletenessPct': 100.0 * len(present) / n if n else None,
           'Distinct': len(distinct), 'DistinctCapped': capped,
           'Min': None, 'Max': None, 'Mean': None, 'Median': None, 'StdDev': None, 'Variance': None,
           'Q1': None, 'Q3': None, 'MinLength': None, 'MaxLength': None, 'MeanLength': None,
           'Mode': None, 'TopValues': None}
    if field.type.is_numeric:
        arr, _ = _numbers(present)
        if arr.size:
            out.update(Min=format_value(float(arr.min()) if field.type.value in ('float', 'double') else present[int(arr.argmin())]),
                       Max=format_value(float(arr.max()) if field.type.value in ('float', 'double') else present[int(arr.argmax())]),
                       Mean=float(arr.mean()), Median=float(np.median(arr)),
                       Q1=float(np.percentile(arr, 25)), Q3=float(np.percentile(arr, 75)))
            if arr.size > 1:
                out.update(StdDev=float(arr.std(ddof=1)), Variance=float(arr.var(ddof=1)))
    elif present and (field.type.is_temporal or field.type.value == 'bool'):
        out.update(Min=format_value(min(present)), Max=format_value(max(present)))
    elif present and field.type.is_string:
        lengths = np.array([len(v) for v in present])
        out.update(Min=min(present), Max=max(present), MinLength=int(lengths.min()), MaxLength=int(lengths.max()),
                   MeanLength=float(lengths.mean()))
    if present and field.type.value != 'blob':
        common = Counter(present).most_common()
        top = sorted(common, key=lambda kv: (-kv[1], format_value(kv[0])))[:top_n]
        out['Mode'] = format_value(top[0][0])
        out['TopValues'] = '; '.join(f'{format_value(v)} ({c})' for v, c in top)
    return out


def _sample(table, cfg):
    limit = cfg.get('sample_records')
    if limit is None or limit >= len(table):
        return table
    return table.take(range(limit))


class BasicDataProfile(Tool):
    SCHEMA = Schema([_text('Field'), _text('Type'), _int('Records'), _int('Missing'), _dbl('CompletenessPct'),
                     _int('Distinct'), Field('DistinctCapped', FieldType.BOOL), _text('Min'), _text('Max'),
                     _dbl('Mean'), _dbl('Median'), _dbl('StdDev'), _dbl('Variance'), _int('MinLength'),
                     _int('MaxLength'), _dbl('MeanLength'), _text('TopValues')])
    OVERVIEW = Schema([_int('Records'), _int('Fields'), _int('ProfiledRecords'), _int('CompleteRecords'),
                       _dbl('CompletenessPct')])
    manifest = ToolManifest(
        tool_id='core.basic_data_profile', version='1.0.0', display_name='Basic Data Profile',
        catalog_ids=('data-investigation:basic-data-profile',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('profile', 'Profile', description='One row per field'),
                 Anchor('overview', 'Overview', description='Dataset-level counts')),
        description='Profile every (or selected) field: type, missing values, completeness, distinct values, '
                    'numeric min/max/mean/median/standard deviation/variance, text lengths, date ranges and the '
                    'most common values. sample_records profiles only the first N records.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'fields': {**_FIELDS, 'default': []},
            'sample_records': {'type': ['integer', 'null'], 'minimum': 1, 'default': None},
            'top_values': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 5},
            'distinct_limit': {'type': 'integer', 'minimum': 1, 'maximum': 10_000_000, 'default': 1_000_000,
                               'description': 'Stop counting distinct values above this (DistinctCapped)'}}})

    def infer(self, ctx, cfg, inputs):
        _selected(inputs['input'], cfg)
        return {'profile': self.SCHEMA, 'overview': self.OVERVIEW}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        names = _selected(table.schema, cfg)
        sample = _sample(table, cfg)
        rows = []
        for name in names:
            ctx.check_cancelled()
            stats = _field_stats(sample.schema.field(name), sample.column(name), cfg['top_values'], cfg['distinct_limit'])
            rows.append(tuple(stats.get(f.name) for f in self.SCHEMA))
        columns = [sample.column(n) for n in names]
        complete = sum(1 for i in range(len(sample)) if all(not _missing(c[i]) for c in columns))
        overview = [(len(table), len(names), len(sample), complete, 100.0 * complete / len(sample) if len(sample) else None)]
        return {'profile': Table.from_rows(self.SCHEMA, rows), 'overview': Table.from_rows(self.OVERVIEW, overview)}


class FieldSummary(Tool):
    SCHEMA = Schema([_text('Field'), _text('Type'), _int('Count'), _int('Missing'), _dbl('PercentMissing'),
                     _int('Unique'), _text('Min'), _text('Max'), _dbl('Mean'), _dbl('StdDev'), _dbl('Q1'),
                     _dbl('Median'), _dbl('Q3'), _text('Mode'), _int('ShortestLength'), _int('LongestLength')])
    manifest = ToolManifest(
        tool_id='core.field_summary', version='1.0.0', display_name='Field Summary',
        catalog_ids=('data-investigation:field-summary',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Summary', description='One row per field'),),
        description='Per-field summary: count, missing and percent missing, unique values, min/max, mean, sample '
                    'standard deviation and quartiles for numbers, text lengths, and mode. The vendor report and '
                    'interactive outputs are not reproduced.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'fields': {**_FIELDS, 'default': []}}})

    def infer(self, ctx, cfg, inputs):
        _selected(inputs['input'], cfg)
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        rows = []
        for name in _selected(table.schema, cfg):
            ctx.check_cancelled()
            s = _field_stats(table.schema.field(name), table.column(name), 1, 10_000_000)
            rows.append((s['Field'], s['Type'], s['Present'], s['Missing'],
                         100.0 * s['Missing'] / s['Records'] if s['Records'] else None, s['Distinct'], s['Min'],
                         s['Max'], s['Mean'], s['StdDev'], s['Q1'], s['Median'], s['Q3'], s['Mode'], s['MinLength'],
                         s['MaxLength']))
        return {'output': Table.from_rows(self.SCHEMA, rows)}


class FrequencyTable(Tool):
    SCHEMA = Schema([_text('Field'), _text('Value'), _int('Frequency'), _dbl('Percent'),
                     _int('CumulativeFrequency'), _dbl('CumulativePercent')])
    manifest = ToolManifest(
        tool_id='core.frequency_table', version='1.0.0', display_name='Frequency Table',
        catalog_ids=('data-investigation:frequency-table',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Frequencies'),),
        description='Count each distinct value of each selected field with percentages and cumulative totals. '
                    'Nulls are a [Null] category unless excluded; max_categories folds the rest into (Other), so '
                    'frequencies always add up to the counted records.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'], 'properties': {
            'fields': {**_FIELDS, 'minItems': 1},
            'sort': {'enum': ['frequency_desc', 'frequency_asc', 'value_asc', 'value_desc'], 'default': 'frequency_desc'},
            'include_nulls': {'type': 'boolean', 'default': True},
            'max_categories': {'type': ['integer', 'null'], 'minimum': 1, 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], cfg['fields'])
        return {'output': self.SCHEMA}

    @staticmethod
    def _order(counts, mode):
        def value_key(item):
            v = item[0]
            return (1, '') if v is None else (0, v) if not isinstance(v, bool) else (0, int(v))
        if mode.startswith('value'):
            try:
                return sorted(counts, key=value_key, reverse=mode == 'value_desc')
            except TypeError:  # mixed Python types in one column: compare as text
                return sorted(counts, key=lambda kv: _label(kv[0]), reverse=mode == 'value_desc')
        sign = -1 if mode == 'frequency_desc' else 1
        return sorted(counts, key=lambda kv: (sign * kv[1], _label(kv[0])))

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        require_fields(table.schema, cfg['fields'])
        rows = []
        for name in cfg['fields']:
            values = [None if _missing(v) else v for v in table.column(name)]
            if not cfg['include_nulls']:
                values = [v for v in values if v is not None]
            total = len(values)
            ordered = self._order(list(Counter(values).items()), cfg['sort'])
            limit = cfg['max_categories']
            if limit and len(ordered) > limit:
                rest = sum(c for _, c in ordered[limit:])
                ordered = ordered[:limit] + [('(Other)', rest)]
            running = 0
            for value, count in ordered:
                running += count
                rows.append((name, value if value == '(Other)' and limit else _label(value), count,
                             100.0 * count / total, running, 100.0 * running / total))
        return {'output': Table.from_rows(self.SCHEMA, rows)}


class Histogram(Tool):
    SCHEMA = Schema([_int('Bin'), _dbl('LowerBound'), _dbl('UpperBound'), _int('Count'), _dbl('RelativeFrequency'),
                     _dbl('Density'), _int('CumulativeCount')])
    manifest = ToolManifest(
        tool_id='core.histogram', version='1.0.0', display_name='Histogram',
        catalog_ids=('data-investigation:histogram',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Bins'), Anchor('chart', 'Chart', description='Rendered SVG histogram')),
        description='Bin one numeric field into equal-width intervals: a fixed bin count or automatic rules '
                    '(Sturges, Freedman-Diaconis, square root). Bins are [lower, upper) except the last, which '
                    'includes its upper bound. Nulls, non-finite values and values outside range_min/range_max are '
                    'excluded and reported. Density integrates to 1.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {
            'field': {'type': 'string', 'minLength': 1},
            'binning': {'enum': ['auto', 'sturges', 'fd', 'sqrt', 'fixed'], 'default': 'auto'},
            'bins': {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 10},
            'range_min': {'type': ['number', 'null'], 'default': None},
            'range_max': {'type': ['number', 'null'], 'default': None},
            'title': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['field']])
        if not schema.field(cfg['field']).type.is_numeric:
            raise SchemaError(f"Histogram field {cfg['field']!r} must be numeric")
        if cfg['range_min'] is not None and cfg['range_max'] is not None and cfg['range_min'] >= cfg['range_max']:
            raise ConfigError('Histogram range_min must be less than range_max')
        from .. import charts
        return {'output': self.SCHEMA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        np = _np()
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        values = table.column(cfg['field'])
        arr, nonfinite = _numbers(values)
        nulls = sum(1 for v in values if _missing(v))
        lo = cfg['range_min'] if cfg['range_min'] is not None else (float(arr.min()) if arr.size else None)
        hi = cfg['range_max'] if cfg['range_max'] is not None else (float(arr.max()) if arr.size else None)
        inside = arr[(arr >= lo) & (arr <= hi)] if arr.size else arr
        outside = int(arr.size - inside.size)
        from .. import charts
        title = cfg['title'] or f"Distribution of {cfg['field']}"
        if inside.size == 0:
            ctx.warn('Histogram has no values to bin', nulls=nulls)
            return {'output': Table.empty(self.SCHEMA), 'chart': charts.chart_table(charts.histogram_chart([0, 1], [0], title=title), title)}
        if lo == hi:  # a single distinct value: one unit-width bin centred on it
            lo, hi = lo - 0.5, hi + 0.5
        method = {'auto': 'auto', 'sturges': 'sturges', 'fd': 'fd', 'sqrt': 'sqrt'}.get(cfg['binning'])
        edges = (np.linspace(lo, hi, cfg['bins'] + 1) if method is None
                 else np.histogram_bin_edges(inside, bins=method, range=(lo, hi)))
        counts, edges = np.histogram(inside, bins=edges)
        widths = np.diff(edges)
        total = int(counts.sum())
        cumulative = np.cumsum(counts)
        rows = [(i + 1, float(edges[i]), float(edges[i + 1]), int(c), c / total, c / (total * widths[i]), int(cumulative[i]))
                for i, c in enumerate(counts)]
        if nulls or nonfinite or outside:
            ctx.info(f'Histogram excluded {nulls} null, {nonfinite} non-finite and {outside} out-of-range value(s)')
        canvas = charts.histogram_chart([float(e) for e in edges], [int(c) for c in counts], title=title, x_label=cfg['field'])
        return {'output': Table.from_rows(self.SCHEMA, rows), 'chart': charts.chart_table(canvas, title)}


class SpearmanCorrelation(Tool):
    SCHEMA = Schema([_text('Field1'), _text('Field2'), _dbl('Spearman'), _dbl('PValue'), _int('N')])
    manifest = ToolManifest(
        tool_id='core.spearman_correlation', version='1.0.0', display_name='Spearman Correlation',
        catalog_ids=('data-investigation:spearman-correlation',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Matrix'),),
        description='Spearman rank correlation for every pair of the selected numeric fields, with tied values '
                    'given average ranks, a two-sided p-value and the number of records used. Nulls are dropped '
                    'pairwise or listwise. Pairs with a constant field, or fewer than three records, have null '
                    'coefficients.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'], 'properties': {
            'fields': {**_FIELDS, 'minItems': 2},
            'missing': {'enum': ['pairwise', 'listwise'], 'default': 'pairwise'}}})

    def infer(self, ctx, cfg, inputs):
        _selected(inputs['input'], cfg, numeric=True)
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        np = _np()
        from scipy import stats
        table = inputs['input']
        names = _selected(table.schema, cfg, numeric=True)
        cols = [np.array([np.nan if _missing(v) else float(v) for v in table.column(n)]) for n in names]
        keep_all = np.all([np.isfinite(c) for c in cols], axis=0) if cols else None
        rows = []
        for i, a in enumerate(cols):
            for j, b in enumerate(cols):
                mask = keep_all if cfg['missing'] == 'listwise' else np.isfinite(a) & np.isfinite(b)
                x, y = a[mask], b[mask]
                n = int(mask.sum())
                if i == j:
                    rho, p = (1.0, 0.0) if n >= 3 and np.ptp(x) > 0 else (None, None)
                elif n < 3 or np.ptp(x) == 0 or np.ptp(y) == 0:
                    rho, p = None, None
                else:
                    result = stats.spearmanr(x, y)
                    rho, p = _finite(result.statistic), _finite(result.pvalue)
                rows.append((names[i], names[j], rho, p, n))
        return {'output': Table.from_rows(self.SCHEMA, rows)}


class ContingencyTable(Tool):
    TABLE = Schema([_text('Row'), _text('Column'), _int('Observed'), _dbl('Expected'), _dbl('RowPercent'),
                    _dbl('ColumnPercent'), _dbl('TotalPercent')])
    STATS = Schema([_int('N'), _int('Rows'), _int('Columns'), _dbl('ChiSquare'), _int('DegreesOfFreedom'),
                    _dbl('PValue'), _dbl('CramersV'), _dbl('MinExpected'), Field('YatesCorrection', FieldType.BOOL)])
    manifest = ToolManifest(
        tool_id='core.contingency_table', version='1.0.0', display_name='Contingency Table',
        catalog_ids=('data-investigation:contingency-table',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('table', 'Table', description='Observed and expected counts with proportions and (Total) margins'),
                 Anchor('statistics', 'Statistics', description="Pearson chi-square test and Cramer's V")),
        description='Cross-tabulate two categorical fields: observed and expected counts, row, column and total '
                    "percentages, (Total) margins, and Pearson's chi-square test of independence with Cramer's V. "
                    'Nulls are a [Null] category unless excluded. Expected counts below 5 are reported as MinExpected.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['row_field', 'column_field'],
                       'properties': {'row_field': {'type': 'string', 'minLength': 1},
                                      'column_field': {'type': 'string', 'minLength': 1},
                                      'include_nulls': {'type': 'boolean', 'default': False},
                                      'yates_correction': {'type': 'boolean', 'default': False,
                                                           'description': 'Continuity correction for 2x2 tables'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['row_field'], cfg['column_field']])
        if cfg['row_field'] == cfg['column_field']:
            raise ConfigError('Contingency Table needs two different fields')
        return {'table': self.TABLE, 'statistics': self.STATS}

    def run(self, ctx, cfg, inputs):
        np = _np()
        from scipy import stats
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        pairs = [(_label(r), _label(c)) for r, c in zip(table.column(cfg['row_field']), table.column(cfg['column_field']))
                 if cfg['include_nulls'] or not (_missing(r) or _missing(c))]
        rows_l = sorted({r for r, _ in pairs}, key=lambda v: (v == '[Null]', v))
        cols_l = sorted({c for _, c in pairs}, key=lambda v: (v == '[Null]', v))
        observed = np.zeros((len(rows_l), len(cols_l)), dtype=np.int64)
        ri, ci = {v: i for i, v in enumerate(rows_l)}, {v: i for i, v in enumerate(cols_l)}
        for r, c in pairs:
            observed[ri[r], ci[c]] += 1
        n = int(observed.sum())
        if n == 0:
            return {'table': Table.empty(self.TABLE),
                    'statistics': Table.from_rows(self.STATS, [(0, 0, 0, None, None, None, None, None, False)])}
        row_tot, col_tot = observed.sum(axis=1), observed.sum(axis=0)
        expected = np.outer(row_tot, col_tot) / n
        out = []
        for i, r in enumerate(rows_l):
            for j, c in enumerate(cols_l):
                o = int(observed[i, j])
                out.append((r, c, o, float(expected[i, j]), 100.0 * o / row_tot[i], 100.0 * o / col_tot[j], 100.0 * o / n))
            out.append((r, '(Total)', int(row_tot[i]), float(row_tot[i]), 100.0, None, 100.0 * row_tot[i] / n))
        for j, c in enumerate(cols_l):
            out.append(('(Total)', c, int(col_tot[j]), float(col_tot[j]), None, 100.0, 100.0 * col_tot[j] / n))
        out.append(('(Total)', '(Total)', n, float(n), 100.0, 100.0, 100.0))
        r_count, c_count = len(rows_l), len(cols_l)
        yates = bool(cfg['yates_correction'] and r_count == 2 and c_count == 2)
        if r_count > 1 and c_count > 1:
            chi2, p, dof, _ = stats.chi2_contingency(observed, correction=yates)
            cramers = math.sqrt(chi2 / (n * (min(r_count, c_count) - 1)))
            stat_row = (n, r_count, c_count, float(chi2), int(dof), float(p), cramers, float(expected.min()), yates)
            if expected.min() < 5:
                ctx.warn(f'Some expected counts are below 5 (minimum {expected.min():.3g}); the chi-square approximation may be unreliable')
        else:
            stat_row = (n, r_count, c_count, None, None, None, None, float(expected.min()), False)
        return {'table': Table.from_rows(self.TABLE, out), 'statistics': Table.from_rows(self.STATS, [stat_row])}


# scipy name, display name, parameter names, requires strictly positive data, fixed location
_DISTRIBUTIONS = {
    'normal': ('norm', 'Normal', ('mean', 'std_dev'), False),
    'lognormal': ('lognorm', 'Lognormal', ('shape', 'scale'), True),
    'exponential': ('expon', 'Exponential', ('scale',), True),
    'gamma': ('gamma', 'Gamma', ('shape', 'scale'), True),
    'weibull': ('weibull_min', 'Weibull', ('shape', 'scale'), True),
    'uniform': ('uniform', 'Uniform', ('lower', 'width'), False),
}


class DistributionAnalysis(Tool):
    FITS = Schema([_int('Rank'), _text('Distribution'), _text('Status'), _text('Param1Name'), _dbl('Param1'),
                   _text('Param2Name'), _dbl('Param2'), _dbl('LogLikelihood'), _dbl('AIC'), _dbl('BIC'),
                   _dbl('KSStatistic'), _dbl('KSPValue'), _dbl('AndersonDarling')])
    MOMENTS = Schema([_int('N'), _int('Excluded'), _dbl('Mean'), _dbl('StdDev'), _dbl('Skewness'),
                      _dbl('ExcessKurtosis'), _dbl('Min'), _dbl('Max')])
    manifest = ToolManifest(
        tool_id='core.distribution_analysis', version='1.0.0', display_name='Distribution Analysis',
        catalog_ids=('data-investigation:distribution-analysis',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('fits', 'Fits', description='Fitted distributions ranked by AIC'),
                 Anchor('moments', 'Moments', description='Sample moments of the field')),
        description='Fit candidate distributions to one numeric field by maximum likelihood (normal, lognormal, '
                    'exponential, gamma, Weibull, uniform) and rank them by AIC, with log-likelihood, BIC, '
                    'Kolmogorov-Smirnov and Anderson-Darling statistics. Positive-only distributions use location '
                    '0 and are not applicable to data with zero or negative values. KS p-values are approximate '
                    'because parameters are estimated from the same data.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {
            'field': {'type': 'string', 'minLength': 1},
            'distributions': {'type': 'array', 'uniqueItems': True, 'minItems': 1,
                              'items': {'enum': list(_DISTRIBUTIONS)}, 'default': list(_DISTRIBUTIONS)}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['field']])
        if not schema.field(cfg['field']).type.is_numeric:
            raise SchemaError(f"Distribution Analysis field {cfg['field']!r} must be numeric")
        return {'fits': self.FITS, 'moments': self.MOMENTS}

    def run(self, ctx, cfg, inputs):
        np = _np()
        from scipy import stats
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        values = table.column(cfg['field'])
        x, nonfinite = _numbers(values)
        excluded = len(values) - x.size
        if x.size < 3:
            raise DataError(f'Distribution Analysis needs at least 3 numeric values; found {x.size}')
        moments = [(int(x.size), excluded, float(x.mean()), float(x.std(ddof=1)),
                    _finite(float(stats.skew(x, bias=False))) if np.ptp(x) else None,
                    _finite(float(stats.kurtosis(x, bias=False))) if np.ptp(x) else None,
                    float(x.min()), float(x.max()))]
        fits = []
        for key in cfg['distributions']:
            ctx.check_cancelled()
            scipy_name, display, params, positive = _DISTRIBUTIONS[key]
            if positive and (x <= 0).any():
                fits.append([None, display, 'not applicable: requires values greater than 0'] + [None] * 10)
                continue
            if np.ptp(x) == 0:
                fits.append([None, display, 'not applicable: all values are equal'] + [None] * 10)
                continue
            dist = getattr(stats, scipy_name)
            try:
                fitted = dist.fit(x, floc=0) if positive else dist.fit(x)
            except Exception as e:  # scipy optimiser failure for this family
                fits.append([None, display, f'fit failed: {type(e).__name__}'] + [None] * 10)
                continue
            if positive:
                shape_args = fitted[:-2]
                named = list(shape_args) + [fitted[-1]]  # (shape..., scale); location fixed at 0
            else:
                named = list(fitted)
            loglik = float(np.sum(dist.logpdf(x, *fitted)))
            k = len(named)
            ks = stats.kstest(x, dist(*fitted).cdf)
            ad = None  # scipy provides Anderson-Darling critical tables for these families only
            if key in ('normal', 'exponential'):
                # ``method`` was added to SciPy's anderson API after older supported
                # releases. The statistic itself is available in both signatures.
                import inspect
                anderson_kwargs = {'dist': {'normal': 'norm', 'exponential': 'expon'}[key]}
                if 'method' in inspect.signature(stats.anderson).parameters:
                    anderson_kwargs['method'] = 'interpolate'
                ad = _finite(float(stats.anderson(x, **anderson_kwargs).statistic))
            p1 = (params[0], _finite(named[0]))
            p2 = (params[1], _finite(named[1])) if len(params) > 1 else (None, None)
            fits.append([None, display, 'fitted', p1[0], p1[1], p2[0], p2[1], _finite(loglik),
                         _finite(2 * k - 2 * loglik), _finite(k * math.log(x.size) - 2 * loglik),
                         _finite(float(ks.statistic)), _finite(float(ks.pvalue)), ad])
        ranked = sorted([f for f in fits if f[2] == 'fitted' and f[8] is not None], key=lambda f: f[8])
        for rank, f in enumerate(ranked, start=1):
            f[0] = rank
        ordered = ranked + [f for f in fits if f not in ranked]
        return {'fits': Table.from_rows(self.FITS, [tuple(f) for f in ordered]),
                'moments': Table.from_rows(self.MOMENTS, moments)}


TOOLS = [BasicDataProfile, FieldSummary, FrequencyTable, Histogram, SpearmanCorrelation, ContingencyTable,
         DistributionAnalysis]
