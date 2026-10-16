"""Batch 8: grouping, time series, A/B testing and prescriptive tools checked against known answers."""
import datetime as dt
import math
import random
import unittest

import numpy as np

from flowengine.executor import run_workflow

from helpers import ROOT, edge, run_tool, text_input, workflow


def run(nodes, edges):
    r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
    assert r.ok, r.error
    return r


BLOBS = [[float(cx + random.Random(i).gauss(0, 0.3)), float(cy + random.Random(i + 999).gauss(0, 0.3)), label, f'id{i}']
         for i, (cx, cy, label) in enumerate([(0, 0, 'A'), (5, 5, 'B'), (0, 8, 'C')] * 40)]
BF = [('x', 'double'), ('y', 'double'), ('truth', 'v_wstring'), ('id', 'v_wstring')]


class GroupingTests(unittest.TestCase):
    def test_kcentroids_recovers_blobs_and_appends_clusters(self):
        from sklearn.metrics import adjusted_rand_score
        for method in ('kmeans', 'kmedians'):
            with self.subTest(method=method):
                r = run([text_input('d', BF, BLOBS), text_input('new', BF, [[5.1, 4.9, 'B', 'n1'], [0.1, 7.9, 'C', 'n2']]),
                         {'id': 'k', 'tool': 'core.k_centroids_cluster_analysis', 'config': {'fields': ['x', 'y'], 'k': 3, 'method': method}},
                         {'id': 'a', 'tool': 'core.append_cluster', 'config': {}}, {'id': 'a2', 'tool': 'core.append_cluster', 'config': {}}],
                        [edge('d', 'output', 'k', 'input'), edge('k', 'model', 'a', 'model'), edge('d', 'output', 'a', 'input'),
                         edge('k', 'model', 'a2', 'model'), edge('new', 'output', 'a2', 'input')])
                labelled = r.output('a').to_dicts()
                self.assertEqual(adjusted_rand_score([x['truth'] for x in labelled], [x['Cluster'] for x in labelled]), 1.0)
                cluster_of = {x['truth']: x['Cluster'] for x in labelled}
                self.assertEqual([x['Cluster'] for x in r.output('a2').to_dicts()], [cluster_of['B'], cluster_of['C']])
                sizes = {x['Cluster']: x['Size'] for x in r.output('k', 'summary').to_dicts()}
                self.assertEqual(sorted(sizes.values()), [40, 40, 40])

    def test_diagnostics_prefer_the_true_k(self):
        out, _ = run_tool('core.k_centroids_diagnostics', {'fields': ['x', 'y'], 'k_min': 2, 'k_max': 6, 'bootstrap': 3},
                          {'input': (BF, BLOBS)})
        rows = out['output'].to_dicts()
        self.assertEqual(max(rows, key=lambda r: r['Silhouette'])['K'], 3)
        self.assertGreater(next(r for r in rows if r['K'] == 3)['MeanAdjustedRand'], 0.9)

    def test_pca_matches_eigen_decomposition_and_mds_reproduces_distances(self):
        rng = np.random.default_rng(2)
        base = rng.normal(size=(200, 2))
        data = np.column_stack([base[:, 0], base[:, 0] * 2 + base[:, 1] * 0.1, base[:, 1]])
        rows = [list(map(float, r)) for r in data]
        fields = [('a', 'double'), ('b', 'double'), ('c', 'double')]
        out, _ = run_tool('core.principal_components', {'fields': ['a', 'b', 'c']}, {'input': (fields, rows)})
        expected = np.sort(np.linalg.eigvalsh(np.corrcoef(data, rowvar=False)))[::-1]
        got = [r['Eigenvalue'] for r in out['variance'].to_dicts()]
        np.testing.assert_allclose(got, expected, rtol=1e-9, atol=1e-12)
        self.assertAlmostEqual(out['variance'].to_dicts()[-1]['CumulativeVariance'], 1.0)
        scores = np.array([[r['PC1'], r['PC2']] for r in out['output'].to_dicts()])
        self.assertAlmostEqual(np.corrcoef(scores, rowvar=False)[0, 1], 0.0, places=10)
        plane = [[float(x), float(y)] for x, y in rng.normal(size=(30, 2))]
        mds, _ = run_tool('core.multidimensional_scaling', {'fields': ['a', 'b'], 'standardize': False},
                          {'input': ([('a', 'double'), ('b', 'double')], plane)})
        fit = {r['Metric']: r['Value'] for r in mds['fit'].to_dicts()}
        self.assertLess(fit['Stress'], 1e-9)  # 2-D data embeds exactly in 2 dimensions
        coords = np.array([[r['Dim1'], r['Dim2']] for r in mds['output'].to_dicts()])
        orig = np.array(plane)
        self.assertAlmostEqual(np.linalg.norm(coords[0] - coords[1]), np.linalg.norm(orig[0] - orig[1]), places=8)

    def test_nearest_neighbours_match_brute_force(self):
        out, _ = run_tool('core.find_nearest_neighbors', {'fields': ['x', 'y'], 'id_field': 'id', 'k': 3, 'standardize': False},
                          {'input': (BF, BLOBS)})
        got = {}
        for r in out['output'].to_dicts():
            got.setdefault(r['QueryID'], []).append(r['NeighborID'])
        pts = {b[3]: np.array(b[:2]) for b in BLOBS}
        for qid in ('id0', 'id17', 'id101'):
            brute = sorted((np.linalg.norm(pts[qid] - p), i) for i, p in pts.items() if i != qid)[:3]
            self.assertEqual(got[qid], [i for _, i in brute])

    def test_market_basket_rules_affinity_and_inspect(self):
        baskets = [['bread', 'milk'], ['bread', 'diapers', 'beer', 'eggs'], ['milk', 'diapers', 'beer', 'cola'],
                   ['bread', 'milk', 'diapers', 'beer'], ['bread', 'milk', 'diapers', 'cola']]
        rows = [[t, item] for t, items in enumerate(baskets) for item in items]
        fields = [('t', 'int32'), ('item', 'v_wstring')]
        r = run([text_input('d', fields, rows),
                 {'id': 'rules', 'tool': 'core.mb_rules', 'config': {'transaction_field': 't', 'item_field': 'item', 'min_support': 0.4, 'min_confidence': 0.5}},
                 {'id': 'inspect', 'tool': 'core.mb_inspect', 'config': {'consequent_contains': 'beer', 'min_lift': 1}},
                 {'id': 'aff', 'tool': 'core.mb_affinity', 'config': {'transaction_field': 't', 'item_field': 'item'}}],
                [edge('d', 'output', 'rules', 'input'), edge('rules', 'output', 'inspect', 'input'), edge('d', 'output', 'aff', 'input')])
        rule = next(x for x in r.output('rules').to_dicts() if x['Antecedent'] == 'diapers' and x['Consequent'] == 'beer')
        # Textbook example: support 3/5, confidence 3/4, lift (3/4)/(3/5) = 1.25
        self.assertEqual((rule['Count'], rule['Support'], rule['Confidence']), (3, 0.6, 0.75))
        self.assertAlmostEqual(rule['Lift'], 1.25)
        self.assertTrue(all('beer' in x['Consequent'].split(' + ') for x in r.output('inspect').to_dicts()))
        pair = next(x for x in r.output('aff').to_dicts() if {x['Item1'], x['Item2']} == {'beer', 'diapers'})
        self.assertEqual((pair['CoOccurrence'], pair['Jaccard']), (3, 0.75))


def monthly(n, start=dt.date(2018, 1, 1)):
    """ISO date strings (Text Input takes dates as text) for n consecutive month starts."""
    out, d = [], start
    for _ in range(n):
        out.append(d.isoformat())
        d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


class TimeSeriesTests(unittest.TestCase):
    def test_arima_recovers_ar1_and_forecast_intervals_widen(self):
        rng = np.random.default_rng(4)
        y = [10.0]
        for _ in range(299):
            y.append(10 + 0.7 * (y[-1] - 10) + rng.normal(0, 1))
        rows = [[d, v] for d, v in zip(monthly(300), y)]
        fields = [('date', 'date'), ('value', 'double')]
        r = run([text_input('d', fields, rows),
                 {'id': 'm', 'tool': 'core.arima', 'config': {'date_field': 'date', 'value_field': 'value', 'order': [1, 0, 0]}},
                 {'id': 'f', 'tool': 'core.ts_forecast', 'config': {'periods': 24}}],
                [edge('d', 'output', 'm', 'input'), edge('m', 'model', 'f', 'models')])
        import json
        params = json.loads(r.output('m', 'model').column('Info')[0])['model']['params']
        self.assertAlmostEqual(params[1], 0.7, delta=0.08)  # AR(1) coefficient (after the constant)
        fc = r.output('f').to_dicts()
        self.assertEqual(fc[0]['Date'], dt.datetime(2043, 1, 1))  # monthly spacing continues
        widths = [x['Upper95'] - x['Lower95'] for x in fc]
        self.assertLess(widths[0], widths[-1])
        self.assertAlmostEqual(fc[-1]['Forecast'], 10, delta=1.0)  # mean reversion
        self.assertTrue(all(x['Lower95'] < x['Lower80'] < x['Forecast'] < x['Upper80'] < x['Upper95'] for x in fc))

    def test_ets_and_compare_on_trend_with_seasonality(self):
        dates = monthly(84)
        y = [100 + 2 * t + 10 * math.sin(2 * math.pi * t / 12) for t in range(84)]
        train = [[d, v] for d, v in zip(dates[:72], y[:72])]
        hold = [[d, v] for d, v in zip(dates[72:], y[72:])]
        fields = [('date', 'date'), ('value', 'double')]
        r = run([text_input('d', fields, train), text_input('h', fields, hold),
                 {'id': 'ets', 'tool': 'core.ets', 'config': {'date_field': 'date', 'value_field': 'value', 'seasonal_period': 12}},
                 {'id': 'cmp', 'tool': 'core.ts_compare', 'config': {'date_field': 'date', 'value_field': 'value'}}],
                [edge('d', 'output', 'ets', 'input'), edge('ets', 'model', 'cmp', 'models'), edge('h', 'output', 'cmp', 'input')])
        metrics = r.output('cmp').to_dicts()[0]
        self.assertLess(metrics['MAPE'], 1.0)  # a deterministic trend + season is forecast almost exactly
        detail = r.output('cmp', 'detail').to_dicts()
        self.assertAlmostEqual(metrics['MAE'], np.mean([abs(x['Error']) for x in detail]))

    def test_covariate_forecast_uses_future_covariates(self):
        rng = np.random.default_rng(1)
        x = rng.uniform(0, 10, 120)
        rows = [[d, float(2 * xi + rng.normal(0, 0.1)), float(xi)] for d, xi in zip(monthly(120), x)]
        fields = [('date', 'date'), ('value', 'double'), ('x', 'double')]
        future = [[d, None, v] for d, v in zip(monthly(3, dt.date(2028, 1, 1)), [1.0, 5.0, 9.0])]
        r = run([text_input('d', fields, rows), text_input('fut', fields, future),
                 {'id': 'm', 'tool': 'core.arima', 'config': {'date_field': 'date', 'value_field': 'value', 'order': [0, 0, 0], 'covariates': ['x']}},
                 {'id': 'f', 'tool': 'core.ts_covariate_forecast', 'config': {}}],
                [edge('d', 'output', 'm', 'input'), edge('m', 'model', 'f', 'models'), edge('fut', 'output', 'f', 'input')])
        self.assertEqual([round(x['Forecast']) for x in r.output('f').to_dicts()], [2, 10, 18])

    def test_filler_plot_and_factory(self):
        rows = [[d, 'a', float(i)] for i, d in enumerate(monthly(12)) if i not in (3, 4)] + \
               [[d, 'b', float(10 * i)] for i, d in enumerate(monthly(12))]
        fields = [('date', 'date'), ('store', 'v_wstring'), ('sales', 'double')]
        filled, _ = run_tool('core.ts_filler', {'date_field': 'date', 'interval': 'month', 'group_by': ['store'],
                                                'value_fields': ['sales'], 'fill': 'linear'}, {'input': (fields, rows)})
        a = [x for x in filled['output'].to_dicts() if x['store'] == 'a']
        self.assertEqual(len(a), 12)
        self.assertEqual([x['sales'] for x in a if x['Filled']], [3.0, 4.0])  # straight line between 2 and 5
        y = [100 + 10 * math.sin(2 * math.pi * t / 12) + t for t in range(48)]
        plot, _ = run_tool('core.ts_plot', {'date_field': 'date', 'value_field': 'v', 'seasonal_period': 12},
                           {'input': ([('date', 'date'), ('v', 'double')], [[d, v] for d, v in zip(monthly(48), y)])})
        for c in plot['components'].to_dicts():
            self.assertAlmostEqual(c['Trend'] + c['Seasonal'] + c['Remainder'], c['Observed'])
        r = run([text_input('d', fields, [r for r in rows if r[1] == 'b'] + [[d, 'c', float(5 + i)] for i, d in enumerate(monthly(12))]),
                 {'id': 'mf', 'tool': 'core.ts_model_factory', 'config': {'date_field': 'date', 'value_field': 'sales', 'group_by': ['store']}},
                 {'id': 'ff', 'tool': 'core.ts_forecast_factory', 'config': {'periods': 2}}],
                [edge('d', 'output', 'mf', 'input'), edge('mf', 'model', 'ff', 'models')])
        self.assertEqual(r.output('mf', 'model').column('Name'), ['b', 'c'])
        fc = r.output('ff').to_dicts()
        self.assertEqual(len(fc), 4)
        self.assertAlmostEqual(next(x for x in fc if x['Model'] == 'b')['Forecast'], 120, delta=2)


class ABTests(unittest.TestCase):
    def test_matched_controls_and_lift(self):
        rng = random.Random(3)
        units = [[f'u{i}', 1000 + 50 * i + rng.gauss(0, 20), 0.5 + 0.01 * (i % 7)] for i in range(40)]
        ufields = [('unit', 'v_wstring'), ('base', 'double'), ('growth', 'double')]
        perf = []
        treated = {'u5', 'u15', 'u25', 'u35'}
        for unit, base, _ in units:
            for week in range(16):
                d = (dt.date(2026, 1, 5) + dt.timedelta(weeks=week)).isoformat()
                bump = 1.10 if unit in treated and week >= 8 else 1.0
                perf.append([unit, d, base * bump * (1 + rng.gauss(0, 0.01))])
        pfields = [('unit', 'v_wstring'), ('date', 'date'), ('sales', 'double')]
        r = run([text_input('units', ufields, units), text_input('treat', ufields, [u for u in units if u[0] in treated]),
                 text_input('perf', pfields, perf),
                 {'id': 'trend', 'tool': 'core.ab_trend', 'config': {'unit_field': 'unit', 'date_field': 'date', 'value_field': 'sales'}},
                 {'id': 'ctl', 'tool': 'core.ab_controls', 'config': {'unit_field': 'unit', 'criteria': ['base'], 'controls_per_treatment': 3}},
                 {'id': 'an', 'tool': 'core.ab_analysis', 'config': {'unit_field': 'unit', 'date_field': 'date', 'value_field': 'sales',
                                                                      'comparison_start': '2026-01-05', 'comparison_end': '2026-02-28',
                                                                      'test_start': '2026-03-02', 'test_end': '2026-04-30'}},
                 {'id': 'pick', 'tool': 'core.ab_treatments', 'config': {'unit_field': 'unit', 'criteria': ['base'], 'count': 2}}],
                [edge('perf', 'output', 'trend', 'input'), edge('treat', 'output', 'ctl', 'treatments'), edge('units', 'output', 'ctl', 'input'),
                 edge('ctl', 'output', 'an', 'pairs'), edge('perf', 'output', 'an', 'input'), edge('units', 'output', 'pick', 'input')])
        pairs = r.output('ctl').to_dicts()
        self.assertEqual(len(pairs), 12)
        self.assertFalse({p['Control'] for p in pairs} & treated)
        summary = r.output('an', 'summary').to_dicts()[0]
        self.assertAlmostEqual(summary['MeanLift'], 0.10, delta=0.02)
        self.assertLess(summary['CILower'], 0.10)
        self.assertGreater(summary['CILower'], 0)  # the effect is detected
        self.assertLess(summary['PValue'], 0.01)
        picked = {x['unit'] for x in r.output('pick').to_dicts()}
        self.assertEqual(picked, {'u19', 'u20'})  # the two units nearest the mean base level
        self.assertEqual(len(r.output('trend')), 40)


class PrescriptiveTests(unittest.TestCase):
    def solve(self, variables, constraints, **cfg):
        vf = [('Variable', 'v_wstring'), ('Objective', 'double'), ('Lower', 'double'), ('Upper', 'double'), ('Type', 'v_wstring')]
        names = [v[0] for v in variables]
        cf = [('Constraint', 'v_wstring'), ('Sense', 'v_wstring'), ('RHS', 'double')] + [(n, 'double') for n in names]
        out, _ = run_tool('core.optimization', cfg, {'variables': (vf, variables), 'constraints': (cf, constraints)})
        return out

    def test_textbook_linear_programme_with_shadow_prices(self):
        # maximise 3x + 5y s.t. x <= 4, 2y <= 12, 3x + 2y <= 18  ->  x = 2, y = 6, objective 36, duals 0, 1.5, 1
        out = self.solve([['x', 3, 0, None, 'continuous'], ['y', 5, 0, None, 'continuous']],
                         [['c1', '<=', 4, 1, 0], ['c2', '<=', 12, 0, 2], ['c3', '<=', 18, 3, 2]], goal='maximize')
        sol = {r['Variable']: r['Value'] for r in out['solution'].to_dicts()}
        self.assertAlmostEqual(sol['x'], 2)
        self.assertAlmostEqual(sol['y'], 6)
        self.assertAlmostEqual(out['summary'].to_dicts()[0]['Objective'], 36)
        duals = {r['Constraint']: r['ShadowPrice'] for r in out['constraint_report'].to_dicts()}
        self.assertAlmostEqual(duals['c1'], 0)
        self.assertAlmostEqual(duals['c2'], 1.5)
        self.assertAlmostEqual(duals['c3'], 1)
        slack = {r['Constraint']: r['Slack'] for r in out['constraint_report'].to_dicts()}
        self.assertAlmostEqual(slack['c1'], 2)

    def test_knapsack_and_infeasibility(self):
        items = [['a', 10, 0, 1, 'binary'], ['b', 13, 0, 1, 'binary'], ['c', 7, 0, 1, 'binary'], ['d', 8, 0, 1, 'binary']]
        out = self.solve(items, [['weight', '<=', 10, 5, 6, 3, 4]], goal='maximize')
        chosen = {r['Variable'] for r in out['solution'].to_dicts() if r['Value'] > 0.5}
        self.assertEqual(chosen, {'b', 'd'})  # value 21 within weight 10 (a+c gives 17)
        infeasible = self.solve([['x', 1, 0, None, 'continuous']], [['lo', '>=', 5, 1], ['hi', '<=', 3, 1]])
        self.assertEqual(infeasible['summary'].to_dicts()[0]['Status'], 'infeasible')

    def test_sampling_scoring_and_summary(self):
        cfg = {'iterations': 20000, 'seed': 3, 'variables': [
            {'field': 'demand', 'distribution': 'normal', 'params': {'mean': 100, 'sd': 15}},
            {'field': 'price', 'distribution': 'triangular', 'params': {'low': 5, 'mode': 6, 'high': 9}},
            {'field': 'orders', 'distribution': 'poisson', 'params': {'mean': 4}}]}
        out, _ = run_tool('core.simulation_sampling', cfg, {})
        t = out['output']
        demand = np.array(t.column('demand'))
        self.assertAlmostEqual(demand.mean(), 100, delta=0.5)
        self.assertAlmostEqual(demand.std(ddof=1), 15, delta=0.3)
        self.assertTrue(5 <= min(t.column('price')) and max(t.column('price')) <= 9)
        again, _ = run_tool('core.simulation_sampling', cfg, {})
        self.assertEqual(again['output'].column('demand')[:5], t.column('demand')[:5])  # reproducible
        summary, _ = run_tool('core.simulation_summary', {'value_field': 'demand', 'above': 130},
                              {'input': ([('demand', 'double')], [[v] for v in demand])})
        s = summary['output'].to_dicts()[0]
        self.assertAlmostEqual(s['P95'], float(np.percentile(demand, 95)))
        self.assertAlmostEqual(s['ProbAbove'], 0.0228, delta=0.004)  # P(Z > 2)
        rng = random.Random(9)
        rows = [[float(x), 3 * x + rng.gauss(0, 2)] for x in range(100)]
        fields = [('x', 'double'), ('y', 'double')]
        r = run([text_input('d', fields, rows), text_input('new', fields, [[50.0, None]]),
                 {'id': 'm', 'tool': 'core.linear_regression', 'config': {'target_field': 'y', 'predictors': ['x']}},
                 {'id': 's', 'tool': 'core.simulation_scoring', 'config': {'iterations': 5000}}],
                [edge('d', 'output', 'm', 'input'), edge('m', 'model', 's', 'model'), edge('new', 'output', 's', 'input')])
        sims = np.array(r.output('s').column('Simulated'))
        self.assertAlmostEqual(sims.mean(), 150, delta=0.5)
        self.assertAlmostEqual(sims.std(), 2, delta=0.2)


if __name__ == '__main__':
    unittest.main()


class ExampleTests(unittest.TestCase):
    def test_diesel_forecast_example(self):
        from flowengine.executor import run_file
        result = run_file(ROOT / 'examples/timeseries/diesel_forecast.flow.json')
        self.assertTrue(result.ok, result.error)
        fc = result.output('forecast').to_dicts()
        self.assertEqual(len(fc), 26)
        self.assertEqual((fc[1]['Date'] - fc[0]['Date']).days, 7)  # weekly spacing continues
        self.assertTrue(all(r['Lower95'] < r['Forecast'] < r['Upper95'] for r in fc))
