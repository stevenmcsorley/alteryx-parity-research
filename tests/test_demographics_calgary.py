"""Allocate tools against a fixture of the documented Census ACS API, and the Calgary tools on the open .fbcal store."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import flowengine.db.vault as vault_module
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
COUNTIES = {('06', '001'): ('Alameda County, California', '1663823'), ('06', '075'): ('San Francisco County, California', '-666666666'),
            ('48', '201'): ('Harris County, Texas', '4780913')}


class H(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def send(self, code, body=None, headers=None):
        data = b'' if body is None else json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts = urlsplit(self.path)
        path, q = unquote(parts.path), {k: v[0] for k, v in parse_qs(parts.query).items()}
        H.calls.append((path, q))
        if q.get('key') != 'K1':
            return self.send(302, None, {'Location': 'https://api.census.gov/data/missing_key.html'})
        base = '/data/2023/acs/acs5'
        if path.startswith(f'{base}/variables/') and path.endswith('.json'):
            code = path.rsplit('/', 1)[1][:-5]
            return self.send(200, {'name': code, 'label': f'Estimate!!Total!!{code}', 'concept': 'Population',
                                   'predicateType': 'string' if code.endswith('_S') else 'int'})
        if path == f'{base}/groups/B01003.json':
            return self.send(200, {'variables': {'B01003_001E': {'label': 'Estimate!!Total', 'concept': 'Total Population', 'group': 'B01003',
                                                                 'predicateType': 'int'},
                                                 'B01003_001M': {'label': 'Margin of Error!!Total', 'concept': 'Total Population', 'group': 'B01003',
                                                                 'predicateType': 'int'}}})
        if path == base:
            names = q['get'].split(',')
            assert len(names) <= 50, 'at most 50 names per request'
            geo = q['for']
            if geo.startswith('county:'):
                state = q['in'].split(':')[1]
                # a test variable B99999_nnnE has the value nnn + 1
                rows = [[name] + [pop if n == 'B01003_001E' else str(int(n[7:10]) + 1) for n in names[1:]] + [s, c]
                        for (s, c), (name, pop) in COUNTIES.items() if s == state]
                return self.send(200, [names + ['state', 'county'], *rows])
            if geo.startswith('zip code tabulation area:'):
                codes = geo.split(':')[1].split(',')
                return self.send(200, [names + ['zip code tabulation area'],
                                       *[[f'ZCTA5 {z}'] + ['100'] * (len(names) - 1) + [z] for z in codes if z != '99999']])
        return self.send(404, None)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.root = f'http://127.0.0.1:{cls.http.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        H.calls = []
        self._home = tempfile.TemporaryDirectory()
        self.dir = Path(self._home.name)
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        store = ServiceConnectionStore()
        self.census = store.save({'name': 'ACS', 'kind': 'web', 'base_url': f'{self.root}/data', 'auth': 'signed-url'}, secret='key=K1').id
        self.nokey = store.save({'name': 'ACS no key', 'kind': 'web', 'base_url': f'{self.root}/data', 'auth': 'none'}).id

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def run_tool(self, tool, config, data=None, limits=NET):
        nodes = [{'id': 'x', 'tool': tool, 'config': config}]
        edges = []
        if data:
            nodes.insert(0, text_input('t', *data))
            edges.append(edge('t', 'output', 'x', 'input'))
        return run_workflow(workflow(nodes, edges), base_dir=self.dir, limits=limits)

    def rows(self, r, anchor='output'):
        t = r.outputs['x'][anchor]
        return [dict(zip(t.schema.names, row)) for row in t.rows()]


class AllocateTests(Base):
    def acs(self, **cfg):
        return {'provider': 'us-census-acs', 'connection_id': self.census, **cfg}

    def test_input_reads_a_level_with_typed_values_and_nulls_for_sentinels(self):
        r = self.run_tool('core.allocate_input', self.acs(level='county', state='06', variables=['B01003_001E']))
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual([(x['GEOID'], x['B01003_001E']) for x in self.rows(r)], [('06001', 1663823), ('06075', None)])
        self.assertIn('needs a county', self.run_tool('core.allocate_input', self.acs(level='tract', state='06', variables=['B01003_001E'])).error['message'])
        missing = self.run_tool('core.allocate_input', {**self.acs(level='state', variables=['B01003_001E']), 'connection_id': self.nokey})
        self.assertIn('key_signup', missing.error['message'])

    def test_append_matches_geoids_one_request_per_state_and_chunks_variables(self):
        variables = ['B01003_001E'] + [f'B99999_{i:03d}E' for i in range(60)]
        r = self.run_tool('core.allocate_append', self.acs(key_field='fips', level='county', variables=variables, prefix='acs_'),
                          ([('fips', 'v_wstring'), ('n', 'int32')], [['06001', '1'], ['48201', '2'], ['06999', '3']]))
        self.assertEqual(r.status, 'succeeded', r.error)
        out = self.rows(r)
        self.assertEqual([(o['acs_B01003_001E'], o['acs_GeographyName']) for o in out],
                         [(1663823, 'Alameda County, California'), (4780913, 'Harris County, Texas'), (None, None)])
        self.assertEqual(out[0]['acs_B99999_059E'], 60, 'second chunk of variables merged')
        data_calls = [c for c in H.calls if c[0] == '/data/2023/acs/acs5']
        self.assertEqual(len(data_calls), 4, 'two states x two variable chunks')
        bad = self.run_tool('core.allocate_append', self.acs(key_field='fips', level='tract', variables=['B01003_001E']),
                            ([('fips', 'v_wstring')], [['06001']]))
        self.assertIn('11 digits', bad.error['message'])
        z = self.run_tool('core.allocate_append', self.acs(key_field='zip', level='zip code tabulation area', variables=['B01003_001E']),
                          ([('zip', 'v_wstring')], [['94110'], ['99999']]))
        self.assertEqual([o['B01003_001E'] for o in self.rows(z)], [100, None])

    def test_metainfo_and_report(self):
        meta = self.run_tool('core.allocate_metainfo', self.acs(groups=['B01003'], search='total'))
        self.assertEqual([m['Variable'] for m in self.rows(meta)], ['B01003_001E', 'B01003_001M'])
        rep = self.run_tool('core.allocate_report', self.acs(geography_field='g', variables=['B01003_001E']),
                            ([('g', 'v_wstring'), ('B01003_001E', 'int64')], [['Alameda', '1663823'], ['Harris', '4780913']]))
        doc = json.loads(self.rows(rep)[0]['Report'])
        self.assertEqual(doc['kind'], 'table')
        self.assertEqual([c['label'] for c in doc['columns']], ['Variable', 'Alameda', 'Harris'])
        self.assertEqual(doc['rows'][0], ['Total – B01003_001E (B01003_001E)', '1,663,823', '4,780,913'])


class CalgaryTests(Base):
    def load(self):
        fields = [('id', 'int64'), ('region', 'v_wstring'), ('segment', 'v_wstring'), ('spend', 'double'), ('joined', 'date')]
        rows = [[str(i), ['North', 'South', 'East'][i % 3], ['A', 'B'][i % 2], str(i * 1.5), f'2026-0{i % 9 + 1}-01'] for i in range(1, 31)]
        r = self.run_tool('core.calgary_loader', {'path': 'db/customers.fbcal', 'index_fields': ['region', 'segment']}, (fields, rows), {})
        self.assertEqual(r.status, 'succeeded', r.error)
        return self.rows(r)[0]

    def test_loader_builds_an_indexed_open_file(self):
        summary = self.load()
        self.assertEqual(summary['Records'], 30)
        conn = sqlite3.connect(self.dir / 'db/customers.fbcal')
        self.assertIn('idx_0', [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")])
        conn.close()
        self.assertIn('cydb', self.run_tool('core.calgary_input', {'path': 'x.cydb'}).error['message'])

    def test_input_queries_with_conditions_and_keeps_types(self):
        self.load()
        r = self.run_tool('core.calgary_input', {'path': 'db/customers.fbcal', 'fields': ['id', 'spend', 'joined'],
                                                  'query': [{'field': 'region', 'op': 'in', 'values': ['North']},
                                                            {'field': 'spend', 'op': 'between', 'values': [10, 30]}]})
        self.assertEqual(r.status, 'succeeded', r.error)
        rows = self.rows(r)
        self.assertEqual([x['id'] for x in rows], [9, 12, 15, 18])
        import datetime
        self.assertIsInstance(rows[0]['joined'], datetime.date)
        self.assertIn('not in the database', self.run_tool('core.calgary_input', {'path': 'db/customers.fbcal',
                                                                                  'query': [{'field': 'nope', 'op': 'is_null'}]}).error['message'])

    def test_join_cross_count_and_append(self):
        self.load()
        j = self.run_tool('core.calgary_join', {'path': 'db/customers.fbcal', 'on': [{'input': 'r', 'database': 'region'}],
                                                 'query': [{'field': 'segment', 'op': '=', 'value': 'A'}], 'fields': ['id']},
                          ([('r', 'v_wstring')], [['North'], ['West']]))
        self.assertEqual(j.status, 'succeeded', j.error)
        self.assertEqual(sorted(x['DB_id'] for x in self.rows(j, 'joined')), [6, 12, 18, 24, 30])  # North (id % 3 == 0) and A (even)
        self.assertEqual([x['r'] for x in self.rows(j, 'unjoined')], ['West'])
        cc = self.run_tool('core.calgary_cross_count', {'path': 'db/customers.fbcal', 'fields': ['region', 'segment']})
        self.assertEqual([(x['region'], x['segment'], x['Count']) for x in self.rows(cc)][:2], [('East', 'A', 5), ('East', 'B', 5)])
        self.assertEqual(sum(x['Count'] for x in self.rows(cc)), 30)
        app = self.run_tool('core.calgary_cross_count_append', {'path': 'db/customers.fbcal', 'on': [{'input': 'r', 'database': 'region'}],
                                                                'cross_field': 'segment'}, ([('r', 'v_wstring')], [['North'], ['West']]))
        self.assertEqual([(x['Count_A'], x['Count_B'], x['Count_Total']) for x in self.rows(app)], [(5, 5, 10), (0, 0, 0)])


if __name__ == '__main__':
    unittest.main()
