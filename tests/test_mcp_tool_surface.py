"""Through the real MCP stdio server: every registered tool can be found and described, provider-backed tools say how to
set up their provider, and saved Web/API connections and provider status are visible (never secrets)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from flowengine.plugin import default_registry

ROOT = Path(__file__).resolve().parents[1]


def mcp(workspace, home, calls):
    messages = [{'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}}]
    messages += [{'jsonrpc': '2.0', 'id': i + 1, 'method': 'tools/call', 'params': {'name': n, 'arguments': a}}
                 for i, (n, a) in enumerate(calls)]
    proc = subprocess.run([sys.executable, '-m', 'flowengine', 'mcp', '--workspace', str(workspace)],
                          input='\n'.join(json.dumps(m) for m in messages) + '\n', capture_output=True, text=True,
                          cwd=ROOT, env={**os.environ, 'FLOWENGINE_HOME': str(home)}, timeout=300)
    answers = {}
    for line in proc.stdout.splitlines():
        msg = json.loads(line)
        if msg.get('id'):
            content = msg['result']['content'][0]['text'] if 'result' in msg else json.dumps(msg.get('error'))
            answers[msg['id']] = json.loads(content) if content.startswith(('{', '[')) else content
    return [answers.get(i + 1) for i in range(len(calls))]


class McpToolSurfaceTests(unittest.TestCase):
    def test_every_tool_is_discoverable_and_provider_tools_explain_their_setup(self):
        tools = sorted(t.manifest.tool_id for t in default_registry().tools())
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as home:
            from flowengine.service_connections import ServiceConnectionStore
            ServiceConnectionStore(Path(home) / 'service_connections.json').save(
                {'name': 'Routing', 'kind': 'web', 'base_url': 'http://localhost:8002', 'auth': 'none'})
            # small batches: a large answer is bounded (stored_result) by design, as for any MCP client
            calls = [('describe_tool', {'tool_ids': [t]}) for t in tools]
            calls += [('find_tools', {'query': 'salesforce'}), ('list_connections', {}),
                      ('list_providers', {'tool_id': 'core.non_overlap_drivetime'})]
            *described, found, connections, providers = mcp(ws, home, calls)
        specs = {}
        for batch in described:
            self.assertIn('tools', batch, f'a describe batch was bounded: {str(batch)[:200]}')
            for tid, spec in batch['tools'].items():
                self.assertNotIn('error', spec, tid)
                specs[tid] = spec
        self.assertEqual(sorted(specs), tools, 'every registered tool is describable through MCP')
        provider_tools = [t for t, s in specs.items() if any(x['name'] == 'provider' for x in s['settings'])]
        self.assertGreater(len(provider_tools), 40)
        for tid in provider_tools:
            block = specs[tid].get('contract', specs[tid]).get('provider') or specs[tid].get('provider')
            self.assertTrue(block and block['options'], f'{tid} explains its provider')
            self.assertTrue(all(o.get('setup') for o in block['options']), f'{tid} says how to set the provider up')
        self.assertIn('core.salesforce_output', [t['tool_id'] for t in found['tools']])
        self.assertEqual([(c['name'], c['auth'], c['credential_saved']) for c in connections['web_connections']],
                         [('Routing', 'none', True)])
        self.assertNotIn('secret', json.dumps(connections).lower().replace('secret access key', ''))
        self.assertEqual([p['id'] for p in providers['providers']], ['valhalla'])
        self.assertTrue(providers['providers'][0]['ready'], 'a Web/API connection exists, so Valhalla is ready')


if __name__ == '__main__':
    unittest.main()
