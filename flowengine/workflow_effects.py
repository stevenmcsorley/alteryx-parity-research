"""Discover workflow side effects across embedded and file-backed macros.

This is a permission preflight, not an execution shortcut: it only identifies
effects so the desktop can ask the user. The run still receives explicit,
per-run permission flags and kernels enforce them at execution time.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .plugin import CancelToken, RunContext, default_registry

MACRO_TOOLS = {'flowengine.macro', 'flowengine.batch_macro', 'flowengine.iterative_macro'}
DB_WRITE_TOOLS = {'core.db_output', 'core.write_indb', 'core.data_stream_in'}
FILE_WRITE_TOOLS = {'core.blob_output'}
# Outbound requests that can carry record data; database tools keep their connection semantics.
NETWORK_TOOLS = {'core.download', 'core.github_last_commit', 'core.github_commit', 'core.email'}
# Changes made outside this computer (in addition to the network request itself).
REMOTE_WRITE_TOOLS = {'core.github_commit', 'core.email'}


def workflow_effects(document: dict, base_dir: str | Path, registry=None) -> dict:
    registry = registry or default_registry()
    from .tools.macros import WorkflowMacro
    import datetime as dt

    found = {'file_writes': [], 'database_writes': [], 'processes': [], 'network_requests': [], 'remote_writes': [], 'unresolved': []}
    visited: set[str] = set()

    def visit(doc, directory: Path, stack: tuple[str, ...], depth: int):
        if depth > 128:
            found['unresolved'].append({'path': list(stack), 'reason': 'macro nesting exceeds permission scan limit'})
            return
        for node in doc.get('nodes', []):
            node_id, tool_id = str(node.get('id', '?')), node.get('tool')
            path = (*stack, node_id)
            try:
                tool = registry.get(tool_id) if tool_id else None
            except Exception:
                # If the manifest cannot be resolved, its side effects are unknown.
                # Mark the scan incomplete so the desktop cannot authorize a run.
                found['unresolved'].append({'path': list(path), 'reason': 'tool effects could not be determined'})
                tool = None
            if tool:
                effect = tool.manifest.side_effects
                record = {'node_id': node_id, 'tool_id': tool_id, 'path': list(path)}
                # Blob Output is the file capability guarded by allow_file_writes.
                # Output Data has its established workflow destination semantics and
                # must not acquire a new confirmation just because its manifest is a sink.
                if tool_id in FILE_WRITE_TOOLS and effect == 'writes-files':
                    found['file_writes'].append(record)
                elif effect == 'process':
                    found['processes'].append(record)
                if tool_id in DB_WRITE_TOOLS:
                    found['database_writes'].append(record)
                if tool_id in NETWORK_TOOLS:
                    found['network_requests'].append(record)
                if tool_id in REMOTE_WRITE_TOOLS:
                    found['remote_writes'].append(record)
            if tool_id not in MACRO_TOOLS:
                continue
            cfg = node.get('config') or {}
            ctx = RunContext(node_id=node_id, base_dir=directory, clock=dt.datetime.now,
                             emit=lambda *args, **kwargs: None, cancel_token=CancelToken())
            try:
                child, child_dir = WorkflowMacro()._definition(ctx, cfg)
                if cfg.get('definition_path'):
                    identity = os.path.normcase(str((directory / cfg['definition_path']).resolve()))
                else:
                    raw = json.dumps(child, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                    identity = 'embedded:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()
                if identity in visited:
                    continue
                visited.add(identity)
                visit(child, child_dir, path, depth + 1)
            except Exception as exc:
                # An uninspected child must never cause the UI to grant a global
                # capability that could authorize a hidden writer.
                found['unresolved'].append({'path': list(path), 'reason': type(exc).__name__})

    visit(document, Path(base_dir).resolve(), (), 0)
    for key in ('file_writes', 'database_writes', 'processes', 'network_requests', 'remote_writes'):
        unique = {}
        for item in found[key]:
            unique[tuple(item['path'])] = item
        found[key] = list(unique.values())
    found['complete'] = not found['unresolved']
    return found
