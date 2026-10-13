"""Real-engine tests. Start deterministic containers with ``python tests/integration/db_env.py up``."""
import json
import os
import tempfile
import unittest
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

    def test_read_only_guard_rejects_write_and_password_never_persisted(self):
        from flowengine.db.dialects import ReadOnlyViolation, check_read_only_sql
        with self.assertRaises(ReadOnlyViolation):
            check_read_only_sql('WITH x AS (DELETE FROM sales.customers RETURNING *) SELECT * FROM x')
        raw = (self.home / 'connections.json').read_text(encoding='utf-8')
        self.assertNotIn('password', raw.lower())
        self.assertNotIn('FLOWBENCH_SECRET', raw)


if __name__ == '__main__':
    unittest.main()
