"""Advanced Data Investigation kernels with real computed statistics and rendered charts.

Each graphical tool returns its numbers as ordinary tables and a ``chart`` anchor holding a rendered
SVG (``flowengine.charts``), which the desktop shows as an image. Methods are standard, documented
statistics and are not the proprietary Alteryx/R macro implementations. Each tool states its method.
"""
from __future__ import annotations

import math
from collections import defaultdict

from .. import charts
from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Schema
from ._common import require_fields
from .investigation import _FIELDS, _dbl, _int, _label, _missing, _np, _text

CHART = Anchor('chart', 'Chart', description='Rendered SVG chart (shown as an image in the results panel)')
_SIZE = {'width': {'type': 'integer', 'minimum': 240, 'maximum': 4000, 'default': 720},
         'height': {'type': 'integer', 'minimum': 180, 'maximum': 4000, 'default': 420},
         'title': {'type': ['string', 'null'], 'default': None}}
_MAX_GROUPS = 60


def _numeric_field(schema, name, tool):
    require_fields(schema, [name])
    if not schema.field(name).type.is_numeric:
        raise SchemaError(f'{tool} field {name!r} must be numeric')


def _groups(values, keep_nulls=True):
    labels = [_label(v) for v in values]
    order = sorted(set(labels), key=lambda v: (v == '[Null]', v))
    if not keep_nulls:
        order = [g for g in order if g != '[Null]']
    return labels, order


def _float(v):
    return float('nan') if _missing(v) else float(v)


class AssociationAnalysis(Tool):
    SCHEMA = Schema([_text('Field1'), _text('Field2'), _text('Measure'), _dbl('Value'), _dbl('Strength'),
                     _dbl('PValue'), _int('N')])
    manifest = ToolManifest(
        tool_id='core.association_analysis', version='1.0.0', display_name='Association Analysis',
        catalog_ids=('data-investigation:association-analysis',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Associations'), CHART),
        description='Pairwise association between fields with a measure suited to each pair of types: '
                    "Pearson r for two numeric fields, Cramer's V (chi-square test) for two categorical fields, "
                    'and the correlation ratio eta (one-way ANOVA F test) for numeric with categorical. Strength '
                    'is the 0-1 magnitude used in the heat map. Missing values are removed pairwise.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'], 'properties': {
            'fields': {**_FIELDS, 'minItems': 2},
            'target_field': {'type': ['string', 'null'], 'default': None,
                             'description': 'Only report pairs that include this field'},
            **_SIZE}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], cfg['fields'] + ([cfg['target_field']] if cfg['target_field'] else []))
        return {'output': self.SCHEMA, 'chart': charts.chart_schema()}

    @staticmethod
    def _pair(np, stats, a, a_num, b, b_num):
        keep = [i for i in range(len(a)) if not (_missing(a[i]) or _missing(b[i]))]
        n = len(keep)
        if a_num and b_num:
            x = np.array([float(a[i]) for i in keep])
            y = np.array([float(b[i]) for i in keep])
            if n < 3 or np.ptp(x) == 0 or np.ptp(y) == 0:
                return 'pearson', None, None, None, n
            r, p = stats.pearsonr(x, y)
            return 'pearson', float(r), abs(float(r)), float(p), n
        if not a_num and not b_num:
            ra, rb = [_label(a[i]) for i in keep], [_label(b[i]) for i in keep]
            la, lb = sorted(set(ra)), sorted(set(rb))
            if len(la) < 2 or len(lb) < 2:
                return 'cramers_v', None, None, None, n
            obs = np.zeros((len(la), len(lb)))
            ia, ib = {v: i for i, v in enumerate(la)}, {v: i for i, v in enumerate(lb)}
            for u, v in zip(ra, rb):
                obs[ia[u], ib[v]] += 1
            chi2, p, _, _ = stats.chi2_contingency(obs, correction=False)
            v = math.sqrt(chi2 / (n * (min(obs.shape) - 1)))
            return 'cramers_v', v, v, float(p), n
        num, cat = (a, b) if a_num else (b, a)
        values = np.array([float(num[i]) for i in keep])
        labels = [_label(cat[i]) for i in keep]
        groups = defaultdict(list)
        for v, g in zip(values, labels):
            groups[g].append(v)
        if len(groups) < 2 or n <= len(groups) or np.ptp(values) == 0:
            return 'correlation_ratio', None, None, None, n
        mean = values.mean()
        ss_between = sum(len(g) * (np.mean(g) - mean) ** 2 for g in groups.values())
        ss_total = float(((values - mean) ** 2).sum())
        eta = math.sqrt(ss_between / ss_total)
        p = float(stats.f_oneway(*[np.array(g) for g in groups.values()]).pvalue)
        return 'correlation_ratio', eta, eta, p if math.isfinite(p) else None, n

    def run(self, ctx, cfg, inputs):
        np = _np()
        from scipy import stats
        table = inputs['input']
        names = list(cfg['fields'])
        if cfg['target_field'] and cfg['target_field'] not in names:
            names.append(cfg['target_field'])
        require_fields(table.schema, names)
        cols = {n: table.column(n) for n in names}
        numeric = {n: table.schema.field(n).type.is_numeric for n in names}
        rows, strength = [], {}
        for i, f1 in enumerate(names):
            for f2 in names[i + 1:]:
                if cfg['target_field'] and cfg['target_field'] not in (f1, f2):
                    continue
                ctx.check_cancelled()
                measure, value, s, p, n = self._pair(np, stats, cols[f1], numeric[f1], cols[f2], numeric[f2])
                rows.append((f1, f2, measure, value, s, p, n))
                strength[(f1, f2)] = strength[(f2, f1)] = s
        matrix = [[1.0 if a == b else strength.get((a, b)) for b in names] for a in names]
        title = cfg['title'] or 'Association strength'
        canvas = charts.heatmap_chart(names, names, matrix, palette='blues', title=title, value_label='Strength',
                                      lo=0.0, hi=1.0, width=cfg['width'], height=max(cfg['height'], 300))
        return {'output': Table.from_rows(self.SCHEMA, rows), 'chart': charts.chart_table(canvas, title)}


class HeatPlot(Tool):
    SCHEMA = Schema([_text('X'), _text('Y'), _dbl('XLower'), _dbl('XUpper'), _dbl('YLower'), _dbl('YUpper'),
                     _int('Count'), _dbl('Value')])
    manifest = ToolManifest(
        tool_id='core.heat_plot', version='1.0.0', display_name='Heat Plot',
        catalog_ids=('data-investigation:heat-plot',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Grid'), CHART),
        description='Two-dimensional heat map. Numeric x/y fields are cut into equal-width bins; text fields '
                    'are used as categories. Each cell aggregates value_field (count, sum, mean or median, or '
                    'the record count when no value field is set). Empty cells are left blank. Colours follow '
                    'the chosen palette.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['x_field', 'y_field'], 'properties': {
            'x_field': {'type': 'string', 'minLength': 1}, 'y_field': {'type': 'string', 'minLength': 1},
            'value_field': {'type': ['string', 'null'], 'default': None},
            'aggregation': {'enum': ['count', 'sum', 'mean', 'median'], 'default': 'count'},
            'x_bins': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 10},
            'y_bins': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 10},
            'palette': {'enum': sorted(charts.PALETTES), 'default': 'viridis'}, **_SIZE}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['x_field'], cfg['y_field']] + ([cfg['value_field']] if cfg['value_field'] else []))
        if cfg['aggregation'] != 'count' and not cfg['value_field']:
            raise ConfigError(f"Heat Plot aggregation {cfg['aggregation']!r} needs value_field")
        if cfg['value_field'] and cfg['aggregation'] != 'count':
            _numeric_field(schema, cfg['value_field'], 'Heat Plot value')
        return {'output': self.SCHEMA, 'chart': charts.chart_schema()}

    @staticmethod
    def _axis(np, field, values, bins):
        """Return (labels, bounds, index-per-record) for one axis; index None = excluded."""
        if field.type.is_numeric:
            arr = np.array([_float(v) for v in values])
            finite = arr[np.isfinite(arr)]
            if not finite.size:
                return [], [], [None] * len(values)
            lo, hi = float(finite.min()), float(finite.max())
            if lo == hi:
                lo, hi = lo - 0.5, hi + 0.5
            edges = np.linspace(lo, hi, bins + 1)
            idx = [None if not math.isfinite(v) else min(int(np.searchsorted(edges, v, side='right')) - 1, bins - 1)
                   for v in arr]
            bounds = [(float(edges[i]), float(edges[i + 1])) for i in range(bins)]
            labels = [f'{a:.4g}–{b:.4g}' for a, b in bounds]
            return labels, bounds, idx
        labels, order = _groups(values, keep_nulls=False)
        pos = {g: i for i, g in enumerate(order)}
        return order, [(None, None)] * len(order), [pos.get(g) for g in labels]

    def run(self, ctx, cfg, inputs):
        np = _np()
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        xl, xb, xi = self._axis(np, table.schema.field(cfg['x_field']), table.column(cfg['x_field']), cfg['x_bins'])
        yl, yb, yi = self._axis(np, table.schema.field(cfg['y_field']), table.column(cfg['y_field']), cfg['y_bins'])
        if len(xl) > 200 or len(yl) > 200:
            raise DataError('Heat Plot categorical axes are limited to 200 categories')
        values = table.column(cfg['value_field']) if cfg['value_field'] else [1] * len(table)
        cells = defaultdict(list)
        counts = defaultdict(int)
        for a, b, v in zip(xi, yi, values):
            if a is None or b is None:
                continue
            counts[(a, b)] += 1
            if not _missing(v):
                cells[(a, b)].append(float(v))
        agg = cfg['aggregation']

        def reduce(vals, count):
            if agg == 'count':
                return float(count)
            if not vals:
                return None
            return float(sum(vals)) if agg == 'sum' else float(np.mean(vals)) if agg == 'mean' else float(np.median(vals))
        rows, matrix = [], [[None] * len(xl) for _ in yl]
        for b in range(len(yl)):
            for a in range(len(xl)):
                count = counts.get((a, b), 0)
                value = reduce(cells.get((a, b), []), count) if count else None
                matrix[len(yl) - 1 - b][a] = value  # highest y bin at the top
                rows.append((xl[a], yl[b], xb[a][0], xb[a][1], yb[b][0], yb[b][1], count, value))
        title = cfg['title'] or f"{cfg['y_field']} by {cfg['x_field']}"
        canvas = charts.heatmap_chart(xl, list(reversed(yl)), matrix, palette=cfg['palette'], title=title,
                                      x_label=cfg['x_field'], y_label=cfg['y_field'],
                                      value_label=agg if not cfg['value_field'] else f"{agg} of {cfg['value_field']}",
                                      width=cfg['width'], height=max(cfg['height'], 300))
        return {'output': Table.from_rows(self.SCHEMA, rows), 'chart': charts.chart_table(canvas, title)}


def _entropy(np, labels):
    _, counts = np.unique(labels, return_counts=True)
    p = counts / counts.sum()
    return float(-(p * np.log2(p)).sum())


class ImportanceWeights(Tool):
    SCHEMA = Schema([_int('Rank'), _text('Field'), _text('Method'), _dbl('Weight'), _dbl('NormalizedWeight'), _int('N')])
    manifest = ToolManifest(
        tool_id='core.importance_weights', version='1.0.0', display_name='Importance Weights',
        catalog_ids=('data-investigation:importance-weights',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Weights'), CHART),
        description='Rank predictor fields by their information about a target field: mutual information (bits), '
                    'gain ratio (mutual information / predictor entropy) or symmetrical uncertainty. Numeric '
                    'fields are discretised into equal-frequency bins; missing predictor values are their own '
                    'category; records with a missing target are excluded. This is a standard information-theory '
                    'method, not the vendor macro algorithm.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['target_field', 'fields'], 'properties': {
            'target_field': {'type': 'string', 'minLength': 1}, 'fields': {**_FIELDS, 'minItems': 1},
            'method': {'enum': ['mutual_information', 'gain_ratio', 'symmetrical_uncertainty'], 'default': 'mutual_information'},
            'bins': {'type': 'integer', 'minimum': 2, 'maximum': 100, 'default': 10}, **_SIZE}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['target_field'], *cfg['fields']])
        if cfg['target_field'] in cfg['fields']:
            raise ConfigError('Importance Weights target_field cannot also be a predictor')
        return {'output': self.SCHEMA, 'chart': charts.chart_schema()}

    @staticmethod
    def _discrete(np, field, values, bins):
        if not field.type.is_numeric:
            return np.array([_label(v) for v in values], dtype=object)
        arr = np.array([_float(v) for v in values])
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.array(['[Null]'] * len(values), dtype=object)
        edges = np.unique(np.quantile(finite, np.linspace(0, 1, bins + 1)))
        codes = np.searchsorted(edges[1:-1], arr, side='right')
        return np.array(['[Null]' if not math.isfinite(v) else f'bin{c}' for v, c in zip(arr, codes)], dtype=object)

    def run(self, ctx, cfg, inputs):
        np = _np()
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        keep = [i for i, v in enumerate(table.column(cfg['target_field'])) if not _missing(v)]
        if len(keep) < 2:
            raise DataError('Importance Weights needs at least two records with a target value')
        sub = table.take(keep)
        target = self._discrete(np, sub.schema.field(cfg['target_field']), sub.column(cfg['target_field']), cfg['bins'])
        h_t = _entropy(np, target)
        results = []
        for name in cfg['fields']:
            ctx.check_cancelled()
            x = self._discrete(np, sub.schema.field(name), sub.column(name), cfg['bins'])
            h_x = _entropy(np, x)
            joint = np.array([f'{a}\x1f{b}' for a, b in zip(x, target)], dtype=object)
            mi = max(0.0, h_x + h_t - _entropy(np, joint))
            if cfg['method'] == 'gain_ratio':
                weight = mi / h_x if h_x > 0 else 0.0
            elif cfg['method'] == 'symmetrical_uncertainty':
                weight = 2 * mi / (h_x + h_t) if h_x + h_t > 0 else 0.0
            else:
                weight = mi
            results.append((name, weight))
        total = sum(w for _, w in results)
        ranked = sorted(results, key=lambda r: (-r[1], r[0]))
        rows = [(i + 1, n, cfg['method'], w, w / total if total else None, len(keep)) for i, (n, w) in enumerate(ranked)]
        title = cfg['title'] or f"Importance for {cfg['target_field']}"
        canvas = charts.bar_chart([r[1] for r in rows], {cfg['method'].replace('_', ' '): [r[3] for r in rows]},
                                  title=title, y_label='Weight', width=cfg['width'], height=cfg['height'])
        return {'output': Table.from_rows(self.SCHEMA, rows), 'chart': charts.chart_table(canvas, title)}


class PlotOfMeans(Tool):
    MEANS = Schema([_text('Group'), _int('N'), _dbl('Mean'), _dbl('StdDev'), _dbl('StdError'), _dbl('CILower'), _dbl('CIUpper')])
    ANOVA = Schema([_int('Groups'), _int('N'), _dbl('F'), _int('DFBetween'), _int('DFWithin'), _dbl('PValue')])
    manifest = ToolManifest(
        tool_id='core.plot_of_means', version='1.0.0', display_name='Plot of Means',
        catalog_ids=('data-investigation:plot-of-means',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Means'), Anchor('anova', 'ANOVA', description='One-way ANOVA across groups'), CHART),
        description='Mean of value_field per group with sample standard deviation, standard error and a '
                    'Student-t confidence interval, plus a one-way ANOVA F test across groups. The chart plots each '
                    'mean with its interval. Missing values and missing groups are excluded.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['value_field', 'group_field'], 'properties': {
            'value_field': {'type': 'string', 'minLength': 1}, 'group_field': {'type': 'string', 'minLength': 1},
            'confidence': {'type': 'number', 'exclusiveMinimum': 0, 'exclusiveMaximum': 1, 'default': 0.95}, **_SIZE}})

    def infer(self, ctx, cfg, inputs):
        _numeric_field(inputs['input'], cfg['value_field'], 'Plot of Means value')
        require_fields(inputs['input'], [cfg['group_field']])
        return {'output': self.MEANS, 'anova': self.ANOVA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        np = _np()
        from scipy import stats
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        groups = defaultdict(list)
        for g, v in zip(table.column(cfg['group_field']), table.column(cfg['value_field'])):
            if not (_missing(g) or _missing(v)) and math.isfinite(float(v)):
                groups[_label(g)].append(float(v))
        order = sorted(groups)
        if len(order) > _MAX_GROUPS:
            raise DataError(f'Plot of Means supports at most {_MAX_GROUPS} groups; found {len(order)}')
        rows = []
        for g in order:
            arr = np.array(groups[g])
            n, mean = arr.size, float(arr.mean())
            sd = float(arr.std(ddof=1)) if n > 1 else None
            se = sd / math.sqrt(n) if sd is not None else None
            half = float(stats.t.ppf(0.5 + cfg['confidence'] / 2, n - 1)) * se if se is not None else None
            rows.append((g, n, mean, sd, se, mean - half if half is not None else None, mean + half if half is not None else None))
        usable = [np.array(groups[g]) for g in order if len(groups[g]) > 0]
        total = sum(a.size for a in usable)
        if len(usable) >= 2 and total > len(usable):
            res = stats.f_oneway(*usable)
            f_val = float(res.statistic) if math.isfinite(res.statistic) else None
            p_val = float(res.pvalue) if math.isfinite(res.pvalue) else None
            anova = [(len(usable), total, f_val, len(usable) - 1, total - len(usable), p_val)]
        else:
            anova = [(len(usable), total, None, None, None, None)]
        title = cfg['title'] or f"Mean {cfg['value_field']} by {cfg['group_field']} ({cfg['confidence']:.0%} CI)"
        canvas = charts.means_chart([r[0] for r in rows], [r[2] for r in rows], [r[5] for r in rows], [r[6] for r in rows],
                                    title=title, x_label=cfg['group_field'], y_label=cfg['value_field'],
                                    width=cfg['width'], height=cfg['height'])
        return {'output': Table.from_rows(self.MEANS, rows), 'anova': Table.from_rows(self.ANOVA, anova),
                'chart': charts.chart_table(canvas, title)}


class Scatterplot(Tool):
    TREND = Schema([_text('Group'), _int('N'), _dbl('Slope'), _dbl('Intercept'), _dbl('RSquared'), _dbl('PValue')])
    manifest = ToolManifest(
        tool_id='core.scatterplot', version='1.0.0', display_name='Scatterplot',
        catalog_ids=('data-investigation:scatterplot',),
        inputs=(Anchor('input', 'Input'),), outputs=(CHART, Anchor('trend', 'Trend', description='Least-squares fit per group')),
        description='Plot y_field against x_field, coloured by an optional group field, with optional '
                    'ordinary least-squares trend lines (slope, intercept, R² and p-value per group). Records with '
                    'missing or non-finite coordinates are excluded. Above max_points, an evenly spaced subset is '
                    'drawn, but the trend uses all records.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['x_field', 'y_field'], 'properties': {
            'x_field': {'type': 'string', 'minLength': 1}, 'y_field': {'type': 'string', 'minLength': 1},
            'group_field': {'type': ['string', 'null'], 'default': None},
            'trend': {'enum': ['none', 'linear'], 'default': 'linear'},
            'max_points': {'type': 'integer', 'minimum': 10, 'maximum': 200000, 'default': 5000}, **_SIZE}})

    def infer(self, ctx, cfg, inputs):
        for name in (cfg['x_field'], cfg['y_field']):
            _numeric_field(inputs['input'], name, 'Scatterplot')
        if cfg['group_field']:
            require_fields(inputs['input'], [cfg['group_field']])
        return {'chart': charts.chart_schema(), 'trend': self.TREND}

    def run(self, ctx, cfg, inputs):
        np = _np()
        from scipy import stats
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        xs, ys = [_float(v) for v in table.column(cfg['x_field'])], [_float(v) for v in table.column(cfg['y_field'])]
        gs = [_label(v) for v in table.column(cfg['group_field'])] if cfg['group_field'] else ['All'] * len(table)
        groups = defaultdict(list)
        for x, y, g in zip(xs, ys, gs):
            if math.isfinite(x) and math.isfinite(y):
                groups[g].append((x, y))
        order = sorted(groups, key=lambda v: (v == '[Null]', v))
        if len(order) > _MAX_GROUPS:
            raise DataError(f'Scatterplot supports at most {_MAX_GROUPS} groups; found {len(order)}')
        trend_rows, lines = [], {}
        for g in order:
            pts = np.array(groups[g])
            if cfg['trend'] == 'linear' and len(pts) >= 3 and np.ptp(pts[:, 0]) > 0:
                fit = stats.linregress(pts[:, 0], pts[:, 1])
                lines[g] = (float(fit.slope), float(fit.intercept))
                trend_rows.append((g, len(pts), float(fit.slope), float(fit.intercept), float(fit.rvalue) ** 2,
                                   float(fit.pvalue) if math.isfinite(fit.pvalue) else None))
            elif cfg['trend'] == 'linear':
                trend_rows.append((g, len(pts), None, None, None, None))
        total = sum(len(v) for v in groups.values())
        step = max(1, math.ceil(total / cfg['max_points']))
        drawn = {g: groups[g][::step] for g in order}
        if step > 1:
            ctx.info(f'Scatterplot drew every {step}th point ({total} records)')
        title = cfg['title'] or f"{cfg['y_field']} vs {cfg['x_field']}"
        canvas = charts.scatter_chart(drawn, lines, title=title, x_label=cfg['x_field'], y_label=cfg['y_field'],
                                      width=cfg['width'], height=cfg['height'])
        return {'chart': charts.chart_table(canvas, title), 'trend': Table.from_rows(self.TREND, trend_rows)}


class ViolinPlot(Tool):
    SUMMARY = Schema([_text('Group'), _int('N'), _dbl('Min'), _dbl('Q1'), _dbl('Median'), _dbl('Q3'), _dbl('Max'), _dbl('Bandwidth')])
    DENSITY = Schema([_text('Group'), _dbl('Value'), _dbl('Density')])
    manifest = ToolManifest(
        tool_id='core.violin_plot', version='1.0.0', display_name='Violin Plot',
        catalog_ids=('data-investigation:violin-plot',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('summary', 'Summary'), Anchor('density', 'Density'), CHART),
        description='Gaussian kernel density estimate of value_field per group (Scott or Silverman bandwidth, or '
                    'a fixed bandwidth), drawn as mirrored violins with median and quartile boxes. Density rows '
                    'are evaluated over each group\'s range extended by three bandwidths. Groups with fewer than '
                    'two distinct values have no density.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['value_field'], 'properties': {
            'value_field': {'type': 'string', 'minLength': 1}, 'group_field': {'type': ['string', 'null'], 'default': None},
            'bandwidth': {'enum': ['scott', 'silverman', 'fixed'], 'default': 'scott'},
            'fixed_bandwidth': {'type': ['number', 'null'], 'exclusiveMinimum': 0, 'default': None},
            'grid_points': {'type': 'integer', 'minimum': 16, 'maximum': 2000, 'default': 128}, **_SIZE}})

    def infer(self, ctx, cfg, inputs):
        _numeric_field(inputs['input'], cfg['value_field'], 'Violin Plot value')
        if cfg['group_field']:
            require_fields(inputs['input'], [cfg['group_field']])
        if cfg['bandwidth'] == 'fixed' and not cfg['fixed_bandwidth']:
            raise ConfigError('Violin Plot fixed bandwidth needs fixed_bandwidth')
        return {'summary': self.SUMMARY, 'density': self.DENSITY, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        np = _np()
        from scipy import stats
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        gs = [_label(v) for v in table.column(cfg['group_field'])] if cfg['group_field'] else ['All'] * len(table)
        groups = defaultdict(list)
        for g, v in zip(gs, table.column(cfg['value_field'])):
            fv = _float(v)
            if math.isfinite(fv):
                groups[g].append(fv)
        order = sorted(groups, key=lambda v: (v == '[Null]', v))
        if len(order) > _MAX_GROUPS:
            raise DataError(f'Violin Plot supports at most {_MAX_GROUPS} groups; found {len(order)}')
        summary, density, grids, dens, quarts = [], [], [], [], []
        for g in order:
            arr = np.array(groups[g])
            q = tuple(float(v) for v in np.percentile(arr, [0, 25, 50, 75, 100]))
            bw = None
            grid, d = np.array([q[2]]), np.array([0.0])
            if np.unique(arr).size >= 2:
                method = cfg['bandwidth'] if cfg['bandwidth'] != 'fixed' else cfg['fixed_bandwidth'] / float(arr.std(ddof=1))
                kde = stats.gaussian_kde(arr, bw_method=method)
                bw = float(kde.factor * arr.std(ddof=1))
                grid = np.linspace(q[0] - 3 * bw, q[4] + 3 * bw, cfg['grid_points'])
                d = kde(grid)
                density.extend((g, float(x), float(y)) for x, y in zip(grid, d))
            summary.append((g, int(arr.size), *q, bw))
            grids.append([float(v) for v in grid])
            dens.append([float(v) for v in d])
            quarts.append(q)
        title = cfg['title'] or f"Distribution of {cfg['value_field']}" + (f" by {cfg['group_field']}" if cfg['group_field'] else '')
        canvas = charts.violin_chart(order, grids, dens, quarts, title=title, x_label=cfg['group_field'],
                                     y_label=cfg['value_field'], width=cfg['width'], height=cfg['height'])
        return {'summary': Table.from_rows(self.SUMMARY, summary), 'density': Table.from_rows(self.DENSITY, density),
                'chart': charts.chart_table(canvas, title)}


TOOLS = [AssociationAnalysis, HeatPlot, ImportanceWeights, PlotOfMeans, Scatterplot, ViolinPlot]
