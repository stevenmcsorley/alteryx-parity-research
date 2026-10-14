"""Scalable execution: Arrow/Parquet, streaming, spilling, parallel scheduling, history, profiles."""
import csv
import datetime as dt
import json
import random
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from flowengine import arrow
from flowengine.executor import run_workflow
from flowengine.history import RunHistory
from flowengine.storage import TableStore
from flowengine.table import Table
from flowengine.types import Field, FieldType as T

from helpers import ROOT, edge, fixed_clock, rows, text_input, workflow


def write_big_csv(path, n, seed=7):
    rng = random.Random(seed)
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['id', 'region', 'amount', 'note'])
        for i in range(n):
            w.writerow([i, rng.choice(['N', 'S', 'E', 'W', '']), f'{rng.uniform(-100, 1000):.2f}',
                        rng.choice(['ok', '  spaced  ', 'zoë', '', 'a,b'])])


PIPE_NODES = [
    {'id': 'src', 'tool': 'core.input_data', 'config': {
        'path': 'big.csv', 'empty_as_null': True,
        'fields': [{'name': 'id', 'type': 'int64'}, {'name': 'region', 'type': 'v_string', 'size': 4},
                   {'name': 'amount', 'type': 'double'}, {'name': 'note', 'type': 'v_wstring', 'size': 40}]}},
    {'id': 'flt', 'tool': 'core.filter', 'config': {'expression': '[amount] > 0 AND !IsNull([region])'}},
    {'id': 'fx', 'tool': 'core.formula', 'config': {'formulas': [
        {'field': 'band', 'expression': 'IIF([amount] > 500, "high", "low")'},
        {'field': 'note', 'expression': 'Trim([note])'}]}},
    {'id': 'rid', 'tool': 'core.record_id', 'config': {'start': 100}},
    {'id': 'sel', 'tool': 'core.select', 'config': {'fields': [{'field': 'note', 'selected': False}]}},
    {'id': 'out', 'tool': 'core.output_data', 'config': {'path': 'out/result.csv'}},
]
PIPE_EDGES = [edge('src', 'output', 'flt', 'input'), edge('flt', 'true', 'fx', 'input'),
              edge('fx', 'output', 'rid', 'input'), edge('rid', 'output', 'sel', 'input'),
              edge('sel', 'output', 'out', 'input')]


def pipe():
    return workflow(json.loads(json.dumps(PIPE_NODES)), list(PIPE_EDGES))


class ArrowTests(unittest.TestCase):
    def test_roundtrip_all_types(self):
        fields = [Field('b', T.BOOL), Field('i8', T.BYTE), Field('i', T.INT32), Field('l', T.INT64),
                  Field('d', T.DOUBLE), Field('f', T.FIXED_DECIMAL, 12, 2), Field('s', T.V_WSTRING, 20),
                  Field('dt', T.DATE), Field('ts', T.DATETIME), Field('tm', T.TIME), Field('bl', T.BLOB)]
        row = [True, 7, -3, 2**40, 1.5, Decimal('12.34'), 'zoë', dt.date(2024, 2, 29),
               dt.datetime(2024, 1, 2, 3, 4, 5), dt.time(1, 2, 3), b'\x00\x01']
        t = Table.from_rows(fields, [row, [None] * len(fields)])
        self.assertEqual(arrow.from_arrow(arrow.to_arrow(t)), t)

    def test_parquet_and_ipc_io_through_tools(self):
        with tempfile.TemporaryDirectory() as d:
            doc = workflow([text_input('t', [('a', 'int32'), ('s', 'v_wstring')], [[1, 'x'], [None, 'ü']]),
                            {'id': 'p', 'tool': 'core.output_data', 'config': {'path': 'o.parquet'}},
                            {'id': 'a', 'tool': 'core.output_data', 'config': {'path': 'o.arrow'}}],
                           [edge('t', 'output', 'p', 'input'), edge('t', 'output', 'a', 'input')])
            self.assertTrue(run_workflow(doc, base_dir=Path(d)).ok)
            for f in ('o.parquet', 'o.arrow'):
                r = run_workflow(workflow([{'id': 'i', 'tool': 'core.input_data', 'config': {'path': f}}], []),
                                 base_dir=Path(d))
                self.assertEqual(rows(r.output('i')), [[1, 'x'], [None, 'ü']], f)
                self.assertEqual(r.output('i').schema.field('a').type, T.INT32)


class StreamingTests(unittest.TestCase):
    def test_streamed_equals_batch(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_big_csv(d / 'big.csv', 5000)
            batch = run_workflow(pipe(), base_dir=d, clock=fixed_clock)
            text_batch = (d / 'out/result.csv').read_text(encoding='utf-8')
            streamed = run_workflow(pipe(), base_dir=d, clock=fixed_clock, batch_rows=333, retain='none')
            self.assertTrue(streamed.ok, streamed.error)
            self.assertEqual((d / 'out/result.csv').read_text(encoding='utf-8'), text_batch)
            for n in ('src', 'flt', 'fx', 'rid', 'sel'):
                self.assertTrue(streamed.manifest['nodes'][n].get('streamed'), n)
                self.assertEqual(streamed.manifest['nodes'][n]['outputs'], batch.manifest['nodes'][n]['outputs'], n)
            info = [e['message'] for e in streamed.events
                    if e['type'] == 'info' and 'streamed segment' in e.get('message', '')]
            self.assertIn('16 batch(es)', info[0])

    def test_non_streamable_tool_breaks_segment(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_big_csv(d / 'big.csv', 50)
            doc = pipe()
            doc['nodes'].insert(4, {'id': 'srt', 'tool': 'core.sort', 'config': {'keys': [{'field': 'amount'}]}})
            doc['edges'] = PIPE_EDGES[:3] + [edge('rid', 'output', 'srt', 'input'),
                                             edge('srt', 'output', 'sel', 'input'), PIPE_EDGES[4]]
            r = run_workflow(doc, base_dir=d, batch_rows=10)
            self.assertTrue(r.ok, r.error)
            self.assertTrue(r.manifest['nodes']['flt'].get('streamed') is None)  # source has a non-linear tail
            self.assertFalse(r.manifest['nodes']['srt'].get('streamed'))
            self.assertTrue(r.manifest['nodes']['sel'].get('streamed') is None)

    def test_streaming_conversion_error_reports_absolute_row(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / 'big.csv').write_text('id,region,amount,note\n' + ''.join(f'{i},N,1,x\n' for i in range(25))
                                       + 'oops,N,1,x\n', encoding='utf-8')
            r = run_workflow(pipe(), base_dir=d, batch_rows=10)
            self.assertEqual(r.error['code'], 'data-error')
            self.assertIn('row 26', r.error['message'])
            self.assertFalse((d / 'out/result.csv').exists())
            self.assertFalse(list((d / 'out').glob('.flowengine-*')))


class SpillAndParallelTests(unittest.TestCase):
    def doc(self):
        nodes = [json.loads(json.dumps(PIPE_NODES[0])),
                 {'id': 'a', 'tool': 'core.sort', 'config': {'keys': [{'field': 'amount'}]}},
                 {'id': 'b', 'tool': 'core.summarize', 'config': {'actions': [
                     {'field': 'region', 'op': 'group_by'}, {'field': 'amount', 'op': 'sum'}]}},
                 {'id': 'c', 'tool': 'core.unique', 'config': {'fields': ['note']}},
                 {'id': 'j', 'tool': 'core.join', 'config': {'by': 'fields',
                                                             'keys': [{'left': 'region', 'right': 'region'}]}}]
        edges = [edge('src', 'output', 'a', 'input'), edge('src', 'output', 'b', 'input'),
                 edge('a', 'output', 'c', 'input'), edge('c', 'unique', 'j', 'left'), edge('b', 'output', 'j', 'right')]
        return workflow(nodes, edges)

    def test_spill_and_parallel_give_identical_results(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_big_csv(d / 'big.csv', 3000)
            base = run_workflow(self.doc(), base_dir=d, clock=fixed_clock)
            for kw in ({'memory_budget': 50_000}, {'workers': 4}, {'workers': 3, 'memory_budget': 1}):
                r = run_workflow(self.doc(), base_dir=d, clock=fixed_clock, **kw)
                self.assertTrue(r.ok, (kw, r.error))
                self.assertEqual({n: s['outputs'] for n, s in r.manifest['nodes'].items()},
                                 {n: s['outputs'] for n, s in base.manifest['nodes'].items()}, kw)
                again = run_workflow(self.doc(), base_dir=d, clock=fixed_clock, **kw)
                seq = lambda res: [(e['type'], e['node_id']) for e in res.events if e['type'].startswith('node-')]
                self.assertEqual(seq(r), seq(again), kw)  # deterministic event order per configuration
                if kw.get('workers', 1) == 1:
                    self.assertEqual(seq(r), seq(base), kw)
            r = run_workflow(self.doc(), base_dir=d, memory_budget=50_000, spill_dir=d)
            self.assertGreater(r.manifest['stats']['spills'], 0)
            self.assertFalse(list(d.glob('flowengine-spill-*')), 'spill directory removed after run')

    def test_store_protects_current_and_reloads(self):
        events = []
        s = TableStore(10, emit=lambda t, **k: events.append(k.get('spill')))
        t1 = Table([Field('a', T.INT32)], [[1, 2, 3]])
        t2 = Table([Field('a', T.INT32)], [[4, 5, 6]])
        s.put(('x', 'o'), t1)
        s.put(('y', 'o'), t2)
        self.assertEqual(s.get(('x', 'o')), t1)
        self.assertIn('write', events)
        self.assertIn('reload', events)
        s.close()


class HistoryAndProfileTests(unittest.TestCase):
    def test_history_records_runs(self):
        with tempfile.TemporaryDirectory() as d:
            h = RunHistory(Path(d) / 'h.sqlite')
            doc = workflow([text_input('t', [('a', 'int32')], [[1]])], [], name='hist test')
            r1 = run_workflow(doc, base_dir=ROOT, history=h)
            r2 = run_workflow(doc, base_dir=ROOT, history=h, profile='ayx-2026.2-e2')
            self.assertEqual({x['run_id'] for x in h.list()}, {r1.run_id, r2.run_id})
            got = h.get(r2.run_id)
            self.assertEqual(got['manifest']['profile']['name'], 'ayx-2026.2-e2')
            self.assertEqual(got['workflow'], doc)
            self.assertEqual([e['type'] for e in got['events']], [e['type'] for e in r2.events])

    def test_profiles_change_only_evidenced_behaviour(self):
        with tempfile.TemporaryDirectory() as d:
            doc = workflow([text_input('t', [('x', 'double')], [[1.4000000000000001]]),
                            {'id': 'o', 'tool': 'core.output_data', 'config': {'path': 'o.csv'}}],
                           [edge('t', 'output', 'o', 'input')])
            run_workflow(doc, base_dir=Path(d), profile='ayx-2026.2-e1')
            self.assertEqual((Path(d) / 'o.csv').read_text(), 'x\n1.4\n')
            run_workflow(doc, base_dir=Path(d), profile='ayx-2026.2-e2')
            self.assertEqual((Path(d) / 'o.csv').read_text(), 'x\n1.4000000000000001\n')
        from flowengine.errors import ConfigError
        with self.assertRaises(ConfigError):
            run_workflow(workflow([], []), base_dir=ROOT, profile='nope')


if __name__ == '__main__':
    unittest.main()
