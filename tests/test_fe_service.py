"""Editor service protocol tests (JSON-RPC over stdio)."""
import json
import subprocess
import sys
import unittest

from flowengine.service import respond

from helpers import ROOT, edge, text_input, workflow


def call(method, params=None, rid=1):
    return json.loads(respond(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params or {}})))


class ServiceTests(unittest.TestCase):
    def test_info_tools_catalog(self):
        self.assertEqual(call('engine.info')['result']['protocol'], 1)
        tools = call('tools.list')['result']
        self.assertTrue(any(t['tool_id'] == 'core.filter' for t in tools))
        cat = call('catalog.list')['result']
        self.assertEqual(len(cat), 323)
        self.assertEqual(next(c for c in cat if c['id'] == 'preparation:filter')['kernel'], 'core.filter')

    def test_validate_returns_schemas_and_problems(self):
        wf = workflow([text_input('t', [('a', 'int32')], [[1]]),
                       {'id': 'f', 'tool': 'core.formula', 'config': {'formulas': [{'field': 'b', 'expression': '[a]+1'}]}}],
                      [edge('t', 'output', 'f', 'input')])
        r = call('workflow.validate', {'workflow': wf, 'base_dir': str(ROOT)})['result']
        self.assertTrue(r['valid'])
        self.assertEqual([f['name'] for f in r['schemas']['f']['output']], ['a', 'b'])
        wf['nodes'][1]['config']['formulas'][0]['expression'] = '[zz]'
        r = call('workflow.validate', {'workflow': wf, 'base_dir': str(ROOT)})['result']
        self.assertFalse(r['valid'])
        self.assertEqual(r['problems'][0]['node_id'], 'f')

    def test_run_previews_and_errors(self):
        wf = workflow([text_input('t', [('a', 'int32')], [[i] for i in range(5)]),
                       {'id': 'b', 'tool': 'core.browse', 'config': {}}], [edge('t', 'output', 'b', 'input')])
        r = call('workflow.run', {'workflow': wf, 'base_dir': str(ROOT), 'preview_rows': 2})['result']
        self.assertEqual(r['status'], 'succeeded')
        self.assertEqual(r['previews']['t']['output']['rows'], [['0'], ['1']])
        self.assertEqual(r['browse']['b']['total_rows'], 5)
        self.assertEqual(call('nope')['error']['code'], -32601)
        self.assertEqual(call('workflow.run', {'workflow': wf})['error']['data']['code'], 'bad-request')
        self.assertEqual(json.loads(respond('{bad'))['error']['code'], -32700)

    def test_stdio_process(self):
        reqs = '\n'.join(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': 'engine.info'}) for i in range(3)) + '\n'
        p = subprocess.run([sys.executable, '-m', 'flowengine', 'serve'], input=reqs, capture_output=True, text=True,
                           cwd=ROOT, timeout=60)
        lines = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([l['id'] for l in lines], [0, 1, 2])


if __name__ == '__main__':
    unittest.main()
