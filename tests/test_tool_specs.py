"""Tool contracts: generated for every tool, and the curated semantics hold on the engine."""
import tempfile
import unittest

from flowengine import agent, tool_specs
from flowengine.executor import run_workflow
from flowengine.plugin import default_registry

from helpers import edge, text_input, workflow


def run(nodes, edges, node, anchor='output'):
    r = run_workflow(workflow(nodes, edges), base_dir=tempfile.mkdtemp())
    assert r.status == 'succeeded', r.error
    return list(r.outputs[node][anchor].rows())


class ContractTests(unittest.TestCase):
    def test_every_tool_has_a_contract_with_purpose_settings_and_execution(self):
        ctx = agent.Context('.')
        thin = []
        for tool in default_registry().tools():
            spec = agent.describe_tool(ctx, tool.manifest.tool_id)
            self.assertIn('execution', spec['contract'])
            self.assertIsInstance(spec['contract']['errors'], list)
            if len(spec['purpose'] or '') < 30:
                thin.append(tool.manifest.tool_id)
        self.assertEqual(thin, [], 'tools whose purpose is too thin to choose them by')

    def test_errors_are_read_from_the_kernel(self):
        spec = agent.describe_tool(agent.Context('.'), 'core.summarize')['contract']
        messages = [e['message'] for e in spec['errors']]
        self.assertIn('sum requires a numeric field; {src.name} is {src.type.value}', messages, 'errors raised in helper functions are found')
        self.assertIn('{op} on {name} requires numeric values', messages)

    def test_formula_reference_lists_real_functions(self):
        ref = agent.call('formula_reference', {}, agent.Context('.'))
        names = {f['name'] for f in ref['functions']}
        self.assertTrue({'IIF', 'TITLECASE', 'DATETIMEADD', 'COALESCE'} <= names)
        self.assertEqual(next(f for f in ref['functions'] if f['name'] == 'IIF')['min_args'], 3)
        self.assertIn('core.formula', [t for t in ('core.formula',) if 'expression_settings' in tool_specs.contract(default_registry().get(t))])


class SemanticsTests(unittest.TestCase):
    """Each curated rule in tool_specs.SEMANTICS, checked on the engine."""

    def test_formula_null_division_case_and_in(self):
        rows = run([text_input('t', [('a', 'double'), ('s', 'v_wstring')], [['1', 'x'], ['0', None]]),
                    {'id': 'f', 'tool': 'core.formula', 'config': {'formulas': [
                        {'field': 'div', 'expression': '10 / [a]', 'type': 'double'},
                        {'field': 'cat', 'expression': '[s] + "!"', 'type': 'v_wstring'},
                        {'field': 'cmp', 'expression': '[s] = "x"', 'type': 'bool'},
                        {'field': 'nul', 'expression': '[a] + NULL()', 'type': 'double'},
                        {'field': 'case', 'expression': 'iif([a] > 0, "pos", "zero")', 'type': 'v_wstring'},
                        {'field': 'member', 'expression': '[s] IN ("x", "y")', 'type': 'bool'},
                        {'field': 'later', 'expression': '[div] * 2', 'type': 'double'}]}}],
                   [edge('t', 'output', 'f', 'input')], 'f')
        self.assertEqual(rows[0][2:], (10.0, 'x!', True, None, 'pos', True, 20.0))
        self.assertEqual(rows[1][2:], (None, '!', False, None, 'zero', False, None))

    def test_summarize_null_handling_and_names(self):
        rows = run([text_input('t', [('g', 'v_wstring'), ('v', 'int32')], [['a', '1'], ['a', None], ['a', '3'], [None, '5']]),
                    {'id': 's', 'tool': 'core.summarize', 'config': {'actions': [
                        {'field': 'g', 'op': 'group_by'}, {'field': 'v', 'op': 'count'}, {'field': 'v', 'op': 'count_non_null'},
                        {'field': 'v', 'op': 'sum'}, {'field': 'v', 'op': 'avg'}, {'field': 'v', 'op': 'std_dev'}, {'field': 'v', 'op': 'first'}]}}],
                   [edge('t', 'output', 's', 'input')], 's')
        self.assertEqual(rows[0], (None, 1, 1, 5, 5.0, 0.0, 5), 'null is its own group, sorted first; one value has std_dev 0')
        self.assertEqual(rows[1], ('a', 3, 2, 4, 2.0, 2 ** 0.5, 1))

    def test_filter_routes_null_to_false_and_union_fills_missing_fields(self):
        t = text_input('t', [('v', 'int32')], [['1'], [None], ['3']])
        f = {'id': 'f', 'tool': 'core.filter', 'config': {'expression': '[v] > 1'}}
        self.assertEqual(run([t, f], [edge('t', 'output', 'f', 'input')], 'f', 'false'), [(1,), (None,)])
        a = text_input('a', [('k', 'v_wstring'), ('x', 'int32')], [['p', '1']])
        b = text_input('b', [('k', 'v_wstring')], [['q']])
        u = {'id': 'u', 'tool': 'core.union', 'config': {}}
        self.assertEqual(run([a, b, u], [edge('a', 'output', 'u', 'inputs'), edge('b', 'output', 'u', 'inputs')], 'u'), [('p', 1), ('q', None)])

    def test_join_nulls_match_and_unique_keeps_first(self):
        l = text_input('l', [('k', 'v_wstring'), ('a', 'int32')], [[None, '1'], ['x', '2']])
        r = text_input('r', [('k', 'v_wstring'), ('b', 'int32')], [[None, '9']])
        j = {'id': 'j', 'tool': 'core.join', 'config': {'by': 'fields', 'keys': [{'left': 'k', 'right': 'k'}]}}
        joined = run([l, r, j], [edge('l', 'output', 'j', 'left'), edge('r', 'output', 'j', 'right')], 'j', 'joined')
        self.assertEqual(len(joined), 1)
        u = {'id': 'u', 'tool': 'core.unique', 'config': {'fields': ['k']}}
        t = text_input('t', [('k', 'v_wstring'), ('n', 'int32')], [['a', '1'], ['b', '2'], ['a', '3']])
        self.assertEqual(run([t, u], [edge('t', 'output', 'u', 'input')], 'u', 'unique'), [('a', 1), ('b', 2)])


if __name__ == '__main__':
    unittest.main()
