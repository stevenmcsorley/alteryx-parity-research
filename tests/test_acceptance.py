"""Acceptance scenarios A, B, D and E: an orchestrator operates Flowbench end to end.

Each scenario is scripted the way a competent orchestrator would act, and checked independently
(pandas recomputes every number). With FLOWBENCH_ACCEPTANCE_MODEL=ollama:<model> the live variants
give the same tasks to a real model. Scenario C (manual and AI editing on one canvas, with undo) and
F (no false completion) need the editor and are covered in desktop/src/ai/AiSidebar.test.tsx.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from acceptance_harness import FileServer, LiveAgent, Session, live_model

FUEL = 'week,petrol,diesel\n2024-01-01,140.2,148.9\n2024-01-08,139.8,149.5\n2024-01-15,141.0,151.2\n2024-01-22,142.4,150.1\n'
SALES = ('Order,Category,Sales\n1, Toys ,12.50\n2,Toys,7.5\n3,Games,30\n4,Games,\n5,Books,4.25\n6,books ,5.75\n7,Games,10\n')


def edge(a, b, c, d):
    return {'from': {'node': a, 'anchor': b}, 'to': {'node': c, 'anchor': d}}


def flow(nodes, edges):
    return {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': nodes, 'edges': edges}


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.site = self.root / 'site'
        self.site.mkdir()
        self.work = self.root / 'project'
        self.work.mkdir()
        self.web = FileServer(self.site)
        self.session = Session(self.work)

    def tearDown(self):
        self.web.close()
        self.tmp.cleanup()

    def write_flow(self, name, doc):
        self.session.ok('write_text_file', path=name, text=json.dumps(doc, indent=1), overwrite=True)

    def summary_of(self, run_id, node_id):
        return self.session.ok('query_results', run_id=run_id, node_id=node_id, limit=None)['rows']


class AcquisitionTests(Scenario):
    """A: find and download a public CSV, import it, and produce a verified summary with provenance."""

    def test_download_build_run_and_cite_evidence(self):
        (self.site / 'weekly_fuel_prices.csv').write_text(FUEL)
        s = self.session
        got = s.ok('download_file', url=self.web.url('weekly_fuel_prices.csv'))
        source = json.loads(Path(got['path'] + '.source.json').read_text())
        self.assertEqual((source['url'], source['sha256']), (self.web.url('weekly_fuel_prices.csv'), hashlib.sha256(FUEL.encode()).hexdigest()))
        self.assertEqual(s.ok('profile_file', path='data/weekly_fuel_prices.csv')['rows'], 4)
        tools = {t['tool_id'] for t in s.ok('find_tools', query='calculate a new column')['tools']}
        self.assertIn('core.formula', tools)
        self.write_flow('fuel.flow.json', flow([
            {'id': 'read', 'tool': 'core.input_data', 'config': {'path': 'data/weekly_fuel_prices.csv'}},
            {'id': 'types', 'tool': 'core.auto_field', 'config': {}},
            {'id': 'premium', 'tool': 'core.formula', 'config': {'formulas': [{'field': 'premium', 'expression': '[diesel] - [petrol]', 'type': 'double'}]}},
            {'id': 'stats', 'tool': 'core.summarize', 'config': {'actions': [{'field': 'premium', 'op': 'avg', 'output': 'avg_premium'},
                                                                            {'field': 'premium', 'op': 'max', 'output': 'max_premium'}]}}],
            [edge('read', 'output', 'types', 'input'), edge('types', 'output', 'premium', 'input'), edge('premium', 'output', 'stats', 'input')]))
        self.assertEqual(s.ok('validate_workflow', path='fuel.flow.json')['valid'], True)
        run = s.ok('run_workflow', path='fuel.flow.json')
        self.assertEqual(run['status'], 'succeeded')
        row = self.summary_of(run['run_id'], 'stats')[0]
        df = pd.read_csv(self.work / 'data' / 'weekly_fuel_prices.csv')
        premium = df.diesel - df.petrol
        self.assertAlmostEqual(row['avg_premium'], premium.mean(), places=9)
        self.assertAlmostEqual(row['max_premium'], premium.max(), places=9)
        evidence = s.ok('run_evidence', run_id=run['run_id'])
        self.assertEqual(evidence['input_files'][0]['sha256'], source['sha256'], 'the result traces to the downloaded bytes')
        self.assertEqual(evidence['nodes']['stats']['tool'], 'core.summarize')


class WorkflowConstructionTests(Scenario):
    """B: clean a local sales CSV, group by category, total and average, sort, chart; stays editable."""

    def build(self, session):
        session.ok('make_folder', path='out')
        self.write_flow('sales.flow.json', flow([
            {'id': 'read', 'tool': 'core.input_data', 'config': {'path': 'sales.csv'}},
            {'id': 'clean', 'tool': 'core.data_cleansing', 'config': {'fields': ['Category'], 'trim_whitespace': True}},
            {'id': 'case', 'tool': 'core.formula', 'config': {'formulas': [{'field': 'Category', 'expression': 'TitleCase([Category])', 'type': 'v_wstring'}]}},
            {'id': 'types', 'tool': 'core.auto_field', 'config': {}},
            {'id': 'totals', 'tool': 'core.summarize', 'config': {'actions': [
                {'field': 'Category', 'op': 'group_by'}, {'field': 'Sales', 'op': 'sum', 'output': 'Total'},
                {'field': 'Sales', 'op': 'avg', 'output': 'Average'}]}},
            {'id': 'sorted', 'tool': 'core.sort', 'config': {'keys': [{'field': 'Total', 'order': 'desc'}]}},
            {'id': 'chart', 'tool': 'core.interactive_chart', 'config': {'chart_type': 'bar', 'x_field': 'Category', 'y_fields': ['Total'], 'title': 'Sales by category'}},
            {'id': 'report', 'tool': 'core.render', 'config': {'path': 'out/sales_by_category.html', 'report_field': 'Report', 'if_exists': 'overwrite'}},
            {'id': 'table', 'tool': 'core.output_data', 'config': {'path': 'out/sales_by_category.csv', 'if_exists': 'overwrite'}}],
            [edge('read', 'output', 'clean', 'input'), edge('clean', 'output', 'case', 'input'), edge('case', 'output', 'types', 'input'),
             edge('types', 'output', 'totals', 'input'), edge('totals', 'output', 'sorted', 'input'), edge('sorted', 'output', 'chart', 'input'),
             edge('chart', 'output', 'report', 'input'), edge('sorted', 'output', 'table', 'input')]))

    def test_clean_group_sort_chart_and_reopen(self):
        (self.work / 'sales.csv').write_text(SALES)
        self.build(self.session)
        run = self.session.ok('run_workflow', path='sales.flow.json')
        self.assertEqual(run['status'], 'succeeded', run)
        rows = self.summary_of(run['run_id'], 'sorted')
        df = pd.read_csv(self.work / 'sales.csv')
        df['Category'] = df.Category.str.strip().str.title()
        expected = df.groupby('Category').Sales.agg(['sum', 'mean']).sort_values('sum', ascending=False)
        self.assertEqual([r['Category'] for r in rows], list(expected.index))
        for r, (_, e) in zip(rows, expected.iterrows()):
            self.assertAlmostEqual(r['Total'], e['sum'], places=9)
            self.assertAlmostEqual(r['Average'], e['mean'], places=9)
        html = (self.work / 'out' / 'sales_by_category.html').read_text()
        self.assertIn('<svg', html)
        self.assertIn('Sales by category', html)
        self.assertEqual(pd.read_csv(self.work / 'out' / 'sales_by_category.csv').Category.tolist(), list(expected.index))
        doc = json.loads((self.work / 'sales.flow.json').read_text())
        self.assertEqual(len(doc['nodes']), 9, 'the workflow is a plain, editable document')
        again = self.session.ok('run_workflow', path='sales.flow.json')
        first, second = (self.session.ok('run_evidence', run_id=r['run_id']) for r in (run, again))
        self.assertEqual(first['nodes']['sorted']['outputs']['output']['digest'], second['nodes']['sorted']['outputs']['output']['digest'])

    @unittest.skipUnless(live_model(), 'set FLOWBENCH_ACCEPTANCE_MODEL=ollama:<model> to run with a real model')
    def test_live_model_builds_and_runs_the_sales_workflow(self):
        (self.work / 'sales.csv').write_text(SALES)
        answer = LiveAgent(live_model()).run(self.session, 'Using Flowbench, build and run a workflow on sales.csv that trims the Category text, '
                                             'totals and averages Sales by Category, sorts by total (highest first) and charts the totals. '
                                             'Save the workflow as sales.flow.json.')
        print('\nLIVE MODEL ANSWER:\n', answer, '\nTRANSCRIPT:')
        for t in self.session.transcript:
            detail = '' if t['ok'] else f" -> {json.dumps(t['result'].get('error', t['result']))[:300]}"
            print(f"  {t['tool']}({json.dumps(t['arguments'])[:200]}){detail}")
        self.assertGreater(self.session.count('run_workflow', ok=True), 0, 'the model must run a Flowbench workflow')
        run_id = next(t['result']['run_id'] for t in reversed(self.session.transcript) if t['tool'] == 'run_workflow' and t['ok'])
        self.assertEqual(self.session.ok('run_evidence', run_id=run_id)['status'], 'succeeded')


class RepairTests(Scenario):
    """D: a wrong column name fails the run; the error is read, the workflow repaired, and nothing repeated."""

    def test_failed_run_is_repaired_without_repeating_successful_steps(self):
        (self.site / 'sales.csv').write_text(SALES)
        s = self.session
        s.ok('download_file', url=self.web.url('sales.csv'))
        nodes = [{'id': 'read', 'tool': 'core.input_data', 'config': {'path': 'data/sales.csv'}},
                 {'id': 'types', 'tool': 'core.auto_field', 'config': {}},
                 {'id': 'totals', 'tool': 'core.summarize', 'config': {'actions': [{'field': 'Category', 'op': 'group_by'}, {'field': 'Sale', 'op': 'sum', 'output': 'Total'}]}}]
        edges = [edge('read', 'output', 'types', 'input'), edge('types', 'output', 'totals', 'input')]
        self.write_flow('sales.flow.json', flow(nodes, edges))
        failed, ok = s.call('run_workflow', path='sales.flow.json')
        problem = json.dumps(failed)
        self.assertFalse(ok and failed.get('status') == 'succeeded')
        self.assertIn('Sale', problem, 'the error names the bad field')
        fields = [f['name'] for f in s.ok('profile_file', path='data/sales.csv')['fields']]
        self.assertIn('Sales', fields)
        nodes[2]['config']['actions'][1]['field'] = 'Sales'
        self.write_flow('sales.flow.json', flow(nodes, edges))
        run = s.ok('run_workflow', path='sales.flow.json')
        self.assertEqual(run['status'], 'succeeded')
        self.assertEqual(s.count('download_file'), 1, 'the download was not repeated')
        totals = {r['Category']: r['Total'] for r in self.summary_of(run['run_id'], 'totals')}
        self.assertAlmostEqual(totals['Games'], 40.0)


class ExternalExecutionTests(Scenario):
    """E: retrieve API data, transform it with Python, load the result into Flowbench and analyse it."""

    def test_python_prepares_api_data_that_a_workflow_analyses(self):
        api = {'results': [{'region': {'code': 'N', 'name': 'North'}, 'measures': {'households': 120, 'spend': 1534.5}},
                           {'region': {'code': 'S', 'name': 'South'}, 'measures': {'households': 80, 'spend': 2010.0}},
                           {'region': {'code': 'N', 'name': 'North'}, 'measures': {'households': 30, 'spend': 400.0}}]}
        (self.site / 'api.json').write_text(json.dumps(api))
        s = self.session
        s.ok('download_file', url=self.web.url('api.json'), filename='api.json')
        script = s.ok('run_python', code=(
            "import json, pandas as pd\n"
            "rows = json.load(open('data/api.json'))['results']\n"
            "pd.json_normalize(rows).rename(columns=lambda c: c.split('.')[-1] if c != 'region.name' else 'region_name')"
            ".to_csv('data/api_flat.csv', index=False)\n"))
        self.assertEqual(script['exit_code'], 0, script['stderr'])
        self.assertIn(str(self.work / 'data' / 'api_flat.csv'), script['files_written'])
        self.assertTrue(Path(script['script']).is_file(), 'the Python step is recorded')
        self.write_flow('api.flow.json', flow([
            {'id': 'read', 'tool': 'core.input_data', 'config': {'path': 'data/api_flat.csv'}},
            {'id': 'types', 'tool': 'core.auto_field', 'config': {}},
            {'id': 'per_region', 'tool': 'core.summarize', 'config': {'actions': [
                {'field': 'code', 'op': 'group_by'}, {'field': 'households', 'op': 'sum', 'output': 'households'},
                {'field': 'spend', 'op': 'sum', 'output': 'spend'}]}},
            {'id': 'rate', 'tool': 'core.formula', 'config': {'formulas': [{'field': 'spend_per_household', 'expression': '[spend] / [households]', 'type': 'double'}]}}],
            [edge('read', 'output', 'types', 'input'), edge('types', 'output', 'per_region', 'input'), edge('per_region', 'output', 'rate', 'input')]))
        run = s.ok('run_workflow', path='api.flow.json')
        rows = {r['code']: r for r in self.summary_of(run['run_id'], 'rate')}
        self.assertAlmostEqual(rows['N']['spend_per_household'], (1534.5 + 400.0) / 150)
        self.assertAlmostEqual(rows['S']['spend_per_household'], 2010.0 / 80)


if __name__ == '__main__':
    unittest.main()
