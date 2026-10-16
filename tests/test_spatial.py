"""Spatial tools: coordinates, geodesic distance, buffers, nearest search and predicates checked independently."""
import math
import random
import unittest

from flowengine.errors import WorkflowValidationError

from helpers import run_tool

LONDON, PARIS, EDINBURGH, CARDIFF = (-0.1276, 51.5072), (2.3522, 48.8566), (-3.1883, 55.9533), (-3.1791, 51.4816)


def haversine_km(a, b, r=6371.0088):
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    dla, dlo = la2 - la1, math.radians(b[0] - a[0])
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def wkt(p):
    return f'POINT ({p[0]} {p[1]})'


class PointsDistanceTests(unittest.TestCase):
    def test_create_points_validates_and_reprojects(self):
        out, _ = run_tool('core.create_points', {'x_field': 'lon', 'y_field': 'lat'},
                          {'input': ([('lon', 'double'), ('lat', 'double')], [[*LONDON], [None, 1.0]])})
        self.assertEqual(out['output'].column('Point'), ['POINT (-0.1276 51.5072)', None])
        bng, _ = run_tool('core.create_points', {'x_field': 'lon', 'y_field': 'lat', 'output_crs': 'EPSG:27700'},
                          {'input': ([('lon', 'double'), ('lat', 'double')], [[*LONDON]])})
        x, y = map(float, bng['output'].column('Point')[0][7:-1].split())
        self.assertAlmostEqual(x, 530_034, delta=50)  # British National Grid easting/northing of central London
        self.assertAlmostEqual(y, 180_381, delta=50)
        with self.assertRaisesRegex(AssertionError, 'outside longitude'):
            run_tool('core.create_points', {'x_field': 'lat', 'y_field': 'lon'},
                     {'input': ([('lon', 'double'), ('lat', 'double')], [[100.0, 120.0]])})
        with self.assertRaises(WorkflowValidationError):
            run_tool('core.create_points', {'x_field': 'lon', 'y_field': 'lat', 'crs': 'EPSG:999999'},
                     {'input': ([('lon', 'double'), ('lat', 'double')], [])})

    def test_geodesic_and_planar_distance(self):
        rows = [[wkt(LONDON), wkt(PARIS)], [wkt(LONDON), wkt(LONDON)],
                ['POLYGON ((-1 51, 1 51, 1 52, -1 52, -1 51))', wkt(LONDON)]]
        out, _ = run_tool('core.distance', {'source_field': 'a', 'target_field': 'b'},
                          {'input': ([('a', 'v_wstring'), ('b', 'v_wstring')], rows)})
        d = out['output'].column('Distance')
        self.assertAlmostEqual(d[0], haversine_km(LONDON, PARIS), delta=0.005 * d[0])  # ellipsoid vs sphere
        self.assertAlmostEqual(d[0], 343.9, delta=0.6)
        self.assertEqual(d[1:], [0.0, 0.0])  # identical points; point inside polygon
        self.assertAlmostEqual(out['output'].column('Bearing')[0], 148.1, delta=0.5)  # London -> Paris is SSE
        miles, _ = run_tool('core.distance', {'source_field': 'a', 'target_field': 'b', 'units': 'mi'},
                            {'input': ([('a', 'v_wstring'), ('b', 'v_wstring')], rows[:1])})
        self.assertAlmostEqual(miles['output'].column('Distance')[0], d[0] / 1.609344)
        planar, _ = run_tool('core.distance', {'source_field': 'a', 'target_field': 'b', 'crs': 'EPSG:27700',
                                               'method': 'planar', 'units': 'm'},
                             {'input': ([('a', 'v_wstring'), ('b', 'v_wstring')], [['POINT (0 0)', 'POINT (300 400)']])})
        self.assertEqual(planar['output'].column('Distance'), [500.0])
        with self.assertRaisesRegex(WorkflowValidationError, 'measure degrees'):
            run_tool('core.distance', {'source_field': 'a', 'target_field': 'b', 'method': 'planar'},
                     {'input': ([('a', 'v_wstring'), ('b', 'v_wstring')], [])})

    def test_buffers_are_metric_not_degrees(self):
        from pyproj import Geod
        import shapely
        out, _ = run_tool('core.buffer', {'geometry_field': 'g', 'distance': 10, 'units': 'km', 'resolution': 32},
                          {'input': ([('g', 'v_wstring')], [['POINT (10 60)']])})
        poly = shapely.from_wkt(out['output'].column('Buffer')[0])
        geod = Geod(ellps='WGS84')
        area, _ = geod.geometry_area_perimeter(poly)
        self.assertAlmostEqual(abs(area) / (math.pi * 10_000 ** 2), 1.0, delta=0.01)
        for x, y in list(poly.exterior.coords)[::16]:
            self.assertAlmostEqual(geod.inv(10, 60, x, y)[2], 10_000, delta=60)  # every vertex is 10 km away
        self.assertGreater(poly.bounds[2] - poly.bounds[0], 1.9 * (poly.bounds[3] - poly.bounds[1]))  # wider in degrees at 60N
        projected, _ = run_tool('core.buffer', {'geometry_field': 'g', 'distance': 100, 'units': 'm', 'crs': 'EPSG:27700'},
                                {'input': ([('g', 'v_wstring')], [['POINT (1000 1000)']])})
        n = 4 * 16  # default resolution: a regular 64-gon inscribed in the 100 m circle
        self.assertAlmostEqual(shapely.from_wkt(projected['output'].column('Buffer')[0]).area, 0.5 * n * 100 ** 2 * math.sin(2 * math.pi / n), delta=1e-6)


class NearestMatchTests(unittest.TestCase):
    CITIES = [['London', wkt(LONDON)], ['Paris', wkt(PARIS)], ['Edinburgh', wkt(EDINBURGH)], ['Cardiff', wkt(CARDIFF)]]

    def test_find_nearest_order_limits_and_self_matches(self):
        fields = [('name', 'v_wstring'), ('g', 'v_wstring')]
        cfg = {'target_geometry_field': 'g', 'universe_geometry_field': 'g', 'max_matches': 2, 'ignore_identical': True}
        out, _ = run_tool('core.find_nearest', cfg, {'targets': (fields, self.CITIES[:1]), 'universe': (fields, self.CITIES)})
        m = out['matched'].to_dicts()
        self.assertEqual([(r['U_name'], r['Rank']) for r in m], [('Cardiff', 1), ('Paris', 2)])
        self.assertAlmostEqual(m[0]['Distance'], haversine_km(LONDON, CARDIFF), delta=1.5)
        near, _ = run_tool('core.find_nearest', {**cfg, 'max_distance': 100},
                           {'targets': (fields, self.CITIES[:1]), 'universe': (fields, self.CITIES)})
        self.assertEqual(len(near['matched']), 0)
        self.assertEqual(near['unmatched'].column('name'), ['London'])

    def test_indexed_search_matches_brute_force(self):
        from pyproj import Geod
        rng = random.Random(11)
        universe = [[f'u{i}', f'POINT ({rng.uniform(-10, 10)} {rng.uniform(40, 60)})'] for i in range(600)]
        targets = [[f't{i}', f'POINT ({rng.uniform(-10, 10)} {rng.uniform(40, 60)})'] for i in range(25)]
        fields = [('id', 'v_wstring'), ('g', 'v_wstring')]
        out, _ = run_tool('core.find_nearest', {'target_geometry_field': 'g', 'universe_geometry_field': 'g', 'max_matches': 3},
                          {'targets': (fields, targets), 'universe': (fields, universe)})
        geod = Geod(ellps='WGS84')
        coords = lambda s: tuple(map(float, s[7:-1].split()))
        got = {}
        for r in out['matched'].to_dicts():
            got.setdefault(r['id'], []).append(r['U_id'])
        for tid, tg in targets:
            tx, ty = coords(tg)
            brute = sorted((geod.inv(tx, ty, *coords(ug))[2], i) for i, (_, ug) in enumerate(universe))[:3]
            self.assertEqual(got[tid], [universe[i][0] for _, i in brute])

    def test_spatial_match_predicates(self):
        polys = [['A', 'POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))'], ['B', 'POLYGON ((2 0, 4 0, 4 2, 2 2, 2 0))']]
        points = [['p1', 'POINT (1 1)'], ['p2', 'POINT (2 1)'], ['p3', 'POINT (9 9)']]
        fields = [('id', 'v_wstring'), ('g', 'v_wstring')]
        base = {'target_geometry_field': 'g', 'universe_geometry_field': 'g'}
        out, _ = run_tool('core.spatial_match', {**base, 'predicate': 'within'}, {'targets': (fields, points), 'universe': (fields, polys)})
        self.assertEqual([(r['id'], r['U_id']) for r in out['matched'].to_dicts()], [('p1', 'A')])  # p2 is on the boundary
        self.assertEqual(out['unmatched'].column('id'), ['p2', 'p3'])
        out, _ = run_tool('core.spatial_match', {**base, 'predicate': 'intersects'}, {'targets': (fields, points), 'universe': (fields, polys)})
        self.assertEqual([(r['id'], r['U_id']) for r in out['matched'].to_dicts()], [('p1', 'A'), ('p2', 'A'), ('p2', 'B')])
        out, _ = run_tool('core.spatial_match', {**base, 'predicate': 'touches'}, {'targets': (fields, polys), 'universe': (fields, polys)})
        self.assertEqual([(r['id'], r['U_id']) for r in out['matched'].to_dicts()], [('A', 'B'), ('B', 'A')])
        out, _ = run_tool('core.spatial_match', {**base, 'predicate': 'contains'}, {'targets': (fields, polys), 'universe': (fields, points)})
        self.assertEqual([(r['id'], r['U_id']) for r in out['matched'].to_dicts()], [('A', 'p1')])


class InfoGridTests(unittest.TestCase):
    def test_spatial_info_geodesic_and_planar_measures(self):
        rows = [['POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))'], ['LINESTRING (0 0, 0 1)'], ['POLYGON ((0 0, 1 1, 1 0, 0 1, 0 0))'], [None]]
        out, _ = run_tool('core.spatial_info', {'geometry_field': 'g'}, {'input': ([('g', 'v_wstring')], rows)})
        t = out['output'].to_dicts()
        r = 6371.0072  # authalic radius (km): spherical area of a 1x1 degree cell at the equator
        sphere = r * r * math.radians(1) * math.sin(math.radians(1))
        self.assertAlmostEqual(t[0]['Area'], sphere, delta=0.01 * sphere)
        self.assertAlmostEqual(t[1]['Length'], 110.574, delta=0.01)  # one degree of latitude at the equator
        self.assertEqual((t[2]['IsValid'], 'Self-intersection' in t[2]['ValidityReason']), (False, True))
        self.assertEqual((t[0]['MinX'], t[0]['MaxY'], t[0]['CentroidX'], t[0]['NumPoints']), (0.0, 1.0, 0.5, 5))
        self.assertIsNone(t[3]['GeometryType'])
        planar, _ = run_tool('core.spatial_info', {'geometry_field': 'g', 'crs': 'EPSG:27700', 'area_units': 'ha', 'length_units': 'm'},
                             {'input': ([('g', 'v_wstring')], [['POLYGON ((0 0, 1000 0, 1000 1000, 0 1000, 0 0))']])})
        self.assertEqual((planar['output'].column('Area')[0], planar['output'].column('Length')[0]), (100.0, 4000.0))

    def test_make_grid_extent_metric_cells_and_clip(self):
        import shapely
        out, _ = run_tool('core.make_grid', {'cell_size': 250, 'crs': 'EPSG:27700', 'min_x': 0, 'min_y': 0, 'max_x': 1000, 'max_y': 500}, {})
        t = out['output'].to_dicts()
        self.assertEqual(len(t), 8)
        self.assertEqual((t[0]['Row'], t[0]['Column'], t[0]['CentreX'], t[0]['CentreY']), (0, 0, 125.0, 125.0))
        self.assertEqual(shapely.from_wkt(t[-1]['Cell']).bounds, (750.0, 250.0, 1000.0, 500.0))
        metric, _ = run_tool('core.make_grid', {'cell_size': 5, 'units': 'km', 'min_x': -0.5, 'min_y': 51.3, 'max_x': 0.3, 'max_y': 51.7}, {})
        from pyproj import Geod
        cell = shapely.from_wkt(metric['output'].column('Cell')[0])
        area, _ = Geod(ellps='WGS84').geometry_area_perimeter(cell)
        self.assertAlmostEqual(abs(area) / 25e6, 1.0, delta=0.01)  # 5 km x 5 km, built in UTM
        clipped, _ = run_tool('core.make_grid', {'cell_size': 1, 'geometry_field': 'g', 'clip': True},
                              {'input': ([('g', 'v_wstring')], [['POINT (0.5 0.5)'], ['POINT (3.5 2.5)']])})
        self.assertEqual(len(clipped['output']), 2)
        with self.assertRaisesRegex(AssertionError, 'max_cells'):
            run_tool('core.make_grid', {'cell_size': 1, 'crs': 'EPSG:27700', 'min_x': 0, 'min_y': 0, 'max_x': 1e5, 'max_y': 1e5,
                                        'max_cells': 1000}, {})

    def test_invalid_geometry_handling(self):
        rows = [['not a geometry'], ['POINT (1 1)']]
        with self.assertRaisesRegex(AssertionError, 'Invalid geometry in record 1'):
            run_tool('core.buffer', {'geometry_field': 'g'}, {'input': ([('g', 'v_wstring')], rows)})
        out, _ = run_tool('core.buffer', {'geometry_field': 'g', 'on_invalid': 'null', 'format': 'geojson'},
                          {'input': ([('g', 'v_wstring')], rows)})
        values = out['output'].column('Buffer')
        self.assertIsNone(values[0])
        self.assertTrue(values[1].startswith('{"type":"Polygon"'))


if __name__ == '__main__':
    unittest.main()


class ExampleTests(unittest.TestCase):
    def test_uk_spatial_example(self):
        from flowengine.executor import run_file
        from helpers import ROOT
        result = run_file(ROOT / 'examples/spatial/uk_spatial.flow.json')
        self.assertTrue(result.ok, result.error)
        to_london = {r['City']: r['Distance'] for r in result.output('to_london').to_dicts()}
        self.assertAlmostEqual(to_london['Edinburgh'], haversine_km(LONDON, EDINBURGH), delta=3)
        self.assertEqual(to_london['London'], 0.0)
        nearest = [(r['Depot'], r['City']) for r in result.output('nearest_cities', 'matched').to_dicts() if r['Rank'] == 1]
        self.assertIn(('Midlands hub', 'Birmingham'), nearest)
        areas = result.output('area_info').column('Area')
        self.assertTrue(all(abs(a - math.pi * 50 ** 2) / (math.pi * 50 ** 2) < 0.01 for a in areas))
