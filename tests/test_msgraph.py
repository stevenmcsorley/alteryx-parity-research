"""OAuth and the Microsoft 365 connectors against a controlled fixture of the documented Microsoft identity platform and
Microsoft Graph endpoints (no tenant, no external network)."""
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import flowengine.db.vault as vault_module
import flowengine.oauth as oauth
from flowengine.executor import run_workflow
from flowengine.service import EngineService
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}


class Fake:
    def __init__(self):
        self.files, self.calls, self.chunks = {}, [], []
        self.list_items = {str(i): {'Title': f'Item {i}', 'Region': 'North' if i % 2 else 'South', 'Amount': i * 1.5,
                                    'Due': f'2026-0{i}-01T00:00:00Z'} for i in range(1, 6)}
        self.refresh, self.issued, self.device_polls, self.throttle_once, self.expire_once = 'rt0', 0, 0, False, False

    def token(self):
        self.issued += 1
        return f'at{self.issued}'


class Handler(BaseHTTPRequestHandler):
    fake: Fake = None
    upload_base = ''

    def log_message(self, *args):
        pass

    def send(self, code, body=None, headers=None):
        data = b'' if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        f = Handler.fake
        parts = urlsplit(self.path)
        path, q = unquote(parts.path), parse_qs(parts.query)
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        f.calls.append((method, path, q, dict(self.headers), body))
        # ---- identity platform
        if path.endswith('/oauth2/v2.0/devicecode'):
            return self.send(200, {'device_code': 'dc1', 'user_code': 'ABCD-EFGH', 'verification_uri': 'https://microsoft.com/devicelogin',
                                   'expires_in': 900, 'interval': 5, 'message': 'Go and enter ABCD-EFGH'})
        if path.endswith('/oauth2/v2.0/token'):
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if form.get('client_id') != 'app-1':
                return self.send(400, {'error': 'invalid_client'})
            if form['grant_type'] == 'client_credentials':
                ok = form.get('client_secret') == 'cs1' and form.get('scope') == 'https://graph.microsoft.com/.default'
                return self.send(200, {'access_token': f.token(), 'expires_in': 3599}) if ok else self.send(401, {'error': 'invalid_client'})
            if form['grant_type'] == 'refresh_token':
                if form.get('refresh_token') != f.refresh:
                    return self.send(400, {'error': 'invalid_grant', 'error_description': 'AADSTS70000: refresh token expired'})
                f.refresh += '+'
                return self.send(200, {'access_token': f.token(), 'refresh_token': f.refresh, 'expires_in': 3599})
            if form['grant_type'] == 'urn:ietf:params:oauth:grant-type:device_code':
                f.device_polls += 1
                if f.device_polls < 2:
                    return self.send(400, {'error': 'authorization_pending'})
                return self.send(200, {'access_token': f.token(), 'refresh_token': 'rt-device', 'expires_in': 3599})
        # ---- upload session target (pre-authenticated, outside the Graph base URL)
        if path.startswith('/upload/'):
            assert 'Authorization' not in self.headers, 'the access token must never reach the upload URL'
            name = path[len('/upload/'):]
            start, rest = self.headers['Content-Range'].split(' ')[1].split('-')
            end, total = rest.split('/')
            f.chunks.append((int(start), int(end), int(total)))
            f.files[name] = f.files.get(name, b'') + body
            done = int(end) + 1 == int(total)
            return self.send(201 if done else 202, {'id': 'new'} if done else {'nextExpectedRanges': [f'{int(end) + 1}-']})
        # ---- Graph
        token = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
        if f.expire_once and token:
            f.expire_once = False
            return self.send(401, {'error': {'code': 'InvalidAuthenticationToken', 'message': 'Access token has expired'}})
        if not token.startswith('at'):
            return self.send(401, {'error': {'code': 'InvalidAuthenticationToken', 'message': 'no token'}})
        if f.throttle_once:
            f.throttle_once = False
            return self.send(429, {'error': {'code': 'TooManyRequests', 'message': 'slow down'}}, {'Retry-After': '0'})
        g = path.removeprefix('/v1.0/')
        m = None
        import re
        if (m := re.fullmatch(r'(me/drive|users/[^/]+/drive|sites/(?:[^/]+|.+?:)/drive)/root:/(.+):/content', g)):
            name = f'{m.group(1)}|{m.group(2)}'
            if method == 'GET':
                return self.send(200, f.files[name]) if name in f.files else self.send(404, {'error': {'code': 'itemNotFound', 'message': 'The resource could not be found.'}})
            if method == 'PUT':
                if q.get('@microsoft.graph.conflictBehavior') == ['fail'] and name in f.files:
                    return self.send(409, {'error': {'code': 'nameAlreadyExists', 'message': 'exists'}})
                f.files[name] = body
                return self.send(201, {'id': 'x', 'name': name})
        if (m := re.fullmatch(r'(me/drive|users/[^/]+/drive|sites/(?:[^/]+|.+?:)/drive)/root:/(.+):/createUploadSession', g)):
            name = f'{m.group(1)}|{m.group(2)}'
            f.files.pop(name, None)
            return self.send(200, {'uploadUrl': f'{Handler.upload_base}/upload/{name}', 'expirationDateTime': '2026-12-01T00:00:00Z'})
        if (m := re.fullmatch(r'sites/(?:[^/]+|.+?:)/lists/([^/]+)/items', g)):
            if method == 'POST':
                new_id = str(max(map(int, f.list_items)) + 1)
                f.list_items[new_id] = json.loads(body)['fields']
                return self.send(201, {'id': new_id})
            ids = sorted(f.list_items, key=int)
            if q.get('$filter'):
                ids = [i for i in ids if f.list_items[i].get('Region') == q['$filter'][0].split("'")[1]]
            start = int(q.get('skip', ['0'])[0])
            page = ids[start:start + 2]
            from urllib.parse import urlencode
            others = {k: v[0] for k, v in q.items() if k != 'skip'}
            nxt = f"{Handler.graph}/{g}?{urlencode({**others, 'skip': start + 2})}" if start + 2 < len(ids) else None
            return self.send(200, {'value': [{'id': i, 'fields': {'@odata.etag': 'e', 'ContentType': 'Item', **f.list_items[i]}}
                                             for i in page], **({'@odata.nextLink': nxt} if nxt else {})})
        if (m := re.fullmatch(r'sites/(?:[^/]+|.+?:)/lists/[^/]+/items/(\d+)(/fields)?', g)):
            if method == 'PATCH':
                f.list_items[m.group(1)].update(json.loads(body))
                return self.send(200, f.list_items[m.group(1)])
            if method == 'DELETE':
                del f.list_items[m.group(1)]
                return self.send(204)
        if (m := re.fullmatch(r'(me|users/[^/]+)/mailFolders/([^/]+)/messages', g)):
            msgs = [{'id': f'm{i}', 'receivedDateTime': f'2026-10-1{i}T09:00:00Z', 'subject': f'Report {i}', 'isRead': i % 2 == 0,
                     'from': {'emailAddress': {'name': 'Ada', 'address': 'ada@example.com'}},
                     'toRecipients': [{'emailAddress': {'address': 'me@example.com'}}, {'emailAddress': {'address': 'b@example.com'}}],
                     'body': {'contentType': 'text', 'content': f'Body {i}'}, 'bodyPreview': f'Body {i}', 'hasAttachments': False,
                     'importance': 'normal', 'conversationId': 'c', 'webLink': f'https://outlook.office.com/m{i}'} for i in range(5, 0, -1)]
            return self.send(200, {'value': msgs})
        return self.send(404, {'error': {'code': 'notFound', 'message': g}})

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')

    def do_PUT(self):
        self.handle_any('PUT')

    def do_PATCH(self):
        self.handle_any('PATCH')

    def do_DELETE(self):
        self.handle_any('DELETE')


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.root = f'http://127.0.0.1:{cls.http.server_address[1]}'
        Handler.graph = f'{cls.root}/v1.0'
        Handler.upload_base = cls.root.replace('127.0.0.1', 'localhost')  # another origin than the Graph base URL

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        Handler.fake = self.fake = Fake()
        oauth._CACHE.clear()
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        self.app = self.save(scope='https://graph.microsoft.com/.default', secret=json.dumps({'client_secret': 'cs1'}))

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def save(self, secret=None, **extra):
        return ServiceConnectionStore().save({'name': f'Graph {len(self.fake.calls)}{extra.get("scope", "")[:3]}', 'kind': 'web',
                                              'base_url': f'{self.root}/v1.0', 'auth': 'oauth2', 'client_id': 'app-1',
                                              'token_url': f'{self.root}/tenant/oauth2/v2.0/token', **extra}, secret=secret).id

    def run_tool(self, tool, config, limits=NET, data=None):
        nodes = [{'id': 'x', 'tool': tool, 'config': {'provider': 'microsoft-graph', 'connection_id': self.app, **config}}]
        edges = []
        if data:
            nodes.insert(0, text_input('t', *data))
            edges.append(edge('t', 'output', 'x', 'input'))
        return run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)

    # ---------------------------------------------------------------- OAuth

    def test_device_sign_in_saves_a_refresh_token_that_rotates(self):
        svc = EngineService()
        conn = self.save(scope='Files.ReadWrite offline_access', device_code_url=f'{self.root}/tenant/oauth2/v2.0/devicecode')
        start = svc.handle('credentials.oauth.device_start', {'connection_id': conn})
        self.assertEqual((start['user_code'], start['verification_uri']), ('ABCD-EFGH', 'https://microsoft.com/devicelogin'))
        self.assertEqual(svc.handle('credentials.oauth.device_poll', {'connection_id': conn, 'device_code': 'dc1'}), {'status': 'pending'})
        self.assertEqual(svc.handle('credentials.oauth.device_poll', {'connection_id': conn, 'device_code': 'dc1'}), {'status': 'signed_in'})
        store = ServiceConnectionStore()
        self.assertEqual(json.loads(store.secret(store.get(conn))), {'refresh_token': 'rt-device'})
        self.fake.refresh = 'rt-device'
        oauth._CACHE.clear()
        self.app = conn
        self.fake.files['me/drive|a.csv'] = b'x\n1\n'
        self.assertEqual(self.run_tool('core.onedrive_input', {'path': 'a.csv'}).status, 'succeeded')
        self.assertEqual(json.loads(store.secret(store.get(conn)))['refresh_token'], 'rt-device+', 'rotated token saved')
        self.assertTrue(all('rt-device' not in json.dumps(c[3]) for c in self.fake.calls if c[1].startswith('/v1.0')))

    def test_client_secret_typed_later_joins_the_saved_sign_in(self):
        svc = EngineService()
        conn = self.save(secret=json.dumps({'refresh_token': 'rt0'}))
        c = ServiceConnectionStore().get(conn).public()
        svc.handle('credentials.save', {'connection': c, 'secret': 'cs-new'})
        store = ServiceConnectionStore()
        self.assertEqual(json.loads(store.secret(store.get(conn))), {'refresh_token': 'rt0', 'client_secret': 'cs-new'})
        self.assertNotIn('secret', json.dumps(svc.handle('credentials.list', {})).replace('client_secret', ''))

    def test_bad_refresh_token_is_explained_and_expired_access_token_is_renewed(self):
        self.app = self.save(secret=json.dumps({'refresh_token': 'stale'}))
        r = self.run_tool('core.outlook365_input', {})
        self.assertIn('AADSTS70000', r.error['message'])
        self.app = self.save(scope='https://graph.microsoft.com/.default', secret=json.dumps({'client_secret': 'cs1'}))
        self.fake.expire_once = self.fake.throttle_once = True
        self.assertEqual(self.run_tool('core.outlook365_input', {}).status, 'succeeded')
        self.assertEqual(self.fake.issued, 2, 'the token was renewed once after the 401')

    # ---------------------------------------------------------------- files

    def test_onedrive_and_sharepoint_files_round_trip_in_their_formats(self):
        fields, rows = [('id', 'int32'), ('city', 'v_wstring')], [['1', 'Zürich'], ['2', 'Lagos']]
        r = self.run_tool('core.onedrive_output', {'path': 'Reports/2026/cities.parquet'}, WRITE, (fields, rows))
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertIn('me/drive|Reports/2026/cities.parquet', self.fake.files)
        out = self.run_tool('core.onedrive_input', {'path': 'Reports/2026/cities.parquet'}).outputs['x']['output']
        self.assertEqual(list(out.rows()), [(1, 'Zürich'), (2, 'Lagos')])
        site = {'location': 'site', 'site': 'contoso.sharepoint.com:/sites/Sales:', 'path': 'Shared/cities.csv'}
        self.assertEqual(self.run_tool('core.sharepoint_files_output', site, WRITE, (fields, rows)).status, 'succeeded')
        sp = self.run_tool('core.sharepoint_files_input', {**site, 'read': {'fields': [{'name': 'id', 'type': 'int32'},
                                                                                       {'name': 'city', 'type': 'v_wstring'}]}})
        self.assertEqual(list(sp.outputs['x']['output'].rows()), [(1, 'Zürich'), (2, 'Lagos')])
        self.assertTrue(any(c[1] == '/v1.0/sites/contoso.sharepoint.com:/sites/Sales:/drive/root:/Shared/cities.csv:/content' for c in self.fake.calls))
        r = self.run_tool('core.onedrive_output', {'path': 'Reports/2026/cities.parquet', 'if_exists': 'error'}, WRITE, (fields, rows))
        self.assertIn('nameAlreadyExists', r.error['message'])
        self.assertEqual(self.run_tool('core.onedrive_output', {'path': 'x.csv'}, NET, (fields, rows)).error['code'], 'permission-denied')

    def test_large_upload_uses_a_session_without_the_access_token(self):
        import flowengine.tools.msgraph as msgraph
        rows = [[str(i), 'y' * 60] for i in range(30000)]
        with mock.patch.object(msgraph, 'SIMPLE_UPLOAD_MAX', 64 * 1024), mock.patch.object(msgraph, 'CHUNK', 320 * 1024):
            r = self.run_tool('core.onedrive_output', {'path': 'big.csv', 'location': 'user', 'user': 'ada@contoso.com'}, WRITE,
                              ([('i', 'int32'), ('pad', 'v_wstring')], rows))
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertGreater(len(self.fake.chunks), 3)
        self.assertTrue(all((e - s + 1) % (320 * 1024) == 0 for s, e, t in self.fake.chunks[:-1]), 'chunks are 320 KiB multiples')
        data = self.fake.files['users/ada@contoso.com/drive|big.csv']
        self.assertEqual(len(data), self.fake.chunks[-1][2])
        self.assertTrue(data.startswith(b'i,pad\n0,'))

    # ---------------------------------------------------------------- lists and mail

    def test_list_input_pages_and_list_output_appends_updates_and_deletes(self):
        site = {'site': 'contoso.sharepoint.com:/sites/Sales:', 'list': 'Orders'}
        out = self.run_tool('core.sharepoint_list_input', site).outputs['x']['output']
        self.assertEqual(out.schema.names, ['ItemId', 'Title', 'Region', 'Amount', 'Due'])
        self.assertEqual(len(out), 5, 'all three pages read')
        self.assertEqual(next(out.rows())[3:], (1.5, __import__('datetime').datetime(2026, 1, 1)))
        north = self.run_tool('core.sharepoint_list_input', {**site, 'filter': "fields/Region eq 'North'"}).outputs['x']['output']
        self.assertEqual([r[0] for r in north.rows()], ['1', '3', '5'])
        r = self.run_tool('core.sharepoint_list_output', site, WRITE, ([('Title', 'v_wstring'), ('Amount', 'double')], [['New', '9.5']]))
        self.assertEqual(next(r.outputs['x']['output'].rows())[-1], '6')
        self.run_tool('core.sharepoint_list_output', {**site, 'mode': 'update'}, WRITE,
                      ([('ItemId', 'v_wstring'), ('Amount', 'double')], [['2', '100']]))
        self.assertEqual(self.fake.list_items['2']['Amount'], 100.0)
        r = self.run_tool('core.sharepoint_list_output', {**site, 'mode': 'delete'}, WRITE, ([('ItemId', 'v_wstring')], [['3']]))
        self.assertIn('confirm_delete', r.error['message'])
        self.run_tool('core.sharepoint_list_output', {**site, 'mode': 'delete', 'confirm_delete': True}, WRITE, ([('ItemId', 'v_wstring')], [['3']]))
        self.assertNotIn('3', self.fake.list_items)

    def test_outlook_reads_newest_first_with_plain_text_bodies(self):
        r = self.run_tool('core.outlook365_input', {'mailbox': 'ada@contoso.com', 'filter': 'isRead eq false', 'limit': 3})
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['x']['output']
        self.assertEqual(len(out), 3)
        first = dict(zip(out.schema.names, next(out.rows())))
        self.assertEqual((first['Subject'], first['To'], first['Body']), ('Report 5', 'me@example.com; b@example.com', 'Body 5'))
        call = next(c for c in self.fake.calls if 'messages' in c[1])
        self.assertEqual(call[1], '/v1.0/users/ada@contoso.com/mailFolders/inbox/messages')
        self.assertEqual((call[2]['$filter'], call[2]['$orderby']), (['isRead eq false'], ['receivedDateTime desc']))
        self.assertEqual(call[3]['Prefer'], 'outlook.body-content-type="text"')


if __name__ == '__main__':
    unittest.main()
