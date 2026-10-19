"""DataRobot AutoModel/Predict and Adobe Analytics against controlled fixtures of their documented REST APIs."""
import csv
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import flowengine.db.vault as vault_module
import flowengine.oauth as oauth
import flowengine.tools.datarobot_adobe as dra
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}


class St:
    def __init__(self):
        self.calls, self.polls, self.datasets, self.projects, self.pred_rows = [], {}, {}, {}, []


class H(BaseHTTPRequestHandler):
    st: St = None
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

    def _status(self, sid, done_url):
        """An async status: RUNNING once, then 303 to the result (as DataRobot does)."""
        self.st.polls[sid] = done_url
        return f'{H.root}/api/v2/status/{sid}/'

    def handle_any(self, method):
        st, parts = H.st, urlsplit(self.path)
        path, q = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        st.calls.append((method, path, dict(self.headers), body))
        if path == '/ims/token':
            return self.send(200, {'access_token': 'adobe-token', 'expires_in': 86399})
        if path.startswith('/api/v2/'):
            if self.headers.get('Authorization') != 'Bearer dr-key':
                return self.send(401, {'message': 'Invalid API key'})
            if (sid := path[len('/api/v2/status/'):-1]) and path.startswith('/api/v2/status/'):
                seen = st.polls.setdefault(f'seen-{sid}', 0)
                st.polls[f'seen-{sid}'] = seen + 1
                if seen == 0:
                    return self.send(200, {'status': 'RUNNING', 'statusId': sid})
                return self.send(303, None, {'Location': st.polls[sid]})
            if path == '/api/v2/datasets/fromFile/':
                data = body.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0]
                st.datasets['ds1'] = list(csv.DictReader(io.StringIO(data.decode())))
                return self.send(202, {'catalogId': 'ds1', 'catalogVersionId': 'v1', 'statusId': 's1'},
                                 {'Location': self._status('s1', f'{H.root}/api/v2/datasets/ds1/')})
            if path == '/api/v2/projects/' and method == 'POST':
                doc = json.loads(body)
                st.projects['p1'] = {'name': doc['projectName'], 'dataset': doc['datasetId']}
                return self.send(202, {'pid': 'p1'}, {'Location': self._status('s2', f'{H.root}/api/v2/projects/p1/')})
            if path == '/api/v2/projects/p1/' and method == 'PATCH':
                st.projects['p1'].update(json.loads(body))
                return self.send(204)
            if path == '/api/v2/projects/p1/aim/':
                st.projects['p1'].update(json.loads(body))
                return self.send(202, None, {'Location': self._status('s3', f'{H.root}/api/v2/projects/p1/')})
            if path == '/api/v2/projects/p1/recommendedModels/recommendedModel/':
                return self.send(200, {'modelId': 'm-best', 'recommendationType': 'RECOMMENDED_FOR_DEPLOYMENT'})
            if path == '/api/v2/projects/p1/predictionDatasets/fileUploads/':
                data = body.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0]
                st.pred_rows = list(csv.DictReader(io.StringIO(data.decode())))
                return self.send(202, None, {'Location': self._status('s4', f'{H.root}/api/v2/projects/p1/predictionDatasets/pd1/')})
            if path == '/api/v2/projects/p1/predictionDatasets/pd1/':
                return self.send(200, {'id': 'pd1', 'numRows': len(st.pred_rows)})
            if path == '/api/v2/projects/p1/predictions/' and method == 'POST':
                assert json.loads(body) == {'modelId': 'm-best', 'datasetId': 'pd1'}
                return self.send(202, None, {'Location': self._status('s5', f'{H.root}/api/v2/projects/p1/predictions/pr1/')})
            if path == '/api/v2/projects/p1/predictions/pr1/':
                data = [{'rowId': i, 'prediction': 'yes' if float(r['tenure']) < 12 else 'no',
                         'predictionValues': [{'label': 'yes', 'value': 0.8 if float(r['tenure']) < 12 else 0.1},
                                              {'label': 'no', 'value': 0.2 if float(r['tenure']) < 12 else 0.9}]}
                        for i, r in enumerate(st.pred_rows)]
                return self.send(200, {'task': 'Binary', 'data': data})
            return self.send(200, {})
        if path == '/api/ACME1/reports':
            assert self.headers.get('Authorization') == 'Bearer adobe-token' and self.headers.get('x-api-key') == 'adobe-client'
            doc = json.loads(body)
            assert doc['globalFilters'][0] == {'type': 'dateRange', 'dateRange': '2026-09-01T00:00:00.000/2026-10-01T00:00:00.000'}
            if 'dimension' not in doc:
                return self.send(200, {'summaryData': {'totals': [1500, 320]}, 'rows': []})
            pages = [[('1', 'home', [900, 200]), ('2', 'pricing', [400, 80])], [('3', 'docs', [200, 40])]]
            page = doc['settings']['page']
            rows = [{'itemId': i, 'value': v, 'data': d} for i, v, d in pages[page]]
            return self.send(200, {'rows': rows, 'totalPages': 2, 'number': page, 'lastPage': page == 1})
        return self.send(404, {'message': path})

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')

    def do_PATCH(self):
        self.handle_any('PATCH')


class Tests(unittest.TestCase):
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
                         mock.patch.object(vault_module, '_VAULT', MemoryVault()), mock.patch.object(dra, 'POLL_SECONDS', 0)]
        for p in self._patches:
            p.start()
        store = ServiceConnectionStore()
        self.dr = store.save({'name': 'DataRobot', 'kind': 'web', 'base_url': f'{self.root}/api/v2', 'auth': 'bearer'}, secret='dr-key').id
        self.adobe = store.save({'name': 'Adobe', 'kind': 'web', 'base_url': f'{self.root}/api/ACME1', 'auth': 'oauth2', 'client_id': 'adobe-client',
                                 'scope': 'openid,AdobeID,additional_info.projectedProductContext', 'token_url': f'{self.root}/ims/token'},
                                secret=json.dumps({'client_secret': 'cs'})).id

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
        r = run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)
        self.assertEqual(r.status, 'succeeded', r.error)
        t = r.outputs['x']['output']
        return [dict(zip(t.schema.names, row)) for row in t.rows()]

    def test_automodel_uploads_creates_the_project_and_starts_autopilot(self):
        data = ([('tenure', 'int32'), ('churn', 'v_wstring')], [[str(i), 'yes' if i < 12 else 'no'] for i in range(1, 31)])
        out = self.run_tool('core.datarobot_automodel', {'provider': 'datarobot', 'connection_id': self.dr, 'project_name': 'Churn',
                                                          'target': 'churn', 'mode': 'quick', 'worker_count': 4}, data, WRITE)
        self.assertEqual((out[0]['ProjectId'], out[0]['ProjectUrl']), ('p1', f'{self.root}/projects/p1/models'))
        self.assertEqual(len(self.st.datasets['ds1']), 30)
        self.assertEqual({k: self.st.projects['p1'][k] for k in ('name', 'dataset', 'target', 'mode', 'workerCount')},
                         {'name': 'Churn', 'dataset': 'ds1', 'target': 'churn', 'mode': 'quick', 'workerCount': 4})
        statuses = [c for c in self.st.calls if c[1].startswith('/api/v2/status/')]
        self.assertEqual(len(statuses), 6, 'three async steps, each polled until it redirected')
        denied = run_workflow(workflow([text_input('t', *data), {'id': 'x', 'tool': 'core.datarobot_automodel', 'config': {
            'provider': 'datarobot', 'connection_id': self.dr, 'project_name': 'C', 'target': 'churn'}}], [edge('t', 'output', 'x', 'input')]),
            base_dir=ROOT, limits=NET)
        self.assertEqual(denied.error['code'], 'permission-denied')

    def test_predict_uses_the_recommended_model_and_joins_by_row(self):
        out = self.run_tool('core.datarobot_predict', {'provider': 'datarobot', 'connection_id': self.dr, 'project_id': 'p1'},
                            ([('id', 'int32'), ('tenure', 'int32')], [['1', '3'], ['2', '40']]))
        self.assertEqual([(o['id'], o['Prediction'], o['Probability_yes']) for o in out], [(1, 'yes', 0.8), (2, 'no', 0.1)])

    def test_adobe_report_pages_through_a_dimension_and_reads_totals(self):
        cfg = {'provider': 'adobe-analytics', 'connection_id': self.adobe, 'report_suite': 'acme.prod', 'metrics': ['metrics/pageviews', 'metrics/visits'],
               'start': '2026-09-01', 'end': '2026-10-01'}
        rows = self.run_tool('core.adobe_analytics', {**cfg, 'dimension': 'variables/page'})
        self.assertEqual([(r['page'], r['pageviews'], r['visits']) for r in rows], [('home', 900, 200), ('pricing', 400, 80), ('docs', 200, 40)])
        self.assertEqual(rows[0]['ItemId'], '1')
        totals = self.run_tool('core.adobe_analytics', cfg)
        self.assertEqual(totals, [{'pageviews': 1500, 'visits': 320}])


if __name__ == '__main__':
    unittest.main()
