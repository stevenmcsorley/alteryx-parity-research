"""Salesforce Input/Output and ServiceNow Input against controlled fixtures of their documented REST APIs."""
import base64
import datetime as dt
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import flowengine.db.vault as vault_module
import flowengine.oauth as oauth
from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}
V = '/services/data/v62.0'


class St:
    def __init__(self):
        self.calls, self.sf = [], {}
        self.contacts = [{'attributes': {'type': 'Contact', 'url': f'{V}/sobjects/Contact/003{i}'}, 'Id': f'003{i:015d}',
                          'Name': f'Person {i}', 'CreatedDate': f'2026-0{i % 9 + 1}-01T08:30:00.000+0000', 'Birthdate': '1990-05-17',
                          'Account': {'attributes': {'type': 'Account'}, 'Name': f'Acme {i % 2}'},
                          'Cases': {'totalSize': 1, 'done': True, 'records': [{'attributes': {'type': 'Case'}, 'Subject': 'Help'}]}}
                         for i in range(5)]
        self.incidents = [{'sys_id': f's{i}', 'number': f'INC00{i}', 'priority': str(i % 3 + 1), 'short_description': f'Issue {i}',
                           'assigned_to': '' if i % 2 else 'u1'} for i in range(7)]


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
        if path == '/token':
            return self.send(200, {'access_token': 'sf', 'instance_url': 'https://x.my.salesforce.com', 'token_type': 'Bearer'})
        if path.startswith('/sn/'):
            if self.headers.get('Authorization') != 'Basic ' + base64.b64encode(b'integration:pw').decode():
                return self.send(401, {'error': {'message': 'User Not Authenticated'}})
            rows = [r for r in st.incidents if 'priority=1' not in q.get('sysparm_query', '') or r['priority'] == '1']
            off, lim = int(q['sysparm_offset']), int(q['sysparm_limit'])
            page = rows[off:off + lim]
            if q.get('sysparm_fields'):
                page = [{k: r[k] for k in q['sysparm_fields'].split(',')} for r in page]
            if q['sysparm_display_value'] == 'true':
                page = [{**r, **({'assigned_to': 'Ada Lovelace'} if r.get('assigned_to') == 'u1' else {})} for r in page]
            return self.send(200, {'result': page})
        if self.headers.get('Authorization') != 'Bearer sf':
            return self.send(401, [{'message': 'Session expired or invalid', 'errorCode': 'INVALID_SESSION_ID'}])
        if path in (f'{V}/query', f'{V}/queryAll'):
            assert q['q'].startswith('SELECT')
            return self.send(200, {'totalSize': 5, 'done': False, 'records': st.contacts[:2], 'nextRecordsUrl': f'{V}/query/01g-2'})
        if path == f'{V}/query/01g-2':
            return self.send(200, {'totalSize': 5, 'done': False, 'records': st.contacts[2:4], 'nextRecordsUrl': f'{V}/query/01g-4'})
        if path == f'{V}/query/01g-4':
            return self.send(200, {'totalSize': 5, 'done': True, 'records': st.contacts[4:]})
        if path.startswith(f'{V}/composite/sobjects'):
            if method == 'DELETE':
                ids = q['ids'].split(',')
                results = []
                for i in ids:
                    found = st.sf.pop(i, None) is not None
                    results.append({'id': i, 'success': found, 'errors': [] if found else
                                    [{'statusCode': 'ENTITY_IS_DELETED', 'message': 'entity is deleted', 'fields': []}]})
                return self.send(200, results)
            doc = json.loads(body)
            assert len(doc['records']) <= 200
            results = []
            for r in doc['records']:
                assert r['attributes']['type'] == 'Account'
                if not r.get('Name') and method == 'POST':
                    results.append({'success': False, 'errors': [{'statusCode': 'REQUIRED_FIELD_MISSING',
                                                                  'message': 'Required fields are missing: [Name]', 'fields': ['Name']}]})
                    continue
                if method == 'POST':
                    rid = f'001{len(st.sf):015d}'
                elif path.endswith('/Account/External_Id__c'):
                    rid = next((k for k, v in st.sf.items() if v.get('External_Id__c') == r['External_Id__c']), f'001X{len(st.sf):014d}')
                else:
                    rid = r['Id']
                st.sf.setdefault(rid, {}).update({k: v for k, v in r.items() if k != 'attributes'})
                results.append({'id': rid, 'success': True, 'errors': []})
            return self.send(200, results)
        return self.send(404, [{'message': f'no fixture {path}', 'errorCode': 'NOT_FOUND'}])

    def do_GET(self):
        self.go('GET')

    def do_POST(self):
        self.go('POST')

    def do_PATCH(self):
        self.go('PATCH')

    def do_DELETE(self):
        self.go('DELETE')


class CrmTests(unittest.TestCase):
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
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        store = ServiceConnectionStore()
        self.sf = store.save({'name': 'SF', 'kind': 'web', 'base_url': self.root, 'auth': 'oauth2', 'client_id': 'ck',
                              'token_url': f'{self.root}/token'}, secret=json.dumps({'client_secret': 'cs'})).id
        self.sn = store.save({'name': 'SN', 'kind': 'web', 'base_url': f'{self.root}/sn', 'auth': 'basic', 'username': 'integration'},
                             secret='pw').id

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

    def test_soql_follows_next_records_url_and_flattens_relationships(self):
        r = self.run_tool('core.salesforce_input', {'provider': 'salesforce', 'connection_id': self.sf,
                                                    'soql': 'SELECT Id, Name, Account.Name, (SELECT Subject FROM Cases) FROM Contact'})
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['x']['output']
        self.assertEqual(len(out), 5, 'three pages')
        self.assertEqual(out.schema.names, ['Id', 'Name', 'CreatedDate', 'Birthdate', 'Account.Name', 'Cases'])
        first = dict(zip(out.schema.names, next(out.rows())))
        self.assertEqual((first['CreatedDate'], first['Birthdate'], first['Account.Name']),
                         (dt.datetime(2026, 1, 1, 8, 30), dt.date(1990, 5, 17), 'Acme 0'))
        self.assertEqual(json.loads(first['Cases']), [{'Subject': 'Help'}])
        deleted = self.run_tool('core.salesforce_input', {'provider': 'salesforce', 'connection_id': self.sf,
                                                          'soql': 'SELECT Id FROM Contact', 'include_deleted': True})
        self.assertEqual(deleted.status, 'succeeded')
        self.assertTrue(any(c[1] == f'{V}/queryAll' for c in self.st.calls))

    def test_output_reports_each_record_and_batches_two_hundred(self):
        cfg = {'provider': 'salesforce', 'connection_id': self.sf, 'object': 'Account'}
        rows = [[f'Co {i}', f'E{i}'] for i in range(450)] + [['', 'E-bad']]
        r = self.run_tool('core.salesforce_output', cfg, WRITE, ([('Name', 'v_wstring'), ('External_Id__c', 'v_wstring')], rows))
        self.assertEqual(r.status, 'succeeded', r.error)
        out = list(r.outputs['x']['output'].rows())
        self.assertEqual(len([c for c in self.st.calls if c[0] == 'POST' and 'composite' in c[1]]), 3, '200 + 200 + 51')
        self.assertEqual(out[0][2:4], ('001000000000000000', True))
        self.assertEqual((out[-1][3], out[-1][4]), (False, 'REQUIRED_FIELD_MISSING: Required fields are missing: [Name] (Name)'))
        self.assertIn('failed', self.run_tool('core.salesforce_output', {**cfg, 'fail_on_errors': True}, WRITE,
                                              ([('Name', 'v_wstring')], [['']])).error['message'])
        up = self.run_tool('core.salesforce_output', {**cfg, 'mode': 'upsert', 'external_id_field': 'External_Id__c'}, WRITE,
                           ([('External_Id__c', 'v_wstring'), ('Name', 'v_wstring')], [['E1', 'Renamed']]))
        self.assertEqual(up.status, 'succeeded', up.error)
        self.assertTrue(any(c[1] == f'{V}/composite/sobjects/Account/External_Id__c' and c[0] == 'PATCH' for c in self.st.calls))
        some_id = next(k for k, v in self.st.sf.items() if v.get('External_Id__c') == 'E1')
        self.assertEqual(self.st.sf[some_id]['Name'], 'Renamed')
        r = self.run_tool('core.salesforce_output', {**cfg, 'mode': 'delete'}, WRITE, ([('Id', 'v_wstring')], [[some_id]]))
        self.assertIn('confirm_delete', r.error['message'])
        r = self.run_tool('core.salesforce_output', {**cfg, 'mode': 'delete', 'confirm_delete': True}, WRITE,
                          ([('Id', 'v_wstring')], [[some_id], ['001nope']]))
        self.assertEqual([x[-2] for x in r.outputs['x']['output'].rows()], [True, False])
        with self.assertRaises(WorkflowValidationError):
            self.run_tool('core.salesforce_output', {**cfg, 'mode': 'update'}, WRITE, ([('Name', 'v_wstring')], [['x']]))
        self.assertEqual(self.run_tool('core.salesforce_output', cfg, NET, ([('Name', 'v_wstring')], [['x']])).error['code'], 'permission-denied')

    def test_servicenow_pages_with_basic_auth_and_keeps_stored_values_as_text(self):
        cfg = {'provider': 'servicenow', 'connection_id': self.sn, 'table': 'incident', 'page_size': 3}
        out = self.run_tool('core.servicenow_input', cfg).outputs['x']['output']
        self.assertEqual(len(out), 7, 'three pages of three')
        self.assertTrue(all(f.type.value == 'v_wstring' for f in out.schema))
        self.assertEqual(dict(zip(out.schema.names, next(out.rows())))['priority'], '1')
        shown = self.run_tool('core.servicenow_input', {**cfg, 'fields': ['number', 'assigned_to'], 'display_values': True,
                                                         'query': 'priority=1'}).outputs['x']['output']
        self.assertEqual(shown.schema.names, ['number', 'assigned_to'])
        self.assertEqual(list(shown.rows())[0], ('INC000', 'Ada Lovelace'))
        self.assertIn('table name', self.run_tool('core.servicenow_input', {**cfg, 'table': 'incident?x=1'}).error['message'])


if __name__ == '__main__':
    unittest.main()
