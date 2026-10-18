"""OAuth browser sign-in and service accounts, and the Google connectors, against a controlled fixture of the documented
Google endpoints (token, Drive v3, Sheets v4, BigQuery v2, Analytics Data v1beta). No Google project, no network."""
import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import flowengine.db.vault as vault_module
import flowengine.oauth as oauth
from flowengine.executor import run_workflow
from flowengine.service import EngineService
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def b64d(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


class G:
    def __init__(self):
        self.calls, self.files, self.uploads, self.sheets, self.bq_tables, self.jobs = [], {}, {}, {'Data': [['a', 'b'], [1, True], [2.5, False]]}, {}, {}
        self.codes = {}


class Handler(BaseHTTPRequestHandler):
    g: G = None
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
        g, parts = Handler.g, urlsplit(self.path)
        path, q = unquote(parts.path), {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        g.calls.append((method, path, q, dict(self.headers), body))
        if path == '/token':
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if form['grant_type'] == 'urn:ietf:params:oauth:grant-type:jwt-bearer':
                head, claims, sig = form['assertion'].split('.')
                try:
                    KEY.public_key().verify(b64d(sig), f'{head}.{claims}'.encode(), padding.PKCS1v15(), hashes.SHA256())
                except Exception:
                    return self.send(400, {'error': 'invalid_grant', 'error_description': 'Invalid JWT Signature.'})
                c = json.loads(b64d(claims))
                if c['aud'] != f'{Handler.root}/token' or c['iss'] != 'svc@proj.iam.gserviceaccount.com' or c['exp'] - c['iat'] != 3600:
                    return self.send(400, {'error': 'invalid_grant'})
                g.jwt_claims = c
                return self.send(200, {'access_token': 'sa-token', 'expires_in': 3599, 'token_type': 'Bearer'})
            if form['grant_type'] == 'authorization_code':
                challenge = g.codes.get(form['code'])
                verifier_ok = challenge and base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'].encode()).digest()).rstrip(b'=').decode() == challenge
                if not verifier_ok or form.get('client_secret') != 'desktop-secret':
                    return self.send(400, {'error': 'invalid_grant', 'error_description': 'Bad code verifier'})
                return self.send(200, {'access_token': 'user-token', 'refresh_token': 'g-refresh', 'expires_in': 3599})
            if form['grant_type'] == 'refresh_token':
                return self.send(200, {'access_token': 'user-token', 'expires_in': 3599})
        token = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
        if token not in ('sa-token', 'user-token'):
            return self.send(401, {'error': {'code': 401, 'message': 'Request had invalid authentication credentials.'}})
        # ---- Drive
        if path == '/drive/v3/files' and method == 'GET':
            m = re.match(r"name = '((?:[^'\\]|\\.)*)'", q['q'])
            name = m.group(1).replace("\\'", "'")
            return self.send(200, {'files': [{'id': i, 'name': f['name'], 'mimeType': f['mime']} for i, f in g.files.items()
                                             if f['name'] == name]})
        if (m := re.fullmatch(r'/drive/v3/files/([^/]+)(/export)?', path)):
            f = g.files.get(m.group(1))
            if not f:
                return self.send(404, {'error': {'code': 404, 'message': 'File not found'}})
            if m.group(2):
                assert q['mimeType'].endswith('spreadsheetml.sheet')
                return self.send(200, f['export'])
            if q.get('alt') == 'media':
                return self.send(200, f['data'])
            return self.send(200, {'id': m.group(1), 'name': f['name'], 'mimeType': f['mime']})
        if path == '/upload/bigquery/v2/projects/proj/jobs' and q.get('uploadType') == 'resumable':
            sid = f's{len(g.uploads) + 1}'
            g.uploads[sid] = {'meta': json.loads(body), 'data': b'', 'size': int(self.headers['X-Upload-Content-Length']), 'fid': None}
            return self.send(200, {}, {'Location': f'{Handler.root}/upload/session/{sid}'})
        if path.startswith('/upload/drive/v3/files'):
            fid = path.rsplit('/', 1)[1] if path.count('/') > 4 else None
            if q.get('uploadType') == 'multipart':
                boundary = self.headers['Content-Type'].split('boundary=')[1].encode()
                parts_ = body.split(b'--' + boundary)
                meta = json.loads(parts_[1].split(b'\r\n\r\n', 1)[1])
                data = parts_[2].split(b'\r\n\r\n', 1)[1][:-2]
                fid = fid or f'f{len(g.files) + 1}'
                g.files[fid] = {'name': meta.get('name', g.files.get(fid, {}).get('name')), 'mime': 'text/csv', 'data': data}
                return self.send(200, {'id': fid})
            if q.get('uploadType') == 'resumable':
                sid = f's{len(g.uploads) + 1}'
                g.uploads[sid] = {'meta': json.loads(body), 'data': b'', 'size': int(self.headers['X-Upload-Content-Length']), 'fid': fid}
                return self.send(200, {}, {'Location': f'{Handler.root}/upload/session/{sid}'})
        if path.startswith('/upload/session/'):
            u = g.uploads[path.rsplit('/', 1)[1]]
            u['data'] += body
            if len(u['data']) < u['size']:
                return self.send(308, None, {'Range': f"bytes=0-{len(u['data']) - 1}"})
            if 'configuration' in u['meta']:  # BigQuery load job
                load = u['meta']['configuration']['load']
                key = load['destinationTable']['tableId']
                rows = [json.loads(line) for line in u['data'].decode().splitlines() if line]
                if any('bad' in str(r.values()) for r in rows):
                    status = {'state': 'DONE', 'errorResult': {'message': 'Error while reading data'},
                              'errors': [{'message': 'Invalid value at row 2'}]}
                else:
                    if load['writeDisposition'] == 'WRITE_TRUNCATE' or key not in g.bq_tables:
                        g.bq_tables[key] = {'schema': load['schema'], 'rows': []}
                    g.bq_tables[key]['rows'].extend(rows)
                    status = {'state': 'RUNNING'}
                jid = f'load{len(g.jobs) + 1}'
                g.jobs[jid] = {'status': status, 'polls': 0, 'rows': len(rows)}
                return self.send(200, {'jobReference': {'projectId': 'proj', 'jobId': jid, 'location': 'EU'}, 'status': status})
            fid = u['fid'] or f'f{len(g.files) + 1}'
            g.files[fid] = {'name': u['meta'].get('name'), 'mime': 'text/csv', 'data': u['data']}
            return self.send(200, {'id': fid})
        # ---- Sheets
        if (m := re.fullmatch(r'/v4/spreadsheets/ss1(?::batchUpdate)?', path)):
            if method == 'GET':
                return self.send(200, {'sheets': [{'properties': {'title': t}} for t in g.sheets]})
            for r in json.loads(body)['requests']:
                g.sheets[r['addSheet']['properties']['title']] = []
            return self.send(200, {})
        if (m := re.fullmatch(r"/v4/spreadsheets/ss1/values/'((?:[^']|'')*)'(?:!([A-Z0-9:]+))?(:clear|:append)?", path)):
            tab, cell, action = m.group(1).replace("''", "'"), m.group(2), m.group(3)
            if action == ':clear':
                g.sheets[tab] = []
                return self.send(200, {})
            if action == ':append':
                assert q['valueInputOption'] == 'RAW' and q['insertDataOption'] == 'INSERT_ROWS'
                g.sheets[tab].extend(json.loads(body)['values'])
                return self.send(200, {})
            if method == 'PUT':
                assert cell == 'A1' and q['valueInputOption'] == 'RAW'
                g.sheets[tab] = json.loads(body)['values']
                return self.send(200, {})
            values = g.sheets.get(tab, [])
            if cell == 'A1:A2':
                values = [r[:1] for r in values[:2]]
            return self.send(200, {'range': tab, 'values': values} if values else {'range': tab})
        # ---- BigQuery
        if path == '/bigquery/v2/projects/proj/queries' and method == 'POST':
            req = json.loads(body)
            assert req['useLegacySql'] is False
            return self.send(200, {'jobComplete': False, 'jobReference': {'projectId': 'proj', 'jobId': 'q1', 'location': 'EU'}})
        if path == '/bigquery/v2/projects/proj/queries/q1':
            assert q['location'] == 'EU'
            schema = {'fields': [{'name': 'id', 'type': 'INTEGER'}, {'name': 'ts', 'type': 'TIMESTAMP'},
                                 {'name': 'amount', 'type': 'NUMERIC'}, {'name': 'tags', 'type': 'STRING', 'mode': 'REPEATED'},
                                 {'name': 'addr', 'type': 'RECORD', 'fields': [{'name': 'city', 'type': 'STRING'}]}]}
            rows = [{'f': [{'v': str(i)}, {'v': f'{1767225600 + i * 3600}.0'}, {'v': f'{i}.25'},
                           {'v': [{'v': 'x'}, {'v': 'y'}]}, {'v': {'f': [{'v': f'City {i}'}]}}]} for i in range(5)]
            page = int(q.get('pageToken', '0'))
            chunk = rows[page * 2:page * 2 + 2]
            nxt = {'pageToken': str(page + 1)} if page * 2 + 2 < len(rows) else {}
            return self.send(200, {'jobComplete': True, 'schema': schema, 'rows': chunk, 'totalRows': '5', **nxt,
                                   'jobReference': {'projectId': 'proj', 'jobId': 'q1', 'location': 'EU'}})
        if (m := re.fullmatch(r'/bigquery/v2/projects/proj/jobs/(load\d+)', path)):
            job = g.jobs[m.group(1)]
            job['polls'] += 1
            if job['status']['state'] == 'RUNNING' and job['polls'] >= 2:
                job['status'] = {'state': 'DONE'}
            return self.send(200, {'jobReference': {'jobId': m.group(1)}, 'status': job['status'],
                                   'statistics': {'load': {'outputRows': str(job['rows'])}}})
        # ---- Analytics Data
        if path == '/v1beta/properties/123:runReport':
            req = json.loads(body)
            all_rows = [{'dimensionValues': [{'value': f'2026100{d}'}, {'value': c}], 'metricValues': [{'value': str(d * 10)}, {'value': '0.5'}]}
                        for d in range(1, 4) for c in ('UK', 'FR')]
            off, lim = int(req['offset']), int(req['limit'])
            return self.send(200, {'dimensionHeaders': [{'name': 'date'}, {'name': 'country'}],
                                   'metricHeaders': [{'name': 'activeUsers', 'type': 'TYPE_INTEGER'}, {'name': 'bounceRate', 'type': 'TYPE_FLOAT'}],
                                   'rows': all_rows[off:off + min(lim, 4)], 'rowCount': len(all_rows)})
        return self.send(404, {'error': {'code': 404, 'message': f'no fixture for {method} {path}'}})

    def do_GET(self):
        self.go('GET')

    def do_POST(self):
        self.go('POST')

    def do_PUT(self):
        self.go('PUT')

    def do_PATCH(self):
        self.go('PATCH')


class GoogleTests(unittest.TestCase):
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
        Handler.g = self.g = G()
        oauth._CACHE.clear()
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        self.svc = EngineService()
        self.conn = self.save_sa()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def save_sa(self):
        c = self.svc.handle('credentials.save', {'connection': {'name': 'Google SA', 'kind': 'web', 'base_url': self.root, 'auth': 'oauth2',
                                                                'token_url': f'{self.root}/token', 'scope': 'https://www.googleapis.com/auth/drive'},
                                                 'secret': json.dumps({'type': 'service_account', 'client_email': 'svc@proj.iam.gserviceaccount.com',
                                                                       'private_key': PEM, 'private_key_id': 'k1'})})
        return c['connection']['id']

    def run_tool(self, tool, config, limits=NET, data=None):
        nodes = [{'id': 'x', 'tool': tool, 'config': {'provider': 'google', 'connection_id': self.conn, **config}}]
        edges = []
        if data:
            nodes.insert(0, text_input('t', *data))
            edges.append(edge('t', 'output', 'x', 'input'))
        return run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)

    # ---------------------------------------------------------------- auth

    def test_service_account_jwt_is_signed_with_its_key(self):
        store = ServiceConnectionStore()
        self.assertIn('service_account', json.loads(store.secret(store.get(self.conn))), 'a pasted key file is kept as a key')
        self.g.files['f1'] = {'name': 'a.csv', 'mime': 'text/csv', 'data': b'x\n1\n'}
        r = self.run_tool('core.google_drive_input', {'file_id': 'f1'})
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual(self.g.jwt_claims['scope'], 'https://www.googleapis.com/auth/drive')

    def test_browser_sign_in_with_pkce_on_a_loopback_redirect(self):
        conn = self.svc.handle('credentials.save', {'connection': {
            'name': 'Google user', 'kind': 'web', 'base_url': self.root, 'auth': 'oauth2', 'token_url': f'{self.root}/token',
            'client_id': 'desktop-app', 'scope': 'https://www.googleapis.com/auth/spreadsheets',
            'authorize_url': f'{self.root}/o/oauth2/v2/auth'}, 'secret': 'desktop-secret'})['connection']['id']
        start = self.svc.handle('credentials.oauth.browser_start', {'connection_id': conn})
        params = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        self.assertEqual((params['response_type'], params['code_challenge_method'], params['client_id']), ('code', 'S256', 'desktop-app'))
        self.assertTrue(params['redirect_uri'].startswith('http://127.0.0.1:'))
        self.assertEqual(self.svc.handle('credentials.oauth.browser_poll', {'session_id': start['session_id']}), {'status': 'pending'})
        # the user's browser: wrong state first (refused), then the real redirect from the provider
        bad = urllib.request.urlopen(f"{params['redirect_uri']}?{urlencode({'code': 'c1', 'state': 'forged'})}", timeout=10).read()
        self.assertIn(b'not the one Flowbench started', bad)
        self.assertEqual(self.svc.handle('credentials.oauth.browser_poll', {'session_id': start['session_id']})['status'], 'failed')
        start = self.svc.handle('credentials.oauth.browser_start', {'connection_id': conn})
        params = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        self.g.codes['c2'] = params['code_challenge']
        page = urllib.request.urlopen(f"{params['redirect_uri']}?{urlencode({'code': 'c2', 'state': params['state']})}", timeout=10).read()
        self.assertIn(b'Signed in', page)
        self.assertEqual(self.svc.handle('credentials.oauth.browser_poll', {'session_id': start['session_id']}), {'status': 'signed_in'})
        store = ServiceConnectionStore()
        self.assertEqual(json.loads(store.secret(store.get(conn))), {'client_secret': 'desktop-secret', 'refresh_token': 'g-refresh'})

    # ---------------------------------------------------------------- Drive

    def test_drive_reads_by_name_exports_sheets_and_writes_small_and_large_files(self):
        fields, rows = [('id', 'int32'), ('city', 'v_wstring')], [['1', "Côte d'Ivoire"], ['2', 'Lagos']]
        r = self.run_tool('core.google_drive_output', {'name': "o'brien.csv", 'folder_id': 'fold1'}, WRITE, (fields, rows))
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual(self.g.files['f1']['data'], "id,city\n1,Côte d'Ivoire\n2,Lagos\n".encode())
        read = self.run_tool('core.google_drive_input', {'name': "o'brien.csv", 'read': {'fields': [{'name': 'id', 'type': 'int32'}, {'name': 'city', 'type': 'v_wstring'}]}})
        self.assertEqual(list(read.outputs['x']['output'].rows()), [(1, "Côte d'Ivoire"), (2, 'Lagos')])
        self.assertIn('fold1', [c for c in self.g.calls if c[1] == '/drive/v3/files'][0][2]['q'])
        self.assertIn('already exists', self.run_tool('core.google_drive_output', {'name': "o'brien.csv", 'if_exists': 'error'}, WRITE,
                                                     (fields, rows)).error['message'])
        # a Google Sheets file is exported as Excel and read as a worksheet
        from openpyxl import Workbook
        wb = Workbook()
        wb.active.append(['k', 'v'])
        wb.active.append(['a', 3])
        path = os.path.join(self._home.name, 'x.xlsx')
        wb.save(path)
        self.g.files['gs'] = {'name': 'Budget', 'mime': 'application/vnd.google-apps.spreadsheet', 'export': open(path, 'rb').read()}
        out = self.run_tool('core.google_drive_input', {'file_id': 'gs'}).outputs['x']['output']
        self.assertEqual(list(out.rows()), [('a', 3)])
        import flowengine.tools.google as google
        with mock.patch.object(google, 'SIMPLE_UPLOAD_MAX', 1024), mock.patch.object(google, 'RESUMABLE_CHUNK', 256 * 1024):
            big = [[str(i), 'z' * 50] for i in range(12000)]
            r = self.run_tool('core.google_drive_output', {'name': 'big.csv'}, WRITE, (fields, big))
        self.assertEqual(r.status, 'succeeded', r.error)
        data = next(f['data'] for f in self.g.files.values() if f['name'] == 'big.csv')
        self.assertEqual(data.count(b'\n'), 12001)
        puts = [c for c in self.g.calls if c[1].startswith('/upload/session/')]
        self.assertGreater(len(puts), 2, 'several 256 KiB chunks, each answered 308 until the last')

    # ---------------------------------------------------------------- Sheets

    def test_sheets_read_typed_values_and_write_overwrite_and_append(self):
        out = self.run_tool('core.google_sheets_input', {'spreadsheet_id': 'ss1', 'sheet': 'Data'}).outputs['x']['output']
        self.assertEqual([(f.name, f.type.value) for f in out.schema], [('a', 'double'), ('b', 'bool')])
        self.assertEqual(list(out.rows()), [(1.0, True), (2.5, False)])
        fields, rows = [('name', 'v_wstring'), ('n', 'int32')], [['x', '1'], ['y', '2']]
        self.assertEqual(self.run_tool('core.google_sheets_output', {'spreadsheet_id': 'ss1', 'sheet': "Q4 'final'"}, WRITE,
                                       (fields, rows)).status, 'succeeded')
        self.assertEqual(self.g.sheets["Q4 'final'"], [['name', 'n'], ['x', 1], ['y', 2]], 'tab created, header written')
        self.run_tool('core.google_sheets_output', {'spreadsheet_id': 'ss1', 'sheet': "Q4 'final'", 'mode': 'append'}, WRITE, (fields, rows[:1]))
        self.assertEqual(self.g.sheets["Q4 'final'"][-1], ['x', 1])
        self.assertEqual(len(self.g.sheets["Q4 'final'"]), 4, 'no second header when appending')
        self.assertEqual(self.run_tool('core.google_sheets_output', {'spreadsheet_id': 'ss1', 'sheet': 'Data'}, NET, (fields, rows)).error['code'],
                         'permission-denied')

    # ---------------------------------------------------------------- BigQuery

    def test_bigquery_query_waits_pages_and_types_results(self):
        r = self.run_tool('core.google_bigquery_input', {'project': 'proj', 'query': 'SELECT * FROM d.t', 'location': 'EU'})
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['x']['output']
        self.assertEqual([(f.name, f.type.value) for f in out.schema],
                         [('id', 'int64'), ('ts', 'datetime'), ('amount', 'fixed_decimal'), ('tags', 'v_wstring'), ('addr', 'v_wstring')])
        rows = list(out.rows())
        self.assertEqual(len(rows), 5, 'three pages')
        import datetime as dt
        from decimal import Decimal
        self.assertEqual(rows[1][:3], (1, dt.datetime(2026, 1, 1, 1, 0), Decimal('1.25')))
        self.assertEqual((json.loads(rows[1][3]), json.loads(rows[1][4])), (['x', 'y'], {'city': 'City 1'}))

    def test_bigquery_load_job_appends_replaces_and_reports_errors(self):
        fields = [('id', 'int64'), ('name', 'v_wstring'), ('day', 'date'), ('price', 'fixed_decimal')]
        spec = [{'name': n, 'type': t, **({'size': 12, 'scale': 2} if t == 'fixed_decimal' else {})} for n, t in fields]
        rows = [['1', 'a', '2026-01-01', '9.99'], ['2', 'b', '2026-01-02', '1.50']]
        cfg = {'project': 'proj', 'dataset': 'sales', 'table': 'orders', 'location': 'EU'}
        r = self.run_tool('core.google_bigquery_output', cfg, WRITE, (spec, rows))
        self.assertEqual(r.status, 'succeeded', r.error)
        t = self.g.bq_tables['orders']
        self.assertEqual([(f['name'], f['type']) for f in t['schema']['fields']],
                         [('id', 'INTEGER'), ('name', 'STRING'), ('day', 'DATE'), ('price', 'NUMERIC')])
        self.assertEqual(t['rows'][0], {'id': 1, 'name': 'a', 'day': '2026-01-01', 'price': '9.99'})
        self.run_tool('core.google_bigquery_output', cfg, WRITE, (spec, rows))
        self.assertEqual(len(self.g.bq_tables['orders']['rows']), 4)
        self.run_tool('core.google_bigquery_output', {**cfg, 'mode': 'replace'}, WRITE, (spec, rows[:1]))
        self.assertEqual(len(self.g.bq_tables['orders']['rows']), 1)
        bad = self.run_tool('core.google_bigquery_output', cfg, WRITE, (spec, [['3', 'bad', '2026-01-03', '1']]))
        self.assertIn('Invalid value at row 2', bad.error['message'])

    # ---------------------------------------------------------------- Analytics 4

    def test_ga4_report_pages_and_types_dates_and_metrics(self):
        r = self.run_tool('core.google_analytics4_input', {'property_id': '123', 'dimensions': ['date', 'country'],
                                                            'metrics': ['activeUsers', 'bounceRate'], 'start_date': '2026-10-01',
                                                            'end_date': '2026-10-03'})
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['x']['output']
        self.assertEqual([(f.name, f.type.value) for f in out.schema],
                         [('date', 'date'), ('country', 'v_wstring'), ('activeUsers', 'int64'), ('bounceRate', 'double')])
        import datetime as dt
        self.assertEqual(len(out), 6, 'two pages')
        self.assertEqual(next(out.rows()), (dt.date(2026, 10, 1), 'UK', 10, 0.5))


if __name__ == '__main__':
    unittest.main()
