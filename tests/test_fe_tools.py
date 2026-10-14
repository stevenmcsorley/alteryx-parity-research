"""Per-tool kernel tests over original fixtures (engine contract; not vendor conformance)."""
import datetime as dt
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from flowengine.executor import run_workflow
from flowengine.types import FieldType as T

from helpers import ROOT, edge, fixed_clock, names, rows, run_tool, run_tool_result, text_input, workflow

PEOPLE = ([('id', 'int32'), ('name', 'v_string'), ('region', 'v_string'), ('score', 'double')],
          [[1, 'Ada', 'N', 75], [2, 'Ben', 'S', None], [3, 'Cy', 'N', 92.5], [4, 'Di', None, 0], [5, 'Ed', 'S', -5]])


class InOutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, nodes, edges):
        return run_workflow(workflow(nodes, edges), base_dir=self.dir, clock=fixed_clock)

    def test_csv_untyped_reads_text_and_preserves_unicode(self):
        (self.dir / 'a.csv').write_text('﻿k,v\n1,zoë\n2,"x, y"\n', encoding='utf-8')
        r = self._run([{'id': 'i', 'tool': 'core.input_data', 'config': {'path': 'a.csv'}}], [])
        t = r.output('i')
        self.assertEqual(names(t), ['k', 'v'])
        self.assertEqual(rows(t), [['1', 'zoë'], ['2', 'x, y']])
        self.assertEqual(t.schema.field('k').type, T.V_WSTRING)

    def test_csv_typed_conversion_error_policy(self):
        (self.dir / 'a.csv').write_text('n\n1\nx\n3\n', encoding='utf-8')
        node = {'id': 'i', 'tool': 'core.input_data',
                'config': {'path': 'a.csv', 'fields': [{'name': 'n', 'type': 'int32'}]}}
        r = self._run([node], [])
        self.assertEqual(r.status, 'failed')
        self.assertEqual(r.error['code'], 'data-error')
        self.assertIn('row 2', r.error['message'])
        node['config']['on_conversion_error'] = 'null'
        r = self._run([node], [])
        self.assertEqual(rows(r.output('i')), [[1], [None], [3]])
        self.assertEqual(len(r.warnings()), 1)

    def test_csv_errors(self):
        cases = {
            'missing.csv': ('data-error', 'not found'),
        }
        (self.dir / 'dup.csv').write_text('a,a\n1,2\n', encoding='utf-8')
        (self.dir / 'ragged.csv').write_text('a,b\n1\n', encoding='utf-8')
        (self.dir / 'empty.csv').write_text('', encoding='utf-8')
        (self.dir / 'bad.csv').write_bytes(b'a\n\xff\xfe\n')
        cases.update({'dup.csv': ('data-error', 'duplicate header'), 'ragged.csv': ('data-error', 'expected 2'),
                      'empty.csv': ('data-error', 'empty'), 'bad.csv': ('data-error', 'decode')})
        for f, (code, msg) in cases.items():
            r = self._run([{'id': 'i', 'tool': 'core.input_data', 'config': {'path': f}}], [])
            self.assertEqual(r.error['code'], code, f)
            self.assertIn(msg, r.error['message'], f)

    def test_ragged_pad_and_no_header_and_limits(self):
        (self.dir / 'r.txt').write_text('1|2|3\n4\n5|6|7\n', encoding='utf-8')
        r = self._run([{'id': 'i', 'tool': 'core.input_data', 'config': {
            'path': 'r.txt', 'format': 'delimited', 'delimiter': '|', 'has_header': False,
            'ragged_rows': 'pad', 'max_records': 2}}], [])
        self.assertEqual(names(r.output('i')), ['Field_1', 'Field_2', 'Field_3'])
        self.assertEqual(rows(r.output('i')), [['1', '2', '3'], ['4', None, None]])

    def test_json_input_types_and_nested(self):
        (self.dir / 'a.json').write_text(json.dumps([{'a': 1, 'b': 1.5, 'c': True, 'd': {'x': 1}},
                                                     {'a': 2, 'b': 2, 'e': 'new'}]), encoding='utf-8')
        r = self._run([{'id': 'i', 'tool': 'core.input_data', 'config': {'path': 'a.json'}}], [])
        t = r.output('i')
        self.assertEqual([f.type for f in t.schema], [T.INT64, T.DOUBLE, T.BOOL, T.V_WSTRING, T.V_WSTRING])
        self.assertEqual(rows(t), [[1, 1.5, True, '{"x": 1}', None], [2, 2, None, None, 'new']])

    def test_output_roundtrip_csv_json_and_if_exists(self):
        nodes = [text_input('t', [('a', 'int32'), ('b', 'v_wstring'), ('d', 'date')],
                            [[1, 'x,"y"', '2024-01-02'], [None, 'ü', None]]),
                 {'id': 'o', 'tool': 'core.output_data', 'config': {'path': 'out/x.csv', 'null_text': ''}},
                 {'id': 'j', 'tool': 'core.output_data', 'config': {'path': 'out/x.jsonl'}}]
        r = self._run(nodes, [edge('t', 'output', 'o', 'input'), edge('t', 'output', 'j', 'input')])
        self.assertTrue(r.ok, r.error)
        self.assertEqual((self.dir / 'out/x.csv').read_text(encoding='utf-8'), 'a,b,d\n1,"x,""y""",2024-01-02\n,ü,\n')
        self.assertEqual([json.loads(l) for l in (self.dir / 'out/x.jsonl').read_text(encoding='utf-8').splitlines()],
                         [{'a': 1, 'b': 'x,"y"', 'd': '2024-01-02'}, {'a': None, 'b': 'ü', 'd': None}])
        nodes[1]['config']['if_exists'] = 'error'
        r = self._run(nodes[:2], [edge('t', 'output', 'o', 'input')])
        self.assertEqual(r.error['code'], 'data-error')
        self.assertFalse(list((self.dir / 'out').glob('.flowengine-*')), 'temp files must be cleaned up')

    def test_text_input_validation(self):
        r = run_tool_result('core.browse', {}, {'input': ([('a', 'int32')], [['x']])})
        self.assertEqual(r.error['code'], 'data-error')

    def test_browse_captures(self):
        _, r = run_tool('core.browse', {}, {'input': PEOPLE})
        self.assertEqual(len(r.browse['subject']), 5)

    def test_date_time_now_uses_injected_clock(self):
        r = run_workflow(workflow([{'id': 'n', 'tool': 'core.date_time_now', 'config': {'output': 'date'}}], []),
                         base_dir=ROOT, clock=fixed_clock)
        self.assertEqual(rows(r.output('n')), [[dt.date(2026, 1, 15)]])

    def test_directory_listing_sorted_and_filtered(self):
        for n in ('b.csv', 'a.csv', 'c.txt', '.hidden.csv'):
            (self.dir / n).write_text('x', encoding='utf-8')
        (self.dir / 'sub').mkdir()
        (self.dir / 'sub' / 'd.csv').write_text('xy', encoding='utf-8')
        r = self._run([{'id': 'd', 'tool': 'core.directory', 'config': {'path': '.', 'pattern': '*.csv',
                                                                         'recursive': True}}], [])
        self.assertEqual(r.output('d').column('FileName'), ['a.csv', 'b.csv', 'd.csv'])
        self.assertEqual(r.output('d').column('SizeBytes'), [1, 1, 2])


class PreparationTests(unittest.TestCase):
    def test_filter_basic_and_custom_partition(self):
        out, _ = run_tool('core.filter', {'field': 'score', 'operator': '>', 'value': 50}, {'input': PEOPLE})
        self.assertEqual(out['true'].column('id'), [1, 3])
        self.assertEqual(out['false'].column('id'), [2, 4, 5])  # null score goes to False
        out, _ = run_tool('core.filter', {'expression': '[region] = "S" OR IsNull([region])'}, {'input': PEOPLE})
        self.assertEqual(out['true'].column('id'), [2, 4, 5])
        out, _ = run_tool('core.filter', {'field': 'region', 'operator': 'is_null'}, {'input': PEOPLE})
        self.assertEqual(out['true'].column('id'), [4])

    def test_filter_rejects_non_boolean_and_bad_config(self):
        from flowengine.errors import WorkflowValidationError
        with self.assertRaisesRegex(WorkflowValidationError, 'must be boolean'):
            run_tool_result('core.filter', {'expression': '[name]'}, {'input': PEOPLE})
        with self.assertRaises(WorkflowValidationError) as cm:
            run_tool_result('core.filter', {'field': 'nope', 'operator': '='}, {'input': PEOPLE})
        self.assertIn('requires a value', str(cm.exception))

    def test_formula_sequential_and_types(self):
        out, _ = run_tool('core.formula', {'formulas': [
            {'field': 'double_score', 'expression': '[score] * 2'},
            {'field': 'label', 'expression': '[name] + ":" + ToString([double_score])'},
            {'field': 'score', 'expression': 'IIF(IsNull([score]), 0, [score])'},
            {'field': 'big', 'expression': '[score] > 50', 'type': 'bool'}]}, {'input': PEOPLE})
        t = out['output']
        self.assertEqual(names(t), ['id', 'name', 'region', 'score', 'double_score', 'label', 'big'])
        self.assertEqual(t.column('double_score'), [150.0, None, 185.0, 0.0, -10.0])
        self.assertEqual(t.column('label'), ['Ada:150', 'Ben:', 'Cy:185', 'Di:0', 'Ed:-10'])  # ToString(null) = ''
        self.assertEqual(t.column('score'), [75.0, 0.0, 92.5, 0.0, -5.0])
        self.assertEqual(t.schema.field('double_score').type, T.DOUBLE)

    def test_formula_conversion_policy(self):
        r = run_tool_result('core.formula', {'formulas': [{'field': 'n', 'expression': '[name]', 'type': 'int32'}]},
                            {'input': PEOPLE})
        self.assertEqual(r.error['code'], 'data-error')
        out, r = run_tool('core.formula', {'on_conversion_error': 'null',
                                           'formulas': [{'field': 'n', 'expression': '[name]', 'type': 'int32'}]},
                          {'input': PEOPLE})
        self.assertEqual(out['output'].column('n'), [None] * 5)
        self.assertTrue(r.warnings())

    def test_select_rename_retype_reorder_unknown(self):
        out, _ = run_tool('core.select', {'fields': [
            {'field': 'score', 'type': 'int32'}, {'field': 'name', 'rename': 'who'},
            {'field': 'region', 'selected': False}], 'on_conversion_error': 'null'}, {'input': PEOPLE})
        t = out['output']
        self.assertEqual(names(t), ['score', 'who', 'id'])
        self.assertEqual(t.column('score'), [75, None, 93, 0, -5])  # half away from zero [ref:probe-select-int-rounding]
        out, _ = run_tool('core.select', {'fields': [{'field': 'id'}], 'unknown_fields': 'exclude'}, {'input': PEOPLE})
        self.assertEqual(names(out['output']), ['id'])
        from flowengine.errors import WorkflowValidationError
        with self.assertRaises(WorkflowValidationError) as cm:
            run_tool_result('core.select', {'fields': [{'field': 'zzz'}]}, {'input': PEOPLE})
        self.assertEqual(cm.exception.problems[0]['code'], 'schema-invalid')
        out, r = run_tool('core.select', {'fields': [{'field': 'zzz'}, {'field': 'id'}], 'missing_fields': 'ignore',
                                          'unknown_fields': 'exclude'}, {'input': PEOPLE})
        self.assertEqual(names(out['output']), ['id'])
        self.assertTrue(r.warnings())

    def test_sort_multikey_stable_nulls(self):
        out, _ = run_tool('core.sort', {'keys': [{'field': 'region'}, {'field': 'score', 'order': 'desc'}]},
                          {'input': PEOPLE})
        self.assertEqual(out['output'].column('id'), [4, 3, 1, 5, 2])  # null last when descending
        data = ([('k', 'v_string'), ('i', 'int32')], [['b', 1], ['A', 2], ['a', 3], ['B', 4]])
        out, _ = run_tool('core.sort', {'keys': [{'field': 'k'}], 'case_insensitive': True}, {'input': data})
        self.assertEqual(out['output'].column('i'), [2, 3, 1, 4])

    def test_record_id_variants(self):
        out, _ = run_tool('core.record_id', {}, {'input': PEOPLE})
        self.assertEqual(out['output'].column('RecordID'), [1, 2, 3, 4, 5])
        self.assertEqual(names(out['output'])[0], 'RecordID')
        out, _ = run_tool('core.record_id', {'type': 'v_string', 'size': 4, 'start': 9, 'position': 'last',
                                             'group_by': ['region']}, {'input': PEOPLE})
        self.assertEqual(out['output'].column('RecordID'), ['0009', '0009', '0010', '0009', '0010'])
        r = run_tool_result('core.record_id', {'type': 'int16', 'start': 32767}, {'input': PEOPLE})
        self.assertEqual(r.error['code'], 'data-error')

    def test_sample_modes(self):
        cases = {('first', 2): [1, 2], ('last', 2): [4, 5], ('skip', 3): [4, 5], ('every_nth', 2): [1, 3, 5],
                 ('first_percent', 40): [1, 2], ('first', 0): []}
        for (mode, n), ids in cases.items():
            out, _ = run_tool('core.sample', {'mode': mode, 'n': n}, {'input': PEOPLE})
            self.assertEqual(out['output'].column('id'), ids, mode)
        out, _ = run_tool('core.sample', {'mode': 'first', 'n': 1, 'group_by': ['region']}, {'input': PEOPLE})
        self.assertEqual(out['output'].column('id'), [4, 1, 2])  # groups in key order, null first [ref:sample-first-grouped]

    def test_unique(self):
        data = ([('k', 'v_string'), ('i', 'int32')], [['a', 1], ['A', 2], ['a', 3], [None, 4], [None, 5]])
        out, _ = run_tool('core.unique', {'fields': ['k']}, {'input': data})
        self.assertEqual(out['unique'].column('i'), [4, 2, 1])  # key order, nulls first [ref:probe-unique-order]
        self.assertEqual(out['duplicates'].column('i'), [5, 3])
        out, _ = run_tool('core.unique', {'fields': ['k'], 'case_insensitive': True}, {'input': data})
        self.assertEqual(out['unique'].column('i'), [4, 1])

    def test_generate_rows(self):
        r = run_workflow(workflow([{'id': 'g', 'tool': 'core.generate_rows', 'config': {
            'field': 'n', 'type': 'int32', 'init': '1', 'condition': '[n] <= 4', 'loop': '[n] + 1'}}], []),
            base_dir=ROOT)
        self.assertEqual(r.output('g').column('n'), [1, 2, 3, 4])
        out, _ = run_tool('core.generate_rows', {'field': 'd', 'type': 'date', 'init': '[start]',
                                                 'condition': '[d] <= [end]',
                                                 'loop': 'DateTimeAdd([d], 1, "days")'},
                          {'input': ([('start', 'date'), ('end', 'date')], [['2024-02-28', '2024-03-01']])})
        self.assertEqual(out['output'].column('d'), [dt.date(2024, 2, 28), dt.date(2024, 2, 29), dt.date(2024, 3, 1)])
        r = run_workflow(workflow([{'id': 'g', 'tool': 'core.generate_rows', 'config': {
            'field': 'n', 'type': 'int32', 'init': '1', 'condition': 'TRUE', 'loop': '[n]',
            'max_rows_per_record': 50}}], []), base_dir=ROOT)
        self.assertEqual(r.error['code'], 'data-error')

    def test_multi_row_formula(self):
        data = ([('g', 'v_string'), ('v', 'int32')], [['a', 1], ['a', 2], ['b', 10], ['a', 3], ['b', 20]])
        out, _ = run_tool('core.multi_row_formula', {'field': 'cum', 'type': 'int64',
                                                     'expression': '[Row-1:cum] + [v]',
                                                     'nonexistent_rows': 'zero_or_empty', 'group_by': ['g']},
                          {'input': data})
        self.assertEqual(out['output'].column('cum'), [1, 3, 10, 6, 30])
        out, _ = run_tool('core.multi_row_formula', {'field': 'next_v', 'type': 'int32',
                                                     'expression': '[Row+1:v]'}, {'input': data})
        self.assertEqual(out['output'].column('next_v'), [2, 10, 3, 20, None])

    def test_data_cleansing(self):
        data = ([('s', 'v_wstring'), ('n', 'int32')], [['  a\tb  c ', None], [None, 2], [None, None]])
        out, _ = run_tool('core.data_cleansing', {'trim_whitespace': True, 'remove_tabs_linebreaks': True,
                                                  'collapse_whitespace': True, 'replace_null_strings': True,
                                                  'replace_null_numbers': True, 'case': 'upper'}, {'input': data})
        self.assertEqual(rows(out['output']), [['AB C', 0], ['', 2], ['', 0]])
        out, _ = run_tool('core.data_cleansing', {'remove_null_rows': True}, {'input': data})
        self.assertEqual(len(out['output']), 2)

    def test_data_cleanse_pro_unicode_characters_nulls_and_selected_fields(self):
        data = ([('text', 'v_wstring'), ('amount', 'fixed_decimal'), ('untouched', 'v_string')], [
            [' <h1>Zoë</h1>\u200b A\t  B! ', None, 'keep'],
            ['A1-B!', '0.00', 'same'],
            [None, '2.50', 'null text'],
        ])
        cfg = {'fields': ['text', 'amount'], 'remove_html_tags': True, 'remove_invisible_characters': True,
               'remove_tabs_line_breaks_and_duplicates': True, 'remove_leading_and_trailing_whitespace': True,
               'modify_case': True, 'case': 'upper', 'replace_string_columns': True, 'string_nulls': 'blank',
               'replace_numeric_columns': True, 'numeric_nulls': 'zero'}
        out, _ = run_tool('core.data_cleanse_pro', cfg, {'input': data})
        self.assertEqual(rows(out['output']), [['ZOË A B!', Decimal('0'), 'keep'], ['A1-B!', Decimal('0'), 'same'],
                                               ['', Decimal('2.50'), 'null text']])
        self.assertEqual(out['output'].schema.field('amount').type, T.FIXED_DECIMAL)

    def test_data_cleanse_pro_unicode_categories_exceptions_and_null_filtering(self):
        data = ([('s', 'v_wstring'), ('n', 'int32'), ('empty', 'v_string')], [
            ['A٢½–💷!', 0, ''], [None, None, ''], [None, None, None],
        ])
        cfg = {'remove_letters': True, 'letters_except': 'A', 'remove_numbers': True, 'numbers_except': '٢',
               'remove_punctuation': True, 'punctuation_except': '!', 'replace_numeric_columns': True,
               'numeric_nulls': 'nullify_zero', 'remove_null_rows': True, 'fields': ['s', 'n']}
        out, _ = run_tool('core.data_cleanse_pro', cfg, {'input': data})
        self.assertEqual(rows(out['output']), [['A٢!', None, ''], [None, None, '']])
        self.assertEqual(out['output'].schema.names, ['s', 'n', 'empty'])
        cfg['remove_null_columns'] = True
        out, _ = run_tool('core.data_cleanse_pro', cfg, {'input': data})
        self.assertEqual(out['output'].schema.names, ['s', 'empty'])

    def test_data_cleanse_pro_rejects_missing_selected_field(self):
        from flowengine.errors import WorkflowValidationError
        with self.assertRaises(WorkflowValidationError):
            run_tool_result('core.data_cleanse_pro', {'fields': ['missing']}, {'input': PEOPLE})


class JoinTests(unittest.TestCase):
    L = ([('id', 'int32'), ('v', 'v_string')], [[1, 'a'], [2, 'b'], [2, 'b2'], [3, 'c'], [None, 'n']])
    R = ([('id', 'int32'), ('v', 'v_string'), ('w', 'int32')], [[2, 'x', 20], [2, 'y', 21], [4, 'z', 40], [None, 'q', 0]])

    def test_join_three_outputs_many_to_many_nulls(self):
        out, _ = run_tool('core.join', {'by': 'fields', 'keys': [{'left': 'id', 'right': 'id'}]},
                          {'left': self.L, 'right': self.R})
        j = out['joined']
        self.assertEqual(names(j), ['id', 'v', 'Right_id', 'Right_v', 'w'])
        # null keys match; key order; right-major within a key [ref:join-many-to-many-nulls]
        self.assertEqual(rows(j), [[None, 'n', None, 'q', 0], [2, 'b', 2, 'x', 20], [2, 'b2', 2, 'x', 20],
                                   [2, 'b', 2, 'y', 21], [2, 'b2', 2, 'y', 21]])
        self.assertEqual(out['left_unjoined'].column('v'), ['a', 'c'])
        self.assertEqual(out['right_unjoined'].column('v'), ['z'])

    def test_join_options(self):
        out, _ = run_tool('core.join', {'by': 'fields', 'keys': [{'left': 'id', 'right': 'id'}],
                                        'drop_right_keys': True, 'right_fields': ['id', 'w']},
                          {'left': self.L, 'right': self.R})
        self.assertEqual(names(out['joined']), ['id', 'v', 'w'])
        out, _ = run_tool('core.join', {'by': 'position'}, {'left': self.L, 'right': self.R})
        self.assertEqual(len(out['joined']), 4)
        self.assertEqual(out['left_unjoined'].column('v'), ['n'])
        self.assertEqual(len(out['right_unjoined']), 0)

    def test_join_case_insensitive_and_type_check(self):
        a = ([('k', 'v_string')], [['ABC'], ['x']])
        b = ([('k', 'v_string'), ('z', 'int32')], [['abc', 1]])
        out, _ = run_tool('core.join', {'by': 'fields', 'keys': [{'left': 'k', 'right': 'k'}], 'case_insensitive': True},
                          {'left': a, 'right': b})
        self.assertEqual(len(out['joined']), 1)
        from flowengine.errors import WorkflowValidationError
        with self.assertRaisesRegex(WorkflowValidationError, 'incompatible'):
            run_tool('core.join', {'by': 'fields', 'keys': [{'left': 'v', 'right': 'w'}]},
                     {'left': self.L, 'right': self.R})

    def test_union_by_name_and_position(self):
        a = ([('x', 'int16'), ('y', 'v_string')], [[1, 'a']])
        b = ([('y', 'v_wstring'), ('x', 'int64'), ('z', 'bool')], [['b', 2, True]])
        out, _ = run_tool('core.union', {}, {'inputs': [a, b]})
        t = out['output']
        self.assertEqual(names(t), ['x', 'y', 'z'])
        self.assertEqual(t.schema.field('x').type, T.INT64)
        self.assertEqual(rows(t), [[1, 'a', None], [2, 'b', True]])
        out, _ = run_tool('core.union', {'missing_fields': 'intersection'}, {'inputs': [a, b]})
        self.assertEqual(names(out['output']), ['x', 'y'])
        from flowengine.errors import WorkflowValidationError
        with self.assertRaisesRegex(WorkflowValidationError, 'missing from some inputs'):
            run_tool_result('core.union', {'missing_fields': 'error'}, {'inputs': [a, b]})
        out, _ = run_tool('core.union', {'mode': 'position'}, {'inputs': [a, ([('p', 'int32'), ('q', 'v_string')], [[9, 'z']])]})
        self.assertEqual(rows(out['output']), [[1, 'a'], [9, 'z']])

    def test_append_fields(self):
        t = ([('a', 'int32')], [[1], [2]])
        s = ([('a', 'int32'), ('b', 'v_string')], [[10, 'x'], [20, 'y']])
        out, _ = run_tool('core.append_fields', {}, {'target': t, 'source': s})
        self.assertEqual(names(out['output']), ['a', 'Source_a', 'b'])
        self.assertEqual(rows(out['output']), [[1, 10, 'x'], [1, 20, 'y'], [2, 10, 'x'], [2, 20, 'y']])
        r = run_tool_result('core.append_fields', {'max_source_records': 1}, {'target': t, 'source': s})
        self.assertEqual(r.error['code'], 'data-error')


class TransformTests(unittest.TestCase):
    SALES = ([('r', 'v_string'), ('p', 'v_string'), ('amt', 'fixed_decimal'), ('q', 'int32')],
             [['N', 'x', '1.10', 1], ['S', 'y', '2.00', None], ['N', 'y', '3.30', 3], ['N', 'x', None, 4]])

    def test_arrange_expands_records_with_keys_literals_names_descriptions_and_typed_values(self):
        data = ([{'name': 'store', 'type': 'int32'},
                 {'name': 'Jan', 'type': 'fixed_decimal', 'size': 8, 'scale': 2},
                 {'name': 'Feb', 'type': 'fixed_decimal', 'size': 8, 'scale': 2}],
                [[1, '1.25', '2.50'], [2, '3.00', None]])
        cfg = {'key_fields': ['store'], 'columns': [
            {'name': 'Period', 'mode': 'literal'}, {'name': 'Source', 'mode': 'field_name'},
            {'name': 'Description', 'mode': 'field_description'}, {'name': 'Amount', 'mode': 'field'}],
            'rows': [['January', 'Jan', 'Jan', 'Jan'], ['February', 'Feb', 'Feb', 'Feb']]}
        out, _ = run_tool('core.arrange', cfg, {'input': data})
        table = out['output']
        self.assertEqual(table.schema.names, ['store', 'Period', 'Source', 'Description', 'Amount'])
        self.assertEqual(table.schema.field('Amount').type, T.FIXED_DECIMAL)
        self.assertEqual(rows(table), [[1, 'January', 'Jan', None, Decimal('1.25')],
                                       [1, 'February', 'Feb', None, Decimal('2.50')],
                                       [2, 'January', 'Jan', None, Decimal('3.00')],
                                       [2, 'February', 'Feb', None, None]])

    def test_arrange_empty_input_preserves_configured_schema_and_bad_config_fails(self):
        data = ([('id', 'int32'), ('amount', 'double')], [])
        cfg = {'key_fields': ['id'], 'columns': [{'name': 'Amount', 'mode': 'field'}], 'rows': [['amount']]}
        out, _ = run_tool('core.arrange', cfg, {'input': data})
        self.assertEqual(out['output'].schema.names, ['id', 'Amount'])
        self.assertEqual(len(out['output']), 0)
        from flowengine.errors import WorkflowValidationError
        with self.assertRaises(WorkflowValidationError):
            run_tool_result('core.arrange', {**cfg, 'rows': [['missing']]}, {'input': data})
        with self.assertRaises(WorkflowValidationError):
            run_tool_result('core.arrange', {**cfg, 'rows': [['amount', 'extra']]}, {'input': data})

    def test_summarize(self):
        out, _ = run_tool('core.summarize', {'actions': [
            {'field': 'r', 'op': 'group_by'}, {'field': 'amt', 'op': 'sum'}, {'field': 'q', 'op': 'avg'},
            {'field': 'q', 'op': 'count'}, {'field': 'q', 'op': 'count_non_null'},
            {'field': 'p', 'op': 'concat', 'separator': '|'}, {'field': 'p', 'op': 'count_distinct'},
            {'field': 'q', 'op': 'max'}, {'field': 'amt', 'op': 'first'}, {'field': 'q', 'op': 'std_dev'}]},
            {'input': self.SALES})
        t = out['output']
        self.assertEqual(names(t), ['r', 'Sum_amt', 'Avg_q', 'Count_q', 'CountNonNull_q', 'Concat_p',
                                    'CountDistinct_p', 'Max_q', 'First_amt', 'StdDev_q'])
        n, s = rows(t)
        self.assertEqual(n[:8], ['N', Decimal('4.400000'), 8 / 3, 3, 3, 'x|y|x', 2, 4])
        self.assertAlmostEqual(n[9], 1.5275252316519468, places=12)
        self.assertEqual(s[:5], ['S', Decimal('2.000000'), None, 1, 0])
        self.assertIsNone(s[9])

    def test_summarize_empty_and_errors(self):
        out, _ = run_tool('core.summarize', {'actions': [{'field': 'q', 'op': 'sum'}, {'field': 'q', 'op': 'count'}]},
                          {'input': (self.SALES[0], [])})
        self.assertEqual(rows(out['output']), [[None, 0]])
        from flowengine.errors import WorkflowValidationError
        with self.assertRaisesRegex(WorkflowValidationError, 'numeric'):
            run_tool('core.summarize', {'actions': [{'field': 'p', 'op': 'sum'}]}, {'input': self.SALES})
        with self.assertRaisesRegex(WorkflowValidationError, 'Duplicate output'):
            run_tool('core.summarize', {'actions': [{'field': 'q', 'op': 'sum'}, {'field': 'q', 'op': 'sum'}]},
                     {'input': self.SALES})

    def test_count_records(self):
        out, _ = run_tool('core.count_records', {}, {'input': self.SALES})
        self.assertEqual(rows(out['output']), [[4]])
        out, _ = run_tool('core.count_records', {}, {'input': (self.SALES[0], [])})
        self.assertEqual(rows(out['output']), [[0]])

    def test_running_total(self):
        out, _ = run_tool('core.running_total', {'fields': ['q'], 'group_by': ['r']}, {'input': self.SALES})
        self.assertEqual(out['output'].column('r'), ['N', 'N', 'N', 'S'])  # grouped -> key order
        self.assertEqual(out['output'].column('RunTot_q'), [1, 4, 8, 0])

    def test_transpose_and_crosstab_roundtrip(self):
        out, _ = run_tool('core.transpose', {'key_fields': ['r'], 'data_fields': ['p', 'q']}, {'input': self.SALES})
        self.assertEqual(rows(out['output'])[:2], [['N', 'p', 'x'], ['N', 'q', '1']])
        self.assertEqual(rows(out['output'])[3], ['S', 'q', None])
        out, _ = run_tool('core.cross_tab', {'group_by': ['r'], 'header_field': 'p', 'value_field': 'q'},
                          {'input': self.SALES})
        t = out['output']
        self.assertEqual(names(t), ['r', 'x', 'y'])
        self.assertEqual(rows(t), [['N', 5, 3], ['S', None, None]])


class ParseTests(unittest.TestCase):
    DATA = ([('s', 'v_string')], [['a,b;c'], ['d'], [None], ['"e,f",g']])

    def test_text_to_columns(self):
        out, _ = run_tool('core.text_to_columns', {'field': 's', 'delimiters': ',;', 'mode': 'columns', 'columns': 2},
                          {'input': self.DATA})
        self.assertEqual([r[1:] for r in rows(out['output'])],
                         [['a', 'b;c'], ['d', None], [None, None], ['"e', 'f",g']])
        out, _ = run_tool('core.text_to_columns', {'field': 's', 'delimiters': ',', 'mode': 'columns', 'columns': 2,
                                                   'quote_aware': True, 'extra': 'drop'}, {'input': self.DATA})
        self.assertEqual(rows(out['output'])[3][1:], ['"e,f"', 'g'])
        out, _ = run_tool('core.text_to_columns', {'field': 's', 'delimiters': ',;', 'mode': 'rows'}, {'input': self.DATA})
        self.assertEqual(out['output'].column('s'), ['a', 'b', 'c', 'd', None, '"e', 'f"', 'g'])

    def test_regex_modes(self):
        data = ([('s', 'v_string')], [['john smith 42'], ['no digits'], [None]])
        out, _ = run_tool('core.regex', {'field': 's', 'pattern': r'(\w+) (\w+) (\d+)', 'mode': 'parse',
                                         'group_fields': ['first', 'last', 'age']}, {'input': data})
        self.assertEqual(rows(out['output'])[0], ['john smith 42', 'john', 'smith', '42'])
        self.assertEqual(rows(out['output'])[1][1:], [None, None, None])
        out, _ = run_tool('core.regex', {'field': 's', 'pattern': r'\d+', 'mode': 'replace', 'replacement': '#'},
                          {'input': data})
        self.assertEqual(out['output'].column('s'), ['john smith #', 'no digits', None])
        out, _ = run_tool('core.regex', {'field': 's', 'pattern': r'.*\d.*', 'mode': 'match', 'keep_unmatched': False},
                          {'input': data})
        self.assertEqual(len(out['output']), 1)
        out, _ = run_tool('core.regex', {'field': 's', 'pattern': r'\w+', 'mode': 'tokenize_rows'}, {'input': data})
        self.assertEqual(out['output'].column('s'), ['john', 'smith', '42', 'no', 'digits'])
        from flowengine.errors import WorkflowValidationError
        with self.assertRaisesRegex(WorkflowValidationError, 'capture group'):
            run_tool('core.regex', {'field': 's', 'pattern': 'x', 'mode': 'parse'}, {'input': data})
        with self.assertRaisesRegex(WorkflowValidationError, 'Invalid regular'):
            run_tool('core.regex', {'field': 's', 'pattern': '(', 'mode': 'match'}, {'input': data})


if __name__ == '__main__':
    unittest.main()
