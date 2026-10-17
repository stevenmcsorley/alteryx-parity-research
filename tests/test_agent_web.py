"""Web research operations against local servers: SearXNG search, pages, data files, CKAN, OpenAPI, pagination."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from flowengine import agent, agent_web

PAGE = """<html><head><title>Weekly fuel prices</title><script>var x = 1;</script></head><body>
<h1>Weekly fuel prices</h1><p>Prices in pence per litre.</p>
<a href="/files/prices.csv">Prices (CSV)</a> <a href="/files/prices.xlsx">Prices (Excel)</a>
<a href="/about">About</a> <a href="https://elsewhere.example/x.csv">Mirror</a></body></html>"""
ABOUT = '<html><body><a href="/files/archive.zip">Archive</a></body></html>'
OPENAPI = {'openapi': '3.0.0', 'info': {'title': 'Prices API', 'version': '2'}, 'servers': [{'url': '/v2'}],
           'paths': {'/prices': {'get': {'summary': 'List prices', 'parameters': [{'name': 'page', 'in': 'query'}]}}}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, ctype='application/json', status=200):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == '/page':
            return self.send(PAGE, 'text/html')
        if url.path == '/about':
            return self.send(ABOUT, 'text/html')
        if url.path == '/search':
            if q.get('format') != 'json':
                return self.send('json not enabled', 'text/plain', 403)
            return self.send({'results': [{'title': 'Weekly road fuel prices', 'url': 'https://www.gov.uk/x', 'content': 'Official', 'engine': 'bing'}],
                              'unresponsive_engines': [['google', 'access denied']]})
        if url.path == '/api/3/action/package_search':
            return self.send({'result': {'results': [{'title': 'Fuel', 'name': 'fuel', 'notes': 'Weekly', 'metadata_modified': '2026-10-01',
                                                      'organization': {'title': 'DESNZ'},
                                                      'resources': [{'name': 'CSV', 'url': 'http://x/f.csv', 'format': 'CSV'}]}]}})
        if url.path == '/openapi.json':
            return self.send(OPENAPI)
        if url.path == '/v2/prices':
            page = int(q.get('page', 1))
            items = [{'week': f'2024-{page:02d}-0{i}', 'price': page * 10 + i} for i in range(1, 3)] if page <= 3 else []
            return self.send({'data': {'items': items}})
        if url.path == '/v2/cursor':
            cursor = q.get('cursor')
            body = {'items': [{'n': 1}], 'next': 'b'} if not cursor else {'items': [{'n': 2}], 'next': None}
            return self.send(body)
        return self.send({'error': 'not found'}, status=404)


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self.tmp.name})
        self.home.start()
        self.work = Path(self.tmp.name) / 'work'
        self.work.mkdir()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def call(self, name, permissions=('network', 'file_writes'), **args):
        return agent.call_envelope(name, args, agent.Context(self.work, permissions))


class SearchTests(WebCase):
    def test_searxng_is_used_once_configured_and_reports_unresponsive_engines(self):
        self.assertEqual(self.call('web_search', provider='searxng', query='fuel')['error']['code'], 'not-configured')
        agent_web.save_search_settings(self.base)
        out = self.call('web_search', query='fuel prices')['result']
        self.assertEqual((out['provider'], out['results'][0]['url'], out['results'][0]['engine']), ('searxng', 'https://www.gov.uk/x', 'bing'))
        self.assertEqual(out['unresponsive_engines'], [['google', 'access denied']])
        self.assertEqual(self.call('web_search', permissions=(), query='x')['error']['code'], 'permission-required')

    def test_auto_falls_back_to_duckduckgo_when_searxng_finds_nothing(self):
        agent_web.save_search_settings(self.base)
        with mock.patch.object(agent_web, '_searxng', return_value=([], [['brave', 'too many requests']])), \
             mock.patch.object(agent_web, '_duckduckgo', return_value=[{'title': 'T', 'url': 'https://a', 'snippet': ''}]) as ddg:
            out = self.call('web_search', query='fuel')['result']
        self.assertEqual(out['provider'], 'duckduckgo')
        self.assertIn('fell back to DuckDuckGo', out['notes'][0])
        ddg.assert_called_once()

    def test_ckan_catalogue_search_lists_files(self):
        out = self.call('search_datasets', query='fuel', catalogue='ckan', ckan_url=self.base)['result']
        self.assertEqual(out['results'][0]['files'][0]['format'], 'CSV')
        self.assertEqual(out['results'][0]['publisher'], 'DESNZ')


class PageTests(WebCase):
    def test_page_text_links_and_data_files(self):
        page = self.call('fetch_page', url=f'{self.base}/page')['result']
        self.assertEqual(page['title'], 'Weekly fuel prices')
        self.assertIn('Prices in pence per litre.', page['text'])
        self.assertNotIn('var x', page['text'])
        self.assertEqual([f['url'] for f in page['data_files']],
                         [f'{self.base}/files/prices.csv', f'{self.base}/files/prices.xlsx', 'https://elsewhere.example/x.csv'])

    def test_crawl_finds_files_on_linked_pages_of_the_same_site(self):
        out = self.call('find_data_files', url=f'{self.base}/page', depth=1)['result']
        found = {f['url']: f['found_on'] for f in out['data_files']}
        self.assertEqual(found[f'{self.base}/files/archive.zip'], f'{self.base}/about')
        self.assertNotIn('https://elsewhere.example/', ' '.join(out['pages_visited']))


class ApiTests(WebCase):
    def test_openapi_summary(self):
        api = self.call('inspect_api', url=f'{self.base}/openapi.json')['result']
        self.assertEqual((api['title'], api['endpoints'][0]['path'], api['endpoints'][0]['parameters'][0]['name']), ('Prices API', '/prices', 'page'))

    def test_page_pagination_saves_all_items_with_provenance(self):
        out = self.call('fetch_api', url=f'{self.base}/v2/prices', items_path='data.items', pagination={'mode': 'page'}, save_as='data/prices.csv')['result']
        self.assertEqual((out['item_count'], len(out['pages'])), (6, 4))
        saved = Path(out['saved_as'])
        self.assertEqual(saved.read_text().splitlines()[0], 'week,price')
        self.assertEqual(json.loads(Path(f'{saved}.source.json').read_text())['sha256'], out['sha256'])
        self.assertEqual(self.call('profile_file', path='data/prices.csv')['result']['rows'], 6)

    def test_cursor_pagination(self):
        out = self.call('fetch_api', url=f'{self.base}/v2/cursor', items_path='items', pagination={'mode': 'cursor', 'cursor_path': 'next'})['result']
        self.assertEqual(out['items'], [{'n': 1}, {'n': 2}])


class HostExtensionTests(WebCase):
    def test_environment_and_long_running_processes(self):
        import sys
        env = self.call('inspect_environment')['result']
        self.assertTrue(env['python'] and env['packages'].get('pyarrow'))
        started = self.call('start_process', permissions=('processes',),
                            argv=[sys.executable, '-c', 'import time\nprint("a", flush=True)\ntime.sleep(30)'])['result']
        import time
        for _ in range(50):
            status = self.call('process_status', process_id=started['process_id'])['result']
            if 'a' in status['output']:
                break
            time.sleep(0.1)
        self.assertTrue(status['running'])
        stopped = self.call('stop_process', permissions=('processes',), process_id=started['process_id'])['result']
        self.assertFalse(stopped['running'])

    def test_package_names_are_checked_and_missing_r_is_reported(self):
        bad = self.call('install_python_packages', permissions=('processes', 'network'), packages=['ok; rm -rf /'])
        self.assertEqual(bad['error']['code'], 'bad-package')
        with mock.patch('flowengine.runtimes.find_rscript', return_value=None):
            self.assertEqual(self.call('run_r', permissions=('processes',), code='1')['error']['code'], 'not-installed')


if __name__ == '__main__':
    unittest.main()
