"""Anaplan Input and Output through the Anaplan Integration API v2; no vendor code.

Functional source: the public Anaplan Input/Output tool help and Anaplan's API documentation (help.anaplan.com):
basic authentication at ``https://auth.anaplan.com/token/authenticate`` (one token per run; the docs advise
against a token per call), then ``Authorization: AnaplanAuthToken <token>`` on
``https://api.anaplan.com/2/0/workspaces/<ws>/models/<model>/...``.

* Input runs an export action (``exports/<id>/tasks``), polls the task until it is COMPLETE, then downloads the
  export file's chunks (``files/<id>/chunks``) and reads them as Input Data would (CSV by default).
* Output writes the records as CSV, sets the chunk count to -1, uploads the chunks in order, marks the upload
  complete, runs the import action and polls it. A COMPLETE task can still have failed: its result says whether it
  succeeded and whether a failure dump is available, and the tool reports both.

The connection is a Web/API connection with basic authentication (the Anaplan user's email and password); the
token request goes to Anaplan's own authentication host only.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ._remote_files import read_bytes, written_file
from .download import _HttpOnlyRedirects
from .github import _permit
from .msgraph import _NoRedirect, _graph_error

AUTH_URL = 'https://auth.anaplan.com/token/authenticate'
CHUNK = 10 * 1024 * 1024
POLL_SECONDS = 2


class Anaplan:
    auth_url = AUTH_URL

    def __init__(self, cfg):
        from ..service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        self.conn = store.get(cfg['connection_id'], kind='web')
        if self.conn.auth != 'basic':
            raise ConfigError('An Anaplan connection uses basic authentication: the Anaplan user email and password')
        self.secret = store.secret(self.conn)
        self.base = (f"{self.conn.base_url.rstrip('/')}/workspaces/{urllib.parse.quote(cfg['workspace_id'], safe='')}"
                     f"/models/{urllib.parse.quote(cfg['model_id'], safe='')}")
        self.timeout = cfg['timeout_seconds']
        self.opener = urllib.request.build_opener(_HttpOnlyRedirects(self.conn))
        self.token = None

    def _authenticate(self):
        basic = base64.b64encode(f'{self.conn.username}:{self.secret}'.encode()).decode()
        req = urllib.request.Request(self.auth_url, data=b'', method='POST', headers={'Authorization': f'Basic {basic}'})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=self.timeout) as r:
                doc = json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            e.close()
            raise DataError(f'Anaplan refused the sign-in ({e.code}); check the email and password, and that the user may '
                            'use basic authentication (single sign-on users must be exception users)') from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DataError(f'Could not reach Anaplan authentication: {getattr(e, "reason", None) or e}') from None
        token = (doc.get('tokenInfo') or {}).get('tokenValue')
        if not token:
            raise DataError('Anaplan returned no token')
        self.token = token

    def request(self, method, path, body=None, content_type='application/json', raw=False):
        url = f'{self.base}/{path}'
        if not self.conn.covers(url):
            raise ConfigError(f'{url} is outside the connection base URL')
        if self.token is None:
            self._authenticate()
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        headers = {'Authorization': f'AnaplanAuthToken {self.token}', 'Accept': 'application/octet-stream' if raw else 'application/json'}
        if data is not None:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                content = r.read()
                return content if raw else (json.loads(content) if content.strip() else None)
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read(64 * 1024)
            raise DataError(f'Anaplan {method} {path} returned {e.code}: {_status_message(detail)}') from None
        except (urllib.error.URLError, OSError) as e:
            raise DataError(f'Could not reach Anaplan: {getattr(e, "reason", None) or e}') from None

    def run_task(self, kind, action_id, ctx):
        """Start an import or export task and wait until it is COMPLETE or CANCELLED; returns the task."""
        q = urllib.parse.quote(action_id, safe='')
        started = self.request('POST', f'{kind}/{q}/tasks', {'localeName': 'en_US'})
        task_id = (started.get('task') or {}).get('taskId')
        if not task_id:
            raise DataError(f'Anaplan did not start the {kind[:-1]} task')
        while True:
            ctx.check_cancelled()
            task = self.request('GET', f'{kind}/{q}/tasks/{urllib.parse.quote(task_id, safe="")}').get('task', {})
            if task.get('taskState') in ('COMPLETE', 'CANCELLED'):
                return task
            time.sleep(POLL_SECONDS)


def _status_message(detail: bytes) -> str:
    try:
        status = json.loads(detail).get('status', {})
        if isinstance(status, dict) and status.get('message'):
            return status['message']
    except (ValueError, AttributeError):
        pass
    return _graph_error(detail)


def _find(items, key, wanted, what):
    match = [x for x in items if wanted in (x.get('id'), x.get('name'))]
    if not match:
        names = ', '.join(sorted(str(x.get('name')) for x in items)[:20]) or 'none'
        raise ConfigError(f'No Anaplan {what} {wanted!r} in this model; available: {names}')
    return match[0]


_COMMON = {'provider': {'type': 'string', 'enum': ['anaplan']}, 'provider_semantics': {'type': 'string'},
           'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                             'description': 'Saved Anaplan connection, base URL https://api.anaplan.com/2/0, basic authentication'},
           'workspace_id': {'type': 'string', 'minLength': 1}, 'model_id': {'type': 'string', 'minLength': 1},
           'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600, 'default': 300}}


def _provider(cfg):
    from .. import providers
    providers.resolve(cfg, 'anaplan')


def _task_failed(task, kind):
    result = task.get('result') or {}
    if task.get('taskState') == 'CANCELLED':
        return f'The Anaplan {kind} task was cancelled'
    if result.get('successful') is False:
        dump = ' A failure dump is available in Anaplan.' if result.get('failureDumpAvailable') else ''
        details = '; '.join(str(d.get('localMessageText') or d) for d in result.get('details', [])[:5])
        return f'The Anaplan {kind} did not succeed{": " + details if details else ""}.{dump}'
    return None


class AnaplanInput(Tool):
    manifest = ToolManifest(
        tool_id='core.anaplan_input', version='1.0.0', display_name='Anaplan Input', catalog_ids=('connectors:anaplan-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Records of the export file'),),
        side_effects='network', deterministic=False,
        description='Run an Anaplan export action (by id or name) and read the file it produces, downloaded in chunks, as '
                    'Input Data reads it (CSV by default; read holds the Input Data settings).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'workspace_id', 'model_id', 'export'], 'properties': {**_COMMON,
            'export': {'type': 'string', 'minLength': 1, 'description': 'Export action id or name'},
            'read': {'type': 'object', 'default': {}}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx)
        ap = Anaplan(cfg)
        export = _find((ap.request('GET', 'exports') or {}).get('exports', []), 'id', cfg['export'], 'export')
        task = ap.run_task('exports', export['id'], ctx)
        failed = _task_failed(task, 'export')
        if failed:
            raise DataError(failed)
        fid = urllib.parse.quote(export['id'], safe='')  # an export writes the file with the export's id
        chunks = (ap.request('GET', f'files/{fid}/chunks') or {}).get('chunks', [])
        data = b''.join(ap.request('GET', f"files/{fid}/chunks/{urllib.parse.quote(str(c['id']), safe='')}", raw=True)
                        for c in sorted(chunks, key=lambda c: int(c['id'])))
        name = export.get('name') or 'export.csv'
        suffix = '.' + name.rsplit('.', 1)[1] if '.' in name else '.csv'
        return {'output': read_bytes(ctx, data, suffix, cfg['read'], name)}


class AnaplanOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.anaplan_output', version='1.0.0', display_name='Anaplan Output', catalog_ids=('connectors:anaplan-output',),
        inputs=(Anchor('input', 'I', description='Records to import'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Upload records as the data file of an Anaplan import action (by id or name) in chunks, then run the '
                    'import and report whether it succeeded (and whether a failure dump is available).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'workspace_id', 'model_id', 'import_action', 'file'], 'properties': {**_COMMON,
            'import_action': {'type': 'string', 'minLength': 1, 'description': 'Import action id or name'},
            'file': {'type': 'string', 'minLength': 1, 'description': 'The import data source file, id or name'},
            'first_data_row': {'type': 'integer', 'minimum': 1, 'default': 2},
            'header_row': {'type': 'integer', 'minimum': 0, 'default': 1},
            'write': {'type': 'object', 'default': {}}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx, writes=True)
        ap = Anaplan(cfg)
        action = _find((ap.request('GET', 'imports') or {}).get('imports', []), 'id', cfg['import_action'], 'import')
        target = _find((ap.request('GET', 'files') or {}).get('files', []), 'id', cfg['file'], 'file')
        fid = urllib.parse.quote(target['id'], safe='')
        with written_file(ctx, cfg['write'], inputs, '.csv') as local:
            ap.request('POST', f'files/{fid}', {'chunkCount': -1})
            count = 0
            with open(local, 'rb') as fh:
                while chunk := fh.read(CHUNK):
                    ctx.check_cancelled()
                    ap.request('PUT', f'files/{fid}/chunks/{count}', chunk, 'application/octet-stream')
                    count += 1
            ap.request('POST', f'files/{fid}/complete', {'id': target['id'], 'name': target.get('name'), 'chunkCount': count,
                                                          'firstDataRow': cfg['first_data_row'], 'headerRow': cfg['header_row']})
        task = ap.run_task('imports', action['id'], ctx)
        failed = _task_failed(task, 'import')
        if failed:
            raise DataError(failed)
        ctx.info(f"imported {len(inputs['input']):,} records through {action.get('name', action['id'])} ({count} chunk(s))")
        return {}


TOOLS = [AnaplanInput, AnaplanOutput]
