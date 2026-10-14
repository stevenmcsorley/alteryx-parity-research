"""Real-engine tests. Start deterministic containers with ``python tests/integration/db_env.py up``."""
import json
import os
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path

from flowengine.db.connections import ConnectionStore
from flowengine.executor import run_workflow
from flowengine.service import EngineService

ENV_FILE = Path(__file__).with_name('.db_env.json')


@unittest.skipUnless(ENV_FILE.is_file(), 'real database containers are not provisioned')
class DatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env_before = {k: os.environ.get(k) for k in ('FLOWENGINE_HOME', 'FLOWBENCH_VAULT')}
        cls.root = tempfile.TemporaryDirectory(prefix='flowbench-db-test-')
        cls.home = Path(cls.root.name)
        os.environ['FLOWENGINE_HOME'] = str(cls.home)
        os.environ['FLOWBENCH_VAULT'] = 'env'
        from flowengine.db import vault as vault_module
        vault_module._VAULT = None
        cfg = json.loads(ENV_FILE.read_text(encoding='utf-8'))
        cls.connections = {}
        store = ConnectionStore()
        for dialect in ('postgresql', 'sqlserver'):
            server = cfg[dialect]
            cred = server['reader']
            cid = 'test_' + dialect
            os.environ['FLOWBENCH_SECRET_' + cid.upper()] = cred['password']
            connection = store.save({'id': cid, 'name': dialect, 'dialect': dialect, 'host': server['host'],
                                     'port': server['port'], 'database': server['database'], 'user': cred['user'],
                                     'tls': 'require' if dialect == 'postgresql' else 'disable',
                                     'ca_file': server.get('ca_file'), 'query_timeout': 30})
            cls.connections[dialect] = connection
            writer = server['writer']
            writer_id = 'test_writer_' + dialect
            os.environ['FLOWBENCH_SECRET_' + writer_id.upper()] = writer['password']
            cls.connections[writer_id] = store.save({'id': writer_id, 'name': dialect + ' writer',
                'dialect': dialect, 'host': server['host'], 'port': server['port'],
                'database': server['database'], 'user': writer['user'],
                'tls': 'require' if dialect == 'postgresql' else 'disable',
                'ca_file': server.get('ca_file'), 'allow_writes': True, 'query_timeout': 30})
        ms = cfg['sqlserver']
        os.environ['FLOWBENCH_SECRET_TEST_SQLSERVER_ADMIN'] = ms['admin_password']
        cls.admin_connection = store.save({'id': 'test_sqlserver_admin', 'name': 'sqlserver admin',
            'dialect': 'sqlserver', 'host': ms['host'], 'port': ms['port'], 'database': ms['database'],
            'user': 'sa', 'tls': 'disable', 'allow_writes': True, 'query_timeout': 30})

    @classmethod
    def tearDownClass(cls):
        for k, value in cls.env_before.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value
        for k in list(os.environ):
            if k.startswith('FLOWBENCH_SECRET_TEST_'):
                os.environ.pop(k, None)
        from flowengine.db import vault as vault_module
        vault_module._VAULT = None
        cls.root.cleanup()

    def test_connection_rpc_browses_real_metadata_and_preview(self):
        svc = EngineService()
        c = self.connections['postgresql']
        self.assertTrue(svc.handle('database.test', {'connection_id': c.id})['ok'])
        self.assertIn('sales', svc.handle('database.schemas', {'connection_id': c.id}))
        names = {x['name'] for x in svc.handle('database.tables', {'connection_id': c.id, 'schema': 'sales'})}
        self.assertIn('customers', names)
        preview = svc.handle('database.preview', {'connection_id': c.id, 'mode': 'table', 'schema': 'sales',
                                                   'table': 'customers', 'limit': 50})
        self.assertEqual(preview['total_rows'], 41)
        self.assertIn('Zoë Ünïcode', str(preview['rows']))

    def test_live_cross_database_join_and_rerun_refresh(self):
        pg, ms = self.connections['postgresql'], self.connections['sqlserver']
        nodes = [
            {'id': 'customers', 'tool': 'core.db_input', 'config': {'connection_id': pg.id, 'mode': 'table',
             'schema': 'sales', 'table': 'customers', 'order_by': [{'field': 'customer_id'}]}},
            {'id': 'orders', 'tool': 'core.db_input', 'config': {'connection_id': ms.id, 'mode': 'table',
             'schema': 'ops', 'table': 'orders', 'order_by': [{'field': 'order_id'}]}},
            {'id': 'join', 'tool': 'core.join', 'config': {'by': 'fields',
             'keys': [{'left': 'customer_id', 'right': 'customer_id'}], 'drop_right_keys': True,
             'left_fields': ['customer_id', 'name', 'region'],
             'right_fields': ['order_id', 'customer_id', 'qty', 'unit_price']}},
            {'id': 'browse', 'tool': 'core.browse', 'config': {}},
        ]
        doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'name': 'database integration', 'nodes': nodes,
               'edges': [{'from': {'node': 'customers', 'anchor': 'output'}, 'to': {'node': 'join', 'anchor': 'left'}},
                         {'from': {'node': 'orders', 'anchor': 'output'}, 'to': {'node': 'join', 'anchor': 'right'}},
                         {'from': {'node': 'join', 'anchor': 'joined'}, 'to': {'node': 'browse', 'anchor': 'input'}}]}
        # This row is absent for customer 1 before the live change and appears after it.
        from flowengine.tools.database import open_dialect
        cfg, d = open_dialect(self.admin_connection.id)
        conn = d.connect(read_only=False)
        try:
            cur = conn.cursor()
            cur.execute('DELETE FROM ops.orders WHERE order_id = %s', (9001,))
            conn.commit()
        finally:
            conn.close()
        first = run_workflow(doc, base_dir=self.home, retain='all')
        self.assertTrue(first.ok, first.error)
        joined = first.browse['browse']
        self.assertEqual(len(joined), 110)
        self.assertEqual(first.manifest['database_queries'], [])
        self.assertTrue(any(e['type'] == 'pushdown-fallback' and e.get('node_id') == 'join'
                            for e in first.events), repr(first.events))
        self.assertIn('Zoë Ünïcode', [r[1] for r in joined.rows()])
        conn = d.connect(read_only=False)
        try:
            cur = conn.cursor()
            cur.execute('IF NOT EXISTS (SELECT 1 FROM ops.orders WHERE order_id = 9001) '
                        'INSERT INTO ops.orders VALUES (%s,%s,%s,%s,%s,%s)',
                        (9001, 1, '2026-01-01', 1, 2.50, 'web'))
            conn.commit()
        finally:
            conn.close()
        second = run_workflow(doc, base_dir=self.home, retain='all')
        self.assertTrue(second.ok, second.error)
        self.assertEqual(len(second.browse['browse']), 111)
        self.assertEqual(first.manifest['nodes']['orders']['outputs']['output']['rows'], 120)
        self.assertEqual(second.manifest['nodes']['orders']['outputs']['output']['rows'], 121)
        conn = d.connect(read_only=False)
        try:
            cur = conn.cursor()
            cur.execute('DELETE FROM ops.orders WHERE order_id = %s', (9001,))
            conn.commit()
        finally:
            conn.close()

    def test_type_matrix_preserves_null_unicode_decimal_and_binary_values(self):
        for dialect, schema in (('postgresql', 'sales'), ('sqlserver', 'ops')):
            conn = self.connections[dialect]
            doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
                {'id': 'db', 'tool': 'core.db_input', 'config': {'connection_id': conn.id, 'mode': 'table',
                 'schema': schema, 'table': 'type_matrix', 'order_by': [{'field': 'id'}]}},
                {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
                'edges': [{'from': {'node': 'db', 'anchor': 'output'},
                           'to': {'node': 'browse', 'anchor': 'input'}}]}
            result = run_workflow(doc, base_dir=self.home, retain='all')
            self.assertTrue(result.ok, result.error)
            t = result.browse['browse']
            self.assertEqual(len(t), 2)
            self.assertEqual(t.column('id'), [1, 2])
            self.assertTrue(any(v == 'zoë ✓' for name in t.schema.names for v in t.column(name)))
            binary = t.column('bin')
            self.assertEqual(binary[0], b'\x00\xff\x10')
            self.assertIsNone(binary[1])
            decimals = [t.column(n)[0] for n in t.schema.names if n in ('dec', 'd')]
            self.assertEqual(str(decimals[0]), '12345678.1234')
            self.assertIsNone(t.column('i16')[1] if dialect == 'postgresql' else t.column('tiny')[1])

    def test_real_filter_select_and_summarize_sql_executes_and_matches_local(self):
        conn = self.connections['postgresql']
        doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
            {'id': 'source', 'tool': 'core.db_input', 'config': {'connection_id': conn.id, 'mode': 'table',
             'schema': 'sales', 'table': 'customers'}},
            {'id': 'select', 'tool': 'core.select', 'config': {'fields': [
             {'field': 'customer_id'}, {'field': 'region'}, {'field': 'credit'}], 'unknown_fields': 'exclude'}},
            {'id': 'filter', 'tool': 'core.filter', 'config': {'expression': '[region] = "North"'}},
            {'id': 'sum', 'tool': 'core.summarize', 'config': {'actions': [
             {'field': 'region', 'op': 'group_by'}, {'field': 'credit', 'op': 'sum', 'output': 'credit_total'},
             {'field': 'customer_id', 'op': 'count', 'output': 'customers'}]}},
            {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
            'edges': [
                {'from': {'node': 'source', 'anchor': 'output'}, 'to': {'node': 'select', 'anchor': 'input'}},
                {'from': {'node': 'select', 'anchor': 'output'}, 'to': {'node': 'filter', 'anchor': 'input'}},
                {'from': {'node': 'filter', 'anchor': 'true'}, 'to': {'node': 'sum', 'anchor': 'input'}},
                {'from': {'node': 'sum', 'anchor': 'output'}, 'to': {'node': 'browse', 'anchor': 'input'}}]}
        pushed = run_workflow(doc, base_dir=self.home, retain='all', enable_pushdown=True)
        local = run_workflow(doc, base_dir=self.home, retain='all', enable_pushdown=False)
        self.assertTrue(pushed.ok, pushed.error)
        self.assertTrue(local.ok, local.error)
        self.assertEqual(pushed.browse['browse'], local.browse['browse'])
        self.assertEqual(len(pushed.manifest['database_queries']), 1, repr(pushed.events))
        query = pushed.manifest['database_queries'][0]
        self.assertEqual(query['nodes'], ['source', 'select', 'filter', 'sum'])
        self.assertEqual(query['status'], 'succeeded')
        self.assertIn('%s', query['sql'])
        self.assertEqual(query['parameters'], [{'index': 0, 'value': '<redacted>'}])
        self.assertEqual(query['steps'][-1], 'aggregate group_by(region), sum(credit), count(customer_id)')
        self.assertTrue(query['transferred_bytes_estimate'] > 0)
        self.assertTrue(all(node_stats['pushed_to_database'] for node_stats in
                            (pushed.manifest['nodes'][n] for n in query['nodes'])))
        self.assertTrue(any(event['type'] == 'database-pushdown' for event in pushed.events))
        self.assertTrue(pushed.manifest['nodes']['sum']['outputs']['output']['digest'])

    def test_sqlserver_filter_select_and_summarize_pushdown_matches_local(self):
        conn = self.connections['sqlserver']
        doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
            {'id': 'source', 'tool': 'core.db_input', 'config': {'connection_id': conn.id, 'mode': 'table',
             'schema': 'ops', 'table': 'type_matrix'}},
            {'id': 'select', 'tool': 'core.select', 'config': {'fields': [
             {'field': 'id'}, {'field': 'nv'}, {'field': 'd'}], 'unknown_fields': 'exclude'}},
            {'id': 'filter', 'tool': 'core.filter', 'config': {'expression': '[nv] = "zoë ✓"'}},
            {'id': 'sum', 'tool': 'core.summarize', 'config': {'actions': [
             {'field': 'nv', 'op': 'group_by'}, {'field': 'd', 'op': 'sum', 'output': 'decimal_total'},
             {'field': 'id', 'op': 'count', 'output': 'records'}]}},
            {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
            'edges': [
                {'from': {'node': 'source', 'anchor': 'output'}, 'to': {'node': 'select', 'anchor': 'input'}},
                {'from': {'node': 'select', 'anchor': 'output'}, 'to': {'node': 'filter', 'anchor': 'input'}},
                {'from': {'node': 'filter', 'anchor': 'true'}, 'to': {'node': 'sum', 'anchor': 'input'}},
                {'from': {'node': 'sum', 'anchor': 'output'}, 'to': {'node': 'browse', 'anchor': 'input'}}]}
        pushed = run_workflow(doc, base_dir=self.home, retain='all', enable_pushdown=True)
        local = run_workflow(doc, base_dir=self.home, retain='all', enable_pushdown=False)
        self.assertTrue(pushed.ok, pushed.error)
        self.assertTrue(local.ok, local.error)
        self.assertEqual(pushed.browse['browse'], local.browse['browse'])
        self.assertEqual(len(pushed.manifest['database_queries']), 1, repr(pushed.events))
        self.assertEqual(pushed.manifest['database_queries'][0]['dialect'], 'sqlserver')

    def test_same_database_join_pushdown_matches_local(self):
        cases = (
            ('postgresql', 'sales', 'join_cases', 'k', 'seq', 'amount', 'amount'),
            ('sqlserver', 'ops', 'orders', 'customer_id', 'order_id', 'channel', 'unit_price'),
        )
        for dialect, schema, table, key, left_id, left_value, right_value in cases:
            with self.subTest(dialect=dialect):
                conn = self.connections[dialect]
                doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
                    {'id': 'left', 'tool': 'core.db_input', 'config': {'connection_id': conn.id,
                     'mode': 'table', 'schema': schema, 'table': table,
                     'order_by': [{'field': left_id}]}},
                    {'id': 'right', 'tool': 'core.db_input', 'config': {'connection_id': conn.id,
                     'mode': 'table', 'schema': schema, 'table': table,
                     'order_by': [{'field': left_id}]}},
                    {'id': 'join', 'tool': 'core.join', 'config': {'by': 'fields',
                     'keys': [{'left': key, 'right': key}], 'drop_right_keys': True,
                     'left_fields': [left_id, key, left_value], 'right_fields': [left_id, right_value]}},
                    {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
                    'edges': [
                     {'from': {'node': 'left', 'anchor': 'output'}, 'to': {'node': 'join', 'anchor': 'left'}},
                     {'from': {'node': 'right', 'anchor': 'output'}, 'to': {'node': 'join', 'anchor': 'right'}},
                     {'from': {'node': 'join', 'anchor': 'joined'}, 'to': {'node': 'browse', 'anchor': 'input'}}]}
                pushed = run_workflow(doc, base_dir=self.home, retain='all', enable_pushdown=True)
                local = run_workflow(doc, base_dir=self.home, retain='all', enable_pushdown=False)
                self.assertTrue(pushed.ok, pushed.error)
                self.assertTrue(local.ok, local.error)
                self.assertEqual(list(pushed.browse['browse'].rows()), list(local.browse['browse'].rows()),
                                 f"pushed={list(pushed.browse['browse'].rows())[:5]!r}; "
                                 f"local={list(local.browse['browse'].rows())[:5]!r}")
                self.assertEqual(len(pushed.manifest['database_queries']), 1)
                self.assertEqual(pushed.manifest['database_queries'][0]['nodes'][-1], 'join')
                if dialect == 'postgresql':
                    rows = list(pushed.browse['browse'].rows())
                    self.assertEqual(len(rows), 8)
                    self.assertEqual({row[1] for row in rows}, {'A', 'a', 'É', 'é', None})
                starts = [e['node_id'] for e in pushed.events if e['type'] == 'node-started']
                self.assertEqual(starts.count('join'), 1)

                branched = deepcopy(doc)
                branched['nodes'].extend({'id': node, 'tool': 'core.browse', 'config': {}}
                                         for node in ('left_only', 'right_only', 'source_branch'))
                branched['edges'].extend([
                    {'from': {'node': 'join', 'anchor': 'left_unjoined'},
                     'to': {'node': 'left_only', 'anchor': 'input'}},
                    {'from': {'node': 'join', 'anchor': 'right_unjoined'},
                     'to': {'node': 'right_only', 'anchor': 'input'}},
                    {'from': {'node': 'left', 'anchor': 'output'},
                     'to': {'node': 'source_branch', 'anchor': 'input'}}])
                branch_push = run_workflow(branched, base_dir=self.home, retain='all', enable_pushdown=True)
                branch_local = run_workflow(branched, base_dir=self.home, retain='all', enable_pushdown=False)
                self.assertTrue(branch_push.ok, branch_push.error)
                self.assertEqual(branch_push.manifest['database_queries'], [])
                for output in ('browse', 'left_only', 'right_only', 'source_branch'):
                    self.assertEqual(list(branch_push.browse[output].rows()), list(branch_local.browse[output].rows()))
                starts = [e['node_id'] for e in branch_push.events if e['type'] == 'node-started']
                for node in ('left', 'right', 'join', 'left_only', 'right_only', 'source_branch'):
                    self.assertEqual(starts.count(node), 1, node)

                if dialect == 'sqlserver':
                    text_join = deepcopy(doc)
                    text_join['nodes'][2]['config'].update(
                        keys=[{'left': 'channel', 'right': 'channel'}],
                        left_fields=['order_id', 'channel', 'customer_id'],
                        right_fields=['order_id', 'channel', 'unit_price'])
                    fallback = run_workflow(text_join, base_dir=self.home, retain='all', enable_pushdown=True)
                    local_text = run_workflow(text_join, base_dir=self.home, retain='all', enable_pushdown=False)
                    self.assertTrue(fallback.ok, fallback.error)
                    self.assertEqual(fallback.manifest['database_queries'], [])
                    self.assertEqual(list(fallback.browse['browse'].rows()), list(local_text.browse['browse'].rows()))
                    self.assertTrue(any('code-point ordering' in e.get('reason', '') for e in fallback.events
                                        if e['type'] == 'pushdown-fallback'))

    def test_read_only_guard_rejects_write_and_password_never_persisted(self):
        from flowengine.db.dialects import ReadOnlyViolation, check_read_only_sql
        from flowengine.errors import ConfigError
        with self.assertRaises(ReadOnlyViolation):
            check_read_only_sql('WITH x AS (DELETE FROM sales.customers RETURNING *) SELECT * FROM x')
        with self.assertRaises(ConfigError):
            ConnectionStore().save({'id': 'nested_secret', 'name': 'invalid', 'dialect': 'postgresql', 'host': 'h',
                'port': 5432, 'database': 'd', 'user': 'u', 'options': {'driver': {'password': 'must-not-persist'}}})
        raw = (self.home / 'connections.json').read_text(encoding='utf-8')
        self.assertNotIn('password', raw.lower())
        self.assertNotIn('FLOWBENCH_SECRET', raw)
        from flowengine.tools.database import open_dialect
        for dialect, schema in (('postgresql', 'sales'), ('sqlserver', 'ops')):
            _, d = open_dialect(self.connections[dialect].id)
            conn = d.connect(read_only=False)
            try:
                with self.assertRaises(Exception):
                    d.run(conn, f'UPDATE {d.qualified(schema, "live_counter")} SET value = value + 1 WHERE id = %s',
                          (1,))
            finally:
                conn.rollback()
                conn.close()

    def test_async_run_exports_full_typed_result_to_csv_and_parquet(self):
        conn = self.connections['postgresql']
        doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
            {'id': 'db', 'tool': 'core.db_input', 'config': {'connection_id': conn.id, 'mode': 'table',
             'schema': 'sales', 'table': 'type_matrix'}},
            {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
            'edges': [{'from': {'node': 'db', 'anchor': 'output'},
                       'to': {'node': 'browse', 'anchor': 'input'}}]}
        svc = EngineService()
        run_id = svc.handle('run.start', {'workflow': doc, 'base_dir': str(self.home), 'preview_rows': 1})['run_id']
        deadline = time.monotonic() + 20
        while svc.handle('run.result', {'run_id': run_id})['status'] == 'running':
            if time.monotonic() > deadline:
                self.fail('database workflow did not finish')
            time.sleep(0.05)
        result = svc.handle('run.result', {'run_id': run_id})['result']
        self.assertEqual(len(result['browse']['browse']['rows']), 1)
        csv_path, parquet_path = self.home / 'export.csv', self.home / 'export.parquet'
        self.assertEqual(svc.handle('run.export', {'run_id': run_id, 'node_id': 'browse', 'anchor': '(browse)',
                         'path': str(csv_path), 'format': 'csv'})['format'], 'csv')
        self.assertEqual(svc.handle('run.export', {'run_id': run_id, 'node_id': 'browse', 'anchor': '(browse)',
                         'path': str(parquet_path), 'format': 'parquet'})['format'], 'parquet')
        from flowengine.arrow import read_parquet
        exported = read_parquet(parquet_path)
        self.assertEqual(len(exported), 2)
        self.assertEqual(exported.column('bin')[0], bytes([0, 255, 16]))
        self.assertEqual(str(exported.column('dec')[0]), '12345678.1234')
        self.assertIn('zoë ✓', csv_path.read_text(encoding='utf-8-sig'))

    def test_database_output_requires_capability_and_rolls_back_failed_batch(self):
        for dialect, schema in (('postgresql', 'sales'), ('sqlserver', 'ops')):
            with self.subTest(dialect=dialect):
                writer = self.connections['test_writer_' + dialect]
                from flowengine.tools.database import open_dialect
                _, d = open_dialect(writer.id)
                with d.connect(read_only=True) as conn:
                    cur = conn.cursor()
                    cur.execute(f'SELECT COALESCE(MAX(id), 0) FROM {d.qualified(schema, "live_counter")}')
                    base_id = int(cur.fetchone()[0]) + 100
                doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
                    {'id': 'rows', 'tool': 'core.text_input', 'config': {
                        'fields': [{'name': 'id', 'type': 'int32'}, {'name': 'value', 'type': 'int32'}],
                        'rows': [[str(base_id), '20']]}},
                    {'id': 'output', 'tool': 'core.db_output', 'config': {
                        'connection_id': writer.id, 'schema': schema, 'table': 'live_counter', 'mode': 'append'}}],
                    'edges': [{'from': {'node': 'rows', 'anchor': 'output'},
                               'to': {'node': 'output', 'anchor': 'input'}}]}
                denied = run_workflow(doc, base_dir=self.home, limits={'allow_database_writes': False})
                self.assertEqual(denied.status, 'failed')
                self.assertEqual(denied.error['code'], 'permission-denied')
                accepted = run_workflow(doc, base_dir=self.home, limits={'allow_database_writes': True})
                self.assertTrue(accepted.ok, accepted.error)

                doc['nodes'][0]['config']['rows'] = [[str(base_id + 1), '30'], [str(base_id + 1), '31']]
                failed = run_workflow(doc, base_dir=self.home, limits={'allow_database_writes': True})
                self.assertFalse(failed.ok)
                from flowengine.db.dialects import Query
                with Query(d, f'SELECT COUNT(*) AS n FROM {d.qualified(schema, "live_counter")} '
                               'WHERE id = %s', (base_id + 1,)) as q:
                    fields = q.describe()
                    rows = [row for batch in q.batches(fields, 10) for row in batch]
                self.assertEqual(rows[0][0], 0)

    def test_authentication_tls_verification_and_password_rotation(self):
        from dataclasses import asdict
        from flowengine.db.connections import ConnectionStore
        from flowengine.errors import EngineError
        from flowengine.tools.database import open_dialect
        store = ConnectionStore()
        source = self.connections['postgresql']
        failures = []
        bad_id = 'test_bad_auth'
        bad_secret = 'definitely-not-the-real-secret'
        os.environ['FLOWBENCH_SECRET_TEST_BAD_AUTH'] = bad_secret
        bad_cfg = asdict(source); bad_cfg.update(id=bad_id, name='bad auth')
        bad = store.save(bad_cfg)
        try:
            with self.assertRaises(EngineError) as raised:
                EngineService().handle('database.test', {'connection_id': bad.id})
            self.assertNotIn(bad_secret, str(raised.exception))
            failures.append('bad authentication rejected')
        finally:
            store.delete(bad_id)
            os.environ.pop('FLOWBENCH_SECRET_TEST_BAD_AUTH', None)

        tls_id = 'test_tls_verify'
        os.environ['FLOWBENCH_SECRET_TEST_TLS_VERIFY'] = os.environ['FLOWBENCH_SECRET_TEST_POSTGRESQL']
        tls_cfg = asdict(source); tls_cfg.update(id=tls_id, name='verified TLS', host='localhost',
                                                 tls='verify-full', ca_file=str(json.loads(ENV_FILE.read_text())
                                                 ['postgresql']['ca_file']))
        store.save(tls_cfg)
        try:
            self.assertTrue(EngineService().handle('database.test', {'connection_id': tls_id})['ok'])
            invalid = dict(tls_cfg, host='127.0.0.1', id='test_tls_bad_host')
            os.environ['FLOWBENCH_SECRET_TEST_TLS_BAD_HOST'] = os.environ['FLOWBENCH_SECRET_TEST_POSTGRESQL']
            store.save(invalid)
            with self.assertRaises(EngineError):
                EngineService().handle('database.test', {'connection_id': invalid['id']})
            store.delete(invalid['id'])
        finally:
            store.delete(tls_id)
            os.environ.pop('FLOWBENCH_SECRET_TEST_TLS_VERIFY', None)
            os.environ.pop('FLOWBENCH_SECRET_TEST_TLS_BAD_HOST', None)

        original_secret = os.environ['FLOWBENCH_SECRET_TEST_POSTGRESQL']
        rotated_secret = 'FlowbenchRotated_2026!'
        pg_server = json.loads(ENV_FILE.read_text(encoding='utf-8'))['postgresql']
        import psycopg
        from psycopg import sql
        admin = psycopg.connect(host=pg_server['host'], port=pg_server['port'], dbname=pg_server['database'],
                                user='postgres', password=pg_server['admin_password'], sslmode='require',
                                autocommit=True)
        try:
            admin.execute(sql.SQL('ALTER ROLE fb_reader WITH PASSWORD {}').format(sql.Literal(rotated_secret)))
            os.environ['FLOWBENCH_SECRET_TEST_POSTGRESQL'] = rotated_secret
            self.assertTrue(EngineService().handle('database.test',
                          {'connection_id': source.id})['ok'])
        finally:
            admin.execute(sql.SQL('ALTER ROLE fb_reader WITH PASSWORD {}').format(sql.Literal(original_secret)))
            admin.close()
            os.environ['FLOWBENCH_SECRET_TEST_POSTGRESQL'] = original_secret

    def test_database_timeout_cancellation_and_batched_large_results(self):
        from dataclasses import asdict
        from flowengine.db.connections import ConnectionStore
        from flowengine.db.dialects import DatabaseError, Query
        from flowengine.errors import CancelledError
        from flowengine.plugin import CancelToken
        from flowengine.tools.database import open_dialect
        source = self.connections['postgresql']
        timeout_id = 'test_pg_timeout'
        timeout_cfg = asdict(source); timeout_cfg.update(id=timeout_id, query_timeout=1)
        ConnectionStore().save(timeout_cfg)
        os.environ['FLOWBENCH_SECRET_TEST_PG_TIMEOUT'] = os.environ['FLOWBENCH_SECRET_TEST_POSTGRESQL']
        try:
            _, timed = open_dialect(timeout_id)
            with self.assertRaises(DatabaseError) as raised:
                with Query(timed, 'SELECT pg_sleep(3) AS waited') as q:
                    fields = q.describe()
                    list(row for batch in q.batches(fields, 10) for row in batch)
            self.assertEqual(raised.exception.code, 'database-timeout')
        finally:
            ConnectionStore().delete(timeout_id)
            os.environ.pop('FLOWBENCH_SECRET_TEST_PG_TIMEOUT', None)

        _, pg = open_dialect(source.id)
        token = CancelToken()
        timer = threading.Timer(0.25, token.cancel)
        timer.start()
        try:
            with self.assertRaises(CancelledError):
                with Query(pg, 'SELECT pg_sleep(10) AS waited', cancel_token=token) as q:
                    fields = q.describe()
                    list(row for batch in q.batches(fields, 10) for row in batch)
        finally:
            timer.cancel()

        for dialect, schema in (('postgresql', 'sales'), ('sqlserver', 'ops')):
            _, d = open_dialect(self.connections[dialect].id)
            with Query(d, f'SELECT id FROM {d.qualified(schema, "big")} ORDER BY id') as q:
                fields = q.describe()
                total, max_batch = 0, 0
                for batch in q.batches(fields, 4096):
                    total += len(batch)
                    max_batch = max(max_batch, len(batch))
            self.assertEqual(total, json.loads(ENV_FILE.read_text(encoding='utf-8'))['big_rows'])
            self.assertLessEqual(max_batch, 4096)

    def test_connection_loss_is_reported_as_database_failure(self):
        import psycopg
        from flowengine.db.dialects import DatabaseError, Query
        from flowengine.tools.database import open_dialect
        source = self.connections['postgresql']
        _, d = open_dialect(source.id)
        server = json.loads(ENV_FILE.read_text(encoding='utf-8'))['postgresql']
        with Query(d, 'SELECT id FROM sales.big') as q:
            fields = q.describe()
            pid = q.conn.info.backend_pid
            admin = psycopg.connect(host=server['host'], port=server['port'], dbname=server['database'],
                                    user='postgres', password=server['admin_password'], sslmode='require',
                                    autocommit=True)
            try:
                self.assertTrue(admin.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0])
            finally:
                admin.close()
            with self.assertRaises(DatabaseError):
                list(row for batch in q.batches(fields, 10) for row in batch)


if __name__ == '__main__':
    unittest.main()
