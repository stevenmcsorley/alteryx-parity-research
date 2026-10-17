"""Batch 9: Machine Learning pipeline tools checked against known answers and direct scikit-learn fits."""
import json
import math
import random
import unittest

import numpy as np

from flowengine.executor import run_workflow

from helpers import ROOT, edge, run_tool, run_tool_result, text_input, workflow


def run(nodes, edges):
    r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
    assert r.ok, r.error
    return r


def node(nid, tool, config=None):
    return {'id': nid, 'tool': tool, 'config': config or {}}


# Two well-separated classes from two numeric features and a category that carries no signal.
_rng = random.Random(4)
CLS = [[round(_rng.gauss(0 if k == 'no' else 3, 0.7), 4), round(_rng.gauss(0 if k == 'no' else -2, 0.7), 4),
        _rng.choice(['red', 'blue']), f'c{i:03d}', k] for i, k in enumerate(['no', 'yes'] * 40)]
CF = [('a', 'double'), ('b', 'double'), ('colour', 'v_wstring'), ('id', 'v_wstring'), ('label', 'v_wstring')]


class PipelineTests(unittest.TestCase):
    def test_feature_types_autodetect_and_override(self):
        rows = [[i, i * 0.5, ['x', 'y'][i % 2], 'yes' if i % 3 else 'no', f'2024-01-{i % 28 + 1:02d}',
                 f'a longer sentence number {i} with words'] for i in range(40)]
        fields = [('n', 'int64'), ('d', 'double'), ('c', 'v_wstring'), ('b', 'v_wstring'), ('when', 'v_wstring'), ('t', 'v_wstring')]
        out, _ = run_tool('core.ml_feature_types', {'overrides': {'n': 'Index'}}, {'input': (fields, rows)})
        types = {r['Name']: r['FeatureType'] for r in out['types'].to_dicts()}
        self.assertEqual(types, {'n': 'Index', 'd': 'Double', 'c': 'Categorical', 'b': 'Boolean', 'when': 'Datetime',
                                 't': 'NaturalLanguage'})
        info = json.loads(out['output'].column('ML_Pipeline')[0])
        self.assertEqual(info['feature_types']['n'], 'Index')
        self.assertEqual(out['output'].column('d'), [r[1] for r in rows])

    def test_classification_pipeline_matches_a_direct_scikit_learn_fit(self):
        from sklearn.ensemble import RandomForestClassifier
        train, new = CLS[:60], CLS[60:]
        r = run([text_input('d', CF, train), text_input('n', CF[:4], [row[:4] for row in new]),
                 node('t', 'core.ml_transformation', {'ids': ['id'], 'one_hot': ['colour']}),
                 node('c', 'core.ml_classification', {'target_field': 'label', 'algorithm': 'random_forest', 'n_estimators': 50}),
                 node('f', 'core.ml_fit'), node('p', 'core.ml_predict')],
                [edge('d', 'output', 't', 'input'), edge('t', 'output', 'c', 'input'), edge('c', 'output', 'f', 'input'),
                 edge('f', 'model', 'p', 'model'), edge('n', 'output', 'p', 'input')])
        X = np.array([[row[0], row[1], row[2] == 'blue', row[2] == 'red'] for row in train], dtype=float)
        y = np.array([row[4] == 'yes' for row in train], dtype=int)
        direct = RandomForestClassifier(n_estimators=50, max_depth=10, max_features='sqrt', random_state=10, n_jobs=1).fit(X, y)
        Xn = np.array([[row[0], row[1], row[2] == 'blue', row[2] == 'red'] for row in new], dtype=float)
        out = r.output('p').to_dicts()
        np.testing.assert_allclose([o['Probability_yes'] for o in out], direct.predict_proba(Xn)[:, 1])
        self.assertEqual([o['Prediction'] for o in out], [row[4] for row in new])
        importance = {x['Field']: x['Importance'] for x in r.output('f', 'importance').to_dicts()}
        self.assertNotIn('id', importance)
        self.assertGreater(importance['a'] + importance['b'], 0.8)
        self.assertEqual(r.output('f', 'model').column('ModelType'), ['ml_pipeline'])

    def test_missing_values_need_a_clean_up_and_the_training_median_is_reused(self):
        rows = [[float(x), 2.0 * x + 1] for x in range(1, 21)] + [[None, 99.0]]
        fields = [('x', 'double'), ('y', 'double')]
        reg = node('r', 'core.ml_regression', {'target_field': 'y', 'algorithm': 'linear_regression'})
        failed = run_workflow(workflow([text_input('d', fields, rows), reg, node('f', 'core.ml_fit')],
                                       [edge('d', 'output', 'r', 'input'), edge('r', 'output', 'f', 'input')]), base_dir=ROOT)
        self.assertFalse(failed.ok)
        self.assertIn('Clean Up Missing Values', str(failed.error))
        clean = rows[:-1]
        r = run([text_input('d', fields, clean), text_input('n', [('x', 'double')], [[None], [100.0]]),
                 node('t', 'core.ml_transformation', {'impute_median': ['x']}), reg, node('f', 'core.ml_fit'),
                 node('p', 'core.ml_predict')],
                [edge('d', 'output', 't', 'input'), edge('t', 'output', 'r', 'input'), edge('r', 'output', 'f', 'input'),
                 edge('f', 'model', 'p', 'model'), edge('n', 'output', 'p', 'input')])
        preds = r.output('p').column('Prediction')
        self.assertAlmostEqual(preds[0], 2.0 * 10.5 + 1)  # median of 1..20
        self.assertAlmostEqual(preds[1], 201.0)
        fit = {m['Metric']: m['Value'] for m in r.output('f', 'fit').to_dicts()}
        self.assertAlmostEqual(fit['R2'], 1.0)

    def test_unknown_categories_can_be_an_error(self):
        rows = [[c, float(i % 2)] for i, c in enumerate(['a', 'b'] * 10)]
        fields = [('cat', 'v_wstring'), ('y', 'double')]
        r = run_workflow(workflow([
            text_input('d', fields, rows), text_input('n', [('cat', 'v_wstring')], [['zzz']]),
            node('t', 'core.ml_transformation', {'one_hot': ['cat'], 'unknown': 'error'}),
            node('r', 'core.ml_regression', {'target_field': 'y', 'algorithm': 'decision_tree'}),
            node('f', 'core.ml_fit'), node('p', 'core.ml_predict')],
            [edge('d', 'output', 't', 'input'), edge('t', 'output', 'r', 'input'), edge('r', 'output', 'f', 'input'),
             edge('f', 'model', 'p', 'model'), edge('n', 'output', 'p', 'input')]), base_dir=ROOT)
        self.assertFalse(r.ok)
        self.assertIn("'zzz'", str(r.error))

    def test_pipeline_survives_sampling_and_refits_in_cross_validation(self):
        r = run([text_input('d', CF, CLS), node('c', 'core.ml_classification', {'target_field': 'label', 'features': ['a', 'b']}),
                 node('s', 'core.sample', {'mode': 'first', 'n': 50}), node('f', 'core.ml_fit'),
                 node('cv', 'core.cross_validation', {'folds': 5})],
                [edge('d', 'output', 'c', 'input'), edge('c', 'output', 's', 'input'), edge('s', 'output', 'f', 'input'),
                 edge('f', 'model', 'cv', 'models'), edge('d', 'output', 'cv', 'input')])
        self.assertEqual(json.loads(r.output('f', 'model').column('Info')[0])['algorithm'], 'logistic_regression')
        summary = {m['Metric']: m['Mean'] for m in r.output('cv', 'summary').to_dicts()}
        self.assertGreater(summary['Accuracy'], 0.95)


class DataHealthTests(unittest.TestCase):
    def test_scores_outliers_and_recommendations(self):
        rows = [[float(i % 10), None if i < 10 else 'k', 1.0, 'x' if i else 'y'] for i in range(40)]
        rows[5][0] = 1000.0
        fields = [('v', 'double'), ('half', 'v_wstring'), ('const', 'double'), ('mostly', 'v_wstring')]
        out, _ = run_tool('core.data_health', {'recommendations': True, 'scale': 'unit'}, {'input': (fields, rows)})
        s = {r['Field']: r for r in out['scores'].to_dicts()}
        self.assertAlmostEqual(s['v']['Outliers'], 1 / 40)
        self.assertAlmostEqual(s['v']['Score'], 39 / 40)
        self.assertAlmostEqual(s['half']['Missing'], 0.25)
        self.assertAlmostEqual(s['const']['Score'], 0.0)
        self.assertIn('Constant', s['const']['Recommendation'])
        self.assertAlmostEqual(s['mostly']['Sparsity'], 39 / 40)
        self.assertAlmostEqual(s['mostly']['Score'], 1 - (39 / 40 - 0.9) / 0.1)
        self.assertAlmostEqual(s['(Overall)']['Score'], np.mean([s[k]['Score'] for k in ('v', 'half', 'const', 'mostly')]))
        outliers = out['outliers'].to_dicts()
        self.assertEqual([(o['RecordID'], o['OutlierFields']) for o in outliers], [(6, 'v')])
        self.assertEqual(len(out['report']), 3)

    def test_needs_thirty_records(self):
        r = run_tool_result('core.data_health', {}, {'input': ([('v', 'double')], [[1.0]] * 29)})
        self.assertFalse(r.ok)


class BuildFeaturesTests(unittest.TestCase):
    CUST = ([('cid', 'int64'), ('region', 'v_wstring'), ('joined', 'date')],
            [[1, 'north', '2024-03-02'], [2, 'south', '2024-06-15'], [3, 'north', '2024-07-20']])
    SESS = ([('sid', 'int64'), ('cid', 'int64'), ('device', 'v_wstring')],
            [[10, 1, 'web'], [11, 1, 'app'], [12, 2, 'web']])
    TX = ([('tid', 'int64'), ('sid', 'int64'), ('amount', 'double')],
          [[100, 10, 5.0], [101, 10, 7.0], [102, 11, 3.0], [103, 12, 10.0]])
    RELS = [{'parent': 1, 'parent_key': 'cid', 'child': 2, 'child_key': 'cid'},
            {'parent': 2, 'parent_key': 'sid', 'child': 3, 'child_key': 'sid'}]

    def test_aggregations_transforms_and_stacked_features(self):
        out, _ = run_tool('core.build_features', {'table_names': ['customers', 'sessions', 'transactions'], 'target_key': 'cid',
                                                  'relationships': self.RELS, 'depth': 2},
                          {'inputs': [self.CUST, self.SESS, self.TX]})
        rows = {r['cid']: r for r in out['output'].to_dicts()}
        self.assertEqual([rows[c]['COUNT(sessions)'] for c in (1, 2, 3)], [2, 1, 0])
        self.assertEqual(rows[1]['MONTH(joined)'], 3)
        self.assertEqual(rows[2]['WEEKDAY(joined)'], 5)
        self.assertEqual(rows[1]['MODE(sessions.device)'], 'app')  # tie broken alphabetically
        self.assertEqual(rows[1]['NUM_UNIQUE(sessions.device)'], 2)
        self.assertAlmostEqual(rows[1]['MEAN(sessions.SUM(transactions.amount))'], (12.0 + 3.0) / 2)
        self.assertAlmostEqual(rows[1]['SUM(sessions.COUNT(transactions))'], 3)
        self.assertIsNone(rows[3]['MEAN(sessions.SUM(transactions.amount))'])
        self.assertNotIn('SUM(sessions.sid)', rows[1])  # keys are not features

    def test_direct_features_from_a_parent(self):
        out, _ = run_tool('core.build_features', {'table_names': ['customers', 'sessions'], 'target_table': 2, 'target_key': 'sid',
                                                  'relationships': self.RELS[:1], 'depth': 1},
                          {'inputs': [self.CUST, self.SESS]})
        self.assertEqual(out['output'].column('customers.region'), ['north', 'north', 'south'])


class AssistedModelingTests(unittest.TestCase):
    def test_compares_algorithms_and_returns_a_usable_model(self):
        r = run([text_input('d', CF, CLS), text_input('n', CF[:4], [[3.1, -2.2, 'red', 'zz1'], [0.1, 0.2, 'blue', 'zz2']]),
                 node('a', 'core.assisted_modeling', {'target_field': 'label', 'folds': 4}), node('p', 'core.ml_predict'),
                 node('f', 'core.ml_fit')],
                [edge('d', 'output', 'a', 'input'), edge('a', 'model', 'p', 'model'), edge('n', 'output', 'p', 'input'),
                 edge('a', 'pipeline', 'f', 'input')])
        board = r.output('a', 'leaderboard').to_dicts()
        self.assertEqual(len(board), 4)
        self.assertEqual(sum(b['Selected'] for b in board), 1)
        self.assertTrue(board[0]['Selected'])
        self.assertGreaterEqual(board[0]['Accuracy'], 0.95)
        self.assertEqual([b['Score'] for b in board], sorted((b['Score'] for b in board), reverse=True))
        steps = {p['Field']: p['Step'] for p in r.output('a', 'preparation').to_dicts()}
        self.assertEqual(steps['id'], 'Dropped: identifier')
        self.assertEqual(steps['colour'], 'One-hot encode')
        self.assertEqual(r.output('p').column('Prediction'), ['yes', 'no'])
        best = next(b['Algorithm'] for b in board if b['Selected'])
        self.assertEqual(r.output('f', 'model').column('Name'), [best])

    def test_regression_metric_and_task_checks(self):
        rows = [[float(x), 3.0 * x - 4 + (0.01 if x % 2 else -0.01)] for x in range(30)]
        fields = [('x', 'double'), ('y', 'double')]
        out, _ = run_tool('core.assisted_modeling', {'target_field': 'y', 'metric': 'mae'}, {'input': (fields, rows)})
        board = out['leaderboard'].to_dicts()
        self.assertEqual(board[0]['Algorithm'], 'Linear Regression')
        self.assertLess(board[0]['MAE'], 0.05)
        bad = run_tool_result('core.assisted_modeling', {'target_field': 'y', 'metric': 'accuracy'}, {'input': (fields, rows)})
        self.assertFalse(bad.ok)


if __name__ == '__main__':
    unittest.main()
