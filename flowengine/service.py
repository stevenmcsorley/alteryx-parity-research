"""Editor-facing engine service: JSON-RPC 2.0.

Transports:
* stdio (newline-delimited) - used by the Tauri desktop shell as a sidecar process;
* HTTP (``POST /rpc``) + Server-Sent Events (``GET /events``) - used by the browser
  development build and the automated UI tests. Both share :class:`EngineService`.

Requests: engine.info, tools.list, catalog.list, profiles.list,
workflow.validate {workflow, base_dir, profile?}, workflow.run {...} (synchronous),
run.start {workflow, base_dir, profile?, workers?, batch_rows?, memory_budget?, preview_rows?, clock?} -> {run_id},
run.cancel {run_id}, run.result {run_id}, history.list {limit?}, history.get {run_id},
file.write_text {path, text} / file.read_text {path} (workflow documents only, *.flow.json),
file.write_export {path, text} (.csv/.tsv/.json/.txt only).

Notifications (server -> client): ``run.event`` (every engine event, live) and
``run.finished`` {run_id, status}. All errors are JSON-RPC errors with ``data.code``.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import threading
from pathlib import Path

from . import __version__, catalog
from .errors import EngineError, WorkflowValidationError
from .executor import plan_workflow, run_workflow
from .plugin import CancelToken, default_registry
from .profiles import PROFILES
from .table import Table
from .types import format_value

PROTOCOL = 2


def _preview(t: Table, n: int, double_text='g15') -> dict:
    return {'fields': t.schema.to_list(), 'total_rows': len(t),
            'rows': [[None if v is None else format_value(v, double_text) for v in r]
                     for _, r in zip(range(n), t.rows())]}


class EngineService:
    def __init__(self, notify=None, history_path: Path | None = None):
        import atexit
        import shutil
        import uuid
        self.notify = notify or (lambda method, params: None)
        self.runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        from .runtimes import home
        self._artifact_root = home() / 'results' / uuid.uuid4().hex
        atexit.register(shutil.rmtree, self._artifact_root, ignore_errors=True)
        self.history = None
        if history_path:
            from .history import RunHistory
            self.history = RunHistory(history_path)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _base(params) -> Path:
        base = params.get('base_dir')
        if not base:
            raise EngineError('base_dir is required (directory that relative paths resolve against)', code='bad-request')
        return Path(base)

    @staticmethod
    def _clock(params):
        if params.get('clock'):
            fixed = _dt.datetime.fromisoformat(params['clock'])
            return lambda: fixed
        return None

    def _result_payload(self, r, n):
        dt = r.manifest['profile']['double_text']
        return {'run_id': r.run_id, 'status': r.status, 'error': r.error, 'manifest': r.manifest, 'events': r.events,
                'previews': {node: {a: _preview(t, n, dt) for a, t in outs.items()} for node, outs in r.outputs.items()},
                'browse': {node: _preview(t, n, dt) for node, t in r.browse.items()}}

    def _run_kwargs(self, params):
        return dict(base_dir=self._base(params), clock=self._clock(params), profile=params.get('profile'),
                    limits={'allow_processes': bool(params.get('allow_processes')),
                            'allow_database_writes': bool(params.get('allow_database_writes')),
                            'allow_file_writes': bool(params.get('allow_file_writes'))},
                    workers=int(params.get('workers', 1)), batch_rows=params.get('batch_rows'),
                    memory_budget=params.get('memory_budget'), retain='preview', history=self.history)

    # ------------------------------------------------------------------ dispatch
    def handle(self, method: str, params: dict):
        reg = default_registry()
        if method == 'engine.info':
            return {'engine': 'flowengine', 'version': __version__, 'protocol': PROTOCOL,
                    'python': sys.version.split()[0], 'history': bool(self.history), 'cwd': os.getcwd()}
        if method == 'tools.list':
            return [t.manifest.to_dict() for t in reg.tools()]
        if method == 'profiles.list':
            return [p.to_dict() for p in PROFILES.values()]
        if method.startswith('database.'):
            return self._database(method, params)
        if method == 'catalog.list':
            cmap = reg.catalog_map()
            status = _load_status()
            return [{'id': e['id'], 'name': e['name'], 'category': e['category'],
                     'implementation_status': e['implementation_status'],
                     'verification_status': e['verification_status'], 'kernel': cmap.get(e['id']),
                     'documentation_url': e.get('documentation_url'),
                     'external_prerequisites_blocking': status.get(e['id'], {}).get('external_prerequisites_blocking', False),
                     'unavailable_reason': None if cmap.get(e['id']) else _unavailable_reason(e, status.get(e['id'], {}))}
                    for e in catalog.entries()]
        if method == 'workflow.validate':
            from .interface_app import bind_application
            from .profiles import get_profile
            document = bind_application(params['workflow'], params.get('application_values'))
            plan, problems = plan_workflow(document, reg, base_dir=self._base(params),
                                           profile=get_profile(params.get('profile')))
            schemas = {}
            if plan:
                for (node, anchor), s in sorted(plan.schemas.items()):
                    schemas.setdefault(node, {})[anchor] = s.to_list()
            return {'valid': not problems, 'problems': problems, 'schemas': schemas}
        if method == 'workflow.application':
            from .interface_app import describe_application
            return describe_application(params['workflow'])
        if method == 'workflow.run':
            n = int(params.get('preview_rows', 100))
            try:
                from .interface_app import bind_application
                original = params['workflow']
                document = bind_application(original, params.get('application_values'))
                kwargs = self._run_kwargs(params)
                if (original.get('metadata') or {}).get('application'):
                    kwargs['history_workflow'] = original
                r = run_workflow(document, **kwargs)
            except WorkflowValidationError as e:
                return {'status': 'invalid', 'problems': e.problems}
            return self._result_payload(r, n)
        if method == 'run.start':
            return self._start(params)
        if method == 'run.cancel':
            run = self.runs.get(params.get('run_id'))
            if not run:
                raise EngineError('Unknown run id', code='bad-request')
            run['token'].cancel()
            return {'run_id': params['run_id'], 'cancelling': run['status'] == 'running'}
        if method == 'run.result':
            run = self.runs.get(params.get('run_id'))
            if not run:
                raise EngineError('Unknown run id', code='bad-request')
            return {'status': run['status'], 'result': run.get('result')}
        if method == 'run.export':
            return self._export_result(params)
        if method == 'history.list':
            return self.history.list(int(params.get('limit', 50))) if self.history else []
        if method == 'history.get':
            return self.history.get(params['run_id']) if self.history else None
        if method == 'file.write_text':
            p = _workflow_path(params['path'])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(params['text'], encoding='utf-8')
            return {'path': str(p)}
        if method == 'workflow.import':
            from .interop.yxmd import import_file
            try:
                doc, report = import_file(Path(params['path']))
            except ValueError as e:
                raise EngineError(str(e), code='import-error') from None
            return {'workflow': doc, 'report': report, 'base_dir': str(Path(params['path']).parent)}
        if method == 'workflow.export':
            from .interop.yxmd import export_document
            p = Path(params['path'])
            if p.suffix.lower() != '.yxmd':
                raise EngineError('Export target must be a .yxmd file', code='bad-request')
            text, problems = export_document(params['workflow'])
            if problems and not params.get('allow_partial'):
                return {'written': False, 'problems': problems}
            p.write_text(text, encoding='utf-8')
            return {'written': True, 'path': str(p), 'problems': problems}
        if method == 'file.write_export':
            p = Path(params['path'])
            if p.suffix.lower() not in ('.csv', '.tsv', '.json', '.txt'):
                raise EngineError('Exports must be .csv, .tsv, .json or .txt', code='bad-request')
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(params['text'], encoding='utf-8')
            return {'path': str(p)}
        if method == 'file.read_text':
            return {'path': str(_workflow_path(params['path'])), 'text': _workflow_path(params['path']).read_text(encoding='utf-8')}
        raise KeyError(method)

    def _database(self, method, params):
        """Connection management and metadata operations; passwords never appear in responses."""
        from .db.connections import ConnectionStore
        from .db.dialects import Query
        from .tools.database import open_dialect
        store = ConnectionStore()
        if method == 'database.connections.list':
            return [c.public() for c in store.list()]
        if method == 'database.connections.save':
            cfg = dict(params.get('connection') or {})
            password = params.get('password')
            c = store.save(cfg, password=password)
            return {'connection': c.public(), 'credential_saved': password is not None}
        if method == 'database.connections.delete':
            store.delete(params['connection_id'])
            return {'deleted': True}
        if method == 'database.test':
            cfg, d = open_dialect(params['connection_id'])
            conn = d.connect(read_only=True)
            try:
                cur = conn.cursor()
                cur.execute('SELECT 1')
                cur.fetchone()
            finally:
                conn.rollback()
                conn.close()
            return {'ok': True, 'dialect': d.name, 'database': cfg.database}
        cfg, d = open_dialect(params['connection_id'])
        if method == 'database.preview':
            from .tools.database import source_sql
            mode = params.get('mode', 'table')
            src_cfg = {'mode': mode, 'schema': params.get('schema'), 'table': params.get('table'),
                       'columns': params.get('columns'), 'query': params.get('query'),
                       'params': params.get('params', []), 'order_by': [], 'max_rows': None}
            sql, values = source_sql(d, src_cfg)
            limit = min(max(int(params.get('limit', 100)), 1), 1000)
            with Query(d, d.limit(sql, limit), values) as q:
                fields = q.describe()
                rows = [row for batch in q.batches(fields, limit) for row in batch]
                from .table import Table
                from .types import Schema
                table = Table.from_rows(Schema(fields), rows)
                return _preview(table, len(table))
        with Query(d, 'SELECT 1') as q:
            if method == 'database.schemas':
                return d.schemas(q.conn)
            if method == 'database.tables':
                return d.tables(q.conn, params['schema'])
            if method == 'database.columns':
                return [f.to_dict() for f in d.columns(q.conn, params.get('schema'), params['table'])]
        raise KeyError(method)

    def _start(self, params):
        import uuid
        from .interface_app import bind_application
        params = dict(params)
        original = params['workflow']
        try:
            params['workflow'] = bind_application(original, params.pop('application_values', None))
        except EngineError as e:
            return {'status': 'invalid', 'problems': [{'code': e.code, 'message': e.message, 'node_id': e.node_id}]}
        run_id = uuid.uuid4().hex
        token = CancelToken()
        n = int(params.get('preview_rows', 100))
        entry = {'status': 'running', 'token': token, 'artifacts': {}, 'artifact_errors': {}}
        with self._lock:
            self.runs[run_id] = entry
        # validate synchronously so problems are returned to the caller immediately
        kw = self._run_kwargs(params)
        if (original.get('metadata') or {}).get('application'):
            kw['history_workflow'] = original
        from .profiles import get_profile
        _, problems = plan_workflow(params['workflow'], default_registry(), base_dir=kw['base_dir'],
                                    profile=get_profile(params.get('profile')))
        if problems:
            entry['status'] = 'invalid'
            entry['result'] = {'status': 'invalid', 'problems': problems}
            return {'run_id': run_id, 'status': 'invalid', 'problems': problems}

        def work():
            try:
                artifact_dir = self._artifact_root / run_id
                artifact_dir.mkdir(parents=True, exist_ok=True)

                def capture(node_id, anchor, table):
                    import hashlib
                    key = (node_id, anchor)
                    filename = hashlib.sha256((node_id + '\0' + anchor).encode('utf-8')).hexdigest() + '.parquet'
                    target = artifact_dir / filename
                    from .arrow import write_parquet
                    write_parquet(table, target)
                    entry['artifacts'][key] = target

                r = run_workflow(params['workflow'], run_id=run_id, cancel_token=token,
                                 listener=lambda ev: self.notify('run.event', ev), result_sink=capture, **kw)
                entry['result'] = self._result_payload(r, n)
                entry['status'] = r.status
            except Exception as e:  # never let a worker thread die silently
                entry['status'] = 'failed'
                entry['result'] = {'status': 'failed', 'error': {'code': 'internal-error', 'message': str(e)}}
            self.notify('run.finished', {'run_id': run_id, 'status': entry['status']})
        threading.Thread(target=work, name=f'run-{run_id[:8]}', daemon=True).start()
        return {'run_id': run_id, 'status': 'running'}

    def _export_result(self, params):
        import csv
        import os
        import shutil
        import uuid
        from . import arrow
        from .arrow import pa
        arrow.require()
        run = self.runs.get(params.get('run_id'))
        if not run or run.get('status') == 'running':
            raise EngineError('Run is not complete or does not exist', code='bad-request')
        if run.get('status') != 'succeeded':
            raise EngineError('Only successful workflow results can be exported', code='bad-request')
        node, anchor = params.get('node_id'), params.get('anchor')
        source = run.get('artifacts', {}).get((node, anchor))
        if source is None:
            raise EngineError('Full result data is unavailable for this output', code='result-unavailable')
        fmt = str(params.get('format', '')).lower()
        suffix = {'csv': '.csv', 'parquet': '.parquet'}.get(fmt)
        target = Path(params.get('path', '')).expanduser().resolve()
        if not suffix or target.suffix.lower() != suffix:
            raise EngineError('Export format and file extension must be CSV/.csv or Parquet/.parquet',
                              code='bad-request')
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
        try:
            if fmt == 'parquet':
                shutil.copyfile(source, temp)
            else:
                import pyarrow.parquet as pq
                from .types import format_value
                with temp.open('w', encoding='utf-8-sig', newline='') as f:
                    writer = csv.writer(f, lineterminator='\r\n')
                    parquet = pq.ParquetFile(source)
                    writer.writerow(parquet.schema_arrow.names)
                    for batch in parquet.iter_batches(batch_size=8192):
                        table = arrow.from_arrow(pa.Table.from_batches([batch]))
                        for row in table.rows():
                            writer.writerow(['' if value is None else format_value(value) for value in row])
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return {'path': str(target), 'format': fmt}

    # ------------------------------------------------------------------ JSON-RPC envelope
    def respond(self, line: str) -> str | None:
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            return json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': f'Parse error: {e}'}})
        rid = req.get('id') if isinstance(req, dict) else None
        try:
            if not isinstance(req, dict) or req.get('jsonrpc') != '2.0' or not isinstance(req.get('method'), str):
                raise ValueError('Invalid request')
            result = self.handle(req['method'], req.get('params') or {})
            resp = {'jsonrpc': '2.0', 'id': rid, 'result': result}
        except KeyError as e:
            resp = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': f'Method not found: {e}'}}
        except ValueError as e:
            resp = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32600, 'message': str(e)}}
        except EngineError as e:
            resp = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': e.message, 'data': e.to_dict()}}
        except OSError as e:
            resp = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': str(e),
                                                            'data': {'code': 'io-error', 'message': str(e)}}}
        if rid is None and 'error' not in resp:
            return None  # notification
        return json.dumps(resp, ensure_ascii=False, default=str)


def _workflow_path(path: str) -> Path:
    p = Path(path)
    if not p.name.endswith('.flow.json'):
        raise EngineError('Only *.flow.json workflow documents can be read or written through the service',
                          code='bad-request')
    return p


_STATUS_CACHE = None


def _load_status():
    global _STATUS_CACHE
    if _STATUS_CACHE is None:
        p = catalog.CATALOG_PATH.parent / 'tool-status.json'
        _STATUS_CACHE = {t['id']: t for t in json.loads(p.read_text(encoding='utf-8'))['tools']} if p.is_file() else {}
    return _STATUS_CACHE


def _unavailable_reason(entry, status):
    parts = ['No independent kernel has been implemented for this tool yet; workflows using it fail validation '
             'with "tool-unavailable".']
    if status.get('external_prerequisites_blocking'):
        parts.append('Exact behaviour also depends on licensed data or an external service that has not been '
                     'provisioned for testing.')
    return ' '.join(parts)


# =========================================================================== transports

def respond(line: str) -> str | None:  # backwards-compatible module-level helper (stateless calls)
    return EngineService().respond(line)


def serve(stdin=sys.stdin, stdout=sys.stdout, history_path: Path | None = None):
    lock = threading.Lock()

    def write(obj):
        with lock:
            stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + '\n')
            stdout.flush()
    svc = EngineService(notify=lambda m, p: write({'jsonrpc': '2.0', 'method': m, 'params': p}),
                        history_path=history_path)
    for line in stdin:
        if not line.strip():
            continue
        out = svc.respond(line)
        if out is not None:
            with lock:
                stdout.write(out + '\n')
                stdout.flush()


def serve_http(host: str = '127.0.0.1', port: int = 8765, history_path: Path | None = None):
    """HTTP transport for the browser build: POST /rpc, GET /events (SSE). Loopback only."""
    import queue
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise EngineError('The HTTP transport only binds to loopback', code='bad-request')
    subscribers: list[queue.Queue] = []
    sub_lock = threading.Lock()

    def notify(method, params):
        msg = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params}, ensure_ascii=False, default=str)
        with sub_lock:
            for q in subscribers:
                q.put(msg)
    svc = EngineService(notify=notify, history_path=history_path)

    class Handler(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', 'content-type')

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self):
            if self.path != '/rpc':
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get('content-length', 0))).decode('utf-8')
            out = svc.respond(body) or 'null'
            data = out.encode('utf-8')
            self.send_response(200)
            self._cors()
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/health':
                data = b'ok'
                self.send_response(200)
                self._cors()
                self.send_header('content-length', '2')
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path != '/events':
                self.send_error(404)
                return
            q: queue.Queue = queue.Queue()
            with sub_lock:
                subscribers.append(q)
            self.send_response(200)
            self._cors()
            self.send_header('content-type', 'text/event-stream')
            self.send_header('cache-control', 'no-cache')
            self.end_headers()
            try:
                self.wfile.write(b': connected\n\n')
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(f'data: {msg}\n\n'.encode('utf-8'))
                    except queue.Empty:
                        self.wfile.write(b': keep-alive\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                with sub_lock:
                    subscribers.remove(q)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f'flowengine HTTP service on http://{host}:{server.server_address[1]}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
