import unittest

from helpers import rows, run_tool, run_tool_result


class CorrelationTests(unittest.TestCase):
    def test_positive_negative_and_constant_matrix(self):
        out, _ = run_tool('core.pearson_correlation', {'fields': ['x', 'y', 'c']}, {
            'input': ([('x', 'double'), ('y', 'double'), ('c', 'double')],
                      [[1, 6, 4], [2, 4, 4], [3, 2, 4]])})
        matrix = rows(out['output'])
        self.assertEqual(len(matrix), 9)
        self.assertEqual(matrix[0], ['x', 'x', 1.0])
        self.assertEqual(matrix[1], ['x', 'y', -1.0])
        self.assertIsNone(matrix[2][2])
        self.assertIsNone(matrix[-1][2])

    def test_sample_covariance(self):
        out, _ = run_tool('core.pearson_correlation',
            {'fields': ['x', 'y'], 'calculation': 'covariance'}, {
            'input': ([('x', 'int32'), ('y', 'int32')], [[1, 2], [2, 4], [3, 6]])})
        self.assertEqual(rows(out['output']), [
            ['x', 'x', 1.0], ['x', 'y', 2.0], ['y', 'x', 2.0], ['y', 'y', 4.0]])

    def test_rejects_nulls_nonfinite_and_insufficient_rows(self):
        for data in ([[1, None], [2, 3]], [[1, float('inf')], [2, 3]], [[1, 2]], []):
            with self.subTest(data=data):
                result = run_tool_result('core.pearson_correlation', {'fields': ['x', 'y']}, {
                    'input': ([('x', 'double'), ('y', 'double')], data)})
                self.assertFalse(result.ok)
                self.assertEqual(result.error['code'], 'data-error')

    def test_rejects_text_and_missing_fields(self):
        for fields in (['x', 'text'], ['x', 'missing']):
            result = run_tool_result('core.pearson_correlation', {'fields': fields}, {
                'input': ([('x', 'int32'), ('text', 'v_wstring')], [[1, 'a'], [2, 'b']])})
            self.assertFalse(result.ok)
            self.assertEqual(result.error['code'], 'schema-invalid')
