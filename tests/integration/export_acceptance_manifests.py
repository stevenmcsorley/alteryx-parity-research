#!/usr/bin/env python3
"""Export only credential-safe metrics for the manual desktop acceptance runs.

Uses Python's standard library only. Do not use this to export the full history database:
it contains unrelated workflows and is intentionally not copied to the evidence bundle.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path


def safe_manifest(manifest: dict, events: list[dict]) -> dict:
    nodes = {}
    for node_id, node in manifest.get('nodes', {}).items():
        nodes[node_id] = {
            'tool': node.get('tool'),
            'duration_ms': node.get('duration_ms'),
            'pushed_to_database': bool(node.get('pushed_to_database', False)),
            'outputs': {
                anchor: {
                    'rows': output.get('rows'),
                    'fields': [{'name': field.get('name'), 'type': field.get('type')}
                               for field in output.get('fields', [])],
                    'digest': output.get('digest'),
                }
                for anchor, output in node.get('outputs', {}).items()
            },
        }
    query_summaries = []
    for query in manifest.get('database_queries', []):
        query_summaries.append({key: query.get(key) for key in (
            'dialect', 'nodes', 'duration_ms', 'rows', 'transferred_bytes_estimate', 'digest', 'status'
        )})
    event_counts = Counter(event.get('type', 'unknown') for event in events)
    return {
        'run_id': manifest.get('run_id'),
        'status': manifest.get('status'),
        'engine': manifest.get('engine'),
        'engine_version': manifest.get('engine_version'),
        'workflow_digest': manifest.get('workflow_digest'),
        'profile': (manifest.get('profile') or {}).get('name'),
        'nodes': nodes,
        'database_queries': query_summaries,
        'stats': {
            key: (manifest.get('stats') or {}).get(key)
            for key in ('wall_ms', 'workers', 'spills', 'spilled_bytes_estimate', 'peak_resident_bytes_estimate')
        },
        'event_counts': dict(sorted(event_counts.items())),
        'pushdown_fallback_nodes': sorted({event.get('node_id') for event in events
                                           if event.get('type') == 'pushdown-fallback' and event.get('node_id')}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', required=True, type=Path, help='Flowbench app-data history.sqlite')
    parser.add_argument('--workflow-name', default='M21 Customer Order Manual Acceptance')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--limit', type=int, default=5)
    args = parser.parse_args()
    if not args.history.is_file():
        parser.error(f'history database not found: {args.history}')
    if not 2 <= args.limit <= 20:
        parser.error('--limit must be between 2 and 20')

    history_uri = args.history.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(history_uri, uri=True)) as db:
        rows = db.execute(
            'SELECT run_id, started_utc, status, manifest FROM runs WHERE workflow_name = ? '
            'ORDER BY started_utc DESC LIMIT ?', (args.workflow_name, args.limit)
        ).fetchall()
        if not rows:
            parser.error(f'no recorded runs named {args.workflow_name!r}')
        exported = []
        for run_id, started, status, manifest_json in reversed(rows):
            events = [json.loads(row[0]) for row in db.execute(
                'SELECT body FROM events WHERE run_id = ? ORDER BY seq', (run_id,)
            )]
            manifest = json.loads(manifest_json)
            exported.append({'started_utc': started, 'status': status,
                             'manifest': safe_manifest(manifest, events)})

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'format': 'flowbench.acceptance-manifest-summary',
                                       'version': 1, 'runs': exported}, indent=2), encoding='utf-8')
    print(f'Wrote {len(exported)} credential-filtered run manifest(s) to {args.output}')
    print('SQL text, parameters, workflows, connection settings, and event messages were omitted.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
