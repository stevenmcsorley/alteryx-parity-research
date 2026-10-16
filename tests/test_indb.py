"""In-DB tools against a recording fake database: SQL composition, parameters, permissions and limits.

Real execution on PostgreSQL and SQL Server is covered by tests/integration/test_indb_integration.py.
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import flowengine.db.dialects as dialects
import flowengine.tools.database as database
from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_workflow
from flowengine.types import Field, FieldType
from flowengine.workflow_effects import workflow_effects

from helpers import ROOT, edge, text_input, workflow

SALES = ([('region', 'v_wstring'), ('amount', 'double'), ('units', 'int32')],
         [['North', 10.0, 1], ['South', 20.0, 2], [None, 5.0, 3]])


class Recorder:
    def __init__(self):
        self.queries, self.writes, self.fetch_rows, self.fetch_fields = [], [], [], None


def fake_dialect(kind, recorder, allow_writes=True):
    base = dialects.PostgreSQL if kind == 'postgresql' else dialects.SQLServer

    class FakeDialect(base):
        def connect(self, read_only=True):
            outer = recorder

            class Cursor:
                def execute(self, sql, params=None):
                    outer.writes.append((sql, tuple(params or ())))

                def executemany(self, sql, rows):
                    outer.writes.append((sql, ('<%d rows>' % len(rows),)))

            class Conn:
                def cursor(self):
                    return Cursor()

                def commit(self):
                    outer.writes.append(('COMMIT', ()))

                def rollback(self):
                    pass

                def close(self):
                    pass
            return Conn()
    cfg = SimpleNamespace(name=f'fake {kind}', allow_writes=allow_writes, dialect=kind)
    return cfg, FakeDialect(cfg, None)


class FakeQuery:
    recorder = None

    def __init__(self, d, sql, params=(), cancel_token=None, read_only=True):
        self.sql, self.params, self.rows_fetched, self.first_row_ms = sql, tuple(params or ()), 0, None
        if read_only:
            dialects.check_read_only_sql(sql)
        FakeQuery.recorder.queries.append((sql, self.params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def describe(self):
        return list(FakeQuery.recorder.fetch_fields or [])

    def batches(self, fields, batch_rows, max_rows=None):
        rows = FakeQuery.recorder.fetch_rows[:max_rows] if max_rows is not None else FakeQuery.recorder.fetch_rows
        self.rows_fetched = len(rows)
        yield rows


class InDBTestCase(unittest.TestCase):
    kind = 'postgresql'

    def setUp(self):
        self.rec = Recorder()
        FakeQuery.recorder = self.rec
        conns = {cid: fake_dialect(kind, self.rec, allow_writes=(cid != 'readonly'))
                 for cid, kind in (('pg', 'postgresql'), ('ms', 'sqlserver'), ('readonly', 'postgresql'), ('pg2', 'postgresql'))}
        self.patches = [mock.patch.object(database, 'open_dialect', lambda cid: conns[cid]),
                        mock.patch.object(dialects, 'Query', FakeQuery)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def run_doc(self, nodes, edges, limits=None):
        result = run_workflow(workflow(nodes, edges), base_dir=ROOT, limits=limits or {})
        return result

    def stream_of(self, result, node, anchor='output'):
        from flowengine.tools.indb import Stream
        s = Stream.read(result.output(node, anchor))
        self.assertEqual(s.sql.count('%s'), len(s.params), s.sql)  # every marker has exactly one parameter
        return s

    def source(self, conn='pg'):
        return [text_input('local', *SALES), {'id': 'in', 'tool': 'core.data_stream_in', 'config': {'connection_id': conn}}]


class CompositionTests(InDBTestCase):
    def test_pipeline_wraps_queries_and_keeps_parameters_in_order(self):
        nodes = self.source() + [
            {'id': 'sel', 'tool': 'core.select_indb', 'config': {'fields': ['region', 'amount'], 'renames': {'amount': 'value'}}},
            {'id': 'flt', 'tool': 'core.filter_indb', 'config': {'expression': '[value] > 6 AND [region] != "south"'}},
            {'id': 'fml', 'tool': 'core.formula_indb', 'config': {'formulas': [
                {'field': 'double_value', 'expression': '[value] * 2'},
                {'field': 'is_north', 'expression': '[region] = "NORTH"'},
                {'field': 'label', 'expression': '"r:"'}]}},
            {'id': 'sum', 'tool': 'core.summarize_indb', 'config': {'actions': [
                {'field': 'region', 'op': 'group_by'}, {'field': 'double_value', 'op': 'sum', 'output': 'total'},
                {'field': 'region', 'op': 'count', 'output': 'n'}]}},
            {'id': 'out', 'tool': 'core.dynamic_output_indb', 'config': {}}]
        edges = [edge('local', 'output', 'in', 'input'), edge('in', 'output', 'sel', 'input'), edge('sel', 'output', 'flt', 'input'),
                 edge('flt', 'true', 'fml', 'input'), edge('fml', 'output', 'sum', 'input'), edge('sum', 'output', 'out', 'input')]
        result = self.run_doc(nodes, edges)
        self.assertTrue(result.ok, result.error)
        values = self.stream_of(result, 'in')
        self.assertEqual(values.params[:3], ['North', 10.0, 1])  # inline VALUES are bound parameters
        flt = self.stream_of(result, 'flt', 'true')
        self.assertIn('CASE WHEN', flt.sql)  # two-valued predicate: null comparisons are false
        self.assertIn('LOWER(', flt.sql)  # case-insensitive text comparison
        self.assertIn('NOT (', self.stream_of(result, 'flt', 'false').sql)
        fml = self.stream_of(result, 'fml')
        self.assertEqual([f.type for f in fml.fields][-3:], [FieldType.DOUBLE, FieldType.BOOL, FieldType.V_WSTRING])
        summary = self.stream_of(result, 'sum')
        self.assertEqual([(f.name, f.type) for f in summary.fields],
                         [('region', FieldType.V_WSTRING), ('total', FieldType.DOUBLE), ('n', FieldType.INT64)])
        self.assertIn('GROUP BY', summary.sql)
        self.assertIn('COLLATE "C"', summary.sql)  # binary grouping, like the local engine
        record = result.output('out').to_dicts()[0]
        # Outermost formulas' literals come first in the SQL text, then the inline VALUES.
        self.assertEqual(json.loads(record['Parameters'])[:6], ['r:', 'NORTH', 2, 'North', 10.0, 1])
        self.assertIn('summarize', record['Steps'])
        self.assertEqual(self.rec.queries, [])  # nothing was executed

    def test_parameters_follow_their_markers_in_sql_order(self):
        nodes = self.source() + [
            {'id': 'flt', 'tool': 'core.filter_indb', 'config': {'expression': '[region] = "North"'}},
            {'id': 'fml', 'tool': 'core.formula_indb', 'config': {'formulas': [{'field': 'big', 'expression': '[amount] > 7'}]}}]
        edges = [edge('local', 'output', 'in', 'input'), edge('in', 'output', 'flt', 'input'), edge('flt', 'true', 'fml', 'input')]
        s = self.stream_of(self.run_doc(nodes, edges), 'fml')
        # The formula literal (7) appears first in the SQL text, then the inline VALUES, then the filter literal.
        self.assertEqual(s.params[0], 7)
        self.assertEqual(s.params[1:4], ['North', 10.0, 1])
        self.assertEqual(s.params[-1], 'North')

    def test_untranslatable_expressions_are_rejected_with_reason(self):
        nodes = self.source() + [{'id': 'fml', 'tool': 'core.formula_indb',
                                  'config': {'formulas': [{'field': 'x', 'expression': 'Uppercase([region])'}]}}]
        result = self.run_doc(nodes, [edge('local', 'output', 'in', 'input'), edge('in', 'output', 'fml', 'input')])
        self.assertIn('cannot run in the database', result.error['message'])

    def test_join_union_and_sample(self):
        right = [text_input('local2', [('region', 'v_wstring'), ('manager', 'v_wstring'), ('amount', 'double')], [['North', 'Ann', 1.0]]),
                 {'id': 'in2', 'tool': 'core.data_stream_in', 'config': {'connection_id': 'pg'}}]
        nodes = self.source() + right + [
            {'id': 'join', 'tool': 'core.join_indb', 'config': {'keys': [{'left': 'region', 'right': 'region'}], 'join_type': 'left'}},
            {'id': 'union', 'tool': 'core.union_indb', 'config': {}},
            {'id': 'first', 'tool': 'core.sample_indb', 'config': {'method': 'first_n', 'n': 2, 'order_by': [{'field': 'amount', 'order': 'desc'}]}}]
        edges = [edge('local', 'output', 'in', 'input'), edge('local2', 'output', 'in2', 'input'),
                 edge('in', 'output', 'join', 'left'), edge('in2', 'output', 'join', 'right'),
                 edge('in', 'output', 'union', 'inputs'), edge('in2', 'output', 'union', 'inputs'),
                 edge('union', 'output', 'first', 'input')]
        result = self.run_doc(nodes, edges)
        self.assertTrue(result.ok, result.error)
        join = self.stream_of(result, 'join')
        self.assertEqual([f.name for f in join.fields], ['region', 'amount', 'units', 'Right_region', 'manager', 'Right_amount'])
        self.assertIn('IS NULL AND', join.sql)  # null keys match null keys, as in the local Join
        self.assertIn('LEFT OUTER JOIN', join.sql)
        union = self.stream_of(result, 'union')
        self.assertEqual([f.name for f in union.fields], ['region', 'amount', 'units', 'manager'])
        self.assertEqual(union.sql.count('CAST(NULL AS'), 2)  # units missing from the right, manager from the left
        first = self.stream_of(result, 'first')
        self.assertTrue(first.sql.endswith('LIMIT %s') and first.params[-1] == 2)

    def test_sql_server_specifics(self):
        nodes = self.source('ms') + [{'id': 'first', 'tool': 'core.sample_indb',
                                      'config': {'method': 'first_n', 'n': 1, 'order_by': [{'field': 'amount'}]}},
                                     {'id': 'pct', 'tool': 'core.sample_indb', 'config': {'method': 'random_percent', 'percent': 25}}]
        edges = [edge('local', 'output', 'in', 'input'), edge('in', 'output', 'first', 'input'), edge('in', 'output', 'pct', 'input')]
        result = self.run_doc(nodes, edges)
        self.assertTrue(result.ok, result.error)
        first = self.stream_of(result, 'first')
        self.assertTrue(first.sql.startswith('SELECT TOP (%s)') and first.params[0] == 1)
        pct = self.stream_of(result, 'pct')
        self.assertNotIn(' % ', pct.sql)  # no modulo operator that would clash with %s markers
        many = [text_input('local', [(f'c{i}', 'int32') for i in range(30)], [[i] * 30 for i in range(80)]),
                {'id': 'in', 'tool': 'core.data_stream_in', 'config': {'connection_id': 'ms'}}]
        too_many = self.run_doc(many, [edge('local', 'output', 'in', 'input')])
        self.assertIn('parameters', too_many.error['message'])
        with self.assertRaisesRegex(WorkflowValidationError, 'order_by'):
            self.run_doc(self.source() + [{'id': 's', 'tool': 'core.sample_indb', 'config': {'method': 'first_n'}}],
                         [edge('local', 'output', 'in', 'input'), edge('in', 'output', 's', 'input')])

    def test_streams_from_different_connections_cannot_join(self):
        nodes = self.source() + [text_input('l2', *SALES), {'id': 'in2', 'tool': 'core.data_stream_in', 'config': {'connection_id': 'pg2'}},
                                 {'id': 'join', 'tool': 'core.join_indb', 'config': {'keys': [{'left': 'region', 'right': 'region'}]}}]
        edges = [edge('local', 'output', 'in', 'input'), edge('l2', 'output', 'in2', 'input'),
                 edge('in', 'output', 'join', 'left'), edge('in2', 'output', 'join', 'right')]
        self.assertIn('different connections', self.run_doc(nodes, edges).error['message'])


class ExecutionTests(InDBTestCase):
    def test_connect_stream_out_and_browse_execute_the_built_query(self):
        self.rec.fetch_fields = [Field('id', FieldType.INT32), Field('name', FieldType.V_WSTRING, 40)]
        self.rec.fetch_rows = [(1, 'Ann'), (2, 'Bo'), (3, 'Cy')]
        nodes = [{'id': 'c', 'tool': 'core.connect_indb', 'config': {'connection_id': 'pg', 'mode': 'table', 'schema': 'sales', 'table': 'people'}},
                 {'id': 'f', 'tool': 'core.filter_indb', 'config': {'expression': '[id] >= 2'}},
                 {'id': 'b', 'tool': 'core.browse_indb', 'config': {'preview_rows': 2}},
                 {'id': 'o', 'tool': 'core.data_stream_out', 'config': {}}]
        edges = [edge('c', 'output', 'f', 'input'), edge('f', 'true', 'b', 'input'), edge('b', 'output', 'o', 'input')]
        result = self.run_doc(nodes, edges)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output('o').to_dicts()[0], {'id': 1, 'name': 'Ann'})  # rows as returned by the database
        self.assertEqual(len(result.browse['b']), 2)
        sql, params = self.rec.queries[-1]
        self.assertIn('FROM "sales"."people"', sql)
        self.assertEqual(params, (2,))
        with self.assertRaisesRegex(WorkflowValidationError, 'read-only|SELECT'):
            self.run_doc([{'id': 'c', 'tool': 'core.connect_indb', 'config': {'connection_id': 'pg', 'mode': 'query',
                                                                              'query': 'DELETE FROM sales.people'}}], [])

    def test_writes_need_a_writable_connection_and_a_run_grant(self):
        nodes = self.source() + [{'id': 'w', 'tool': 'core.write_indb', 'config': {'schema': 'scratch', 'table': 'copy', 'mode': 'replace'}}]
        edges = [edge('local', 'output', 'in', 'input'), edge('in', 'output', 'w', 'input')]
        denied = self.run_doc(nodes, edges)
        self.assertEqual(denied.error['code'], 'permission-denied')
        self.assertEqual(self.rec.writes, [])
        ok = self.run_doc(nodes, edges, limits={'allow_database_writes': True})
        self.assertTrue(ok.ok, ok.error)
        statements = [w[0] for w in self.rec.writes]
        self.assertEqual(statements[0], 'DROP TABLE IF EXISTS "scratch"."copy"')
        self.assertTrue(statements[1].startswith('CREATE TABLE "scratch"."copy" AS SELECT'))
        self.assertEqual(len(self.rec.writes[1][1]), 9)  # the stream's bound parameters travel with it
        readonly = self.source('readonly') + [nodes[-1]]
        self.assertIn('read-only', self.run_doc(readonly, edges, limits={'allow_database_writes': True}).error['message'])
        ms = self.source('ms') + [{'id': 'w', 'tool': 'core.write_indb', 'config': {'table': 't', 'mode': 'create'}}]
        self.rec.writes.clear()
        self.assertTrue(self.run_doc(ms, edges, limits={'allow_database_writes': True}).ok)
        self.assertTrue(self.rec.writes[0][0].startswith('SELECT [region], [amount], [units] INTO [t] FROM (SELECT * FROM (VALUES'))
        effects = workflow_effects(workflow(nodes, edges), ROOT)
        self.assertEqual(sorted(e['node_id'] for e in effects['database_writes']), ['in', 'w'])

    def test_dynamic_input_checks_every_query(self):
        self.rec.fetch_fields = [Field('id', FieldType.INT32)]
        queries = [['SELECT id FROM a'], ['SELECT id FROM b']]
        nodes = [text_input('q', [('sql', 'v_wstring')], queries),
                 {'id': 'd', 'tool': 'core.dynamic_input_indb', 'config': {'connection_id': 'pg', 'source_field': 'sql'}}]
        result = self.run_doc(nodes, [edge('q', 'output', 'd', 'input')])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.stream_of(result, 'd').sql.count('UNION ALL'), 1)
        nodes[0] = text_input('q', [('sql', 'v_wstring')], [['DROP TABLE a']])
        self.assertIn('record 1', self.run_doc(nodes, [edge('q', 'output', 'd', 'input')]).error['message'])

    def test_in_db_macro_passes_streams(self):
        child = workflow([{'id': 'mi', 'tool': 'core.macro_input_indb', 'config': {'name': 'Input'}},
                          {'id': 'f', 'tool': 'core.filter_indb', 'config': {'expression': '[units] > 1'}},
                          {'id': 'mo', 'tool': 'core.macro_output_indb', 'config': {'name': 'Output'}}],
                         [edge('mi', 'output', 'f', 'input'), edge('f', 'true', 'mo', 'input')])
        nodes = self.source() + [{'id': 'm', 'tool': 'flowengine.macro', 'config': {'workflow': child, 'input_bindings': {'Input': 'input'}}},
                                 {'id': 'out', 'tool': 'core.dynamic_output_indb', 'config': {}}]
        edges = [edge('local', 'output', 'in', 'input'), edge('in', 'output', 'm', 'input'), edge('m', 'output', 'out', 'input')]
        result = self.run_doc(nodes, edges)
        self.assertTrue(result.ok, result.error)
        self.assertIn('[units] > 1', result.output('out').column('Steps')[0])
        bad = self.run_doc([text_input('local', *SALES), nodes[2], nodes[3]], [edge('local', 'output', 'm', 'input'), edge('m', 'output', 'out', 'input')])
        self.assertIn('In-DB stream', bad.error['message'])


if __name__ == '__main__':
    unittest.main()
