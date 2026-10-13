"""Editor-facing engine service: JSON-RPC 2.0 over newline-delimited stdio.

The desktop editor (docs/ARCHITECTURE.md, "Visual editor") runs this process as
a sidecar and never imports engine internals. Methods:

  engine.info                      -> version, protocol
  tools.list                       -> tool manifests (palette + property-panel schemas)
  catalog.list                     -> every catalogue entry with status and kernel id
  workflow.validate {workflow, base_dir} -> problems + statically inferred schemas per anchor
  workflow.run {workflow, base_dir, preview_rows?, clock?} -> status, error, manifest, events,
                                      previews per node/anchor (first N rows), browse previews

All errors are JSON-RPC errors with ``data.code`` set to the engine error code.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

from . import __version__, catalog
from .errors import EngineError, WorkflowValidationError
from .executor import plan_workflow, run_workflow
from .plugin import default_registry
from .table import Table
from .types import format_value

PROTOCOL = 1


def _preview(t: Table, n: int) -> dict:
    return {'fields': t.schema.to_list(), 'total_rows': len(t),
            'rows': [[None if v is None else format_value(v) for v in r] for _, r in zip(range(n), t.rows())]}


def _base(params) -> Path:
    base = params.get('base_dir')
    if not base:
        raise EngineError('base_dir is required (directory that relative paths resolve against)', code='bad-request')
    return Path(base)


def handle(method: str, params: dict):
    reg = default_registry()
    if method == 'engine.info':
        return {'engine': 'flowengine', 'version': __version__, 'protocol': PROTOCOL}
    if method == 'tools.list':
        return [t.manifest.to_dict() for t in reg.tools()]
    if method == 'catalog.list':
        cmap = reg.catalog_map()
        return [{'id': e['id'], 'name': e['name'], 'category': e['category'],
                 'implementation_status': e['implementation_status'],
                 'verification_status': e['verification_status'], 'kernel': cmap.get(e['id']),
                 'documentation_url': e.get('documentation_url')} for e in catalog.entries()]
    if method == 'workflow.validate':
        plan, problems = plan_workflow(params['workflow'], reg, base_dir=_base(params))
        schemas = {}
        if plan:
            for (node, anchor), s in sorted(plan.schemas.items()):
                schemas.setdefault(node, {})[anchor] = s.to_list()
        return {'valid': not problems, 'problems': problems, 'schemas': schemas}
    if method == 'workflow.run':
        n = int(params.get('preview_rows', 100))
        clock = None
        if params.get('clock'):
            fixed = _dt.datetime.fromisoformat(params['clock'])
            clock = lambda: fixed  # noqa: E731
        try:
            r = run_workflow(params['workflow'], base_dir=_base(params), registry=reg, clock=clock)
        except WorkflowValidationError as e:
            return {'status': 'invalid', 'problems': e.problems}
        return {'status': r.status, 'error': r.error, 'manifest': r.manifest, 'events': r.events,
                'previews': {node: {a: _preview(t, n) for a, t in outs.items()} for node, outs in r.outputs.items()},
                'browse': {node: _preview(t, n) for node, t in r.browse.items()}}
    raise KeyError(method)


def respond(line: str) -> str | None:
    try:
        req = json.loads(line)
    except json.JSONDecodeError as e:
        return json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': f'Parse error: {e}'}})
    rid = req.get('id')
    try:
        if req.get('jsonrpc') != '2.0' or not isinstance(req.get('method'), str):
            raise ValueError('Invalid request')
        result = handle(req['method'], req.get('params') or {})
        resp = {'jsonrpc': '2.0', 'id': rid, 'result': result}
    except KeyError as e:
        resp = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': f'Method not found: {e}'}}
    except ValueError as e:
        resp = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32600, 'message': str(e)}}
    except EngineError as e:
        resp = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': e.message, 'data': e.to_dict()}}
    if rid is None and 'error' not in resp:
        return None  # notification
    return json.dumps(resp, ensure_ascii=False, default=str)


def serve(stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        if not line.strip():
            continue
        out = respond(line)
        if out is not None:
            stdout.write(out + '\n')
            stdout.flush()
