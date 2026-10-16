"""Runner: run workflow files named by records and route their logs by outcome."""
import json
import tempfile
import unittest
from pathlib import Path

from flowengine.executor import run_workflow
from flowengine.workflow_effects import workflow_effects

from helpers import edge, text_input, workflow

ALLOW = {'allow_processes': True}


def runner(**config):
    return {'id': 'run', 'tool': 'core.runner', 'config': {'path_field': 'path', **config}}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.save('good.flow.json', workflow(
            [text_input('t', [('n', 'int32')], [[1], [2]]),
             {'id': 'out', 'tool': 'core.output_data', 'config': {'path': 'good.csv'}}],
            [edge('t', 'output', 'out', 'input')]))
        self.save('bad.flow.json', workflow(
            [{'id': 'in', 'tool': 'core.input_data', 'config': {'path': 'missing.csv'}}], []))

    def tearDown(self):
        self._tmp.cleanup()

    def save(self, name, doc):
        (self.dir / name).write_text(json.dumps(doc), encoding='utf-8')

    def run_paths(self, paths, limits=ALLOW, **config):
        doc = workflow([text_input('paths', [('path', 'v_wstring')], [[p] for p in paths]), runner(**config)],
                       [edge('paths', 'output', 'run', 'input')])
        return run_workflow(doc, base_dir=self.dir, limits=limits)

    def test_requires_process_permission_and_is_reported_by_preflight(self):
        result = self.run_paths(['good.flow.json'], limits={})
        self.assertEqual(result.error['code'], 'permission-denied')
        self.assertFalse((self.dir / 'good.csv').exists())
        doc = workflow([text_input('paths', [('path', 'v_wstring')], []), runner()], [])
        self.assertEqual([p['tool_id'] for p in workflow_effects(doc, self.dir)['processes']], ['core.runner'])

    def test_routes_success_and_failure_logs_without_failing_the_parent(self):
        result = self.run_paths(['good.flow.json', 'bad.flow.json', 'nowhere.flow.json', ''])
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.dir / 'good.csv').read_text(encoding='utf-8').split(), ['n', '1', '2'])
        success, failure = result.output('run', 'success'), result.output('run', 'failure')
        self.assertEqual(success.schema.names, ['Workflow', 'Status', 'Sequence', 'Event', 'NodeID', 'Message'])
        self.assertEqual({row[:2] for row in success.rows()}, {('good.flow.json', 'succeeded')})
        self.assertIn(('node-completed', 'out'), [(row[3], row[4]) for row in success.rows()])
        by_workflow = {}
        for row in failure.rows():
            by_workflow.setdefault(row[0], []).append(row)
        self.assertEqual(by_workflow['bad.flow.json'][0][1], 'failed')
        self.assertIn('node-failed', [row[3] for row in by_workflow['bad.flow.json']])
        self.assertEqual(by_workflow['nowhere.flow.json'][0][1:4], ('invalid', 1, 'load-failed'))
        self.assertIn('empty path', by_workflow[''][0][5])

    def test_stop_on_failure_marks_remaining_workflows_not_run(self):
        (self.dir / 'good.csv').unlink(missing_ok=True)
        result = self.run_paths(['bad.flow.json', 'good.flow.json'], on_failure='stop')
        self.assertTrue(result.ok)
        self.assertEqual(len(result.output('run', 'success')), 0)
        self.assertIn(('good.flow.json', 'not-run'), [row[:2] for row in result.output('run', 'failure').rows()])
        self.assertFalse((self.dir / 'good.csv').exists())

    def test_children_inherit_permissions_and_recursion_is_stopped(self):
        self.save('writer.flow.json', workflow(
            [text_input('t', [('Payload', 'v_wstring')], [['b25l']]),
             {'id': 'blob', 'tool': 'core.blob_output', 'config': {'path': 'x.bin', 'blob_field': 'Payload'}}],
            [edge('t', 'output', 'blob', 'input')]))
        result = self.run_paths(['writer.flow.json'])  # processes granted, file writes not
        messages = [row[5] or '' for row in result.output('run', 'failure').rows()]
        self.assertTrue(any('disabled for this run' in m for m in messages), messages)
        self.assertFalse((self.dir / 'x.bin').exists())

        self.save('self.flow.json', workflow([text_input('paths', [('path', 'v_wstring')], [['self.flow.json']]),
                                              runner()], [edge('paths', 'output', 'run', 'input')]))
        result = self.run_paths(['self.flow.json'])
        self.assertTrue(result.ok, result.error)
        self.assertEqual({row[1] for row in result.output('run', 'success').rows()}, {'succeeded'})


if __name__ == '__main__':
    unittest.main()
