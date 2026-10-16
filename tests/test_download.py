"""Download against a local HTTP server (no external network)."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_workflow
from flowengine.workflow_effects import workflow_effects

from helpers import ROOT, edge, text_input, workflow

ALLOW = {'allow_network': True}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body, content_type='application/json; charset=utf-8', extra=()):
        payload = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def _route(self):
        parts = urlsplit(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length).decode('utf-8') if length else ''
        if parts.path == '/echo':
            self._reply(200, json.dumps({'method': self.command, 'query': parse_qs(parts.query),
                                         'token': self.headers.get('X-Token'), 'region': self.headers.get('Region'),
                                         'type': self.headers.get('Content-Type'), 'body': body}))
        elif parts.path == '/missing':
            self._reply(404, '{"error": "not found"}')
        elif parts.path == '/binary':
            self._reply(200, bytes(range(256)), 'application/octet-stream')
        elif parts.path == '/big':
            self._reply(200, 'x' * 5000, 'text/plain')
        elif parts.path == '/to-file':
            self._reply(302, '', extra=[('Location', 'file:///etc/passwd')])
        elif parts.path == '/latin':
            self._reply(200, 'café'.encode('latin-1'), 'text/plain; charset=latin-1')
        else:
            self._reply(500, 'unexpected')

    do_GET = do_POST = do_PUT = do_HEAD = _route


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_download(self, fields, rows, limits=ALLOW, **config):
        doc = workflow([text_input('t', fields, rows),
                        {'id': 'dl', 'tool': 'core.download', 'config': {'url_field': 'url', **config}}],
                       [edge('t', 'output', 'dl', 'input')])
        return run_workflow(doc, base_dir=ROOT, limits=limits)

    def test_requires_network_permission_and_is_reported_by_preflight(self):
        result = self.run_download([('url', 'v_wstring')], [[self.base + '/echo']], limits={})
        self.assertEqual(result.error['code'], 'permission-denied')
        doc = workflow([text_input('t', [('url', 'v_wstring')], []),
                        {'id': 'dl', 'tool': 'core.download', 'config': {'url_field': 'url'}}], [])
        self.assertEqual([e['tool_id'] for e in workflow_effects(doc, ROOT)['network_requests']], ['core.download'])

    def test_get_with_query_and_header_fields_keeps_record_order(self):
        rows = [[self.base + '/echo?fixed=1', 'N', 'a b'], [self.base + '/missing', 'S', 'c'], [self.base + '/echo', None, 'd']]
        result = self.run_download([('url', 'v_wstring'), ('Region', 'v_wstring'), ('q', 'v_wstring')], rows,
                                   headers=[{'name': 'X-Token', 'value': 'secret'}], header_fields=['Region'],
                                   query_fields=['q'], connections=3)
        self.assertTrue(result.ok, result.error)
        out = result.output('dl')
        self.assertEqual(out.schema.names[-4:], ['DownloadData', 'DownloadHeaders', 'StatusCode', 'DownloadError'])
        self.assertEqual(out.column('StatusCode'), [200, 404, 200])
        first, third = json.loads(out.column('DownloadData')[0]), json.loads(out.column('DownloadData')[2])
        self.assertEqual(first['query'], {'fixed': ['1'], 'q': ['a b']})
        self.assertEqual((first['token'], first['region']), ('secret', 'N'))
        self.assertIsNone(third['region'])  # null header fields are omitted
        self.assertIn('Content-Type: application/json', out.column('DownloadHeaders')[1])
        self.assertEqual(out.column('DownloadError'), [None, None, None])
        self.assertNotIn('secret', json.dumps(result.events))

    def test_post_bodies_from_fields(self):
        fields = [('url', 'v_wstring'), ('name', 'v_wstring'), ('amount', 'int32')]
        rows = [[self.base + '/echo', 'Ann', 3]]
        for body, expected_type, expected_body in [
                ('json', 'application/json', {'name': 'Ann', 'amount': '3'}),
                ('form', 'application/x-www-form-urlencoded', 'name=Ann&amount=3')]:
            with self.subTest(body=body):
                result = self.run_download(fields, rows, method='POST', body=body, body_fields=['name', 'amount'])
                echo = json.loads(result.output('dl').column('DownloadData')[0])
                self.assertEqual((echo['method'], echo['type']), ('POST', expected_type))
                self.assertEqual(json.loads(echo['body']) if body == 'json' else echo['body'], expected_body)
        result = self.run_download(fields, rows, method='PUT', body='field', body_field='name', content_type='text/plain')
        echo = json.loads(result.output('dl').column('DownloadData')[0])
        self.assertEqual((echo['method'], echo['type'], echo['body']), ('PUT', 'text/plain', 'Ann'))

    def test_binary_charset_and_size_limit(self):
        result = self.run_download([('url', 'v_wstring')], [[self.base + '/binary']], output='blob')
        self.assertEqual(result.output('dl').column('DownloadData'), [bytes(range(256))])
        result = self.run_download([('url', 'v_wstring')], [[self.base + '/latin']])
        self.assertEqual(result.output('dl').column('DownloadData'), ['café'])
        result = self.run_download([('url', 'v_wstring')], [[self.base + '/big']], max_response_bytes=100)
        self.assertIn('max_response_bytes', result.error['message'])

    def test_failures_stop_or_are_recorded(self):
        rows = [['ftp://example.invalid/x'], [self.base + '/to-file'], ['http://127.0.0.1:1/closed'], ['']]
        result = self.run_download([('url', 'v_wstring')], rows)
        self.assertEqual(result.status, 'failed')
        self.assertIn('record 1', result.error['message'])
        result = self.run_download([('url', 'v_wstring')], rows, on_error='record', timeout_seconds=5)
        self.assertTrue(result.ok, result.error)
        errors = result.output('dl').column('DownloadError')
        self.assertTrue(all(errors), errors)
        self.assertIn('http:// or https://', errors[0])
        self.assertIn('non-HTTP(S)', errors[1])
        self.assertEqual(result.output('dl').column('StatusCode'), [None] * 4)

    def test_configuration_and_schema_are_validated(self):
        cases = [({'method': 'GET', 'body': 'json', 'body_fields': ['url']}, 'cannot send a body'),
                 ({'headers': [{'name': 'Bad Header', 'value': 'x'}]}, 'Invalid HTTP header'),
                 ({'query_fields': ['nope']}, 'Unknown field'),
                 ({'body': 'form', 'method': 'POST'}, 'requires body_fields')]
        for config, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(WorkflowValidationError, message):
                self.run_download([('url', 'v_wstring')], [['x']], **config)
        with self.assertRaisesRegex(WorkflowValidationError, 'already exist'):
            self.run_download([('url', 'v_wstring'), ('StatusCode', 'int32')], [['x', 1]])


if __name__ == '__main__':
    unittest.main()
