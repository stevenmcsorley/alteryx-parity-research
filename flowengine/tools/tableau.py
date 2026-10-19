"""Tableau Output and Publish to Tableau Server: Hyper extracts written locally and/or published; no vendor code.

Functional source: the public tool help and Tableau's REST API and Hyper API documentation. Extracts are written
with Tableau's Hyper API (``tableauhyperapi``, Apache-2.0 on PyPI), which the user installs (it has wheels for
Windows and Linux x86-64 only); Flowbench does not bundle it. Spatial fields given as WKT or GeoJSON become
GEOGRAPHY columns.

Publishing uses the Tableau REST API: sign in with a personal access token (a Web/API connection with basic
authentication: the token name as username, the token secret as the secret, base URL the Tableau Server or Tableau
Cloud address), find the project, and publish the .hyper as a data source (overwrite or append), using a chunked
file upload above 64 MB. The session is signed out at the end.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import quoteattr

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..types import FieldType
from .download import _HttpOnlyRedirects
from .github import _permit

SINGLE_REQUEST_MAX = 64 * 1024 * 1024
UPLOAD_CHUNK = 32 * 1024 * 1024


def _hyper():
    try:
        import tableauhyperapi
    except ImportError:
        raise ConfigError('Writing Tableau extracts needs the Tableau Hyper API: pip install tableauhyperapi into the '
                          "engine's Python (Windows or Linux x86-64; see Settings > Integrations)") from None
    return tableauhyperapi


def _sql_type(h, f, spatial):
    t = f.type
    if f.name in spatial:
        return h.SqlType.geography()
    if t is FieldType.BOOL:
        return h.SqlType.bool()
    if t in (FieldType.BYTE, FieldType.INT16):
        return h.SqlType.small_int()
    if t is FieldType.INT32:
        return h.SqlType.int()
    if t is FieldType.INT64:
        return h.SqlType.big_int()
    if t is FieldType.FIXED_DECIMAL:
        return h.SqlType.numeric(min(f.size or 18, 38), f.scale or 0)
    if t in (FieldType.FLOAT, FieldType.DOUBLE):
        return h.SqlType.double()
    if t is FieldType.DATE:
        return h.SqlType.date()
    if t is FieldType.DATETIME:
        return h.SqlType.timestamp()
    if t is FieldType.TIME:
        return h.SqlType.time()
    if t is FieldType.BLOB:
        return h.SqlType.bytes()
    return h.SqlType.text()


def _wkt(value):
    if value is None:
        return None
    from .spatial import parse
    geom = parse(value, 'error', '')
    return None if geom is None else geom.wkt


def write_hyper(table, path: Path, spatial_fields=(), table_name='Extract'):
    """A .hyper file with one table (schema Extract) holding the records."""
    h = _hyper()
    spatial = set(spatial_fields)
    missing = spatial - set(table.schema.names)
    if missing:
        raise ConfigError(f'Spatial field(s) {sorted(missing)} are not in the input')
    name = h.TableName('Extract', table_name)
    columns = [h.TableDefinition.Column(f.name, _sql_type(h, f, spatial)) for f in table.schema]
    definition = h.TableDefinition(name, columns)
    # Hyper writes a server log; keep it in a temporary folder that is removed afterwards.
    with tempfile.TemporaryDirectory(prefix='flowbench-hyperd-') as log_dir, \
            h.HyperProcess(telemetry=h.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={'log_dir': log_dir}) as process, \
            h.Connection(process.endpoint, str(path), h.CreateMode.CREATE_AND_REPLACE) as conn:
        conn.catalog.create_schema_if_not_exists('Extract')
        conn.catalog.create_table(definition)
        # spatial values arrive as WKT text and are cast to GEOGRAPHY by Hyper
        inserter_cols = [h.TableDefinition.Column(f.name if f.name not in spatial else f'__wkt_{f.name}',
                                                  h.SqlType.text() if f.name in spatial else _sql_type(h, f, spatial))
                         for f in table.schema]
        mapping = [h.Inserter.ColumnMapping(f.name, f'CAST({h.escape_name("__wkt_" + f.name)} AS GEOGRAPHY)')
                   if f.name in spatial else h.Inserter.ColumnMapping(f.name) for f in table.schema]
        with h.Inserter(conn, definition, mapping, inserter_definition=inserter_cols) as ins:
            for row in table.rows():
                ins.add_row([_wkt(v) if f.name in spatial else _hyper_value(v, f) for v, f in zip(row, table.schema)])
            ins.execute()


def _hyper_value(v, f):
    if v is None:
        return None
    if f.type is FieldType.FIXED_DECIMAL and not isinstance(v, Decimal):
        return Decimal(str(v))
    return v


# --------------------------------------------------------------------------- REST publishing

class Tableau:
    def __init__(self, cfg):
        from ..service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        self.conn = store.get(cfg['connection_id'], kind='web')
        if self.conn.auth != 'basic':
            raise ConfigError('A Tableau connection uses a personal access token: its name as username and its secret '
                              'as the secret (basic authentication)')
        self.secret = store.secret(self.conn)
        self.api = f"{self.conn.base_url.rstrip('/')}/api/{cfg['api_version']}"
        self.timeout = cfg['timeout_seconds']
        self.opener = urllib.request.build_opener(_HttpOnlyRedirects(self.conn))
        self.token = self.site_id = None
        self.site = cfg['site']

    def request(self, method, path, body=None, content_type='application/json'):
        url = f'{self.api}/{path}'
        if not self.conn.covers(url):
            raise ConfigError(f'{url} is outside the connection base URL')
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['X-Tableau-Auth'] = self.token
        if data is not None:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                content = r.read()
                return json.loads(content) if content.strip() else None
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read(64 * 1024)
            try:
                err = json.loads(detail).get('error', {})
                message = f"{err.get('code', '')} {err.get('summary', '')}: {err.get('detail', '')}".strip()
            except (ValueError, AttributeError):
                message = detail[:300].decode('utf-8', 'replace')
            raise DataError(f'Tableau {method} {path.split("?")[0]} returned {e.code}: {message}') from None
        except (urllib.error.URLError, OSError) as e:
            raise DataError(f'Could not reach Tableau: {getattr(e, "reason", None) or e}') from None

    def __enter__(self):
        doc = self.request('POST', 'auth/signin', {'credentials': {'personalAccessTokenName': self.conn.username,
                                                                   'personalAccessTokenSecret': self.secret,
                                                                   'site': {'contentUrl': self.site}}})
        cred = doc['credentials']
        self.token, self.site_id = cred['token'], cred['site']['id']
        return self

    def __exit__(self, *exc):
        try:
            self.request('POST', 'auth/signout')
        except DataError:
            pass
        self.token = None

    def project_id(self, name):
        q = urllib.parse.urlencode({'filter': f'name:eq:{name}'})
        found = ((self.request('GET', f'sites/{self.site_id}/projects?{q}') or {}).get('projects') or {}).get('project', [])
        match = [p for p in found if p.get('name') == name]
        if not match:
            raise DataError(f'No Tableau project called {name!r} on this site')
        return match[0]['id']

    def publish(self, ctx, hyper: Path, name: str, project: str, mode: str):
        pid = self.project_id(project)
        payload = (f'<tsRequest><datasource name={quoteattr(name)}><project id={quoteattr(pid)}/></datasource></tsRequest>').encode()
        flag = 'overwrite=true' if mode == 'overwrite' else 'append=true'
        size = hyper.stat().st_size
        if size <= SINGLE_REQUEST_MAX:
            body, ctype = _multipart([('request_payload', None, 'text/xml', payload),
                                      ('tableau_datasource', f'{_safe_file(name)}.hyper', 'application/octet-stream', hyper.read_bytes())])
            doc = self.request('POST', f'sites/{self.site_id}/datasources?{flag}', body, ctype)
        else:
            upload = self.request('POST', f'sites/{self.site_id}/fileUploads')['fileUpload']['uploadSessionId']
            with open(hyper, 'rb') as fh:
                while chunk := fh.read(UPLOAD_CHUNK):
                    ctx.check_cancelled()
                    body, ctype = _multipart([('request_payload', None, 'text/xml', b''),
                                              ('tableau_file', 'file', 'application/octet-stream', chunk)])
                    self.request('PUT', f'sites/{self.site_id}/fileUploads/{upload}', body, ctype)
            body, ctype = _multipart([('request_payload', None, 'text/xml', payload)])
            doc = self.request('POST', f'sites/{self.site_id}/datasources?uploadSessionId={upload}&datasourceType=hyper&{flag}',
                               body, ctype)
        return (doc or {}).get('datasource', {})


def _safe_file(name):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', name)[:100] or 'extract'


def _multipart(parts):
    boundary = uuid.uuid4().hex
    out = b''
    for field, filename, ctype, data in parts:
        disp = f'form-data; name="{field}"' + (f'; filename="{filename}"' if filename else '')
        out += f'--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n'.encode() + data + b'\r\n'
    out += f'--{boundary}--\r\n'.encode()
    return out, f'multipart/mixed; boundary={boundary}'


_PUBLISH = {
    'connection_id': {'type': ['string', 'null'], 'default': None, 'x-flowbench-connection': 'web',
                      'description': 'Saved Tableau connection (personal access token)'},
    'site': {'type': 'string', 'default': '', 'description': 'Site content URL; empty for the Default site'},
    'project': {'type': 'string', 'default': 'Default'},
    'datasource': {'type': ['string', 'null'], 'default': None, 'description': 'Published data source name'},
    'mode': {'enum': ['overwrite', 'append'], 'default': 'overwrite'},
    'api_version': {'type': 'string', 'pattern': '^3\\.[0-9]{1,2}$', 'default': '3.21'},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600, 'default': 600},
}
_COMMON = {'provider': {'type': 'string', 'enum': ['tableau']}, 'provider_semantics': {'type': 'string'},
           'spatial_fields': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True, 'default': [],
                              'description': 'Fields holding WKT or GeoJSON, written as GEOGRAPHY'},
           'table_name': {'type': 'string', 'minLength': 1, 'default': 'Extract'}}


def _publish(ctx, cfg, local):
    if not cfg['connection_id'] or not cfg['datasource']:
        raise ConfigError('Publishing needs connection_id and datasource')
    _permit(ctx, writes=True)
    with Tableau(cfg) as tab:
        ds = tab.publish(ctx, local, cfg['datasource'], cfg['project'], cfg['mode'])
    ctx.info(f"published {cfg['datasource']!r} to project {cfg['project']!r} ({cfg['mode']}); id {ds.get('id')}")


class TableauOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.tableau_output', version='1.0.0', display_name='Tableau Output', catalog_ids=('connectors:tableau-output',),
        inputs=(Anchor('input', 'I', description='Records to write'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Write records as a Tableau Hyper extract to a local file and/or publish it to Tableau Server or '
                    'Tableau Cloud as a data source (overwrite or append). Spatial fields (WKT/GeoJSON) become GEOGRAPHY.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'output'], 'properties': {
            **_COMMON, **_PUBLISH,
            'output': {'enum': ['local', 'publish', 'both'], 'description': 'Write a .hyper file, publish it, or both'},
            'path': {'type': ['string', 'null'], 'default': None, 'description': 'Local .hyper file (local or both)'}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'} if cfg.get('output') in ('publish', 'both') else set()

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'tableau')
        if cfg['output'] in ('local', 'both') and not cfg['path']:
            raise ConfigError('A local output needs path, for example extracts/sales.hyper')
        with tempfile.TemporaryDirectory(prefix='flowbench-hyper-') as d:
            local = ctx.resolve_path(cfg['path']) if cfg['output'] in ('local', 'both') else Path(d) / 'extract.hyper'
            if local.suffix.lower() != '.hyper':
                raise ConfigError('The extract file must end in .hyper')
            local.parent.mkdir(parents=True, exist_ok=True)
            write_hyper(inputs['input'], local, cfg['spatial_fields'], cfg['table_name'])
            ctx.info(f"wrote {len(inputs['input']):,} records to {local.name}")
            if cfg['output'] in ('publish', 'both'):
                _publish(ctx, cfg, local)
        return {}


class PublishToTableauServer(Tool):
    manifest = ToolManifest(
        tool_id='core.publish_to_tableau_server', version='1.0.0', display_name='Publish to Tableau Server',
        catalog_ids=('connectors:publish-to-tableau-server',),
        inputs=(Anchor('input', 'I', description='Records to publish'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Publish records to Tableau Server or Tableau Cloud as a Hyper extract data source (overwrite or append).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'datasource'],
                       'properties': {**_COMMON, **_PUBLISH}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'tableau')
        with tempfile.TemporaryDirectory(prefix='flowbench-hyper-') as d:
            local = Path(d) / 'extract.hyper'
            write_hyper(inputs['input'], local, cfg['spatial_fields'], cfg['table_name'])
            _publish(ctx, cfg, local)
        return {}


TOOLS = [TableauOutput, PublishToTableauServer]
