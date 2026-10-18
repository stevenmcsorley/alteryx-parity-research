"""Google Drive, Google Sheets, Google BigQuery and Google Analytics 4 through their REST APIs; no Google SDK.

Functional source: the public tool help for each connector and the Google API references (Drive v3,
Sheets v4, BigQuery v2, Analytics Data v1beta). Each API has its own host, so each has its own
Web/API connection with OAuth (flowengine/oauth.py), token URL ``https://oauth2.googleapis.com/token``:

* Drive      ``https://www.googleapis.com``           scope https://www.googleapis.com/auth/drive
* Sheets     ``https://sheets.googleapis.com``        scope https://www.googleapis.com/auth/spreadsheets
* BigQuery   ``https://bigquery.googleapis.com``      scope https://www.googleapis.com/auth/bigquery
* Analytics  ``https://analyticsdata.googleapis.com`` scope https://www.googleapis.com/auth/analytics.readonly

Users sign in in the browser (authorization URL ``https://accounts.google.com/o/oauth2/v2/auth``;
Google's device sign-in does not allow these scopes), or a service account key is saved as the secret.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, cast_value, format_value, type_for_values
from .download import UNBOUNDED_TEXT
from .github import _permit
from .msgraph import Graph

SIMPLE_UPLOAD_MAX = 5 * 1024 * 1024
RESUMABLE_CHUNK = 256 * 1024 * 32     # resumable chunks must be multiples of 256 KiB
GOOGLE_NATIVE = {'application/vnd.google-apps.spreadsheet':
                 ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'),
                 'application/vnd.google-apps.document': ('text/plain', '.txt')}


class Google(Graph):
    service = 'Google API'

    def __init__(self, cfg, service):
        self.service = service
        super().__init__(cfg)


def _google_error(detail: bytes) -> str:
    try:
        err = json.loads(detail).get('error', {})
        return err.get('message') or json.dumps(err)
    except (ValueError, AttributeError):
        return detail[:300].decode('utf-8', 'replace')


def _q(value: str) -> str:
    """A Drive query string literal."""
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def _provider(cfg):
    from .. import providers
    providers.resolve(cfg, 'google-cloud')


_CONN = {'provider': {'type': 'string', 'enum': ['google']}, 'provider_semantics': {'type': 'string'},
         'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web'},
         'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600, 'default': 300}}


# --------------------------------------------------------------------------- Drive

def _drive_file(g, cfg):
    """(id, name, mimeType) of the configured file: by id, or by name inside a folder."""
    if cfg.get('file_id'):
        return g.request('GET', f"drive/v3/files/{urllib.parse.quote(cfg['file_id'], safe='')}?fields=id,name,mimeType"
                                "&supportsAllDrives=true")
    if not cfg.get('name'):
        raise ConfigError('Name the file (file_id, or name with an optional folder_id)')
    q = f"name = {_q(cfg['name'])} and trashed = false" + (f" and {_q(cfg['folder_id'])} in parents" if cfg.get('folder_id') else '')
    found = (g.request('GET', 'drive/v3/files?' + urllib.parse.urlencode({
        'q': q, 'fields': 'files(id,name,mimeType)', 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true',
        'pageSize': '10'})) or {}).get('files', [])
    if len(found) > 1:
        raise DataError(f"{len(found)} files are called {cfg['name']!r} there; use file_id or a folder_id")
    return found[0] if found else None


class GoogleDriveInput(Tool):
    manifest = ToolManifest(
        tool_id='core.google_drive_input', version='1.0.0', display_name='Google Drive Input',
        catalog_ids=('connectors:google-drive-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Records read from the file'),),
        side_effects='network', deterministic=False,
        description='Read a Google Drive file (by id, or by name in a folder) in any format Input Data reads. Google '
                    'Sheets files are exported as Excel; read.sheet picks the tab.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id'],
                       'properties': {**_CONN,
            'file_id': {'type': ['string', 'null'], 'default': None},
            'name': {'type': ['string', 'null'], 'default': None},
            'folder_id': {'type': ['string', 'null'], 'default': None},
            'read': {'type': 'object', 'default': {}, 'description': 'Input Data settings except path'}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .inout import InputData
        _provider(cfg)
        if 'path' in cfg['read']:
            raise ConfigError('read holds format settings; the file is named by file_id or name')
        _permit(ctx)
        g = Google(cfg, 'Google Drive')
        meta = _drive_file(g, cfg)
        if not meta:
            raise DataError(f"No Google Drive file called {cfg['name']!r} was found")
        fid = urllib.parse.quote(meta['id'], safe='')
        if meta.get('mimeType') in GOOGLE_NATIVE:
            export, suffix = GOOGLE_NATIVE[meta['mimeType']]
            data = g.request('GET', f'drive/v3/files/{fid}/export?' + urllib.parse.urlencode({'mimeType': export}), raw=True)
        elif str(meta.get('mimeType', '')).startswith('application/vnd.google-apps.'):
            raise DataError(f"{meta['name']!r} is a {meta['mimeType']} file, which cannot be read as data")
        else:
            suffix = Path(meta['name']).suffix
            data = g.request('GET', f'drive/v3/files/{fid}?alt=media&supportsAllDrives=true', raw=True)
        reader = InputData()
        with tempfile.TemporaryDirectory(prefix='flowbench-gdrive-') as d:
            local = Path(d) / f'file{suffix}'
            local.write_bytes(data)
            ctx.info(f"Downloaded {meta['name']} ({len(data):,} bytes)")
            return {'output': reader.run(ctx, reader.normalize_config({**cfg['read'], 'path': str(local)}), {})['output']}


class GoogleDriveOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.google_drive_output', version='1.0.0', display_name='Google Drive Output',
        catalog_ids=('connectors:google-drive-output',),
        inputs=(Anchor('input', 'I', description='Records to write'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Write records to a Google Drive file (name in a folder) in any format Output Data writes. An existing '
                    'file of that name is replaced, or the run fails (if_exists error). Large files use a resumable upload.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'name'],
                       'properties': {**_CONN,
            'name': {'type': 'string', 'minLength': 1},
            'folder_id': {'type': ['string', 'null'], 'default': None, 'description': 'Parent folder id; empty for My Drive'},
            'if_exists': {'enum': ['overwrite', 'error'], 'default': 'overwrite'},
            'write': {'type': 'object', 'default': {}, 'description': 'Output Data settings except path'}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        from .inout import OutputData
        _provider(cfg)
        if 'path' in cfg['write']:
            raise ConfigError('write holds format settings; the file is named by name')
        _permit(ctx, writes=True)
        g = Google(cfg, 'Google Drive')
        existing = _drive_file(g, {'name': cfg['name'], 'folder_id': cfg['folder_id']})
        if existing and cfg['if_exists'] == 'error':
            raise DataError(f"A file called {cfg['name']!r} already exists there")
        writer = OutputData()
        with tempfile.TemporaryDirectory(prefix='flowbench-gdrive-') as d:
            local = Path(d) / f"upload{Path(cfg['name']).suffix}"
            writer.run(ctx, writer.normalize_config({**cfg['write'], 'path': str(local)}), inputs)
            size = local.stat().st_size
            meta = {'name': cfg['name'], **({'parents': [cfg['folder_id']]} if cfg['folder_id'] and not existing else {})}
            target = (f"upload/drive/v3/files/{urllib.parse.quote(existing['id'], safe='')}" if existing else 'upload/drive/v3/files')
            method = 'PATCH' if existing else 'POST'
            if size <= SIMPLE_UPLOAD_MAX:
                boundary = 'flowbench' + base64.b16encode(__import__('os').urandom(8)).decode()
                body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n'
                        f'--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + local.read_bytes() + \
                    f'\r\n--{boundary}--\r\n'.encode()
                g.request(method, f'{target}?uploadType=multipart&supportsAllDrives=true', body,
                          {'Content-Type': f'multipart/related; boundary={boundary}'})
            else:
                _resumable(ctx, g, method, f'{target}?uploadType=resumable&supportsAllDrives=true', meta, local, size)
        ctx.info(f"Uploaded {len(inputs['input']):,} records to {cfg['name']} ({size:,} bytes)")
        return {}


def _resumable(ctx, g, method, url, meta, local, size):
    _, headers = g.request(method, url, json.dumps(meta).encode(), {'Content-Type': 'application/json; charset=UTF-8',
                                                                     'X-Upload-Content-Length': str(size)}, raw=True, with_headers=True)
    session = headers.get('Location')
    if not session:
        raise DataError(f'{g.service} did not start a resumable upload')
    with open(local, 'rb') as fh:
        start = 0
        while start < size:
            ctx.check_cancelled()
            chunk = fh.read(RESUMABLE_CHUNK)
            end = start + len(chunk) - 1
            # the session URI carries the upload; it is on the same Google host, so it is checked against the connection
            g.request('PUT', session, chunk, {'Content-Range': f'bytes {start}-{end}/{size}',
                                              'Content-Type': 'application/octet-stream'}, raw=True, accept_incomplete=True)
            start = end + 1


# --------------------------------------------------------------------------- Sheets

def _a1(sheet: str, cell: str = '') -> str:
    return "'" + sheet.replace("'", "''") + "'" + (f'!{cell}' if cell else '')


class GoogleSheetsInput(Tool):
    manifest = ToolManifest(
        tool_id='core.google_sheets_input', version='1.0.0', display_name='Google Sheets Input',
        catalog_ids=('connectors:google-sheets-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per row below the header'),),
        side_effects='network', deterministic=False,
        description='Read a tab (or a cell range) of a Google Sheets spreadsheet. The first row is the header; numbers '
                    'and booleans keep their type; dates arrive as their displayed text.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'spreadsheet_id', 'sheet'], 'properties': {**_CONN,
            'spreadsheet_id': {'type': 'string', 'minLength': 1, 'description': 'The id in the spreadsheet URL'},
            'sheet': {'type': 'string', 'minLength': 1, 'description': 'Tab name'},
            'range': {'type': ['string', 'null'], 'default': None, 'pattern': '^[A-Za-z]{1,3}[0-9]*(:[A-Za-z]{1,3}[0-9]*)?$'},
            'has_header': {'type': 'boolean', 'default': True}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx)
        g = Google(cfg, 'Google Sheets')
        rng = urllib.parse.quote(_a1(cfg['sheet'], cfg['range'] or ''), safe='')
        doc = g.request('GET', f"v4/spreadsheets/{urllib.parse.quote(cfg['spreadsheet_id'], safe='')}/values/{rng}?"
                               'valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=FORMATTED_STRING') or {}
        values = doc.get('values', [])
        if not values:
            return {'output': Table([], [])}
        width = max(len(r) for r in values)
        if cfg['has_header']:
            names = [str(v).strip() for v in values[0]] + [f'Field_{i + 1}' for i in range(len(values[0]), width)]
            body = values[1:]
        else:
            names, body = [f'Field_{i + 1}' for i in range(width)], values
        names = [n or f'Field_{i + 1}' for i, n in enumerate(names)]
        if len(set(names)) != len(names):
            raise DataError(f'Duplicate header names {sorted({n for n in names if names.count(n) > 1})}')
        rows = [r + [None] * (width - len(r)) for r in body]
        cols = [[None if v == '' else v for v in c] for c in zip(*rows)] if rows else [[] for _ in names]
        fields = []
        for n, c in zip(names, cols):
            t = type_for_values(c)
            fields.append(Field(n, t, UNBOUNDED_TEXT if t.is_string else None))
        return {'output': Table(Schema(fields), [[cast_value(v, f)[0] for v in c] for f, c in zip(fields, cols)])}


def _sheet_cell(v):
    if v is None:
        return ''
    if isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        raise DataError('Google Sheets cannot hold binary values')
    return format_value(v)


class GoogleSheetsOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.google_sheets_output', version='1.0.0', display_name='Google Sheets Output',
        catalog_ids=('connectors:google-sheets-output',),
        inputs=(Anchor('input', 'I', description='Records to write'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Write records to a tab of a Google Sheets spreadsheet: overwrite the tab (cleared first) or append '
                    'rows below the existing data. The tab is created when missing. Values are written as typed (RAW).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'spreadsheet_id', 'sheet'], 'properties': {**_CONN,
            'spreadsheet_id': {'type': 'string', 'minLength': 1},
            'sheet': {'type': 'string', 'minLength': 1},
            'mode': {'enum': ['overwrite', 'append'], 'default': 'overwrite'},
            'write_header': {'type': 'boolean', 'default': True}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx, writes=True)
        g = Google(cfg, 'Google Sheets')
        table: Table = inputs['input']
        sid = urllib.parse.quote(cfg['spreadsheet_id'], safe='')
        tabs = [s['properties']['title'] for s in (g.request('GET', f'v4/spreadsheets/{sid}?fields=sheets.properties.title')
                                                   or {}).get('sheets', [])]
        if cfg['sheet'] not in tabs:
            g.request('POST', f'v4/spreadsheets/{sid}:batchUpdate', {'requests': [{'addSheet': {'properties': {'title': cfg['sheet']}}}]})
        rows = [[_sheet_cell(v) for v in r] for r in table.rows()]
        rng = urllib.parse.quote(_a1(cfg['sheet']), safe='')
        if cfg['mode'] == 'overwrite':
            g.request('POST', f'v4/spreadsheets/{sid}/values/{rng}:clear', {})
            values = ([list(table.schema.names)] if cfg['write_header'] else []) + rows
            g.request('PUT', f"v4/spreadsheets/{sid}/values/{urllib.parse.quote(_a1(cfg['sheet'], 'A1'), safe='')}"
                             '?valueInputOption=RAW', {'values': values})
        else:
            existing = (g.request('GET', f'v4/spreadsheets/{sid}/values/{rng}?majorDimension=ROWS') or {}).get('values', [])
            values = ([list(table.schema.names)] if cfg['write_header'] and not existing else []) + rows
            g.request('POST', f'v4/spreadsheets/{sid}/values/{rng}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS',
                      {'values': values})
        ctx.info(f"{cfg['mode']}: {len(rows):,} rows to {cfg['sheet']}")
        return {}


# --------------------------------------------------------------------------- BigQuery

_BQ_TYPES = {'INTEGER': FieldType.INT64, 'INT64': FieldType.INT64, 'FLOAT': FieldType.DOUBLE, 'FLOAT64': FieldType.DOUBLE,
             'NUMERIC': FieldType.FIXED_DECIMAL, 'BOOLEAN': FieldType.BOOL, 'BOOL': FieldType.BOOL, 'DATE': FieldType.DATE,
             'DATETIME': FieldType.DATETIME, 'TIMESTAMP': FieldType.DATETIME, 'TIME': FieldType.TIME, 'BYTES': FieldType.BLOB}


def _bq_field(f):
    if f.get('mode') == 'REPEATED' or f['type'] in ('RECORD', 'STRUCT'):
        return Field(f['name'], FieldType.V_WSTRING, UNBOUNDED_TEXT)  # nested values as JSON text
    t = _BQ_TYPES.get(f['type'], FieldType.V_WSTRING)
    if t is FieldType.FIXED_DECIMAL:
        return Field(f['name'], t, 38, 9)
    return Field(f['name'], t, UNBOUNDED_TEXT if t.is_string else None)


def _bq_value(v, f):
    """BigQuery's JSON row format ({f: [{v: ...}]}) to a Python value; nested values as JSON."""
    if isinstance(v, dict) and 'f' in v:
        return {sf['name']: _bq_value(x.get('v'), sf) for sf, x in zip(f.get('fields', []), v['f'])}
    if f.get('mode') == 'REPEATED':
        return [_bq_value(x.get('v'), {**f, 'mode': 'NULLABLE'}) for x in (v or [])]
    if v is None:
        return None
    t = f['type']
    if t in ('INTEGER', 'INT64'):
        return int(v)
    if t in ('FLOAT', 'FLOAT64'):
        return float(v)
    if t in ('BOOLEAN', 'BOOL'):
        return v == 'true' or v is True
    if t == 'TIMESTAMP':
        return _dt.datetime.fromtimestamp(float(v), _dt.timezone.utc).replace(tzinfo=None)
    if t == 'BYTES':
        return base64.b64decode(v)
    if t == 'NUMERIC':
        return Decimal(v)
    if t in ('RECORD', 'STRUCT'):
        return v
    return v


def _bq_cell(v, field):
    if field.type.is_string and isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    if field.type is FieldType.DATETIME and isinstance(v, str):
        return v.replace('T', ' ')
    return v


class GoogleBigQueryInput(Tool):
    manifest = ToolManifest(
        tool_id='core.google_bigquery_input', version='1.0.0', display_name='Google BigQuery Input',
        catalog_ids=('connectors:google-bigquery-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Query result rows'),),
        side_effects='network', deterministic=False,
        description='Run a GoogleSQL query in BigQuery and read every result row (paged). Types follow the result schema; '
                    'TIMESTAMP becomes a UTC datetime; RECORD and REPEATED columns arrive as JSON text.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'project', 'query'],
                       'properties': {**_CONN,
            'project': {'type': 'string', 'minLength': 1, 'description': 'Project that runs (and pays for) the query'},
            'query': {'type': 'string', 'minLength': 1},
            'location': {'type': ['string', 'null'], 'default': None, 'description': 'Dataset location, for example EU'},
            'maximum_bytes_billed': {'type': ['integer', 'null'], 'minimum': 1, 'default': None,
                                     'description': 'Refuse to run a query that would bill more than this'}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx)
        g = Google(cfg, 'BigQuery')
        project = urllib.parse.quote(cfg['project'], safe='')
        body = {'query': cfg['query'], 'useLegacySql': False, 'timeoutMs': 10000, 'maxResults': 10000}
        if cfg['location']:
            body['location'] = cfg['location']
        if cfg['maximum_bytes_billed']:
            body['maximumBytesBilled'] = str(cfg['maximum_bytes_billed'])
        res = g.request('POST', f'bigquery/v2/projects/{project}/queries', body)
        job = res['jobReference']
        jid = urllib.parse.quote(job['jobId'], safe='')
        loc = {'location': job['location']} if job.get('location') else {}
        while not res.get('jobComplete'):
            ctx.check_cancelled()
            res = g.request('GET', f'bigquery/v2/projects/{project}/queries/{jid}?' + urllib.parse.urlencode(
                {'timeoutMs': '10000', 'maxResults': '10000', **loc}))
        schema = res.get('schema', {}).get('fields', [])
        fields = [_bq_field(f) for f in schema]
        rows = list(res.get('rows', []))
        token = res.get('pageToken')
        while token:
            ctx.check_cancelled()
            page = g.request('GET', f'bigquery/v2/projects/{project}/queries/{jid}?' + urllib.parse.urlencode(
                {'pageToken': token, 'maxResults': '10000', **loc}))
            rows.extend(page.get('rows', []))
            token = page.get('pageToken')
        cols = [[cast_value(_bq_cell(_bq_value(r['f'][i].get('v'), sf), fld), fld)[0] for r in rows]
                for i, (sf, fld) in enumerate(zip(schema, fields))]
        ctx.info(f"read {len(rows):,} rows (job {job['jobId']}, {res.get('totalBytesProcessed', '?')} bytes processed)")
        return {'output': Table(Schema(fields), cols) if fields else Table([], [])}


def _bq_type(f: Field) -> str:
    t = f.type
    if t is FieldType.BOOL:
        return 'BOOLEAN'
    if t.is_integer:
        return 'INTEGER'
    if t is FieldType.FIXED_DECIMAL:
        return 'NUMERIC' if (f.size or 0) - (f.scale or 0) <= 29 and (f.scale or 0) <= 9 else 'BIGNUMERIC'
    if t in (FieldType.FLOAT, FieldType.DOUBLE):
        return 'FLOAT'
    return {FieldType.DATE: 'DATE', FieldType.DATETIME: 'DATETIME', FieldType.TIME: 'TIME', FieldType.BLOB: 'BYTES'}.get(t, 'STRING')


def _bq_json(v):
    if isinstance(v, bytes):
        return base64.b64encode(v).decode('ascii')
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (_dt.date, _dt.datetime, _dt.time)):
        return v.isoformat()
    return v


class GoogleBigQueryOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.google_bigquery_output', version='1.0.0', display_name='Google BigQuery Output',
        catalog_ids=('connectors:google-bigquery-output',),
        inputs=(Anchor('input', 'I', description='Records to load'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Load records into a BigQuery table with a load job (all rows or none): append, or replace the '
                    "table's contents. The table is created from the input schema when missing.",
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'project', 'dataset', 'table'], 'properties': {**_CONN,
            'project': {'type': 'string', 'minLength': 1},
            'dataset': {'type': 'string', 'pattern': '^[A-Za-z0-9_]{1,1024}$'},
            'table': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,1024}$'},
            'mode': {'enum': ['append', 'replace'], 'default': 'append'},
            'location': {'type': ['string', 'null'], 'default': None}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx, writes=True)
        g = Google(cfg, 'BigQuery')
        table: Table = inputs['input']
        project = urllib.parse.quote(cfg['project'], safe='')
        job = {'configuration': {'load': {
            'destinationTable': {'projectId': cfg['project'], 'datasetId': cfg['dataset'], 'tableId': cfg['table']},
            'sourceFormat': 'NEWLINE_DELIMITED_JSON', 'createDisposition': 'CREATE_IF_NEEDED',
            'writeDisposition': 'WRITE_TRUNCATE' if cfg['mode'] == 'replace' else 'WRITE_APPEND',
            'schema': {'fields': [{'name': f.name, 'type': _bq_type(f), 'mode': 'NULLABLE'} for f in table.schema]}}},
            **({'jobReference': {'projectId': cfg['project'], 'location': cfg['location']}} if cfg['location'] else {})}
        with tempfile.TemporaryDirectory(prefix='flowbench-bq-') as d:
            local = Path(d) / 'rows.ndjson'
            with open(local, 'w', encoding='utf-8') as fh:
                for rec in table.to_dicts():
                    fh.write(json.dumps({k: _bq_json(v) for k, v in rec.items()}, ensure_ascii=False) + '\n')
            size = local.stat().st_size
            started = g.request('POST', f'upload/bigquery/v2/projects/{project}/jobs?uploadType=resumable', json.dumps(job).encode(),
                                {'Content-Type': 'application/json; charset=UTF-8', 'X-Upload-Content-Length': str(size),
                                 'X-Upload-Content-Type': 'application/octet-stream'}, raw=True, with_headers=True)
            session = started[1].get('Location')
            if not session:
                raise DataError('BigQuery did not start the load upload')
            result = None
            with open(local, 'rb') as fh:
                start = 0
                while start < size or (size == 0 and result is None):
                    ctx.check_cancelled()
                    chunk = fh.read(RESUMABLE_CHUNK)
                    end = start + len(chunk) - 1
                    rng = f'bytes {start}-{end}/{size}' if chunk else f'bytes */{size}'
                    out = g.request('PUT', session, chunk, {'Content-Range': rng, 'Content-Type': 'application/octet-stream'},
                                    raw=True, accept_incomplete=True)
                    if out:
                        result = json.loads(out)
                    start = end + 1
        if not result or 'jobReference' not in result:
            raise DataError('BigQuery did not return the load job')
        ref = result['jobReference']
        loc = {'location': ref['location']} if ref.get('location') else {}
        while result.get('status', {}).get('state') != 'DONE':
            ctx.check_cancelled()
            time.sleep(1)
            result = g.request('GET', f"bigquery/v2/projects/{project}/jobs/{urllib.parse.quote(ref['jobId'], safe='')}?"
                                      + urllib.parse.urlencode(loc))
        err = result['status'].get('errorResult')
        if err:
            details = '; '.join(e.get('message', '') for e in result['status'].get('errors', [])[:5])
            raise DataError(f"BigQuery load failed: {err.get('message')} {details}".strip())
        ctx.info(f"loaded {result.get('statistics', {}).get('load', {}).get('outputRows', len(table))} rows into "
                 f"{cfg['dataset']}.{cfg['table']} ({cfg['mode']})")
        return {}


# --------------------------------------------------------------------------- Analytics 4

_GA_METRIC = {'TYPE_INTEGER': FieldType.INT64}


class GoogleAnalytics4Input(Tool):
    manifest = ToolManifest(
        tool_id='core.google_analytics4_input', version='1.0.0', display_name='Google Analytics 4 Input',
        catalog_ids=('connectors:google-analytics-4-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per report row'),),
        side_effects='network', deterministic=False,
        description='Run a Google Analytics 4 report (Analytics Data API runReport): dimensions, metrics and date ranges, '
                    'paged. The date dimension becomes a date; integer metrics are integers, others decimals.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'property_id', 'metrics', 'start_date', 'end_date'],
                       'properties': {**_CONN,
            'property_id': {'type': 'string', 'pattern': '^[0-9]{1,20}$'},
            'dimensions': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'maxItems': 9, 'default': []},
            'metrics': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'minItems': 1, 'maxItems': 10},
            'start_date': {'type': 'string', 'description': 'YYYY-MM-DD, or relative such as 30daysAgo'},
            'end_date': {'type': 'string', 'description': 'YYYY-MM-DD, or today / yesterday'},
            'limit': {'type': 'integer', 'minimum': 0, 'default': 0}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx)
        g = Google(cfg, 'Google Analytics')
        body = {'dimensions': [{'name': d} for d in cfg['dimensions']], 'metrics': [{'name': m} for m in cfg['metrics']],
                'dateRanges': [{'startDate': cfg['start_date'], 'endDate': cfg['end_date']}]}
        rows, offset, page_size = [], 0, 100000
        dim_headers = metric_headers = None
        while True:
            ctx.check_cancelled()
            want = page_size if not cfg['limit'] else min(page_size, cfg['limit'] - len(rows))
            res = g.request('POST', f"v1beta/properties/{cfg['property_id']}:runReport", {**body, 'limit': str(want), 'offset': str(offset)})
            dim_headers = res.get('dimensionHeaders', [])
            metric_headers = res.get('metricHeaders', [])
            batch = res.get('rows', [])
            rows.extend(batch)
            offset += len(batch)
            total = int(res.get('rowCount', 0))
            if not batch or offset >= total or (cfg['limit'] and len(rows) >= cfg['limit']):
                break
        fields = [Field(h['name'], FieldType.DATE if h['name'] == 'date' else FieldType.V_WSTRING,
                        None if h['name'] == 'date' else UNBOUNDED_TEXT) for h in dim_headers]
        fields += [Field(h['name'], _GA_METRIC.get(h.get('type'), FieldType.DOUBLE)) for h in metric_headers]
        out = []
        for r in rows:
            dims = [v.get('value') for v in r.get('dimensionValues', [])]
            dims = [f'{v[:4]}-{v[4:6]}-{v[6:]}' if h['name'] == 'date' and v and re.fullmatch(r'\d{8}', v) else v
                    for h, v in zip(dim_headers, dims)]
            out.append(dims + [v.get('value') for v in r.get('metricValues', [])])
        return {'output': Table.from_rows(Schema(fields), [[cast_value(v, f)[0] for v, f in zip(r, fields)] for r in out])}


TOOLS = [GoogleDriveInput, GoogleDriveOutput, GoogleSheetsInput, GoogleSheetsOutput, GoogleBigQueryInput,
         GoogleBigQueryOutput, GoogleAnalytics4Input]
