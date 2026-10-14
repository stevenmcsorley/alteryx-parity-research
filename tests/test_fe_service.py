"""Editor service protocol tests (JSON-RPC over stdio and HTTP/SSE)."""
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from flowengine.service import EngineService, respond

from helpers import ROOT, edge, text_input, workflow


def call(method, params=None, rid=1, svc=None):
    return json.loads((svc or EngineService()).respond(
        json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params or {}})))


def slow_workflow(n=60000):
    return workflow([{'id': 'g', 'tool': 'core.generate_rows', 'config': {
        'field': 'n', 'type': 'int64', 'init': '1', 'condition': f'[n] <= {n}', 'loop': '[n] + 1',
        'max_rows_per_record': n + 1}},
        {'id': 'f', 'tool': 'core.formula', 'config': {'formulas': [{'field': 'sq', 'expression': '[n] * [n]'}]}},
        {'id': 's', 'tool': 'core.sort', 'config': {'keys': [{'field': 'sq', 'order': 'desc'}]}}],
        [edge('g', 'output', 'f', 'input'), edge('f', 'output', 's', 'input')])


class ServiceTests(unittest.TestCase):
    def test_info_tools_catalog_profiles(self):
        self.assertEqual(call('engine.info')['result']['protocol'], 2)
        tools = call('tools.list')['result']
        self.assertTrue(any(t['tool_id'] == 'core.filter' for t in tools))
        cat = call('catalog.list')['result']
        self.assertEqual(len(cat), 323)
        flt = next(c for c in cat if c['id'] == 'preparation:filter')
        self.assertEqual((flt['kernel'], flt['unavailable_reason']), ('core.filter', None))
        geo = next(c for c in cat if c['id'] == 'address:us-geocoder')
        self.assertIsNone(geo['kernel'])
        self.assertIn('licensed data', geo['unavailable_reason'])
        self.assertEqual({p['name'] for p in call('profiles.list')['result']},
                         {'ayx-2026.2-e1', 'ayx-2026.2-e2', 'ayx-2026.1-e1'})

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

    def test_typed_application_answers_bind_actions_for_real_execution(self):
        wf = workflow([text_input('t', [('a', 'int32')], [[3]]),
                       {'id': 'f', 'tool': 'core.formula', 'config': {'formulas': [
                           {'field': 'b', 'expression': '[a] + 1'}]}}],
                      [edge('t', 'output', 'f', 'input')])
        wf['metadata'] = {'application': {
            'questions': [{'id': 'increment', 'label': 'Increment', 'type': 'choice',
                           'options': ['[a] + 1', '[a] + 4'], 'default': '[a] + 1'}],
            'actions': [{'question': 'increment', 'node_id': 'f', 'config_path': ['formulas', '0', 'expression']}],
        }}
        service = EngineService()
        form = service.handle('workflow.application', {'workflow': wf})
        self.assertEqual(form['questions'][0]['options'], ['[a] + 1', '[a] + 4'])
        result = service.handle('workflow.run', {'workflow': wf, 'base_dir': str(ROOT),
                                                'application_values': {'increment': '[a] + 4'}})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['previews']['f']['output']['rows'], [['3', '7']])
        # Actions run on a private per-run document and do not rewrite the saved workflow.
        self.assertEqual(wf['nodes'][1]['config']['formulas'][0]['expression'], '[a] + 1')

    def test_application_rejects_bad_answers_and_unsafe_action_targets(self):
        wf = workflow([text_input('t', [('a', 'int32')], [[3]])], [])
        wf['metadata'] = {'application': {
            'questions': [{'id': 'enabled', 'label': 'Enabled', 'type': 'boolean', 'default': False}],
            'actions': [{'question': 'enabled', 'node_id': 'missing', 'config_path': ['x']}],
        }}
        invalid = call('run.start', {'workflow': wf, 'base_dir': str(ROOT), 'application_values': {'enabled': True}})
        self.assertEqual(invalid['result']['status'], 'invalid')
        self.assertEqual(invalid['result']['problems'][0]['code'], 'application-invalid')
        wf['metadata']['application']['actions'] = []
        invalid = call('workflow.validate', {'workflow': wf, 'base_dir': str(ROOT),
                                             'application_values': {'enabled': 'yes'}})
        self.assertEqual(invalid['error']['data']['code'], 'application-input-invalid')

    def test_application_values_are_not_persisted_in_run_history(self):
        with tempfile.TemporaryDirectory() as d:
            service = EngineService(history_path=Path(d) / 'history.sqlite')
            wf = workflow([text_input('t', [('a', 'int32')], [[3]]),
                           {'id': 'f', 'tool': 'core.formula', 'config': {'formulas': [
                               {'field': 'b', 'expression': '[a] + 1'}]}}],
                          [edge('t', 'output', 'f', 'input')])
            wf['metadata'] = {'application': {
                'questions': [{'id': 'calculation', 'label': 'Calculation', 'type': 'text', 'secret': True,
                               'required': True}],
                'actions': [{'question': 'calculation', 'node_id': 'f', 'config_path': ['formulas', '0', 'expression']}],
            }}
            form = service.handle('workflow.application', {'workflow': wf})
            self.assertTrue(form['questions'][0]['secret'])
            self.assertNotIn('default', form['questions'][0])
            service.handle('workflow.run', {'workflow': wf, 'base_dir': str(ROOT),
                                             'application_values': {'calculation': '[a] + 4'}})
            run_id = service.handle('history.list', {'limit': 1})[0]['run_id']
            stored = service.handle('history.get', {'run_id': run_id})['workflow']
            self.assertEqual(stored['nodes'][1]['config']['formulas'][0]['expression'], '[a] + 1')
            self.assertNotIn('[a] + 4', json.dumps(stored))

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

    def test_async_run_streams_events_and_finishes(self):
        got, done = [], threading.Event()

        def notify(method, params):
            got.append((method, params))
            if method == 'run.finished':
                done.set()
        svc = EngineService(notify=notify)
        wf = workflow([text_input('t', [('a', 'int32')], [[1], [2]]),
                       {'id': 'c', 'tool': 'core.count_records', 'config': {}}], [edge('t', 'output', 'c', 'input')])
        rid = call('run.start', {'workflow': wf, 'base_dir': str(ROOT)}, svc=svc)['result']['run_id']
        self.assertTrue(done.wait(30))
        types = [p['type'] for m, p in got if m == 'run.event']
        self.assertEqual(types[0], 'run-started')
        self.assertIn('node-completed', types)
        res = call('run.result', {'run_id': rid}, svc=svc)['result']
        self.assertEqual(res['status'], 'succeeded')
        self.assertEqual(res['result']['previews']['c']['output']['rows'], [['2']])

    def test_async_run_reports_invalid_immediately(self):
        r = call('run.start', {'workflow': workflow([{'id': 'x', 'tool': 'core.nope', 'config': {}}], []),
                               'base_dir': str(ROOT)})['result']
        self.assertEqual(r['status'], 'invalid')
        self.assertEqual(r['problems'][0]['code'], 'tool-unavailable')

    def test_cancel_running_workflow(self):
        done = threading.Event()
        svc = EngineService(notify=lambda m, p: done.set() if m == 'run.finished' else None)
        rid = call('run.start', {'workflow': slow_workflow(), 'base_dir': str(ROOT)}, svc=svc)['result']['run_id']
        self.assertTrue(call('run.cancel', {'run_id': rid}, svc=svc)['result']['run_id'] == rid)
        self.assertTrue(done.wait(60))
        self.assertEqual(call('run.result', {'run_id': rid}, svc=svc)['result']['status'], 'cancelled')

    def test_history_through_service(self):
        with tempfile.TemporaryDirectory() as d:
            svc = EngineService(history_path=Path(d) / 'h.sqlite')
            wf = workflow([text_input('t', [('a', 'int32')], [[1]])], [], name='svc-history')
            call('workflow.run', {'workflow': wf, 'base_dir': str(ROOT)}, svc=svc)
            runs = call('history.list', svc=svc)['result']
            self.assertEqual(runs[0]['workflow_name'], 'svc-history')
            self.assertEqual(call('history.get', {'run_id': runs[0]['run_id']}, svc=svc)['result']['workflow'], wf)

    def test_file_access_limited_to_workflow_documents(self):
        with tempfile.TemporaryDirectory() as d:
            ok = call('file.write_text', {'path': str(Path(d) / 'a.flow.json'), 'text': '{}'})
            self.assertIn('result', ok)
            self.assertEqual(call('file.read_text', {'path': str(Path(d) / 'a.flow.json')})['result']['text'], '{}')
            bad = call('file.write_text', {'path': str(Path(d) / 'evil.txt'), 'text': 'x'})
            self.assertEqual(bad['error']['data']['code'], 'bad-request')

    def test_stdio_process(self):
        reqs = '\n'.join(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': 'engine.info'}) for i in range(3)) + '\n'
        p = subprocess.run([sys.executable, '-m', 'flowengine', 'serve'], input=reqs, capture_output=True, text=True,
                           cwd=ROOT, timeout=60)
        lines = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([l['id'] for l in lines], [0, 1, 2])

    def test_http_transport_with_sse(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen([sys.executable, '-m', 'flowengine', 'serve', '--http', str(port)], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    urllib.request.urlopen(base + '/health', timeout=1)
                    break
                except OSError:
                    time.sleep(0.1)
            events = []
            stream = urllib.request.urlopen(base + '/events', timeout=30)

            def read_events():
                for raw in stream:
                    line = raw.decode('utf-8').strip()
                    if line.startswith('data: '):
                        events.append(json.loads(line[6:]))
                        if events[-1]['method'] == 'run.finished':
                            return
            t = threading.Thread(target=read_events, daemon=True)
            t.start()
            time.sleep(0.2)
            wf = workflow([text_input('t', [('a', 'int32')], [[1]])], [])
            req = urllib.request.Request(base + '/rpc', data=json.dumps(
                {'jsonrpc': '2.0', 'id': 7, 'method': 'run.start', 'params': {'workflow': wf, 'base_dir': str(ROOT)}}).encode(),
                headers={'content-type': 'application/json'})
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            self.assertEqual(resp['id'], 7)
            t.join(30)
            self.assertEqual(events[-1]['params']['status'], 'succeeded')
            self.assertTrue(any(e['method'] == 'run.event' and e['params']['type'] == 'node-completed' for e in events))
        finally:
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
