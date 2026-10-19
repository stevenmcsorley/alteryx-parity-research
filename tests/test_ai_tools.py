"""AI Tools against fixtures of the OpenAI-compatible Chat Completions and Anthropic Messages APIs, and live against a
local Ollama model when FLOWBENCH_OLLAMA_URL and FLOWBENCH_OLLAMA_MODEL are set (the llm-runtime CI job)."""
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import flowengine.db.vault as vault_module
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
LIVE = bool(os.environ.get('FLOWBENCH_OLLAMA_URL') and os.environ.get('FLOWBENCH_OLLAMA_MODEL'))


def answer_for(prompt, schema):
    """The fake model: deterministic answers for the prompts the tools send."""
    if 'country is' in prompt:
        return {'Paris': 'France', 'Tokyo': 'Japan'}.get(prompt.split('is ')[1].split('?')[0], 'Unknown')
    if 'Standardise' in prompt:
        values = json.loads(prompt.split('Values: ')[1])
        canon = {'colour': 'color', 'Color': 'color', 'COLOR': 'color', 'grey': 'gray'}
        return json.dumps({'mapping': [{'original': v, 'standard': 'Purple' if v == 'violet' else canon.get(v, v)} for v in values]})
    if 'Generate' in prompt:
        n = int(prompt.split('Generate ')[1].split(' ')[0])
        return json.dumps({'rows': [{'Name': f'Person {i}', 'Age': 20 + i} for i in range(n)]})
    if 'Extract this invoice' in prompt:
        return json.dumps({'metadata': {'InvoiceNumber': 'INV-7', 'Total': 42.5, 'Currency': 'EUR'},
                           'line_items': [{'Description': 'Widget', 'Quantity': 2, 'Amount': 40}, {'Description': 'Ship', 'Quantity': 1, 'Amount': 2.5}]})
    if 'pandas code' in prompt:
        if 'previous code failed' not in prompt:
            return json.dumps({'steps': [{'description': 'broken', 'code': 'df = df.nonexistent()'}]})
        return json.dumps({'steps': [{'description': 'Rename columns', 'code': "df = df.rename(columns={'full_name': 'Name', 'yrs': 'Age'})"},
                                     {'description': 'Age as integer', 'code': "df['Age'] = df['Age'].astype(int)"}]})
    if schema is not None:
        return '{"not": "matching"}'
    return 'plain answer'


class H(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        H.calls.append((self.path, dict(self.headers), body))
        if self.path == '/v1/chat/completions':
            assert self.headers.get('Authorization') == 'Bearer sk-test'
            msg = body['messages'][-1]['content']
            text = msg if isinstance(msg, str) else next(p['text'] for p in msg if p['type'] == 'text')
            schema = (body.get('response_format') or {}).get('json_schema', {}).get('schema')
            return self.send(200, {'choices': [{'message': {'role': 'assistant', 'content': answer_for(text, schema)}}]})
        if self.path == '/v1/messages':
            assert self.headers.get('x-api-key') == 'ak-test' and self.headers.get('anthropic-version') == '2023-06-01'
            assert body['max_tokens'] > 0
            text = next(b['text'] for b in body['messages'][0]['content'] if b['type'] == 'text')
            schema = json.loads(text.split('JSON schema:\n')[1]) if 'JSON schema:\n' in text else None
            return self.send(200, {'content': [{'type': 'text', 'text': answer_for(text.split('\n\nAnswer with JSON')[0], schema)}]})
        return self.send(404, {'error': {'message': 'no such endpoint'}})


class AiToolTests(unittest.TestCase):
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
        H.calls = []
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        store = ServiceConnectionStore()
        self.oai = store.save({'name': 'OpenAI', 'kind': 'web', 'base_url': f'{self.root}/v1', 'auth': 'bearer'}, secret='sk-test').id
        self.ant = store.save({'name': 'Anthropic', 'kind': 'web', 'base_url': self.root, 'auth': 'header', 'header_name': 'x-api-key'},
                              secret='ak-test').id

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def png(self):
        """A real image file read by Blob Input (field Blob), as a workflow would attach one."""
        path = os.path.join(self._home.name, 'dot.png')
        with open(path, 'wb') as fh:
            fh.write(PNG)
        return {'id': 't', 'tool': 'core.blob_input', 'config': {'mode': 'file', 'path': path}}

    def override(self, provider='openai-compatible'):
        return {'id': 'm', 'tool': 'core.llm_override', 'config': {'provider': provider, 'connection_id': self.oai if provider == 'openai-compatible'
                                                                   else self.ant, 'model': 'test-model', 'temperature': 0}}

    def flow(self, nodes, edges, limits=NET):
        r = run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits)
        self.assertEqual(r.status, 'succeeded', r.error)
        return r

    def rows(self, r, node='x', anchor='output'):
        t = r.outputs[node][anchor]
        return [dict(zip(t.schema.names, row)) for row in t.rows()]

    def test_prompt_per_record_through_llm_override_on_both_apis(self):
        for provider in ('openai-compatible', 'anthropic'):
            r = self.flow([self.override(provider), text_input('t', [('city', 'v_wstring')], [['Paris'], ['Tokyo']]),
                          {'id': 'x', 'tool': 'core.prompt', 'config': {'prompt': 'In which country is [city]? One word.'}}],
                         [edge('m', 'model', 'x', 'model'), edge('t', 'output', 'x', 'data')])
            self.assertEqual([(o['city'], o['LLM_Response']) for o in self.rows(r)], [('Paris', 'France'), ('Tokyo', 'Japan')], provider)
        body = H.calls[0][2]
        self.assertEqual((body['model'], body['temperature']), ('test-model', 0))
        self.assertNotIn('sk-test', json.dumps(r.outputs['m']['model'].to_dicts()), 'the model record carries no key')

    def test_prompt_attachments_schema_validation_and_unknown_fields(self):
        cfg = {'provider': 'openai-compatible', 'connection_id': self.oai, 'model': 'vision', 'prompt': 'Describe the image',
               'attachment_fields': ['Blob']}
        img = self.png()
        r = self.flow([img, {'id': 'x', 'tool': 'core.prompt', 'config': cfg}], [edge('t', 'output', 'x', 'data')])
        parts = H.calls[-1][2]['messages'][-1]['content']
        self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertEqual(self.rows(r)[0]['LLM_Response'], 'plain answer')
        bad = {**cfg, 'attachment_fields': [], 'schema': {'type': 'object', 'required': ['label'], 'properties': {'label': {'type': 'string'}}}}
        r = self.flow([img, {'id': 'x', 'tool': 'core.prompt', 'config': bad}], [edge('t', 'output', 'x', 'data')])
        self.assertIsNone(self.rows(r)[0]['LLM_Response'], 'an answer that misses the schema becomes null')
        strict = run_workflow(workflow([img, {'id': 'x', 'tool': 'core.prompt', 'config': {**bad, 'on_invalid': 'error'}}],
                                       [edge('t', 'output', 'x', 'data')]), base_dir=ROOT, limits=NET)
        self.assertIn('valid JSON', strict.error['message'])
        unknown = run_workflow(workflow([img, {'id': 'x', 'tool': 'core.prompt', 'config': {**cfg, 'prompt': 'Hi [nope]', 'attachment_fields': []}}],
                                        [edge('t', 'output', 'x', 'data')]), base_dir=ROOT, limits=NET)
        self.assertIn('[nope]', unknown.error['message'])

    def test_precision_match_sends_each_value_once_and_respects_the_reference(self):
        data = text_input('d', [('c', 'v_wstring')], [['colour'], ['Color'], ['colour'], ['grey'], ['violet'], [None]])
        ref = text_input('r', [('std', 'v_wstring')], [['color'], ['gray']])
        r = self.flow([self.override(), data, ref, {'id': 'x', 'tool': 'core.precision_match', 'config': {'field': 'c', 'reference_field': 'std'}}],
                     [edge('m', 'model', 'x', 'model'), edge('d', 'output', 'x', 'data'), edge('r', 'output', 'x', 'reference')])
        self.assertEqual([o['Standardized'] for o in self.rows(r)], ['color', 'color', 'color', 'gray', 'violet', None],
                         'violet maps to Purple, which is not in the reference, so it stays unchanged')
        sent = json.loads(H.calls[-1][2]['messages'][-1]['content'].split('Values: ')[1])
        self.assertEqual(sent, ['Color', 'colour', 'grey', 'violet'], 'distinct values, once each')
        self.assertEqual(len(self.rows(r, anchor='mapping')), 4)

    def test_synthetic_input_batches_and_types(self):
        r = self.flow([self.override(), {'id': 'x', 'tool': 'core.synthetic_input', 'config': {'use_case': 'gym members', 'columns': ['Name', 'Age'],
                                                                                               'rows': 7, 'batch_size': 3}}],
                     [edge('m', 'model', 'x', 'model')])
        out = r.outputs['x']['output']
        self.assertEqual((len(out), [(f.name, f.type.value) for f in out.schema]), (7, [('Name', 'v_wstring'), ('Age', 'int64')]))
        self.assertEqual(len(H.calls), 3, 'batches of 3, 3 and 1')

    def test_invoice_extractor_returns_header_and_line_items(self):
        r = self.flow([self.override('anthropic'), self.png(), {'id': 'x', 'tool': 'core.invoice_extractor',
                                                                'config': {'field': 'Blob', 'date_format': 'YYYY-MM-DD'}}],
                     [edge('m', 'model', 'x', 'model'), edge('t', 'output', 'x', 'data')])
        self.assertEqual([(o['Line'], o['Description'], o['Amount']) for o in self.rows(r)], [(1, 'Widget', 40.0), (2, 'Ship', 2.5)])
        meta = self.rows(r, anchor='metadata')[0]
        self.assertEqual((meta['InvoiceNumber'], meta['Total'], meta['Currency'], meta['DueDate']), ('INV-7', 42.5, 'EUR', None))
        block = H.calls[-1][2]['messages'][0]['content'][0]
        self.assertEqual((block['type'], block['source']['media_type']), ('image', 'image/png'))

    def test_schema_fit_runs_model_code_in_the_isolated_process_and_retries_on_failure(self):
        target = text_input('tg', [('Name', 'v_wstring'), ('Age', 'int64')], [['Ada', '36']])
        source = text_input('s', [('full_name', 'v_wstring'), ('yrs', 'v_wstring')], [['Grace', '45'], ['Alan', '41']])
        nodes = [self.override(), target, source, {'id': 'x', 'tool': 'core.schema_fit', 'config': {}}]
        edges = [edge('m', 'model', 'x', 'model'), edge('tg', 'output', 'x', 'target'), edge('s', 'output', 'x', 'source')]
        denied = run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=NET)
        self.assertEqual(denied.error['code'], 'permission-denied', 'model-written code needs the process permission')
        r = self.flow(nodes, edges, {**NET, 'allow_processes': True})
        self.assertEqual(self.rows(r), [{'Name': 'Grace', 'Age': 45}, {'Name': 'Alan', 'Age': 41}])
        self.assertEqual([s['Transformation Step'] for s in self.rows(r, anchor='steps')], ['Rename columns', 'Age as integer'])
        self.assertIn('previous code failed', H.calls[-1][2]['messages'][-1]['content'], 'the first failure was sent back')


@unittest.skipUnless(LIVE, 'set FLOWBENCH_OLLAMA_URL and FLOWBENCH_OLLAMA_MODEL to run against a local Ollama model')
class LiveOllamaTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name})
        self._env.start()
        conn = ServiceConnectionStore().save({'name': 'Ollama', 'kind': 'web', 'base_url': os.environ['FLOWBENCH_OLLAMA_URL'], 'auth': 'none'}).id
        self.model = {'provider': 'openai-compatible', 'connection_id': conn, 'model': os.environ['FLOWBENCH_OLLAMA_MODEL'], 'temperature': 0}

    def tearDown(self):
        self._env.stop()
        self._home.cleanup()

    def test_prompt_and_structured_synthetic_records_from_a_real_model(self):
        doc = workflow([text_input('t', [('n', 'int32')], [['2'], ['3']]),
                        {'id': 'x', 'tool': 'core.prompt', 'config': {**self.model, 'prompt': 'What is [n] plus [n]? Reply with the number only.',
                                                                       'schema': {'type': 'object', 'required': ['answer'],
                                                                                  'properties': {'answer': {'type': 'integer'}}}}}],
                       [edge('t', 'output', 'x', 'data')])
        r = run_workflow(doc, base_dir=ROOT, limits=NET)
        self.assertEqual(r.status, 'succeeded', r.error)
        answers = [json.loads(row[-1]) for row in r.outputs['x']['output'].rows()]
        self.assertTrue(all(isinstance(a['answer'], int) for a in answers), 'a real model returned schema-valid JSON')
        doc = workflow([{'id': 'x', 'tool': 'core.synthetic_input', 'config': {**self.model, 'use_case': 'library books',
                                                                               'columns': ['Title', 'Year'], 'rows': 3}}], [])
        r = run_workflow(doc, base_dir=ROOT, limits=NET)
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual((len(r.outputs['x']['output']), r.outputs['x']['output'].schema.names), (3, ['Title', 'Year']))


if __name__ == '__main__':
    unittest.main()
