"""Executable tool examples harvested from the test suite.

``scripts/build_tool_examples.py`` runs the tests with ``FLOWENGINE_EXAMPLE_LOG`` set; the executor
then records, for every successful node of a side-effect-free tool, its configuration (defaults
removed), small input tables and the output it produced. The script keeps the smallest distinct
examples per tool in ``data/tool-examples.json``; ``tests/test_tool_examples.py`` re-runs every
stored example against its kernel, so an example can never describe behaviour the tool lacks.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .types import format_value

EXAMPLES_PATH = Path(__file__).resolve().parents[1] / 'data' / 'tool-examples.json'
MAX_INPUT_ROWS, MAX_FIELDS, MAX_TEXT, OUTPUT_ROWS = 200, 24, 200, 10
_lock = threading.Lock()


def _fields(t):
    return [{k: v for k, v in f.to_dict().items() if k in ('name', 'type', 'size', 'scale')} for f in t.schema]


def _input_table(t):
    """Whole table, replayable through Text Input: no blobs, bounded size."""
    if len(t) > MAX_INPUT_ROWS or len(t.schema) > MAX_FIELDS or any(f.type.value == 'blob' for f in t.schema):
        return None
    rows = []
    for r in t.rows():
        row = [None if v is None else format_value(v) for v in r]
        if any(isinstance(v, str) and len(v) > MAX_TEXT for v in row):
            return None
        rows.append(row)
    return {'fields': _fields(t), 'rows': rows}


def _output_table(t):
    """Schema, row count and the first rows; blob cells (models, charts, images) are marked, not stored."""
    blobs = {i for i, f in enumerate(t.schema) if f.type.value == 'blob'}
    rows = []
    for _, r in zip(range(OUTPUT_ROWS), t.rows()):
        rows.append([('[blob]' if v is not None else None) if i in blobs else (None if v is None else format_value(v)[:MAX_TEXT])
                     for i, v in enumerate(r)])
    return {'fields': _fields(t), 'row_count': len(t), 'rows': rows}


def _minimal(config: dict, schema: dict) -> dict:
    props = (schema or {}).get('properties', {})
    return {k: v for k, v in config.items() if not (k in props and 'default' in props[k] and props[k]['default'] == v)}


def record(tool, config: dict, inputs: dict, outputs: dict) -> None:
    """Called by the executor when FLOWENGINE_EXAMPLE_LOG is set; never raises."""
    log = os.environ.get('FLOWENGINE_EXAMPLE_LOG')
    if not log or tool.manifest.side_effects != 'none' or not isinstance(outputs, dict):
        return
    try:
        ins = {}
        for anchor, value in (inputs or {}).items():
            if isinstance(value, list):
                return  # multi-connection inputs are not replayed
            t = _input_table(value)
            if t is None:
                return
            ins[anchor] = t
        outs = {}
        for anchor, value in outputs.items():
            if not hasattr(value, 'schema'):
                return
            outs[anchor] = _output_table(value)
        entry = {'tool_id': tool.manifest.tool_id, 'tool_version': tool.manifest.version,
                 'config': _minimal(config, tool.manifest.config_schema), 'inputs': ins, 'outputs': outs,
                 'test': os.environ.get('PYTEST_CURRENT_TEST') or os.environ.get('FLOWENGINE_EXAMPLE_TEST', '')}
        line = json.dumps(entry, sort_keys=True, default=str)
        with _lock, open(log, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except Exception:
        return


_cache: dict | None = None


def examples_for(tool_id: str) -> list[dict]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(EXAMPLES_PATH.read_text(encoding='utf-8')).get('tools', {})
        except (OSError, ValueError):
            _cache = {}
    return _cache.get(tool_id, [])


def example_workflow(tool_id: str, example: dict) -> dict:
    """The example as a runnable workflow: one Text Input per input anchor feeding the tool."""
    nodes, edges = [], []
    for anchor, table in example['inputs'].items():
        nid = f'in_{anchor}'
        nodes.append({'id': nid, 'tool': 'core.text_input', 'config': {'fields': table['fields'], 'rows': table['rows']}})
        edges.append({'from': {'node': nid, 'anchor': 'output'}, 'to': {'node': 'tool', 'anchor': anchor}})
    nodes.append({'id': 'tool', 'tool': tool_id, 'config': example['config']})
    return {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': nodes, 'edges': edges}


def replay(tool_id: str, example: dict, base_dir) -> str | None:
    """Runs the example; returns None when the tool still produces the recorded output, else the difference."""
    from .executor import run_workflow
    try:
        result = run_workflow(example_workflow(tool_id, example), base_dir=base_dir)
    except Exception as e:
        return f'{type(e).__name__}: {e}'
    if result.status != 'succeeded':
        return f'run {result.status}: {(result.error or {}).get("message")}'
    produced = result.outputs.get('tool', {})
    for anchor, expected in example['expected_outputs'].items():
        table = produced.get(anchor)
        if table is None:
            return f'no output on anchor {anchor!r}'
        got = _output_table(table)
        if [(f['name'], f['type']) for f in got['fields']] != [(f['name'], f['type']) for f in expected['fields']]:
            return f'{anchor}: fields {got["fields"]} != {expected["fields"]}'
        if got['row_count'] != expected['row_count']:
            return f'{anchor}: {got["row_count"]} rows != {expected["row_count"]}'
        if example.get('check', 'values') == 'values' and not _same_rows(got['rows'], expected['rows']):
            return f'{anchor}: first rows differ'
    return None


def _same_cell(a, b) -> bool:
    if a == b:
        return True
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(x - y) <= 1e-9 + 1e-6 * max(abs(x), abs(y))


def _same_rows(got, expected) -> bool:
    """Equal rows, allowing last-digit differences in numbers between platforms' maths libraries."""
    return len(got) == len(expected) and all(len(g) == len(e) and all(_same_cell(a, b) for a, b in zip(g, e))
                                             for g, e in zip(got, expected))
