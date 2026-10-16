"""Predictive tools checked against independent references and known data-generating processes."""
import json
import math
import random
import unittest

import numpy as np

from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_workflow
from flowengine.modeling import MODEL_SCHEMA, model_table, sk_loads

from helpers import ROOT, edge, run_tool, text_input, workflow

RNG = random.Random(7)
F = [('x1', 'double'), ('x2', 'double'), ('noise', 'double'), ('grp', 'v_wstring'), ('y', 'double'), ('cls', 'v_wstring'),
     ('count', 'int32'), ('cost', 'double'), ('exposure', 'double')]


def make_rows(n=400, seed=7):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        x1, x2, noise = rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)
        grp = 'b' if i % 3 == 0 else 'a'
        y = 2 + 3 * x1 - 1.5 * x2 + (1.0 if grp == 'b' else 0.0) + rng.gauss(0, 0.5)
        cls = 'yes' if 1.2 * x1 - 0.8 * x2 + rng.gauss(0, 0.7) > 0 else 'no'
        exposure = rng.uniform(0.5, 2.0)
        lam = exposure * math.exp(0.3 + 0.5 * x1)
        k, p, l = 0, 1.0, math.exp(-lam)  # Knuth Poisson sampler
        while True:
            p *= rng.random()
            if p <= l:
                break
            k += 1
        cost = rng.gammavariate(4.0, math.exp(1.0 + 0.4 * x2) / 4.0)
        rows.append([x1, x2, noise, grp, y, cls, k, cost, exposure])
    return rows


ROWS = make_rows()


def col(name):
    return np.array([r[[f[0] for f in F].index(name)] for r in ROWS])


def coef(out, term):
    return next(r for r in out['coefficients'].to_dicts() if r['Term'] == term)


def chain(*stages):
    """Build a workflow: training data -> model tool -> (Score etc.)"""
    return stages


class RegressionTests(unittest.TestCase):
    def test_linear_regression_matches_least_squares(self):
        out, _ = run_tool('core.linear_regression', {'target_field': 'y', 'predictors': ['x1', 'x2', 'grp']}, {'input': (F, ROWS)})
        X = np.column_stack([np.ones(len(ROWS)), col('x1'), col('x2'), (col('grp') == 'b').astype(float)])
        beta, *_ = np.linalg.lstsq(X, col('y'), rcond=None)
        for term, b in zip(['(Intercept)', 'x1', 'x2', 'grp_b'], beta):
            self.assertAlmostEqual(coef(out, term)['Estimate'], b, places=8)
        resid = col('y') - X @ beta
        r2 = 1 - (resid ** 2).sum() / ((col('y') - col('y').mean()) ** 2).sum()
        fit = {r['Metric']: r['Value'] for r in out['fit'].to_dicts()}
        self.assertAlmostEqual(fit['R2'], r2, places=10)
        self.assertLess(coef(out, 'x1')['PValue'], 1e-50)

    def test_logistic_regression_matches_unpenalised_sklearn(self):
        from sklearn.linear_model import LogisticRegression
        out, _ = run_tool('core.logistic_regression', {'target_field': 'cls', 'predictors': ['x1', 'x2']}, {'input': (F, ROWS)})
        ref = LogisticRegression(C=np.inf, tol=1e-12, max_iter=100000).fit(np.column_stack([col('x1'), col('x2')]), col('cls') == 'yes')
        self.assertAlmostEqual(coef(out, 'x1')['Estimate'], ref.coef_[0][0], places=4)
        self.assertAlmostEqual(coef(out, '(Intercept)')['Estimate'], ref.intercept_[0], places=4)
        self.assertAlmostEqual(coef(out, 'x1')['ExpEstimate'], math.exp(coef(out, 'x1')['Estimate']))
        sep = [[float(i), 0.0, 0.0, 'a', 0.0, 'yes' if i > 10 else 'no', 0, 1.0, 1.0] for i in range(20)]
        result = run_workflow(workflow([text_input('t', F, sep), {'id': 'm', 'tool': 'core.logistic_regression',
                                                                   'config': {'target_field': 'cls', 'predictors': ['x1']}}],
                                       [edge('t', 'output', 'm', 'input')]), base_dir=ROOT)
        self.assertIn('separation', result.error['message'])

    def test_count_and_gamma_recover_known_effects(self):
        poisson, _ = run_tool('core.count_regression', {'target_field': 'count', 'predictors': ['x1'], 'offset_field': 'exposure'},
                              {'input': (F, ROWS)})
        self.assertAlmostEqual(coef(poisson, 'x1')['Estimate'], 0.5, delta=0.1)
        self.assertAlmostEqual(coef(poisson, '(Intercept)')['Estimate'], 0.3, delta=0.15)
        nb, _ = run_tool('core.count_regression', {'target_field': 'count', 'predictors': ['x1'], 'offset_field': 'exposure',
                                                   'distribution': 'negative_binomial'}, {'input': (F, ROWS)})
        self.assertIn('Alpha', {r['Metric'] for r in nb['fit'].to_dicts()})
        gamma, _ = run_tool('core.gamma_regression', {'target_field': 'cost', 'predictors': ['x2']}, {'input': (F, ROWS)})
        self.assertAlmostEqual(coef(gamma, 'x2')['Estimate'], 0.4, delta=0.08)
        bad = [[0, 0, 0, 'a', 0, 'no', -1, 1.0, 1.0]] * 5
        with self.assertRaisesRegex(AssertionError, 'non-negative whole-number'):
            run_tool('core.count_regression', {'target_field': 'count', 'predictors': ['x1']}, {'input': (F, bad)})


class LearnerAndScoreTests(unittest.TestCase):
    def score(self, model_tool, cfg, data=None):
        nodes = [text_input('train', F, ROWS), text_input('new', F, data or ROWS[:50]),
                 {'id': 'm', 'tool': model_tool, 'config': cfg}, {'id': 's', 'tool': 'core.score', 'config': {}}]
        edges = [edge('train', 'output', 'm', 'input'), edge('m', 'model', 's', 'model'), edge('new', 'output', 's', 'input')]
        result = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        return result

    def test_every_classifier_scores_probabilities_that_sum_to_one(self):
        cfg = {'target_field': 'cls', 'predictors': ['x1', 'x2', 'grp']}
        for tool in ('core.logistic_regression', 'core.decision_tree', 'core.forest_model', 'core.boosted_model',
                     'core.support_vector_machine', 'core.neural_network', 'core.spline_model', 'core.naive_bayes'):
            with self.subTest(tool=tool):
                out = self.score(tool, cfg).output('s').to_dicts()
                self.assertTrue(all(abs(r['Score_no'] + r['Score_yes'] - 1) < 1e-9 for r in out))
                accuracy = sum(r['Prediction'] == r['cls'] for r in out) / len(out)
                self.assertGreater(accuracy, 0.8)

    def test_regression_learners_and_spline_capture_curvature(self):
        rng = random.Random(1)
        curved = [[x, 0.0, 0.0, 'a', x * x + rng.gauss(0, 0.1), 'no', 0, 1.0, 1.0] for x in np.linspace(-3, 3, 200)]
        r2 = {}
        for tool in ('core.linear_regression', 'core.spline_model', 'core.boosted_model'):
            out, _ = run_tool(tool, {'target_field': 'y', 'predictors': ['x1']}, {'input': (F, curved)})
            r2[tool] = {m['Metric']: m['Value'] for m in out['fit'].to_dicts()}['R2']
        self.assertLess(r2['core.linear_regression'], 0.05)  # a straight line cannot fit a parabola
        self.assertGreater(r2['core.spline_model'], 0.98)
        self.assertGreater(r2['core.boosted_model'], 0.98)

    def test_score_handles_missing_and_unseen_values(self):
        new = [list(ROWS[0]), list(ROWS[1])]
        new[0][0] = None          # missing predictor -> null score
        new[1][3] = 'zzz'         # unseen category -> encoded as baseline, warned
        result = self.score('core.linear_regression', {'target_field': 'y', 'predictors': ['x1', 'x2', 'grp']}, new)
        scores = result.output('s').column('Score')
        self.assertIsNone(scores[0])
        self.assertIsNotNone(scores[1])
        self.assertTrue(any('did not see' in w['message'] for w in result.warnings()))

    def test_tree_outputs_and_importance(self):
        out, _ = run_tool('core.decision_tree', {'target_field': 'cls', 'predictors': ['x1', 'x2', 'noise'], 'max_depth': 3},
                          {'input': (F, ROWS)})
        tree = out['tree'].to_dicts()
        self.assertEqual(tree[0]['Samples'], len(ROWS))
        self.assertTrue(tree[0]['Split'].startswith(('x1 <=', 'x2 <=')))
        importance = {r['Field']: r['Importance'] for r in out['importance'].to_dicts()}
        self.assertLess(importance['noise'], importance['x1'])
        self.assertAlmostEqual(sum(importance.values()), 1.0)


class EvaluationTests(unittest.TestCase):
    def models_workflow(self, extra_nodes, extra_edges, data=None):
        nodes = [text_input('train', F, ROWS), text_input('valid', F, data or make_rows(200, seed=99)),
                 {'id': 'logit', 'tool': 'core.logistic_regression', 'config': {'target_field': 'cls', 'predictors': ['x1', 'x2'], 'model_name': 'Logit'}},
                 {'id': 'tree', 'tool': 'core.decision_tree', 'config': {'target_field': 'cls', 'predictors': ['x1', 'x2'], 'model_name': 'Tree', 'max_depth': 2}}]
        edges = [edge('train', 'output', 'logit', 'input'), edge('train', 'output', 'tree', 'input')]
        result = run_workflow(workflow(nodes + extra_nodes, edges + extra_edges), base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        return result

    def test_comparison_cross_validation_and_lift(self):
        tools = [{'id': 'cmp', 'tool': 'core.model_comparison', 'config': {}},
                 {'id': 'cv', 'tool': 'core.cross_validation', 'config': {'folds': 4}},
                 {'id': 'lift', 'tool': 'core.lift_chart', 'config': {}},
                 {'id': 'coef', 'tool': 'core.model_coefficients', 'config': {}}]
        edges = [edge(m, 'model', t, 'models') for t in ('cmp', 'cv', 'lift', 'coef') for m in ('logit', 'tree')]
        edges += [edge('valid', 'output', t, 'input') for t in ('cmp', 'lift')] + [edge('train', 'output', 'cv', 'input')]
        r = self.models_workflow(tools, edges)
        cmp = {row['Model']: row for row in r.output('cmp').to_dicts()}
        self.assertGreater(cmp['Logit']['AUC'], cmp['Tree']['AUC'])  # a depth-2 tree is coarser than the true linear boundary
        self.assertEqual(sum(row['Count'] for row in r.output('cmp', 'confusion').to_dicts() if row['Model'] == 'Logit'), 200)
        self.assertEqual(len([f for f in r.output('cv', 'folds').to_dicts() if f['Model'] == 'Logit']), 4)
        summary = {(s['Model'], s['Metric']): s['Mean'] for s in r.output('cv', 'summary').to_dicts()}
        self.assertGreater(summary[('Logit', 'Accuracy')], 0.8)
        lift = [row for row in r.output('lift').to_dicts() if row['Model'] == 'Logit']
        self.assertEqual(len(lift), 10)
        self.assertAlmostEqual(lift[-1]['CumulativeCapturePct'], 100.0)
        self.assertGreater(lift[0]['Lift'], 1.5)
        terms = [row['Term'] for row in r.output('coef').to_dicts() if row['Model'] == 'Logit']
        self.assertEqual(terms, ['(Intercept)', 'x1', 'x2'])

    def test_lift_of_a_perfect_ranking(self):
        perfect = [[float(i), 0.0, 0.0, 'a', 0.0, 'yes' if i >= 150 else 'no', 0, 1.0, 1.0] for i in range(200)]
        nodes = [text_input('t', F, perfect), {'id': 'm', 'tool': 'core.decision_tree', 'config': {'target_field': 'cls', 'predictors': ['x1']}},
                 {'id': 'lift', 'tool': 'core.lift_chart', 'config': {'bins': 4}}]
        r = run_workflow(workflow(nodes, [edge('t', 'output', 'm', 'input'), edge('m', 'model', 'lift', 'models'),
                                          edge('t', 'output', 'lift', 'input')]), base_dir=ROOT)
        first = r.output('lift').to_dicts()[0]
        self.assertEqual((first['Positives'], first['CumulativeCapturePct'], first['Lift']), (50, 100.0, 4.0))

    def test_stepwise_drops_noise_and_nested_test_matches_statsmodels(self):
        import statsmodels.api as sm
        nodes = [text_input('t', F, ROWS),
                 {'id': 'full', 'tool': 'core.linear_regression', 'config': {'target_field': 'y', 'predictors': ['x1', 'x2', 'noise']}},
                 {'id': 'red', 'tool': 'core.linear_regression', 'config': {'target_field': 'y', 'predictors': ['x1']}},
                 {'id': 'step', 'tool': 'core.stepwise', 'config': {}},
                 {'id': 'nest', 'tool': 'core.nested_test', 'config': {}}]
        edges = [edge('t', 'output', 'full', 'input'), edge('t', 'output', 'red', 'input'), edge('full', 'model', 'step', 'model'),
                 edge('t', 'output', 'step', 'input'), edge('red', 'model', 'nest', 'models'), edge('full', 'model', 'nest', 'models'),
                 edge('t', 'output', 'nest', 'input')]
        r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        kept = [row['Term'] for row in r.output('step', 'coefficients').to_dicts()]
        self.assertEqual(kept, ['(Intercept)', 'x1', 'x2'])
        test = r.output('nest').to_dicts()[0]
        full = sm.OLS(col('y'), sm.add_constant(np.column_stack([col('x1'), col('x2'), col('noise')]))).fit()
        red = sm.OLS(col('y'), sm.add_constant(col('x1'))).fit()
        lr, p, df = full.compare_lr_test(red)
        self.assertAlmostEqual(test['LRStatistic'], lr, places=6)
        self.assertEqual(test['DF'], int(df))


class StatisticsTests(unittest.TestCase):
    def test_test_of_means_and_vif(self):
        from scipy import stats
        from statsmodels.stats.outliers_influence import variance_inflation_factor
        out, _ = run_tool('core.test_of_means', {'value_field': 'y', 'group_field': 'grp'}, {'input': (F, ROWS)})
        row = out['output'].to_dicts()[0]
        ya, yb = col('y')[col('grp') == 'a'], col('y')[col('grp') == 'b']
        ref = stats.ttest_ind(ya, yb, equal_var=False)
        self.assertAlmostEqual(row['T'], ref.statistic)
        self.assertAlmostEqual(row['PValue'], ref.pvalue)
        collinear = [r[:2] + [r[0] * 2 + r[1] + RNG.gauss(0, 0.05)] + r[3:] for r in ROWS]
        vif, _ = run_tool('core.variance_inflation_factors', {'predictors': ['x1', 'x2', 'noise']}, {'input': (F, collinear)})
        X = np.column_stack([np.ones(len(collinear))] + [np.array([r[i] for r in collinear]) for i in range(3)])
        for j, row in enumerate(vif['output'].to_dicts(), start=1):
            self.assertAlmostEqual(row['VIF'], variance_inflation_factor(X, j), places=6)
        self.assertGreater(vif['output'].to_dicts()[2]['VIF'], 100)


class SurvivalNetworkTests(unittest.TestCase):
    def test_kaplan_meier_matches_hand_calculation(self):
        # times 1,2,2,3,4 with events 1,1,0,1,0: S(1)=4/5, S(2)=4/5*3/4=0.6, S(3)=0.6*1/2=0.3
        rows = [[1.0, 1], [2.0, 1], [2.0, 0], [3.0, 1], [4.0, 0]]
        out, _ = run_tool('core.survival_analysis', {'time_field': 't', 'event_field': 'e'}, {'input': ([('t', 'double'), ('e', 'int32')], rows)})
        km = {r['Time']: r for r in out['survival'].to_dicts()}
        for t, expected in [(1.0, 0.8), (2.0, 0.6), (3.0, 0.3), (4.0, 0.3)]:
            self.assertAlmostEqual(km[t]['Survival'], expected)
        self.assertEqual((km[2.0]['AtRisk'], km[2.0]['Events'], km[2.0]['Censored']), (4, 1, 1))

    def test_cox_model_and_survival_score(self):
        rng = random.Random(5)
        rows = []
        for i in range(300):
            x = rng.choice([0.0, 1.0])
            t = rng.expovariate(0.1 * math.exp(0.8 * x))
            rows.append([min(t, 20.0), 1 if t < 20 else 0, x])
        fields = [('t', 'double'), ('e', 'int32'), ('x', 'double')]
        nodes = [text_input('d', fields, rows),
                 {'id': 'sa', 'tool': 'core.survival_analysis', 'config': {'time_field': 't', 'event_field': 'e', 'predictors': ['x']}},
                 {'id': 'ss', 'tool': 'core.survival_score', 'config': {'times': [2, 10]}}]
        r = run_workflow(workflow(nodes, [edge('d', 'output', 'sa', 'input'), edge('sa', 'model', 'ss', 'model'),
                                          edge('d', 'output', 'ss', 'input')]), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        hr = r.output('sa', 'coefficients').to_dicts()[0]['ExpEstimate']
        self.assertAlmostEqual(math.log(hr), 0.8, delta=0.3)
        scored = r.output('ss').to_dicts()
        exposed = next(s for s in scored if s['x'] == 1.0)
        unexposed = next(s for s in scored if s['x'] == 0.0)
        self.assertGreater(exposed['RelativeRisk'], unexposed['RelativeRisk'])
        self.assertLess(exposed['Survival_10'], exposed['Survival_2'])  # survival falls over time
        self.assertAlmostEqual(unexposed['Survival_10'], math.exp(-0.1 * 10), delta=0.1)

    def test_network_centrality_and_communities(self):
        edges = [('hub', f'n{i}') for i in range(5)] + [('a', 'b'), ('b', 'c'), ('a', 'c'), ('c', 'hub')]
        out, _ = run_tool('core.network_analysis', {'from_field': 'f', 'to_field': 't'},
                          {'input': ([('f', 'v_wstring'), ('t', 'v_wstring')], [list(e) for e in edges])})
        nodes = {r['Node']: r for r in out['nodes'].to_dicts()}
        self.assertEqual(nodes['hub']['Degree'], 6)
        self.assertEqual(max(nodes, key=lambda n: nodes[n]['Betweenness']), 'hub')
        self.assertEqual(nodes['n0']['Betweenness'], 0.0)
        self.assertEqual(len({nodes[n]['Community'] for n in ('a', 'b', 'c')}), 1)
        summary = {r['Metric']: r['Value'] for r in out['summary'].to_dicts()}
        self.assertEqual((summary['Nodes'], summary['Edges']), (9, 9))
        self.assertTrue(out['chart'].column('Chart')[0].startswith('<svg'))


class SafetyTests(unittest.TestCase):
    def test_models_with_untrusted_types_are_refused(self):
        import pathlib
        import skops.io as sio
        from flowengine.modeling import predict
        from flowengine.table import Table
        from flowengine.types import Field, FieldType, Schema
        payload = sio.dumps({'x': pathlib.PurePosixPath('/tmp')})  # skops flags this type as untrusted
        with self.assertRaisesRegex(Exception, 'not trusted'):
            sk_loads(payload)
        info = {'family': 'forest', 'task': 'regression', 'target': 'y', 'predictors': ['x1'],
                'design': [{'field': 'x1', 'kind': 'numeric'}], 'classes': None}
        with self.assertRaisesRegex(Exception, 'not trusted'):
            predict(info, payload, Table.from_rows(Schema([Field('x1', FieldType.DOUBLE)]), [(1.0,)]))
        trusted = model_table('ok', 'forest', 'y', info, sio.dumps(__import__('sklearn.linear_model', fromlist=['LinearRegression']).LinearRegression()))
        self.assertEqual(len(trusted), 1)

    def test_score_rejects_non_model_input(self):
        nodes = [text_input('a', F, ROWS[:3]), text_input('b', F, ROWS[:3]), {'id': 's', 'tool': 'core.score', 'config': {}}]
        with self.assertRaisesRegex(WorkflowValidationError, 'Model output'):
            run_workflow(workflow(nodes, [edge('a', 'output', 's', 'model'), edge('b', 'output', 's', 'input')]), base_dir=ROOT)


if __name__ == '__main__':
    unittest.main()


class ExampleTests(unittest.TestCase):
    def test_fuel_models_example(self):
        from flowengine.executor import run_file
        result = run_file(ROOT / 'examples/predictive/fuel_models.flow.json')
        self.assertTrue(result.ok, result.error)
        compare = {r['Model']: r for r in result.output('compare').to_dicts()}
        self.assertGreater(compare['Logistic']['AUC'], 0.9)
        self.assertGreater(compare['Forest']['Accuracy'], 0.85)
        scored = result.output('score').to_dicts()
        rmse = math.sqrt(sum((float(r['PetrolPencePerLitre']) - r['Score']) ** 2 for r in scored) / len(scored))
        self.assertLess(rmse, 5)  # pence per litre on held-out weeks
