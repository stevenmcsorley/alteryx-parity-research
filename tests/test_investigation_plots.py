"""Advanced Data Investigation: statistics checked independently; charts are real, well-formed SVG."""
import math
import statistics
import unittest
import xml.etree.ElementTree as ET

from helpers import run_tool

SVG = '{http://www.w3.org/2000/svg}'


def svg_of(table):
    svg = table.column('Chart')[0]
    root = ET.fromstring(svg)  # well-formed, all text escaped
    return svg, root


def first(table, **where):
    return next(r for r in table.to_dicts() if all(r[k] == v for k, v in where.items()))


class AssociationAndHeatTests(unittest.TestCase):
    def test_association_measures_by_type_pair(self):
        rows = [[float(i), float(2 * i + (i % 3)), 'a' if i < 10 else 'b', 'x' if i < 10 else 'y'] for i in range(20)]
        fields = [('n1', 'double'), ('n2', 'double'), ('c1', 'v_wstring'), ('c2', 'v_wstring')]
        out, _ = run_tool('core.association_analysis', {'fields': ['n1', 'n2', 'c1', 'c2']}, {'input': (fields, rows)})
        t = out['output']
        r = first(t, Field1='n1', Field2='n2')
        self.assertEqual(r['Measure'], 'pearson')
        self.assertAlmostEqual(r['Value'], statistics.correlation([x[0] for x in rows], [x[1] for x in rows]))
        self.assertAlmostEqual(first(t, Field1='c1', Field2='c2')['Value'], 1.0)  # perfectly associated categories
        eta = first(t, Field1='n1', Field2='c1')
        values = [x[0] for x in rows]
        mean = statistics.fmean(values)
        between = 10 * (statistics.fmean(values[:10]) - mean) ** 2 + 10 * (statistics.fmean(values[10:]) - mean) ** 2
        self.assertEqual(eta['Measure'], 'correlation_ratio')
        self.assertAlmostEqual(eta['Value'], math.sqrt(between / sum((v - mean) ** 2 for v in values)))
        svg, root = svg_of(out['chart'])
        self.assertGreater(len(root.findall(f'{SVG}rect')), 16)
        targeted, _ = run_tool('core.association_analysis', {'fields': ['n1', 'n2', 'c1'], 'target_field': 'c2'},
                               {'input': (fields, rows)})
        self.assertEqual({r['Field2'] for r in targeted['output'].to_dicts()}, {'c2'})

    def test_heat_plot_bins_and_aggregation(self):
        rows = [[0.0, 'low', 1.0], [0.1, 'low', 3.0], [0.9, 'high', 5.0], [1.0, 'high', 7.0], [0.95, 'low', None]]
        fields = [('x', 'double'), ('band', 'v_wstring'), ('v', 'double')]
        out, _ = run_tool('core.heat_plot', {'x_field': 'x', 'y_field': 'band', 'x_bins': 2, 'value_field': 'v',
                                             'aggregation': 'mean'}, {'input': (fields, rows)})
        grid = out['output']
        low_left = first(grid, Y='low', XLower=0.0)
        self.assertEqual((low_left['Count'], low_left['Value']), (2, 2.0))
        high_right = first(grid, Y='high', XLower=0.5)
        self.assertEqual((high_right['Count'], high_right['Value']), (2, 6.0))
        self.assertEqual(first(grid, Y='low', XLower=0.5)['Count'], 1)  # counted, but its value is missing
        self.assertIsNone(first(grid, Y='high', XLower=0.0)['Value'])  # empty cell
        svg, _ = svg_of(out['chart'])
        self.assertIn('mean of v', svg)


class ImportanceMeansTests(unittest.TestCase):
    def test_importance_ranks_informative_field_first(self):
        rows = [[i % 4, (i * 7) % 5, 'yes' if i % 4 < 2 else 'no'] for i in range(200)]
        fields = [('signal', 'int32'), ('noise', 'int32'), ('target', 'v_wstring')]
        for method in ('mutual_information', 'gain_ratio', 'symmetrical_uncertainty'):
            with self.subTest(method=method):
                out, _ = run_tool('core.importance_weights', {'target_field': 'target', 'fields': ['noise', 'signal'],
                                                              'method': method}, {'input': (fields, rows)})
                t = out['output'].to_dicts()
                self.assertEqual(t[0]['Field'], 'signal')
                if method == 'mutual_information':
                    self.assertAlmostEqual(t[0]['Weight'], 1.0)  # signal fully determines a balanced binary target
                self.assertLess(t[1]['Weight'], 0.05)
                self.assertAlmostEqual(sum(r['NormalizedWeight'] for r in t), 1.0)

    def test_plot_of_means_confidence_intervals_and_anova(self):
        from scipy import stats
        data = {'A': [4.0, 5.0, 6.0, 5.0], 'B': [8.0, 9.0, 10.0, 9.0, 11.0]}
        rows = [[g, v] for g, vs in data.items() for v in vs] + [[None, 1.0], ['A', None]]
        out, _ = run_tool('core.plot_of_means', {'value_field': 'v', 'group_field': 'g', 'confidence': 0.9},
                          {'input': ([('g', 'v_wstring'), ('v', 'double')], rows)})
        a = first(out['output'], Group='A')
        se = statistics.stdev(data['A']) / math.sqrt(4)
        self.assertEqual(a['N'], 4)
        self.assertAlmostEqual(a['CIUpper'] - a['Mean'], stats.t.ppf(0.95, 3) * se)
        anova = out['anova'].to_dicts()[0]
        grand = statistics.fmean(data['A'] + data['B'])
        ssb = sum(len(v) * (statistics.fmean(v) - grand) ** 2 for v in data.values())
        ssw = sum((x - statistics.fmean(v)) ** 2 for v in data.values() for x in v)
        self.assertAlmostEqual(anova['F'], (ssb / 1) / (ssw / 7))
        svg_of(out['chart'])


class ScatterViolinTests(unittest.TestCase):
    def test_scatter_trend_per_group_and_point_cap(self):
        rows = [[float(x), 3.0 * x + 2.0, 'g1'] for x in range(30)] + [[float(x), -x + 1.0, 'g2'] for x in range(30)]
        out, _ = run_tool('core.scatterplot', {'x_field': 'x', 'y_field': 'y', 'group_field': 'g', 'max_points': 20},
                          {'input': ([('x', 'double'), ('y', 'double'), ('g', 'v_wstring')], rows)})
        g1 = first(out['trend'], Group='g1')
        self.assertAlmostEqual(g1['Slope'], 3.0)
        self.assertAlmostEqual(g1['Intercept'], 2.0)
        self.assertAlmostEqual(g1['RSquared'], 1.0)
        self.assertEqual(g1['N'], 30)  # trend uses every record even when the chart is thinned
        _, root = svg_of(out['chart'])
        self.assertLessEqual(len(root.findall(f'{SVG}circle')), 20)

    def test_violin_density_integrates_to_one(self):
        import numpy as np
        rng = np.random.default_rng(3)
        values = list(rng.normal(10, 2, 300)) + list(rng.normal(20, 1, 300))
        rows = [[float(v), 'a' if i < 300 else 'b'] for i, v in enumerate(values)] + [[5.0, 'single']]
        out, _ = run_tool('core.violin_plot', {'value_field': 'v', 'group_field': 'g', 'grid_points': 400},
                          {'input': ([('v', 'double'), ('g', 'v_wstring')], rows)})
        a = first(out['summary'], Group='a')
        self.assertAlmostEqual(a['Median'], float(np.median(values[:300])))
        dens = [r for r in out['density'].to_dicts() if r['Group'] == 'a']
        xs, ys = [r['Value'] for r in dens], [r['Density'] for r in dens]
        self.assertAlmostEqual(float(np.trapezoid(ys, xs)), 1.0, places=2)
        self.assertIsNone(first(out['summary'], Group='single')['Bandwidth'])
        _, root = svg_of(out['chart'])
        self.assertEqual(len(root.findall(f'{SVG}polygon')), 2)

    def test_chart_text_is_escaped(self):
        rows = [[1.0, '<script>alert(1)</script>'], [2.0, 'b&c']]
        out, _ = run_tool('core.plot_of_means', {'value_field': 'v', 'group_field': 'g'},
                          {'input': ([('v', 'double'), ('g', 'v_wstring')], rows)})
        svg, _ = svg_of(out['chart'])
        self.assertNotIn('<script>', svg)
        self.assertIn('&lt;script&gt;', svg)


if __name__ == '__main__':
    unittest.main()


class ChartExampleTests(unittest.TestCase):
    def test_fuel_charts_example_renders_every_chart(self):
        from flowengine.executor import run_file
        from helpers import ROOT
        result = run_file(ROOT / 'examples/investigation/fuel_charts.flow.json')
        self.assertTrue(result.ok, result.error)
        charts_seen = [n for n, t in result.browse.items() if t.schema.names == ['Chart', 'Title']]
        self.assertEqual(len(charts_seen), 7)
        for name in charts_seen:
            ET.fromstring(result.browse[name].column('Chart')[0])
