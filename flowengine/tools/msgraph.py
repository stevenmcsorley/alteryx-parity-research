"""Microsoft 365 through Microsoft Graph (v1.0): OneDrive and SharePoint files, SharePoint lists, Outlook mail.

Functional source: the public tool help for each connector and the Microsoft Graph REST reference.
Every tool uses a saved Web/API connection with OAuth (flowengine/oauth.py): base URL
``https://graph.microsoft.com/v1.0``, token URL ``https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token``,
and either an application secret (app-only access with ``.default`` scope) or a refresh token from
the device sign-in. Responses are paged with ``@odata.nextLink``; throttling (429/503) waits for
``Retry-After``; a 401 renews the token once.

Files are read exactly as Input Data reads them and written exactly as Output Data writes them.
Uploads above 4 MB use an upload session: Graph returns a pre-authenticated upload URL, which
receives only the file bytes and never the access token.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, cast_value, format_value, type_for_values
from .download import UNBOUNDED_TEXT, _HttpOnlyRedirects
from .github import _permit

SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
CHUNK = 320 * 1024 * 20          # upload-session chunks must be multiples of 320 KiB
MAX_RETRIES = 4


class Graph:
    """A JSON API behind an OAuth connection with OData paging (Microsoft Graph, Dataverse, Power BI)."""
    service = 'Microsoft Graph'

    def __init__(self, cfg):
        from ..oauth import OAuthSession
        from ..service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        self.conn = store.get(cfg['connection_id'], kind='web')
        if self.conn.auth != 'oauth2':
            raise ConfigError(f'{self.service} connections use OAuth: an app secret or a device sign-in')
        self.oauth = OAuthSession(self.conn, store.secret(self.conn), store)
        self.base = self.conn.base_url.rstrip('/')
        self.timeout = cfg.get('timeout_seconds', 120)
        self.opener = urllib.request.build_opener(_HttpOnlyRedirects(self.conn))

    def url(self, path: str) -> str:
        return path if path.startswith('https://') or path.startswith('http://') else f'{self.base}/{path.lstrip("/")}'

    def request(self, method, path, body=None, headers=None, raw=False, with_headers=False):
        url = self.url(path)
        if not self.conn.covers(url):
            raise ConfigError(f'{url} is outside the connection base URL')
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        renewed, renew_now = False, False
        for attempt in range(MAX_RETRIES + 1):
            h = {'Accept': 'application/json', 'Authorization': f'Bearer {self.oauth.access_token(refresh=renew_now)}',
                 **(headers or {})}
            renew_now = False
            if data is not None and not isinstance(body, bytes):
                h['Content-Type'] = 'application/json'
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    content = r.read()
                    result = content if raw else (json.loads(content) if content.strip() else None)
                    return (result, r.headers) if with_headers else result
            except urllib.error.HTTPError as e:
                with e:
                    detail = e.read(64 * 1024)
                retry = e.headers.get('Retry-After')
                if e.code == 401 and not renewed:
                    renewed = renew_now = True  # one fresh token; later retries reuse it
                    continue
                if e.code in (429, 503) and attempt < MAX_RETRIES:
                    time.sleep(min(float(retry) if retry and retry.isdigit() else 2 ** attempt, 60))
                    continue
                raise DataError(f'{self.service} {method} {urllib.parse.urlsplit(url).path} returned {e.code}: '
                                f'{_graph_error(detail)}') from None
            except (urllib.error.URLError, OSError) as e:
                raise DataError(f'Could not reach {self.service}: {getattr(e, "reason", None) or e}') from None
        raise DataError(f'{self.service} kept throttling the request')

    def paged(self, path, ctx, limit=0, headers=None):
        url, count, seen = path, 0, set()
        while url:
            ctx.check_cancelled()
            if url in seen:
                raise DataError(f'{self.service} returned a next page that was already read; stopping to avoid a loop')
            seen.add(url)
            page = self.request('GET', url, headers=headers) or {}
            for item in page.get('value', []):
                yield item
                count += 1
                if limit and count >= limit:
                    return
            url = page.get('@odata.nextLink')


def _graph_error(detail: bytes) -> str:
    try:
        err = json.loads(detail).get('error', {})
        return f"{err.get('code', '')}: {err.get('message', '')}".strip(': ')
    except (ValueError, AttributeError):
        return detail[:300].decode('utf-8', 'replace')


def _seg(value: str) -> str:
    return urllib.parse.quote(value, safe='')


# --------------------------------------------------------------------------- drive locations

def _drive_root(cfg) -> str:
    kind = cfg['location']
    if kind == 'me':
        root = 'me/drive'
    elif kind == 'user':
        if not cfg.get('user'):
            raise ConfigError('Name the user (user principal name or id) whose OneDrive to use')
        root = f"users/{_seg(cfg['user'])}/drive"
    else:
        site = cfg.get('site') or ''
        if not site:
            raise ConfigError('Name the SharePoint site, for example contoso.sharepoint.com:/sites/Sales: or a site id')
        # hostname:/server-relative-path: addressing keeps the colons; ids are opaque
        site_ref = urllib.parse.quote(site, safe=':/,.-')
        root = f'sites/{site_ref}/drives/{_seg(cfg["drive"])}' if cfg.get('drive') else f'sites/{site_ref}/drive'
    return root


def _item_path(cfg) -> str:
    path = cfg['path'].strip('/')
    if not path or any(p in ('.', '..') for p in path.split('/')):
        raise ConfigError('File path must name a file inside the drive, for example Reports/2026/sales.xlsx')
    return '/'.join(_seg(p) for p in path.split('/'))


_FILE_COMMON = {
    'provider': {'type': 'string', 'enum': ['microsoft-graph']},
    'provider_semantics': {'type': 'string'},
    'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                      'description': 'Saved Microsoft Graph connection (OAuth)'},
    'path': {'type': 'string', 'minLength': 1, 'description': 'File path inside the drive, for example Reports/sales.xlsx'},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600, 'default': 300},
}


def _file_props(locations):
    props = {**_FILE_COMMON, 'location': {'enum': list(locations), 'default': locations[0]}}
    if 'user' in locations:
        props['user'] = {'type': ['string', 'null'], 'default': None, 'description': 'User principal name, for location user'}
    if 'site' in locations:
        props['site'] = {'type': ['string', 'null'], 'default': None,
                         'description': 'Site, for example contoso.sharepoint.com:/sites/Sales: (or a site id)'}
        props['drive'] = {'type': ['string', 'null'], 'default': None, 'description': 'Document library id (default library if empty)'}
    return props


class _FileInput(Tool):
    locations = ('me', 'user')

    def infer(self, ctx, cfg, inputs):
        return None

    def effects(self, cfg):
        return {'network_requests'}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        from .inout import InputData
        providers.resolve(cfg, 'microsoft-365')
        if 'path' in cfg['read']:
            raise ConfigError('read holds format settings; the file is named by path')
        _permit(ctx)
        g = Graph(cfg)
        data = g.request('GET', f'{_drive_root(cfg)}/root:/{_item_path(cfg)}:/content', raw=True)
        reader = InputData()
        with tempfile.TemporaryDirectory(prefix='flowbench-graph-') as d:
            local = Path(d) / f'file{Path(cfg["path"]).suffix}'
            local.write_bytes(data)
            ctx.info(f"Downloaded {cfg['path']} ({len(data):,} bytes)")
            return {'output': reader.run(ctx, reader.normalize_config({**cfg['read'], 'path': str(local)}), {})['output']}


class _FileOutput(Tool):
    locations = ('me', 'user')

    def infer(self, ctx, cfg, inputs):
        return {}

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        from .inout import OutputData
        providers.resolve(cfg, 'microsoft-365')
        if 'path' in cfg['write']:
            raise ConfigError('write holds format settings; the file is named by path')
        _permit(ctx, writes=True)
        g = Graph(cfg)
        target = f'{_drive_root(cfg)}/root:/{_item_path(cfg)}:'
        writer = OutputData()
        with tempfile.TemporaryDirectory(prefix='flowbench-graph-') as d:
            local = Path(d) / f'upload{Path(cfg["path"]).suffix}'
            writer.run(ctx, writer.normalize_config({**cfg['write'], 'path': str(local)}), inputs)
            size = local.stat().st_size
            conflict = 'replace' if cfg['if_exists'] == 'overwrite' else 'fail'
            if size <= SIMPLE_UPLOAD_MAX:
                g.request('PUT', f'{target}/content?@microsoft.graph.conflictBehavior={conflict}', local.read_bytes(),
                          {'Content-Type': 'application/octet-stream'})
            else:
                session = g.request('POST', f'{target}/createUploadSession',
                                    {'item': {'@microsoft.graph.conflictBehavior': conflict}})
                _upload_session(ctx, session['uploadUrl'], local, size, cfg['timeout_seconds'])
        ctx.info(f"Uploaded {len(inputs['input']):,} records to {cfg['path']} ({size:,} bytes)")
        return {}


def _upload_session(ctx, upload_url, local, size, timeout):
    """PUT the file in ranges to the pre-authenticated upload URL; no Authorization header is sent there."""
    parts = urllib.parse.urlsplit(upload_url)
    if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')):
        raise DataError('Microsoft Graph returned an upload URL that is not https')
    with open(local, 'rb') as fh:
        start = 0
        while start < size:
            ctx.check_cancelled()
            chunk = fh.read(CHUNK)
            end = start + len(chunk) - 1
            req = urllib.request.Request(upload_url, data=chunk, method='PUT', headers={
                'Content-Length': str(len(chunk)), 'Content-Range': f'bytes {start}-{end}/{size}'})
            try:
                with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
                    r.read()
            except urllib.error.HTTPError as e:
                with e:
                    detail = e.read(64 * 1024)
                raise DataError(f'Upload session failed at byte {start}: {e.code} {_graph_error(detail)}') from None
            start = end + 1


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _file_tools(prefix, name, catalog_in, catalog_out, locations, where):
    read_props = {**_file_props(locations), 'read': {'type': 'object', 'default': {}, 'description': 'Input Data settings except path'}}
    write_props = {**_file_props(locations), 'write': {'type': 'object', 'default': {}, 'description': 'Output Data settings except path'},
                   'if_exists': {'enum': ['overwrite', 'error'], 'default': 'overwrite'}}

    class Input(_FileInput):
        manifest = ToolManifest(
            tool_id=f'core.{prefix}_input', version='1.0.0', display_name=f'{name} Input', catalog_ids=(catalog_in,),
            inputs=(), outputs=(Anchor('output', 'O', description='Records read from the file'),),
            side_effects='network', deterministic=False,
            description=f'Read a file from {where} through Microsoft Graph in any format Input Data reads (CSV and other '
                        'delimited text, Excel, JSON, Parquet, ...). read holds the Input Data settings.',
            config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'path'],
                           'properties': read_props})

    class Output(_FileOutput):
        manifest = ToolManifest(
            tool_id=f'core.{prefix}_output', version='1.0.0', display_name=f'{name} Output', catalog_ids=(catalog_out,),
            inputs=(Anchor('input', 'I', description='Records to write'),), outputs=(),
            side_effects='network', deterministic=False,
            description=f'Write records to a file in {where} through Microsoft Graph, in any format Output Data writes. '
                        'Files over 4 MB use an upload session.',
            config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'path'],
                           'properties': write_props})

    Input.__name__ = Input.__qualname__ = f'{prefix.title().replace("_", "")}Input'
    Output.__name__ = Output.__qualname__ = f'{prefix.title().replace("_", "")}Output'
    return Input, Output


OneDriveInput, OneDriveOutput = _file_tools('onedrive', 'OneDrive', 'connectors:onedrive-input', 'connectors:onedrive-output',
                                            ('me', 'user'), 'OneDrive')
SharePointFilesInput, SharePointFilesOutput = _file_tools(
    'sharepoint_files', 'SharePoint Files', 'connectors:sharepoint-files-input', 'connectors:sharepoint-files-output',
    ('site',), 'a SharePoint document library')


# --------------------------------------------------------------------------- Outlook 365

MAIL_FIELDS = [('Id', 'text'), ('ReceivedDateTime', 'datetime'), ('FromName', 'text'), ('FromAddress', 'text'),
               ('To', 'text'), ('Cc', 'text'), ('Subject', 'text'), ('BodyPreview', 'text'), ('Body', 'text'),
               ('HasAttachments', 'bool'), ('IsRead', 'bool'), ('Importance', 'text'), ('ConversationId', 'text'),
               ('WebLink', 'text')]


def _mail_schema():
    types = {'text': (FieldType.V_WSTRING, UNBOUNDED_TEXT), 'datetime': (FieldType.DATETIME, None), 'bool': (FieldType.BOOL, None)}
    return Schema([Field(n, *types[t]) for n, t in MAIL_FIELDS])


def _addresses(items):
    return '; '.join(x.get('emailAddress', {}).get('address', '') for x in items or []) or None


class Outlook365Input(Tool):
    manifest = ToolManifest(
        tool_id='core.outlook365_input', version='1.0.0', display_name='Outlook 365 Input', catalog_ids=('connectors:outlook-365-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per message'),),
        side_effects='network', deterministic=False,
        description='Read messages from an Outlook 365 mail folder through Microsoft Graph: newest first, with an optional '
                    'OData filter (for example isRead eq false) or search text. Body is plain text.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id'], 'properties': {
            'provider': {'type': 'string', 'enum': ['microsoft-graph']},
            'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web'},
            'mailbox': {'type': ['string', 'null'], 'default': None, 'description': 'User principal name; empty for the signed-in user'},
            'folder': {'type': 'string', 'minLength': 1, 'default': 'inbox', 'description': 'Well-known name (inbox, sentitems, ...) or folder id'},
            'filter': {'type': ['string', 'null'], 'default': None, 'description': 'OData $filter, for example isRead eq false'},
            'search': {'type': ['string', 'null'], 'default': None, 'description': 'Search text (Graph $search)'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 500},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return {'output': _mail_schema()}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'microsoft-365')
        if cfg['filter'] and cfg['search']:
            raise ConfigError('Graph does not combine $filter with $search; use one of them')
        _permit(ctx)
        g = Graph(cfg)
        owner = f"users/{_seg(cfg['mailbox'])}" if cfg['mailbox'] else 'me'
        q = {'$top': str(min(cfg['limit'], 100)),
             '$select': 'id,receivedDateTime,from,toRecipients,ccRecipients,subject,bodyPreview,body,hasAttachments,isRead,'
                        'importance,conversationId,webLink'}
        if cfg['search']:
            q['$search'] = json.dumps(cfg['search'])
        else:
            q['$orderby'] = 'receivedDateTime desc'
            if cfg['filter']:
                q['$filter'] = cfg['filter']
        url = f"{owner}/mailFolders/{_seg(cfg['folder'])}/messages?" + urllib.parse.urlencode(q)
        schema = _mail_schema()
        rows = []
        for m in g.paged(url, ctx, cfg['limit'], headers={'Prefer': 'outlook.body-content-type="text"'}):
            sender = (m.get('from') or {}).get('emailAddress', {})
            received = m.get('receivedDateTime')
            rows.append([m.get('id'), received.replace('T', ' ').rstrip('Z') if received else None, sender.get('name'),
                         sender.get('address'), _addresses(m.get('toRecipients')), _addresses(m.get('ccRecipients')),
                         m.get('subject'), m.get('bodyPreview'), (m.get('body') or {}).get('content'), m.get('hasAttachments'),
                         m.get('isRead'), m.get('importance'), m.get('conversationId'), m.get('webLink')])
        return {'output': Table.from_rows(schema, [[cast_value(v, f)[0] for v, f in zip(r, schema)] for r in rows])}


# --------------------------------------------------------------------------- SharePoint lists

_LIST_COMMON = {
    'provider': {'type': 'string', 'enum': ['microsoft-graph']},
    'provider_semantics': {'type': 'string'},
    'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web'},
    'site': {'type': 'string', 'minLength': 1, 'description': 'Site, for example contoso.sharepoint.com:/sites/Sales: (or a site id)'},
    'list': {'type': 'string', 'minLength': 1, 'description': 'List display name or id'},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120},
}
_SYSTEM_FIELDS = {'@odata.etag', 'ContentType', 'Attachments', 'Edit', 'LinkTitleNoMenu', 'LinkTitle', 'ItemChildCount',
                  'FolderChildCount', '_ComplianceFlags', '_ComplianceTag', '_ComplianceTagWrittenTime',
                  '_ComplianceTagUserId', '_UIVersionString', 'AppAuthorLookupId', 'AppEditorLookupId', 'DocIcon'}


def _list_base(cfg):
    return f"sites/{urllib.parse.quote(cfg['site'], safe=':/,.-')}/lists/{_seg(cfg['list'])}"


class SharePointListInput(Tool):
    manifest = ToolManifest(
        tool_id='core.sharepoint_list_input', version='1.0.0', display_name='SharePoint List Input',
        catalog_ids=('connectors:sharepoint-list-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per list item'),),
        side_effects='network', deterministic=False,
        description='Read the items of a SharePoint list through Microsoft Graph: the item id plus its column values '
                    '(choose columns, or all user columns). Lookup and person columns arrive as their ids or JSON text.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'site', 'list'],
                       'properties': {**_LIST_COMMON,
            'columns': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'uniqueItems': True, 'default': [],
                        'description': 'Internal column names; empty reads every user column'},
            'filter': {'type': ['string', 'null'], 'default': None, 'description': 'OData filter on fields, e.g. fields/Region eq \'North\''},
            'limit': {'type': 'integer', 'minimum': 0, 'default': 0}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'microsoft-365')
        _permit(ctx)
        g = Graph(cfg)
        expand = f"fields(select={','.join(cfg['columns'])})" if cfg['columns'] else 'fields'
        q = {'expand': expand, '$top': '999'}
        headers = None
        if cfg['filter']:
            q['$filter'] = cfg['filter']
            headers = {'Prefer': 'HonorNonIndexedQueriesWarningMayFailRandomly'}
        records = []
        for item in g.paged(f'{_list_base(cfg)}/items?' + urllib.parse.urlencode(q), ctx, cfg['limit'], headers=headers):
            fields = {k: v for k, v in (item.get('fields') or {}).items() if k not in _SYSTEM_FIELDS and not k.startswith('@')}
            records.append({'ItemId': item.get('id'), **{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
                                                          for k, v in fields.items()}})
        names = ['ItemId'] + (list(cfg['columns']) if cfg['columns'] else [])
        for r in records:
            names.extend(k for k in r if k not in names)
        fields = []
        for n in names:
            t = FieldType.V_WSTRING if n == 'ItemId' else type_for_values([_list_value(r.get(n)) for r in records])
            fields.append(Field(n, t, UNBOUNDED_TEXT if t.is_string else None))
        cols = [[cast_value(_list_value(r.get(f.name)), f)[0] for r in records] for f in fields]
        return {'output': Table(Schema(fields), cols)}


def _list_value(v):
    """Graph returns dates as ISO text with Z; keep them as datetimes without the zone (they are UTC)."""
    if isinstance(v, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', v):
        import datetime as _dt
        return _dt.datetime.fromisoformat(v[:-1])
    return v


class SharePointListOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.sharepoint_list_output', version='1.0.0', display_name='SharePoint List Output',
        catalog_ids=('connectors:sharepoint-list-output',),
        inputs=(Anchor('input', 'I', description='Records to write as list items'),),
        outputs=(Anchor('output', 'O', description='Each record with the id of the item written'),),
        side_effects='network', deterministic=False,
        description='Write records to a SharePoint list through Microsoft Graph: append new items, update items by id, or '
                    'delete items by id (needs confirm_delete). Record fields are list column internal names.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'site', 'list'],
                       'properties': {**_LIST_COMMON,
            'mode': {'enum': ['append', 'update', 'delete'], 'default': 'append'},
            'id_field': {'type': 'string', 'minLength': 1, 'default': 'ItemId', 'description': 'Field holding the item id (update, delete)'},
            'confirm_delete': {'type': 'boolean', 'default': False}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        if cfg['mode'] != 'append' and cfg['id_field'] not in schema:
            raise ConfigError(f"{cfg['mode'].capitalize()} needs the item id field {cfg['id_field']!r}")
        out = [f for f in schema if f.name != 'WrittenItemId']
        return {'output': Schema([*out, Field('WrittenItemId', FieldType.V_WSTRING, UNBOUNDED_TEXT)])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'microsoft-365')
        table: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        if cfg['mode'] == 'delete' and not cfg['confirm_delete']:
            raise ConfigError('Delete removes list items; set confirm_delete to run it')
        _permit(ctx, writes=True)
        g = Graph(cfg)
        base = _list_base(cfg)
        out = []
        for rec in table.to_dicts():
            ctx.check_cancelled()
            values = {k: _json_value(v) for k, v in rec.items() if k not in (cfg['id_field'], 'WrittenItemId')}
            if cfg['mode'] == 'append':
                item_id = (g.request('POST', f'{base}/items', {'fields': values}) or {}).get('id')
            else:
                item_id = rec[cfg['id_field']]
                if item_id in (None, ''):
                    raise DataError(f"A record has no {cfg['id_field']}; {cfg['mode']} needs the item id")
                item_id = format_value(item_id)
                if cfg['mode'] == 'update':
                    g.request('PATCH', f'{base}/items/{_seg(item_id)}/fields', values)
                else:
                    g.request('DELETE', f'{base}/items/{_seg(item_id)}')
            out.append([rec.get(f.name) for f in schema if f.name != 'WrittenItemId'] + [item_id])
        ctx.info(f"{cfg['mode']}: {len(out)} list item(s)")
        return {'output': Table.from_rows(schema, out)}


def _json_value(v):
    import datetime as _dt
    from decimal import Decimal
    if isinstance(v, _dt.datetime):
        return v.strftime('%Y-%m-%dT%H:%M:%SZ')
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        raise DataError('SharePoint list columns cannot hold binary values')
    return v


TOOLS = [OneDriveInput, OneDriveOutput, SharePointFilesInput, SharePointFilesOutput, Outlook365Input,
         SharePointListInput, SharePointListOutput]
