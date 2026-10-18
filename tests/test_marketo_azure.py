"""Marketo, Azure AI Language and Power Automate against controlled fixtures of their documented APIs."""
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
import flowengine.tools.marketo as marketo
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}


class St:
    def __init__(self):
        self.calls, self.tokens, self.expire_once, self.rate_once = [], 0, False, False
        self.leads = {i: {'id': i, 'email': f'p{i}@example.com', 'firstName': f'P{i}', 'updatedAt': '2026-10-0%dT09:00:00Z' % (i % 9 + 1)}
                      for i in range(1, 8)}
        self.flow_calls = []


class H(BaseHTTPRequestHandler):
    st: St = None

    def log_message(self, *a):
        pass

    def send(self, code, body=None):
        data = b'' if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def go(self, method):
        st, parts = H.st, urlsplit(self.path)
        path, q = unquote(parts.path), {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        st.calls.append((method, path, q, dict(self.headers), body))
        if path == '/identity/oauth/token':
            assert method == 'GET' and q == {'grant_type': 'client_credentials', 'client_id': 'mk-id', 'client_secret': 'mk-secret'}
            st.tokens += 1
            return self.send(200, {'access_token': f'mt{st.tokens}', 'token_type': 'bearer', 'expires_in': 3599})
        if path.startswith('/rest/'):
            assert 'access_token' not in q, 'the token goes in the Authorization header only'
            if st.expire_once:
                st.expire_once = False
                return self.send(200, {'requestId': 'r', 'success': False, 'errors': [{'code': '602', 'message': 'Access token expired'}]})
            if st.rate_once:
                st.rate_once = False
                return self.send(200, {'requestId': 'r', 'success': False, 'errors': [{'code': '606', 'message': 'Max rate limit exceeded'}]})
            if self.headers.get('Authorization') != f'Bearer mt{st.tokens}':
                return self.send(200, {'success': False, 'errors': [{'code': '601', 'message': 'Access token invalid'}]})
            if path == '/rest/v1/leads.json' and method == 'GET':
                want = q['filterValues'].split(',')
                assert len(want) <= 300
                found = [l for l in st.leads.values() if str(l[q['filterType']]) in want]
                start = int(q.get('nextPageToken', '0'))
                page = found[start:start + 2]
                more = start + 2 < len(found)
                return self.send(200, {'success': True, 'result': page, 'moreResult': more, **({'nextPageToken': str(start + 2)} if more else {})})
            if path == '/rest/v1/leads.json' and method == 'POST':
                doc = json.loads(body)
                assert len(doc['input']) <= 300
                result = []
                for lead in doc['input']:
                    match = next((l for l in st.leads.values() if l['email'] == lead.get('email')), None)
                    if match and doc['action'] == 'createOnly':
                        result.append({'status': 'skipped', 'reasons': [{'code': '1005', 'message': 'Lead already exists'}]})
                    elif match:
                        match.update(lead)
                        result.append({'id': match['id'], 'status': 'updated'})
                    else:
                        new = max(st.leads) + 1
                        st.leads[new] = {'id': new, **lead}
                        result.append({'id': new, 'status': 'created'})
                return self.send(200, {'success': True, 'result': result})
            if path == '/rest/v1/activities/pagingtoken.json':
                return self.send(200, {'success': True, 'nextPageToken': 'T0'})
            if path == '/rest/v1/activities.json':
                assert q['activityTypeIds'] == '1,12'
                if q['nextPageToken'] == 'T0':
                    return self.send(200, {'success': True, 'moreResult': True, 'nextPageToken': 'T1', 'result': [
                        {'id': 101, 'leadId': 1, 'activityDate': '2026-10-02T10:00:00Z', 'activityTypeId': 1,
                         'attributes': [{'name': 'Webpage URL', 'value': '/pricing'}]}]})
                return self.send(200, {'success': True, 'moreResult': False, 'nextPageToken': 'T2', 'result': [
                    {'id': 102, 'leadId': 2, 'activityDate': '2026-10-03T11:00:00Z', 'activityTypeId': 12, 'attributes': []}]})
        if path == '/language/:analyze-text':
            assert self.headers.get('Ocp-Apim-Subscription-Key') == 'lang-key' and q['api-version'] == '2023-04-01'
            doc = json.loads(body)
            docs = doc['analysisInput']['documents']
            assert len(docs) <= {'SentimentAnalysis': 10, 'KeyPhraseExtraction': 10, 'EntityRecognition': 5, 'LanguageDetection': 1000}[doc['kind']]
            out, errs = [], []
            for d in docs:
                if 'BOOM' in d['text']:
                    errs.append({'id': d['id'], 'error': {'code': 'InvalidArgument', 'message': 'Invalid document'}})
                elif doc['kind'] == 'SentimentAnalysis':
                    pos = 'love' in d['text']
                    out.append({'id': d['id'], 'sentiment': 'positive' if pos else 'negative',
                                'confidenceScores': {'positive': 0.9 if pos else 0.1, 'neutral': 0.0, 'negative': 0.1 if pos else 0.9}})
                elif doc['kind'] == 'KeyPhraseExtraction':
                    out.append({'id': d['id'], 'keyPhrases': [w for w in d['text'].split() if len(w) > 4]})
                elif doc['kind'] == 'EntityRecognition':
                    out.append({'id': d['id'], 'entities': [{'text': 'Paris', 'category': 'Location', 'offset': 0, 'length': 5,
                                                             'confidenceScore': 0.99}] if 'Paris' in d['text'] else []})
                else:
                    out.append({'id': d['id'], 'detectedLanguage': {'name': 'French' if 'Bonjour' in d['text'] else 'English',
                                                                    'iso6391Name': 'fr' if 'Bonjour' in d['text'] else 'en', 'confidenceScore': 1.0}})
            return self.send(200, {'kind': doc['kind'] + 'Results', 'results': {'documents': out, 'errors': errs}})
        if path == '/workflows/abc/triggers/manual/paths/invoke':
            if q.get('sig') != 'SIGNED':
                return self.send(401, {'error': {'code': 'DirectApiAuthorizationRequired', 'message': 'bad signature'}})
            payload = json.loads(body)
            st.flow_calls.append(payload)
            if isinstance(payload, dict) and payload.get('n') == 3:
                return self.send(400, {'error': {'code': 'InvalidTemplate', 'message': 'Flow rejected n=3'}})
            return self.send(202, None)
        return self.send(404, {'error': {'code': 'NotFound', 'message': path}})

    def do_GET(self):
        self.go('GET')

    def do_POST(self):
        self.go('POST')


class Tests(unittest.TestCase):
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
        oauth._CACHE.clear()
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault()),
                         mock.patch.object(oauth, 'token_in_query', lambda url: '/identity/oauth/token' in url),
                         mock.patch.object(marketo, 'RATE_LIMIT_WAIT', 0)]
        for p in self._patches:
            p.start()
        store = ServiceConnectionStore()
        self.mk = store.save({'name': 'Marketo', 'kind': 'web', 'base_url': f'{self.root}/rest', 'auth': 'oauth2', 'client_id': 'mk-id',
                              'token_url': f'{self.root}/identity/oauth/token'}, secret=json.dumps({'client_secret': 'mk-secret'})).id
        self.lang = store.save({'name': 'Language', 'kind': 'web', 'base_url': self.root, 'auth': 'header',
                                'header_name': 'Ocp-Apim-Subscription-Key'}, secret='lang-key').id
        self.flow = store.save({'name': 'Flow', 'kind': 'web', 'base_url': f'{self.root}/workflows/abc/triggers/manual/paths/invoke',
                                'auth': 'signed-url'}, secret='api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=SIGNED').id

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
        return run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)

    def test_marketo_reads_leads_and_activities_renewing_and_waiting_as_told(self):
        cfg = {'provider': 'marketo', 'connection_id': self.mk}
        self.st.expire_once = self.st.rate_once = True
        r = self.run_tool('core.marketo_input', {**cfg, 'filter_values': [f'p{i}@example.com' for i in (1, 2, 3, 9)]})
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['x']['output']
        self.assertEqual([row[0] for row in out.rows()], [1, 2, 3], 'two pages')
        self.assertEqual(self.st.tokens, 2, 'error 602 renewed the token once')
        self.assertEqual(out.schema.field('updatedAt').type.value, 'datetime')
        acts = self.run_tool('core.marketo_input', {**cfg, 'read': 'activities', 'since': '2026-10-01T00:00:00Z',
                                                     'activity_type_ids': [1, 12]}).outputs['x']['output']
        self.assertEqual(len(acts), 2)
        self.assertEqual(dict(zip(acts.schema.names, next(acts.rows())))['attribute.Webpage URL'], '/pricing')
        self.assertIn('activity_type_ids', self.run_tool('core.marketo_input', {**cfg, 'read': 'activities', 'since': '2026-01-01T00:00:00Z'}).error['message'])

    def test_marketo_output_reports_each_lead_and_append_matches_by_email(self):
        cfg = {'provider': 'marketo', 'connection_id': self.mk}
        r = self.run_tool('core.marketo_output', cfg, WRITE, ([('email', 'v_wstring'), ('firstName', 'v_wstring')],
                                                              [['p1@example.com', 'Ada'], ['new@example.com', 'Neo']]))
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual([row[-3:-1] for row in r.outputs['x']['output'].rows()], [(1, 'updated'), (8, 'created')])
        skip = self.run_tool('core.marketo_output', {**cfg, 'action': 'createOnly'}, WRITE, ([('email', 'v_wstring')], [['p2@example.com']]))
        self.assertEqual(next(skip.outputs['x']['output'].rows())[-2:], ('skipped', '1005: Lead already exists'))
        app = self.run_tool('core.marketo_append', {**cfg, 'key_field': 'mail'}, NET,
                            ([('mail', 'v_wstring'), ('order', 'int32')], [['p3@example.com', '5'], ['nobody@example.com', '6']]))
        self.assertEqual(app.status, 'succeeded', app.error)
        out = app.outputs['x']['output']
        rows = [dict(zip(out.schema.names, r)) for r in out.rows()]
        self.assertEqual((rows[0]['Marketo_id'], rows[0]['Marketo_firstName']), (3, 'P3'))
        self.assertIsNone(rows[1]['Marketo_id'])
        self.assertEqual(self.run_tool('core.marketo_output', cfg, NET, ([('email', 'v_wstring')], [['a@b.c']])).error['code'], 'permission-denied')

    def test_text_analytics_batches_by_feature_and_reports_bad_texts(self):
        texts = [['I love Paris'], ['This broke'], ['Bonjour tout le monde'], [None], ['x' * 6000], ['BOOM text']]
        texts += [[f'I love item {i}'] for i in range(12)]
        r = self.run_tool('core.azure_text_analytics', {'provider': 'azure-ai-language', 'connection_id': self.lang, 'text_field': 'txt',
                                                         'analyses': ['sentiment', 'key_phrases', 'entities', 'language']}, NET,
                          ([{'name': 'txt', 'type': 'v_wstring', 'size': 100000}], texts))
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['x']['output']
        rows = [dict(zip(out.schema.names, row)) for row in out.rows()]
        self.assertEqual((rows[0]['Sentiment'], rows[0]['PositiveScore'], json.loads(rows[0]['Entities'])[0]['category']),
                         ('positive', 0.9, 'Location'))
        self.assertEqual((rows[2]['DetectedLanguageCode'], rows[1]['Sentiment']), ('fr', 'negative'))
        self.assertEqual(rows[3]['AnalysisError'], 'No text to analyse')
        self.assertIn('5120', rows[4]['AnalysisError'])
        self.assertEqual(rows[5]['AnalysisError'], 'InvalidArgument: Invalid document')
        kinds = [json.loads(c[4])['kind'] for c in self.st.calls if c[1] == '/language/:analyze-text']
        self.assertEqual((kinds.count('SentimentAnalysis'), kinds.count('EntityRecognition'), kinds.count('LanguageDetection')), (2, 4, 1))

    def test_power_automate_posts_each_record_with_the_signed_url(self):
        cfg = {'provider': 'power-automate', 'connection_id': self.flow}
        data = ([('n', 'int32'), ('name', 'v_wstring')], [['1', 'a'], ['2', 'b']])
        r = self.run_tool('core.power_automate', cfg, WRITE, data)
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual(self.st.flow_calls, [{'n': 1, 'name': 'a'}, {'n': 2, 'name': 'b'}])
        self.assertEqual([row[-2] for row in r.outputs['x']['output'].rows()], [202, 202])
        batch = self.run_tool('core.power_automate', {**cfg, 'mode': 'batch'}, WRITE, data)
        self.assertEqual(self.st.flow_calls[-1], [{'n': 1, 'name': 'a'}, {'n': 2, 'name': 'b'}])
        self.assertEqual(batch.outputs['x']['output'].schema.names, ['StatusCode', 'Response'])
        bad = self.run_tool('core.power_automate', cfg, WRITE, ([('n', 'int32')], [['3']]))
        self.assertIn('failed', bad.error['message'])
        soft = self.run_tool('core.power_automate', {**cfg, 'fail_on_error': False}, WRITE, ([('n', 'int32')], [['3']]))
        self.assertEqual(next(soft.outputs['x']['output'].rows())[-2:], (400, 'InvalidTemplate: Flow rejected n=3'))
        self.assertEqual(self.run_tool('core.power_automate', cfg, NET, data).error['code'], 'permission-denied')


if __name__ == '__main__':
    unittest.main()
