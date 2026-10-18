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
# Results larger than this are kept in full under <workspace>/.flowbench/results and reach the client as a
# bounded view with a reference; read_tool_result pages through them (as in the desktop app).
MODEL_RESULT_MAX_BYTES = 24 * 1024
PREVIEW_CHARS = 1500


def view(value, depth=0, preview=PREVIEW_CHARS):
    if isinstance(value, str):
        return value if len(value) <= preview else {'text_preview': value[:preview], 'chars': len(value), 'truncated': True}
    if isinstance(value, list):
        if depth >= 4:
            return {'items': len(value)}
        first = [view(v, depth + 1, preview // 3) for v in value[:3]]
        return first if len(value) <= 3 else {'items': len(value), 'first': first, 'truncated': True}
    if isinstance(value, dict):
        if depth >= 5:
            return {'keys': list(value)[:40], 'key_count': len(value)}
        return {k: view(v, depth + 1, preview) for k, v in list(value.items())[:60]}
    return value


def pointer(doc, path: str):
    for part in [p.replace('~1', '/').replace('~0', '~') for p in path.split('/')[1:]] if path else []:
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return doc
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
        self.results_dir = self.workspace / '.flowbench' / 'results'
        self._result_count = 0

    def _workflow_ops(self):
        path_schema = agent._obj({'path': {'type': 'string', 'minLength': 1}}, ['path'])
        return [
            agent.Op('workflow_format', 'How to write a Flowbench workflow file (.flow.json): document fields, node and edge shapes, '
                     'and a small example. Use describe_tool for each tool\'s config and anchors, write the file with '
                     'write_text_file, then validate_workflow and run_workflow.', agent._obj({}), lambda ctx: WORKFLOW_FORMAT),
            agent.Op('read_tool_result', 'Read part of a result that was too large to send in full (see stored_result.ref): path is a JSON pointer, '
                     'offset and limit page through text (characters) or lists (items).',
                     agent._obj({'ref': {'type': 'string'}, 'path': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
                                 'limit': {'type': 'integer', 'minimum': 1}}, ['ref']),
                     lambda ctx, ref, path='', offset=0, limit=None: self._read_result(ref, path, offset, limit)),
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

    def _bound(self, name, payload):
        text = json.dumps(payload, default=str)
        if len(text) <= MODEL_RESULT_MAX_BYTES:
            return payload
        self._result_count += 1
        ref = f'res_{self._result_count:04d}_{name}'
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / f'{ref}.json').write_text(text, encoding='utf-8')
        preview = PREVIEW_CHARS
        shape = view(payload, 0, preview)
        while len(json.dumps(shape, default=str)) > MODEL_RESULT_MAX_BYTES and preview > 100:
            preview //= 3
            shape = view(payload, 0, preview)
        return {'stored_result': {'ref': ref, 'tool': name, 'bytes': len(text), 'estimated_tokens': len(text) // 4, 'view': shape,
                                  'note': 'Too large to send in full; Flowbench kept all of it. Read parts with read_tool_result.'}}

    def _read_result(self, ref, path, offset, limit):
        target = self.results_dir / f'{ref}.json'
        if not (ref.startswith('res_') and target.is_file()):
            raise EngineError('Unknown result ref', code='unknown-ref')
        try:
            value = pointer(json.loads(target.read_text(encoding='utf-8')), path)
        except (KeyError, IndexError, ValueError):
            raise EngineError(f'No value at {path!r}', code='bad-path') from None
        if isinstance(value, str):
            limit = min(limit or 8000, 16000)
            end = min(offset + limit, len(value))
            return {'kind': 'text', 'chars': len(value), 'offset': offset, 'text': value[offset:end], 'next_offset': end if end < len(value) else None}
        if isinstance(value, list):
            limit = min(limit or 20, 200)
            part = value[offset:offset + limit]
            while len(json.dumps(part, default=str)) > MODEL_RESULT_MAX_BYTES and len(part) > 1:
                part = part[:len(part) // 2]
            end = offset + len(part)
            return {'kind': 'array', 'items': len(value), 'offset': offset, 'values': part, 'next_offset': end if end < len(value) else None}
        return {'kind': 'value', 'value': view(value)}

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
            if name != 'read_tool_result':
                payload = self._bound(name, payload)
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
