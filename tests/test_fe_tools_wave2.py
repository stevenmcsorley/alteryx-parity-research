"""Wave-2 kernels: Preparation, Join, Parse, Transform and Developer additions."""
import base64
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_workflow
from flowengine.runtimes import find_rscript
from flowengine.types import FieldType as T

from helpers import ROOT, edge, names, rows, run_tool, run_tool_result, text_input, workflow

NUMS = ([('g', 'v_string'), ('x', 'double'), ('w', 'double')],
        [['a', 1.0, 1.0], ['a', 3.0, 3.0], ['b', None, 2.0], ['b', 10.0, 0.0], ['a', 3.0, 1.0], ['c', 7.0, 2.0]])
TXT = ([('id', 'int32'), ('s', 'v_wstring')], [[1, '42'], [2, '-7'], [3, None], [4, '']])


class PreparationWave2(unittest.TestCase):
    def test_auto_field_picks_smallest_types(self):
        data = ([('i', 'v_string'), ('big', 'v_string'), ('d', 'v_string'), ('dt', 'v_string'), ('s', 'v_wstring')],
                [['1', '70000', '1.5', '2024-01-02', 'ab'], ['255', '-3', '2', '2024-02-29', 'zoë'], [None, '', '', None, None]])
        out, _ = run_tool('core.auto_field', {}, {'input': data})
        t = out['output']
        self.assertEqual([f.type for f in t.schema], [T.BYTE, T.INT32, T.DOUBLE, T.DATE, T.V_WSTRING])
        self.assertEqual(t.schema.field('s').size, 3)
        self.assertEqual(rows(t)[2], [None, None, None, None, None])
        self.assertEqual(t.column('dt')[1], dt.date(2024, 2, 29))

    def test_multi_field_formula(self):
        data = ([('a', 'v_string'), ('b', 'v_string'), ('n', 'int32')], [['x', 'Yy', 1], [None, 'z', 2]])
        out, _ = run_tool('core.multi_field_formula', {'fields': ['a', 'b'], 'expression': 'Uppercase([_CurrentField_])'},
                          {'input': data})
        self.assertEqual(rows(out['output']), [['X', 'YY', 1], [None, 'Z', 2]])
        out, _ = run_tool('core.multi_field_formula', {'fields': ['a', 'b'], 'copy_output': True, 'suffix': '_name',
                                                       'expression': '[_CurrentFieldName_] + ":" + [_CurrentField_]'},
                          {'input': data})
        self.assertEqual(names(out['output']), ['a', 'b', 'n', 'a_name', 'b_name'])
        self.assertEqual(out['output'].column('a_name'), ['a:x', 'a:'])

    def test_tile_methods(self):
        out, _ = run_tool('core.tile', {'method': 'equal_records', 'tiles': 3}, {'input': NUMS})
        self.assertEqual(out['output'].column('Tile_Num'), [1, 1, 2, 2, 3, 3])
        self.assertEqual(out['output'].column('Tile_SequenceNum'), [1, 2, 1, 2, 1, 2])
        out, _ = run_tool('core.tile', {'method': 'manual', 'field': 'x', 'cutoffs': [2, 5]}, {'input': NUMS})
        self.assertEqual(out['output'].column('Tile_Num'), [1, 2, None, 3, 2, 3])
        out, _ = run_tool('core.tile', {'method': 'unique_value', 'field': 'g'}, {'input': NUMS})
        self.assertEqual(out['output'].column('Tile_Num'), [1, 1, 2, 2, 1, 3])
        out, _ = run_tool('core.tile', {'method': 'equal_records', 'tiles': 2, 'group_by': ['g']}, {'input': NUMS})
        self.assertEqual(out['output'].column('Tile_Num'), [1, 1, 1, 2, 2, 1])

    def test_rank_ties(self):
        # x = [1, 3, null, 10, 3, 7], descending by default; nulls are not ranked
        expect = {'min': [5, 3, None, 1, 3, 2], 'dense': [4, 3, None, 1, 3, 2], 'ordinal': [5, 3, None, 1, 4, 2],
                  'average': [5, 3.5, None, 1, 3.5, 2], 'max': [5, 4, None, 1, 4, 2]}
        for ties, want in expect.items():
            out, _ = run_tool('core.rank', {'field': 'x', 'ties': ties}, {'input': NUMS})
            self.assertEqual(out['output'].column('Rank'), want, ties)

    def test_random_samples_are_seeded_and_reproducible(self):
        data = ([('i', 'int32')], [[i] for i in range(100)])
        a, _ = run_tool('core.random_sample', {'mode': 'percent', 'n': 10, 'seed': 7}, {'input': data})
        b, _ = run_tool('core.random_sample', {'mode': 'percent', 'n': 10, 'seed': 7}, {'input': data})
        self.assertEqual(rows(a['output']), rows(b['output']))
        self.assertEqual(len(a['output']), 10)
        self.assertEqual(a['output'].column('i'), sorted(a['output'].column('i')))
        out, _ = run_tool('core.create_samples', {'estimation_pct': 60, 'validation_pct': 30, 'seed': 3}, {'input': data})
        parts = [out['estimation'].column('i'), out['validation'].column('i'), out['holdout'].column('i')]
        self.assertEqual(sorted(sum(parts, [])), list(range(100)))
        self.assertTrue(40 < len(parts[0]) < 80)
        flags = ([('y', 'v_string')], [['Yes']] * 10 + [['No']] * 90)
        out, _ = run_tool('core.oversample_field', {'field': 'y', 'value': 'Yes', 'desired_pct': 50}, {'input': flags})
        self.assertEqual(out['output'].column('y').count('Yes'), 10)
        self.assertEqual(out['output'].column('y').count('No'), 10)

    def test_select_records_ranges(self):
        data = ([('i', 'int32')], [[i] for i in range(1, 31)])
        out, _ = run_tool('core.select_records', {'ranges': '-3\n10-12\n20\n28+'}, {'input': data})
        self.assertEqual(out['output'].column('i'), [1, 2, 3, 10, 11, 12, 20, 28, 29, 30])
        with self.assertRaises(WorkflowValidationError):
            run_tool('core.select_records', {'ranges': 'abc'}, {'input': data})

    def test_imputation_and_binning(self):
        out, _ = run_tool('core.imputation', {'fields': ['x'], 'with': 'mean', 'indicator': True}, {'input': NUMS})
        self.assertEqual(out['output'].column('x')[2], 4.8)
        self.assertEqual(out['output'].column('x_Indicator'), [0, 0, 1, 0, 0, 0])
        out, _ = run_tool('core.imputation', {'fields': ['x'], 'with': 'median'}, {'input': NUMS})
        self.assertEqual(out['output'].column('x')[2], 3.0)
        out, _ = run_tool('core.multi_field_binning', {'fields': ['x'], 'bins': 2, 'method': 'equal_interval'}, {'input': NUMS})
        self.assertEqual(out['output'].column('x_Tile_Num'), [1, 1, None, 2, 1, 2])


class JoinParseTransformWave2(unittest.TestCase):
    def test_find_replace(self):
        f = ([('txt', 'v_wstring')], [['the cat sat'], ['Dog'], [None]])
        r = ([('find', 'v_string'), ('rep', 'v_string'), ('code', 'int32')], [['cat', 'lion', 1], ['dog', 'wolf', 2]])
        out, _ = run_tool('core.find_replace', {'find_field': 'txt', 'search_field': 'find', 'mode': 'replace',
                                                'replace_field': 'rep', 'case_insensitive': True},
                          {'find': f, 'replace': r})
        self.assertEqual(out['output'].column('txt'), ['the lion sat', 'wolf', None])
        out, _ = run_tool('core.find_replace', {'find_field': 'txt', 'search_field': 'find', 'mode': 'append',
                                                'append_fields': ['code'], 'match': 'entire', 'case_insensitive': True},
                          {'find': f, 'replace': r})
        self.assertEqual(out['output'].column('code'), [None, 2, None])

    def test_join_multiple(self):
        a = ([('k', 'int32'), ('a', 'v_string')], [[1, 'x'], [2, 'y']])
        b = ([('k', 'int32'), ('b', 'v_string')], [[2, 'p'], [3, 'q']])
        out, _ = run_tool('core.join_multiple', {'by': 'fields', 'keys': ['k', 'k']}, {'inputs': [a, b]})
        self.assertEqual(names(out['output']), ['Input_#1_k', 'a', 'Input_#2_k', 'b'])
        self.assertEqual(rows(out['output']), [[1, 'x', None, None], [2, 'y', 2, 'p'], [None, None, 3, 'q']])
        out, _ = run_tool('core.join_multiple', {'by': 'fields', 'keys': ['k', 'k'], 'join_only': True}, {'inputs': [a, b]})
        self.assertEqual(len(out['output']), 1)
        out, _ = run_tool('core.join_multiple', {'by': 'position'}, {'inputs': [a, b]})
        self.assertEqual(rows(out['output'])[0], [1, 'x', 2, 'p'])

    def test_make_group(self):
        pairs = ([('a', 'v_string'), ('b', 'v_string')], [['k3', 'k1'], ['k2', 'k5'], ['k1', 'k4'], ['k9', 'k9']])
        out, _ = run_tool('core.make_group', {'key1': 'a', 'key2': 'b'}, {'input': pairs})
        self.assertEqual(rows(out['output']), [['k1', 'k1'], ['k1', 'k3'], ['k1', 'k4'], ['k2', 'k2'], ['k2', 'k5'],
                                               ['k9', 'k9']])

    def test_datetime_parse_and_format(self):
        data = ([('s', 'v_string')], [['March 05, 2024'], ['nope'], [None]])
        out, r = run_tool('core.datetime', {'direction': 'to_date', 'field': 's', 'format': 'Month dd, yyyy',
                                            'output_field': 'd'}, {'input': data})
        self.assertEqual(out['output'].column('d'), [dt.date(2024, 3, 5), None, None])
        self.assertTrue(r.warnings())
        data = ([('t', 'datetime')], [['2024-03-05 14:07:09']])
        out, _ = run_tool('core.datetime', {'direction': 'to_text', 'field': 't', 'format': 'dd/MM/yyyy HH:mm:ss',
                                            'output_field': 'txt'}, {'input': data})
        self.assertEqual(out['output'].column('txt'), ['05/03/2024 14:07:09'])

    def test_xml_parse(self):
        data = ([('x', 'v_wstring')], [['<orders><order id="1"><sku>A</sku><qty>2</qty></order><order id="2"><sku>B</sku></order></orders>']])
        out, _ = run_tool('core.xml_parse', {'field': 'x', 'element': 'order'}, {'input': data})
        t = out['output']
        self.assertEqual(names(t), ['x', 'id', 'order', 'sku', 'qty'])  # element text field [ref:xml-parse-children]
        self.assertEqual([r[1:] for r in rows(t)], [['1', '', 'A', '2'], ['2', '', 'B', None]])
        bad = ([('x', 'v_wstring')], [['<a>']])
        self.assertEqual(run_tool_result('core.xml_parse', {'field': 'x'}, {'input': bad}).error['code'], 'data-error')

    def test_make_columns_and_weighted_average(self):
        data = ([('v', 'int32')], [[i] for i in range(1, 6)])
        out, _ = run_tool('core.make_columns', {'columns': 2}, {'input': data})
        self.assertEqual(rows(out['output']), [[1, 2], [3, 4], [5, None]])
        self.assertEqual(names(out['output']), ['Column_1_v', 'Column_2_v'])  # [ref:make-columns-horizontal]
        out, _ = run_tool('core.make_columns', {'columns': 2, 'horizontal': False}, {'input': data})
        self.assertEqual(rows(out['output']), [[1, 4], [2, 5], [3, None]])
        out, _ = run_tool('core.weighted_average', {'value': 'x', 'weight': 'w', 'group_by': ['g']}, {'input': NUMS})
        self.assertEqual(rows(out['output']), [['a', 2.6], ['b', 0.0], ['c', 7.0]])  # null value keeps its weight [ref:weighted-average-macro]


class DeveloperWave2(unittest.TestCase):
    def test_field_info_and_dynamic_select(self):
        out, _ = run_tool('core.field_info', {}, {'input': NUMS})
        self.assertEqual(rows(out['output'])[1][:4], ['x', 'Double', 8, None])  # reference vocabulary [ref:field-info-basic]
        out, _ = run_tool('core.dynamic_select', {'mode': 'types', 'types': ['double']}, {'input': NUMS})
        self.assertEqual(names(out['output']), ['x', 'w'])
        out, _ = run_tool('core.dynamic_select', {'mode': 'formula', 'expression': 'StartsWith([Name], "g") OR [Position] = 2'},
                          {'input': NUMS})
        self.assertEqual(names(out['output']), ['g', 'w'])

    def test_json_parse(self):
        data = ([('j', 'v_wstring')], [['{"a": 1, "b": [true, {"c": "x"}], "d": null}']])
        out, _ = run_tool('core.json_parse', {'field': 'j'}, {'input': data})
        self.assertEqual(rows(out['output']), [['a', '1'], ['b.0', '1'], ['b.1.c', 'x'], ['d', None]])  # [ref:json-parse-single]
        out, _ = run_tool('core.json_parse', {'field': 'j', 'single_field': False}, {'input': data})
        self.assertEqual(rows(out['output'])[0], ['a', None, 1, None, None])

    def test_dynamic_rename_modes(self):
        data = ([('Field1', 'v_string'), ('Field2', 'v_string')], [['name', 'city'], ['Ada', 'Leeds']])
        out, _ = run_tool('core.dynamic_rename', {'mode': 'first_row'}, {'input': data})
        self.assertEqual((names(out['output']), rows(out['output'])), (['name', 'city'], [['Ada', 'Leeds']]))
        out, _ = run_tool('core.dynamic_rename', {'mode': 'add_prefix', 'text': 'p_', 'fields': ['Field2']}, {'input': data})
        self.assertEqual(names(out['output']), ['Field1', 'p_Field2'])
        out, _ = run_tool('core.dynamic_rename', {'mode': 'formula', 'expression': 'Lowercase([_CurrentField_])'}, {'input': data})
        self.assertEqual(names(out['output']), ['field1', 'field2'])
        m = ([('Old', 'v_string'), ('New', 'v_string')], [['Field1', 'first']])
        out, _ = run_tool('core.dynamic_rename', {'mode': 'right_input'}, {'input': data, 'names': m})
        self.assertEqual(names(out['output']), ['first', 'Field2'])

    def test_blob_and_base64(self):
        data = ([('s', 'v_wstring')], [['zoë'], [None]])
        out, _ = run_tool('core.base64_encoder', {'field': 's'}, {'input': data})
        self.assertEqual(out['output'].column('s_Base64_Encoded'), [base64.b64encode('zoë'.encode()).decode(), None])
        out, _ = run_tool('core.blob_convert', {'direction': 'to_blob', 'field': 's', 'output_field': 'b'}, {'input': data})
        self.assertEqual(out['output'].column('b'), ['zoë'.encode(), None])

    def test_message_test_and_expect_equals(self):
        out, r = run_tool('core.message', {'message': '"count " + ToString([x])', 'when': 'every', 'level': 'warning',
                                           'limit': 2}, {'input': NUMS})
        self.assertEqual(len(out['output']), 6)
        self.assertEqual([w['message'] for w in r.warnings()], ['count 1', 'count 3'])
        r = run_tool_result('core.message', {'message': '"bad"', 'level': 'error'}, {'input': NUMS})
        self.assertEqual(r.error['code'], 'message-error')
        r = run_tool_result('core.test', {'tests': [{'name': 'six', 'connection': 1, 'type': 'record_count_equals',
                                                     'value': 6}]}, {'inputs': [NUMS]})
        self.assertTrue(r.ok)
        r = run_tool_result('core.test', {'tests': [{'name': 'pos', 'connection': 1, 'type': 'expression_true_all',
                                                     'value': '[w] > 0'}]}, {'inputs': [NUMS]})
        self.assertEqual(r.error['code'], 'test-failed')
        r = run_tool_result('core.expect_equals', {}, {'expected': NUMS, 'actual': NUMS})
        self.assertTrue(r.ok)
        other = (NUMS[0], NUMS[1][:-1] + [['c', 7.5, 2.0]])
        self.assertEqual(run_tool_result('core.expect_equals', {}, {'expected': NUMS, 'actual': other}).error['code'],
                         'test-failed')
        self.assertTrue(run_tool_result('core.expect_equals', {'tolerance': 0.6}, {'expected': NUMS, 'actual': other}).ok)

    def test_detour_and_block_until_done(self):
        doc = workflow([text_input('t', [('a', 'int32')], [[1], [2]]),
                        {'id': 'd', 'tool': 'core.detour', 'config': {'detour_right': True}},
                        {'id': 'f', 'tool': 'core.formula', 'config': {'formulas': [{'field': 'a', 'expression': '[a] * 10'}]}},
                        {'id': 'e', 'tool': 'core.detour_end', 'config': {}},
                        {'id': 'b', 'tool': 'core.block_until_done', 'config': {}}],
                       [edge('t', 'output', 'd', 'input'), edge('d', 'right', 'f', 'input'), edge('f', 'output', 'e', 'right'),
                        edge('d', 'left', 'e', 'left'), edge('e', 'output', 'b', 'input')])
        r = run_workflow(doc, base_dir=ROOT)
        self.assertEqual(rows(r.output('e')), [[10], [20]])
        self.assertEqual(rows(r.output('b', 'output3')), [[10], [20]])

    def test_throttle_passes_records_and_honours_rate(self):
        import time
        data = ([('a', 'int32')], [[1], [2], [3]])
        t0 = time.perf_counter()
        out, _ = run_tool('core.throttle', {'records_per_minute': 600}, {'input': data})
        self.assertGreaterEqual(time.perf_counter() - t0, 0.19)
        self.assertEqual(rows(out['output']), [[1], [2], [3]])


class UserCodeTools(unittest.TestCase):
    CODE = (
        'from flowbench_io import read_input, write_output, input_count\n'
        'rows = read_input(1)\n'
        'write_output(1, [{"x": r["x"], "x2": (r["x"] or 0) * 2, "when": r["d"]} for r in rows],\n'
        '             schema=[{"name": "x", "type": "double"}, {"name": "x2", "type": "double"}, {"name": "when", "type": "date"}])\n'
        'write_output(2, [{"inputs": input_count(), "label": "ok"}])\n')

    def doc(self, code, env='default'):
        return workflow([text_input('t', [('x', 'double'), ('d', 'date')], [[1.5, '2024-02-29'], [None, None]]),
                         {'id': 'py', 'tool': 'core.python', 'config': {'code': code, 'environment': env,
                                                                        'timeout_seconds': 60}}],
                        [edge('t', 'output', 'py', 'inputs')])

    def test_python_tool_runs_isolated_code_with_typed_exchange(self):
        r = run_workflow(self.doc(self.CODE), base_dir=ROOT, limits={'allow_processes': True})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(rows(r.output('py', 'output1')), [[1.5, 3.0, dt.date(2024, 2, 29)], [None, 0.0, None]])
        self.assertEqual(r.output('py', 'output2').schema.field('inputs').type, T.INT64)
        self.assertEqual(len(r.output('py', 'output3')), 0)

    def test_python_tool_requires_permission(self):
        r = run_workflow(self.doc(self.CODE), base_dir=ROOT)
        self.assertEqual(r.error['code'], 'permission-denied')

    def test_python_errors_timeouts_and_environment(self):
        r = run_workflow(self.doc('raise ValueError("boom")'), base_dir=ROOT, limits={'allow_processes': True})
        self.assertEqual(r.error['code'], 'user-code-error')
        self.assertIn('boom', r.error['message'])
        doc = self.doc('import time\ntime.sleep(30)')
        doc['nodes'][1]['config']['timeout_seconds'] = 1
        r = run_workflow(doc, base_dir=ROOT, limits={'allow_processes': True})
        self.assertEqual(r.error['code'], 'timeout')
        r = run_workflow(self.doc('import os\nfrom flowbench_io import write_output\n'
                                  'write_output(1, [{"secret": os.environ.get("GITHUB_TOKEN", "absent")}])'),
                         base_dir=ROOT, limits={'allow_processes': True})
        self.assertEqual(r.output('py', 'output1').column('secret'), ['absent'])
        r = run_workflow(self.doc(self.CODE, env='does-not-exist'), base_dir=ROOT, limits={'allow_processes': True})
        self.assertEqual(r.error['code'], 'config-invalid')

    def test_run_command(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / 'double.py'
            script.write_text('import csv\nrows=list(csv.reader(open("in.csv", encoding="utf-8")))\n'
                              'w=csv.writer(open("out.csv","w",newline="",encoding="utf-8"))\nw.writerow(["v2"])\n'
                              'w.writerows([[int(r[0])*2] for r in rows[1:]])\n', encoding='utf-8')
            doc = workflow([text_input('t', [('v', 'int32')], [[2], [5]]),
                            {'id': 'rc', 'tool': 'core.run_command', 'config': {
                                'program': sys.executable, 'arguments': [str(script)], 'working_dir': d,
                                'input_file': 'in.csv', 'output_file': 'out.csv'}}],
                           [edge('t', 'output', 'rc', 'input')])
            r = run_workflow(doc, base_dir=Path(d), limits={'allow_processes': True})
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.output('rc').column('v2'), ['4', '10'])
            self.assertEqual(run_workflow(doc, base_dir=Path(d)).error['code'], 'permission-denied')

    @unittest.skipUnless(find_rscript(), 'R is not installed on this machine (R tool remains unverified)')
    def test_r_tool(self):
        doc = workflow([text_input('t', [('x', 'int32')], [[1], [2]]),
                        {'id': 'r', 'tool': 'core.r', 'config': {'code': 'd <- read_input(1)\nd$y <- d$x * 2\nwrite_output(1, d)'}}],
                       [edge('t', 'output', 'r', 'inputs')])
        r = run_workflow(doc, base_dir=ROOT, limits={'allow_processes': True})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.output('r', 'output1').column('y'), ['2', '4'])


if __name__ == '__main__':
    unittest.main()


class ManagedEnvironmentTests(unittest.TestCase):
    def test_python_tool_runs_in_a_named_isolated_environment(self):
        import os
        from flowengine import runtimes
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get('FLOWENGINE_HOME')
            os.environ['FLOWENGINE_HOME'] = home
            try:
                manifest = runtimes.create_env('ci-env', [])
                self.assertTrue(manifest['python'].startswith('3.'))
                self.assertIn('ci-env', [e['name'] for e in runtimes.list_envs()])
                code = ('import sys\nfrom flowbench_io import write_output\n'
                        'write_output(1, [{"prefix": sys.prefix}])')
                doc = workflow([{'id': 'py', 'tool': 'core.python', 'config': {'code': code, 'environment': 'ci-env'}}], [])
                r = run_workflow(doc, base_dir=ROOT, limits={'allow_processes': True})
                self.assertTrue(r.ok, r.error)
                self.assertIn('ci-env', r.output('py', 'output1').column('prefix')[0])
            finally:
                if old is None:
                    os.environ.pop('FLOWENGINE_HOME', None)
                else:
                    os.environ['FLOWENGINE_HOME'] = old
