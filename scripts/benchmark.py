#!/usr/bin/env python3
"""Reproducible performance measurements for flowengine (writes docs/BENCHMARKS.md).

Synthetic data is generated deterministically. Reports per-tool throughput on N records and the
effect of streaming, parallel workers and a memory budget on an end-to-end pipeline. Timings are
machine-dependent; the report records the machine, Python version and engine version.
"""
from __future__ import annotations

import argparse
import csv
import platform
import random
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flowengine import __version__  # noqa: E402
from flowengine.executor import run_workflow  # noqa: E402


def make_csv(path: Path, n: int):
    rng = random.Random(42)
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['id', 'region', 'product', 'qty', 'price', 'day'])
        for i in range(n):
            w.writerow([i, rng.choice('NSEW'), f'p{rng.randrange(200)}', rng.randrange(1, 20), f'{rng.uniform(1, 100):.2f}',
                        f'2025-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}'])


SRC = {'id': 'src', 'tool': 'core.input_data', 'config': {
    'path': 'data.csv', 'fields': [{'name': 'id', 'type': 'int64'}, {'name': 'region', 'type': 'v_string', 'size': 2},
                                   {'name': 'product', 'type': 'v_string', 'size': 8}, {'name': 'qty', 'type': 'int32'},
                                   {'name': 'price', 'type': 'double'}, {'name': 'day', 'type': 'date'}]}}
TOOLS = {
    'Filter': {'tool': 'core.filter', 'config': {'expression': '[qty] > 5 AND [region] != "N"'}, 'out': 'true'},
    'Formula': {'tool': 'core.formula', 'config': {'formulas': [{'field': 'total', 'expression': '[qty] * [price]'},
                                                                {'field': 'label', 'expression': '[region] + "-" + [product]'}]}},
    'Sort': {'tool': 'core.sort', 'config': {'keys': [{'field': 'product'}, {'field': 'price', 'order': 'desc'}]}},
    'Summarize': {'tool': 'core.summarize', 'config': {'actions': [{'field': 'region', 'op': 'group_by'},
                                                                   {'field': 'product', 'op': 'group_by'},
                                                                   {'field': 'price', 'op': 'sum'},
                                                                   {'field': 'qty', 'op': 'avg'}]}},
    'Unique': {'tool': 'core.unique', 'config': {'fields': ['product', 'region']}, 'out': 'unique'},
    'Record ID': {'tool': 'core.record_id', 'config': {}},
    'Cross Tab': {'tool': 'core.cross_tab', 'config': {'group_by': ['product'], 'header_field': 'region',
                                                       'value_field': 'qty', 'method': 'sum'}},
}


def doc(nodes, edges):
    return {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': nodes, 'edges': edges}


def edge(a, aa, b, ba):
    return {'from': {'node': a, 'anchor': aa}, 'to': {'node': b, 'anchor': ba}}


def timed(fn):
    tracemalloc.start()
    t0 = time.perf_counter()
    r = fn()
    dt = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return r, dt, peak


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--rows', type=int, default=200_000)
    p.add_argument('--out', type=Path, default=ROOT / 'docs' / 'BENCHMARKS.md')
    args = p.parse_args()
    n = args.rows
    lines = []
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        make_csv(d / 'data.csv', n)
        _, t_read, m_read = timed(lambda: run_workflow(doc([SRC], []), base_dir=d, retain='none'))
        tool_rows = [('Input Data (typed CSV)', t_read, m_read)]
        for name, spec in TOOLS.items():
            wf = doc([SRC, {'id': 't', 'tool': spec['tool'], 'config': spec['config']}], [edge('src', 'output', 't', 'input')])
            r, dt, mem = timed(lambda: run_workflow(wf, base_dir=d, retain='none'))
            assert r.ok, r.error
            tool_rows.append((name, max(dt - t_read, 1e-9), mem))
        pipe = doc([SRC, {'id': 'f', 'tool': 'core.filter', 'config': TOOLS['Filter']['config']},
                    {'id': 'fx', 'tool': 'core.formula', 'config': TOOLS['Formula']['config']},
                    {'id': 'o', 'tool': 'core.output_data', 'config': {'path': 'out.csv'}}],
                   [edge('src', 'output', 'f', 'input'), edge('f', 'true', 'fx', 'input'), edge('fx', 'output', 'o', 'input')])
        modes = []
        for label, kw in [('batch', {}), ('streaming (50k-row batches)', {'batch_rows': 50_000})]:
            r, dt, mem = timed(lambda: run_workflow(pipe, base_dir=d, retain='none', **kw))
            assert r.ok, r.error
            modes.append((label, dt, mem, (d / 'out.csv').stat().st_size))
        specs = list(TOOLS.values())
        fan = doc([SRC] + [{'id': f'b{i}', 'tool': spec['tool'], 'config': spec['config']} for i, spec in enumerate(specs)]
                  + [{'id': f'c{i}', 'tool': 'core.count_records', 'config': {}} for i in range(len(specs))],
                  [edge('src', 'output', f'b{i}', 'input') for i in range(len(specs))]
                  + [edge(f'b{i}', spec.get('out', 'output'), f'c{i}', 'input') for i, spec in enumerate(specs)])
        par = []
        for label, kw in [('1 worker', {'workers': 1}), ('4 workers', {'workers': 4}),
                          ('1 worker, 20 MB memory budget (spilling)', {'workers': 1, 'memory_budget': 20_000_000})]:
            r, dt, mem = timed(lambda: run_workflow(fan, base_dir=d, retain='none', **kw))
            assert r.ok, r.error
            par.append((label, dt, mem, r.manifest['stats']['spills']))
    lines += ['# Performance measurements', '',
              f'Generated by `python scripts/benchmark.py --rows {n}`. Engine {__version__}, Python {platform.python_version()}, '
              f'{platform.system()} {platform.release()}, {platform.processor() or platform.machine()}.', '',
              'Timings are wall-clock on one machine and vary between runs; they are measurements, not guarantees. '
              'Peak memory is the Python-allocated peak (tracemalloc), excluding interpreter baseline.', '',
              f'## Per-tool throughput ({n:,} records)', '', '| Tool | Seconds | Records / s | Peak MB |', '|---|---:|---:|---:|']
    for name, dt, mem in tool_rows:
        lines.append(f'| {name} | {dt:.2f} | {n / dt:,.0f} | {mem / 1e6:,.0f} |')
    lines += ['', '## Streaming (Input → Filter → Formula → Output)', '',
              '| Mode | Seconds | Peak MB | Output bytes |', '|---|---:|---:|---:|']
    for label, dt, mem, size in modes:
        lines.append(f'| {label} | {dt:.2f} | {mem / 1e6:,.0f} | {size:,} |')
    lines += ['', '## Parallel branches and spilling (one input, seven independent tools, each followed by Count Records)', '',
              '| Configuration | Seconds | Peak MB | Spills |', '|---|---:|---:|---:|']
    for label, dt, mem, spills in par:
        lines.append(f'| {label} | {dt:.2f} | {mem / 1e6:,.0f} | {spills} |')
    lines += ['', 'Notes: kernels are pure Python (ADR-001), so CPU-bound branches gain little from threads (GIL); '
              'streaming bounds memory for eligible linear segments; the memory budget spills idle intermediate '
              'tables to disk, which bounds memory when many large tables coexist; in this benchmark the peak is '
              'set by the single largest operation (one kernel plus its inputs), so spilling adds time and reload '
              'overhead without lowering the peak. Kernel-level external algorithms (external sort/aggregate) are '
              'required to bound that and are not implemented yet. Arrow-backed kernels are the planned '
              'optimisation path and must pass the same conformance cases.', '']
    args.out.write_text('\n'.join(lines), encoding='utf-8')
    print(f'wrote {args.out}')


if __name__ == '__main__':
    main()
