"""Regression coverage for data types at file, Union and Arrow boundaries."""
from decimal import Decimal
from pathlib import Path

from flowengine.executor import run_workflow
from flowengine.types import FieldType as T


def _edge(src, src_anchor, dst, dst_anchor):
    return {'from': {'node': src, 'anchor': src_anchor}, 'to': {'node': dst, 'anchor': dst_anchor}}


def _workflow(nodes, edges):
    return {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': nodes, 'edges': edges}


def case_csv_string_filter_semantics_are_not_numeric_inference(tmp_path):
    values = ['0', '1', '10', '09', '00123', '-2', '2', '3', '5', '6', '11', '12', '20', '50', '100']
    source = tmp_path / 'quantities.csv'
    source.write_text('Quantity,Sales\n' + ''.join(f'{q},1.25\n' for q in values), encoding='utf-8')
    nodes = [
        {'id': 'input', 'tool': 'core.input_data', 'config': {'path': str(source)}},
        {'id': 'filter', 'tool': 'core.filter', 'config': {'field': 'Quantity', 'operator': '>', 'value': '10'}},
    ]
    result = run_workflow(_workflow(nodes, [_edge('input', 'output', 'filter', 'input')]), base_dir=tmp_path)
    assert result.ok
    string_table = result.outputs['input']['output']
    assert string_table.schema.field('Quantity').type is T.V_WSTRING
    assert string_table.schema.field('Quantity').size == 2**30 - 1
    assert len(result.outputs['filter']['true']) == 9
    assert len(result.outputs['filter']['false']) == 6
    assert {'2', '3', '5', '6'} <= set(result.outputs['filter']['true'].column('Quantity'))

    nodes[0]['config']['fields'] = [
        {'name': 'Quantity', 'type': 'int32'},
        {'name': 'Sales', 'type': 'fixed_decimal', 'size': 8, 'scale': 2},
    ]
    # The desktop's basic Filter value editor sends text. The engine now binds
    # it against the selected field's declared type instead of stringifying it.
    nodes[1]['config']['value'] = '10'
    typed = run_workflow(_workflow(nodes, [_edge('input', 'output', 'filter', 'input')]), base_dir=tmp_path)
    assert typed.ok
    assert typed.outputs['input']['output'].schema.field('Quantity').type is T.INT32
    assert typed.outputs['input']['output'].schema.field('Sales').type is T.FIXED_DECIMAL
    # The explicitly numeric declaration also intentionally converts the text identifier 00123 to 123.
    assert typed.outputs['filter']['true'].column('Quantity') == [123, 11, 12, 20, 50, 100]


def case_filter_numeric_literal_from_text_preserves_int64_precision():
    nodes = [
        {'id': 'input', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'id', 'type': 'int64'}],
            'rows': [[9007199254740992], [9007199254740993], [9007199254740994]]}},
        {'id': 'filter', 'tool': 'core.filter', 'config': {'field': 'id', 'operator': '>',
                                                            'value': '9007199254740992'}},
    ]
    result = run_workflow(_workflow(nodes, [_edge('input', 'output', 'filter', 'input')]), base_dir=Path.cwd())
    assert result.ok
    assert result.outputs['filter']['true'].column('id') == [9007199254740993, 9007199254740994]


def case_basic_filter_parses_date_time_and_datetime_literals_by_field_type():
    for field_type, values, literal, expected in (
        ('date', ['2024-01-01', '2024-01-03'], '2024-01-02', ['2024-01-03']),
        ('time', ['08:00:00', '12:00:00'], '10:00:00', ['12:00:00']),
        ('datetime', ['2024-01-01T08:00:00', '2024-01-01T12:00:00'],
         '2024-01-01T10:00:00', ['2024-01-01T12:00:00']),
    ):
        nodes = [
            {'id': 'input', 'tool': 'core.text_input', 'config': {
                'fields': [{'name': 'value', 'type': field_type}], 'rows': [[v] for v in values]}},
            {'id': 'filter', 'tool': 'core.filter', 'config': {
                'field': 'value', 'operator': '>', 'value': literal}},
        ]
        result = run_workflow(_workflow(nodes, [_edge('input', 'output', 'filter', 'input')]),
                              base_dir=Path.cwd())
        assert result.ok, result.error
        assert [v.isoformat() for v in result.outputs['filter']['true'].column('value')] == expected

    invalid = [
        {'id': 'input', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'value', 'type': 'time'}], 'rows': [['12:00:00']]}},
        {'id': 'filter', 'tool': 'core.filter', 'config': {
            'field': 'value', 'operator': '>', 'value': '25:00:00'}},
    ]
    import pytest
    from flowengine.errors import WorkflowValidationError
    with pytest.raises(WorkflowValidationError, match='valid time'):
        run_workflow(_workflow(invalid, [_edge('input', 'output', 'filter', 'input')]),
                     base_dir=Path.cwd())


def case_union_preserves_large_integer_and_decimal_exactly():
    nodes = [
        {'id': 'decimal', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'amount', 'type': 'fixed_decimal', 'size': 25, 'scale': 2}],
            'rows': [['9007199254740993.01']]}},
        {'id': 'integer', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'amount', 'type': 'int64'}], 'rows': [[9007199254740993]]}},
        {'id': 'union', 'tool': 'core.union', 'config': {}},
    ]
    edges = [_edge('decimal', 'output', 'union', 'inputs'), _edge('integer', 'output', 'union', 'inputs')]
    result = run_workflow(_workflow(nodes, edges), base_dir=Path.cwd())
    assert result.ok
    table = result.outputs['union']['output']
    assert table.schema.field('amount').type is T.FIXED_DECIMAL
    assert table.columns[0] == [Decimal('9007199254740993.01'), Decimal('9007199254740993.00')]


def case_arrow_decimal_precision_above_38_round_trips_exactly():
    from flowengine import arrow
    from flowengine.table import Table
    from flowengine.types import Field

    if arrow.pa is None:
        import pytest
        pytest.skip('pyarrow is unavailable')
    field = Field('amount', T.FIXED_DECIMAL, 40, 2)
    value = Decimal('123456789012345678901234567890123456.78')
    original = Table([field], [[value]])
    physical = arrow.to_arrow(original)
    assert arrow.pa.types.is_decimal256(physical.schema.field('amount').type)
    assert arrow.from_arrow(physical) == original


def case_union_rejects_unproven_lossy_numeric_promotions():
    import pytest
    from flowengine.errors import WorkflowValidationError
    nodes = [
        {'id': 'integer', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'value', 'type': 'int64'}], 'rows': [[9007199254740993]]}},
        {'id': 'decimal', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'value', 'type': 'double'}], 'rows': [[1.5]]}},
        {'id': 'union', 'tool': 'core.union', 'config': {}},
    ]
    edges = [_edge('integer', 'output', 'union', 'inputs'), _edge('decimal', 'output', 'union', 'inputs')]
    with pytest.raises(WorkflowValidationError, match=r'above 2\*\*53'):
        run_workflow(_workflow(nodes, edges), base_dir=Path.cwd())


def case_sort_rejects_nan_and_join_rejects_incompatible_temporal_keys():
    import pytest
    from flowengine.errors import WorkflowValidationError
    nodes = [
        {'id': 'source', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'value', 'type': 'double'}], 'rows': [[1.0], [float('nan')], [2.0]]}},
        {'id': 'sort', 'tool': 'core.sort', 'config': {'keys': [{'field': 'value'}]}},
    ]
    result = run_workflow(_workflow(nodes, [_edge('source', 'output', 'sort', 'input')]), base_dir=Path.cwd())
    assert not result.ok
    assert 'NaN' in result.error['message']

    nodes = [
        {'id': 'dates', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'key', 'type': 'date'}], 'rows': [['2024-01-01']]}},
        {'id': 'times', 'tool': 'core.text_input', 'config': {
            'fields': [{'name': 'key', 'type': 'time'}], 'rows': [['12:00:00']]}},
        {'id': 'join', 'tool': 'core.join', 'config': {
            'by': 'fields', 'keys': [{'left': 'key', 'right': 'key'}]}},
    ]
    edges = [_edge('dates', 'output', 'join', 'left'), _edge('times', 'output', 'join', 'right')]
    with pytest.raises(WorkflowValidationError, match='Key types are incompatible'):
        run_workflow(_workflow(nodes, edges), base_dir=Path.cwd())


def case_csv_select_filter_sort_join_summarize_output_roundtrip_preserves_schema(tmp_path):
    source = tmp_path / 'sales.csv'
    lookup = tmp_path / 'regions.csv'
    output = tmp_path / 'summary.csv'
    source.write_text('id,Quantity,Sales\n001,2,10.10\n002,10,20.20\n003,11,30.30\n'
                      '004,20,40.40\n005,2,5.50\n', encoding='utf-8')
    lookup.write_text('id,Region\n003,North\n004,North\n', encoding='utf-8')
    nodes = [
        {'id': 'sales', 'tool': 'core.input_data', 'config': {'path': str(source)}},
        {'id': 'typed', 'tool': 'core.select', 'config': {'fields': [
            {'field': 'id', 'type': 'v_string', 'size': 8},
            {'field': 'Quantity', 'type': 'int32'},
            {'field': 'Sales', 'type': 'fixed_decimal', 'size': 12, 'scale': 2}]}},
        {'id': 'filter', 'tool': 'core.filter', 'config': {'field': 'Quantity', 'operator': '>', 'value': '10'}},
        {'id': 'sort', 'tool': 'core.sort', 'config': {'keys': [{'field': 'Quantity', 'order': 'desc'}]}},
        {'id': 'regions', 'tool': 'core.input_data', 'config': {'path': str(lookup), 'fields': [
            {'name': 'id', 'type': 'v_string', 'size': 8}, {'name': 'Region', 'type': 'v_wstring', 'size': 16}]}},
        {'id': 'join', 'tool': 'core.join', 'config': {'by': 'fields', 'keys': [{'left': 'id', 'right': 'id'}],
                                                        'right_fields': ['Region']}},
        {'id': 'summary', 'tool': 'core.summarize', 'config': {'actions': [
            {'field': 'Region', 'op': 'group_by'}, {'field': 'Sales', 'op': 'sum'}]}},
        {'id': 'write', 'tool': 'core.output_data', 'config': {'path': str(output), 'format': 'csv'}},
    ]
    edges = [
        _edge('sales', 'output', 'typed', 'input'),
        _edge('typed', 'output', 'filter', 'input'),
        _edge('filter', 'true', 'sort', 'input'),
        _edge('sort', 'output', 'join', 'left'),
        _edge('regions', 'output', 'join', 'right'),
        _edge('join', 'joined', 'summary', 'input'),
        _edge('summary', 'output', 'write', 'input'),
    ]
    result = run_workflow(_workflow(nodes, edges), base_dir=tmp_path)
    assert result.ok
    assert result.outputs['sales']['output'].schema.field('id').type is T.V_WSTRING
    assert result.outputs['typed']['output'].schema.field('Quantity').type is T.INT32
    assert result.outputs['typed']['output'].schema.field('Sales').type is T.FIXED_DECIMAL
    assert result.outputs['filter']['true'].column('Quantity') == [11, 20]
    assert result.outputs['sort']['output'].column('Quantity') == [20, 11]
    assert result.outputs['join']['joined'].column('id') == ['003', '004']
    assert result.outputs['summary']['output'].columns == [['North'], [Decimal('70.70')]]
    assert output.read_text(encoding='utf-8') == 'Region,Sum_Sales\nNorth,70.70\n'

    reimport = run_workflow(_workflow([{'id': 'reload', 'tool': 'core.input_data', 'config': {
        'path': str(output), 'fields': [{'name': 'Region', 'type': 'v_wstring', 'size': 16},
                                        {'name': 'Sum_Sales', 'type': 'fixed_decimal', 'size': 38, 'scale': 2}]}}], []),
                            base_dir=tmp_path)
    assert reimport.ok
    table = reimport.outputs['reload']['output']
    assert table.schema.field('Sum_Sales').type is T.FIXED_DECIMAL
    assert table.column('Sum_Sales') == [Decimal('70.70')]

import tempfile
import unittest


class DataCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flowbench-data-correctness-')
        self.tmp_path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_csv_string_filter_and_declared_numeric_schema(self):
        case_csv_string_filter_semantics_are_not_numeric_inference(self.tmp_path)

    def test_filter_numeric_literal_int64_precision(self):
        case_filter_numeric_literal_from_text_preserves_int64_precision()

    def test_basic_filter_temporal_literals_are_typed(self):
        case_basic_filter_parses_date_time_and_datetime_literals_by_field_type()

    def test_union_decimal_integer_exact(self):
        case_union_preserves_large_integer_and_decimal_exactly()

    def test_arrow_decimal256_roundtrip(self):
        case_arrow_decimal_precision_above_38_round_trips_exactly()

    def test_union_rejects_lossy_numeric_promotions(self):
        case_union_rejects_unproven_lossy_numeric_promotions()

    def test_sort_nan_and_join_key_types(self):
        case_sort_rejects_nan_and_join_rejects_incompatible_temporal_keys()

    def test_full_typed_csv_pipeline_roundtrip(self):
        case_csv_select_filter_sort_join_summarize_output_roundtrip_preserves_schema(self.tmp_path)
