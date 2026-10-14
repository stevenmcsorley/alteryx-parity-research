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


class StandardMacroTests(unittest.TestCase):
    def test_macro_input_filter_formula_output_matches_direct_workflow(self):
        macro_doc, direct_doc = standard_macro_workflow()
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
