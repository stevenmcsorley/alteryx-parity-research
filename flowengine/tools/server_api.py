"""Server API and License API: call Alteryx REST APIs from a workflow; no vendor code.

Functional source: the public Server API tool help and the Server API v3 authentication notes.
Authentication is OAuth 2.0 client credentials: the API key and secret of a saved Web/API
connection (basic authentication; the key is the username and the secret lives in the OS vault)
are sent as a Basic header to ``<base>/oauth2/token``, and the bearer token is used for the calls.
``base_url`` is the server's Web API address, for example ``https://server.example.com/webapi``.

Unlike the reference tool, the token never travels through the workflow: there is no
authorization anchor in or out, because workflows refer to connections and never carry secrets.
Each record on the optional Data input makes one call; ``{field}`` placeholders in the endpoint
and query are filled from that record. GET needs the network permission; POST, PUT and DELETE
also need remote writes, and DELETE needs ``confirm_delete``.

License API calls the Licensing & Downloads Portal API (endpoints from its published OpenAPI document,
for example ``v1/devices`` or ``v2/licenses``) with a bearer access token. The portal hands out a
refresh token, which is the connection's secret; the access token is obtained from the connection's
``token_url`` with the standard OAuth 2.0 refresh grant (RFC 6749 section 6), because the portal does
not publish its own exchange format. A refresh token the server rotates is saved back to the vault.
Its success output flattens nested JSON objects into dotted field names, as the reference help says.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value, type_for_values
from .download import UNBOUNDED_TEXT, _HttpOnlyRedirects
from .github import _permit

_ENDPOINT = re.compile(r'^v[123]/[A-Za-z0-9_./{}@+-]+$')
_PLACEHOLDER = re.compile(r'\{([^{}]+)\}')
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def _text(name):
    return Field(name, FieldType.V_WSTRING, UNBOUNDED_TEXT)


FAIL_SCHEMA = Schema([Field('Call', FieldType.INT64), _text('Method'), _text('URL'), Field('StatusCode', FieldType.INT32),
                      _text('Response')])
LOG_SCHEMA = Schema([Field('Call', FieldType.INT64), _text('Method'), _text('URL'), Field('StatusCode', FieldType.INT32),
                     Field('Seconds', FieldType.DOUBLE), _text('Message'), _text('Response')])


def _fill(template, row, what):
    def sub(m):
        name = m.group(1)
        if name not in row:
            raise ConfigError(f'{what} uses {{{name}}}, which is not a field of the Data input')
        value = row[name]
        if value is None:
            raise DataError(f'{what} needs {name}, which is null in record {row.get("_call")}')
        return urllib.parse.quote(format_value(value), safe='')
    return _PLACEHOLDER.sub(sub, template)


def _flatten(item):
    """One level deep, as the Server API tool parses responses: nested values are kept as JSON text."""
    if not isinstance(item, dict):
        return {'Value': item if not isinstance(item, (list, dict)) else json.dumps(item, ensure_ascii=False)}
    return {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in item.items()}


def _flatten_dotted(item, prefix=''):
    """Nested objects become dotted field names, as the License API tool parses responses; lists stay JSON text."""
    if not isinstance(item, dict):
        return {prefix or 'Value': json.dumps(item, ensure_ascii=False) if isinstance(item, list) else item}
    out = {}
    for k, v in item.items():
        name = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict) and v:
            out.update(_flatten_dotted(v, name))
        else:
            out[name] = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
    return out


class _Session:
    product = 'Alteryx Server'

    def __init__(self, cfg):
        from ..service_connections import ServiceConnectionStore
        self.store = store = ServiceConnectionStore()
        self.connection = store.get(cfg['connection_id'], kind='web')
        self.check_connection()
        self.secret = store.secret(self.connection)
        self.base = self.connection.base_url.rstrip('/')
        self.timeout = cfg['timeout_seconds']
        self.opener = urllib.request.build_opener(_HttpOnlyRedirects(self.connection))
        self.token = None

    def check_connection(self):
        if self.connection.auth != 'basic':
            raise ConfigError('An Alteryx Server connection uses basic authentication: the API key as the username '
                              'and the API secret as the password')

    def _token(self):
        url = f'{self.base}/oauth2/token'
        req = urllib.request.Request(url, data=b'grant_type=client_credentials', method='POST', headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json',
            **self.connection.authorize(url, self.secret)})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                token = json.loads(r.read() or b'{}').get('access_token')
        except urllib.error.HTTPError as e:
            e.close()
            raise DataError(f'Alteryx Server refused the API key and secret ({e.code} from /oauth2/token)') from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DataError(f'Could not reach Alteryx Server for a token: {getattr(e, "reason", None) or e}') from None
        if not token:
            raise DataError('Alteryx Server returned no access_token from /oauth2/token')
        self.token = token

    def call(self, method, url, body):
        """(status, text). Gets a token first and once more if the server says it expired."""
        if not self.connection.covers(url):
            raise ConfigError(f'{url} is outside the connection base URL')
        for attempt in (1, 2):
            if self.token is None:
                self._token()
            headers = {'Accept': 'application/json', 'Authorization': f'Bearer {self.token}'}
            if body is not None:
                headers['Content-Type'] = 'application/json'
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    return r.status, r.read(MAX_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as e:
                with e:
                    text = e.read(MAX_RESPONSE_BYTES + 1)
                if e.code == 401 and attempt == 1:
                    self.token = None
                    continue
                return e.code, text
            except (urllib.error.URLError, OSError) as e:
                return 0, str(getattr(e, 'reason', None) or e).encode()


class _LicenseSession(_Session):
    def check_connection(self):
        if self.connection.auth != 'bearer' or not self.connection.token_url:
            raise ConfigError('A License Portal connection uses a bearer refresh token and needs the token URL '
                              '(the Authentication URL from the portal)')

    def _token(self):
        url = self.connection.token_url
        body = urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': self.secret}).encode()
        req = urllib.request.Request(url, data=body, method='POST', headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
        try:
            # the refresh token goes only to the token URL saved with the connection; redirects are refused
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=self.timeout) as r:
                doc = json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            e.close()
            raise DataError(f'The license portal refused the refresh token ({e.code}); generate new tokens on its '
                            'API Access page and save the refresh token in the connection') from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DataError(f'Could not reach the license portal token URL: {getattr(e, "reason", None) or e}') from None
        if not doc.get('access_token'):
            raise DataError('The license portal token URL returned no access_token')
        rotated = doc.get('refresh_token')
        if rotated and rotated != self.secret:
            self.store.save(self.connection.public(), secret=rotated)
            self.secret = rotated
        self.token = doc['access_token']


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _ApiTool(Tool):
    provider = 'alteryx-server'
    session = _Session
    methods = ('GET', 'POST', 'PUT', 'DELETE')
    flatten = staticmethod(_flatten)

    def infer(self, ctx, cfg, inputs):
        return {'fail': FAIL_SCHEMA, 'log': LOG_SCHEMA}

    def run(self, ctx, cfg, inputs):
        return _run(self, ctx, cfg, inputs)


def _schema(provider, methods, connection_note, endpoint_note):
    return {'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'endpoint'],
            'properties': {
        'provider': {'type': 'string', 'enum': [provider]},
        'provider_semantics': {'type': 'string'},
        'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web', 'description': connection_note},
        'method': {'type': 'string', 'enum': list(methods), 'default': 'GET'},
        'confirm_delete': {'type': 'boolean', 'default': False},
        'endpoint': {'type': 'string', 'minLength': 4, 'description': endpoint_note},
        'query': {'type': 'object', 'additionalProperties': {'type': 'string'}, 'default': {}},
        'payload': {'type': ['string', 'null'], 'default': None, 'description': 'JSON body; {field} placeholders allowed'},
        'payload_field': {'type': ['string', 'null'], 'default': None, 'description': 'Take the JSON body from this field'},
        'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 60}}}


def _anchors(parsing):
    return dict(inputs=(Anchor('data', 'D', required=False, description='One call per record; {field} placeholders read from it'),),
                outputs=(Anchor('success', 'S', description=f'HTTP 200 responses, {parsing}'),
                         Anchor('fail', 'F', description='Calls that did not return HTTP 200'),
                         Anchor('log', 'L', description='One line per call with the full response')))


class ServerApi(_ApiTool):
    manifest = ToolManifest(
        tool_id='core.server_api', version='1.0.0', display_name='Server API', catalog_ids=('developer:server-api',),
        **_anchors('parsed one level deep'), side_effects='network', deterministic=False,
        description='Call the Alteryx Server API (v1/v2/v3) with a saved connection: base URL https://host/webapi, API key '
                    'as username, API secret as password. Endpoint like v3/workflows or v3/users/{id}.',
        config_schema=_schema('alteryx-server', _ApiTool.methods, 'Saved Web/API connection to the Server Web API',
                              'For example v3/workflows or v3/workflows/{id}'))


class LicenseApi(_ApiTool):
    provider = 'alteryx-license-portal'
    session = _LicenseSession
    methods = ('GET', 'POST', 'PATCH', 'DELETE')
    flatten = staticmethod(_flatten_dotted)
    manifest = ToolManifest(
        tool_id='core.license_api', version='1.0.0', display_name='License API', catalog_ids=('developer:license-api',),
        **_anchors('nested objects flattened to dotted field names'), side_effects='network', deterministic=False,
        description='Call the Alteryx Licensing & Downloads Portal API with a saved connection: base URL '
                    'https://us1.alteryxcloud.com/license-portal/api, the refresh token as the secret and the portal '
                    'Authentication URL as token URL. Endpoint like v1/devices or v2/licenses.',
        config_schema=_schema('alteryx-license-portal', ('GET', 'POST', 'PATCH', 'DELETE'),
                              'Saved Web/API connection to the license portal', 'For example v1/devices or v2/licenses'))


def _run(tool, ctx, cfg, inputs):
    from .. import providers
    providers.resolve(cfg, tool.provider)
    method = cfg['method']
    if not _ENDPOINT.match(cfg['endpoint']) or '..' in cfg['endpoint']:
        raise ConfigError('Endpoint must be a path under the Web API such as v3/workflows or v3/users/{id}')
    if method == 'DELETE' and not cfg['confirm_delete']:
        raise ConfigError('DELETE removes resources on the server; set confirm_delete to run it')
    if method == 'GET' and (cfg['payload'] or cfg['payload_field']):
        raise ConfigError('GET calls have no payload')
    _permit(ctx, writes=method != 'GET')
    data: Table | None = inputs.get('data')
    if cfg['payload_field'] and (data is None or cfg['payload_field'] not in data.schema.names):
        raise ConfigError(f'payload_field {cfg["payload_field"]!r} is not a field of the Data input')
    records = [dict(zip(data.schema.names, r)) for r in data.rows()] if data is not None else [{}]
    session = tool.session(cfg)
    ok_rows, fail_rows, log_rows = [], [], []
    for i, row in enumerate(records, start=1):
        ctx.check_cancelled()
        row = {**row, '_call': i}
        url = f'{session.base}/{_fill(cfg["endpoint"], row, "endpoint")}'
        if cfg['query']:
            url += '?' + urllib.parse.urlencode({k: urllib.parse.unquote(_fill(v, row, f'query {k}'))
                                                 for k, v in cfg['query'].items()})
        body = None
        if cfg['payload_field']:
            body = None if row[cfg['payload_field']] is None else str(row[cfg['payload_field']])
        elif cfg['payload']:
            body = _PLACEHOLDER.sub(lambda m: json.dumps(row[m.group(1)])[1:-1] if isinstance(row.get(m.group(1)), str)
                                    else json.dumps(row.get(m.group(1))), cfg['payload'])
        if body is not None:
            try:
                json.loads(body)
            except ValueError as e:
                raise DataError(f'Payload for call {i} is not valid JSON: {e}') from None
        started = time.monotonic()
        status, raw = session.call(method, url, body.encode('utf-8') if body is not None else None)
        seconds = round(time.monotonic() - started, 3)
        text = raw[:MAX_RESPONSE_BYTES].decode('utf-8', errors='replace')
        if status == 200:
            try:
                parsed = json.loads(text) if text.strip() else None
                items = parsed if isinstance(parsed, list) else [parsed] if parsed is not None else []
                ok_rows.extend({'Call': i, **tool.flatten(x)} for x in items)
                message = f'{len(items)} item(s)'
            except ValueError:
                ok_rows.append({'Call': i, 'Extracted': 'Response is not JSON; see DownloadData', 'DownloadData': text})
                message = 'response is not JSON'
        else:
            fail_rows.append((i, method, url, status, text))
            message = text[:300] if status == 0 else f'HTTP {status}'
        log_rows.append((i, method, url, status, seconds, message, text))
    return {'success': _success_table(ok_rows), 'fail': Table.from_rows(FAIL_SCHEMA, fail_rows),
            'log': Table.from_rows(LOG_SCHEMA, log_rows)}


def _success_table(rows):
    names = ['Call']
    for r in rows:
        names.extend(k for k in r if k not in names)
    cols = {n: [r.get(n) for r in rows] for n in names}
    fields = []
    for n in names:
        t = FieldType.INT64 if n == 'Call' else type_for_values(cols[n])
        fields.append(Field(n, t, UNBOUNDED_TEXT if t.is_string else None))
    from ..types import cast_value
    return Table(Schema(fields), [[cast_value(v, f)[0] for v in cols[f.name]] for f in fields])


TOOLS = [ServerApi, LicenseApi]
