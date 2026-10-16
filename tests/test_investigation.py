"""Data Investigation kernels checked against independently computed values."""
import math
import statistics
import unittest

from flowengine.errors import WorkflowValidationError

from helpers import run_tool

DATA = ([('region', 'v_wstring'), ('amount', 'double'), ('units', 'int32'), ('day', 'date')],
        [['N', 10.0, 1, '2026-01-01'], ['S', 20.0, 2, '2026-01-03'], ['N', None, 2, None],
         ['E', 40.0, 4, '2026-01-02'], ['N', 30.0, 3, '2026-01-05']])


def first(table, **where):
    return next(r for r in table.to_dicts() if all(r[k] == v for k, v in where.items()))


class ProfileTests(unittest.TestCase):
    def test_basic_data_profile(self):
        out, _ = run_tool('core.basic_data_profile', {}, {'input': DATA})
        amount, region, day = (first(out['profile'], Field=f) for f in ('amount', 'region', 'day'))
        self.assertEqual((amount['Records'], amount['Missing'], amount['CompletenessPct']), (5, 1, 80.0))
        self.assertEqual((amount['Min'], amount['Max'], amount['Median']), ('10', '40', 25.0))
        self.assertAlmostEqual(amount['StdDev'], statistics.stdev([10, 20, 40, 30]))
        self.assertAlmostEqual(amount['Variance'], statistics.variance([10, 20, 40, 30]))
        self.assertEqual((region['Distinct'], region['MinLength'], region['TopValues']), (3, 1, 'N (3); E (1); S (1)'))
        self.assertEqual((day['Min'], day['Max']), ('2026-01-01', '2026-01-05'))
        overview = out['overview'].to_dicts()[0]
        self.assertEqual((overview['Records'], overview['Fields'], overview['CompleteRecords']), (5, 4, 4))
        sampled, _ = run_tool('core.basic_data_profile', {'fields': ['units'], 'sample_records': 2}, {'input': DATA})
        self.assertEqual(sampled['profile'].column('Records'), [2])
        empty, _ = run_tool('core.basic_data_profile', {}, {'input': (DATA[0], [])})
        self.assertEqual(first(empty['profile'], Field='amount')['CompletenessPct'], None)

    def test_field_summary_quartiles_and_mode(self):
        out, _ = run_tool('core.field_summary', {'fields': ['units', 'region']}, {'input': DATA})
        units, region = first(out['output'], Field='units'), first(out['output'], Field='region')
        self.assertEqual((units['Q1'], units['Median'], units['Q3'], units['Mode']), (2.0, 2.0, 3.0, '2'))
        self.assertEqual((region['Unique'], region['PercentMissing'], region['LongestLength']), (3, 0.0, 1))


class FrequencyHistogramTests(unittest.TestCase):
    def test_frequencies_reconcile_with_records(self):
        rows = [['a'], ['b'], ['a'], [None], ['c'], ['a'], ['b']]
        out, _ = run_tool('core.frequency_table', {'fields': ['k']}, {'input': ([('k', 'v_wstring')], rows)})
        t = out['output'].to_dicts()
        self.assertEqual([(r['Value'], r['Frequency']) for r in t], [('a', 3), ('b', 2), ('[Null]', 1), ('c', 1)])
        self.assertEqual(t[-1]['CumulativeFrequency'], len(rows))
        self.assertAlmostEqual(t[-1]['CumulativePercent'], 100.0)
        out, _ = run_tool('core.frequency_table', {'fields': ['k'], 'include_nulls': False, 'max_categories': 1,
                                                   'sort': 'frequency_desc'}, {'input': ([('k', 'v_wstring')], rows)})
        self.assertEqual([(r['Value'], r['Frequency']) for r in out['output'].to_dicts()], [('a', 3), ('(Other)', 3)])
        out, _ = run_tool('core.frequency_table', {'fields': ['n'], 'sort': 'value_asc'},
                          {'input': ([('n', 'int32')], [[10], [2], [10]])})
        self.assertEqual(out['output'].column('Value'), ['2', '10'])  # numeric, not text, ordering

    def test_histogram_bins_and_density(self):
        values = [[float(v)] for v in [0, 1, 1, 2, 3, 3, 3, 4, 9, 10]] + [[None], [float('nan')]]
        out, _ = run_tool('core.histogram', {'field': 'x', 'binning': 'fixed', 'bins': 5}, {'input': ([('x', 'double')], values)})
        t = out['output'].to_dicts()
        self.assertEqual([(r['LowerBound'], r['UpperBound']) for r in t], [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10)])
        self.assertEqual([r['Count'] for r in t], [3, 4, 1, 0, 2])  # 10 falls in the closed last bin
        self.assertAlmostEqual(sum(r['Density'] * 2 for r in t), 1.0)
        self.assertEqual(t[-1]['CumulativeCount'], 10)
        out, _ = run_tool('core.histogram', {'field': 'x', 'binning': 'fixed', 'bins': 2, 'range_min': 0, 'range_max': 4},
                          {'input': ([('x', 'double')], values)})
        self.assertEqual(sum(out['output'].column('Count')), 8)  # 9 and 10 are out of range
        out, _ = run_tool('core.histogram', {'field': 'x', 'binning': 'sturges'}, {'input': ([('x', 'double')], values)})
        self.assertEqual(len(out['output']), math.ceil(math.log2(10)) + 1)
        with self.assertRaises(WorkflowValidationError):
            run_tool('core.histogram', {'field': 'k'}, {'input': ([('k', 'v_wstring')], [['a']])})


def _ranks(xs):
    order = sorted(range(len(xs)), key=xs.__getitem__)
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


class CorrelationTests(unittest.TestCase):
    def test_spearman_matches_pearson_of_average_ranks(self):
        x = [1, 2, 2, 3, 4, 5, 5, 6]
        y = [2, 1, 4, 3, 7, 8, 6, 9]
        rows = [[a, b, 7, (None if i == 2 else float(i))] for i, (a, b) in enumerate(zip(x, y))]
        fields = [('x', 'int32'), ('y', 'int32'), ('c', 'int32'), ('z', 'double')]
        out, _ = run_tool('core.spearman_correlation', {'fields': ['x', 'y', 'c', 'z']}, {'input': (fields, rows)})
        xy = first(out['output'], Field1='x', Field2='y')
        self.assertAlmostEqual(xy['Spearman'], statistics.correlation(_ranks(x), _ranks(y)))
        self.assertTrue(0 < xy['PValue'] < 0.01)
        self.assertEqual(first(out['output'], Field1='x', Field2='c')['Spearman'], None)  # constant field
        self.assertEqual(first(out['output'], Field1='x', Field2='z')['N'], 7)  # pairwise null removal
        listwise, _ = run_tool('core.spearman_correlation', {'fields': ['x', 'z'], 'missing': 'listwise'},
                               {'input': (fields, rows)})
        self.assertEqual(set(listwise['output'].column('N')), {7})

    def test_contingency_chi_square(self):
        # Classic 2x2: [[10, 20], [30, 40]]; chi-square computed by hand.
        rows = [['A', 'yes']] * 10 + [['A', 'no']] * 20 + [['B', 'yes']] * 30 + [['B', 'no']] * 40 + [[None, 'yes']]
        out, _ = run_tool('core.contingency_table', {'row_field': 'g', 'column_field': 'r'},
                          {'input': ([('g', 'v_wstring'), ('r', 'v_wstring')], rows)})
        obs = [[20, 10], [40, 30]]  # columns sorted: no, yes
        n = 100
        rt, ct = [sum(r) for r in obs], [sum(c) for c in zip(*obs)]
        chi2 = sum((obs[i][j] - rt[i] * ct[j] / n) ** 2 / (rt[i] * ct[j] / n) for i in range(2) for j in range(2))
        stats = out['statistics'].to_dicts()[0]
        self.assertEqual((stats['N'], stats['DegreesOfFreedom']), (100, 1))
        self.assertAlmostEqual(stats['ChiSquare'], chi2)
        self.assertAlmostEqual(stats['CramersV'], math.sqrt(chi2 / n))
        cell = first(out['table'], Row='A', Column='yes')
        self.assertEqual(cell['Observed'], 10)
        self.assertAlmostEqual(cell['Expected'], 30 * 40 / 100)
        self.assertAlmostEqual(cell['RowPercent'], 100 * 10 / 30)
        self.assertEqual(first(out['table'], Row='(Total)', Column='(Total)')['Observed'], 100)
        with_nulls, _ = run_tool('core.contingency_table', {'row_field': 'g', 'column_field': 'r', 'include_nulls': True},
                                 {'input': ([('g', 'v_wstring'), ('r', 'v_wstring')], rows)})
        self.assertEqual(first(with_nulls['table'], Row='[Null]', Column='yes')['Observed'], 1)


class DistributionTests(unittest.TestCase):
    def test_fits_ranked_by_aic_and_positive_only_rules(self):
        import numpy as np
        rng = np.random.default_rng(7)
        sample = rng.lognormal(mean=1.0, sigma=0.4, size=400)
        out, _ = run_tool('core.distribution_analysis', {'field': 'x'},
                          {'input': ([('x', 'double')], [[float(v)] for v in sample])})
        fits = out['fits'].to_dicts()
        self.assertEqual(fits[0]['Distribution'], 'Lognormal')
        self.assertAlmostEqual(fits[0]['Param1'], 0.4, delta=0.05)  # shape = sigma
        self.assertAlmostEqual(fits[0]['Param2'], math.e, delta=0.15)  # scale = exp(mu)
        self.assertEqual([f['Rank'] for f in fits if f['Rank']], list(range(1, 7)))
        normal = first(out['fits'], Distribution='Normal')
        self.assertAlmostEqual(normal['Param1'], float(sample.mean()))
        self.assertIsNotNone(normal['AndersonDarling'])
        mixed, _ = run_tool('core.distribution_analysis', {'field': 'x', 'distributions': ['normal', 'gamma']},
                            {'input': ([('x', 'double')], [[-1.0], [0.5], [2.0], [3.0]])})
        self.assertIn('not applicable', first(mixed['fits'], Distribution='Gamma')['Status'])
        moments = mixed['moments'].to_dicts()[0]
        self.assertEqual((moments['N'], moments['Min']), (4, -1.0))


if __name__ == '__main__':
    unittest.main()


class ExampleWorkflowTests(unittest.TestCase):
    def test_fuel_investigation_example_runs_every_tool(self):
        from flowengine.executor import run_file
        from helpers import ROOT
        result = run_file(ROOT / 'examples/investigation/fuel_investigation.flow.json')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output('profile', 'overview').column('Records'), [459])
        self.assertEqual(sum(result.output('petrol_histogram').column('Count')), 459)
        petrol_diesel = first(result.output('rank_correlation'), Field1='PetrolPencePerLitre', Field2='DieselPencePerLitre')
        self.assertGreater(petrol_diesel['Spearman'], 0.95)
        self.assertEqual(result.output('year_by_band', 'statistics').column('N'), [459])
        self.assertEqual(set(result.browse), {'view_profile', 'view_summary', 'view_frequencies',
                                              'view_petrol_histogram', 'view_rank_correlation',
                                              'view_year_by_band', 'view_diesel_distribution'})
