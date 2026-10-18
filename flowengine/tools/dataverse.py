"""Dataverse and Dynamics CRM Input/Output through the Dataverse Web API (OData v4); no vendor code.

Functional source: the public Dataverse / Dynamics CRM tool help and the Dataverse Web API reference.
Dynamics 365 apps (CRM) keep their data in Dataverse, so both tool pairs use this one API. The
connection is a Web/API connection with OAuth (flowengine/oauth.py): base URL
``https://<org>.crm.dynamics.com/api/data/v9.2`` and scope ``https://<org>.crm.dynamics.com/.default``.

Input pages with ``Prefer: odata.maxpagesize`` and follows ``@odata.nextLink`` unchanged. Output
creates rows (POST; the new id comes from ``OData-EntityId``), updates them (PATCH with
``If-Match: *`` so an update never creates), upserts (PATCH by primary key or an alternate key) or
deletes them (needs ``confirm_delete``).
"""
from __future__ import annotations

import datetime as _dt
import re
import urllib.parse
from decimal import Decimal

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, cast_value, format_value, type_for_values
from .download import UNBOUNDED_TEXT
from .github import _permit
from .msgraph import Graph

_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
_GUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_ISO_Z = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
ODATA = {'OData-MaxVersion': '4.0', 'OData-Version': '4.0'}


class Dataverse(Graph):
    service = 'Dataverse'


def _check_name(value, what):
    if not _NAME.match(value or ''):
        raise ConfigError(f'{what} {value!r} must be a logical name such as accounts or new_projects')
    return value


def _key_literal(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float, Decimal)):
        return format_value(value)
    text = format_value(value)
    return text if _GUID.match(text) else "'" + text.replace("'", "''") + "'"


def _record_key(rec, key_fields):
    if len(key_fields) == 1 and _GUID.match(format_value(rec[key_fields[0]]) if rec[key_fields[0]] is not None else ''):
        return format_value(rec[key_fields[0]])  # primary key: entityset(guid)
    parts = []
    for k in key_fields:
        if rec[k] is None:
            raise DataError(f'Key field {k!r} is null')
        parts.append(f'{k}={_key_literal(rec[k])}')
    return ','.join(parts)


def _json_value(v):
    if isinstance(v, _dt.datetime):
        return v.strftime('%Y-%m-%dT%H:%M:%SZ')
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, _dt.time):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        import base64
        return base64.b64encode(v).decode('ascii')
    return v


def _cell(v):
    if isinstance(v, str) and _ISO_Z.match(v):
        return _dt.datetime.fromisoformat(v[:-1])
    return v


_COMMON = {
    'provider': {'type': 'string', 'enum': ['microsoft-dataverse']},
    'provider_semantics': {'type': 'string'},
    'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                      'description': 'Saved Dataverse connection (OAuth), base URL https://<org>.crm.dynamics.com/api/data/v9.2'},
    'table': {'type': 'string', 'minLength': 1, 'description': 'Entity set name, for example accounts or contacts'},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120},
}


class _DataverseInput(Tool):
    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'microsoft-dataverse')
        table = _check_name(cfg['table'], 'Table')
        for c in cfg['columns']:
            _check_name(c, 'Column')
        _permit(ctx)
        dv = Dataverse(cfg)
        q = {}
        if cfg['columns']:
            q['$select'] = ','.join(cfg['columns'])
        if cfg['filter']:
            q['$filter'] = cfg['filter']
        if cfg['order_by']:
            q['$orderby'] = cfg['order_by']
        if cfg['limit']:
            q['$top'] = str(cfg['limit'])
        headers = {**ODATA, **({} if cfg['limit'] else {'Prefer': f"odata.maxpagesize={cfg['page_size']}"})}
        url = table + ('?' + urllib.parse.urlencode(q, quote_via=urllib.parse.quote) if q else '')
        records = [{k: _cell(v) for k, v in row.items() if not k.startswith('@odata.') and '@' not in k}
                   for row in dv.paged(url, ctx, cfg['limit'], headers=headers)]
        names = list(cfg['columns'])
        for r in records:
            names.extend(k for k in r if k not in names)
        fields = []
        for n in names:
            t = type_for_values([r.get(n) for r in records])
            fields.append(Field(n, t, UNBOUNDED_TEXT if t.is_string else None))
        cols = [[cast_value(r.get(f.name), f)[0] for r in records] for f in fields]
        ctx.info(f'read {len(records)} rows from {table}')
        return {'output': Table(Schema(fields), cols) if fields else Table([], [])}


class _DataverseOutput(Tool):
    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        if cfg['mode'] != 'create' and not cfg['key_fields']:
            raise ConfigError(f"{cfg['mode'].capitalize()} needs key_fields: the primary key column, or an alternate key's columns")
        missing = [k for k in cfg['key_fields'] if k not in schema]
        if missing:
            raise ConfigError(f'Key field(s) {missing} are not in the input')
        return {'output': Schema([*[f for f in schema if f.name != 'RecordUrl'], Field('RecordUrl', FieldType.V_WSTRING, UNBOUNDED_TEXT)])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'microsoft-dataverse')
        table_name = _check_name(cfg['table'], 'Table')
        data: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': data.schema})['output']
        if cfg['mode'] == 'delete' and not cfg['confirm_delete']:
            raise ConfigError('Delete removes rows from Dataverse; set confirm_delete to run it')
        for f in data.schema:
            if f.name != 'RecordUrl':
                _check_name(f.name, 'Column')
        _permit(ctx, writes=True)
        dv = Dataverse(cfg)
        out = []
        for rec in data.to_dicts():
            ctx.check_cancelled()
            key_fields = cfg['key_fields']
            body = {k: _json_value(v) for k, v in rec.items()
                    if k != 'RecordUrl' and not (cfg['mode'] in ('update', 'upsert') and k in key_fields)}
            if cfg['mode'] == 'create':
                _, headers = dv.request('POST', table_name, body, ODATA, with_headers=True)
                url = headers.get('OData-EntityId')
            else:
                url = dv.url(f'{table_name}({urllib.parse.quote(_record_key(rec, key_fields), safe="=,")})')
                if cfg['mode'] == 'update':
                    dv.request('PATCH', url, body, {**ODATA, 'If-Match': '*'})
                elif cfg['mode'] == 'upsert':
                    dv.request('PATCH', url, body, ODATA)
                else:
                    dv.request('DELETE', url, None, ODATA)
            out.append([rec.get(f.name) for f in schema if f.name != 'RecordUrl'] + [url])
        ctx.info(f"{cfg['mode']}: {len(out)} row(s) in {table_name}")
        return {'output': Table.from_rows(schema, out)}


def _pair(prefix, name, catalog_in, catalog_out, what):
    class Input(_DataverseInput):
        manifest = ToolManifest(
            tool_id=f'core.{prefix}_input', version='1.0.0', display_name=f'{name} Input', catalog_ids=(catalog_in,),
            inputs=(), outputs=(Anchor('output', 'O', description='One record per row'),),
            side_effects='network', deterministic=False,
            description=f'Read rows of a {what} table through the Dataverse Web API: columns, OData filter and order, '
                        'optional limit. Lookups arrive as _<name>_value ids; dates are UTC.',
            config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'table'],
                           'properties': {**_COMMON,
                'columns': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True, 'default': []},
                'filter': {'type': ['string', 'null'], 'default': None, 'description': "OData $filter, e.g. statecode eq 0"},
                'order_by': {'type': ['string', 'null'], 'default': None, 'description': 'OData $orderby, e.g. createdon desc'},
                'limit': {'type': 'integer', 'minimum': 0, 'default': 0},
                'page_size': {'type': 'integer', 'minimum': 1, 'maximum': 5000, 'default': 5000}}})

    class Output(_DataverseOutput):
        manifest = ToolManifest(
            tool_id=f'core.{prefix}_output', version='1.0.0', display_name=f'{name} Output', catalog_ids=(catalog_out,),
            inputs=(Anchor('input', 'I', description='Records to write; field names are column logical names'),),
            outputs=(Anchor('output', 'O', description='Each record with the URL of the row written'),),
            side_effects='network', deterministic=False,
            description=f'Write records to a {what} table through the Dataverse Web API: create, update (never creates), '
                        'upsert by primary or alternate key, or delete (needs confirm_delete).',
            config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'table'],
                           'properties': {**_COMMON,
                'mode': {'enum': ['create', 'update', 'upsert', 'delete'], 'default': 'create'},
                'key_fields': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True, 'default': [],
                               'description': 'Primary key column (a GUID), or the columns of an alternate key'},
                'confirm_delete': {'type': 'boolean', 'default': False}}})

    Input.__name__ = Input.__qualname__ = f'{name.replace(" ", "")}Input'
    Output.__name__ = Output.__qualname__ = f'{name.replace(" ", "")}Output'
    return Input, Output


DataverseInput, DataverseOutput = _pair('dataverse', 'Dataverse', 'connectors:dataverse-input', 'connectors:dataverse-output',
                                        'Dataverse')
DynamicsCrmInput, DynamicsCrmOutput = _pair('dynamics_crm', 'Dynamics CRM', 'connectors:dynamics-crm-input',
                                            'connectors:dynamics-crm-output', 'Dynamics 365 (CRM)')

TOOLS = [DataverseInput, DataverseOutput, DynamicsCrmInput, DynamicsCrmOutput]
