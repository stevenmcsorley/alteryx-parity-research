"""Box, Dropbox and Azure Data Lake Storage Gen2 file Input/Output through their REST APIs; no vendor SDKs.

Functional source: the public tool help for each connector and each service's API reference. Files are
read exactly as Input Data reads them and written exactly as Output Data writes them. Every tool uses a
Web/API connection with OAuth (flowengine/oauth.py):

* Box      base ``https://api.box.com/2.0``; token ``https://api.box.com/oauth2/token``; browser sign-in at
           ``https://account.box.com/api/oauth2/authorize``. Uploads go to Box's upload host; downloads are
           redirected to a pre-signed link that is fetched without the token.
* Dropbox  base ``https://api.dropboxapi.com/2``; token ``https://api.dropboxapi.com/oauth2/token``; browser
           sign-in at ``https://www.dropbox.com/oauth2/authorize``. File content goes through Dropbox's content host.
* ADLS     base ``https://<account>.dfs.core.windows.net``; Microsoft Entra token URL; scope
           ``https://storage.azure.com/.default``. Files are created, appended in chunks and flushed.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ._remote_files import read_bytes, written_file
from .github import _permit
from .msgraph import Graph, _NoRedirect

CHUNK = 8 * 1024 * 1024


def _provider(cfg, family):
    from .. import providers
    providers.resolve(cfg, family)


def _no_token_get(url, timeout):
    """GET a pre-signed URL the API redirected to, with no Authorization header."""
    if urllib.parse.urlsplit(url).scheme != 'https' and urllib.parse.urlsplit(url).hostname not in ('127.0.0.1', 'localhost'):
        raise DataError('The download link is not https')
    try:
        with urllib.request.build_opener(_NoRedirect).open(urllib.request.Request(url), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        e.close()
        raise DataError(f'Download link returned {e.code}') from None
    except (urllib.error.URLError, OSError) as e:
        raise DataError(f'Could not download the file: {getattr(e, "reason", None) or e}') from None


def _clean_path(path: str) -> str:
    p = '/' + path.strip().strip('/')
    if p == '/' or any(seg in ('.', '..') for seg in p.split('/')):
        raise ConfigError('Path must name a file, for example /Reports/sales.csv')
    return p


def _schema(extra, required):
    return {'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', *required],
            'properties': {'provider_semantics': {'type': 'string'},
                           'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web'},
                           'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600, 'default': 300},
                           **extra}}


_READ = {'read': {'type': 'object', 'default': {}, 'description': 'Input Data settings except path'}}
_WRITE = {'write': {'type': 'object', 'default': {}, 'description': 'Output Data settings except path'},
          'if_exists': {'enum': ['overwrite', 'error'], 'default': 'overwrite'}}


class _In(Tool):
    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None


class _Out(Tool):
    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        return {}


# --------------------------------------------------------------------------- Box

class Box(Graph):
    service = 'Box'
    extra_origins = ('https://upload.box.com/api/2.0',)
    upload_base = 'https://upload.box.com/api/2.0'


def _box_find(box, folder_id, name, ctx):
    """The file called name in the folder, or None (folder items are paged with offset and limit)."""
    offset = 0
    while True:
        ctx.check_cancelled()
        page = box.request('GET', f"folders/{urllib.parse.quote(folder_id, safe='')}/items?"
                                  + urllib.parse.urlencode({'fields': 'id,name,type', 'limit': '1000', 'offset': str(offset)}))
        for e in page.get('entries', []):
            if e.get('type') == 'file' and e.get('name') == name:
                return e['id']
        offset += len(page.get('entries', []))
        if not page.get('entries') or offset >= int(page.get('total_count', 0)):
            return None


_BOX_PROPS = {'provider': {'type': 'string', 'enum': ['box']},
              'file_id': {'type': ['string', 'null'], 'default': None},
              'folder_id': {'type': 'string', 'default': '0', 'description': 'Folder id; 0 is All Files'},
              'name': {'type': ['string', 'null'], 'default': None}}


class BoxInput(_In):
    manifest = ToolManifest(
        tool_id='core.box_input', version='1.0.0', display_name='Box Input', catalog_ids=('connectors:box-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Records read from the file'),),
        side_effects='network', deterministic=False,
        description='Read a Box file (by id, or by name in a folder) in any format Input Data reads.',
        config_schema=_schema({**_BOX_PROPS, **_READ}, []))

    def run(self, ctx, cfg, inputs):
        _provider(cfg, 'box')
        _permit(ctx)
        box = Box(cfg)
        fid = cfg['file_id'] or (cfg['name'] and _box_find(box, cfg['folder_id'], cfg['name'], ctx))
        if not fid:
            raise DataError(f"No Box file called {cfg['name']!r} in folder {cfg['folder_id']}" if cfg['name'] else 'Name the file (file_id or name)')
        meta = box.request('GET', f"files/{urllib.parse.quote(fid, safe='')}?fields=name")
        # Box answers 302 with a pre-signed download link; that link is fetched without the token.
        _, headers = box.request('GET', f"files/{urllib.parse.quote(fid, safe='')}/content", raw=True, with_headers=True,
                                 accept_incomplete=True, accept_redirect=True)
        link = headers.get('Location')
        data = _no_token_get(link, cfg['timeout_seconds']) if link else box.request('GET', f'files/{fid}/content', raw=True)
        return {'output': read_bytes(ctx, data, PurePosixPath(meta['name']).suffix, cfg['read'], meta['name'])}


class BoxOutput(_Out):
    manifest = ToolManifest(
        tool_id='core.box_output', version='1.0.0', display_name='Box Output', catalog_ids=('connectors:box-output',),
        inputs=(Anchor('input', 'I', description='Records to write'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Write records to a Box file (name in a folder) in any format Output Data writes; an existing file '
                    'gets a new version, or the run fails (if_exists error).',
        config_schema=_schema({**{k: v for k, v in _BOX_PROPS.items() if k != 'file_id'}, **_WRITE}, ['name']))

    def run(self, ctx, cfg, inputs):
        _provider(cfg, 'box')
        _permit(ctx, writes=True)
        box = Box(cfg)
        existing = _box_find(box, cfg['folder_id'], cfg['name'], ctx)
        if existing and cfg['if_exists'] == 'error':
            raise DataError(f"A file called {cfg['name']!r} already exists in folder {cfg['folder_id']}")
        with written_file(ctx, cfg['write'], inputs, PurePosixPath(cfg['name']).suffix) as local:
            data = local.read_bytes()
            boundary = 'flowbench' + __import__('secrets').token_hex(8)
            attributes = {'name': cfg['name']} if existing else {'name': cfg['name'], 'parent': {'id': cfg['folder_id']}}
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="attributes"\r\n\r\n{json.dumps(attributes)}\r\n'
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload"\r\n'
                    'Content-Type: application/octet-stream\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
            url = f"{Box.upload_base}/files/{urllib.parse.quote(existing, safe='')}/content" if existing else f'{Box.upload_base}/files/content'
            box.request('POST', url, body, {'Content-Type': f'multipart/form-data; boundary={boundary}'})
        ctx.info(f"Uploaded {len(inputs['input']):,} records to Box {cfg['name']} ({len(data):,} bytes)")
        return {}


# --------------------------------------------------------------------------- Dropbox

class Dropbox(Graph):
    service = 'Dropbox'
    extra_origins = ('https://content.dropboxapi.com/2',)
    content_base = 'https://content.dropboxapi.com/2'


def _dropbox_arg(value) -> str:
    # The Dropbox-API-Arg header is JSON with non-ASCII characters escaped, as the API requires.
    return json.dumps(value, ensure_ascii=True)


_DBX_PROPS = {'provider': {'type': 'string', 'enum': ['dropbox']},
              'path': {'type': 'string', 'minLength': 1, 'description': 'File path, for example /Reports/sales.csv'}}


class DropboxInput(_In):
    manifest = ToolManifest(
        tool_id='core.dropbox_input', version='1.0.0', display_name='Dropbox Input', catalog_ids=('connectors:dropbox-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Records read from the file'),),
        side_effects='network', deterministic=False,
        description='Read a Dropbox file by path in any format Input Data reads.',
        config_schema=_schema({**_DBX_PROPS, **_READ}, ['path']))

    def run(self, ctx, cfg, inputs):
        _provider(cfg, 'dropbox')
        _permit(ctx)
        path = _clean_path(cfg['path'])
        dbx = Dropbox(cfg)
        data = dbx.request('POST', f'{Dropbox.content_base}/files/download', b'',
                           {'Dropbox-API-Arg': _dropbox_arg({'path': path}), 'Content-Type': 'application/octet-stream'}, raw=True)
        return {'output': read_bytes(ctx, data, PurePosixPath(path).suffix, cfg['read'], path)}


class DropboxOutput(_Out):
    manifest = ToolManifest(
        tool_id='core.dropbox_output', version='1.0.0', display_name='Dropbox Output', catalog_ids=('connectors:dropbox-output',),
        inputs=(Anchor('input', 'I', description='Records to write'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Write records to a Dropbox file by path in any format Output Data writes; overwrite or fail when it '
                    'exists. Files over 8 MB use an upload session.',
        config_schema=_schema({**_DBX_PROPS, **_WRITE}, ['path']))

    def run(self, ctx, cfg, inputs):
        _provider(cfg, 'dropbox')
        _permit(ctx, writes=True)
        path = _clean_path(cfg['path'])
        dbx = Dropbox(cfg)
        mode = 'overwrite' if cfg['if_exists'] == 'overwrite' else 'add'
        commit = {'path': path, 'mode': mode, 'autorename': False, 'mute': True}
        with written_file(ctx, cfg['write'], inputs, PurePosixPath(path).suffix) as local:
            size = local.stat().st_size
            octet = {'Content-Type': 'application/octet-stream'}
            try:
                if size <= CHUNK:
                    dbx.request('POST', f'{Dropbox.content_base}/files/upload', local.read_bytes(),
                                {**octet, 'Dropbox-API-Arg': _dropbox_arg(commit)})
                else:
                    with open(local, 'rb') as fh:
                        first = fh.read(CHUNK)
                        session = dbx.request('POST', f'{Dropbox.content_base}/files/upload_session/start', first,
                                              {**octet, 'Dropbox-API-Arg': _dropbox_arg({'close': False})})['session_id']
                        offset = len(first)
                        while chunk := fh.read(CHUNK):
                            ctx.check_cancelled()
                            if offset + len(chunk) >= size:
                                break
                            dbx.request('POST', f'{Dropbox.content_base}/files/upload_session/append_v2', chunk,
                                        {**octet, 'Dropbox-API-Arg': _dropbox_arg({'cursor': {'session_id': session, 'offset': offset}})})
                            offset += len(chunk)
                        dbx.request('POST', f'{Dropbox.content_base}/files/upload_session/finish', chunk or b'',
                                    {**octet, 'Dropbox-API-Arg': _dropbox_arg({'cursor': {'session_id': session, 'offset': offset},
                                                                              'commit': commit})})
            except DataError as e:
                if 'conflict' in str(e):
                    raise DataError(f'A file already exists at {path}') from None
                raise
        ctx.info(f"Uploaded {len(inputs['input']):,} records to Dropbox {path} ({size:,} bytes)")
        return {}


# --------------------------------------------------------------------------- Azure Data Lake Storage Gen2

class DataLake(Graph):
    service = 'Azure Data Lake Storage'


ADLS_VERSION = {'x-ms-version': '2023-11-03'}
_FS = re.compile(r'^[a-z0-9](?!.*--)[a-z0-9-]{1,61}[a-z0-9]$')
_ADLS_PROPS = {'provider': {'type': 'string', 'enum': ['azure-data-lake']},
               'file_system': {'type': 'string', 'minLength': 3, 'description': 'Container (file system) name'},
               'path': {'type': 'string', 'minLength': 1, 'description': 'File path inside the file system, e.g. raw/2026/sales.parquet'}}


def _adls_url(cfg):
    if not _FS.match(cfg['file_system']):
        raise ConfigError(f"{cfg['file_system']!r} is not a valid file system (container) name")
    path = _clean_path(cfg['path'])
    return f"{cfg['file_system']}{urllib.parse.quote(path, safe='/')}"


class AzureDataLakeInput(_In):
    manifest = ToolManifest(
        tool_id='core.azure_data_lake_input', version='1.0.0', display_name='Azure Data Lake File Input',
        catalog_ids=('connectors:azure-data-lake-file-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Records read from the file'),),
        side_effects='network', deterministic=False,
        description='Read a file from Azure Data Lake Storage Gen2 in any format Input Data reads.',
        config_schema=_schema({**_ADLS_PROPS, **_READ}, ['file_system', 'path']))

    def run(self, ctx, cfg, inputs):
        _provider(cfg, 'azure-data-lake')
        _permit(ctx)
        url = _adls_url(cfg)
        data = DataLake(cfg).request('GET', url, None, ADLS_VERSION, raw=True)
        return {'output': read_bytes(ctx, data, PurePosixPath(cfg['path']).suffix, cfg['read'], cfg['path'])}


class AzureDataLakeOutput(_Out):
    manifest = ToolManifest(
        tool_id='core.azure_data_lake_output', version='1.0.0', display_name='Azure Data Lake File Output',
        catalog_ids=('connectors:azure-data-lake-file-output',),
        inputs=(Anchor('input', 'I', description='Records to write'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Write records to a file in Azure Data Lake Storage Gen2 in any format Output Data writes: the file '
                    'is created (replacing it, or failing when it exists), appended in chunks and flushed.',
        config_schema=_schema({**_ADLS_PROPS, **_WRITE}, ['file_system', 'path']))

    def run(self, ctx, cfg, inputs):
        _provider(cfg, 'azure-data-lake')
        _permit(ctx, writes=True)
        url = _adls_url(cfg)
        lake = DataLake(cfg)
        with written_file(ctx, cfg['write'], inputs, PurePosixPath(cfg['path']).suffix) as local:
            size = local.stat().st_size
            create = {**ADLS_VERSION, **({'If-None-Match': '*'} if cfg['if_exists'] == 'error' else {})}
            try:
                lake.request('PUT', f'{url}?resource=file', b'', create)
            except DataError as e:
                if 'PathAlreadyExists' in str(e) or ' 409' in str(e):
                    raise DataError(f"A file already exists at {cfg['path']}") from None
                raise
            position = 0
            with open(local, 'rb') as fh:
                while chunk := fh.read(CHUNK):
                    ctx.check_cancelled()
                    lake.request('PATCH', f'{url}?action=append&position={position}', chunk,
                                 {**ADLS_VERSION, 'Content-Type': 'application/octet-stream'})
                    position += len(chunk)
            lake.request('PATCH', f'{url}?action=flush&position={position}', b'', ADLS_VERSION)
        ctx.info(f"Uploaded {len(inputs['input']):,} records to {cfg['file_system']}/{cfg['path']} ({size:,} bytes)")
        return {}


TOOLS = [BoxInput, BoxOutput, DropboxInput, DropboxOutput, AzureDataLakeInput, AzureDataLakeOutput]
