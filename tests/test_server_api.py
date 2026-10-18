"""Server API tool against a controlled fixture of the documented Alteryx Server Web API (no real server)."""
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import flowengine.db.vault as vault_module
from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}
KEY, SECRET = 'key-123', 'secret-456'


class FakeServer:
    def __init__(self):
        self.tokens, self.calls, self.expire_next = set(), [], False
        self.workflows = {'wf1': {'id': 'wf1', 'name': 'Sales', 'versions': [{'versionId': 'v1'}], 'isPublic': False},
                          'wf2': {'id': 'wf2', 'name': 'Churn', 'versions': [], 'isPublic': True}}


class Handler(BaseHTTPRequestHandler):
    srv: FakeServer = None

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self):
        token = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
        if self.srv.expire_next and token in self.srv.tokens:
            self.srv.expire_next = False
            self.srv.tokens.discard(token)
        return token in self.srv.tokens

    def _handle(self, method):
        parts = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        self.srv.calls.append((method, parts.path, parse_qs(parts.query), body))
        if parts.path == '/webapi/oauth2/token':
            expected = 'Basic ' + base64.b64encode(f'{KEY}:{SECRET}'.encode()).decode()
            if method != 'POST' or self.headers.get('Authorization') != expected or body != b'grant_type=client_credentials':
                return self._send(401, {'error': 'invalid_client'})
            token = f'tok{len(self.srv.tokens) + len(self.srv.calls)}'
            self.srv.tokens.add(token)
            return self._send(200, {'access_token': token, 'token_type': 'bearer', 'expires_in': 3600})
        if not self._authorised():
            return self._send(401, {'message': 'Authorization has been denied for this request.'})
        if parts.path == '/webapi/v3/workflows' and method == 'GET':
            items = list(self.srv.workflows.values())
            name = parse_qs(parts.query).get('name')
            return self._send(200, [w for w in items if not name or w['name'] == name[0]])
        if parts.path.startswith('/webapi/v3/workflows/'):
            wid = parts.path.rsplit('/', 1)[1]
            if wid not in self.srv.workflows:
                return self._send(404, {'message': 'Workflow not found'})
            if method == 'GET':
                return self._send(200, self.srv.workflows[wid])
            if method == 'PUT':
                self.srv.workflows[wid].update(json.loads(body))
                return self._send(200, self.srv.workflows[wid])
            if method == 'DELETE':
                del self.srv.workflows[wid]
                return self._send(200, b'')
        if parts.path == '/webapi/v3/report.txt':
            return self._send(200, b'plain text, not json')
        return self._send(404, {'message': 'No such endpoint'})

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_DELETE(self):
        self._handle('DELETE')


class ServerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.http.server_address[1]}/webapi'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        Handler.srv = self.srv = FakeServer()
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        self.conn = ServiceConnectionStore().save({'name': 'Server', 'kind': 'web', 'base_url': self.base, 'auth': 'basic',
                                                   'username': KEY}, secret=SECRET).id

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def run_api(self, data=None, limits=NET, **config):
        nodes = [{'id': 'api', 'tool': 'core.server_api',
                  'config': {'provider': 'alteryx-server', 'connection_id': self.conn, **config}}]
        edges = []
        if data:
            nodes.insert(0, text_input('d', *data))
            edges.append(edge('d', 'output', 'api', 'data'))
        return run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)

    def test_list_call_authenticates_and_parses_one_level_deep(self):
        r = self.run_api(endpoint='v3/workflows')
        self.assertEqual(r.status, 'succeeded', r.error)
        ok = r.outputs['api']['success']
        self.assertEqual(ok.schema.names, ['Call', 'id', 'name', 'versions', 'isPublic'])
        self.assertEqual([(row[1], row[4]) for row in ok.rows()], [('wf1', False), ('wf2', True)])
        self.assertEqual(json.loads(next(ok.rows())[3]), [{'versionId': 'v1'}], 'nested values stay JSON text')
        self.assertEqual([c[1] for c in self.srv.calls], ['/webapi/oauth2/token', '/webapi/v3/workflows'])

    def test_each_data_record_is_one_call_and_failures_go_to_f(self):
        r = self.run_api(data=([('wid', 'v_wstring')], [['wf2'], ['nope']]), endpoint='v3/workflows/{wid}',
                         query={'view': 'Full'})
        self.assertEqual([row[:3] for row in r.outputs['api']['success'].rows()], [(1, 'wf2', 'Churn')])
        fail = list(r.outputs['api']['fail'].rows())
        self.assertEqual((fail[0][0], fail[0][3]), (2, 404))
        self.assertIn('Workflow not found', fail[0][4])
        self.assertEqual([row[3] for row in r.outputs['api']['log'].rows()], [200, 404])
        self.assertEqual(sum(c[1] == '/webapi/oauth2/token' for c in self.srv.calls), 1, 'one token per run')
        self.assertEqual(self.srv.calls[-1][2], {'view': ['Full']})

    def test_an_expired_token_is_renewed_once(self):
        self.srv.expire_next = True
        r = self.run_api(endpoint='v3/workflows', query={'name': 'Sales'})
        self.assertEqual([row[2] for row in r.outputs['api']['success'].rows()], ['Sales'])
        self.assertEqual(sum(c[1] == '/webapi/oauth2/token' for c in self.srv.calls), 2)

    def test_writes_need_permission_and_delete_needs_confirmation(self):
        payload = '{"name": "{new_name}"}'
        data = ([('wid', 'v_wstring'), ('new_name', 'v_wstring')], [['wf1', 'Sales "2026"']])
        r = self.run_api(data=data, method='PUT', endpoint='v3/workflows/{wid}', payload=payload)
        self.assertEqual(r.error['code'], 'permission-denied')
        r = self.run_api(data=data, limits=WRITE, method='PUT', endpoint='v3/workflows/{wid}', payload=payload)
        self.assertEqual(self.srv.workflows['wf1']['name'], 'Sales "2026"', r.error)
        r = self.run_api(data=data, limits=WRITE, method='DELETE', endpoint='v3/workflows/{wid}')
        self.assertIn('confirm_delete', r.error['message'])
        r = self.run_api(data=data, limits=WRITE, method='DELETE', endpoint='v3/workflows/{wid}', confirm_delete=True)
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertNotIn('wf1', self.srv.workflows)

    def test_wrong_secret_endpoint_outside_api_and_non_json_responses(self):
        r = self.run_api(endpoint='v3/report.txt')
        self.assertEqual(next(r.outputs['api']['success'].rows())[1:], ('Response is not JSON; see DownloadData', 'plain text, not json'))
        for bad in ('../oauth2/token', 'v4/things', 'https://elsewhere/v3/x'):
            self.assertEqual(self.run_api(endpoint=bad).status, 'failed', bad)
        ServiceConnectionStore().save({**ServiceConnectionStore().get(self.conn).public()}, secret='wrong')
        r = self.run_api(endpoint='v3/workflows')
        self.assertIn('refused the API key', r.error['message'])
        with self.assertRaises(WorkflowValidationError):  # the node must name this tool's provider
            self.run_api(endpoint='v3/workflows', provider='spark-local')


if __name__ == '__main__':
    unittest.main()
