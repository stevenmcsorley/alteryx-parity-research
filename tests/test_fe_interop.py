"""Workflow document import/export (original fixtures only)."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from flowengine.errors import WorkflowValidationError
from flowengine.executor import plan_workflow, run_workflow
from flowengine.interop.yxmd import export_document, import_document, import_file

from helpers import ROOT, rows

FIXTURE = ROOT / 'tests' / 'fixtures' / 'yxmd' / 'regional_sales.yxmd'


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.doc, self.report = import_file(FIXTURE)

    def test_everything_is_accounted_for(self):
        by_node = {n['node']: n for n in self.report['nodes']}
        self.assertEqual(by_node['t10']['as'], 'container')
        self.assertEqual(by_node['t9']['as'], 'comment')
        self.assertEqual(by_node['t8']['status'], 'unsupported')
        self.assertEqual(by_node['t1']['status'], 'partial')  # Text Input types inferred (warning)
        self.assertTrue(all(by_node[f't{i}']['status'] == 'mapped' for i in (2, 3, 4, 5, 6, 7)))
        ui = self.doc['metadata']['ui']
        self.assertEqual(ui['containers'][0]['children'], ['t2', 't3'])
        self.assertEqual(ui['comments'][0]['text'], 'Sums revenue by region.')
        self.assertIn('t8', self.doc['metadata']['import']['nodes'])  # original XML preserved
        self.assertIn('BufferDistance', self.doc['metadata']['import']['nodes']['t8']['xml'])
        self.assertEqual(next(n for n in self.doc['nodes'] if n['id'] == 't1')['annotation'], 'synthetic sales')

    def test_validation_reports_the_unsupported_catalogue_tool(self):
        problems = plan_workflow(self.doc, base_dir=ROOT)[1]
        self.assertEqual([(p['node_id'], p['code']) for p in problems], [('t8', 'tool-unavailable')])
        self.assertIn('Buffer', problems[0]['message'])
        with self.assertRaises(WorkflowValidationError):
            run_workflow(self.doc, base_dir=ROOT)

    def test_runs_once_the_unsupported_branch_is_removed(self):
        doc = json.loads(json.dumps(self.doc))
        doc['nodes'] = [n for n in doc['nodes'] if n['id'] != 't8']
        doc['edges'] = [e for e in doc['edges'] if e['to']['node'] != 't8']
        r = run_workflow(doc, base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(rows(r.output('t5')), [['East', 21.0], ['North', 11.5], ['South', 10.0]])
        self.assertEqual(len(r.browse['t7']), 6)  # the 3-row stream unioned twice (#1, #2)

    def test_export_round_trip(self):
        doc = json.loads(json.dumps(self.doc))
        doc['nodes'] = [n for n in doc['nodes'] if n['id'] != 't8']
        doc['edges'] = [e for e in doc['edges'] if e['to']['node'] != 't8']
        xml, problems = export_document(doc)
        self.assertEqual(problems, [])
        again, report = import_document(xml)
        self.assertEqual([(n['tool'], n['config']) for n in again['nodes']],
                         [(n['tool'], n['config']) for n in doc['nodes']])
        a = run_workflow(again, base_dir=ROOT)
        self.assertEqual(rows(a.output('t5')), [['East', 21.0], ['North', 11.5], ['South', 10.0]])

    def test_export_lists_what_cannot_be_expressed(self):
        doc = {'format': 'flowengine.workflow', 'formatVersion': 1,
               'nodes': [{'id': 'a', 'tool': 'core.python', 'config': {'code': 'pass'}}], 'edges': []}
        _, problems = export_document(doc)
        self.assertEqual(problems, ['a: core.python has no export mapping'])

    def test_packaged_workflow(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / 'sales.yxzp'
            with zipfile.ZipFile(pkg, 'w') as z:
                z.write(FIXTURE, 'regional_sales.yxmd')
                z.writestr('data/readme.txt', 'asset')
            doc, report = import_file(pkg)
            self.assertEqual(len(doc['nodes']), len(self.doc['nodes']))
            self.assertIn('data/readme.txt', report['package_members'])

    def test_rejects_non_workflow_documents(self):
        with self.assertRaises(ValueError):
            import_document('<NotAWorkflow />')
        with self.assertRaises(ValueError):
            import_document('not xml')


if __name__ == '__main__':
    unittest.main()
