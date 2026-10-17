"""Agent operations: discovery, native profiling and result queries, evidence and downloads."""
import json
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flowengine import agent
from flowengine.service import EngineService

from helpers import edge, workflow

CSV = 'Region,Product,Sales\nNorth,A,10\nSouth,B,5\nNorth,B,7\nEast,A,\n'


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / 'sales.csv').write_text(CSV)
        self.service = EngineService()

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, permissions=(), **arguments):
        return self.service.handle('agent.call', {'name': name, 'arguments': arguments, 'base_dir': str(self.dir),
                                                  'permissions': list(permissions)})

    def run_sales(self):
        doc = workflow([{'id': 'in', 'tool': 'core.input_data', 'config': {'path': 'sales.csv'}},
                        {'id': 'typed', 'tool': 'core.auto_field', 'config': {}}],
                       [edge('in', 'output', 'typed', 'input')])
        run_id = self.service.handle('run.start', {'workflow': doc, 'base_dir': str(self.dir)})['run_id']
        for _ in range(200):
            if self.service.runs[run_id]['status'] != 'running':
                break
            time.sleep(0.02)
        self.assertEqual(self.service.runs[run_id]['status'], 'succeeded')
        return run_id


class DiscoveryTests(AgentCase):
    def test_specs_are_mcp_shaped(self):
        specs = self.service.handle('agent.tools_list', {})
        self.assertIn('find_tools', [s['name'] for s in specs])
        for s in specs:
            self.assertEqual(set(s), {'name', 'description', 'inputSchema', 'annotations', 'x-flowbench'})
            self.assertEqual(s['inputSchema']['type'], 'object')
        download = next(s for s in specs if s['name'] == 'download_file')
        self.assertTrue(download['annotations']['openWorldHint'])
        self.assertEqual(download['x-flowbench']['permissions'], ['network', 'file_writes'])

    def test_task_words_find_the_native_tools(self):
        found = lambda q: [t['tool_id'] for t in self.call('find_tools', query=q)['result']['tools']][:5]
        self.assertIn('core.summarize', found('average sales by region'))
        self.assertIn('core.download', found('download a csv from a url'))
        self.assertIn('core.join', found('join two tables'))

    def test_describe_lists_every_setting_and_notes(self):
        spec = self.call('describe_tool', tool_id='core.summarize')['result']
        self.assertEqual([s['name'] for s in spec['settings']], ['actions', 'sort_groups'])
        self.assertTrue(spec['settings'][0]['required'])
        self.assertTrue(spec['usage_notes'])
        self.assertEqual(self.call('describe_tool', tool_id='core.nope')['error']['code'], 'unknown-tool')

    def test_bad_arguments_are_structured_errors(self):
        out = self.call('find_tools', query='')
        self.assertEqual((out['ok'], out['error']['code']), (False, 'invalid-arguments'))
        self.assertIn('query', out['error']['message'])


class DataTests(AgentCase):
    def test_profile_computes_types_and_stats_natively(self):
        p = self.call('profile_file', path='sales.csv')['result']
        self.assertEqual(p['rows'], 4)
        sales = next(f for f in p['fields'] if f['name'] == 'Sales')
        self.assertEqual((sales['type_as_read'], sales['nulls'], sales['min'], sales['max']), ('v_wstring', 1, 5.0, 10.0))
        self.assertEqual(len(p['sha256']), 64)
        self.assertEqual(self.call('list_files')['result']['entries'][0]['name'], 'sales.csv')

    def test_query_results_aggregates_the_full_output(self):
        run_id = self.run_sales()
        out = self.call('query_results', run_id=run_id, node_id='typed', group_by=['Region'],
                        aggregates=[{'field': 'Sales', 'op': 'sum'}, {'field': 'Sales', 'op': 'count'}],
                        sort=[{'field': 'Sales_sum', 'descending': True}])['result']
        self.assertEqual(out['rows'][0], {'Region': 'North', 'Sales_sum': 17, 'Sales_count': 2})
        self.assertEqual((out['source_rows'], out['computed_by']), (4, 'flowbench'))
        whole = self.call('query_results', run_id=run_id, node_id='typed', aggregates=[{'field': 'Sales', 'op': 'avg'}])['result']
        self.assertAlmostEqual(whole['rows'][0]['Sales_mean'], 22 / 3)
        everything = self.call('query_results', run_id=run_id, node_id='typed', limit=None)['result']
        self.assertEqual(everything['returned_rows'], 4)
        self.assertEqual(self.call('query_results', run_id=run_id, node_id='typed', columns=['Nope'])['error']['code'], 'unknown-field')

    def test_evidence_names_versions_digests_and_artifacts(self):
        run_id = self.run_sales()
        ev = self.call('run_evidence', run_id=run_id)['result']
        self.assertEqual(ev['status'], 'succeeded')
        self.assertEqual(len(ev['workflow_digest']), 64)
        self.assertEqual(ev['nodes']['typed']['outputs']['output']['rows'], 4)
        self.assertIn({'node_id': 'typed', 'anchor': 'output'}, ev['artifacts'])


class DownloadTests(AgentCase):
    def test_download_needs_permission_and_records_provenance(self):
        served = self.dir / 'site'
        served.mkdir()
        (served / 'fuel.csv').write_text('week,price\n1,140.2\n')
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(_Quiet, directory=str(served)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_address[1]}/fuel.csv'
        try:
            denied = self.call('download_file', url=url)
            self.assertEqual(denied['error']['code'], 'permission-required')
            self.assertEqual(denied['error']['permissions'], ['network', 'file_writes'])
            got = self.call('download_file', permissions=('network', 'file_writes'), url=url)['result']
        finally:
            server.shutdown()
        saved = Path(got['path'])
        self.assertEqual((saved.parent.name, saved.read_text()), ('data', 'week,price\n1,140.2\n'))
        source = json.loads((saved.parent / 'fuel.csv.source.json').read_text())
        self.assertEqual((source['url'], source['bytes'], source['sha256']), (url, 19, got['sha256']))
        self.assertEqual(self.call('profile_file', path='data/fuel.csv')['result']['rows'], 1)


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


if __name__ == '__main__':
    unittest.main()


class McpTests(AgentCase):
    def test_stdio_session_lists_and_calls_tools_and_runs_a_workflow_file(self):
        import io
        import subprocess
        import sys
        doc = workflow([{'id': 'in', 'tool': 'core.input_data', 'config': {'path': 'sales.csv'}},
                        {'id': 'typed', 'tool': 'core.auto_field', 'config': {}}], [edge('in', 'output', 'typed', 'input')])
        (self.dir / 'sales.flow.json').write_text(json.dumps(doc))
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'run_workflow', 'arguments': {'path': 'sales.flow.json'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'download_file', 'arguments': {'url': 'http://127.0.0.1:9/x.csv'}}},
        ]
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.run([sys.executable, '-m', 'flowengine', 'mcp', '--workspace', str(self.dir)], cwd=root,
                              input='\n'.join(json.dumps(m) for m in messages) + '\n', capture_output=True, text=True, timeout=120)
        replies = {r['id']: r for r in map(json.loads, proc.stdout.splitlines())}
        self.assertEqual(sorted(replies), [1, 2, 3, 4])
        self.assertEqual(replies[1]['result']['serverInfo']['name'], 'flowbench')
        names = [t['name'] for t in replies[2]['result']['tools']]
        self.assertTrue({'find_tools', 'query_results', 'run_workflow', 'download_file'} <= set(names))
        ran = replies[3]['result']
        self.assertFalse(ran['isError'])
        self.assertEqual((ran['structuredContent']['status'], ran['structuredContent']['nodes']['typed']['output']), ('succeeded', 4))
        denied = replies[4]['result']
        self.assertTrue(denied['isError'])
        self.assertEqual(denied['structuredContent']['error']['code'], 'permission-required')
