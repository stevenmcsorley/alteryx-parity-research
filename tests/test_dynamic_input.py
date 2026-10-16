"""Dynamic Input: read the files named by incoming records."""
import tempfile
import unittest
from pathlib import Path

from flowengine.errors import WorkflowValidationError
from flowengine.executor import plan_workflow, run_workflow

from helpers import edge, text_input, workflow


def dynamic(config):
    return {'id': 'dyn', 'tool': 'core.dynamic_input', 'config': config}


class DynamicInputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / 'data').mkdir()
        (self.dir / 'data' / 'jan.csv').write_text('region,amount\nN,1\nS,2\n', encoding='utf-8')
        (self.dir / 'data' / 'feb.csv').write_text('region,amount\nN,5\n', encoding='utf-8')
        (self.dir / 'data' / 'mar.csv').write_text('amount,channel\n7,web\n', encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def run_paths(self, paths, **config):
        doc = workflow([text_input('paths', [('path', 'v_wstring')], [[p] for p in paths]),
                        dynamic({'path_field': 'path', **config})], [edge('paths', 'output', 'dyn', 'input')])
        return run_workflow(doc, base_dir=self.dir)

    def test_reads_each_named_file_in_record_order(self):
        result = self.run_paths(['data/feb.csv', 'data/jan.csv', 'data/feb.csv'], add_file_name_field='Source')
        self.assertTrue(result.ok, result.error)
        out = result.output('dyn')
        self.assertEqual(out.schema.names, ['region', 'amount', 'Source'])
        self.assertEqual(out.column('amount'), ['5', '1', '2', '5'])
        self.assertEqual([Path(p).name for p in out.column('Source')], ['feb.csv', 'jan.csv', 'jan.csv', 'feb.csv'])

    def test_directory_listing_drives_dynamic_input(self):
        doc = workflow([{'id': 'dir', 'tool': 'core.directory', 'config': {'path': 'data', 'pattern': '*.csv',
                                                                          'recursive': False}},
                        {'id': 'pick', 'tool': 'core.filter', 'config': {'expression': '[FileName] != "mar.csv"'}},
                        dynamic({'path_field': 'FullPath', 'type_inference': 'flowbench'})],
                       [edge('dir', 'output', 'pick', 'input'), edge('pick', 'true', 'dyn', 'input')])
        result = run_workflow(doc, base_dir=self.dir)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(sorted(result.output('dyn').column('amount')), [1, 2, 5])

    def test_schema_mismatch_fails_unless_union_is_chosen(self):
        result = self.run_paths(['data/jan.csv', 'data/mar.csv'])
        self.assertEqual(result.status, 'failed')
        self.assertIn('schema_mode union', result.error['message'])
        result = self.run_paths(['data/jan.csv', 'data/mar.csv'], schema_mode='union')
        self.assertTrue(result.ok, result.error)
        out = result.output('dyn')
        self.assertEqual(out.schema.names, ['region', 'amount', 'channel'])
        self.assertEqual(list(out.rows()), [('N', '1', None), ('S', '2', None), (None, '7', 'web')])

    def test_bad_paths_fail_with_the_record_number(self):
        for paths, message in [(['data/jan.csv', ''], 'record 2 has an empty path'),
                               (['data/missing.csv'], 'not found')]:
            with self.subTest(paths=paths):
                result = self.run_paths(paths)
                self.assertEqual(result.status, 'failed')
                self.assertIn(message, result.error['message'])

    def test_declared_fields_give_a_static_schema_and_an_empty_result(self):
        fields = [{'name': 'region', 'type': 'v_wstring'}, {'name': 'amount', 'type': 'int64'}]
        result = self.run_paths(['data/jan.csv'], fields=fields)
        self.assertEqual(result.output('dyn').column('amount'), [1, 2])
        empty = workflow([text_input('paths', [('path', 'v_wstring')], []),
                          dynamic({'path_field': 'path', 'fields': fields})], [edge('paths', 'output', 'dyn', 'input')])
        plan, problems = plan_workflow(empty, base_dir=self.dir)
        self.assertEqual(problems, [])
        self.assertEqual([f.name for f in plan.schemas[('dyn', 'output')]], ['region', 'amount'])
        result = run_workflow(empty, base_dir=self.dir)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.output('dyn')), 0)
        empty['nodes'][1]['config'].pop('fields')
        self.assertIn('declare fields', run_workflow(empty, base_dir=self.dir).error['message'])

    def test_path_field_must_exist_and_be_text(self):
        for fields, rows, message in [([('path', 'int32')], [[1]], 'must be a text field'),
                                      ([('other', 'v_wstring')], [['x']], 'not in the input')]:
            doc = workflow([text_input('paths', fields, rows), dynamic({'path_field': 'path'})],
                           [edge('paths', 'output', 'dyn', 'input')])
            with self.subTest(message=message), self.assertRaisesRegex(WorkflowValidationError, message):
                run_workflow(doc, base_dir=self.dir)


if __name__ == '__main__':
    unittest.main()
