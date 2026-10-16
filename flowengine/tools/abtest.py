"""A/B testing: trend measures, treatment selection, matched controls and difference-in-differences analysis.

Independent implementations of a standard test-and-control design. Units are matched on z-scored
criteria. Analysis compares each treatment unit's growth from the comparison period to the test
period with the average growth of its matched controls (a ratio difference-in-differences). The
confidence interval and p-value come from a one-sample t-test of the lifts across treatment units.
"""
from __future__ import annotations

import datetime as _dt
import math

from .. import charts
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import missing
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields
from .grouping import _numeric_matrix
from .timeseries import _to_datetime

TEXT = 2**30 - 1
_CRITERIA = {'type': 'array', 'uniqueItems': True, 'minItems': 1, 'items': {'type': 'string', 'minLength': 1}}


class ABTrend(Tool):
    SCHEMA = Schema([Field('Unit', FieldType.V_WSTRING, TEXT), Field('Periods', FieldType.INT64), Field('Mean', FieldType.DOUBLE),
                     Field('Trend', FieldType.DOUBLE), Field('TrendPct', FieldType.DOUBLE), Field('Volatility', FieldType.DOUBLE),
                     Field('Seasonality', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.ab_trend', version='1.0.0', display_name='AB Trend', catalog_ids=('ab-testing:ab-trend',),
        inputs=(Anchor('input', 'Performance', description='Unit, date and value records'),), outputs=(Anchor('output', 'Trends'),),
        description='Summarise each unit\'s performance into weekly (or monthly) totals between start and end dates: '
                    'mean, linear trend per period (and as a percentage of the mean), volatility (coefficient of '
                    'variation), and seasonality strength (share of variance explained by period of year). These '
                    'are criteria for AB Treatments and AB Controls.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['unit_field', 'date_field', 'value_field'], 'properties': {
            'unit_field': {'type': 'string', 'minLength': 1}, 'date_field': {'type': 'string', 'minLength': 1},
            'value_field': {'type': 'string', 'minLength': 1}, 'period': {'enum': ['week', 'month'], 'default': 'week'},
            'start': {'type': ['string', 'null'], 'default': None}, 'end': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['unit_field'], cfg['date_field'], cfg['value_field']])
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        start = _to_datetime(cfg['start']) if cfg['start'] else None
        end = _to_datetime(cfg['end']) if cfg['end'] else None
        totals = {}
        for rec in inputs['input'].to_dicts():
            if missing(rec[cfg['unit_field']]) or missing(rec[cfg['date_field']]) or missing(rec[cfg['value_field']]):
                continue
            d = _to_datetime(rec[cfg['date_field']])
            if (start and d < start) or (end and d > end):
                continue
            key = (d - _dt.timedelta(days=d.weekday())).date() if cfg['period'] == 'week' else d.date().replace(day=1)
            unit = format_value(rec[cfg['unit_field']])
            totals.setdefault(unit, {}).setdefault(key, 0.0)
            totals[unit][key] += float(rec[cfg['value_field']])
        rows = []
        for unit in sorted(totals):
            series = sorted(totals[unit].items())
            y = np.array([v for _, v in series])
            mean = float(y.mean())
            trend = float(np.polyfit(np.arange(len(y)), y, 1)[0]) if len(y) >= 2 else None
            seasonal = None
            if len(y) >= 8:
                labels = [k.isocalendar()[1] if cfg['period'] == 'week' else k.month for k, _ in series]
                groups = {}
                for lab, v in zip(labels, y):
                    groups.setdefault(lab % 13 if cfg['period'] == 'week' else lab, []).append(v)
                ss_between = sum(len(v) * (np.mean(v) - mean) ** 2 for v in groups.values())
                ss_total = float(((y - mean) ** 2).sum())
                seasonal = float(ss_between / ss_total) if ss_total else 0.0
            rows.append((unit, len(y), mean, trend, 100 * trend / mean if trend is not None and mean else None,
                         float(y.std(ddof=1) / mean) if len(y) > 1 and mean else None, seasonal))
        return {'output': Table.from_rows(self.SCHEMA, rows)}


class ABTreatments(Tool):
    manifest = ToolManifest(
        tool_id='core.ab_treatments', version='1.0.0', display_name='AB Treatments', catalog_ids=('ab-testing:ab-treatments',),
        inputs=(Anchor('input', 'Candidates'),), outputs=(Anchor('output', 'Treatments'),),
        description='Choose treatment units, per stratum (group_field) when given. representative picks the units '
                    'closest to the stratum centre on z-scored criteria, so the test group resembles the '
                    'population. random draws a seeded simple random sample. Adds Selected and Distance, and '
                    'returns only selected units unless keep_all.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['unit_field', 'criteria', 'count'], 'properties': {
            'unit_field': {'type': 'string', 'minLength': 1}, 'criteria': _CRITERIA, 'count': {'type': 'integer', 'minimum': 1},
            'group_field': {'type': ['string', 'null'], 'default': None},
            'method': {'enum': ['representative', 'random'], 'default': 'representative'},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1}, 'keep_all': {'type': 'boolean', 'default': False}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['unit_field'], *cfg['criteria']] + ([cfg['group_field']] if cfg['group_field'] else []))
        return {'output': Schema([*inputs['input'], Field('Selected', FieldType.BOOL), Field('Distance', FieldType.DOUBLE)])}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        X, mask, _ = _numeric_matrix(table, cfg['criteria'], True)
        strata = [format_value(v) if cfg['group_field'] else '' for v in (table.column(cfg['group_field']) if cfg['group_field'] else [None] * len(table))]
        selected, distance = np.zeros(len(table), dtype=bool), np.full(len(table), np.nan)
        rng = np.random.default_rng(cfg['seed'])
        for s in sorted(set(strata)):
            idx = np.array([i for i, x in enumerate(strata) if x == s and mask[i]])
            if not len(idx):
                continue
            centre = X[idx].mean(axis=0)
            d = np.sqrt(((X[idx] - centre) ** 2).sum(axis=1))
            distance[idx] = d
            if len(idx) < cfg['count']:
                ctx.warn(f'Stratum {s or "(all)"!r} has only {len(idx)} candidates for {cfg["count"]} treatments')
            pick = idx[np.argsort(d, kind='stable')[:cfg['count']]] if cfg['method'] == 'representative' else \
                rng.choice(idx, size=min(cfg['count'], len(idx)), replace=False)
            selected[pick] = True
        rows = [(*row, bool(sel), float(dist) if np.isfinite(dist) else None)
                for row, sel, dist in zip(table.rows(), selected, distance) if cfg['keep_all'] or sel]
        return {'output': Table.from_rows(schema, rows)}


class ABControls(Tool):
    SCHEMA = Schema([Field('Treatment', FieldType.V_WSTRING, TEXT), Field('Control', FieldType.V_WSTRING, TEXT),
                     Field('Rank', FieldType.INT32), Field('Distance', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.ab_controls', version='1.0.0', display_name='AB Controls', catalog_ids=('ab-testing:ab-controls',),
        inputs=(Anchor('treatments', 'Treatments'), Anchor('input', 'Candidates', description='Potential control units')),
        outputs=(Anchor('output', 'Pairs'),),
        description='Match each treatment unit with the controls_per_treatment most similar candidate units (Euclidean '
                    'distance on criteria z-scored over the candidates), never using a treatment unit as a control. '
                    'With unique_controls, a control is used once, assigned greedily by closest pair first. '
                    'match_field restricts matches to the same stratum.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['unit_field', 'criteria'], 'properties': {
            'unit_field': {'type': 'string', 'minLength': 1}, 'criteria': _CRITERIA,
            'controls_per_treatment': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 5},
            'unique_controls': {'type': 'boolean', 'default': False},
            'match_field': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        need = [cfg['unit_field'], *cfg['criteria']] + ([cfg['match_field']] if cfg['match_field'] else [])
        require_fields(inputs['treatments'], need)
        require_fields(inputs['input'], need)
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        treat, cand = inputs['treatments'], inputs['input']
        C, cmask, stats = _numeric_matrix(cand, cfg['criteria'], True)
        T, tmask, _ = _numeric_matrix(treat, cfg['criteria'], False, stats)
        t_ids = [format_value(v) for v in treat.column(cfg['unit_field'])]
        c_ids = [format_value(v) for v in cand.column(cfg['unit_field'])]
        t_strata = [format_value(v) for v in treat.column(cfg['match_field'])] if cfg['match_field'] else [''] * len(treat)
        c_strata = [format_value(v) for v in cand.column(cfg['match_field'])] if cfg['match_field'] else [''] * len(cand)
        treated = set(t_ids)
        pairs = []
        for i in np.flatnonzero(tmask):
            for j in np.flatnonzero(cmask):
                if c_ids[j] in treated or c_strata[j] != t_strata[i]:
                    continue
                pairs.append((float(np.sqrt(((T[i] - C[j]) ** 2).sum())), i, j))
        pairs.sort()
        k, used, chosen = cfg['controls_per_treatment'], set(), {}
        for d, i, j in pairs:
            if len(chosen.get(i, [])) >= k or (cfg['unique_controls'] and j in used):
                continue
            chosen.setdefault(i, []).append((d, j))
            used.add(j)
        rows = []
        for i in sorted(chosen, key=lambda i: t_ids[i]):
            if len(chosen[i]) < k:
                ctx.warn(f'Treatment {t_ids[i]!r} has only {len(chosen[i])} control(s)')
            rows += [(t_ids[i], c_ids[j], r, d) for r, (d, j) in enumerate(sorted(chosen[i]), start=1)]
        missing_t = [t_ids[i] for i in np.flatnonzero(tmask) if i not in chosen]
        if missing_t:
            ctx.warn(f'No controls found for {len(missing_t)} treatment unit(s)')
        return {'output': Table.from_rows(self.SCHEMA, rows)}


class ABAnalysis(Tool):
    UNITS = Schema([Field('Treatment', FieldType.V_WSTRING, TEXT), Field('Controls', FieldType.INT32),
                    Field('TreatmentComparison', FieldType.DOUBLE), Field('TreatmentTest', FieldType.DOUBLE),
                    Field('ControlComparison', FieldType.DOUBLE), Field('ControlTest', FieldType.DOUBLE),
                    Field('TreatmentGrowth', FieldType.DOUBLE), Field('ControlGrowth', FieldType.DOUBLE), Field('Lift', FieldType.DOUBLE)])
    SUMMARY = Schema([Field('Treatments', FieldType.INT32), Field('MeanLift', FieldType.DOUBLE), Field('CILower', FieldType.DOUBLE),
                      Field('CIUpper', FieldType.DOUBLE), Field('T', FieldType.DOUBLE), Field('PValue', FieldType.DOUBLE),
                      Field('Confidence', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.ab_analysis', version='1.0.0', display_name='AB Analysis', catalog_ids=('ab-testing:ab-analysis',),
        inputs=(Anchor('pairs', 'Pairs', description='Treatment/Control pairs from AB Controls'),
                Anchor('input', 'Performance', description='Unit, date and value records')),
        outputs=(Anchor('output', 'Treatments'), Anchor('summary', 'Summary'), Anchor('chart', 'Chart')),
        description='Measure a test\'s effect: each unit\'s mean value in the comparison period and the test period '
                    'gives a growth ratio. Lift = treatment growth / mean control growth − 1 for each treatment, '
                    'and the summary gives the mean lift with a t confidence interval and p-value (one-sample test '
                    'that lift = 0). The chart shows each treatment\'s lift.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['unit_field', 'date_field', 'value_field', 'comparison_start', 'comparison_end', 'test_start', 'test_end'],
                       'properties': {'unit_field': {'type': 'string', 'minLength': 1}, 'date_field': {'type': 'string', 'minLength': 1},
                                      'value_field': {'type': 'string', 'minLength': 1},
                                      **{k: {'type': 'string', 'minLength': 10} for k in ('comparison_start', 'comparison_end', 'test_start', 'test_end')},
                                      'confidence': {'type': 'number', 'exclusiveMinimum': 0, 'exclusiveMaximum': 1, 'default': 0.95}}})

    def infer(self, ctx, cfg, inputs):
        if tuple(inputs['pairs'].names) != tuple(ABControls.SCHEMA.names):
            raise SchemaError('AB Analysis pairs input must be the output of AB Controls')
        require_fields(inputs['input'], [cfg['unit_field'], cfg['date_field'], cfg['value_field']])
        return {'output': self.UNITS, 'summary': self.SUMMARY, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        from scipy import stats
        periods = {k: _to_datetime(cfg[k]) for k in ('comparison_start', 'comparison_end', 'test_start', 'test_end')}
        if not (periods['comparison_start'] <= periods['comparison_end'] < periods['test_start'] <= periods['test_end']):
            raise ConfigError('The comparison period must end before the test period starts')
        sums = {}
        for rec in inputs['input'].to_dicts():
            if missing(rec[cfg['unit_field']]) or missing(rec[cfg['date_field']]) or missing(rec[cfg['value_field']]):
                continue
            d = _to_datetime(rec[cfg['date_field']])
            phase = 'comparison' if periods['comparison_start'] <= d <= periods['comparison_end'] else \
                'test' if periods['test_start'] <= d <= periods['test_end'] else None
            if phase:
                acc = sums.setdefault((format_value(rec[cfg['unit_field']]), phase), [0.0, 0])
                acc[0] += float(rec[cfg['value_field']])
                acc[1] += 1

        def mean(unit, phase):
            acc = sums.get((unit, phase))
            return acc[0] / acc[1] if acc and acc[1] else None
        controls = {}
        for t, c in zip(inputs['pairs'].column('Treatment'), inputs['pairs'].column('Control')):
            controls.setdefault(t, []).append(c)
        rows, lifts = [], []
        for t in sorted(controls):
            tc, tt = mean(t, 'comparison'), mean(t, 'test')
            growths = [mean(c, 'test') / mean(c, 'comparison') for c in controls[t]
                       if mean(c, 'comparison') and mean(c, 'test') is not None]
            cc = float(np.mean([mean(c, 'comparison') for c in controls[t] if mean(c, 'comparison') is not None])) if growths else None
            ct = float(np.mean([mean(c, 'test') for c in controls[t] if mean(c, 'test') is not None])) if growths else None
            if not tc or tt is None or not growths:
                ctx.warn(f'Treatment {t!r} lacks data in one of the periods and is excluded')
                rows.append((t, len(controls[t]), tc, tt, cc, ct, None, None, None))
                continue
            t_growth, c_growth = tt / tc, float(np.mean(growths))
            lift = t_growth / c_growth - 1
            lifts.append(lift)
            rows.append((t, len(growths), tc, tt, cc, ct, t_growth, c_growth, lift))
        if len(lifts) >= 2:
            res = stats.ttest_1samp(lifts, 0.0)
            ci = res.confidence_interval(cfg['confidence'])
            summary = [(len(lifts), float(np.mean(lifts)), float(ci.low), float(ci.high), float(res.statistic), float(res.pvalue), cfg['confidence'])]
        else:
            summary = [(len(lifts), float(np.mean(lifts)) if lifts else None, None, None, None, None, cfg['confidence'])]
        valid = [r for r in rows if r[-1] is not None]
        canvas = charts.bar_chart([r[0] for r in valid], {'Lift %': [100 * r[-1] for r in valid]}, title='Lift by treatment unit',
                                  y_label='Lift (%)')
        return {'output': Table.from_rows(self.UNITS, rows), 'summary': Table.from_rows(self.SUMMARY, summary),
                'chart': charts.chart_table(canvas, 'Lift by treatment unit')}


TOOLS = [ABTrend, ABTreatments, ABControls, ABAnalysis]
