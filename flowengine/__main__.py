"""Command line: validate / run workflows, list tools.

    python -m flowengine run WORKFLOW.json [--log run.jsonl] [--manifest run.json] [--clock 2026-01-01T00:00:00]
    python -m flowengine validate WORKFLOW.json
    python -m flowengine tools [--json]
    python -m flowengine serve            (JSON-RPC 2.0 over stdio for the editor)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path

from .errors import EngineError, WorkflowValidationError
from .executor import load_workflow, plan_workflow, run_file
from .plugin import default_registry


def _cmd_validate(args) -> int:
    path = args.workflow.resolve()
    doc = load_workflow(path)
    plan, problems = plan_workflow(doc, base_dir=path.parent)
    if problems:
        for p in problems:
            print(f"ERROR [{p.get('node_id') or '-'}] {p['code']}: {p['message']}", file=sys.stderr)
        return 2
    for (node, anchor), schema in sorted(plan.schemas.items()):
        print(f'{node}.{anchor}: ' + ', '.join(f"{f.name}:{f.type.value}" for f in schema))
    print(f'VALID: {len(plan.order)} nodes')
    return 0


def _cmd_run(args) -> int:
    clock = None
    if args.clock:
        fixed = _dt.datetime.fromisoformat(args.clock)
        clock = lambda: fixed
    try:
        result = run_file(args.workflow, log_path=args.log, clock=clock, retain='none')
    except WorkflowValidationError as e:
        for p in e.problems:
            print(f"ERROR [{p.get('node_id') or '-'}] {p['code']}: {p['message']}", file=sys.stderr)
        return 2
    for nid, stats in result.manifest['nodes'].items():
        outs = ', '.join(f"{a}={o['rows']}" for a, o in stats['outputs'].items()) or '(sink)'
        print(f'{nid:<24} {stats["tool"]:<26} {outs}')
    for w in result.warnings():
        print(f"WARNING [{w['node_id']}] {w['message']}", file=sys.stderr)
    if args.manifest:
        args.manifest.write_text(json.dumps(result.manifest, indent=2, default=str) + '\n', encoding='utf-8')
    if not result.ok:
        print(f"{result.status.upper()}: {result.error['code']}: [{result.error['node_id']}] {result.error['message']}",
              file=sys.stderr)
        return 1
    print(f'SUCCEEDED run {result.run_id}')
    return 0


def _cmd_tools(args) -> int:
    reg = default_registry()
    if args.json:
        print(json.dumps([t.manifest.to_dict() for t in reg.tools()], indent=2))
        return 0
    for t in reg.tools():
        m = t.manifest
        print(f'{m.tool_id:<26} v{m.version:<7} {",".join(m.catalog_ids):<34} '
              f'in={[a.name for a in m.inputs]} out={[a.name for a in m.outputs]}')
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='flowengine', description='Independent typed workflow engine')
    p.add_argument('-v', '--verbose', action='store_true')
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('workflow', type=Path)
    r.add_argument('--log', type=Path, help='Append structured JSONL run events to this file')
    r.add_argument('--manifest', type=Path, help='Write the reproducibility manifest (digests, counts)')
    r.add_argument('--clock', help='Fixed ISO datetime for tools that read the clock')
    v = sub.add_parser('validate')
    v.add_argument('workflow', type=Path)
    t = sub.add_parser('tools')
    t.add_argument('--json', action='store_true')
    sub.add_parser('serve', help='JSON-RPC 2.0 engine service over stdio (editor sidecar)')
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format='%(levelname)s %(message)s')
    try:
        if args.cmd == 'serve':
            from .service import serve
            serve()
            return 0
        return {'run': _cmd_run, 'validate': _cmd_validate, 'tools': _cmd_tools}[args.cmd](args)
    except EngineError as e:
        print(f'ERROR {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
