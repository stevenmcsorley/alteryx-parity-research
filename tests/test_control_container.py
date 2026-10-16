"""Native Control Container gating, ordering, logging and validation."""
import tempfile
import unittest
from pathlib import Path

from flowengine.errors import WorkflowValidationError
from flowengine.executor import plan_workflow, run_workflow

from helpers import ROOT, edge, text_input, workflow


def container(cid, children, **config):
    return {'id': cid, 'tool': 'core.control_container', 'config': {'children': children, **config}}


def skipped(result):
    return sorted(e['node_id'] for e in result.events if e['type'] == 'node-skipped')


SRC = text_input('src', [('n', 'int32')], [[1], [2], [3]])


class ControlContainerTests(unittest.TestCase):
    def test_unconnected_container_runs_children_then_logs_them(self):
        doc = workflow([SRC, {'id': 'count', 'tool': 'core.count_records', 'config': {}},
                        container('cc', ['count'], caption='Totals')],
                       [edge('src', 'output', 'count', 'input')])
        result = run_workflow(doc, base_dir=ROOT)
        self.assertTrue(result.ok)
        self.assertEqual(result.output('count').column('Count'), [3])
        log = result.output('cc', 'log')
        self.assertEqual(log.schema.names, ['Container', 'NodeID', 'Tool', 'Event', 'Message', 'Records'])
        self.assertEqual([row for row in log.rows() if row[3] == 'completed'],
                         [('cc', 'count', 'core.count_records', 'completed', None, 1)])
        completed = [e['node_id'] for e in result.events if e['type'] == 'node-completed']
        self.assertLess(completed.index('count'), completed.index('cc'))

    def test_empty_control_input_skips_contents_and_their_downstream(self):
        nodes = [SRC, {'id': 'gate', 'tool': 'core.filter', 'config': {'expression': '[n] > 10'}},
                 {'id': 'inside', 'tool': 'core.count_records', 'config': {}},
                 {'id': 'outside', 'tool': 'core.browse', 'config': {}},
                 container('cc', ['inside'])]
        edges = [edge('src', 'output', 'gate', 'input'), edge('gate', 'true', 'cc', 'control'),
                 edge('src', 'output', 'inside', 'input'), edge('inside', 'output', 'outside', 'input')]
        result = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertTrue(result.ok)
        self.assertEqual(skipped(result), ['inside', 'outside'])
        self.assertEqual(len(result.output('cc', 'log')), 0)
        messages = {e['node_id']: e['message'] for e in result.events if e['type'] == 'node-skipped'}
        self.assertIn("Control Container 'cc'", messages['inside'])
        self.assertIn('downstream', messages['outside'])

        nodes[1]['config']['expression'] = '[n] > 2'  # one control record activates the container
        result = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertEqual(skipped(result), [])
        self.assertEqual(result.output('inside').column('Count'), [3])

    def test_chained_containers_run_in_sequence_through_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            nodes = [SRC, {'id': 'write', 'tool': 'core.output_data', 'config': {'path': 'stage.csv'}},
                     container('first', ['write']),
                     {'id': 'read', 'tool': 'core.input_data', 'config': {'path': 'stage.csv'}},
                     {'id': 'total', 'tool': 'core.count_records', 'config': {}},
                     container('second', ['read', 'total'])]
            edges = [edge('src', 'output', 'write', 'input'), edge('first', 'log', 'second', 'control'),
                     edge('read', 'output', 'total', 'input')]
            for workers in (1, 4):
                with self.subTest(workers=workers):
                    (Path(d) / 'stage.csv').unlink(missing_ok=True)
                    result = run_workflow(workflow(nodes, edges), base_dir=Path(d), workers=workers)
                    self.assertTrue(result.ok, result.error)
                    self.assertEqual(result.output('total').column('Count'), [3])
                    order = [e['node_id'] for e in result.events if e['type'] == 'node-completed']
                    self.assertLess(order.index('first'), order.index('read'))

    def test_inactive_container_stops_a_chain_and_nested_containers(self):
        nodes = [SRC, {'id': 'gate', 'tool': 'core.filter', 'config': {'expression': '[n] > 10'}},
                 {'id': 'a', 'tool': 'core.count_records', 'config': {}},
                 {'id': 'b', 'tool': 'core.count_records', 'config': {}},
                 container('inner', ['b']), container('outer', ['a', 'inner']),
                 {'id': 'c', 'tool': 'core.count_records', 'config': {}}, container('next', ['c'])]
        edges = [edge('src', 'output', 'gate', 'input'), edge('gate', 'true', 'outer', 'control'),
                 edge('src', 'output', 'a', 'input'), edge('src', 'output', 'b', 'input'),
                 edge('src', 'output', 'c', 'input'), edge('outer', 'log', 'next', 'control')]
        result = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertTrue(result.ok)
        self.assertEqual(skipped(result), ['a', 'b', 'c', 'inner'])

        nodes[1]['config']['expression'] = '[n] > 0'
        result = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertEqual(skipped(result), [])
        outer_log = result.output('outer', 'log')
        self.assertEqual({row[1] for row in outer_log.rows() if row[3] == 'completed'}, {'a', 'b', 'inner'})

    def test_disabled_container_skips_contents(self):
        cc = container('cc', ['count'])
        cc['disabled'] = True
        doc = workflow([SRC, {'id': 'count', 'tool': 'core.count_records', 'config': {}}, cc],
                       [edge('src', 'output', 'count', 'input')])
        result = run_workflow(doc, base_dir=ROOT)
        self.assertTrue(result.ok)
        self.assertEqual(skipped(result), ['cc', 'count'])

    def test_invalid_membership_and_control_are_reported(self):
        count = {'id': 'count', 'tool': 'core.count_records', 'config': {}}
        cases = {
            'container-child-invalid': ([SRC, container('cc', ['missing'])], []),
            'container-child-duplicate': ([SRC, count, container('a', ['count']), container('b', ['count'])],
                                          [edge('src', 'output', 'count', 'input')]),
            'container-cycle': ([SRC, container('a', ['b']), container('b', ['a'])], []),
            'container-control-inside': ([SRC, count, container('cc', ['count'])],
                                         [edge('src', 'output', 'count', 'input'), edge('count', 'output', 'cc', 'control')]),
        }
        for code, (nodes, edges) in cases.items():
            with self.subTest(code=code):
                _, problems = plan_workflow(workflow(nodes, edges), base_dir=ROOT)
                self.assertIn(code, [p['code'] for p in problems])
                with self.assertRaises(WorkflowValidationError):
                    run_workflow(workflow(nodes, edges), base_dir=ROOT)


if __name__ == '__main__':
    unittest.main()
