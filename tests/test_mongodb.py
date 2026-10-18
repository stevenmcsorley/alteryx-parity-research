"""MongoDB Input/Output live against a real MongoDB server (service-integration CI job).

Needs FLOWBENCH_MONGO_HOST, FLOWBENCH_MONGO_PORT, FLOWBENCH_MONGO_USER and FLOWBENCH_MONGO_PASSWORD (a user in admin).
"""
import datetime as dt
import json
import os
import tempfile
import unittest
import uuid
from decimal import Decimal
from unittest import mock

import flowengine.db.vault as vault_module
from flowengine.db.connections import ConnectionStore
from flowengine.executor import run_workflow

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

LIVE = all(os.environ.get(k) for k in ('FLOWBENCH_MONGO_HOST', 'FLOWBENCH_MONGO_PORT', 'FLOWBENCH_MONGO_USER', 'FLOWBENCH_MONGO_PASSWORD'))
WRITE = {'allow_database_writes': True}


@unittest.skipUnless(LIVE, 'no MongoDB server configured (runs in the service-integration CI job)')
class MongoDBTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        self.db = f'fb_{uuid.uuid4().hex[:10]}'
        self.conn = self.save(allow_writes=True)

    def tearDown(self):
        import pymongo
        client = pymongo.MongoClient(os.environ['FLOWBENCH_MONGO_HOST'], int(os.environ['FLOWBENCH_MONGO_PORT']),
                                     username=os.environ['FLOWBENCH_MONGO_USER'], password=os.environ['FLOWBENCH_MONGO_PASSWORD'])
        client.drop_database(self.db)
        client.close()
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def save(self, allow_writes, password=None):
        c = ConnectionStore().save({'name': f'Mongo {uuid.uuid4().hex[:4]}', 'dialect': 'mongodb', 'host': os.environ['FLOWBENCH_MONGO_HOST'],
                                    'port': int(os.environ['FLOWBENCH_MONGO_PORT']), 'database': self.db,
                                    'user': os.environ['FLOWBENCH_MONGO_USER'], 'tls': 'disable', 'allow_writes': allow_writes},
                                   password=password or os.environ['FLOWBENCH_MONGO_PASSWORD'])
        return c.id

    def write(self, fields, rows, limits=WRITE, conn=None, **cfg):
        doc = workflow([text_input('t', fields, rows), {'id': 'w', 'tool': 'core.mongodb_output', 'config': {
            'provider': 'mongodb', 'connection_id': conn or self.conn, 'collection': 'sales', **cfg}}], [edge('t', 'output', 'w', 'input')])
        return run_workflow(doc, base_dir=ROOT, limits=limits)

    def read(self, **cfg):
        doc = workflow([{'id': 'r', 'tool': 'core.mongodb_input', 'config': {'provider': 'mongodb', 'connection_id': self.conn,
                                                                            'collection': 'sales', **cfg}}], [])
        return run_workflow(doc, base_dir=ROOT, limits={})

    def test_insert_then_find_with_filter_projection_sort_and_types(self):
        fields = [('id', 'int64'), ('region', 'v_wstring'), ('amount', 'double'), ('day', 'date'), ('vip', 'bool')]
        rows = [['1', 'North', '10.5', '2026-01-05', 'true'], ['2', 'South', '7.25', '2026-02-10', 'false'],
                ['3', 'North', '3.0', '2026-03-01', 'false']]
        self.assertEqual(self.write(fields, rows).status, 'succeeded')
        r = self.read(filter='{"region": "North"}', projection='{"_id": 0}', sort='{"amount": 1}')
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['r']['output']
        self.assertEqual([(f.name, f.type.value) for f in out.schema],
                         [('id', 'int64'), ('region', 'v_wstring'), ('amount', 'double'), ('day', 'datetime'), ('vip', 'bool')])
        self.assertEqual(list(out.rows()), [(3, 'North', 3.0, dt.datetime(2026, 3, 1), False),
                                            (1, 'North', 10.5, dt.datetime(2026, 1, 5), True)])

    def test_nested_documents_json_output_and_upsert(self):
        docs = [['{"sku": "A", "price": {"$numberDecimal": "19.99"}, "tags": ["x", "y"], "dims": {"w": 2}}'],
                ['{"sku": "B", "price": {"$numberDecimal": "5.10"}, "tags": [], "dims": {"w": 3}}']]
        self.assertEqual(self.write([('doc', 'v_wstring')], docs, json_field='doc').status, 'succeeded')
        out = self.read(projection='{"_id": 0}', sort='{"sku": 1}').outputs['r']['output']
        rows = list(out.rows())
        self.assertEqual(rows[0][0], 'A')
        self.assertEqual(rows[0][1], Decimal('19.99'))
        self.assertEqual((json.loads(rows[0][2]), json.loads(rows[0][3])), (['x', 'y'], {'w': 2}))
        self.assertEqual(self.write([('sku', 'v_wstring'), ('stock', 'int32')], [['A', '4'], ['C', '9']],
                                    mode='upsert', key_fields=['sku']).status, 'succeeded')
        whole = self.read(output='json', sort='{"sku": 1}').outputs['r']['output']
        skus = [json.loads(v)['sku'] for (v,) in whole.rows()]
        self.assertEqual(skus, ['A', 'B', 'C'])
        self.assertNotIn('price', json.loads(next(whole.rows())[0]), 'upsert replaces the matched document')

    def test_writes_need_permission_writable_connection_and_replace_confirmation(self):
        fields, rows = [('a', 'int32')], [['1']]
        self.assertEqual(self.write(fields, rows, limits={}).error['code'], 'permission-denied')
        self.assertEqual(self.write(fields, rows, conn=self.save(allow_writes=False)).error['code'], 'permission-denied')
        self.assertIn('confirm_replace', self.write(fields, rows, mode='replace').error['message'])
        self.write(fields, [['1'], ['2']])
        self.assertEqual(self.write(fields, [['9']], mode='replace', confirm_replace=True).status, 'succeeded')
        self.assertEqual([r[0] for r in self.read(projection='{"_id": 0}').outputs['r']['output'].rows()], [9])

    def test_bad_password_and_bad_filter_fail_clearly(self):
        self.conn = self.save(allow_writes=False, password='wrong')
        self.assertIn('MongoDB read failed', self.read().error['message'])
        self.assertIn('Extended JSON', self.read(filter='{region: }').error['message'])


if __name__ == '__main__':
    unittest.main()
