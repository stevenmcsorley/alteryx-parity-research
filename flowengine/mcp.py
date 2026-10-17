"""Model Context Protocol server over the Flowbench registry (stdio transport).

    python -m flowengine mcp --workspace DIR [--allow network,file_writes,processes,database_writes]

Serves the same agent operations as the desktop AI (flowengine/agent.py), plus ``validate_workflow``
and ``run_workflow`` for workflow files in the workspace, because an external client has no editor.
Permissions are granted for the session on the command line; anything not granted fails with a
structured ``permission-required`` error.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import __version__, agent
from .errors import EngineError

PROTOCOL_VERSION = '2025-06-18'
RUN_PERMISSIONS = {'network': 'allow_network', 'file_writes': 'allow_file_writes', 'processes': 'allow_processes',
                   'database_writes': 'allow_database_writes', 'remote_writes': 'allow_remote_writes'}


WORKFLOW_FORMAT = {
    'document': {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': '[node, ...]', 'edges': '[edge, ...]'},
    'node': {'id': 'unique text id', 'tool': 'tool id from find_tools, e.g. core.summarize', 'config': 'object matching describe_tool settings'},
    'edge': {'from': {'node': 'source node id', 'anchor': 'output anchor name'}, 'to': {'node': 'target node id', 'anchor': 'input anchor name'}},
    'paths': 'Relative paths in configs resolve against the folder that holds the workflow file.',
    'example': {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
        {'id': 'read', 'tool': 'core.input_data', 'config': {'path': 'data/sales.csv'}},
        {'id': 'types', 'tool': 'core.auto_field', 'config': {}},
        {'id': 'totals', 'tool': 'core.summarize', 'config': {'actions': [{'field': 'Region', 'op': 'group_by'}, {'field': 'Sales', 'op': 'sum', 'output': 'Total'}]}},
        {'id': 'save', 'tool': 'core.output_data', 'config': {'path': 'out/totals.csv', 'if_exists': 'overwrite'}}],
        'edges': [{'from': {'node': 'read', 'anchor': 'output'}, 'to': {'node': 'types', 'anchor': 'input'}},
                  {'from': {'node': 'types', 'anchor': 'output'}, 'to': {'node': 'totals', 'anchor': 'input'}},
                  {'from': {'node': 'totals', 'anchor': 'output'}, 'to': {'node': 'save', 'anchor': 'input'}}]},
}


class Server:
    def __init__(self, workspace: Path, permissions=()):
        from .service import EngineService
        self.workspace = Path(workspace).expanduser().resolve()
        self.permissions = set(permissions)
        self.service = EngineService()
        self.ops = dict(agent.OPS)
        for op in self._workflow_ops():
            self.ops[op.name] = op

    def _workflow_ops(self):
        path_schema = agent._obj({'path': {'type': 'string', 'minLength': 1}}, ['path'])
        return [
            agent.Op('workflow_format', 'How to write a Flowbench workflow file (.flow.json): document fields, node and edge shapes, '
                     'and a small example. Use describe_tool for each tool\'s config and anchors, write the file with '
                     'write_text_file, then validate_workflow and run_workflow.', agent._obj({}), lambda ctx: WORKFLOW_FORMAT),
            agent.Op('validate_workflow', 'Validate a Flowbench workflow file (.flow.json) and return its problems.',
                     path_schema, lambda ctx, path: self._validate(path)),
            agent.Op('run_workflow', 'Run a Flowbench workflow file (.flow.json) to completion. Returns run_id, status, '
                     'per-node row counts and errors; inspect outputs with query_results and run_evidence.',
                     agent._obj({'path': {'type': 'string', 'minLength': 1}, 'timeout_seconds': {'type': 'integer', 'minimum': 1}}, ['path']),
                     lambda ctx, path, timeout_seconds=3600: self._run(path, timeout_seconds), read_only=False),
        ]

    def _load(self, path):
        target = agent.Context(self.workspace).path(path)
        if not target.is_file():
            raise EngineError(f'{target} does not exist', code='not-found')
        return target, json.loads(target.read_text(encoding='utf-8'))

    def _validate(self, path):
        target, doc = self._load(path)
        return self.service.handle('workflow.validate', {'workflow': doc, 'base_dir': str(target.parent)})

    def _run(self, path, timeout_seconds):
        target, doc = self._load(path)
        params = {'workflow': doc, 'base_dir': str(target.parent)}
        params.update({flag: True for perm, flag in RUN_PERMISSIONS.items() if perm in self.permissions})
        started = self.service.handle('run.start', params)
        if started.get('status') == 'invalid':
            return started
        run_id = started['run_id']
        deadline = time.monotonic() + timeout_seconds
        while self.service.runs[run_id]['status'] == 'running':
            if time.monotonic() > deadline:
                self.service.runs[run_id]['token'].cancel()
                raise EngineError(f'The run exceeded {timeout_seconds} s and was cancelled', code='timeout')
            time.sleep(0.05)
        result = self.service.runs[run_id].get('result') or {}
        nodes = (result.get('manifest') or {}).get('nodes') or {}
        return {'run_id': run_id, 'status': self.service.runs[run_id]['status'], 'error': result.get('error'),
                'nodes': {k: {a: o.get('rows') for a, o in (v.get('outputs') or {}).items()} for k, v in nodes.items()}}

    # ------------------------------------------------------------------ protocol
    def tool_specs(self):
        out = []
        for op in self.ops.values():
            spec = op.spec()
            out.append({'name': spec['name'], 'description': spec['description'], 'inputSchema': spec['inputSchema'],
                        'annotations': spec['annotations'], '_meta': {'flowbench': spec['x-flowbench']}})
        return out

    def handle(self, message: dict):
        method, rid, params = message.get('method'), message.get('id'), message.get('params') or {}
        if rid is None:  # notifications such as notifications/initialized need no reply
            return None
        if method == 'initialize':
            return self._ok(rid, {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {'tools': {'listChanged': False}},
                                  'serverInfo': {'name': 'flowbench', 'version': __version__},
                                  'instructions': 'Flowbench does the computing. Discover tools with find_tools and describe_tool, '
                                                  'keep data in Flowbench files and runs, and report results with run_evidence.'})
        if method == 'ping':
            return self._ok(rid, {})
        if method == 'tools/list':
            return self._ok(rid, {'tools': self.tool_specs()})
        if method == 'tools/call':
            name = params.get('name')
            if name not in self.ops:
                return self._error(rid, -32602, f'Unknown tool {name!r}')
            ctx = agent.Context(self.workspace, self.permissions, self.service.runs, service=self.service)
            outcome = agent.call_envelope(name, params.get('arguments') or {}, ctx, self.ops)
            payload = outcome['result'] if outcome['ok'] else {'error': outcome['error']}
            return self._ok(rid, {'content': [{'type': 'text', 'text': json.dumps(payload, default=str)}],
                                  'structuredContent': payload, 'isError': not outcome['ok']})
        return self._error(rid, -32601, f'Method not found: {method}')

    @staticmethod
    def _ok(rid, result):
        return {'jsonrpc': '2.0', 'id': rid, 'result': result}

    @staticmethod
    def _error(rid, code, message):
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}}


def serve(workspace: Path, permissions=(), stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    server = Server(workspace, permissions)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = server.handle(json.loads(line))
        except json.JSONDecodeError:
            reply = Server._error(None, -32700, 'Parse error')
        if reply is not None:
            stdout.write(json.dumps(reply, default=str) + '\n')
            stdout.flush()
