"""Batch 5: geometry editing, trade areas, overlay and density; report furniture, overlays and maps; JSON Build."""
import json
import math
import re
import tempfile
import unittest
from pathlib import Path

import shapely
from pyproj import Geod

from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_workflow

from helpers import edge, run_tool, text_input, workflow
from test_reporting import pdf_text

GEOD = Geod(ellps='WGS84')
G = [('g', 'v_wstring')]


def geo_area_km2(wkt):
    return abs(GEOD.geometry_area_perimeter(shapely.from_wkt(wkt))[0]) / 1e6


class GeometryEditingTests(unittest.TestCase):
    def test_generalize_uses_metric_tolerance(self):
        zigzag = 'LINESTRING (' + ', '.join(f'{x * 100} {(x % 2) * 100}' for x in range(21)) + ')'  # 100 m teeth
        keep, _ = run_tool('core.generalize', {'geometry_field': 'g', 'crs': 'EPSG:27700', 'tolerance': 20, 'units': 'm'},
                           {'input': (G, [[zigzag]])})
        flat, _ = run_tool('core.generalize', {'geometry_field': 'g', 'crs': 'EPSG:27700', 'tolerance': 200, 'units': 'm'},
                           {'input': (G, [[zigzag]])})
        self.assertEqual(len(shapely.from_wkt(keep['output'].column('Generalized')[0]).coords), 21)
        self.assertEqual(len(shapely.from_wkt(flat['output'].column('Generalized')[0]).coords), 2)
        # A 0.1 degree square (~11 km) must survive a 1 km tolerance; degrees are not metres.
        square = 'POLYGON ((0 50, 0.1 50, 0.1 50.1, 0 50.1, 0 50))'
        out, _ = run_tool('core.generalize', {'geometry_field': 'g', 'tolerance': 1}, {'input': (G, [[square]])})
        self.assertAlmostEqual(geo_area_km2(out['output'].column('Generalized')[0]), geo_area_km2(square), delta=0.5)

    def test_smooth_rounds_corners_and_keeps_line_ends(self):
        out, _ = run_tool('core.smooth', {'geometry_field': 'g', 'crs': 'EPSG:27700', 'iterations': 3, 'output_field': 'g'},
                          {'input': (G, [['POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))'], ['LINESTRING (0 0, 5 5, 10 0)']])})
        poly, line = (shapely.from_wkt(v) for v in out['output'].column('g'))
        self.assertTrue(poly.is_valid)
        self.assertEqual(len(poly.exterior.coords), 33)  # 4 corners x 2^3, plus the closing vertex
        self.assertAlmostEqual(poly.area, 83.59375)  # independent Chaikin computation for a 10 x 10 square
        self.assertEqual((line.coords[0], line.coords[-1]), ((0.0, 0.0), (10.0, 0.0)))

    def test_poly_split_and_build(self):
        multi = 'MULTIPOLYGON (((0 0, 4 0, 4 4, 0 4, 0 0), (1 1, 2 1, 2 2, 1 2, 1 1)), ((10 10, 11 10, 11 11, 10 10)))'
        parts, _ = run_tool('core.poly_split', {'geometry_field': 'g'}, {'input': (G, [[multi]])})
        self.assertEqual(parts['output'].column('PartNumber'), [1, 2])
        lines, _ = run_tool('core.poly_split', {'geometry_field': 'g', 'split_to': 'polylines'}, {'input': (G, [[multi]])})
        self.assertEqual(len(lines['output']), 3)  # outer ring, hole, second polygon
        points, _ = run_tool('core.poly_split', {'geometry_field': 'g', 'split_to': 'points'}, {'input': (G, [[multi]])})
        self.assertEqual(len(points['output']), 4 + 4 + 3)  # closing vertices omitted
        rows = [['a', 1, 'POINT (0 0)'], ['a', 2, 'POINT (4 0)'], ['a', 3, 'POINT (4 3)'], ['a', 4, 'POINT (0 3)'],
                ['b', 1, 'POINT (0 0)'], ['b', 2, 'POINT (1 1)'], ['h', 1, 'POINT (0 0)'], ['h', 2, 'POINT (2 0)'],
                ['h', 3, 'POINT (1 1)'], ['h', 4, 'POINT (1 0.2)'], ['h', 5, 'POINT (0 2)']]
        fields = [('grp', 'v_wstring'), ('seq', 'int32'), ('g', 'v_wstring')]
        built, log = run_tool('core.poly_build', {'geometry_field': 'g', 'group_by': ['grp'], 'sequence_field': 'seq'},
                              {'input': (fields, rows)})
        areas = {r['grp']: shapely.from_wkt(r['Built']).area for r in built['output'].to_dicts()}
        self.assertEqual(areas['a'], 12.0)
        self.assertNotIn('b', areas)  # two points cannot make a polygon
        hull, _ = run_tool('core.poly_build', {'geometry_field': 'g', 'group_by': ['grp'], 'method': 'convex_hull'},
                           {'input': (fields, [r for r in rows if r[0] == 'h'])})
        self.assertEqual(shapely.from_wkt(hull['output'].column('Built')[0]).area, 2.0)  # (1 1) is on the hull edge, (1 0.2) inside

    def test_trade_area_rings_are_geodesic(self):
        out, _ = run_tool('core.trade_area', {'geometry_field': 'g', 'radii': [1, 2], 'units': 'km', 'donuts': True,
                                              'resolution': 64}, {'input': (G, [['POINT (-2 53)']])})
        disc, ring = out['output'].to_dicts()
        self.assertAlmostEqual(geo_area_km2(disc['TradeArea']) / math.pi, 1.0, delta=0.01)
        self.assertAlmostEqual(geo_area_km2(ring['TradeArea']) / (math.pi * 3), 1.0, delta=0.01)
        self.assertEqual((ring['Radius'], ring['InnerRadius']), (2, 1))

    def test_spatial_process_overlay_and_dissolve(self):
        a, b = 'POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))', 'POLYGON ((1 0, 3 0, 3 2, 1 2, 1 0))'
        fields = [('a', 'v_wstring'), ('b', 'v_wstring')]
        for action, area in [('intersection', 2.0), ('union', 6.0), ('difference', 2.0), ('symmetric_difference', 4.0)]:
            with self.subTest(action=action):
                out, _ = run_tool('core.spatial_process', {'action': action, 'fields': ['a', 'b']}, {'input': (fields, [[a, b]])})
                self.assertAlmostEqual(shapely.from_wkt(out['output'].column('Result')[0]).area, area)
        rows = [['x', a], ['x', b], ['y', 'POLYGON ((5 5, 6 5, 6 6, 5 5))']]
        out, _ = run_tool('core.spatial_process', {'action': 'dissolve', 'geometry_field': 'g', 'group_by': ['k']},
                          {'input': ([('k', 'v_wstring'), ('g', 'v_wstring')], rows)})
        got = {r['k']: (shapely.from_wkt(r['Result']).area, r['RecordCount']) for r in out['output'].to_dicts()}
        self.assertEqual(got, {'x': (6.0, 2), 'y': (0.5, 1)})

    def test_heat_map_density_integrates_to_point_count(self):
        pts = [[f'POINT ({1000 + dx * 10} {1000 + dy * 10})'] for dx in range(10) for dy in range(10)] + [['POINT (8000 8000)']]
        out, _ = run_tool('core.heat_map', {'geometry_field': 'g', 'crs': 'EPSG:27700', 'cell_size': 100, 'bandwidth': 300,
                                            'units': 'm', 'min_fraction': 0}, {'input': (G, pts)})
        t = out['output'].to_dicts()
        cell_km2 = 0.1 * 0.1
        self.assertAlmostEqual(sum(r['Density'] for r in t) * cell_km2, len(pts), delta=len(pts) * 0.02)
        peak = max(t, key=lambda r: r['Density'])
        c = shapely.from_wkt(peak['Cell']).centroid
        self.assertLess(math.hypot(c.x - 1045, c.y - 1045), 150)  # densest cell sits on the cluster
        self.assertEqual(peak['Class'], 5)


class JsonBuildTests(unittest.TestCase):
    def test_json_build_round_trips_json_parse(self):
        doc = {'id': '007', 'tags': ['a', 'b'], 'score': 9.5, 'ok': True, 'none': None, 'addr': {'geo': [53.8, -1.5]}}
        parsed, _ = run_tool('core.json_parse', {'field': 'j', 'single_field': False},
                             {'input': ([('j', 'v_wstring')], [[json.dumps(doc)]])})
        t = parsed['output']
        out, _ = run_tool('core.json_build', {}, {'input': ([(f.name, f.type.value) for f in t.schema], [list(r) for r in t.rows()])})
        self.assertEqual(json.loads(out['output'].column('JSON')[0]), doc)
        rows = [['u1', 'name', 'Ann'], ['u1', 'age', '41'], ['u2', 'name', 'Bo'], ['u2', 'zip', '007']]
        grouped, _ = run_tool('core.json_build', {'group_by': ['user']},
                              {'input': ([('user', 'v_wstring'), ('JSON_Name', 'v_wstring'), ('JSON_ValueString', 'v_wstring')], rows)})
        self.assertEqual([json.loads(v) for v in grouped['output'].column('JSON')], [{'name': 'Ann', 'age': 41}, {'name': 'Bo', 'zip': '007'}])
        bad = [['a', '1'], ['a.b', '2']]
        with self.assertRaisesRegex(AssertionError, 'value and a container'):
            run_tool('core.json_build', {}, {'input': ([('JSON_Name', 'v_wstring'), ('JSON_ValueString', 'v_wstring')], bad)})


class ReportFurnitureTests(unittest.TestCase):
    def render(self, d, path, nodes, edges):
        nodes = nodes + [{'id': 'out', 'tool': 'core.render', 'config': {'path': path, 'font': 'helvetica'}}]
        result = run_workflow(workflow(nodes, edges), base_dir=Path(d))
        self.assertTrue(result.ok, result.error)
        return result

    def test_header_and_footer_on_every_pdf_page(self):
        rows = [[f'Item {i}', float(i)] for i in range(150)]
        nodes = [text_input('t', [('name', 'v_wstring'), ('v', 'double')], rows),
                 {'id': 'h', 'tool': 'core.report_header', 'config': {'title': 'Inventory', 'subtitle': 'Weekly'}},
                 {'id': 'f', 'tool': 'core.report_footer', 'config': {'left': 'Confidential', 'right': 'Page {page} of {pages}'}},
                 {'id': 'tbl', 'tool': 'core.report_table', 'config': {}},
                 {'id': 'page', 'tool': 'core.report_layout', 'config': {}}]
        edges = [edge('t', 'output', 'tbl', 'input'), edge('h', 'output', 'page', 'inputs'), edge('tbl', 'output', 'page', 'inputs'),
                 edge('f', 'output', 'page', 'inputs'), edge('page', 'output', 'out', 'input')]
        with tempfile.TemporaryDirectory() as d:
            pages = self.render(d, 'r.pdf', nodes, edges).output('out').column('Pages')[0]
            self.assertGreater(pages, 1)
            text = pdf_text(Path(d, 'r.pdf').read_bytes())
            self.assertEqual(text.count('(Inventory) Tj'), pages)
            for n in range(1, pages + 1):
                self.assertIn(f'(Page {n} of {pages}) Tj', text)
            nodes[-1]['config'] = {}
            self.render(d, 'r.html', nodes, edges)
            html = Path(d, 'r.html').read_text(encoding='utf-8')
            self.assertIn('<header class="fb-page">', html)
            self.assertIn('Confidential', html)
            self.assertNotIn('{page}', html)

    def test_overlay_places_elements_over_a_chart(self):
        nodes = [text_input('t', [('x', 'v_wstring'), ('y', 'double')], [['a', 1.0], ['b', 2.0]]),
                 {'id': 'c', 'tool': 'core.interactive_chart', 'config': {'chart_type': 'bar', 'x_field': 'x', 'y_fields': ['y']}},
                 {'id': 'note', 'tool': 'core.report_text', 'config': {'text': 'Draft', 'style': 'heading'}},
                 {'id': 'both', 'tool': 'core.append_fields', 'config': {}},
                 {'id': 'ov', 'tool': 'core.report_overlay', 'config': {'base_field': 'Report', 'positions': {'top_right': 'Right_Report'}}}]
        edges = [edge('t', 'output', 'c', 'input'), edge('c', 'output', 'both', 'target'), edge('note', 'output', 'both', 'source'),
                 edge('both', 'output', 'ov', 'input'), edge('ov', 'output', 'out', 'input')]
        with tempfile.TemporaryDirectory() as d:
            plan = run_workflow(workflow(nodes[:4], edges[:3]), base_dir=Path(d))
            names = plan.output('both').schema.names
            nodes[4]['config']['positions'] = {'top_right': [n for n in names if n != 'Report' and n.endswith('Report')][0]}
            self.render(d, 'o.html', nodes, edges)
            html = Path(d, 'o.html').read_text(encoding='utf-8')
            self.assertIn('fb-ov-top_right', html)
            self.assertIn('Draft', html)
            self.render(d, 'o.pdf', nodes, edges)
            self.assertIn('(Draft) Tj', pdf_text(Path(d, 'o.pdf').read_bytes()))

    def test_report_map_layers_theme_and_legend_round_trip(self):
        areas = [['North', 3.0, 'POLYGON ((0 52, 2 52, 2 54, 0 54, 0 52), (0.5 52.5, 1 52.5, 1 53, 0.5 53, 0.5 52.5))'],
                 ['South', 9.0, 'POLYGON ((0 50, 2 50, 2 52, 0 52, 0 50))']]
        sites = [['Depot', 'POINT (1 51)']]
        cfg = {'title': 'Regions', 'layers': [{'geometry_field': 'g', 'name': 'Regions', 'theme_field': 'v', 'label_field': 'name'},
                                              {'geometry_field': 'g', 'name': 'Sites', 'color': '#D55E00'}]}
        nodes = [text_input('a', [('name', 'v_wstring'), ('v', 'double'), ('g', 'v_wstring')], areas),
                 text_input('s', [('name', 'v_wstring'), ('g', 'v_wstring')], sites),
                 {'id': 'm', 'tool': 'core.report_map', 'config': cfg},
                 {'id': 'split', 'tool': 'core.map_legend_splitter', 'config': {}},
                 {'id': 'build', 'tool': 'core.map_legend_builder', 'config': {'label_field': 'Label', 'color_field': 'Color',
                                                                              'shape_field': 'Shape', 'title': 'Key'}}]
        edges = [edge('a', 'output', 'm', 'layers'), edge('s', 'output', 'm', 'layers'), edge('m', 'output', 'split', 'input'),
                 edge('split', 'output', 'build', 'input')]
        result = run_workflow(workflow(nodes, edges), base_dir=Path('.'))
        self.assertTrue(result.ok, result.error)
        svg = result.output('m').column('Chart')[0]
        self.assertEqual(svg.count('fill-rule="evenodd"'), 2)  # polygons with holes drawn as even-odd paths
        self.assertEqual(svg.count('<circle'), 2)  # the site point and its legend swatch (fill swatches are rects)
        self.assertIn('North', svg)
        legend = result.output('split').to_dicts()
        self.assertEqual([r['Shape'] for r in legend], ['fill'] * 5 + ['point'])
        self.assertEqual(legend[-1]['Label'], 'Sites')
        rebuilt = json.loads(result.output('build').column('Report')[0])
        self.assertEqual((rebuilt['kind'], len(rebuilt['entries']), rebuilt['title']), ('legend', 6, 'Key'))
        with self.assertRaisesRegex(WorkflowValidationError, 'connection'):
            run_workflow(workflow(nodes[:3], edges[:1]), base_dir=Path('.'))


if __name__ == '__main__':
    unittest.main()


class MapReportExampleTests(unittest.TestCase):
    def test_uk_map_report_example(self):
        from flowengine.executor import run_file
        from helpers import ROOT
        result = run_file(ROOT / 'examples/spatial/uk_map_report.flow.json')
        self.assertTrue(result.ok, result.error)
        info = result.output('pdf').to_dicts()[0]
        self.assertEqual((info['Format'], info['Pages']), ('pdf', 1))
        text = pdf_text(Path(info['Path']).read_bytes())
        self.assertIn('Page 1 of 1', text)
