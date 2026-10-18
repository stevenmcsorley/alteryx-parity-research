"""Non Overlap Drivetime against a controlled Valhalla /isochrone fixture (square areas, no road data)."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import shapely
from shapely.geometry import Point, box

import flowengine.db.vault as vault_module
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore
from flowengine.tools.drivetime import split_overlaps

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault


class Valhalla(BaseHTTPRequestHandler):
    """Square 'drive-time' areas: half-width 0.01 degrees per 10 minutes, centred on the point."""
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Valhalla.requests.append((self.path, body))
        loc, minutes = body['locations'][0], body['contours'][0]['time']
        if loc['lon'] > 100:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"No suitable edges near location"}')
            return
        h = 0.001 * minutes
        square = box(loc['lon'] - h, loc['lat'] - h, loc['lon'] + h, loc['lat'] + h)
        data = json.dumps({'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {'contour': minutes, 'metric': 'time'},
             'geometry': shapely.geometry.mapping(square)}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)


class DrivetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), Valhalla)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        Valhalla.requests = []
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        self.conn = ServiceConnectionStore().save({'name': 'Valhalla', 'kind': 'web', 'auth': 'none',
                                                   'base_url': f'http://127.0.0.1:{self.http.server_address[1]}'}).id

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def run_tool(self, points, limits={'allow_network': True}, **config):
        nodes = [text_input('t', [('Store', 'v_wstring'), ('Location', 'v_wstring')], points),
                 {'id': 'd', 'tool': 'core.non_overlap_drivetime', 'config': {
                     'provider': 'valhalla', 'connection_id': self.conn, 'geometry_field': 'Location', 'minutes': 10,
                     'grid_size': 0.05, 'grid_units': 'km', **config}}]
        return run_workflow(workflow(nodes, [edge('t', 'output', 'd', 'input')]), base_dir=ROOT, limits=limits)

    def test_overlapping_areas_are_split_between_the_nearest_points(self):
        r = self.run_tool([['A', 'POINT (-0.100 51.5)'], ['B', 'POINT (-0.088 51.5)'], ['C', 'POINT (0.5 51.5)']])
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['d']['output']
        self.assertEqual(out.schema.names, ['Store', 'Location', 'DriveTimeMinutes', 'DriveTimeArea'])
        areas = {row[0]: shapely.from_wkt(row[3]) for row in out.rows()}
        self.assertLess(areas['A'].intersection(areas['B']).area, 1e-12, 'areas do not overlap')
        squares = {k: box(x - 0.01, 51.49, x + 0.01, 51.51) for k, x in (('A', -0.1), ('B', -0.088), ('C', 0.5))}
        self.assertAlmostEqual(shapely.union_all(list(areas.values())).area, shapely.union_all(list(squares.values())).area, places=7)
        # an area without overlap is unchanged, up to the 1 mm overlay grid and the projection round trip
        self.assertLess(areas['C'].symmetric_difference(squares['C']).area / squares['C'].area, 1e-6)
        self.assertTrue(areas['A'].contains(Point(-0.0945, 51.5)) and areas['B'].contains(Point(-0.0935, 51.5)),
                        'overlap cells go to the nearer point (midline at -0.094)')
        self.assertEqual([b['costing'] for _, b in Valhalla.requests], ['auto'] * 3)
        self.assertTrue(all(p == '/isochrone' and b['polygons'] for p, b in Valhalla.requests))

    def test_needs_network_permission_points_and_a_reachable_location(self):
        self.assertEqual(self.run_tool([['A', 'POINT (0 51)']], limits={}).error['code'], 'permission-denied')
        self.assertIn('point geometry', self.run_tool([['A', 'LINESTRING (0 0, 1 1)']]).error['message'])
        self.assertIn('No suitable edges', self.run_tool([['A', 'POINT (120 10)']]).error['message'])
        self.assertIn('larger grid size', self.run_tool([['A', 'POINT (0 51)'], ['B', 'POINT (0.001 51)']],
                                                       grid_size=0.01, grid_units='km', minutes=200).error['message'])

    def test_split_keeps_every_overlap_cell_inside_its_winner(self):
        a, b = box(0, 0, 10, 10), box(5, 0, 15, 10)
        left, right = split_overlaps([a, b], [Point(5, 5), Point(10, 5)], cell=1)
        self.assertAlmostEqual(left.area + right.area, 150)
        # cells [5,6] and [6,7] are nearer the left point; [7,8] is centred on the midline, a tie won by the first record
        self.assertTrue(left.equals(box(0, 0, 8, 10)), left.wkt)
        self.assertLessEqual(right.difference(b).area, 1e-9)


if __name__ == '__main__':
    unittest.main()
