"""License API against a controlled fixture: a token server and the portal's published endpoints."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import flowengine.db.vault as vault_module
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault


class Portal(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _do(self, method):
        st = Portal.state
        parts = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode()
        st['seen'].append((method, parts.path, dict(self.headers), body))
        if parts.path == '/auth/token':
            form = parse_qs(body)
            if form.get('grant_type') != ['refresh_token'] or form.get('refresh_token') != [st['refresh']]:
                return self._send(400, {'error': 'invalid_grant'})
            st['refresh'] = st['refresh'] + '+'   # the server rotates refresh tokens
            st['access'] = f"acc{len(st['seen'])}"
            return self._send(200, {'access_token': st['access'], 'refresh_token': st['refresh'], 'expires_in': 3600})
        if self.headers.get('Authorization') != f"Bearer {st.get('access')}":
            return self._send(401, {'msg': 'unauthorised'})
        if parts.path == '/license-portal/api/v1/devices' and method == 'GET':
            return self._send(200, [{'id': 'd1', 'name': 'Laptop', 'license': {'key': 'K-1', 'product': {'name': 'Designer'}}, 'tags': ['a']}])
        if parts.path == '/license-portal/api/v1/devices' and method == 'PATCH':
            return self._send(200, {'revoked': json.loads(body)})
        return self._send(404, {'msg': 'not found'})

    def do_GET(self):
        self._do('GET')

    def do_POST(self):
        self._do('POST')

    def do_PATCH(self):
        self._do('PATCH')


class LicenseApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), Portal)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.root = f'http://127.0.0.1:{cls.http.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        Portal.state = {'refresh': 'r0', 'seen': []}
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        self.conn = ServiceConnectionStore().save({'name': 'Portal', 'kind': 'web', 'auth': 'bearer',
                                                   'base_url': f'{self.root}/license-portal/api',
                                                   'token_url': f'{self.root}/auth/token'}, secret='r0').id

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def call(self, limits={'allow_network': True}, data=None, **config):
        nodes = [{'id': 'l', 'tool': 'core.license_api', 'config': {'provider': 'alteryx-license-portal', 'connection_id': self.conn, **config}}]
        edges = []
        if data:
            nodes.insert(0, text_input('d', *data))
            edges.append(edge('d', 'output', 'l', 'data'))
        return run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)

    def test_refresh_grant_dotted_fields_and_rotation_saved(self):
        r = self.call(endpoint='v1/devices')
        self.assertEqual(r.status, 'succeeded', r.error)
        ok = r.outputs['l']['success']
        self.assertEqual(ok.schema.names, ['Call', 'id', 'name', 'license.key', 'license.product.name', 'tags'])
        self.assertEqual(next(ok.rows())[3:5], ('K-1', 'Designer'))
        store = ServiceConnectionStore()
        self.assertEqual(store.secret(store.get(self.conn)), 'r0+', 'the rotated refresh token replaces the old one')
        api_calls = [s for s in Portal.state['seen'] if s[1].startswith('/license-portal')]
        self.assertTrue(all('r0' not in json.dumps(s) for s in api_calls), 'the refresh token never reaches the API')
        self.assertEqual(self.call(endpoint='v1/devices').status, 'succeeded', 'the next run uses the rotated token')
        self.assertIn('"tags"', next(iter(r.outputs['l']['log'].rows()))[6])

    def test_patch_needs_remote_writes_and_a_bad_token_is_explained(self):
        self.assertEqual(self.call(method='PATCH', endpoint='v1/devices', payload='["d1"]').error['code'], 'permission-denied')
        r = self.call(limits={'allow_network': True, 'allow_remote_writes': True}, method='PATCH', endpoint='v1/devices', payload='["d1"]')
        self.assertEqual(next(r.outputs['l']['success'].rows())[1], '["d1"]')
        ServiceConnectionStore().save(ServiceConnectionStore().get(self.conn).public(), secret='stale')
        self.assertIn('generate new tokens', self.call(endpoint='v1/devices').error['message'])

    def test_connection_must_carry_its_token_url(self):
        conn = ServiceConnectionStore().save({'name': 'No token URL', 'kind': 'web', 'base_url': f'{self.root}/license-portal/api'}, secret='r0').id
        r = run_workflow(workflow([{'id': 'l', 'tool': 'core.license_api', 'config': {
            'provider': 'alteryx-license-portal', 'connection_id': conn, 'endpoint': 'v1/devices'}}], []),
            base_dir=ROOT, limits={'allow_network': True})
        self.assertIn('token URL', r.error['message'])


if __name__ == '__main__':
    unittest.main()
