"""Tableau (real Hyper API + REST fixture), Anaplan (fixture of the documented Integration API v2) and ODBC Loader
(live through unixODBC and the SQLite ODBC driver).

Hyper tests need tableauhyperapi; ODBC tests need pyodbc and FLOWBENCH_ODBC_SQLITE_DRIVER (the SQLite3 ODBC driver
name or library path). Both run in the service-integration CI job.
"""
import base64
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import flowengine.db.vault as vault_module
import flowengine.tools.anaplan as anaplan
from flowengine.db.connections import ConnectionStore
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}
HAS_HYPER = importlib.util.find_spec('tableauhyperapi') is not None
ODBC_DRIVER = os.environ.get('FLOWBENCH_ODBC_SQLITE_DRIVER')
HAS_ODBC = importlib.util.find_spec('pyodbc') is not None and bool(ODBC_DRIVER)


class St:
    def __init__(self):
        self.calls, self.published, self.uploads = [], [], {}
        self.files = {'113000000001': {'name': 'Sales.csv', 'chunks': []}}
        self.export_data = [b'Region,Units\nNorth,5\n', b'South,7\n']
        self.import_fail = False


class H(BaseHTTPRequestHandler):
    st: St = None

    def log_message(self, *a):
        pass

    def send(self, code, body=None):
        data = b'' if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def go(self, method):
        st, parts = H.st, urlsplit(self.path)
        path, q = unquote(parts.path), {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        st.calls.append((method, path, q, dict(self.headers), body))
        # ---------------- Anaplan
        if path == '/token/authenticate':
            if self.headers.get('Authorization') != 'Basic ' + base64.b64encode(b'ada@example.com:pw').decode():
                return self.send(401, {'status': 'FAILURE_BAD_CREDENTIAL'})
            return self.send(201, {'status': 'SUCCESS', 'tokenInfo': {'tokenValue': 'ANA', 'expiresAt': 1}})
        m = re.fullmatch(r'/2/0/workspaces/ws1/models/m1/(.+)', path)
        if m:
            assert self.headers.get('Authorization') == 'AnaplanAuthToken ANA'
            sub = m.group(1)
            if sub == 'exports':
                return self.send(200, {'exports': [{'id': '116000000001', 'name': 'Sales by region.csv'}]})
            if sub == 'imports':
                return self.send(200, {'imports': [{'id': '112000000001', 'name': 'Load sales'}]})
            if sub == 'files':
                return self.send(200, {'files': [{'id': k, 'name': v['name']} for k, v in st.files.items()]})
            if re.fullmatch(r'(exports|imports)/\d+/tasks', sub):
                assert json.loads(body) == {'localeName': 'en_US'}
                return self.send(200, {'task': {'taskId': 'T1', 'taskState': 'NOT_STARTED'}})
            if (t := re.fullmatch(r'(exports|imports)/\d+/tasks/T1', sub)):
                polls = sum(1 for c in st.calls if c[1].endswith('/tasks/T1'))
                if polls < 2:
                    return self.send(200, {'task': {'taskId': 'T1', 'taskState': 'IN_PROGRESS'}})
                ok = not (t.group(1) == 'imports' and st.import_fail)
                return self.send(200, {'task': {'taskId': 'T1', 'taskState': 'COMPLETE', 'result': {
                    'successful': ok, 'failureDumpAvailable': not ok,
                    'details': [] if ok else [{'localMessageText': '2 rows rejected'}]}}})
            if sub == 'files/116000000001/chunks':
                return self.send(200, {'chunks': [{'id': '1', 'name': 'Chunk 1'}, {'id': '0', 'name': 'Chunk 0'}]})
            if (c := re.fullmatch(r'files/116000000001/chunks/(\d+)', sub)):
                return self.send(200, st.export_data[int(c.group(1))])
            if sub == 'files/113000000001' and method == 'POST':
                assert json.loads(body) == {'chunkCount': -1}
                st.files['113000000001']['chunks'] = []
                return self.send(200, {'file': {'id': '113000000001', 'chunkCount': -1}})
            if (c := re.fullmatch(r'files/113000000001/chunks/(\d+)', sub)) and method == 'PUT':
                assert int(c.group(1)) == len(st.files['113000000001']['chunks']), 'chunks in order from 0'
                st.files['113000000001']['chunks'].append(body)
                return self.send(204)
            if sub == 'files/113000000001/complete':
                doc = json.loads(body)
                assert doc['chunkCount'] == len(st.files['113000000001']['chunks']) and doc['id'] == '113000000001'
                return self.send(200, {'file': doc})
        # ---------------- Tableau REST
        if path == '/api/3.21/auth/signin':
            cred = json.loads(body)['credentials']
            if (cred['personalAccessTokenName'], cred['personalAccessTokenSecret']) != ('flowbench', 'pat-secret'):
                return self.send(401, {'error': {'code': '401001', 'summary': 'Signin Error', 'detail': 'bad token'}})
            return self.send(200, {'credentials': {'token': 'TAB', 'site': {'id': 'site-1', 'contentUrl': cred['site']['contentUrl']}}})
        if path.startswith('/api/3.21/'):
            if self.headers.get('X-Tableau-Auth') != 'TAB':
                return self.send(401, {'error': {'code': '401002', 'summary': 'Unauthorized', 'detail': 'no token'}})
            if path == '/api/3.21/auth/signout':
                return self.send(204)
            if path == '/api/3.21/sites/site-1/projects':
                return self.send(200, {'projects': {'project': [{'id': 'p-1', 'name': 'Sales'}] if q['filter'] == 'name:eq:Sales' else []}})
            if path == '/api/3.21/sites/site-1/fileUploads' and method == 'POST':
                sid = f'u{len(st.uploads)}'
                st.uploads[sid] = b''
                return self.send(201, {'fileUpload': {'uploadSessionId': sid, 'fileSize': '0'}})
            if (u := re.fullmatch(r'/api/3.21/sites/site-1/fileUploads/(u\d+)', path)):
                st.uploads[u.group(1)] += _part(self.headers, body, 'tableau_file')
                return self.send(200, {'fileUpload': {'uploadSessionId': u.group(1)}})
            if path == '/api/3.21/sites/site-1/datasources':
                assert self.headers['Content-Type'].startswith('multipart/mixed; boundary=')
                payload = _part(self.headers, body, 'request_payload').decode()
                data = st.uploads.pop(q['uploadSessionId']) if 'uploadSessionId' in q else _part(self.headers, body, 'tableau_datasource')
                st.published.append({'payload': payload, 'query': q, 'data': data})
                return self.send(201, {'datasource': {'id': 'ds-1', 'name': re.search(r'name="([^"]+)"', payload).group(1)}})
        return self.send(404, {'error': {'code': '404', 'summary': 'Not found', 'detail': path}})

    def do_GET(self):
        self.go('GET')

    def do_POST(self):
        self.go('POST')

    def do_PUT(self):
        self.go('PUT')


def _part(headers, body, name):
    boundary = headers['Content-Type'].split('boundary=')[1].encode()
    for piece in body.split(b'--' + boundary):
        if f'name="{name}"'.encode() in piece.split(b'\r\n\r\n', 1)[0]:
            return piece.split(b'\r\n\r\n', 1)[1][:-2]
    return b''


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
        H.st = self.st = St()
        self._home = tempfile.TemporaryDirectory()
        self.dir = Path(self._home.name)
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault()),
                         mock.patch.object(anaplan.Anaplan, 'auth_url', f'{self.root}/token/authenticate'),
                         mock.patch.object(anaplan, 'POLL_SECONDS', 0)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def run_tool(self, tool, config, limits=NET, data=None):
        nodes = [{'id': 'x', 'tool': tool, 'config': config}]
        edges = []
        if data:
            nodes.insert(0, text_input('t', *data))
            edges.append(edge('t', 'output', 'x', 'input'))
        return run_workflow(workflow(nodes, edges), base_dir=self.dir, limits=limits)


class AnaplanTests(Base):
    def cfg(self, **extra):
        conn = ServiceConnectionStore().save({'name': 'Anaplan', 'kind': 'web', 'base_url': f'{self.root}/2/0', 'auth': 'basic',
                                              'username': 'ada@example.com'}, secret='pw').id
        return {'provider': 'anaplan', 'connection_id': conn, 'workspace_id': 'ws1', 'model_id': 'm1', **extra}

    def test_export_runs_the_task_and_reads_the_chunks_in_order(self):
        r = self.run_tool('core.anaplan_input', self.cfg(export='Sales by region.csv',
                                                          read={'fields': [{'name': 'Region', 'type': 'v_wstring'}, {'name': 'Units', 'type': 'int32'}]}))
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual(list(r.outputs['x']['output'].rows()), [('North', 5), ('South', 7)])
        self.assertEqual(sum(1 for c in self.st.calls if c[1] == '/token/authenticate'), 1, 'one token per run')
        self.assertIn('No Anaplan export', self.run_tool('core.anaplan_input', self.cfg(export='Nope')).error['message'])

    def test_import_uploads_chunks_completes_and_reports_task_failures(self):
        rows = [[f'R{i}', str(i)] for i in range(3000)]
        with mock.patch.object(anaplan, 'CHUNK', 8 * 1024):
            r = self.run_tool('core.anaplan_output', self.cfg(import_action='Load sales', file='Sales.csv'), WRITE,
                              ([('Region', 'v_wstring'), ('Units', 'int32')], rows))
        self.assertEqual(r.status, 'succeeded', r.error)
        data = b''.join(self.st.files['113000000001']['chunks'])
        self.assertGreater(len(self.st.files['113000000001']['chunks']), 2)
        self.assertTrue(data.startswith(b'Region,Units\nR0,0\n') and data.count(b'\n') == 3001)
        self.st.import_fail = True
        bad = self.run_tool('core.anaplan_output', self.cfg(import_action='112000000001', file='Sales.csv'), WRITE,
                            ([('Region', 'v_wstring')], [['x']]))
        self.assertIn('2 rows rejected', bad.error['message'])
        self.assertIn('failure dump', bad.error['message'])
        conn = ServiceConnectionStore().save({'name': 'Wrong', 'kind': 'web', 'base_url': f'{self.root}/2/0', 'auth': 'basic',
                                              'username': 'ada@example.com'}, secret='nope').id
        self.assertIn('refused the sign-in', self.run_tool('core.anaplan_input', {**self.cfg(export='x'), 'connection_id': conn}).error['message'])


@unittest.skipUnless(HAS_HYPER, 'tableauhyperapi is not installed (runs in the service-integration CI job)')
class TableauTests(Base):
    def conn(self):
        return ServiceConnectionStore().save({'name': 'Tableau', 'kind': 'web', 'base_url': self.root, 'auth': 'basic',
                                              'username': 'flowbench'}, secret='pat-secret').id

    def test_local_extract_holds_typed_and_spatial_columns(self):
        import tableauhyperapi as h
        data = ([('id', 'int64'), ('city', 'v_wstring'), ('day', 'date'), ('loc', 'v_wstring')],
                [['1', 'Zürich', '2026-01-02', 'POINT (8.54 47.37)'], ['2', 'Lagos', None, None]])
        r = self.run_tool('core.tableau_output', {'provider': 'tableau', 'output': 'local', 'path': 'out/cities.hyper',
                                                   'spatial_fields': ['loc']}, {}, data)
        self.assertEqual(r.status, 'succeeded', r.error)
        with h.HyperProcess(telemetry=h.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp, \
                h.Connection(hp.endpoint, str(self.dir / 'out/cities.hyper')) as c:
            rows = c.execute_list_query('SELECT id, city, CAST(loc AS TEXT) FROM "Extract"."Extract" ORDER BY id')
            types = [str(col.type) for col in c.catalog.get_table_definition(h.TableName('Extract', 'Extract')).columns]
        self.assertEqual(rows, [[1, 'Zürich', 'POINT(8.5400000 47.3700000)'], [2, 'Lagos', None]])
        self.assertEqual(types, ['BIG_INT', 'TEXT', 'DATE', 'TABLEAU.TABGEOGRAPHY'])

    def test_publish_signs_in_with_a_token_publishes_and_signs_out(self):
        data = ([('id', 'int64')], [['1'], ['2']])
        cfg = {'provider': 'tableau', 'connection_id': self.conn(), 'datasource': 'Sales extract', 'project': 'Sales', 'site': 'emea'}
        r = self.run_tool('core.publish_to_tableau_server', cfg, WRITE, data)
        self.assertEqual(r.status, 'succeeded', r.error)
        pub = self.st.published[0]
        self.assertEqual((pub['query'], pub['data'][:4] != b''), ({'overwrite': 'true'}, True))
        self.assertIn('<project id="p-1"/>', pub['payload'])
        self.assertTrue(any(c[1] == '/api/3.21/auth/signout' for c in self.st.calls))
        import flowengine.tools.tableau as tableau
        with mock.patch.object(tableau, 'SINGLE_REQUEST_MAX', 10), mock.patch.object(tableau, 'UPLOAD_CHUNK', 4096):
            r = self.run_tool('core.tableau_output', {**cfg, 'output': 'publish', 'mode': 'append'}, WRITE, data)
        self.assertEqual(r.status, 'succeeded', r.error)
        big = self.st.published[1]
        self.assertEqual((big['query']['append'], big['query']['datasourceType']), ('true', 'hyper'))
        self.assertGreater(len([c for c in self.st.calls if c[0] == 'PUT']), 1, 'chunked file upload')
        self.assertIn('No Tableau project', self.run_tool('core.publish_to_tableau_server', {**cfg, 'project': 'Nope'}, WRITE, data).error['message'])
        self.assertEqual(self.run_tool('core.publish_to_tableau_server', cfg, NET, data).error['code'], 'permission-denied')


class TableauWithoutHyperTests(Base):
    def test_a_missing_hyper_api_is_explained(self):
        import builtins
        real = builtins.__import__

        def no_hyper(name, *a, **k):
            if name == 'tableauhyperapi':
                raise ImportError(name)
            return real(name, *a, **k)
        with mock.patch.object(builtins, '__import__', no_hyper):
            r = self.run_tool('core.tableau_output', {'provider': 'tableau', 'output': 'local', 'path': 'x.hyper'}, {},
                              ([('a', 'int32')], [['1']]))
        self.assertIn('pip install tableauhyperapi', r.error['message'])


@unittest.skipUnless(HAS_ODBC, 'pyodbc or the SQLite ODBC driver is missing (runs in the service-integration CI job)')
class OdbcTests(Base):
    def test_metadata_of_tables_views_and_columns(self):
        db = self.dir / 'shop.db'
        sqlite3.connect(db).executescript('create table sales(id integer primary key, region text not null, amount real);'
                                          'create table staff(name text); create view north as select * from sales;')
        conn = ConnectionStore().save({'name': 'Shop', 'dialect': 'odbc', 'host': '', 'port': 0, 'database': str(db), 'user': '',
                                       'options': {'driver': ODBC_DRIVER}}).id
        r = self.run_tool('core.odbc_loader', {'provider': 'odbc', 'connection_id': conn})
        self.assertEqual(r.status, 'succeeded', r.error)
        cols = [dict(zip(r.outputs['x']['columns'].schema.names, row)) for row in r.outputs['x']['columns'].rows()]
        sales = [(c['Column'], c['TypeName'], c['Nullable']) for c in cols if c['Table'] == 'sales']
        self.assertEqual(sales, [('id', 'INTEGER', True), ('region', 'TEXT', False), ('amount', 'REAL', True)])
        self.assertEqual(sorted({(c['Table'], c['TableType']) for c in cols}), [('north', 'VIEW'), ('sales', 'TABLE'), ('staff', 'TABLE')])
        only = self.run_tool('core.odbc_loader', {'provider': 'odbc', 'connection_id': conn, 'table': 'staff'})
        self.assertEqual({row[2] for row in only.outputs['x']['columns'].rows()}, {'staff'})


if __name__ == '__main__':
    unittest.main()
