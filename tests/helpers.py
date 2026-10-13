"""Shared test utilities for flowengine tests."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from flowengine.executor import run_workflow
from flowengine.table import Table
from flowengine.types import Field, FieldType

ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = dt.datetime(2026, 1, 15, 9, 30, 0)


def fixed_clock():
    return FIXED_NOW


def text_input(node_id, fields, rows):
    return {'id': node_id, 'tool': 'core.text_input',
            'config': {'fields': [dict(zip(('name', 'type'), f)) if isinstance(f, tuple) else f for f in fields],
                       'rows': rows}}


def edge(src, src_anchor, dst, dst_anchor):
    return {'from': {'node': src, 'anchor': src_anchor}, 'to': {'node': dst, 'anchor': dst_anchor}}


def workflow(nodes, edges, **extra):
    return {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': nodes, 'edges': edges, **extra}


def run_tool(tool, config, inputs: dict, base_dir=None, **kw):
    """Run one tool fed by Text Input nodes. ``inputs``: anchor -> (fields, rows) or list of those."""
    nodes, edges = [], []
    for anchor, spec in inputs.items():
        specs = spec if isinstance(spec, list) else [spec]
        for k, (fields, rows) in enumerate(specs):
            nid = f'in_{anchor}_{k}'
            nodes.append(text_input(nid, fields, rows))
            edges.append(edge(nid, 'output', 'subject', anchor))
    nodes.append({'id': 'subject', 'tool': tool, 'config': config})
    result = run_workflow(workflow(nodes, edges), base_dir=base_dir or ROOT, clock=fixed_clock, **kw)
    if not result.ok:
        raise AssertionError(f'run failed: {result.error}')
    return result.outputs['subject'], result


def run_tool_result(tool, config, inputs: dict, base_dir=None, **kw):
    nodes, edges = [], []
    for anchor, spec in inputs.items():
        specs = spec if isinstance(spec, list) else [spec]
        for k, (fields, rows) in enumerate(specs):
            nid = f'in_{anchor}_{k}'
            nodes.append(text_input(nid, fields, rows))
            edges.append(edge(nid, 'output', 'subject', anchor))
    nodes.append({'id': 'subject', 'tool': tool, 'config': config})
    return run_workflow(workflow(nodes, edges), base_dir=base_dir or ROOT, clock=fixed_clock, **kw)


def rows(table: Table):
    return [list(r) for r in table.rows()]


def names(table: Table):
    return table.schema.names
