"""Dataverse / Dynamics CRM and Power BI Output against controlled fixtures of their documented REST APIs."""
import datetime as dt
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import flowengine.db.vault as vault_module
import flowengine.oauth as oauth
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}
GUIDS = [f'0000000{i}-0000-0000-0000-00000000000{i}' for i in range(1, 8)]


class State:
    def __init__(self):
        self.accounts = {GUIDS[i]: {'accountid': GUIDS[i], 'name': f'Account {i}', 'revenue': 1000.0 * i,
                                    'accountnumber': f'A-{i}', 'createdon': f'2026-0{i + 1}-01T10:00:00Z', 'statecode': i % 2}
                         for i in range(5)}
        self.calls, self.datasets, self.rows = [], {}, {}


class Handler(BaseHTTPRequestHandler):
    st: State = None
    root = ''

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

    def go(self, method):
        st, parts = Handler.st, urlsplit(self.path)
        path, q = unquote(parts.path), parse_qs(parts.query)
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        st.calls.append((method, path, q, dict(self.headers), body))
        if path.endswith('/oauth2/v2.0/token'):
            return self.send(200, {'access_token': 'at', 'expires_in': 3600})
        if self.headers.get('Authorization') != 'Bearer at':
            return self.send(401, {'error': {'code': '0x80040220', 'message': 'unauthorised'}})
        # ---------------- Dataverse
        if path == '/api/data/v9.2/accounts' and method == 'GET':
            rows = sorted(st.accounts.values(), key=lambda r: r['accountid'])
            if '$filter' in q:
                rows = [r for r in rows if r['statecode'] == int(q['$filter'][0].split()[-1])]
            if '$top' in q:
                return self.send(200, {'value': [{'@odata.etag': 'W/"1"', **r} for r in rows[:int(q['$top'][0])]]})
            size = int(re.search(r'maxpagesize=(\d+)', self.headers.get('Prefer', 'maxpagesize=5000')).group(1))
            start = int(q.get('$skiptoken', ['0'])[0])
            page = rows[start:start + size]
            others = {k: v[0] for k, v in q.items() if k != '$skiptoken'}
            nxt = f"{Handler.root}/api/data/v9.2/accounts?{urlencode({**others, '$skiptoken': start + size})}" if start + size < len(rows) else None
            return self.send(200, {'@odata.context': 'x', 'value': [{'@odata.etag': 'W/"1"', '_ownerid_value': 'u1',
                                                                     '_ownerid_value@OData.Community.Display.V1.FormattedValue': 'Ada', **r}
                                                                    for r in page], **({'@odata.nextLink': nxt} if nxt else {})})
        if path == '/api/data/v9.2/accounts' and method == 'POST':
            new = f'00000009-0000-0000-0000-00000000000{len(st.accounts)}'
            st.accounts[new] = {'accountid': new, **json.loads(body)}
            return self.send(204, None, {'OData-EntityId': f'{Handler.root}/api/data/v9.2/accounts({new})'})
        if (m := re.fullmatch(r'/api/data/v9.2/accounts\((.+)\)', path)):
            key = m.group(1)
            if '=' in key:  # alternate key accountnumber='A-1'
                number = re.fullmatch(r"accountnumber='(.*)'", key).group(1).replace("''", "'")
                found = next((k for k, r in st.accounts.items() if r.get('accountnumber') == number), None)
            else:
                found = key if key in st.accounts else None
            if method == 'DELETE':
                if not found:
                    return self.send(404, {'error': {'code': '0x80040217', 'message': 'account Does Not Exist'}})
                del st.accounts[found]
                return self.send(204)
            if method == 'PATCH':
                if not found and self.headers.get('If-Match') == '*':
                    return self.send(404, {'error': {'code': '0x80040217', 'message': 'account Does Not Exist'}})
                if not found:
                    found = f'0000000a-0000-0000-0000-00000000000{len(st.accounts)}'
                    st.accounts[found] = {'accountid': found, **({'accountnumber': number} if '=' in key else {})}
                st.accounts[found].update(json.loads(body))
                return self.send(204, None, {'OData-EntityId': f'{Handler.root}/api/data/v9.2/accounts({key})'})
        # ---------------- Power BI
        if path in ('/v1.0/myorg/datasets', '/v1.0/myorg/groups/ws1/datasets'):
            if method == 'GET':
                return self.send(200, {'value': [{'id': i, 'name': d['name']} for i, d in st.datasets.items()]})
            doc = json.loads(body)
            did = f'ds{len(st.datasets) + 1}'
            st.datasets[did] = {'name': doc['name'], 'tables': {t['name']: t for t in doc['tables']},
                                'retention': q.get('defaultRetentionPolicy')}
            return self.send(201, {'id': did, 'name': doc['name']})
        if (m := re.fullmatch(r'/v1.0/myorg/(?:groups/ws1/)?datasets/(ds\d+)/tables(?:/([^/]+))?(/rows)?', path)):
            did, table, rows = m.groups()
            ds = st.datasets[did]
            if table is None:
                return self.send(200, {'value': list(ds['tables'].values())})
            if rows and method == 'POST':
                batch = json.loads(body)['rows']
                assert len(batch) <= 10000
                st.rows.setdefault((did, table), []).extend(batch)
                return self.send(200, {})
            if rows and method == 'DELETE':
                st.rows[(did, table)] = []
                return self.send(200, {})
            if method == 'PUT':
                ds['tables'][table] = json.loads(body)
                return self.send(200, json.loads(body))
        return self.send(404, {'error': {'code': 'NotFound', 'message': path}})

    def do_GET(self):
        self.go('GET')

    def do_POST(self):
        self.go('POST')

    def do_PATCH(self):
        self.go('PATCH')

    def do_PUT(self):
        self.go('PUT')

    def do_DELETE(self):
        self.go('DELETE')


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.root = Handler.root = f'http://127.0.0.1:{cls.http.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        Handler.st = self.st = State()
        oauth._CACHE.clear()
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def connect(self, base):
        return ServiceConnectionStore().save({'name': base, 'kind': 'web', 'base_url': f'{self.root}{base}', 'auth': 'oauth2',
                                              'client_id': 'app', 'scope': 'x/.default', 'token_url': f'{self.root}/t/oauth2/v2.0/token'},
                                             secret=json.dumps({'client_secret': 'cs'})).id

    def run_tool(self, tool, config, limits=NET, data=None):
        nodes = [{'id': 'x', 'tool': tool, 'config': config}]
        edges = []
        if data:
            nodes.insert(0, text_input('t', *data))
            edges.append(edge('t', 'output', 'x', 'input'))
        return run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)


class DataverseTests(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.connect('/api/data/v9.2')

    def dv(self, tool, **cfg):
        return {'provider': 'microsoft-dataverse', 'connection_id': self.conn, 'table': 'accounts', **cfg}

    def test_input_pages_with_maxpagesize_and_drops_annotations(self):
        r = self.run_tool('core.dataverse_input', self.dv('in', page_size=2))
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['x']['output']
        self.assertEqual(len(out), 5, 'three pages followed')
        self.assertEqual(out.schema.names, ['_ownerid_value', 'accountid', 'name', 'revenue', 'accountnumber', 'createdon', 'statecode'])
        first = dict(zip(out.schema.names, next(out.rows())))
        self.assertEqual((first['createdon'], first['revenue']), (dt.datetime(2026, 1, 1, 10), 0.0))
        pages = [c for c in self.st.calls if c[1] == '/api/data/v9.2/accounts']
        headers = [{k.lower(): v for k, v in c[3].items()} for c in pages]  # header names are case-insensitive
        self.assertTrue(all(h['prefer'] == 'odata.maxpagesize=2' and h['odata-version'] == '4.0' for h in headers))
        crm = self.run_tool('core.dynamics_crm_input', self.dv('in', columns=['name', 'revenue'], filter='statecode eq 1', limit=10))
        self.assertEqual([r[0] for r in crm.outputs['x']['output'].rows()], ['Account 1', 'Account 3'])
        self.assertEqual(crm.outputs['x']['output'].schema.names[:2], ['name', 'revenue'])

    def test_create_update_upsert_by_alternate_key_and_delete(self):
        r = self.run_tool('core.dataverse_output', self.dv('out'), WRITE, ([('name', 'v_wstring'), ('revenue', 'double')], [['New Co', '5']]))
        url = next(r.outputs['x']['output'].rows())[-1]
        self.assertTrue(url.endswith(')'))
        new_id = url.rsplit('(', 1)[1][:-1]
        self.assertEqual(self.st.accounts[new_id]['name'], 'New Co')
        r = self.run_tool('core.dataverse_output', self.dv('out', mode='update', key_fields=['accountid']), WRITE,
                          ([('accountid', 'v_wstring'), ('revenue', 'double')], [[GUIDS[0], '42']]))
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual(self.st.accounts[GUIDS[0]]['revenue'], 42.0)
        missing = self.run_tool('core.dataverse_output', self.dv('out', mode='update', key_fields=['accountid']), WRITE,
                                ([('accountid', 'v_wstring'), ('revenue', 'double')], [['00000000-dead-0000-0000-000000000000', '1']]))
        self.assertIn('Does Not Exist', missing.error['message'], 'an update never creates (If-Match: *)')
        up = self.run_tool('core.dynamics_crm_output', self.dv('out', mode='upsert', key_fields=['accountnumber']), WRITE,
                           ([('accountnumber', 'v_wstring'), ('name', 'v_wstring')], [['A-1', 'Renamed'], ["O'Brien-9", 'Brand new']]))
        self.assertEqual(up.status, 'succeeded', up.error)
        self.assertEqual(self.st.accounts[GUIDS[1]]['name'], 'Renamed')
        self.assertIn("O'Brien-9", [a.get('accountnumber') for a in self.st.accounts.values()])
        patch = [c for c in self.st.calls if c[0] == 'PATCH' and 'accountnumber' in c[1]]
        self.assertNotIn('accountnumber', json.loads(patch[0][4]), 'alternate key values stay out of the body')
        self.assertIn("accountnumber='O''Brien-9'", patch[1][1])
        r = self.run_tool('core.dataverse_output', self.dv('out', mode='delete', key_fields=['accountid']), WRITE, ([('accountid', 'v_wstring')], [[GUIDS[4]]]))
        self.assertIn('confirm_delete', r.error['message'])
        self.run_tool('core.dataverse_output', self.dv('out', mode='delete', key_fields=['accountid'], confirm_delete=True), WRITE,
                      ([('accountid', 'v_wstring')], [[GUIDS[4]]]))
        self.assertNotIn(GUIDS[4], self.st.accounts)
        self.assertEqual(self.run_tool('core.dataverse_output', self.dv('out'), NET, ([('name', 'v_wstring')], [['x']])).error['code'],
                         'permission-denied')
        bad = self.run_tool('core.dataverse_input', {**self.dv('in'), 'table': 'accounts?$top=1'})
        self.assertIn('logical name', bad.error['message'])


class PowerBITests(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.connect('/v1.0/myorg')

    def push(self, rows, limits=WRITE, **cfg):
        fields = [{'name': 'Region', 'type': 'v_wstring', 'size': 10000}, ('Sales', 'double'), ('Day', 'date'), ('Units', 'int32')]
        return self.run_tool('core.power_bi_output', {'provider': 'microsoft-power-bi', 'connection_id': self.conn,
                                                       'dataset': 'Sales', 'table': 'Daily', **cfg}, limits, (fields, rows))

    def test_creates_the_dataset_then_appends_and_replaces_in_batches(self):
        rows = [['North', '1.5', '2026-10-01', '3']]
        r = self.push(rows, workspace_id='ws1', retention_policy='basicFIFO')
        self.assertEqual(r.status, 'succeeded', r.error)
        ds = self.st.datasets['ds1']
        self.assertEqual(ds['retention'], ['basicFIFO'])
        self.assertEqual([(c['name'], c['dataType']) for c in ds['tables']['Daily']['columns']],
                         [('Region', 'String'), ('Sales', 'Double'), ('Day', 'Datetime'), ('Units', 'Int64')])
        self.assertEqual(self.st.rows[('ds1', 'Daily')], [{'Region': 'North', 'Sales': 1.5, 'Day': '2026-10-01T00:00:00', 'Units': 3}])
        many = [['South', str(i), '2026-10-02', str(i)] for i in range(25001)]
        self.assertEqual(self.push(many, workspace_id='ws1').status, 'succeeded')
        self.assertEqual(len(self.st.rows[('ds1', 'Daily')]), 25002)
        posts = [c for c in self.st.calls if c[0] == 'POST' and c[1].endswith('/rows')]
        self.assertEqual(len(posts), 4, 'one row plus three batches of at most 10,000')
        self.push(rows, workspace_id='ws1', mode='replace')
        self.assertEqual(len(self.st.rows[('ds1', 'Daily')]), 1)
        self.assertEqual(len(self.st.datasets), 1, 'the dataset is found by name, not created again')

    def test_schema_changes_need_update_schema_and_limits_are_checked(self):
        self.push([['North', '1', '2026-10-01', '1']])
        changed = self.run_tool('core.power_bi_output', {'provider': 'microsoft-power-bi', 'connection_id': self.conn,
                                                          'dataset': 'Sales', 'table': 'Daily'}, WRITE,
                                ([('Region', 'v_wstring'), ('Margin', 'double')], [['N', '0.2']]))
        self.assertIn('update_schema', changed.error['message'])
        ok = self.run_tool('core.power_bi_output', {'provider': 'microsoft-power-bi', 'connection_id': self.conn, 'dataset': 'Sales',
                                                     'table': 'Daily', 'update_schema': True}, WRITE,
                           ([('Region', 'v_wstring'), ('Margin', 'double')], [['N', '0.2']]))
        self.assertEqual(ok.status, 'succeeded', ok.error)
        self.assertEqual([c['name'] for c in self.st.datasets['ds1']['tables']['Daily']['columns']], ['Region', 'Margin'])
        self.assertIn('4000 per value', self.push([['x' * 4001, '1', '2026-10-01', '1']]).error['message'])
        self.assertEqual(self.push([['N', '1', '2026-10-01', '1']], limits=NET).error['code'], 'permission-denied')


if __name__ == '__main__':
    unittest.main()
