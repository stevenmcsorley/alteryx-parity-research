"""Drives Flowbench the way an AI orchestrator does: through the MCP server, with every call recorded.

A scripted agent (the acceptance tests) proves the whole path works without a model. Set
FLOWBENCH_ACCEPTANCE_MODEL=ollama:<model> (for example ollama:qwen2.5:7b) to also run the
scenarios with a real local model through LiveAgent; its outcomes are checked the same way.
"""
from __future__ import annotations

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flowengine.mcp import Server


class Session:
    def __init__(self, workspace: Path, permissions=('network', 'file_writes', 'processes')):
        self.workspace = Path(workspace)
        self.server = Server(self.workspace, permissions)
        self.transcript: list[dict] = []
        self._id = 0
        self._rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'acceptance', 'version': '1'}})

    def _rpc(self, method, params=None):
        self._id += 1
        return self.server.handle({'jsonrpc': '2.0', 'id': self._id, 'method': method, 'params': params or {}})

    def tools(self) -> list[dict]:
        return self._rpc('tools/list')['result']['tools']

    def call(self, name: str, **arguments):
        reply = self._rpc('tools/call', {'name': name, 'arguments': arguments})
        if 'error' in reply:
            raise AssertionError(f'{name}: protocol error {reply["error"]}')
        result = reply['result']
        self.transcript.append({'tool': name, 'arguments': arguments, 'ok': not result['isError'], 'result': result['structuredContent']})
        return result['structuredContent'], not result['isError']

    def ok(self, name: str, **arguments):
        payload, ok = self.call(name, **arguments)
        if not ok:
            raise AssertionError(f'{name} failed: {payload}')
        return payload

    def count(self, name: str, ok: bool | None = None) -> int:
        return sum(1 for t in self.transcript if t['tool'] == name and (ok is None or t['ok'] == ok))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class FileServer:
    """A local HTTP server for data the scenarios 'find online', so CI needs no internet."""

    def __init__(self, folder: Path):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(folder)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, name: str) -> str:
        return f'http://127.0.0.1:{self.httpd.server_address[1]}/{name}'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


SYSTEM = ('You are the orchestrator of Flowbench, an analytics engine. Flowbench does all the computing; you direct it with tools. '
          'Find tools with find_tools and read each one with describe_tool before use. Get data with download_file, profile_file and '
          'inspect_spreadsheet. Learn the file format with workflow_format, write the workflow with write_text_file, then '
          'validate_workflow and run_workflow. If a run fails, read the error, fix the file and run it again. Inspect results with '
          'query_results and cite run_evidence. Never calculate results yourself. Finish with a short summary of what Flowbench computed.')


def live_model() -> str | None:
    value = os.environ.get('FLOWBENCH_ACCEPTANCE_MODEL', '')
    return value.split(':', 1)[1] if value.startswith('ollama:') else None


class LiveAgent:
    """A real model operating Flowbench through the same session (Ollama's native chat API)."""

    def __init__(self, model: str, base_url: str = 'http://localhost:11434', context_tokens: int = 32768):
        self.model, self.base_url, self.context_tokens = model, base_url, context_tokens

    def run(self, session: Session, prompt: str, max_steps: int = 40) -> str:
        import requests
        tools = [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'], 'parameters': t['inputSchema']}}
                 for t in session.tools()]
        messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}]
        for _ in range(max_steps):
            reply = requests.post(f'{self.base_url}/api/chat', timeout=1800, json={
                'model': self.model, 'messages': messages, 'tools': tools, 'stream': False,
                'options': {'num_ctx': self.context_tokens}}).json()
            message = reply['message']
            messages.append(message)
            calls = message.get('tool_calls') or []
            if not calls:
                return message.get('content', '')
            for call in calls:
                fn = call['function']
                args = fn.get('arguments') or {}
                if isinstance(args, str):
                    args = json.loads(args or '{}')
                try:
                    payload, _ = session.call(fn['name'], **args)
                except (AssertionError, TypeError) as e:
                    payload = {'error': str(e)}
                messages.append({'role': 'tool', 'tool_name': fn['name'], 'content': json.dumps(payload, default=str)})
        return ''
