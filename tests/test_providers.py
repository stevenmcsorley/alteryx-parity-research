"""Providers, declared entitlements, data packs and the Apache Spark Code tool."""
import datetime as dt
import importlib.util
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from flowengine import providers
from flowengine.errors import ConfigError
from flowengine.executor import run_workflow

from helpers import edge, text_input, workflow

HAS_SPARK = importlib.util.find_spec('pyspark') is not None and shutil.which('java') is not None


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ent = providers.EntitlementStore(self.dir / 'entitlements.json')
        self.packs = providers.DataPackStore(self.dir / 'data_packs.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_entitlements_are_declared_never_validated(self):
        item = self.ent.declare('Alteryx Server', 'Site licence, renewal March')
        self.assertFalse(item['validated'])
        self.assertTrue(self.ent.has('alteryx server'))
        self.assertEqual(self.ent.declare('ALTERYX SERVER')['id'], item['id'], 'one declaration per product')
        self.ent.remove(item['id'])
        self.assertFalse(self.ent.has('Alteryx Server'))
        with self.assertRaises(ConfigError):
            self.ent.declare('  ')

    def test_data_packs_record_licence_size_and_checksum(self):
        folder = self.dir / 'osm'
        folder.mkdir()
        (folder / 'a.pbf').write_bytes(b'x' * 10)
        (folder / 'b.pbf').write_bytes(b'y' * 5)
        pack = self.packs.register('osm-extract', str(folder), 'ODbL-1.0', source='Geofabrik')
        self.assertEqual((pack['bytes'], len(pack['sha256'])), (15, 64))
        self.assertEqual([p['id'] for p in self.packs.for_family('osm-extract')], [pack['id']])
        (folder / 'b.pbf').write_bytes(b'z' * 5)
        self.assertNotEqual(self.packs.register('osm-extract', str(folder), 'ODbL-1.0')['sha256'], pack['sha256'])
        for bad in (('OSM', str(folder), 'ODbL-1.0'), ('osm-extract', str(folder), ''), ('osm-extract', str(self.dir / 'nope'), 'MIT')):
            with self.assertRaises(ConfigError):
                self.packs.register(*bad)

    def test_status_reports_every_missing_requirement(self):
        p = providers.Provider(id='test-vendor', family='test', name='Test vendor', kind='vendor', serves=('x:y',),
                               needs=('entitlement:Test Product', 'datapack:test-data', 'python:no_such_module_xyz'))
        s = providers.status(p, self.ent, self.packs)
        self.assertFalse(s['ready'])
        self.assertEqual([c['ok'] for c in s['checks']], [False, False, False])
        self.ent.declare('Test Product')
        (self.dir / 'd.csv').write_text('a\n')
        self.packs.register('test-data', str(self.dir / 'd.csv'), 'CC-BY-4.0')
        s = providers.status(p, self.ent, self.packs)
        self.assertEqual([c['ok'] for c in s['checks']], [True, True, False])
        self.assertIn('not validated', s['checks'][0]['detail'])

    def test_nodes_name_their_provider_and_switching_semantics_is_caught(self):
        with self.assertRaises(ConfigError):
            providers.resolve({}, 'spark')  # never chosen implicitly
        with self.assertRaises(ConfigError):
            providers.resolve({'provider': 'r'}, 'spark')  # wrong family
        p = providers.resolve({'provider': 'spark-local'}, 'spark')
        self.assertIsNone(providers.check_semantics({'provider_semantics': p.fingerprint()}, p))
        with self.assertRaises(ConfigError):
            providers.resolve({'provider': 'spark-local', 'provider_semantics': '0' * 16}, 'spark')

    def test_availability_lists_the_providers_for_each_entry(self):
        rows = {r['catalog_id']: r for r in providers.availability(self.ent, self.packs)}
        self.assertEqual([p['provider'] for p in rows['developer:apache-spark-code']['providers']], ['spark-local'])
        self.assertEqual(rows['developer:r']['providers'][0]['provider'], 'r')

    def test_spark_code_refuses_to_run_without_its_provider(self):
        doc = workflow([{'id': 's', 'tool': 'core.spark_code', 'config': {'provider': 'spark-local', 'code': 'pass', 'master': 'spark://x:7077'}}], [])
        r = run_workflow(doc, base_dir=self.dir, limits={'allow_processes': True})
        self.assertEqual(r.status, 'failed')
        self.assertIn('local mode', r.error['message'])


class IntegrationsServiceTests(unittest.TestCase):
    def test_settings_page_methods_round_trip_without_secrets(self):
        from flowengine.service import EngineService
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'FLOWENGINE_HOME': home}):
            svc = EngineService()
            ent = svc.handle('integrations.entitlements.declare', {'product': 'Alteryx Server'})
            self.assertEqual([e['id'] for e in svc.handle('integrations.entitlements.list', {})], [ent['id']])
            Path(home, 'pack.csv').write_text('a\n1\n')
            pack = svc.handle('integrations.datapacks.register', {'family': 'test-data', 'path': str(Path(home, 'pack.csv')),
                                                                  'licence': 'CC0-1.0'})
            self.assertEqual(svc.handle('integrations.datapacks.list', {})[0]['sha256'], pack['sha256'])
            spark = next(p for p in svc.handle('integrations.providers', {}) if p['id'] == 'spark-local')
            self.assertEqual([c['need'] for c in spark['checks']], ['python:pyspark', 'exe:java'])
            self.assertIn('python_environments', svc.handle('integrations.runtimes', {}))
            svc.handle('integrations.entitlements.remove', {'id': ent['id']})
            self.assertEqual(svc.handle('integrations.entitlements.list', {}), [])


class SparkRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(HAS_SPARK, 'pyspark and Java are not installed (runs in the spark-runtime CI job)')
    def test_spark_code_transforms_typed_tables(self):
        rows = [['1', 'North', '2026-01-05', '10.50', 'true'], ['2', 'South', '2026-02-10', '7.25', 'false'],
                ['3', 'North', None, '1.00', 'true']]
        nodes = [text_input('t', [('id', 'int64'), ('region', 'v_wstring'), ('day', 'date'), ('amount', 'fixed_decimal'),
                                  ('flag', 'bool')], rows),
                 {'id': 's', 'tool': 'core.spark_code', 'config': {'provider': 'spark-local', 'master': 'local[1]', 'code': (
                     'from flowbench_spark import read_input, write_output\n'
                     'from pyspark.sql import functions as F\n'
                     'df = read_input(1)\n'
                     'write_output(1, df.groupBy("region").agg(F.sum("amount").alias("total"), F.count("*").alias("n"),'
                     ' F.max("day").alias("last")).orderBy("region"))\n'
                     'write_output(2, df.filter(F.col("flag")).select("id", "flag"))\n')}}]
        t = nodes[0]['config']['fields']
        t[3]['size'], t[3]['scale'] = 10, 2
        r = run_workflow(workflow(nodes, [edge('t', 'output', 's', 'inputs')]), base_dir=Path.cwd(),
                         limits={'allow_processes': True})
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['s']['output1']
        self.assertEqual([(f.name, f.type.value) for f in out.schema], [('region', 'v_wstring'), ('total', 'fixed_decimal'),
                                                                       ('n', 'int64'), ('last', 'date')])
        self.assertEqual(list(out.rows()), [('North', Decimal('11.50'), 2, dt.date(2026, 1, 5)),
                                            ('South', Decimal('7.25'), 1, dt.date(2026, 2, 10))])
        self.assertEqual(sorted(r.outputs['s']['output2'].rows()), [(1, True), (3, True)])


if __name__ == '__main__':
    unittest.main()
