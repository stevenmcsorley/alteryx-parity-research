"""Prescriptive tools: linear/integer optimisation and Monte Carlo simulation.

Optimisation uses SciPy's HiGHS solvers: ``linprog`` for pure linear programmes (with shadow prices),
and ``milp`` when any variable is integer or binary. Simulation draws are reproducible for a seed.
"""
from __future__ import annotations

import math

from .. import charts, modeling
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import MODEL_SCHEMA, expect_model_schema, missing, read_models
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1


class Optimization(Tool):
    SOLUTION = Schema([Field('Variable', FieldType.V_WSTRING, TEXT), Field('Value', FieldType.DOUBLE),
                       Field('ObjectiveCoefficient', FieldType.DOUBLE), Field('ReducedCost', FieldType.DOUBLE)])
    CONSTRAINTS = Schema([Field('Constraint', FieldType.V_WSTRING, TEXT), Field('Sense', FieldType.V_WSTRING, 2),
                          Field('Activity', FieldType.DOUBLE), Field('RHS', FieldType.DOUBLE), Field('Slack', FieldType.DOUBLE),
                          Field('ShadowPrice', FieldType.DOUBLE)])
    SUMMARY = Schema([Field('Status', FieldType.V_WSTRING, 64), Field('Message', FieldType.V_WSTRING, TEXT),
                      Field('Objective', FieldType.DOUBLE), Field('Variables', FieldType.INT32), Field('Constraints', FieldType.INT32),
                      Field('Integer', FieldType.BOOL)])
    manifest = ToolManifest(
        tool_id='core.optimization', version='1.0.0', display_name='Optimization', catalog_ids=('prescriptive:optimization',),
        inputs=(Anchor('variables', 'Variables', description='One record per decision variable'),
                Anchor('constraints', 'Constraints', description='One record per constraint, one coefficient column per variable')),
        outputs=(Anchor('solution', 'Solution'), Anchor('constraint_report', 'Constraints'), Anchor('summary', 'Summary')),
        description='Solve a linear or mixed-integer programme with HiGHS. Variables give each variable\'s name, '
                    'objective coefficient, bounds and type (continuous, integer or binary). Constraints give a '
                    'name, sense (<=, >= or =), right-hand side and a coefficient column per variable (missing = 0). '
                    'Outputs the optimal values, constraint activity and slack, and the status. Shadow prices and '
                    'reduced costs are given for pure linear programmes only.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'goal': {'enum': ['minimize', 'maximize'], 'default': 'minimize'},
            'name_field': {'type': 'string', 'default': 'Variable'}, 'objective_field': {'type': 'string', 'default': 'Objective'},
            'lower_field': {'type': ['string', 'null'], 'default': 'Lower'}, 'upper_field': {'type': ['string', 'null'], 'default': 'Upper'},
            'type_field': {'type': ['string', 'null'], 'default': 'Type'},
            'constraint_field': {'type': 'string', 'default': 'Constraint'}, 'sense_field': {'type': 'string', 'default': 'Sense'},
            'rhs_field': {'type': 'string', 'default': 'RHS'},
            'time_limit_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600, 'default': 60}}})

    def _fields(self, cfg, schema, kind):
        if kind == 'variables':
            return [cfg['name_field'], cfg['objective_field']] + [cfg[k] for k in ('lower_field', 'upper_field', 'type_field') if cfg[k] and cfg[k] in schema]
        return [cfg['constraint_field'], cfg['sense_field'], cfg['rhs_field']]

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['variables'], [cfg['name_field'], cfg['objective_field']])
        require_fields(inputs['constraints'], self._fields(cfg, inputs['constraints'], 'constraints'))
        return {'solution': self.SOLUTION, 'constraint_report': self.CONSTRAINTS, 'summary': self.SUMMARY}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, linprog, milp
        variables = inputs['variables'].to_dicts()
        names = [format_value(v[cfg['name_field']]) for v in variables]
        if len(set(names)) != len(names) or any(n is None for n in names):
            raise DataError('Variable names must be unique and not null')
        c = np.array([float(v[cfg['objective_field']] or 0) for v in variables])
        sign = -1.0 if cfg['goal'] == 'maximize' else 1.0

        def get(v, key, default):
            field = cfg[key]
            return default if not field or field not in v or missing(v[field]) else v[field]
        kinds = [str(get(v, 'type_field', 'continuous')).lower() for v in variables]
        bad = [k for k in kinds if k not in ('continuous', 'integer', 'binary')]
        if bad:
            raise DataError(f'Variable type must be continuous, integer or binary, not {bad[0]!r}')
        lower = np.array([0.0 if k == 'binary' else float(get(v, 'lower_field', 0.0)) for v, k in zip(variables, kinds)])
        upper = np.array([1.0 if k == 'binary' else float(get(v, 'upper_field', math.inf)) for v, k in zip(variables, kinds)])
        integrality = np.array([0 if k == 'continuous' else 1 for k in kinds])
        constraints = inputs['constraints'].to_dicts()
        A, senses, rhs, cnames = [], [], [], []
        for number, con in enumerate(constraints, start=1):
            sense = str(con[cfg['sense_field']]).strip()
            if sense not in ('<=', '>=', '='):
                raise DataError(f'Constraint {number}: sense must be <=, >= or =')
            A.append([0.0 if name not in con or missing(con[name]) else float(con[name]) for name in names])
            senses.append(sense)
            rhs.append(float(con[cfg['rhs_field']]))
            cnames.append(format_value(con[cfg['constraint_field']]) or f'C{number}')
        A = np.array(A) if A else np.zeros((0, len(names)))
        rhs = np.array(rhs)
        integer = bool(integrality.any())
        duals = reduced = None
        if integer:
            lo = np.where(np.array(senses) == '<=', -np.inf, rhs)
            hi = np.where(np.array(senses) == '>=', np.inf, rhs)
            res = milp(sign * c, constraints=[LinearConstraint(A, lo, hi)] if len(A) else None, integrality=integrality,
                       bounds=Bounds(lower, upper), options={'time_limit': cfg['time_limit_seconds']})
            x = res.x
        else:
            ub = [(-row if s == '>=' else row) for row, s in zip(A, senses) if s != '=']
            bub = [(-b if s == '>=' else b) for b, s in zip(rhs, senses) if s != '=']
            eq = [row for row, s in zip(A, senses) if s == '=']
            beq = [b for b, s in zip(rhs, senses) if s == '=']
            res = linprog(sign * c, A_ub=np.array(ub) if ub else None, b_ub=np.array(bub) if bub else None,
                          A_eq=np.array(eq) if eq else None, b_eq=np.array(beq) if beq else None,
                          bounds=list(zip(lower, [None if math.isinf(u) else u for u in upper])), method='highs',
                          options={'time_limit': cfg['time_limit_seconds']})
            x = res.x
            if res.status == 0:
                ub_m = iter(res.ineqlin.marginals) if ub else iter(())
                eq_m = iter(res.eqlin.marginals) if eq else iter(())
                duals = [sign * (-next(ub_m) if s == '>=' else next(ub_m)) if s != '=' else sign * next(eq_m) for s in senses]
                reduced = sign * (res.lower.marginals + res.upper.marginals)
        status = {0: 'optimal', 1: 'iteration or time limit', 2: 'infeasible', 3: 'unbounded', 4: 'numerical difficulties'}.get(res.status, 'error')
        summary = [(status, str(res.message), float(sign * res.fun) if x is not None and res.fun is not None else None,
                    len(names), len(cnames), integer)]
        if x is None:
            ctx.warn(f'Optimization did not find a solution: {status}')
            return {'solution': Table.empty(self.SOLUTION), 'constraint_report': Table.empty(self.CONSTRAINTS),
                    'summary': Table.from_rows(self.SUMMARY, summary)}
        if integer:
            x = np.where(integrality == 1, np.round(x), x)
        solution = [(n, float(v), float(ci), float(reduced[i]) if reduced is not None else None)
                    for i, (n, v, ci) in enumerate(zip(names, x, c))]
        activity = A @ x if len(A) else []
        report = [(n, s, float(a), float(b), float(b - a if s == '<=' else a - b if s == '>=' else 0.0),
                   float(duals[i]) if duals is not None else None)
                  for i, (n, s, a, b) in enumerate(zip(cnames, senses, activity, rhs))]
        return {'solution': Table.from_rows(self.SOLUTION, solution), 'constraint_report': Table.from_rows(self.CONSTRAINTS, report),
                'summary': Table.from_rows(self.SUMMARY, summary)}


_DISTRIBUTION = {'type': 'object', 'additionalProperties': False, 'required': ['field', 'distribution'], 'properties': {
    'field': {'type': 'string', 'minLength': 1},
    'distribution': {'enum': ['normal', 'lognormal', 'uniform', 'triangular', 'exponential', 'gamma', 'beta', 'poisson',
                              'binomial', 'empirical']},
    'params': {'type': 'object', 'default': {}, 'additionalProperties': {'type': 'number'}},
    'source_field': {'type': ['string', 'null'], 'default': None, 'description': 'Input field to resample (empirical)'}}}
_PARAMS = {'normal': ('mean', 'sd'), 'lognormal': ('mean_log', 'sd_log'), 'uniform': ('low', 'high'),
           'triangular': ('low', 'mode', 'high'), 'exponential': ('mean',), 'gamma': ('shape', 'scale'), 'beta': ('a', 'b'),
           'poisson': ('mean',), 'binomial': ('n', 'p'), 'empirical': ()}


class SimulationSampling(Tool):
    manifest = ToolManifest(
        tool_id='core.simulation_sampling', version='1.0.0', display_name='Simulation Sampling',
        catalog_ids=('prescriptive:simulation-sampling',),
        inputs=(Anchor('input', 'Input', required=False, description='Data for empirical (bootstrap) resampling'),),
        outputs=(Anchor('output', 'Draws'),),
        description='Generate Monte Carlo draws: iterations records with one field per variable, each drawn from a '
                    'parametric distribution (normal, lognormal, uniform, triangular, exponential, gamma, beta, '
                    'Poisson or binomial with named parameters) or resampled with replacement from an input field '
                    '(empirical). Draws are independent and reproducible for a seed.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['variables'], 'properties': {
            'variables': {'type': 'array', 'minItems': 1, 'items': _DISTRIBUTION},
            'iterations': {'type': 'integer', 'minimum': 1, 'maximum': 10_000_000, 'default': 10000},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1}}})

    def check_config(self, cfg):
        for v in cfg['variables']:
            need = _PARAMS[v['distribution']]
            missing_params = [p for p in need if p not in v['params']]
            if missing_params:
                raise ConfigError(f"Variable {v['field']!r}: {v['distribution']} needs parameter(s) {missing_params}")
            if v['distribution'] == 'empirical' and not v['source_field']:
                raise ConfigError(f"Variable {v['field']!r}: empirical sampling needs source_field")

    def infer(self, ctx, cfg, inputs):
        for v in cfg['variables']:
            if v['distribution'] == 'empirical':
                if 'input' not in inputs:
                    raise ConfigError('Empirical sampling needs a connected input')
                require_fields(inputs['input'], [v['source_field']])
        fields = [Field('Iteration', FieldType.INT64)]
        for v in cfg['variables']:
            kind = FieldType.INT64 if v['distribution'] in ('poisson', 'binomial') else FieldType.DOUBLE
            fields.append(Field(v['field'], kind))
        return {'output': Schema(fields)}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        schema = self.infer(ctx, cfg, {k: t.schema for k, t in inputs.items()})['output']
        rng = np.random.default_rng(cfg['seed'])
        n = cfg['iterations']
        cols = [np.arange(1, n + 1)]
        for v in cfg['variables']:
            p, d = v['params'], v['distribution']
            try:
                if d == 'normal':
                    draw = rng.normal(p['mean'], p['sd'], n)
                elif d == 'lognormal':
                    draw = rng.lognormal(p['mean_log'], p['sd_log'], n)
                elif d == 'uniform':
                    draw = rng.uniform(p['low'], p['high'], n)
                elif d == 'triangular':
                    draw = rng.triangular(p['low'], p['mode'], p['high'], n)
                elif d == 'exponential':
                    draw = rng.exponential(p['mean'], n)
                elif d == 'gamma':
                    draw = rng.gamma(p['shape'], p['scale'], n)
                elif d == 'beta':
                    draw = rng.beta(p['a'], p['b'], n)
                elif d == 'poisson':
                    draw = rng.poisson(p['mean'], n)
                elif d == 'binomial':
                    draw = rng.binomial(int(p['n']), p['p'], n)
                else:
                    source = np.array([float(x) for x in inputs['input'].column(v['source_field']) if not missing(x)])
                    if not source.size:
                        raise DataError(f"Empirical source field {v['source_field']!r} has no values")
                    draw = rng.choice(source, size=n, replace=True)
            except ValueError as e:
                raise ConfigError(f"Variable {v['field']!r}: invalid {d} parameters ({e})") from None
            cols.append(draw)
        return {'output': Table(schema, [[int(x) if f.type is FieldType.INT64 else float(x) for x in col] for col, f in zip(cols, schema)])}


class SimulationScoring(Tool):
    manifest = ToolManifest(
        tool_id='core.simulation_scoring', version='1.0.0', display_name='Simulation Scoring',
        catalog_ids=('prescriptive:simulation-scoring',),
        inputs=(Anchor('model', 'Model'), Anchor('input', 'Data')), outputs=(Anchor('output', 'Simulated'),),
        description='Simulate model outcomes with their uncertainty: for each record, iterations draws of the '
                    'outcome. Regression models give prediction + Normal(0, training RMSE) error. Classification '
                    'models give a class drawn from the predicted probabilities. Count models give Poisson draws '
                    'around the predicted mean. Output repeats the record with Iteration and Simulated. Feed it to '
                    'Simulation Summary.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'iterations': {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 1000},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1},
            'max_rows': {'type': 'integer', 'minimum': 1, 'default': 5_000_000}}})

    def infer(self, ctx, cfg, inputs):
        expect_model_schema(inputs['model'])
        return None  # Simulated is numeric or text depending on the model

    def run(self, ctx, cfg, inputs):
        import numpy as np
        name, info, payload = read_models(inputs['model'])[0]
        table = inputs['input']
        if len(table) * cfg['iterations'] > cfg['max_rows']:
            raise DataError(f"Simulation would produce {len(table) * cfg['iterations']:,} rows (max_rows {cfg['max_rows']:,})")
        res = modeling.predict(info, payload, table)
        rng = np.random.default_rng(cfg['seed'])
        rows = list(table.rows())
        out = []
        if 'proba' in res:
            classes = info['classes']
            schema = Schema([*table.schema, Field('Iteration', FieldType.INT64), Field('Simulated', FieldType.V_WSTRING, TEXT)])
            for row, p, m in zip(rows, res['proba'], res['mask']):
                draws = rng.choice(len(classes), size=cfg['iterations'], p=p / p.sum()) if m and np.all(np.isfinite(p)) else None
                out += [(*row, i + 1, classes[int(d)] if draws is not None else None) for i, d in enumerate(draws if draws is not None else [0] * cfg['iterations'])]
            return {'output': Table.from_rows(schema, out)}
        schema = Schema([*table.schema, Field('Iteration', FieldType.INT64), Field('Simulated', FieldType.DOUBLE)])
        sigma = (info.get('metrics') or {}).get('RMSE')
        count = info['family'] in ('poisson', 'negative_binomial')
        if not count and sigma is None:
            raise DataError(f'Model {name!r} has no training RMSE to simulate errors from')
        for row, mean, m in zip(rows, res['value'], res['mask']):
            if not m or not np.isfinite(mean):
                out += [(*row, i + 1, None) for i in range(cfg['iterations'])]
                continue
            draws = rng.poisson(max(mean, 0), cfg['iterations']) if count else mean + rng.normal(0, sigma, cfg['iterations'])
            out += [(*row, i + 1, float(d)) for i, d in enumerate(draws)]
        return {'output': Table.from_rows(schema, out)}


class SimulationSummary(Tool):
    SCHEMA = Schema([Field('Group', FieldType.V_WSTRING, TEXT), Field('N', FieldType.INT64), Field('Mean', FieldType.DOUBLE),
                     Field('StdDev', FieldType.DOUBLE), Field('Min', FieldType.DOUBLE), Field('P5', FieldType.DOUBLE),
                     Field('P25', FieldType.DOUBLE), Field('Median', FieldType.DOUBLE), Field('P75', FieldType.DOUBLE),
                     Field('P95', FieldType.DOUBLE), Field('Max', FieldType.DOUBLE), Field('ProbAbove', FieldType.DOUBLE),
                     Field('ProbBelow', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.simulation_summary', version='1.0.0', display_name='Simulation Summary',
        catalog_ids=('prescriptive:simulation-summary',),
        inputs=(Anchor('input', 'Simulated'),), outputs=(Anchor('output', 'Summary'), Anchor('chart', 'Chart')),
        description='Summarise simulated outcomes (optionally per group): mean, standard deviation, min, 5th, 25th, '
                    '50th, 75th and 95th percentiles, max, and the probability of exceeding above or falling short '
                    'of below. The chart is a histogram of all draws.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['value_field'], 'properties': {
            'value_field': {'type': 'string', 'minLength': 1}, 'group_field': {'type': ['string', 'null'], 'default': None},
            'above': {'type': ['number', 'null'], 'default': None}, 'below': {'type': ['number', 'null'], 'default': None},
            'bins': {'type': 'integer', 'minimum': 2, 'maximum': 500, 'default': 40}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['value_field']] + ([cfg['group_field']] if cfg['group_field'] else []))
        if not inputs['input'].field(cfg['value_field']).type.is_numeric:
            raise SchemaError('Simulation Summary value_field must be numeric')
        return {'output': self.SCHEMA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        groups = {}
        gvals = table.column(cfg['group_field']) if cfg['group_field'] else ['All'] * len(table)
        for g, v in zip(gvals, table.column(cfg['value_field'])):
            if not missing(v):
                groups.setdefault(format_value(g) if g is not None else '[Null]', []).append(float(v))
        if not groups:
            raise DataError('Simulation Summary received no values')
        rows = []
        for g in sorted(groups):
            x = np.array(groups[g])
            q = np.percentile(x, [5, 25, 50, 75, 95])
            rows.append((g, len(x), float(x.mean()), float(x.std(ddof=1)) if len(x) > 1 else None, float(x.min()), *map(float, q),
                         float(x.max()), float((x > cfg['above']).mean()) if cfg['above'] is not None else None,
                         float((x < cfg['below']).mean()) if cfg['below'] is not None else None))
        everything = np.concatenate([np.array(v) for v in groups.values()])
        counts, edges = np.histogram(everything, bins=cfg['bins'])
        canvas = charts.histogram_chart([float(e) for e in edges], [int(c) for c in counts], title='Simulated outcomes',
                                        x_label=cfg['value_field'])
        return {'output': Table.from_rows(self.SCHEMA, rows), 'chart': charts.chart_table(canvas, 'Simulated outcomes')}


TOOLS = [Optimization, SimulationSampling, SimulationScoring, SimulationSummary]
