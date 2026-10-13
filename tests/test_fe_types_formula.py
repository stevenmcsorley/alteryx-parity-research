"""Type system and expression language (engine policy tests; not vendor conformance)."""
import datetime as dt
import unittest
from decimal import Decimal

from flowengine.errors import ExpressionError, SchemaError
from flowengine.formula import EvalContext, compile_expression, parse
from flowengine.table import Table
from flowengine.types import CastFailure, Field, FieldType as T, Schema, cast_value, format_value, unify_fields


def ev(expr, fields=(), row=(), now=dt.datetime(2026, 1, 15, 9, 30)):
    schema = Schema([Field(n, t) for n, t in fields])
    c = compile_expression(expr, schema)
    ctx = EvalContext(now=lambda: now)
    return c.evaluate(tuple(row), ctx), ctx


class TypeTests(unittest.TestCase):
    def test_integer_ranges(self):
        self.assertEqual(cast_value('127', Field('x', T.INT16))[0], 127)
        self.assertEqual(cast_value(' -5 ', Field('x', T.INT32))[0], -5)
        with self.assertRaises(CastFailure):
            cast_value('256', Field('x', T.BYTE))
        with self.assertRaises(CastFailure):
            cast_value(2**63, Field('x', T.INT64))
        with self.assertRaises(CastFailure):
            cast_value('1.5', Field('x', T.INT32))
        self.assertEqual(cast_value('2.0', Field('x', T.INT32))[0], 2)

    def test_number_grammar_is_strict(self):
        for bad in ('1,000', '12abc', '', '--1', '0x10'):
            with self.assertRaises(CastFailure, msg=bad):
                cast_value(bad, Field('x', T.DOUBLE))
        self.assertEqual(cast_value('1e3', Field('x', T.DOUBLE))[0], 1000.0)

    def test_fixed_decimal_rounding_and_precision(self):
        f = Field('x', T.FIXED_DECIMAL, 6, 2)
        self.assertEqual(cast_value('1.005', f)[0], Decimal('1.00'))  # banker's rounding
        self.assertEqual(cast_value('1.015', f)[0], Decimal('1.02'))
        with self.assertRaises(CastFailure):
            cast_value('123456.7', f)

    def test_string_truncation_is_warning(self):
        v, w = cast_value('abcdef', Field('s', T.V_STRING, 3))
        self.assertEqual(v, 'abc')
        self.assertIn('truncated', w)

    def test_dates(self):
        self.assertEqual(cast_value('2024-02-29', Field('d', T.DATE))[0], dt.date(2024, 2, 29))
        with self.assertRaises(CastFailure):
            cast_value('2023-02-29', Field('d', T.DATE))
        with self.assertRaises(CastFailure):
            cast_value('29/02/2024', Field('d', T.DATE))
        self.assertEqual(cast_value('2024-01-02T03:04:05.5', Field('d', T.DATETIME))[0],
                         dt.datetime(2024, 1, 2, 3, 4, 5, 500000))
        self.assertEqual(cast_value('2024-01-02', Field('d', T.DATETIME))[0], dt.datetime(2024, 1, 2))

    def test_bool(self):
        self.assertIs(cast_value('Yes', Field('b', T.BOOL))[0], True)
        self.assertIs(cast_value('0', Field('b', T.BOOL))[0], False)
        with self.assertRaises(CastFailure):
            cast_value('maybe', Field('b', T.BOOL))

    def test_null_passes_every_type(self):
        for t in T:
            self.assertEqual(cast_value(None, Field('x', t)), (None, None))

    def test_format_value(self):
        self.assertEqual(format_value(3.0), '3')
        self.assertEqual(format_value(0.1), '0.1')
        self.assertEqual(format_value(Decimal('1.50')), '1.50')
        self.assertEqual(format_value(dt.datetime(2024, 1, 2, 3, 4, 5)), '2024-01-02 03:04:05')
        self.assertEqual(format_value(True), 'True')
        self.assertIsNone(format_value(None))

    def test_schema_rules(self):
        with self.assertRaises(SchemaError):
            Schema([Field('a', T.INT32), Field('a', T.DOUBLE)])
        with self.assertRaises(SchemaError):
            Field('', T.INT32)
        self.assertEqual(Field('s', T.V_STRING).size, 254)
        with self.assertRaises(SchemaError):
            Table([Field('a', T.INT32)], [[1, 2], ])  .with_column(Field('b', T.INT32), [1])

    def test_unify(self):
        self.assertEqual(unify_fields(Field('a', T.INT16), Field('a', T.INT64)).type, T.INT64)
        self.assertEqual(unify_fields(Field('a', T.INT32), Field('a', T.FLOAT)).type, T.DOUBLE)
        self.assertEqual(unify_fields(Field('a', T.V_STRING, 10), Field('a', T.V_WSTRING, 5)).size, 10)
        self.assertEqual(unify_fields(Field('a', T.DATE), Field('a', T.INT32)).type, T.V_WSTRING)

    def test_digest_is_order_sensitive(self):
        s = [Field('a', T.INT32)]
        self.assertNotEqual(Table(s, [[1, 2]]).canonical_digest(), Table(s, [[2, 1]]).canonical_digest())
        self.assertEqual(Table(s, [[1, 2]]).canonical_digest(), Table(s, [[1, 2]]).canonical_digest())


class FormulaTests(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(ev('1 + 2 * 3')[0], 7)
        self.assertEqual(ev('(1 + 2) * 3')[0], 9)
        self.assertEqual(ev('-2 * 3')[0], -6)
        self.assertIs(ev('1 = 1 OR 1 = 2 AND 1 = 2')[0], True)  # AND binds tighter
        self.assertIs(ev('NOT 1 = 2')[0], True)
        self.assertEqual(ev('7 / 2')[0], 3.5)

    def test_conditionals(self):
        e = 'IF [x] > 10 THEN "big" ELSEIF [x] > 5 THEN "mid" ELSE "small" ENDIF'
        f = [('x', T.INT32)]
        self.assertEqual(ev(e, f, (11,))[0], 'big')
        self.assertEqual(ev(e, f, (6,))[0], 'mid')
        self.assertEqual(ev(e, f, (None,))[0], 'small')  # null condition counts as false
        self.assertEqual(ev('IIF([x] > 1, "y", "n")', f, (2,))[0], 'y')
        with self.assertRaisesRegex(ExpressionError, 'ELSE'):
            parse('IF 1 THEN 2 ENDIF')

    def test_iif_is_lazy(self):
        # the unselected branch would divide by zero and warn; laziness means no warning
        v, ctx = ev('IIF(TRUE, 1, 1/0)')
        self.assertEqual((v, ctx.warnings), (1, {}))

    def test_null_semantics(self):
        f = [('x', T.INT32), ('s', T.V_STRING)]
        self.assertIsNone(ev('[x] + 1', f, (None, 'a'))[0])
        self.assertEqual(ev('[s] + "b"', f, (1, None))[0], 'b')  # [ref:formula-null-concat]
        self.assertIsNone(ev('[s] + [s]', f, (1, None))[0])
        self.assertEqual(ev('Length([s])', f, (1, None))[0], 0)
        self.assertIsNone(ev('Uppercase([s])', f, (1, None))[0])
        self.assertIs(ev('[x] = NULL()', f, (None, 'a'))[0], True)
        self.assertIs(ev('[x] != 1', f, (None, 'a'))[0], True)
        self.assertIs(ev('[x] > 1', f, (None, 'a'))[0], False)
        self.assertIs(ev('[x] < 1', f, (None, 'a'))[0], False)
        self.assertIs(ev('IsNull([x])', f, (None, 'a'))[0], True)
        self.assertIs(ev('IsEmpty([s])', f, (1, ''))[0], True)

    def test_division_by_zero_warns_and_nulls(self):
        v, ctx = ev('5 / 0')
        self.assertIsNone(v)
        self.assertEqual(ctx.warnings, {'division by zero produced null': 1})

    def test_strings(self):
        self.assertEqual(ev('"ab" + \'cd\'')[0], 'abcd')
        self.assertEqual(ev('"say ""hi"""')[0], 'say "hi"')
        self.assertEqual(ev('Left("hello", 2) + Right("hello", 3)')[0], 'hello')
        self.assertEqual(ev('Substring("hello", 1, 3)')[0], 'ell')
        self.assertEqual(ev('TitleCase("hello wORLD")')[0], 'Hello World')
        self.assertEqual(ev('PadLeft("7", 3, "0")')[0], '007')
        self.assertEqual(ev('FindString("hello", "l")')[0], 2)
        self.assertEqual(ev('FindString("hello", "z")')[0], -1)
        self.assertIs(ev('Contains("Hello", "hell")')[0], True)  # default case-insensitive [ref:probe-contains-flag-and-codepoints]
        self.assertIs(ev('Contains("Hello", "hell", 0)')[0], False)
        self.assertIs(ev('"abc" = "ABC"')[0], True)  # [ref:filter-string-eq-case]
        self.assertIs(ev('"a" < "B"')[0], True)  # [ref:probe-string-ordering]
        self.assertEqual(ev('REGEX_Replace("a1b22", "\\\\d+", "#")')[0], 'a#b#')
        self.assertEqual(ev('REGEX_Replace("john smith", "(\\\\w+) (\\\\w+)", "$2, $1")')[0], 'smith, john')
        self.assertIs(ev('REGEX_Match("abc", "a.c")')[0], True)
        self.assertEqual(ev('Length("zoë")')[0], 3)

    def test_conversion_and_math(self):
        self.assertEqual(ev('ToNumber("42")')[0], 42)
        self.assertEqual(ev('ToNumber(" 4.5 ")')[0], 4.5)
        v, ctx = ev('ToNumber("x")')
        self.assertEqual(v, 0)  # [ref:formula-tonumber]
        self.assertTrue(ctx.warnings)
        self.assertEqual(ev('ToString(3.14159, 2)')[0], '3.14')
        self.assertEqual(ev('Round(2.5, 1)')[0], 3)
        self.assertEqual(ev('Round(-2.5, 1)')[0], -2.0)  # floor(x + 0.5) [ref:formula-rounding]
        self.assertEqual(ev('Round(1.45, 0.1)')[0], 1.4000000000000001)
        self.assertEqual(ev('Round(1234, 100)')[0], 1200)
        self.assertEqual(ev('Mod(7, 3)')[0], 1)
        self.assertEqual(ev('Mod(-7, 3)')[0], -1)
        self.assertEqual(ev('Mod(2.5, 3)')[0], 2)  # operands truncated [ref:formula-rounding]
        self.assertEqual(ev('Max(1, NULL(), 3)')[0], 3)
        self.assertIsNone(ev('Sqrt(-1)')[0])

    def test_dates(self):
        f = [('d', T.DATE), ('t', T.DATETIME)]
        row = (dt.date(2024, 1, 31), dt.datetime(2024, 3, 1, 12, 0))
        self.assertEqual(ev('DateTimeAdd([d], 1, "month")', f, row)[0], dt.date(2024, 2, 29))
        self.assertEqual(ev('DateTimeDiff([t], [d], "days")', f, row)[0], 30)
        self.assertEqual(ev('DateTimeDiff([d], [t], "months")', f, row)[0], -1)
        self.assertEqual(ev('DateTimeFormat([t], "%Y/%m/%d %H:%M")', f, row)[0], '2024/03/01 12:00')
        self.assertEqual(ev('DateTimeParse("05-02-2024", "%d-%m-%Y")')[0], dt.date(2024, 2, 5))
        self.assertEqual(ev('DateTimeNow()')[0], dt.datetime(2026, 1, 15, 9, 30))
        self.assertIs(ev('[d] < "2024-02-01"', f, row)[0], True)
        self.assertEqual(ev('DateTimeYear([t])', f, row)[0], 2024)
        with self.assertRaisesRegex(ExpressionError, 'specifier'):
            ev('DateTimeFormat([t], "%Q")', f, row)

    def test_in_and_switch(self):
        f = [('r', T.V_STRING)]
        self.assertIs(ev('[r] IN ("N", "S")', f, ('S',))[0], True)
        self.assertIs(ev('[r] NOT IN ("N", "S")', f, ('S',))[0], False)
        self.assertEqual(ev('Switch([r], "?", "N", "North", "S", "South")', f, ('S',))[0], 'South')
        self.assertEqual(ev('Switch([r], "?", "N", "North")', f, ('E',))[0], '?')

    def test_comments_and_whitespace(self):
        self.assertEqual(ev('1 + // one\n 2 /* two */')[0], 3)

    def test_static_errors_carry_positions(self):
        cases = {
            '[missing] + 1': 'Unknown field',
            'Foo(1)': 'Unknown function',
            'Left("a")': 'expects 2',
            '"a" + 1': 'ToString',
            '1 +': 'Unexpected',
            '"a" > 1': 'Cannot compare',
            '': 'Empty',
            '1 $ 2': 'Unexpected character',
            '[Row-1:x]': 'Unknown field',
        }
        for expr, msg in cases.items():
            with self.assertRaisesRegex(ExpressionError, msg, msg=expr):
                compile_expression(expr, Schema([]))
        with self.assertRaisesRegex(ExpressionError, 'only allowed'):
            compile_expression('[Row-1:x]', Schema([Field('x', T.INT32)]))
        try:
            compile_expression('1 + [nope]', Schema([]))
        except ExpressionError as e:
            self.assertEqual(e.position, 4)

    def test_runtime_type_error(self):
        f = [('x', T.V_STRING)]
        with self.assertRaises(ExpressionError):
            ev('IIF([x] = "a", 1, 2) AND "text"', f, ('a',))


if __name__ == '__main__':
    unittest.main()
