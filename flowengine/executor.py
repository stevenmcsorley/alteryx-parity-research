"""Workflow validation, planning and execution.

Execution model (ADR-003): a validated DAG is executed node-by-node in a
deterministic topological order (ties broken by document order). Each node
receives fully materialised input tables. Tables are released as soon as their
last consumer has run unless retention is requested. Failure is fail-fast: the
failing node is reported with its id and error code; downstream nodes are
reported as skipped. Every state change is a structured :class:`RunEvent`.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import platform
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import jsonschema

from . import __version__
from .errors import (CancelledError, ConfigError, EngineError, SchemaError, ToolUnavailableError,
                     WorkflowValidationError, DataError, ExpressionError)
from .plugin import CancelToken, Registry, RunContext, default_registry
from .table import Table

log = logging.getLogger('flowengine')
WORKFLOW_SCHEMA_PATH = Path(__file__).resolve().parents[1] / 'schemas' / 'workflow.schema.json'


def _workflow_validator():
    schema = json.loads(WORKFLOW_SCHEMA_PATH.read_text(encoding='utf-8'))
    return jsonschema.Draft202012Validator(schema)


def load_workflow(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise WorkflowValidationError(f'{path}: not valid JSON ({e})') from None
    return doc


def save_workflow(doc: dict, path: Path) -> None:
    problems = list(_schema_problems(doc))
    if problems:
        raise WorkflowValidationError('Refusing to save an invalid workflow document', problems=problems)
    Path(path).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def workflow_digest(doc: dict) -> str:
    """Digest of the semantic content (layout/labels/annotations excluded)."""
    sem = {'nodes': [{k: n.get(k) for k in ('id', 'tool', 'toolVersion', 'config', 'disabled')} for n in doc.get('nodes', [])],
           'edges': doc.get('edges', []), 'constants': doc.get('constants', {})}
    return hashlib.sha256(json.dumps(sem, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _schema_problems(doc):
    if not isinstance(doc, dict):
        yield {'code': 'document-invalid', 'message': 'Workflow document must be a JSON object', 'node_id': None}
        return
    for e in sorted(_workflow_validator().iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))):
        yield {'code': 'document-invalid', 'message': f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}",
               'node_id': None}


# =========================================================================== planning

@dataclass
class PlannedNode:
    id: str
    tool: Any
    config: dict
    disabled: bool
    # anchor -> list of (upstream node, upstream anchor) in connection order
    inputs: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


@dataclass
class Plan:
    order: list[str]
    nodes: dict[str, PlannedNode]
    consumers: dict[tuple[str, str], int]  # (node, anchor) -> number of consuming edges
    skipped: set[str]
    schemas: dict[tuple[str, str], Any]


def plan_workflow(doc: dict, registry: Registry | None = None, *, base_dir: Path | None = None,
                  infer_schemas: bool = True) -> tuple[Plan | None, list[dict]]:
    """Validate a document and build an execution plan. Returns (plan, problems)."""
    registry = registry or default_registry()
    problems = list(_schema_problems(doc))
    if problems:
        return None, problems

    def problem(code, message, node_id=None, **extra):
        problems.append({'code': code, 'message': message, 'node_id': node_id, **extra})

    nodes: dict[str, PlannedNode] = {}
    order_index = {}
    for i, n in enumerate(doc['nodes']):
        nid = n['id']
        if nid in nodes or nid in order_index:
            problem('duplicate-node-id', f'Node id {nid!r} is used more than once', nid)
            continue
        order_index[nid] = i
        try:
            tool = registry.get(n['tool'])
        except ToolUnavailableError as e:
            problem(e.code, e.message, nid)
            continue
        if n.get('toolVersion') and n['toolVersion'].split('.')[0] != tool.manifest.version.split('.')[0]:
            problem('tool-version-mismatch', f"Node requires {n['tool']} v{n['toolVersion']}; installed "
                    f"v{tool.manifest.version} is not major-compatible", nid)
            continue
        try:
            cfg = tool.normalize_config(n['config'])
        except (ConfigError, ExpressionError) as e:
            problem(e.code, e.message, nid)
            continue
        nodes[nid] = PlannedNode(nid, tool, cfg, bool(n.get('disabled')))

    succ = defaultdict(list)
    indeg = {nid: 0 for nid in nodes}
    consumers: dict[tuple[str, str], int] = defaultdict(int)
    known_ids = set(order_index)
    for k, e in enumerate(doc['edges']):
        src, dst = e['from'], e['to']
        if src['node'] not in known_ids or dst['node'] not in known_ids:
            problem('edge-unknown-node', f"Edge {k} references unknown node "
                    f"{src['node'] if src['node'] not in known_ids else dst['node']!r}")
            continue
        if src['node'] not in nodes or dst['node'] not in nodes:
            continue  # node already reported
        a_out = nodes[src['node']].tool.manifest.output(src['anchor'])
        a_in = nodes[dst['node']].tool.manifest.input(dst['anchor'])
        if a_out is None:
            problem('edge-unknown-anchor', f"Node {src['node']!r} has no output anchor {src['anchor']!r} "
                    f"(has {[a.name for a in nodes[src['node']].tool.manifest.outputs]})", src['node'])
            continue
        if a_in is None:
            problem('edge-unknown-anchor', f"Node {dst['node']!r} has no input anchor {dst['anchor']!r} "
                    f"(has {[a.name for a in nodes[dst['node']].tool.manifest.inputs]})", dst['node'])
            continue
        slot = nodes[dst['node']].inputs.setdefault(dst['anchor'], [])
        if slot and not a_in.multiple:
            problem('anchor-overconnected', f"Input anchor {dst['anchor']!r} accepts one connection", dst['node'])
            continue
        if src['node'] == dst['node']:
            problem('cycle', 'Node is connected to itself', src['node'])
            continue
        slot.append((src['node'], src['anchor']))
        consumers[(src['node'], src['anchor'])] += 1
        succ[src['node']].append(dst['node'])
        indeg[dst['node']] += 1

    for nid, pn in nodes.items():
        for anchor in pn.tool.manifest.inputs:
            if anchor.required and not pn.inputs.get(anchor.name):
                problem('anchor-unconnected', f'Required input anchor {anchor.name!r} is not connected', nid)

    # Kahn topological sort; ties broken by document order for determinism
    import heapq
    ready = [(order_index[n], n) for n, d in indeg.items() if d == 0]
    heapq.heapify(ready)
    order = []
    deg = dict(indeg)
    while ready:
        _, nid = heapq.heappop(ready)
        order.append(nid)
        for nxt in succ[nid]:
            deg[nxt] -= 1
            if deg[nxt] == 0:
                heapq.heappush(ready, (order_index[nxt], nxt))
    if len(order) != len(nodes):
        cyc = sorted(n for n in nodes if n not in order)
        problem('cycle', f'Cycle detected among nodes {cyc}')

    # disabled nodes and everything downstream of them are skipped
    skipped = set()
    for nid in order:
        pn = nodes[nid]
        if pn.disabled or any(up in skipped for ups in pn.inputs.values() for up, _ in ups):
            skipped.add(nid)

    plan = Plan(order, nodes, dict(consumers), skipped, {})
    if problems:
        return plan, problems
    if infer_schemas:
        _infer(plan, base_dir or Path.cwd(), problems)
    return plan, problems


def _infer(plan: Plan, base_dir: Path, problems: list):
    """Static schema propagation; stops at nodes whose schema needs runtime data."""
    events = []
    for nid in plan.order:
        if nid in plan.skipped:
            continue
        pn = plan.nodes[nid]
        ins = {}
        unknown = False
        for anchor, ups in pn.inputs.items():
            schemas = [plan.schemas.get(u) for u in ups]
            if any(s is None for s in schemas):
                unknown = True
                break
            ins[anchor] = schemas if pn.tool.manifest.input(anchor).multiple else schemas[0]
        if unknown:
            continue
        ctx = RunContext(nid, base_dir, _dt.datetime.now, lambda *a, **k: events.append((a, k)), CancelToken(),
                         dry_run=True)
        try:
            out = pn.tool.infer(ctx, pn.config, ins)
        except (SchemaError, ConfigError, ExpressionError) as e:
            problems.append({'code': e.code, 'message': e.message, 'node_id': nid})
            continue
        except DataError:
            continue  # data-dependent; checked at run time
        if out:
            for anchor, schema in out.items():
                plan.schemas[(nid, anchor)] = schema


def validate_workflow(doc: dict, registry: Registry | None = None, *, base_dir: Path | None = None) -> list[dict]:
    return plan_workflow(doc, registry, base_dir=base_dir)[1]


# =========================================================================== running

@dataclass
class RunResult:
    run_id: str
    status: str
    events: list[dict]
    outputs: dict[str, dict[str, Table]]
    browse: dict[str, Table]
    error: dict | None
    manifest: dict

    @property
    def ok(self) -> bool:
        return self.status == 'succeeded'

    def output(self, node_id: str, anchor: str = 'output') -> Table:
        return self.outputs[node_id][anchor]

    def warnings(self) -> list[dict]:
        return [e for e in self.events if e['type'] == 'warning']


class _EventLog:
    def __init__(self, run_id: str, log_path: Path | None, now: Callable[[], float], listener=None):
        self.run_id = run_id
        self.events: list[dict] = []
        self._fh = open(log_path, 'a', encoding='utf-8') if log_path else None
        self._now = now
        self._listener = listener

    def emit(self, type_: str, node_id: str | None = None, **data):
        ev = {'seq': len(self.events), 'run_id': self.run_id,
              'ts': _dt.datetime.fromtimestamp(self._now(), _dt.timezone.utc).isoformat(timespec='milliseconds'),
              'type': type_, 'node_id': node_id, **data}
        self.events.append(ev)
        level = logging.WARNING if type_ in ('warning',) else logging.ERROR if type_.endswith('failed') else logging.INFO
        log.log(level, '%s %s %s', type_, node_id or '-', data.get('message', ''))
        if self._fh:
            self._fh.write(json.dumps(ev, ensure_ascii=False, default=str) + '\n')
            self._fh.flush()
        if self._listener:
            self._listener(ev)

    def close(self):
        if self._fh:
            self._fh.close()


def run_workflow(doc: dict, *, base_dir: Path, registry: Registry | None = None,
                 clock: Callable[[], _dt.datetime] | None = None, retain: str = 'all',
                 log_path: Path | None = None, cancel_token: CancelToken | None = None,
                 limits: dict | None = None, run_id: str | None = None,
                 event_time: Callable[[], float] = time.time, listener=None) -> RunResult:
    """Validate then execute ``doc``. Raises WorkflowValidationError if invalid.

    ``retain``: 'all' keeps every output table, 'none' keeps only Browse captures.
    ``clock``: source of "now" for tools (inject a fixed clock for reproducible runs).
    """
    if retain not in ('all', 'none'):
        raise ValueError("retain must be 'all' or 'none'")
    registry = registry or default_registry()
    base_dir = Path(base_dir).resolve()
    plan, problems = plan_workflow(doc, registry, base_dir=base_dir)
    if problems:
        raise WorkflowValidationError(f'Workflow has {len(problems)} problem(s): '
                                      + '; '.join(f"[{p.get('node_id') or '-'}] {p['message']}" for p in problems[:5]),
                                      problems=problems)
    run_id = run_id or uuid.uuid4().hex
    clock = clock or _dt.datetime.now
    cancel_token = cancel_token or CancelToken()
    elog = _EventLog(run_id, log_path, event_time, listener)
    tables: dict[tuple[str, str], Table] = {}
    remaining = dict(plan.consumers)
    retained: dict[str, dict[str, Table]] = {}
    browse: dict[str, Table] = {}
    node_stats: dict[str, dict] = {}
    status, error = 'succeeded', None
    elog.emit('run-started', engine_version=__version__, workflow_digest=workflow_digest(doc),
              node_count=len(plan.order))
    try:
        for idx, nid in enumerate(plan.order):
            pn = plan.nodes[nid]
            if nid in plan.skipped:
                elog.emit('node-skipped', nid, message='disabled or downstream of a disabled node')
                continue
            if cancel_token.cancelled:
                raise CancelledError('Run cancelled before node started', node_id=nid)
            inputs = {}
            for anchor, ups in pn.inputs.items():
                tabs = [tables[u] for u in ups]
                inputs[anchor] = tabs if pn.tool.manifest.input(anchor).multiple else tabs[0]
            ctx = RunContext(nid, base_dir, clock, lambda t, **d: elog.emit(t, nid, **d), cancel_token,
                             limits=dict(limits or {}), browse_sink=lambda n, t: browse.__setitem__(n, t),
                             constants=dict(doc.get('constants', {})))
            in_rows = {a: (sum(len(t) for t in v) if isinstance(v, list) else len(v)) for a, v in inputs.items()}
            elog.emit('node-started', nid, tool=pn.tool.manifest.tool_id, tool_version=pn.tool.manifest.version,
                      input_rows=in_rows)
            started = time.perf_counter()
            try:
                out = pn.tool.run(ctx, pn.config, inputs)
            except EngineError as e:
                e.node_id = e.node_id or nid
                raise
            except Exception as e:  # kernel bug: surface clearly, never swallow
                raise EngineError(f'Internal error in {pn.tool.manifest.tool_id}: {type(e).__name__}: {e}',
                                  node_id=nid, code='internal-error') from e
            declared = {a.name for a in pn.tool.manifest.outputs}
            if set(out) != declared or not all(isinstance(t, Table) for t in out.values()):
                raise EngineError(f'Kernel returned anchors {sorted(out)}; manifest declares {sorted(declared)}',
                                  node_id=nid, code='internal-error')
            for anchor, t in out.items():
                expected = plan.schemas.get((nid, anchor))
                if expected is not None and tuple(expected) != tuple(t.schema):
                    raise EngineError(f'Output {anchor!r} schema differs from statically inferred schema',
                                      node_id=nid, code='internal-error')
                if remaining.get((nid, anchor)):
                    tables[(nid, anchor)] = t
            duration = time.perf_counter() - started
            node_stats[nid] = {'tool': pn.tool.manifest.tool_id, 'tool_version': pn.tool.manifest.version,
                               'outputs': {a: {'rows': len(t), 'fields': t.schema.to_list(),
                                               'digest': t.canonical_digest()} for a, t in sorted(out.items())}}
            elog.emit('node-completed', nid, duration_ms=round(duration * 1000, 3),
                      output_rows={a: len(t) for a, t in out.items()})
            if retain == 'all':
                retained[nid] = out
            for anchor, ups in pn.inputs.items():
                for u in ups:
                    remaining[u] -= 1
                    if remaining[u] == 0:
                        tables.pop(u, None)
    except CancelledError as e:
        status, error = 'cancelled', e.to_dict()
        elog.emit('run-cancelled', e.node_id, message=e.message)
    except EngineError as e:
        status, error = 'failed', e.to_dict()
        elog.emit('node-failed', e.node_id, code=e.code, message=e.message)
        failed_at = plan.order.index(e.node_id) if e.node_id in plan.order else len(plan.order)
        for later in plan.order[failed_at + 1:]:
            elog.emit('node-skipped', later, message='not run because an upstream node failed')
    finally:
        warnings = sum(1 for ev in elog.events if ev['type'] == 'warning')
        elog.emit('run-completed', status=status, warnings=warnings)
        elog.close()
    manifest = {
        'run_id': run_id, 'status': status, 'engine': 'flowengine', 'engine_version': __version__,
        'python': platform.python_version(), 'workflow_digest': workflow_digest(doc),
        'nodes': node_stats, 'error': error,
        'warnings': [{k: ev[k] for k in ('node_id', 'message')} for ev in elog.events if ev['type'] == 'warning'],
    }
    return RunResult(run_id, status, elog.events, retained, browse, error, manifest)


def run_file(path: Path, **kw) -> RunResult:
    path = Path(path).resolve()
    return run_workflow(load_workflow(path), base_dir=path.parent, **kw)
