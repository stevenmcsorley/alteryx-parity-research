"""Executor, validation, logging, determinism and end-to-end golden tests."""
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from flowengine.errors import WorkflowValidationError
from flowengine.executor import plan_workflow, run_file, run_workflow, save_workflow, workflow_digest
from flowengine.plugin import CancelToken, Registry, default_registry

from helpers import ROOT, edge, fixed_clock, rows, run_tool, text_input, workflow

SRC = text_input('src', [('a', 'int32')], [[1], [2], [3]])
EXAMPLE = ROOT / 'examples' / 'sales' / 'monthly_sales.flow.json'
GOLDEN = ROOT / 'tests' / 'golden' / 'monthly_sales'


def problems_of(doc):
    return plan_workflow(doc, base_dir=ROOT)[1]


class ValidationTests(unittest.TestCase):
    def codes(self, doc):
        return sorted(p['code'] for p in problems_of(doc))

    def test_document_schema(self):
        self.assertEqual(set(self.codes({'nodes': []})), {'document-invalid'})  # format, formatVersion, edges
        self.assertEqual(self.codes(workflow([{'id': 'bad id!', 'tool': 'x', 'config': {}}], [])), ['document-invalid'])

    def test_unknown_and_unavailable_tools_are_explicit(self):
        p = problems_of(workflow([{'id': 'x', 'tool': 'core.nope', 'config': {}}], []))
        self.assertEqual(p[0]['code'], 'tool-unavailable')
        p = problems_of(workflow([{'id': 'x', 'tool': 'predictive:linear-regression', 'config': {}}], []))
        self.assertEqual(p[0]['code'], 'tool-unavailable')
        self.assertIn('Linear Regression', p[0]['message'])
        self.assertIn('no working implementation', p[0]['message'])

    def test_catalog_id_resolves_to_implementation(self):
        doc = workflow([SRC, {'id': 'c', 'tool': 'transform:count-records', 'config': {}}],
                       [edge('src', 'output', 'c', 'input')])
        r = run_workflow(doc, base_dir=ROOT)
        self.assertEqual(rows(r.output('c')), [[3]])

    def test_graph_problems(self):
        f = {'id': 'f', 'tool': 'core.filter', 'config': {'expression': '[a] > 1'}}
        self.assertEqual(self.codes(workflow([SRC, f], [])), ['anchor-unconnected'])
        self.assertEqual(self.codes(workflow([SRC, f], [edge('src', 'nope', 'f', 'input')])),
                         ['anchor-unconnected', 'edge-unknown-anchor'])
        self.assertEqual(self.codes(workflow([SRC, f], [edge('src', 'output', 'f', 'input'),
                                                        edge('src', 'output', 'f', 'input')])),
                         ['anchor-overconnected'])
        self.assertEqual(self.codes(workflow([SRC], [edge('src', 'output', 'ghost', 'input')])),
                         ['edge-unknown-node'])
        self.assertEqual(self.codes(workflow([SRC, dict(SRC)], [])), ['duplicate-node-id'])

    def test_cycle(self):
        f1 = {'id': 'f1', 'tool': 'core.sort', 'config': {'keys': [{'field': 'a'}]}}
        f2 = {'id': 'f2', 'tool': 'core.sort', 'config': {'keys': [{'field': 'a'}]}}
        p = problems_of(workflow([f1, f2], [edge('f1', 'output', 'f2', 'input'), edge('f2', 'output', 'f1', 'input')]))
        self.assertEqual([x['code'] for x in p], ['cycle'])
        self.assertIn("['f1', 'f2']", p[0]['message'])

    def test_config_schema_errors_name_the_node(self):
        p = problems_of(workflow([SRC, {'id': 's', 'tool': 'core.sort', 'config': {'keys': [{'field': 'a', 'order': 'up'}]}}],
                                 [edge('src', 'output', 's', 'input')]))
        self.assertEqual((p[0]['code'], p[0]['node_id']), ('config-invalid', 's'))
        self.assertIn("'up' is not one of", p[0]['message'])

    def test_static_schema_errors_detected_before_running(self):
        p = problems_of(workflow([SRC, {'id': 'f', 'tool': 'core.filter', 'config': {'expression': '[b] = 1'}}],
                                 [edge('src', 'output', 'f', 'input')]))
        self.assertEqual((p[0]['code'], p[0]['node_id']), ('expression-error', 'f'))
        self.assertIn('Unknown field [b]', p[0]['message'])

    def test_static_schemas_propagate(self):
        plan, p = plan_workflow(workflow([SRC, {'id': 'f', 'tool': 'core.formula', 'config': {
            'formulas': [{'field': 'b', 'expression': '[a] * 2'}]}}], [edge('src', 'output', 'f', 'input')]))
        self.assertEqual(p, [])
        self.assertEqual(plan.schemas[('f', 'output')].names, ['a', 'b'])

    def test_validation_never_executes_generators(self):
        # regression: schema inference used to run Generate Rows' loop for input-less sources
        import time
        doc = workflow([{'id': 'g', 'tool': 'core.generate_rows', 'config': {
            'field': 'n', 'type': 'int64', 'init': '1', 'condition': '[n] <= 50000000', 'loop': '[n] + 1',
            'max_rows_per_record': 50000001}}], [])
        t0 = time.perf_counter()
        plan, problems = plan_workflow(doc, base_dir=ROOT)
        self.assertEqual(problems, [])
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(plan.schemas[('g', 'output')].names, ['n'])

    def test_tool_version_mismatch(self):
        p = problems_of(workflow([dict(SRC, toolVersion='2.0')], []))
        self.assertEqual(p[0]['code'], 'tool-version-mismatch')

    def test_registry_rejects_duplicate_catalogue_claims(self):
        reg = Registry()
        from flowengine.tools.preparation import Filter
        reg.register(Filter)
        with self.assertRaises(ValueError):
            reg.register(Filter)

    def test_every_manifest_is_wellformed_and_catalogued(self):
        from flowengine import catalog
        ids = {e['id'] for e in catalog.entries()}
        for t in default_registry().tools():
            m = t.manifest
            for cid in m.catalog_ids:
                self.assertIn(cid, ids, m.tool_id)
            json.dumps(m.to_dict())  # serialisable for the editor


class ExecutionTests(unittest.TestCase):
    def test_failure_reports_node_and_skips_downstream(self):
        doc = workflow([text_input('src', [('s', 'v_string')], [['1'], ['x']]),
                        {'id': 'cast', 'tool': 'core.select', 'config': {'fields': [{'field': 's', 'type': 'int32'}]}},
                        {'id': 'after', 'tool': 'core.count_records', 'config': {}}],
                       [edge('src', 'output', 'cast', 'input'), edge('cast', 'output', 'after', 'input')])
        r = run_workflow(doc, base_dir=ROOT)
        self.assertEqual(r.status, 'failed')
        self.assertEqual((r.error['node_id'], r.error['code']), ('cast', 'data-error'))
        types = [(e['type'], e['node_id']) for e in r.events]
        self.assertIn(('node-failed', 'cast'), types)
        self.assertIn(('node-skipped', 'after'), types)
        self.assertEqual(types[-1], ('run-completed', None))

    def test_disabled_nodes_skip_descendants(self):
        doc = workflow([SRC, {'id': 'c', 'tool': 'core.count_records', 'config': {}, 'disabled': True},
                        {'id': 'd', 'tool': 'core.browse', 'config': {}}],
                       [edge('src', 'output', 'c', 'input'), edge('c', 'output', 'd', 'input')])
        r = run_workflow(doc, base_dir=ROOT)
        self.assertTrue(r.ok)
        self.assertEqual(sorted(e['node_id'] for e in r.events if e['type'] == 'node-skipped'), ['c', 'd'])

    def test_disabled_flowbench_tool_container_children_skip_execution(self):
        doc = workflow([SRC, {'id': 'contained_count', 'tool': 'core.count_records', 'config': {}, 'disabled': True},
                        {'id': 'after', 'tool': 'core.browse', 'config': {}}],
                       [edge('src', 'output', 'contained_count', 'input'), edge('contained_count', 'output', 'after', 'input')])
        doc['metadata'] = {'ui': {'containers': [{
            'id': 'sales_group', 'caption': 'Sales group', 'disabled': True, 'collapsed': False,
            'position': {'x': 0, 'y': 0}, 'size': {'width': 400, 'height': 200},
            'children': ['contained_count'],
        }]}}
        result = run_workflow(doc, base_dir=ROOT)
        self.assertTrue(result.ok)
        self.assertEqual(sorted(event['node_id'] for event in result.events if event['type'] == 'node-skipped'),
                         ['after', 'contained_count'])

    def test_cancellation(self):
        tok = CancelToken()
        seen = []

        def listener(ev):
            seen.append(ev['type'])
            if ev['type'] == 'node-completed':
                tok.cancel()
        doc = workflow([SRC, {'id': 'c', 'tool': 'core.count_records', 'config': {}}], [edge('src', 'output', 'c', 'input')])
        r = run_workflow(doc, base_dir=ROOT, cancel_token=tok, listener=listener)
        self.assertEqual(r.status, 'cancelled')
        self.assertIn('run-cancelled', seen)

    def test_jsonl_log_and_retain_none(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'run.jsonl'
            doc = workflow([SRC, {'id': 'b', 'tool': 'core.browse', 'config': {}}], [edge('src', 'output', 'b', 'input')])
            r = run_workflow(doc, base_dir=ROOT, log_path=log, retain='none')
            lines = [json.loads(l) for l in log.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([l['type'] for l in lines], [e['type'] for e in r.events])
            self.assertEqual({l['run_id'] for l in lines}, {r.run_id})
            self.assertEqual(r.outputs, {})
            self.assertEqual(len(r.browse['b']), 3)

    def test_fan_out_and_release(self):
        doc = workflow([SRC, {'id': 'x', 'tool': 'core.count_records', 'config': {}},
                        {'id': 'y', 'tool': 'core.sample', 'config': {'mode': 'first', 'n': 1}}],
                       [edge('src', 'output', 'x', 'input'), edge('src', 'output', 'y', 'input')])
        r = run_workflow(doc, base_dir=ROOT)
        self.assertEqual(rows(r.output('x')), [[3]])
        self.assertEqual(rows(r.output('y')), [[1]])

    def test_internal_kernel_bug_is_surfaced(self):
        from flowengine.plugin import Anchor, Tool, ToolManifest

        class Broken(Tool):
            manifest = ToolManifest('test.broken', '1.0.0', 'Broken', (), (), (Anchor('output', 'O'),),
                                    {'type': 'object'})

            def infer(self, ctx, cfg, inputs):
                return None

            def run(self, ctx, cfg, inputs):
                return {'wrong': None}
        reg = Registry()
        reg.register(Broken)
        r = run_workflow(workflow([{'id': 'b', 'tool': 'test.broken', 'config': {}}], []), base_dir=ROOT, registry=reg)
        self.assertEqual(r.error['code'], 'internal-error')

    def test_save_refuses_invalid_document(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(WorkflowValidationError):
                save_workflow({'nodes': 1}, Path(d) / 'x.json')

    def test_digest_ignores_layout(self):
        a = workflow([dict(SRC, position={'x': 1, 'y': 2})], [])
        b = workflow([dict(SRC, position={'x': 9, 'y': 9}, label='moved')], [])
        self.assertEqual(workflow_digest(a), workflow_digest(b))


class PropertyTests(unittest.TestCase):
    """Seeded randomised invariants (deterministic: fixed seeds)."""

    def random_table(self, rng, n):
        regions = ['N', 'S', 'E', None]
        return ([('k', 'int32'), ('r', 'v_string'), ('v', 'double')],
                [[rng.randint(0, 6), rng.choice(regions), None if rng.random() < 0.1 else round(rng.uniform(-50, 50), 2)]
                 for _ in range(n)])

    def test_invariants(self):
        for seed in range(25):
            rng = random.Random(seed)
            data = self.random_table(rng, rng.randint(0, 40))
            src = data[1]
            out, _ = run_tool('core.filter', {'expression': '[v] > 0'}, {'input': data})
            self.assertEqual(len(out['true']) + len(out['false']), len(src))
            self.assertTrue(all(v is not None and v > 0 for v in out['true'].column('v')))
            out, _ = run_tool('core.sort', {'keys': [{'field': 'k'}]}, {'input': data})
            s = rows(out['output'])
            self.assertEqual(sorted(map(tuple, s), key=repr), sorted(map(tuple, src), key=repr))  # permutation
            self.assertEqual(s, sorted(src, key=lambda r: r[0]))  # stable == Python's stable sort
            out, _ = run_tool('core.unique', {'fields': ['k', 'r']}, {'input': data})
            self.assertEqual(len(out['unique']) + len(out['duplicates']), len(src))
            self.assertEqual(len(out['unique']), len({(r[0], r[1]) for r in src}))
            right = self.random_table(rng, rng.randint(0, 20))
            out, _ = run_tool('core.join', {'by': 'fields', 'keys': [{'left': 'k', 'right': 'k'}]},
                              {'left': data, 'right': right})
            expected = sum(1 for a in src for b in right[1] if a[0] == b[0])
            self.assertEqual(len(out['joined']), expected)
            out, _ = run_tool('core.summarize', {'actions': [{'field': 'r', 'op': 'group_by'},
                                                             {'field': 'v', 'op': 'count'}]}, {'input': data})
            self.assertEqual(sum(out['output'].column('Count_v')), len(src))
            out, _ = run_tool('core.union', {}, {'inputs': [data, right]})
            self.assertEqual(len(out['output']), len(src) + len(right[1]))


class EndToEndTests(unittest.TestCase):
    """Golden test of examples/sales: real files in, real files out, through the CLI path."""

    def run_example(self, tmp):
        work = Path(tmp) / 'sales'
        shutil.copytree(EXAMPLE.parent, work, ignore=shutil.ignore_patterns('out'))
        return run_file(work / EXAMPLE.name, clock=fixed_clock, log_path=Path(tmp) / 'run.jsonl'), work

    def test_golden_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, work = self.run_example(tmp)
            self.assertTrue(r.ok, r.error)
            for name in ('monthly_sales.csv', 'unmatched_orders.csv'):
                self.assertEqual((work / 'out' / name).read_text(encoding='utf-8'),
                                 (GOLDEN / name).read_text(encoding='utf-8'), name)
            self.assertEqual(len(r.browse['preview']), 4)
            counts = {n: {a: o['rows'] for a, o in s['outputs'].items()} for n, s in r.manifest['nodes'].items()}
            self.assertEqual(counts['first_customer_row'], {'duplicates': 1, 'unique': 6})
            self.assertEqual(counts['match_customer'], {'joined': 7, 'left_unjoined': 2, 'right_unjoined': 0})

    def test_runs_are_reproducible(self):
        digests = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                r, _ = self.run_example(tmp)
                digests.append({n: {a: o['digest'] for a, o in s['outputs'].items()} for n, s in r.manifest['nodes'].items()})
        self.assertEqual(digests[0], digests[1])

    def test_cli(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / 'sales'
            shutil.copytree(EXAMPLE.parent, work, ignore=shutil.ignore_patterns('out'))
            p = subprocess.run([sys.executable, '-m', 'flowengine', 'run', str(work / EXAMPLE.name),
                                '--manifest', str(Path(tmp) / 'm.json')], capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('SUCCEEDED', p.stdout)
            m = json.loads((Path(tmp) / 'm.json').read_text())
            self.assertEqual(m['status'], 'succeeded')
            bad = Path(tmp) / 'bad.json'
            bad.write_text(json.dumps(workflow([{'id': 'x', 'tool': 'core.nope', 'config': {}}], [])))
            p = subprocess.run([sys.executable, '-m', 'flowengine', 'validate', str(bad)], capture_output=True,
                               text=True, cwd=ROOT)
            self.assertEqual(p.returncode, 2)
            self.assertIn('tool-unavailable', p.stderr)


if __name__ == '__main__':
    unittest.main()
