"""Batch 10: Email, API Output, Jupyter Flow, Map Input, Transpose In-DB and Visual Layout."""
import base64
import email
import json
import os
import socketserver
import tempfile
import threading
import unittest
from email import policy
from pathlib import Path
from unittest import mock

import flowengine.db.vault as vault_module
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore
from flowengine.workflow_effects import workflow_effects

from helpers import ROOT, edge, run_tool, run_tool_result, text_input, workflow
from test_indb import InDBTestCase
from test_service_connections import MemoryVault

SEND = {'allow_network': True, 'allow_remote_writes': True}


# =========================================================================== fake SMTP server

class _SMTPHandler(socketserver.StreamRequestHandler):
    def reply(self, line):
        self.wfile.write((line + '\r\n').encode())

    def handle(self):
        server = self.server
        self.reply('220 fake ESMTP')
        sender, rcpts = None, []
        while True:
            line = self.rfile.readline().decode().rstrip('\r\n')
            if not line:
                return
            verb = line.split(' ', 1)[0].upper()
            if verb in ('EHLO', 'HELO'):
                self.wfile.write(b'250-fake\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n')
            elif verb == 'AUTH':
                parts = line.split(' ')
                token = parts[2] if len(parts) > 2 else None
                if token is None:
                    self.reply('334 ')
                    token = self.rfile.readline().decode().strip()
                _, user, password = base64.b64decode(token).decode().split('\0')
                server.logins.append((user, password))
                self.reply('235 ok' if password == 'right' else '535 bad credentials')
            elif verb == 'MAIL':
                sender, rcpts = line.split(':', 1)[1].strip().split()[0].strip('<>'), []
                self.reply('250 ok')
            elif verb == 'RCPT':
                rcpts.append(line.split(':', 1)[1].strip().strip('<>'))
                self.reply('250 ok')
            elif verb == 'DATA':
                self.reply('354 go')
                data = []
                while True:
                    chunk = self.rfile.readline().decode()
                    if chunk in ('.\r\n', '.\n'):
                        break
                    data.append(chunk[1:] if chunk.startswith('..') else chunk)
                server.messages.append((sender, rcpts, ''.join(data)))
                self.reply('250 queued')
            elif verb == 'RSET':
                self.reply('250 ok')
            elif verb == 'QUIT':
                self.reply('221 bye')
                return
            else:
                self.reply('502 unknown')


class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), _SMTPHandler)
        self.messages, self.logins = [], []


PEOPLE = ([('name', 'v_wstring'), ('email', 'v_wstring'), ('total', 'double'), ('file', 'v_wstring')],
          [['Ada', 'ada@example.com', 12.5, None], ['Ben', 'Ben <ben@example.org>', 7.0, None]])


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.smtp = FakeSMTP()
        threading.Thread(target=self.smtp.serve_forever, daemon=True).start()
        self.home = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self.home.name}),
                        mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.smtp.shutdown()
        self.smtp.server_close()
        self.home.cleanup()

    def config(self, **extra):
        return {'smtp_host': '127.0.0.1', 'port': self.smtp.server_address[1], 'encryption': 'none',
                'from': 'Reports <reports@example.com>', 'to_field': 'email', 'bcc': 'audit@example.com',
                'subject': 'Statement for [name]', 'body': 'Hello [name], you owe [total]. [unknown] stays.', **extra}

    def send(self, config, rows=PEOPLE, limits=SEND):
        doc = workflow([text_input('d', *rows), {'id': 'e', 'tool': 'core.email', 'config': config}],
                       [edge('d', 'output', 'e', 'input')])
        return run_workflow(doc, base_dir=ROOT, limits=limits)

    def test_one_message_per_record_with_placeholders_bcc_and_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / 'report.csv'
            report.write_text('a,b\n1,2\n')
            r = self.send(self.config(attachments=[str(report)], body_format='html',
                                      body='<p>Hello <b>[name]</b></p>'))
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.output('e').column('EmailStatus'), ['sent', 'sent'])
        self.assertEqual(len(self.smtp.messages), 2)
        sender, rcpts, raw = self.smtp.messages[1]
        self.assertEqual(sender, 'reports@example.com')
        self.assertEqual(rcpts, ['ben@example.org', 'audit@example.com'])
        msg = email.message_from_string(raw, policy=policy.default)
        self.assertEqual(msg['Subject'], 'Statement for Ben')
        self.assertEqual(msg['To'], 'Ben <ben@example.org>')
        self.assertIsNone(msg['Bcc'])  # Bcc recipients are only in the envelope
        self.assertEqual(msg.get_body(('html',)).get_content().strip(), '<p>Hello <b>Ben</b></p>')
        self.assertEqual(msg.get_body(('plain',)).get_content().strip(), 'Hello Ben')
        attachment = next(msg.iter_attachments())
        self.assertEqual(attachment.get_filename(), 'report.csv')
        self.assertEqual(attachment.get_content().replace('\r\n', '\n'), 'a,b\n1,2\n')
        self.assertEqual(r.output('e').column('MessageID')[1], msg['Message-ID'])

    def test_text_templates_keep_unknown_placeholders(self):
        r = self.send(self.config())
        self.assertTrue(r.ok, r.error)
        msg = email.message_from_string(self.smtp.messages[0][2], policy=policy.default)
        self.assertEqual(msg.get_content().strip(), 'Hello Ada, you owe 12.5. [unknown] stays.')

    def test_nothing_is_sent_when_any_record_is_invalid(self):
        rows = (PEOPLE[0], PEOPLE[1] + [['Bad', 'not-an-address', 1.0, None]])
        r = self.send(self.config(), rows=rows)
        self.assertFalse(r.ok)
        self.assertIn("'not-an-address'", r.error['message'])
        self.assertEqual(self.smtp.messages, [])
        missing = self.send(self.config(attachments=['no/such/file.pdf']))
        self.assertIn('was not found', missing.error['message'])
        injected = self.send(self.config(subject='Hi\r\nBcc: victim@example.com'))
        self.assertFalse(injected.ok)
        self.assertEqual(self.smtp.messages, [])

    def test_needs_network_and_remote_write_permission_and_respects_disabled(self):
        denied = self.send(self.config(), limits={'allow_network': True})
        self.assertEqual(denied.error['code'], 'permission-denied')
        off = self.send(self.config(enabled=False), limits={})
        self.assertTrue(off.ok, off.error)
        self.assertEqual(off.output('e').column('EmailStatus'), ['disabled', 'disabled'])
        self.assertEqual(self.smtp.messages, [])
        effects = workflow_effects(workflow([{'id': 'e', 'tool': 'core.email', 'config': self.config()}], []), ROOT)
        self.assertEqual([x['node_id'] for x in effects['network_requests']], ['e'])
        self.assertEqual([x['node_id'] for x in effects['remote_writes']], ['e'])

    def test_saved_smtp_connection_logs_in(self):
        store = ServiceConnectionStore()
        port = self.smtp.server_address[1]
        good = store.save({'name': 'Mail', 'kind': 'smtp', 'base_url': f'smtp://127.0.0.1:{port}', 'auth': 'basic',
                           'username': 'robot'}, secret='right').id
        cfg = {k: v for k, v in self.config().items() if k not in ('smtp_host', 'port', 'encryption')}
        r = self.send({**cfg, 'connection_id': good})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.smtp.logins, [('robot', 'right')])
        bad = store.save({'name': 'Wrong', 'kind': 'smtp', 'base_url': f'smtp://127.0.0.1:{port}', 'auth': 'basic',
                          'username': 'robot'}, secret='wrong').id
        r = self.send({**cfg, 'connection_id': bad})
        self.assertIn('rejected the username or password', r.error['message'])
        with self.assertRaises(Exception):
            store.save({'name': 'Web', 'kind': 'smtp', 'base_url': 'https://mail.example.com', 'auth': 'basic',
                        'username': 'x'}, secret='y')


# =========================================================================== API Output

class APIOutputTests(unittest.TestCase):
    def test_quoted_csv_messages(self):
        fields = [('name', 'v_wstring'), ('note', 'v_wstring'), ('blob', 'blob')]
        _, r = run_tool('core.api_output', {}, {'input': (fields, [['Ada', 'said "hi"', 'AAECAw=='], ['x' * 300, None, None]])})
        lines = [e['message'] for e in r.events if e.get('type') == 'info' and e.get('node_id') == 'subject']
        self.assertEqual(lines[0], '"name","note","blob"')
        self.assertEqual(lines[1], '"Ada","said ""hi""","8 Bytes"')
        self.assertEqual(len(lines[2]), 255)


# =========================================================================== Jupyter Flow

def notebook(*cells):
    return {'nbformat': 4, 'nbformat_minor': 5, 'metadata': {},
            'cells': [{'cell_type': 'code', 'source': c, 'metadata': {}, 'outputs': [], 'execution_count': None}
                      if not c.startswith('# md') else {'cell_type': 'markdown', 'source': c, 'metadata': {}} for c in cells]}


class JupyterFlowTests(unittest.TestCase):
    def run_nb(self, nb, inputs, limits=None):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'analysis.ipynb'
            path.write_text(json.dumps(nb))
            nodes, edges = [], []
            for k, (fields, rows) in enumerate(inputs):
                nodes.append(text_input(f'in{k}', fields, rows))
                edges.append(edge(f'in{k}', 'output', 'nb', 'inputs'))
            nodes.append({'id': 'nb', 'tool': 'core.jupyter_flow', 'config': {'notebook': str(path)}})
            return run_workflow(workflow(nodes, edges), base_dir=Path(d),
                                limits=limits if limits is not None else {'allow_processes': True})

    def test_markers_map_connections_and_outputs(self):
        nb = notebook('import pandas as pd\n%matplotlib inline',
                      '# md notes',
                      '#ayx_input=#2\nprices = pd.DataFrame({\n    "sku": ["dev"],\n    "price": [0.0]})\n'
                      '#ayx_input\nsales = pd.read_csv("dev.csv")',
                      'joined = sales.merge(prices, on="sku")\njoined["value"] = joined["qty"] * joined["price"]\n'
                      '#ayx_output\nresult = joined[["sku", "value"]]\n#ayx_output=3\ntotal = None\n'
                      'total = pd.Series([joined["value"].sum()], name="total")')
        sales = ([('sku', 'v_wstring'), ('qty', 'int64')], [['a', 2], ['b', 3]])
        prices = ([('sku', 'v_wstring'), ('price', 'double')], [['a', 1.5], ['b', 2.0]])
        r = self.run_nb(nb, [sales, prices])
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.output('nb', 'output1').to_dicts(), [{'sku': 'a', 'value': 3.0}, {'sku': 'b', 'value': 6.0}])
        self.assertEqual(r.output('nb', 'output3').to_dicts(), [{'total': 9.0}])
        self.assertEqual(len(r.output('nb', 'output2')), 0)
        self.assertTrue(any('magic' in w['message'] for w in r.warnings()))

    def test_needs_permission_and_connected_inputs(self):
        nb = notebook('#ayx_input=#2\ndf = None\n#ayx_output\nout = df')
        r = self.run_nb(nb, [([('a', 'int64')], [[1]])])
        self.assertIn('reads input(s) [2]', r.error['message'])
        denied = self.run_nb(notebook('#ayx_output\nout = 1'), [], limits={})
        self.assertEqual(denied.error['code'], 'permission-denied')
        bad = self.run_nb(notebook('#ayx_output\nprint("no assignment")'), [])
        self.assertIn('must be followed by an assignment', bad.error['message'])


# =========================================================================== Map Input

class MapInputTests(unittest.TestCase):
    def test_draw_and_select(self):
        out, _ = run_tool('core.map_input', {'objects': [
            {'label': 'Depot', 'geometry': 'POINT (-1.5 53.8)'},
            {'geometry': '{"type":"LineString","coordinates":[[0,0],[1,1]]}'}]}, {})
        self.assertEqual(out['output'].to_dicts(), [
            {'Label': 'Depot', 'Type': 'Point', 'SpatialObj': 'POINT (-1.5 53.8)'},
            {'Label': 'Object 2', 'Type': 'LineString', 'SpatialObj': 'LINESTRING (0 0, 1 1)'}])
        bad = run_tool_result('core.map_input', {'objects': [{'geometry': 'POINT (53.8 -100)'}]}, {})
        self.assertIn('outside longitude', bad.error['message'])
        ref = ([('name', 'v_wstring'), ('geom', 'v_wstring')],
               [['A', 'POINT (0 0)'], ['B', 'POINT (5 5)'], ['C', 'POLYGON ((9 9, 11 9, 11 11, 9 9))']])
        picked, _ = run_tool('core.map_input', {'mode': 'select', 'geometry_field': 'geom', 'label_field': 'name',
                                                'bbox': [4, 4, 10, 10]}, {'reference': ref})
        self.assertEqual(picked['output'].column('Label'), ['B', 'C'])
        named, _ = run_tool('core.map_input', {'mode': 'select', 'geometry_field': 'geom', 'label_field': 'name',
                                               'labels': ['A', 'C'], 'format': 'geojson'}, {'reference': ref})
        self.assertEqual(named['output'].column('Label'), ['A', 'C'])
        self.assertTrue(named['output'].column('SpatialObj')[0].startswith('{"type":"Point"'))


# =========================================================================== Visual Layout

class VisualLayoutTests(unittest.TestCase):
    def test_rows_place_elements_side_by_side(self):
        text = lambda nid, t: {'id': nid, 'tool': 'core.report_text', 'config': {'text': t}}
        nodes = [text_input('d', [('x', 'int64')], [[1]]), text('t1', 'Title'), text('t2', 'Left'), text('t3', 'Right'),
                 text('t4', 'Unused'), {'id': 'v', 'tool': 'core.visual_layout', 'config': {
                     'rows': [[{'input': 1}], [{'input': 2}, {'input': 3}]], 'gap': 8}}]
        edges = [edge('d', 'output', t, 'input') for t in ('t1', 't2', 't3', 't4')] + \
                [edge(t, 'output', 'v', 'inputs') for t in ('t1', 't2', 't3', 't4')]
        r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        doc = json.loads(r.output('v').column('Report')[0])
        self.assertEqual(doc['kind'], 'layout')
        self.assertEqual([i['kind'] for i in doc['items']], ['text', 'layout'])
        self.assertEqual([c['text'] for c in doc['items'][1]['items']], ['Left', 'Right'])
        self.assertEqual(doc['items'][1]['direction'], 'horizontal')
        self.assertTrue(any('#4:1' in w['message'] for w in r.warnings()))
        with tempfile.TemporaryDirectory() as d:
            nodes.append({'id': 'r', 'tool': 'core.render', 'config': {'path': str(Path(d) / 'out.html')}})
            r = run_workflow(workflow(nodes, edges + [edge('v', 'output', 'r', 'input')]), base_dir=ROOT,
                             limits={'allow_file_writes': True})
            self.assertTrue(r.ok, r.error)
            html = (Path(d) / 'out.html').read_text()
            self.assertLess(html.index('Left'), html.index('Right'))

    def test_bad_placement_is_reported(self):
        r = run_tool_result('core.visual_layout', {'rows': [[{'input': 2}]]},
                            {'inputs': [([('Report', 'v_wstring')], [['{"format":"flowbench.report.v1","kind":"text","text":"a"}']])]})
        self.assertIn('does not exist', r.error['message'])


# =========================================================================== Transpose In-DB

class TransposeInDBTests(InDBTestCase):
    def test_union_of_casts_with_parameters_in_marker_order(self):
        nodes = self.source() + [{'id': 't', 'tool': 'core.transpose_indb',
                                  'config': {'key_fields': ['region'], 'data_fields': ['amount', 'units']}}]
        edges = [edge('local', 'output', 'in', 'input'), edge('in', 'output', 't', 'input')]
        result = self.run_doc(nodes, edges)
        self.assertTrue(result.ok, result.error)
        s = self.stream_of(result, 't')
        self.assertEqual([f.name for f in s.fields], ['region', 'Name', 'Value'])
        self.assertEqual(s.sql.count('UNION ALL'), 1)
        self.assertEqual(s.params[0], 'amount')
        self.assertEqual(s.params[1 + 9], 'units')  # each branch repeats the 9 inline VALUES parameters
        self.assertIn('AS text)', s.sql)
        clash = self.run_doc(self.source() + [{'id': 't', 'tool': 'core.transpose_indb',
                                               'config': {'key_fields': ['amount'], 'data_fields': ['amount']}}], edges)
        self.assertFalse(clash.ok)


if __name__ == '__main__':
    unittest.main()
