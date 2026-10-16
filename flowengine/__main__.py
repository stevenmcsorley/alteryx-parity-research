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
        hist = None
        if args.history:
            from .history import RunHistory
            hist = RunHistory(args.history)
        result = run_file(args.workflow, log_path=args.log, clock=clock, retain='none', profile=args.profile,
                          workers=args.workers, batch_rows=args.batch_rows, memory_budget=args.memory_budget,
                          history=hist, limits={'allow_processes': args.allow_processes,
                                                'allow_file_writes': args.allow_file_writes,
                                                'allow_network': args.allow_network})
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
    r.add_argument('--profile', help='Compatibility profile (default ayx-2026.2-e1)')
    r.add_argument('--workers', type=int, default=1, help='Concurrent nodes (outputs identical for any value)')
    r.add_argument('--batch-rows', type=int, help='Enable streaming of eligible segments in batches of N rows')
    r.add_argument('--memory-budget', type=int, help='Bytes of intermediate tables kept in memory before spilling')
    r.add_argument('--history', type=Path, help='Record the run in this SQLite run-history database')
    r.add_argument('--allow-processes', action='store_true',
                   help='Allow Python/R/Run Command tools to start processes for this run')
    r.add_argument('--allow-file-writes', action='store_true',
                   help='Allow Blob Output file writes for this run')
    r.add_argument('--allow-network', action='store_true',
                   help='Allow Download to send HTTP(S) requests for this run')
    v = sub.add_parser('validate')
    v.add_argument('workflow', type=Path)
    t = sub.add_parser('tools')
    t.add_argument('--json', action='store_true')
    sv = sub.add_parser('serve', help='JSON-RPC 2.0 engine service (stdio sidecar, or --http for the browser build)')
    sv.add_argument('--http', type=int, metavar='PORT', help='Serve over loopback HTTP + SSE instead of stdio')
    sv.add_argument('--history', type=Path, help='SQLite run-history database')
    im = sub.add_parser('import', help='Convert a .yxmd/.yxmc/.yxwz/.yxzp document to a flowengine workflow')
    im.add_argument('source', type=Path)
    im.add_argument('-o', '--output', type=Path, required=True)
    ex = sub.add_parser('export', help='Write a flowengine workflow as .yxmd (supported tools only)')
    ex.add_argument('workflow', type=Path)
    ex.add_argument('-o', '--output', type=Path, required=True)
    en = sub.add_parser('env', help='Manage isolated Python environments for the Python tool')
    en_sub = en.add_subparsers(dest='env_cmd', required=True)
    ec = en_sub.add_parser('create')
    ec.add_argument('name')
    ec.add_argument('-r', '--requirements', type=Path, help='Pinned requirements file')
    ec.add_argument('--python', help='Interpreter used to create the environment (default: this one)')
    en_sub.add_parser('list')
    en_sub.add_parser('rscript', help='Show the R installation the R tool would use')
    h = sub.add_parser('history', help='List or show recorded runs')
    h.add_argument('--db', type=Path, required=True)
    h.add_argument('run_id', nargs='?')
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format='%(levelname)s %(message)s')
    try:
        if args.cmd == 'serve':
            from .service import serve, serve_http
            if args.http is not None:
                serve_http(port=args.http, history_path=args.history)
            else:
                serve(history_path=args.history)
            return 0
        if args.cmd == 'import':
            from .interop.yxmd import import_file
            doc, report = import_file(args.source)
            args.output.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            print(json.dumps(report['counts']))
            for n in report['nodes']:
                if n['status'] != 'mapped':
                    print(f"{n['status']:<12} {n['node']:<8} {n['source']}  {n.get('reason') or '; '.join(n.get('warnings', []))}")
            return 0
        if args.cmd == 'export':
            from .interop.yxmd import export_document
            text, problems = export_document(load_workflow(args.workflow))
            for p in problems:
                print(f'NOT EXPORTED {p}', file=sys.stderr)
            if problems:
                return 2
            args.output.write_text(text, encoding='utf-8')
            print(f'wrote {args.output}')
            return 0
        if args.cmd == 'env':
            from . import runtimes
            if args.env_cmd == 'create':
                reqs = [l.strip() for l in args.requirements.read_text(encoding='utf-8').splitlines()
                        if l.strip() and not l.startswith('#')] if args.requirements else []
                m = runtimes.create_env(args.name, reqs, args.python)
                print(f"created {args.name}: Python {m['python']}, {len(m['lock'])} locked packages")
            elif args.env_cmd == 'list':
                for e in runtimes.list_envs():
                    print(f"{e['name']:<20} Python {e['python']:<8} {e['path']}")
            else:
                print(runtimes.find_rscript() or 'No R installation found (install R from CRAN or set FLOWENGINE_RSCRIPT)')
            return 0
        if args.cmd == 'history':
            from .history import RunHistory
            hist = RunHistory(args.db)
            if args.run_id:
                print(json.dumps(hist.get(args.run_id), indent=2, default=str))
            else:
                for r in hist.list():
                    print(f"{r['started_utc']}  {r['status']:<10} {r['profile']:<15} {r['run_id']}  {r['workflow_name'] or ''}")
            return 0
        return {'run': _cmd_run, 'validate': _cmd_validate, 'tools': _cmd_tools}[args.cmd](args)
    except EngineError as e:
        print(f'ERROR {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
