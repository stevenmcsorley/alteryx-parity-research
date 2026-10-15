import unittest
from decimal import Decimal

from flowengine.types import FieldType

from helpers import names, rows, run_tool, run_tool_result


class DynamicReplaceTests(unittest.TestCase):
    def test_dynamic_replace_order_formula_and_summary(self):
        data = ([('Sales', 'double'), ('Region', 'v_wstring')],
                [[12.5, 'North'], [3.0, 'South'], [20.0, 'North']])
        rules = ([('Field', 'v_wstring'), ('Condition', 'v_wstring'), ('Value', 'v_wstring')],
                 [['Sales', '[Sales] > 10', '[Sales] * 0.9'], ['Sales', 'True', '0']])
        out, _ = run_tool('core.dynamic_replace', {'value_is_expression': True}, {'data': data, 'replacements': rules})
        self.assertEqual(rows(out['output']), [[11.25, 'North'], [0.0, 'South'], [18.0, 'North']])
        self.assertIs(out['output'].schema.field('Sales').type, FieldType.DOUBLE)
        self.assertEqual(rows(out['summary']), [['Sales', '0', 1], ['Sales', '11.25', 1], ['Sales', '18.0', 1]])


    def test_dynamic_replace_first_rule_wins_nulls_and_invalid_field(self):
        data = ([('Code', 'v_wstring')], [['A'], ['B'], [None]])
        rules = ([('Field', 'v_wstring'), ('Condition', 'v_wstring'), ('Value', 'v_wstring')],
                 [['Code', '[Code] = "A"', 'first'], ['Code', 'True', 'later']])
        out, _ = run_tool('core.dynamic_replace', {}, {'data': data, 'replacements': rules})
        self.assertEqual(out['output'].column('Code'), ['first', 'later', 'later'])
        broken = ([('Field', 'v_wstring'), ('Condition', 'v_wstring'), ('Value', 'v_wstring')],
                  [['Missing', 'True', 'x']])
        result = run_tool_result('core.dynamic_replace', {}, {'data': data, 'replacements': broken})
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'], 'config-invalid')

    def test_dynamic_replace_explicit_decimal_schema(self):
        data = ([('Amount', 'v_wstring')], [['old'], [None]])
        rules = ([('Field', 'v_wstring'), ('Condition', 'v_wstring'), ('Value', 'v_wstring')],
                 [['Amount', 'True', '1.25']])
        out, _ = run_tool('core.dynamic_replace', {'type_mode': 'explicit', 'output_type': 'fixed_decimal',
                                                    'output_size': 8, 'output_scale': 2},
                          {'data': data, 'replacements': rules})
        self.assertIs(out['output'].schema.field('Amount').type, FieldType.FIXED_DECIMAL)
        self.assertEqual(out['output'].schema.field('Amount').scale, 2)
        self.assertEqual(out['output'].column('Amount'), [Decimal('1.25'), Decimal('1.25')])
