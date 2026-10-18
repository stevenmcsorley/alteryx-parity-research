"""Box, Dropbox and Azure Data Lake Storage Gen2 against controlled fixtures of their documented REST APIs."""
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import flowengine.db.vault as vault_module
import flowengine.oauth as oauth
import flowengine.tools.filestores as fs
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}
FIELDS, ROWS = [('id', 'int32'), ('city', 'v_wstring')], [['1', 'Zürich'], ['2', 'Lagos']]
READ = {'fields': [{'name': 'id', 'type': 'int32'}, {'name': 'city', 'type': 'v_wstring'}]}


class St:
    def __init__(self):
        self.calls, self.box = [], {'f1': {'name': 'sales.csv', 'parent': '0', 'data': b'id,city\n1,Oslo\n'}}
        self.dbx, self.dbx_sessions, self.lake = {}, {}, {}


class H(BaseHTTPRequestHandler):
    st: St = None
    root = ''

    def log_message(self, *a):
        pass

    def send(self, code, body=None, headers=None):
        data = b'' if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def go(self, method):
        st, parts = H.st, urlsplit(self.path)
        path, q = unquote(parts.path), {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        st.calls.append((method, path, q, dict(self.headers), body))
        if path == '/token':
            return self.send(200, {'access_token': 'tok', 'expires_in': 3600})
        if path.startswith('/signed/'):  # a pre-signed download link: must arrive without the token
            if 'Authorization' in self.headers:
                return self.send(400, {'error': 'token sent to the pre-signed link'})
            return self.send(200, st.box[path.rsplit('/', 1)[1]]['data'])
        if self.headers.get('Authorization') != 'Bearer tok':
            return self.send(401, {'error': 'unauthorised'})
        # ---- Box
        if (m := re.fullmatch(r'/box/2.0/folders/(\w+)/items', path)):
            entries = [{'type': 'file', 'id': i, 'name': f['name']} for i, f in st.box.items() if f['parent'] == m.group(1)]
            entries = [{'type': 'folder', 'id': 'd9', 'name': 'Archive'}] + entries
            off, lim = int(q['offset']), 2
            return self.send(200, {'entries': entries[off:off + lim], 'total_count': len(entries), 'offset': off})
        if (m := re.fullmatch(r'/box/2.0/files/(\w+)', path)):
            return self.send(200, {'id': m.group(1), 'name': st.box[m.group(1)]['name']})
        if (m := re.fullmatch(r'/box/2.0/files/(\w+)/content', path)):
            return self.send(302, None, {'Location': f'{H.root}/signed/{m.group(1)}'})
        if (m := re.fullmatch(r'/boxupload/2.0/files(?:/(\w+))?/content', path)):
            boundary = self.headers['Content-Type'].split('boundary=')[1].encode()
            pieces = body.split(b'--' + boundary)
            attrs = json.loads(pieces[1].split(b'\r\n\r\n', 1)[1].rstrip(b'\r\n'))
            data = pieces[2].split(b'\r\n\r\n', 1)[1][:-2]
            fid = m.group(1) or f'f{len(st.box) + 1}'
            st.box[fid] = {'name': attrs['name'], 'parent': attrs.get('parent', {}).get('id', st.box.get(fid, {}).get('parent')), 'data': data}
            return self.send(201, {'entries': [{'id': fid}]})
        # ---- Dropbox
        arg = json.loads(self.headers.get('Dropbox-API-Arg') or '{}')
        if path == '/dropboxcontent/2/files/download':
            if arg['path'] not in st.dbx:
                return self.send(409, {'error_summary': 'path/not_found/..', 'error': {'.tag': 'path'}})
            return self.send(200, st.dbx[arg['path']], {'Dropbox-API-Result': json.dumps({'name': arg['path']})})
        if path == '/dropboxcontent/2/files/upload':
            if arg['mode'] == 'add' and arg['path'] in st.dbx:
                return self.send(409, {'error_summary': 'path/conflict/file/...', 'error': {'.tag': 'path'}})
            st.dbx[arg['path']] = body
            return self.send(200, {'name': arg['path']})
        if path == '/dropboxcontent/2/files/upload_session/start':
            sid = f's{len(st.dbx_sessions)}'
            st.dbx_sessions[sid] = body
            return self.send(200, {'session_id': sid})
        if path == '/dropboxcontent/2/files/upload_session/append_v2':
            c = arg['cursor']
            assert len(st.dbx_sessions[c['session_id']]) == c['offset']
            st.dbx_sessions[c['session_id']] += body
            return self.send(200, None)
        if path == '/dropboxcontent/2/files/upload_session/finish':
            c = arg['cursor']
            assert len(st.dbx_sessions[c['session_id']]) == c['offset']
            st.dbx[arg['commit']['path']] = st.dbx_sessions.pop(c['session_id']) + body
            return self.send(200, {'name': arg['commit']['path']})
        # ---- ADLS Gen2
        if path.startswith('/lake/'):
            assert self.headers.get('x-ms-version') == '2023-11-03'
            key = path[len('/lake/'):]
            if method == 'GET':
                return self.send(200, st.lake[key]['data']) if key in st.lake and st.lake[key]['flushed'] else \
                    self.send(404, {'error': {'code': 'PathNotFound', 'message': 'The specified path does not exist.'}})
            if method == 'PUT' and q.get('resource') == 'file':
                if self.headers.get('If-None-Match') == '*' and key in st.lake:
                    return self.send(409, {'error': {'code': 'PathAlreadyExists', 'message': 'The specified path already exists.'}})
                st.lake[key] = {'data': b'', 'pending': b'', 'flushed': False}
                return self.send(201)
            if method == 'PATCH' and q.get('action') == 'append':
                f = st.lake[key]
                assert int(q['position']) == len(f['pending'])
                f['pending'] += body
                return self.send(202)
            if method == 'PATCH' and q.get('action') == 'flush':
                f = st.lake[key]
                assert int(q['position']) == len(f['pending'])
                f['data'], f['flushed'] = f['pending'], True
                return self.send(200)
        return self.send(404, {'error': {'code': 'NotFound', 'message': path}})

    def do_GET(self):
        self.go('GET')

    def do_POST(self):
        self.go('POST')

    def do_PUT(self):
        self.go('PUT')

    def do_PATCH(self):
        self.go('PATCH')


class FileStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.root = H.root = f'http://127.0.0.1:{cls.http.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        H.st = self.st = St()
        oauth._CACHE.clear()
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault()),
                         # the vendors' fixed content hosts, pointed at the fixture
                         mock.patch.object(fs.Box, 'upload_base', f'{self.root}/boxupload/2.0'),
                         mock.patch.object(fs.Box, 'extra_origins', (f'{self.root}/boxupload/2.0',)),
                         mock.patch.object(fs.Dropbox, 'content_base', f'{self.root}/dropboxcontent/2'),
                         mock.patch.object(fs.Dropbox, 'extra_origins', (f'{self.root}/dropboxcontent/2',))]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def conn(self, base):
        return ServiceConnectionStore().save({'name': base, 'kind': 'web', 'base_url': f'{self.root}{base}', 'auth': 'oauth2',
                                              'client_id': 'c', 'token_url': f'{self.root}/token'},
                                             secret=json.dumps({'client_secret': 's'})).id

    def run_tool(self, tool, config, limits=NET, data=None):
        nodes = [{'id': 'x', 'tool': tool, 'config': config}]
        edges = []
        if data:
            nodes.insert(0, text_input('t', *data))
            edges.append(edge('t', 'output', 'x', 'input'))
        return run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)

    def test_box_finds_by_name_downloads_without_the_token_and_uploads_versions(self):
        c = {'provider': 'box', 'connection_id': self.conn('/box/2.0')}
        r = self.run_tool('core.box_input', {**c, 'name': 'sales.csv', 'read': READ})
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual(list(r.outputs['x']['output'].rows()), [(1, 'Oslo')])
        signed = [x for x in self.st.calls if x[1].startswith('/signed/')]
        self.assertEqual(len(signed), 1)
        self.assertNotIn('Authorization', signed[0][3], 'the pre-signed link never sees the token')
        self.assertEqual(self.run_tool('core.box_output', {**c, 'name': 'new.csv'}, WRITE, (FIELDS, ROWS)).status, 'succeeded')
        new = next(f for f in self.st.box.values() if f['name'] == 'new.csv')
        self.assertEqual((new['parent'], new['data']), ('0', 'id,city\n1,Zürich\n2,Lagos\n'.encode()))
        self.run_tool('core.box_output', {**c, 'name': 'sales.csv'}, WRITE, (FIELDS, ROWS[:1]))
        self.assertEqual(self.st.box['f1']['data'], 'id,city\n1,Zürich\n'.encode(), 'an existing file gets a new version')
        self.assertTrue(any(x[1] == '/boxupload/2.0/files/f1/content' for x in self.st.calls))
        self.assertIn('already exists', self.run_tool('core.box_output', {**c, 'name': 'sales.csv', 'if_exists': 'error'}, WRITE,
                                                     (FIELDS, ROWS)).error['message'])

    def test_dropbox_round_trip_escapes_unicode_paths_and_uses_sessions_for_big_files(self):
        c = {'provider': 'dropbox', 'connection_id': self.conn('/dropboxapi/2')}
        r = self.run_tool('core.dropbox_output', {**c, 'path': '/Rapports/été.csv'}, WRITE, (FIELDS, ROWS))
        self.assertEqual(r.status, 'succeeded', r.error)
        sent = next(x for x in self.st.calls if x[1].endswith('/files/upload'))[3]
        header = {k.lower(): v for k, v in sent.items()}['dropbox-api-arg']  # header names are case-insensitive
        self.assertTrue(header.isascii() and '\\u00e9' in header, 'non-ASCII path escaped in the JSON header')
        back = self.run_tool('core.dropbox_input', {**c, 'path': '/Rapports/été.csv', 'read': READ})
        self.assertEqual(list(back.outputs['x']['output'].rows()), [(1, 'Zürich'), (2, 'Lagos')])
        self.assertIn('already exists', self.run_tool('core.dropbox_output', {**c, 'path': '/Rapports/été.csv', 'if_exists': 'error'},
                                                     WRITE, (FIELDS, ROWS)).error['message'])
        self.assertIn('not_found', self.run_tool('core.dropbox_input', {**c, 'path': '/nope.csv'}).error['message'])
        with mock.patch.object(fs, 'CHUNK', 64 * 1024):
            big = [[str(i), 'q' * 40] for i in range(9000)]
            self.assertEqual(self.run_tool('core.dropbox_output', {**c, 'path': '/big.csv'}, WRITE, (FIELDS, big)).status, 'succeeded')
        self.assertEqual(self.st.dbx['/big.csv'].count(b'\n'), 9001)
        self.assertGreater(len([x for x in self.st.calls if x[1].endswith('append_v2')]), 3)

    def test_data_lake_creates_appends_flushes_and_reads(self):
        c = {'provider': 'azure-data-lake', 'connection_id': self.conn('/lake'), 'file_system': 'raw-data'}
        with mock.patch.object(fs, 'CHUNK', 16):
            r = self.run_tool('core.azure_data_lake_output', {**c, 'path': 'sales/2026/q1.csv'}, WRITE, (FIELDS, ROWS))
        self.assertEqual(r.status, 'succeeded', r.error)
        appends = [x for x in self.st.calls if x[2].get('action') == 'append']
        self.assertGreater(len(appends), 1, 'appended in chunks at increasing positions')
        back = self.run_tool('core.azure_data_lake_input', {**c, 'path': 'sales/2026/q1.csv', 'read': READ})
        self.assertEqual(list(back.outputs['x']['output'].rows()), [(1, 'Zürich'), (2, 'Lagos')])
        self.assertIn('already exists', self.run_tool('core.azure_data_lake_output', {**c, 'path': 'sales/2026/q1.csv', 'if_exists': 'error'},
                                                     WRITE, (FIELDS, ROWS)).error['message'])
        self.assertIn('PathNotFound', self.run_tool('core.azure_data_lake_input', {**c, 'path': 'missing.csv'}).error['message'])
        self.assertIn('valid file system', self.run_tool('core.azure_data_lake_input', {**c, 'file_system': 'Bad_Name', 'path': 'a.csv'}).error['message'])
        self.assertEqual(self.run_tool('core.azure_data_lake_output', {**c, 'path': 'x.csv'}, NET, (FIELDS, ROWS)).error['code'], 'permission-denied')


if __name__ == '__main__':
    unittest.main()
