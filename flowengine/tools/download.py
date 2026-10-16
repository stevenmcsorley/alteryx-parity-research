"""Independent HTTP(S) Download tool; no vendor code or parity claim.

Functional source: public Download tool help linked in the contract. Every
request needs the run's ``allow_network`` permission because it can send
record data off the machine. URLs, header values and bodies are never logged.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ..errors import ConfigError, DataError, EngineError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

UNBOUNDED_TEXT = 2**30 - 1
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_FIELD_LIST = {'type': 'array', 'uniqueItems': True, 'maxItems': 256, 'items': {'type': 'string', 'minLength': 1}}
OUTPUT_FIELDS = ('DownloadData', 'DownloadHeaders', 'StatusCode', 'DownloadError')


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        target = urllib.parse.urljoin(req.full_url, headers.get('Location') or headers.get('URI') or '')
        if urllib.parse.urlsplit(target).scheme.lower() not in ('http', 'https'):
            fp.close()
            raise urllib.error.URLError('redirect to a non-HTTP(S) URL was refused')
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Download(Tool):
    manifest = ToolManifest(
        tool_id='core.download', version='1.0.0', display_name='Download', catalog_ids=('developer:download',),
        inputs=(Anchor('input', 'Input', description='One HTTP(S) request per record'),),
        outputs=(Anchor('output', 'Output', description='Input records with the response appended'),),
        side_effects='network', deterministic=False,
        description='Send one HTTP(S) request per record to the URL in url_field and append DownloadData, '
                    'DownloadHeaders, StatusCode and DownloadError. Non-2xx responses are data, not errors. '
                    'Requires permission to make network requests. Output order matches input order.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['url_field'],
            'properties': {
                'url_field': {'type': 'string', 'minLength': 1},
                'method': {'enum': ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'], 'default': 'GET'},
                'headers': {'type': 'array', 'maxItems': 64, 'default': [], 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['name', 'value'],
                    'properties': {'name': {'type': 'string'}, 'value': {'type': 'string'}}}},
                'header_fields': {**_FIELD_LIST, 'default': [],
                                  'description': 'Fields sent as headers named after the field (null values are omitted)'},
                'query_fields': {**_FIELD_LIST, 'default': [],
                                 'description': 'Fields appended to the URL query string (null values are omitted)'},
                'body': {'enum': ['none', 'field', 'form', 'json'], 'default': 'none'},
                'body_field': {'type': ['string', 'null'], 'default': None},
                'body_fields': {**_FIELD_LIST, 'default': []},
                'content_type': {'type': ['string', 'null'], 'default': None},
                'output': {'enum': ['text', 'blob'], 'default': 'text'},
                'encoding': {'type': 'string', 'default': 'utf-8',
                             'description': 'Text decoding when the response declares no charset'},
                'follow_redirects': {'type': 'boolean', 'default': True},
                'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 30},
                'max_response_bytes': {'type': 'integer', 'minimum': 1, 'maximum': 2**30, 'default': 64 * 2**20},
                'connections': {'type': 'integer', 'minimum': 1, 'maximum': 16, 'default': 1},
                'on_error': {'enum': ['error', 'record'], 'default': 'error',
                             'description': 'Network, size or decoding failures: stop the run, or record DownloadError'},
            },
        })

    def check_config(self, cfg):
        for header in cfg['headers']:
            if not _TOKEN.match(header['name']) or any(c in header['value'] for c in '\r\n'):
                raise ConfigError(f"Invalid HTTP header {header['name']!r}")
        for name in cfg['header_fields']:
            if not _TOKEN.match(name):
                raise ConfigError(f'Field {name!r} is not a valid HTTP header name')
        if cfg['body'] == 'field' and not cfg['body_field']:
            raise ConfigError('Download body "field" requires body_field')
        if cfg['body'] in ('form', 'json') and not cfg['body_fields']:
            raise ConfigError(f"Download body {cfg['body']!r} requires body_fields")
        if cfg['body'] != 'none' and cfg['method'] in ('GET', 'HEAD'):
            raise ConfigError(f"Download {cfg['method']} requests cannot send a body")

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        used = [cfg['url_field'], *cfg['header_fields'], *cfg['query_fields'], *cfg['body_fields']]
        if cfg['body'] == 'field':
            used.append(cfg['body_field'])
        require_fields(schema, used)
        if not schema.field(cfg['url_field']).type.is_string:
            raise SchemaError(f"Download url_field {cfg['url_field']!r} must be a text field")
        clash = [n for n in OUTPUT_FIELDS if n in schema]
        if clash:
            raise SchemaError(f'Download output field(s) {clash} already exist in the input; rename them first')
        data = (Field('DownloadData', FieldType.BLOB) if cfg['output'] == 'blob'
                else Field('DownloadData', FieldType.V_WSTRING, UNBOUNDED_TEXT))
        return {'output': Schema([*schema, data, Field('DownloadHeaders', FieldType.V_WSTRING, UNBOUNDED_TEXT),
                                  Field('StatusCode', FieldType.INT32),
                                  Field('DownloadError', FieldType.V_WSTRING, UNBOUNDED_TEXT)])}

    def _request(self, cfg, record):
        url = record[cfg['url_field']]
        if url is None or not str(url).strip():
            raise ValueError('empty URL')
        parts = urllib.parse.urlsplit(str(url).strip())
        if parts.scheme.lower() not in ('http', 'https') or not parts.netloc:
            raise ValueError('URL must be an absolute http:// or https:// address')
        query = [(n, format_value(record[n])) for n in cfg['query_fields'] if record[n] is not None]
        if query:
            joined = '&'.join(p for p in (parts.query, urllib.parse.urlencode(query)) if p)
            parts = parts._replace(query=joined)
        headers = {h['name']: h['value'] for h in cfg['headers']}
        for name in cfg['header_fields']:
            if record[name] is not None:
                value = format_value(record[name])
                if any(c in value for c in '\r\n'):
                    raise ValueError(f'header field {name!r} contains a line break')
                headers[name] = value
        body = None
        if cfg['body'] == 'field' and record[cfg['body_field']] is not None:
            value = record[cfg['body_field']]
            body = value if isinstance(value, bytes) else format_value(value).encode('utf-8')
        elif cfg['body'] == 'form':
            body = urllib.parse.urlencode([(n, format_value(record[n])) for n in cfg['body_fields']
                                           if record[n] is not None]).encode('ascii')
            headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        elif cfg['body'] == 'json':
            body = json.dumps({n: (None if record[n] is None else format_value(record[n]))
                               for n in cfg['body_fields']}, ensure_ascii=False).encode('utf-8')
            headers.setdefault('Content-Type', 'application/json')
        if cfg['content_type']:
            headers['Content-Type'] = cfg['content_type']
        return urllib.request.Request(urllib.parse.urlunsplit(parts), data=body, headers=headers, method=cfg['method'])

    def _fetch(self, cfg, record):
        """Return (data, headers, status, error) for one record."""
        opener = urllib.request.build_opener(_HttpOnlyRedirects if cfg['follow_redirects'] else _NoRedirects)
        try:
            request = self._request(cfg, record)
            try:
                response = opener.open(request, timeout=cfg['timeout_seconds'])
            except urllib.error.HTTPError as e:
                response = e  # a non-2xx status is still a response
            with response:
                raw = response.read(cfg['max_response_bytes'] + 1)
                if len(raw) > cfg['max_response_bytes']:
                    raise ValueError(f"response exceeds max_response_bytes ({cfg['max_response_bytes']})")
                header_text = ''.join(f'{k}: {v}\n' for k, v in response.headers.items())
                if cfg['output'] == 'blob':
                    data = raw
                else:
                    charset = response.headers.get_content_charset() or cfg['encoding']
                    try:
                        data = raw.decode(charset)
                    except (LookupError, UnicodeDecodeError) as e:
                        raise ValueError(f'response is not valid {charset} text ({type(e).__name__})') from None
                return data, header_text, response.status, None
        except (ValueError, OSError, urllib.error.URLError) as e:
            reason = getattr(e, 'reason', None) or e
            return None, None, None, f'{type(e).__name__}: {reason}'

    def run(self, ctx, cfg, inputs):
        if not ctx.limits.get('allow_network'):
            raise EngineError('Download sends network requests and is disabled for this run. '
                              'Allow it explicitly (CLI --allow-network, or confirm in the desktop app).',
                              code='permission-denied', node_id=ctx.node_id)
        table: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        records = table.to_dicts()
        if cfg['connections'] > 1 and len(records) > 1:
            with ThreadPoolExecutor(max_workers=cfg['connections']) as pool:
                results = list(pool.map(lambda r: self._fetch(cfg, r), records))
        else:
            results = []
            for record in records:
                ctx.check_cancelled()
                results.append(self._fetch(cfg, record))
        ctx.check_cancelled()
        failures = [(i, r[3]) for i, r in enumerate(results, start=1) if r[3]]
        if failures and cfg['on_error'] == 'error':
            number, message = failures[0]
            raise DataError(f'Download record {number} failed: {message}')
        ctx.info(f'{len(records)} request(s), {len(failures)} failed')
        return {'output': Table.from_rows(schema, [(*row, *result) for row, result in zip(table.rows(), results)])}


TOOLS = [Download]
