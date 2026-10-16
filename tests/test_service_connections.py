"""Web/API and GitHub connections: storage, URL scoping and Download authentication."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import flowengine.db.vault as vault_module
from flowengine.errors import ConfigError
from flowengine.executor import run_workflow
from flowengine.service import EngineService
from flowengine.service_connections import ServiceConnection, ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow


class MemoryVault:
    name = 'memory'

    def __init__(self):
        self.items = {}

    def set(self, conn_id, user, secret):
        self.items[conn_id] = secret

    def get(self, conn_id):
        return self.items.get(conn_id)

    def remove(self, conn_id):
        self.items.pop(conn_id, None)


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append((self.server.server_address[1], self.path, self.headers.get('Authorization'),
                                self.headers.get('X-Api-Key')))
        if self.path.startswith('/api/hop'):
            self.send_response(302)
            self.send_header('Location', self.path.split('to=', 1)[1])
            self.end_headers()
            return
        body = json.dumps({'auth': self.headers.get('Authorization'), 'key': self.headers.get('X-Api-Key')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IsolatedHome(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name})
        self._env.start()
        self.vault = MemoryVault()
        self._vault = mock.patch.object(vault_module, '_VAULT', self.vault)
        self._vault.start()
        self.store = ServiceConnectionStore()

    def tearDown(self):
        self._vault.stop()
        self._env.stop()
        self._home.cleanup()


class StoreTests(IsolatedHome):
    def test_secrets_stay_in_the_vault(self):
        with self.assertRaisesRegex(ConfigError, 'needs its token'):
            self.store.save({'name': 'API', 'kind': 'web', 'base_url': 'https://api.example.com/v1'})
        c = self.store.save({'name': 'API', 'kind': 'web', 'base_url': 'https://api.example.com/v1'}, secret='tok-123')
        gh = self.store.save({'name': 'GitHub', 'kind': 'github'}, secret='ghp_abc')
        self.assertEqual(gh.base_url, 'https://api.github.com')
        on_disk = Path(self._home.name, 'service_connections.json').read_text(encoding='utf-8')
        self.assertNotIn('tok-123', on_disk)
        self.assertEqual(self.vault.get(c.id), 'tok-123')
        self.store.save({**c.public(), 'name': 'API v1'})  # editing without a secret keeps the saved one
        self.assertEqual(self.store.secret(self.store.get(c.id)), 'tok-123')
        self.assertEqual([x.id for x in self.store.list('github')], [gh.id])
        self.store.delete(c.id)
        self.assertIsNone(self.vault.get(c.id))
        with self.assertRaisesRegex(ConfigError, 'secrets are passed separately'):
            self.store.save({'name': 'x', 'kind': 'web', 'base_url': 'https://x.test', 'token': 'leak'}, secret='s')

    def test_validation(self):
        for data, message in [({'base_url': 'http://api.example.com'}, 'only for localhost'),
                              ({'base_url': 'https://u:p@api.example.com'}, 'without credentials'),
                              ({'base_url': 'https://api.example.com', 'auth': 'basic'}, 'needs a username'),
                              ({'base_url': 'https://api.example.com', 'auth': 'header', 'header_name': 'Bad Name'}, 'header name'),
                              ({'base_url': 'https://api.example.com', 'kind': 'github', 'auth': 'basic', 'username': 'u'}, 'GitHub')]:
            with self.subTest(message=message), self.assertRaisesRegex(ConfigError, message):
                self.store.save({'name': 'x', 'kind': 'web', **data}, secret='s')

    def test_rpc_never_returns_secrets(self):
        service = EngineService()
        saved = service.handle('credentials.save', {'connection': {'name': 'API', 'kind': 'web', 'base_url': 'https://a.test'},
                                                    'secret': 'very-secret'})
        self.assertTrue(saved['credential_saved'])
        listed = service.handle('credentials.list', {})
        self.assertNotIn('very-secret', json.dumps([saved, listed]))
        self.assertEqual(service.handle('credentials.delete', {'connection_id': listed[0]['id']}), {'deleted': True})


class ScopeTests(unittest.TestCase):
    def test_credentials_only_apply_under_the_base_url(self):
        c = ServiceConnection('svc_a', 'A', 'web', 'https://api.example.com/v1')
        for url, ok in [('https://api.example.com/v1', True), ('https://api.example.com/v1/items?x=1', True),
                        ('https://API.example.com:443/v1/x', True), ('https://api.example.com/v10', False),
                        ('https://api.example.com/v1/../admin', False), ('https://api.example.com/v1/%2e%2e/admin', False),
                        ('http://api.example.com/v1/x', False), ('https://api.example.com:8443/v1/x', False),
                        ('https://evil.example.com/v1/x', False)]:
            with self.subTest(url=url):
                self.assertEqual(c.covers(url), ok)
        self.assertEqual(c.authorize('https://api.example.com/v1/x', 't'), {'Authorization': 'Bearer t'})
        basic = ServiceConnection('svc_b', 'B', 'web', 'https://h.test', auth='basic', username='ann')
        self.assertEqual(basic.authorize('https://h.test/x', 'pw'), {'Authorization': 'Basic YW5uOnB3'})
        with self.assertRaisesRegex(ConfigError, 'outside the base URL'):
            c.authorize('https://evil.example.com/v1', 't')


class DownloadAuthTests(IsolatedHome):
    @classmethod
    def setUpClass(cls):
        cls.servers = [ThreadingHTTPServer(('127.0.0.1', 0), _Handler) for _ in range(2)]
        for server in cls.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.api, cls.other = (f'http://127.0.0.1:{s.server_address[1]}' for s in cls.servers)

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.shutdown()
            server.server_close()

    def download(self, urls, conn_id, **config):
        doc = workflow([text_input('t', [('url', 'v_wstring')], [[u] for u in urls]),
                        {'id': 'dl', 'tool': 'core.download',
                         'config': {'url_field': 'url', 'connection_id': conn_id, 'on_error': 'record', **config}}],
                       [edge('t', 'output', 'dl', 'input')])
        return run_workflow(doc, base_dir=ROOT, limits={'allow_network': True})

    def test_token_is_sent_only_inside_the_base_url(self):
        _Handler.seen.clear()
        c = self.store.save({'name': 'Local API', 'kind': 'web', 'base_url': self.api + '/api'}, secret='tok-xyz')
        result = self.download([self.api + '/api/items', self.other + '/api/items', self.api + '/private',
                                self.api + f'/api/hop?to={self.api}/api/next',
                                self.api + f'/api/hop?to={self.other}/api/stolen'], c.id)
        self.assertTrue(result.ok, result.error)
        out = result.output('dl')
        data, errors = out.column('DownloadData'), out.column('DownloadError')
        self.assertEqual(json.loads(data[0])['auth'], 'Bearer tok-xyz')
        self.assertIn('outside the base URL', errors[1])
        self.assertIn('outside the base URL', errors[2])
        self.assertEqual(json.loads(data[3])['auth'], 'Bearer tok-xyz')  # redirect within scope keeps auth
        self.assertIn("leaves the connection's base URL", errors[4])
        other_port = self.servers[1].server_address[1]
        self.assertEqual([s for s in _Handler.seen if s[0] == other_port], [])
        self.assertNotIn('tok-xyz', json.dumps(result.events))

    def test_header_auth_and_conflicting_headers(self):
        c = self.store.save({'name': 'Key API', 'kind': 'web', 'base_url': self.api, 'auth': 'header',
                             'header_name': 'X-Api-Key'}, secret='k-1')
        result = self.download([self.api + '/anything'], c.id)
        self.assertEqual(json.loads(result.output('dl').column('DownloadData')[0])['key'], 'k-1')
        result = self.download([self.api + '/anything'], c.id, headers=[{'name': 'x-api-key', 'value': 'mine'}])
        self.assertEqual(result.status, 'failed')
        self.assertIn('would replace the authentication', result.error['message'])
        result = self.download([self.api + '/anything'], 'svc_missing')
        self.assertIn('Unknown service connection', result.error['message'])


if __name__ == '__main__':
    unittest.main()
