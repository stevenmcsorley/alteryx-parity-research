"""Executable standard workflow macro tests: real child DAGs and typed table boundaries."""
import unittest
import tempfile
from pathlib import Path

from flowengine.executor import run_workflow
from helpers import ROOT, edge, rows, text_input, workflow


def standard_macro_workflow():
    data = text_input('source', [('customer', 'string'), ('amount', 'int32')],
                      [['A', 5], ['B', 12], ['C', 20]])
    child = workflow([
        {'id': 'mi', 'tool': 'flowengine.macro_input', 'config': {'name': 'Customers'}},
        {'id': 'filter', 'tool': 'core.filter', 'config': {'expression': '[amount] >= 10'}},
        {'id': 'formula', 'tool': 'core.formula', 'config': {
            'formulas': [{'field': 'doubled', 'expression': '[amount] * 2', 'type': 'int32'}]}},
        {'id': 'mo', 'tool': 'flowengine.macro_output', 'config': {'name': 'Sales'}},
    ], [edge('mi', 'output', 'filter', 'input'), edge('filter', 'true', 'formula', 'input'),
        edge('formula', 'output', 'mo', 'input')], name='Customer transform')
    macro = {'id': 'macro', 'tool': 'flowengine.macro', 'config': {
        'workflow': child, 'input_bindings': {'Customers': 'input'}, 'output_name': 'Sales'}}
    outer = workflow([data, macro, {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
                     [edge('source', 'output', 'macro', 'input'), edge('macro', 'output', 'browse', 'input')],
                     name='Standard macro acceptance')
    direct = workflow([data,
        {'id': 'filter', 'tool': 'core.filter', 'config': {'expression': '[amount] >= 10'}},
        {'id': 'formula', 'tool': 'core.formula', 'config': {
            'formulas': [{'field': 'doubled', 'expression': '[amount] * 2', 'type': 'int32'}]}}],
        [edge('source', 'output', 'filter', 'input'), edge('filter', 'true', 'formula', 'input')])
    return outer, direct


def batch_macro_workflow(control_rows, *, max_iterations=10):
    data = text_input('source', [('value', 'int32')], [[1], [2], [3]])
    controls = text_input('controls', [('threshold', 'int32')], control_rows)
    child = workflow([
        {'id': 'input', 'tool': 'flowengine.macro_input', 'config': {'name': 'Input'}},
        {'id': 'filter', 'tool': 'core.filter', 'config': {
            'field': 'value', 'operator': '>=', 'value': 0}},
        {'id': 'output', 'tool': 'flowengine.macro_output', 'config': {'name': 'Output'}},
    ], [edge('input', 'output', 'filter', 'input'), edge('filter', 'true', 'output', 'input')])
    batch = {'id': 'batch', 'tool': 'flowengine.batch_macro', 'config': {
        'workflow': child, 'input_name': 'Input', 'output_name': 'Output',
        'parameters': [{'name': 'threshold', 'field': 'threshold'}],
        'actions': [{'parameter': 'threshold', 'target_node': 'filter', 'config_path': '/config/value'}],
        'max_iterations': max_iterations}}
    doc = workflow([data, controls, batch, {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
                   [edge('source', 'output', 'batch', 'input'), edge('controls', 'output', 'batch', 'control'),
                    edge('batch', 'output', 'browse', 'input')], name='Batch macro acceptance')
    return doc


def iterative_macro_workflow(rows_in, *, max_iterations=5):
    source = text_input('source', [('counter', 'int32')], rows_in)
    child = workflow([
        {'id': 'input', 'tool': 'flowengine.macro_input', 'config': {'name': 'Input'}},
        {'id': 'increment', 'tool': 'core.formula', 'config': {'formulas': [
            {'field': 'counter', 'expression': '[counter] + 1'}]}},
        {'id': 'output', 'tool': 'flowengine.macro_output', 'config': {'name': 'Output'}},
    ], [edge('input', 'output', 'increment', 'input'), edge('increment', 'output', 'output', 'input')])
    iterative = {'id': 'iterative', 'tool': 'flowengine.iterative_macro', 'config': {
        'workflow': child, 'input_name': 'Input', 'output_name': 'Output',
        'terminate_expression': '[counter] >= 3', 'max_iterations': max_iterations}}
    return workflow([source, iterative, {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
                    [edge('source', 'output', 'iterative', 'input'),
                     edge('iterative', 'output', 'browse', 'input')], name='Iterative macro acceptance')


class StandardMacroTests(unittest.TestCase):
    def test_macro_input_filter_formula_output_matches_direct_workflow(self):
        macro_doc, direct_doc = standard_macro_workflow()
        from flowengine.executor import plan_workflow
        plan, problems = plan_workflow(macro_doc, base_dir=ROOT)
        self.assertEqual(problems, [])
        self.assertEqual(plan.schemas[('macro', 'output')].names, ['customer', 'amount', 'doubled'])
        result = run_workflow(macro_doc, base_dir=ROOT)
        direct = run_workflow(direct_doc, base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(rows(result.output('macro', 'output')), rows(direct.output('formula')))
        self.assertEqual(rows(result.output('macro', 'output')), [['B', 12, 24], ['C', 20, 40]])
        self.assertEqual(len(result.browse['browse']), 2)
        started = [e for e in result.events if e['type'] == 'macro-started']
        completed = [e for e in result.events if e['type'] == 'macro-completed']
        self.assertEqual((len(started), len(completed)), (1, 1))
        self.assertEqual(completed[0]['rows'], 2)

    def test_nested_macro_executes_once_at_each_level(self):
        data = text_input('source', [('value', 'int32')], [[4], [7]])
        leaf = workflow([
            {'id': 'input', 'tool': 'flowengine.macro_input', 'config': {'name': 'LeafInput'}},
            {'id': 'formula', 'tool': 'core.formula', 'config': {
                'formulas': [{'field': 'value2', 'expression': '[value] * 2', 'type': 'int32'}]}},
            {'id': 'output', 'tool': 'flowengine.macro_output', 'config': {'name': 'LeafOutput'}},
        ], [edge('input', 'output', 'formula', 'input'), edge('formula', 'output', 'output', 'input')])
        parent_definition = workflow([
            {'id': 'input', 'tool': 'flowengine.macro_input', 'config': {'name': 'ParentInput'}},
            {'id': 'nested', 'tool': 'flowengine.macro', 'config': {
                'workflow': leaf, 'input_bindings': {'LeafInput': 'input'}, 'output_name': 'LeafOutput'}},
            {'id': 'output', 'tool': 'flowengine.macro_output', 'config': {'name': 'ParentOutput'}},
        ], [edge('input', 'output', 'nested', 'input'), edge('nested', 'output', 'output', 'input')])
        outer = workflow([data, {'id': 'macro', 'tool': 'flowengine.macro', 'config': {
            'workflow': parent_definition, 'input_bindings': {'ParentInput': 'input'},
            'output_name': 'ParentOutput'}}, {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
            [edge('source', 'output', 'macro', 'input'), edge('macro', 'output', 'browse', 'input')])
        result = run_workflow(outer, base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(rows(result.output('macro', 'output')), [[4, 8], [7, 14]])
        nested = [event for event in result.events if event['type'] == 'macro-nested-event']
        self.assertEqual(sum(event['type'] == 'macro-started' for event in result.events), 1)
        self.assertEqual(sum(event['type'] == 'macro-completed' for event in result.events), 1)
        self.assertEqual(sum(event['event_type'] == 'macro-started' for event in nested), 1)
        self.assertEqual(sum(event['event_type'] == 'macro-completed' for event in nested), 1)

    def test_macro_binds_multiple_parent_connections_to_distinct_child_inputs(self):
        left = text_input('left', [('id', 'int32'), ('left_value', 'string')], [[1, 'L1'], [2, 'L2']])
        right = text_input('right', [('id', 'int32'), ('right_value', 'string')], [[1, 'R1'], [3, 'R3']])
        child = workflow([
            {'id': 'left-in', 'tool': 'flowengine.macro_input', 'config': {'name': 'Left'}},
            {'id': 'right-in', 'tool': 'flowengine.macro_input', 'config': {'name': 'Right'}},
            {'id': 'join', 'tool': 'core.join', 'config': {'by': 'fields',
                'keys': [{'left': 'id', 'right': 'id'}]}},
            {'id': 'out', 'tool': 'flowengine.macro_output', 'config': {'name': 'Joined'}},
        ], [edge('left-in', 'output', 'join', 'left'), edge('right-in', 'output', 'join', 'right'),
            edge('join', 'joined', 'out', 'input')], name='Two input macro')
        macro = {'id': 'macro', 'tool': 'flowengine.macro', 'config': {
            'workflow': child, 'input_bindings': {'Left': 'input[0]', 'Right': 'input[1]'},
            'output_name': 'Joined'}}
        outer = workflow([left, right, macro, {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
                         [edge('left', 'output', 'macro', 'input'), edge('right', 'output', 'macro', 'input'),
                          edge('macro', 'output', 'browse', 'input')])
        from flowengine.executor import plan_workflow
        plan, problems = plan_workflow(outer, base_dir=ROOT)
        self.assertEqual(problems, [])
        self.assertEqual(plan.schemas[('macro', 'output')].names,
                         ['id', 'left_value', 'Right_id', 'right_value'])
        result = run_workflow(outer, base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(rows(result.output('macro', 'output')), [[1, 'L1', 1, 'R1']])

        macro['config']['input_bindings'] = {'Left': 'input', 'Right': 'input'}
        from flowengine.errors import WorkflowValidationError
        with self.assertRaises(WorkflowValidationError) as caught:
            run_workflow(outer, base_dir=ROOT)
        self.assertIn('select one with an indexed binding', caught.exception.message)

        macro['config']['input_bindings'] = {'Left': 'unknown[0]', 'Right': 'input[1]'}
        with self.assertRaises(WorkflowValidationError) as caught:
            run_workflow(outer, base_dir=ROOT)
        self.assertIn("unconnected parent anchor 'unknown'", caught.exception.message)

    def test_imported_yxmc_macro_input_output_runs_from_relative_path(self):
        from flowengine.interop.yxmd import import_file
        macro_xml = '''<?xml version="1.0"?>
<AlteryxDocument yxmdVer="2026.2.0.8"><Nodes>
 <Node ToolID="1"><GuiSettings Plugin="AlteryxBasePluginsGui.MacroInput.MacroInput"/>
  <Properties><Configuration><Value name="Name">Customers</Value></Configuration></Properties></Node>
 <Node ToolID="2"><GuiSettings Plugin="AlteryxBasePluginsGui.MacroOutput.MacroOutput"/>
  <Properties><Configuration><Value name="Name">Results</Value></Configuration></Properties></Node>
</Nodes><Connections>
 <Connection name="Connection 1"><Origin ToolID="1" Connection="Output"/>
  <Destination ToolID="2" Connection="Input"/></Connection>
</Connections></AlteryxDocument>'''
        with tempfile.TemporaryDirectory() as temp:
            macro_path = Path(temp) / 'Identity.yxmc'
            macro_path.write_text(macro_xml, encoding='utf-8')
            imported, report = import_file(macro_path)
            self.assertEqual(report['counts'], {'mapped': 2})
            doc = workflow([text_input('source', [('id', 'int32')], [[3], [8]]),
                            {'id': 'macro', 'tool': 'flowengine.macro', 'config': {
                                'definition_path': 'Identity.yxmc', 'output_name': 'Results'}},
                            {'id': 'browse', 'tool': 'core.browse', 'config': {}}],
                           [edge('source', 'output', 'macro', 'input'), edge('macro', 'output', 'browse', 'input')])
            result = run_workflow(doc, base_dir=Path(temp))
            self.assertTrue(result.ok, result.error)
            self.assertEqual(rows(result.output('macro', 'output')), [[3], [8]])

    def test_imported_macro_with_unmapped_input_settings_is_rejected(self):
        from flowengine.interop.yxmd import import_file
        macro_xml = '''<?xml version="1.0"?>
<AlteryxDocument yxmdVer="2026.2.0.8"><Nodes>
 <Node ToolID="1"><GuiSettings Plugin="AlteryxBasePluginsGui.MacroInput.MacroInput"/>
  <Properties><Configuration><Value name="Name">Customers</Value>
   <Value name="UseFileInput">True</Value></Configuration></Properties></Node>
 <Node ToolID="2"><GuiSettings Plugin="AlteryxBasePluginsGui.MacroOutput.MacroOutput"/>
  <Properties><Configuration><Value name="Name">Results</Value></Configuration></Properties></Node>
</Nodes><Connections>
 <Connection name="Connection 1"><Origin ToolID="1" Connection="Output"/>
  <Destination ToolID="2" Connection="Input"/></Connection>
</Connections></AlteryxDocument>'''
        with tempfile.TemporaryDirectory() as temp:
            macro_path = Path(temp) / 'Configured.yxmc'
            macro_path.write_text(macro_xml, encoding='utf-8')
            _, report = import_file(macro_path)
            self.assertEqual(report['counts'], {'partial': 1, 'mapped': 1})
            doc = workflow([text_input('source', [('id', 'int32')], [[3]]),
                            {'id': 'macro', 'tool': 'flowengine.macro', 'config': {
                                'definition_path': 'Configured.yxmc', 'output_name': 'Results'}}],
                           [edge('source', 'output', 'macro', 'input')])
            from flowengine.errors import WorkflowValidationError
            with self.assertRaises(WorkflowValidationError) as caught:
                run_workflow(doc, base_dir=Path(temp))
            self.assertIn('unmapped settings', caught.exception.message)
            self.assertIn('UseFileInput', caught.exception.message)

    def test_imported_macro_with_unmapped_anchor_is_rejected(self):
        from flowengine.interop.yxmd import import_file
        macro_xml = '''<?xml version="1.0"?>
<AlteryxDocument yxmdVer="2026.2.0.8"><Nodes>
 <Node ToolID="1"><GuiSettings Plugin="AlteryxBasePluginsGui.MacroInput.MacroInput"/>
  <Properties><Configuration><Value name="Name">Customers</Value></Configuration></Properties></Node>
 <Node ToolID="2"><GuiSettings Plugin="AlteryxBasePluginsGui.MacroOutput.MacroOutput"/>
  <Properties><Configuration><Value name="Name">Results</Value></Configuration></Properties></Node>
</Nodes><Connections>
 <Connection name="Connection 1"><Origin ToolID="1" Connection="Output"/>
  <Destination ToolID="2" Connection="UnmappedAnchor"/></Connection>
</Connections></AlteryxDocument>'''
        with tempfile.TemporaryDirectory() as temp:
            macro_path = Path(temp) / 'BadAnchor.yxmc'
            macro_path.write_text(macro_xml, encoding='utf-8')
            _, report = import_file(macro_path)
            self.assertTrue(report['edge_warnings'])
            doc = workflow([text_input('source', [('id', 'int32')], [[3]]),
                            {'id': 'macro', 'tool': 'flowengine.macro', 'config': {
                                'definition_path': 'BadAnchor.yxmc', 'output_name': 'Results'}}],
                           [edge('source', 'output', 'macro', 'input')])
            from flowengine.errors import WorkflowValidationError
            with self.assertRaises(WorkflowValidationError) as caught:
                run_workflow(doc, base_dir=Path(temp))
            self.assertIn('unmapped imported anchors', caught.exception.message)


class BatchMacroTests(unittest.TestCase):
    def test_control_records_reconfigure_and_run_in_input_order(self):
        result = run_workflow(batch_macro_workflow([[3], [2]]), base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(rows(result.output('batch', 'output')), [[3], [2], [3]])
        events = [event for event in result.events if event['type'] == 'batch-iteration-completed']
        self.assertEqual([event['iteration'] for event in events], [1, 2])
        self.assertEqual([event['rows'] for event in events], [1, 2])
        self.assertTrue(all(event['child_run_id'] for event in events))

    def test_empty_control_table_returns_inferred_empty_schema(self):
        result = run_workflow(batch_macro_workflow([]), base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        table = result.output('batch', 'output')
        self.assertEqual(len(table), 0)
        self.assertEqual(table.schema.names, ['value'])

    def test_batch_iteration_limit_and_bad_action_fail_explicitly(self):
        limited = run_workflow(batch_macro_workflow([[1], [2]], max_iterations=1), base_dir=ROOT)
        self.assertFalse(limited.ok)
        self.assertEqual(limited.error['node_id'], 'batch')
        self.assertIn('iteration limit', limited.error['message'])

        bad = batch_macro_workflow([[2]])
        bad['nodes'][2]['config']['actions'][0]['config_path'] = '/tool/core.filter'
        failed = run_workflow(bad, base_dir=ROOT)
        self.assertFalse(failed.ok)
        self.assertIn('must target an existing child tool config path', failed.error['message'])

    def test_batch_child_validation_failure_reports_iteration_and_call_node(self):
        doc = batch_macro_workflow([[2]])
        doc['nodes'][2]['config']['actions'][0]['config_path'] = '/config/field'
        result = run_workflow(doc, base_dir=ROOT)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['node_id'], 'batch')
        self.assertEqual(result.error['code'], 'batch-macro-iteration-failed')
        self.assertIn('iteration 1 failed validation', result.error['message'])


class IterativeMacroTests(unittest.TestCase):
    def test_state_is_fed_back_until_all_rows_meet_termination(self):
        result = run_workflow(iterative_macro_workflow([[0], [2]]), base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(rows(result.output('iterative', 'output')), [[3], [5]])
        passes = [event for event in result.events if event['type'] == 'iterative-pass-completed']
        self.assertEqual([event['converged'] for event in passes], [False, False, True])
        self.assertEqual([event['iteration'] for event in passes], [1, 2, 3])

    def test_zero_rows_terminate_and_iteration_limit_is_a_failure(self):
        empty = run_workflow(iterative_macro_workflow([]), base_dir=ROOT)
        self.assertTrue(empty.ok, empty.error)
        self.assertEqual(len(empty.output('iterative', 'output')), 0)
        self.assertTrue(next(e for e in empty.events if e['type'] == 'iterative-pass-completed')['converged'])

        limited = run_workflow(iterative_macro_workflow([[0]], max_iterations=2), base_dir=ROOT)
        self.assertFalse(limited.ok)
        self.assertEqual(limited.error['code'], 'iterative-macro-limit')
        self.assertIn('did not converge', limited.error['message'])

    def test_macro_child_failure_reports_call_node_and_stack(self):
        doc, _ = standard_macro_workflow()
        doc['nodes'][1]['config']['workflow']['nodes'][1]['config']['expression'] = '[missing] > 0'
        result = run_workflow(doc, base_dir=ROOT)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['node_id'], 'macro')
        self.assertEqual(result.error['code'], 'macro-execution-failed')
        self.assertIn('call stack:', result.error['message'])

    def test_recursive_macro_definition_is_rejected(self):
        terminal = workflow([{'id': 'in', 'tool': 'flowengine.macro_input', 'config': {'name': 'Input'}},
                             {'id': 'out', 'tool': 'flowengine.macro_output', 'config': {'name': 'Output'}}],
                            [edge('in', 'output', 'out', 'input')])
        nested = {'id': 'self', 'tool': 'flowengine.macro', 'config': {
            'workflow': terminal, 'macro_id': 'outer-definition', 'input_bindings': {'Input': 'input'}}}
        recursive = workflow([{'id': 'in', 'tool': 'flowengine.macro_input', 'config': {'name': 'Input'}}, nested,
                               {'id': 'out', 'tool': 'flowengine.macro_output', 'config': {'name': 'Output'}}],
                              [edge('in', 'output', 'self', 'input'), edge('self', 'output', 'out', 'input')])
        outer = workflow([text_input('src', [('v', 'int32')], [[1]]),
                          {'id': 'call', 'tool': 'flowengine.macro', 'config': {
                              'workflow': recursive, 'macro_id': 'outer-definition',
                              'input_bindings': {'Input': 'input'}}},
                          {'id': 'sink', 'tool': 'core.browse', 'config': {}}],
                         [edge('src', 'output', 'call', 'input'), edge('call', 'output', 'sink', 'input')])
        result = run_workflow(outer, base_dir=ROOT)
        self.assertFalse(result.ok)
        self.assertIn('Recursive macro invocation detected', result.error['message'])


if __name__ == '__main__':
    unittest.main()
