"""Salesforce Input/Output and ServiceNow Input through their REST APIs; no vendor SDKs.

Functional source: the public tool help and the Salesforce REST API / ServiceNow Table API references.

* Salesforce: a Web/API connection with OAuth whose base URL is the org's My Domain
  (``https://<domain>.my.salesforce.com``); token ``https://login.salesforce.com/services/oauth2/token`` (or the My
  Domain one), browser sign-in at ``.../services/oauth2/authorize`` with scope ``api refresh_token``, or the client
  credentials flow. Input runs SOQL (``query`` or ``queryAll``) and follows ``nextRecordsUrl``; relationship fields
  become dotted names. Output writes through sObject Collections, 200 records per request, and returns each record's
  result (id, success, errors) instead of failing the run on the first bad record, unless ``all_or_none``.
* ServiceNow: a Web/API connection to ``https://<instance>.service-now.com`` with basic authentication or OAuth.
  Input reads a table with an encoded query, chosen fields and offset paging. Values are the stored values (not
  display values) unless ``display_values`` is set.
"""
from __future__ import annotations

import datetime as _dt
import json
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

_SF_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,79}$')
_SF_DATETIME = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(\+0000|Z)$')
_SF_DATE = re.compile(r'^\d{4}-\d\d-\d\d$')
COLLECTION_MAX = 200


class Salesforce(Graph):
    service = 'Salesforce'


class ServiceNow(Graph):
    service = 'ServiceNow'
    other_auth = True


def _sf_flat(record, prefix=''):
    """A record with relationship objects flattened to dotted names (Account.Name); 'attributes' dropped."""
    out = {}
    for k, v in record.items():
        if k == 'attributes':
            continue
        name = f'{prefix}{k}'
        if isinstance(v, dict) and 'attributes' in v:
            out.update(_sf_flat(v, f'{name}.'))
        elif isinstance(v, dict) and 'records' in v:  # a child subquery
            out[name] = json.dumps([_sf_flat(r) for r in v['records']], ensure_ascii=False)
        else:
            m = isinstance(v, str) and _SF_DATETIME.match(v)
            out[name] = _dt.datetime.fromisoformat(m.group(1)) if m else (
                _dt.date.fromisoformat(v) if isinstance(v, str) and _SF_DATE.match(v) else v)
    return out


def _typed_table(records, first_names=()):
    names = list(first_names)
    for r in records:
        names.extend(k for k in r if k not in names)
    fields = []
    for n in names:
        t = type_for_values([r.get(n) for r in records])
        fields.append(Field(n, t, UNBOUNDED_TEXT if t.is_string else None))
    cols = [[cast_value(r.get(f.name), f)[0] for r in records] for f in fields]
    return Table(Schema(fields), cols) if fields else Table([], [])


_SF_COMMON = {
    'provider': {'type': 'string', 'enum': ['salesforce']}, 'provider_semantics': {'type': 'string'},
    'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                      'description': 'Saved Salesforce connection (OAuth), base URL https://<domain>.my.salesforce.com'},
    'api_version': {'type': 'string', 'pattern': '^v[0-9]{2}\\.0$', 'default': 'v62.0'},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120},
}


class SalesforceInput(Tool):
    manifest = ToolManifest(
        tool_id='core.salesforce_input', version='1.0.0', display_name='Salesforce Input', catalog_ids=('connectors:salesforce-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per result row'),),
        side_effects='network', deterministic=False,
        description='Run a SOQL query against Salesforce and read every page. Relationship fields become dotted names '
                    '(Account.Name); child subqueries are JSON text; dates and UTC date-times are typed. include_deleted '
                    'uses queryAll.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'soql'],
                       'properties': {**_SF_COMMON,
            'soql': {'type': 'string', 'minLength': 7, 'description': 'For example SELECT Id, Name, Account.Name FROM Contact'},
            'include_deleted': {'type': 'boolean', 'default': False}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'salesforce')
        _permit(ctx)
        sf = Salesforce(cfg)
        endpoint = 'queryAll' if cfg['include_deleted'] else 'query'
        url = f"services/data/{cfg['api_version']}/{endpoint}?" + urllib.parse.urlencode({'q': cfg['soql']})
        records, seen = [], set()
        while url:
            ctx.check_cancelled()
            if url in seen:
                raise DataError('Salesforce returned a next page that was already read; stopping to avoid a loop')
            seen.add(url)
            page = sf.request('GET', url)
            records.extend(_sf_flat(r) for r in page.get('records', []))
            url = None if page.get('done', True) else page.get('nextRecordsUrl')
        ctx.info(f'read {len(records):,} records')
        return {'output': _typed_table(records)}


def _sf_value(v):
    if isinstance(v, _dt.datetime):
        return v.strftime('%Y-%m-%dT%H:%M:%S.000+0000')
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        raise DataError('Binary values cannot be written to Salesforce fields')
    return v


RESULT_FIELDS = [Field('SalesforceId', FieldType.V_WSTRING, UNBOUNDED_TEXT), Field('Success', FieldType.BOOL),
                 Field('Errors', FieldType.V_WSTRING, UNBOUNDED_TEXT)]


class SalesforceOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.salesforce_output', version='1.0.0', display_name='Salesforce Output',
        catalog_ids=('connectors:salesforce-output-new',),
        inputs=(Anchor('input', 'I', description='Records to write; field names are API names'),),
        outputs=(Anchor('output', 'O', description='Each record with SalesforceId, Success and Errors'),),
        side_effects='network', deterministic=False,
        description='Write records to a Salesforce object with sObject Collections (200 per request): insert, update (by '
                    'Id), upsert (by an external id field) or delete (by Id, needs confirm_delete). Each record reports '
                    'its own result; all_or_none rolls back a whole batch when any record fails.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'object'],
                       'properties': {**_SF_COMMON,
            'object': {'type': 'string', 'minLength': 1, 'description': 'API name, for example Account or Invoice__c'},
            'mode': {'enum': ['insert', 'update', 'upsert', 'delete'], 'default': 'insert'},
            'external_id_field': {'type': ['string', 'null'], 'default': None, 'description': 'For upsert, e.g. External_Id__c'},
            'all_or_none': {'type': 'boolean', 'default': False},
            'confirm_delete': {'type': 'boolean', 'default': False},
            'fail_on_errors': {'type': 'boolean', 'default': False, 'description': 'Fail the run if any record fails'}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        mode = cfg['mode']
        if not _SF_NAME.match(cfg['object']):
            raise ConfigError(f"{cfg['object']!r} is not a Salesforce object API name")
        if mode in ('update', 'delete') and 'Id' not in schema:
            raise ConfigError(f'{mode.capitalize()} needs an Id field')
        if mode == 'upsert':
            if not cfg['external_id_field'] or not _SF_NAME.match(cfg['external_id_field']):
                raise ConfigError('Upsert needs external_id_field, the API name of an external id field')
            if cfg['external_id_field'] not in schema:
                raise ConfigError(f"Upsert needs the field {cfg['external_id_field']!r} in the input")
        keep = [f for f in schema if f.name not in {r.name for r in RESULT_FIELDS}]
        return {'output': Schema([*keep, *RESULT_FIELDS])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'salesforce')
        table: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        if cfg['mode'] == 'delete' and not cfg['confirm_delete']:
            raise ConfigError('Delete removes Salesforce records; set confirm_delete to run it')
        for f in table.schema:
            if f.name not in {r.name for r in RESULT_FIELDS} and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,79}', f.name):
                raise ConfigError(f'{f.name!r} is not a Salesforce field API name')
        _permit(ctx, writes=True)
        sf = Salesforce(cfg)
        base = f"services/data/{cfg['api_version']}/composite/sobjects"
        records = table.to_dicts()
        out, failures = [], 0
        for start in range(0, len(records), COLLECTION_MAX):
            ctx.check_cancelled()
            batch = records[start:start + COLLECTION_MAX]
            if cfg['mode'] == 'delete':
                ids = [format_value(r['Id']) for r in batch]
                results = sf.request('DELETE', f'{base}?' + urllib.parse.urlencode(
                    {'ids': ','.join(ids), 'allOrNone': str(cfg['all_or_none']).lower()}))
            else:
                body = {'allOrNone': cfg['all_or_none'], 'records': [
                    {'attributes': {'type': cfg['object']},
                     **{k: _sf_value(v) for k, v in r.items() if k not in {f.name for f in RESULT_FIELDS}}} for r in batch]}
                if cfg['mode'] == 'insert':
                    for rec in body['records']:
                        rec.pop('Id', None)
                    results = sf.request('POST', base, body)
                elif cfg['mode'] == 'update':
                    results = sf.request('PATCH', base, body)
                else:
                    results = sf.request('PATCH', f"{base}/{cfg['object']}/{cfg['external_id_field']}", body)
            if not isinstance(results, list) or len(results) != len(batch):
                raise DataError('Salesforce returned a result list that does not match the records sent')
            for rec, res in zip(batch, results):
                errors = '; '.join(f"{e.get('statusCode', '')}: {e.get('message', '')}"
                                   + (f" ({', '.join(e['fields'])})" if e.get('fields') else '') for e in res.get('errors', []))
                failures += not res.get('success')
                out.append([rec.get(f.name) for f in schema if f.name not in {r.name for r in RESULT_FIELDS}]
                           + [res.get('id') or (format_value(rec['Id']) if rec.get('Id') is not None else None),
                              bool(res.get('success')), errors or None])
        ctx.info(f"{cfg['mode']}: {len(out) - failures:,} succeeded, {failures:,} failed")
        if failures and cfg['fail_on_errors']:
            raise DataError(f"{failures} of {len(out)} Salesforce records failed; see the Errors field")
        return {'output': Table.from_rows(schema, out)}


# --------------------------------------------------------------------------- ServiceNow

_SN_TABLE = re.compile(r'^[a-z0-9_]{1,80}$')


class ServiceNowInput(Tool):
    manifest = ToolManifest(
        tool_id='core.servicenow_input', version='1.0.0', display_name='ServiceNow Input', catalog_ids=('connectors:servicenow-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per table row'),),
        side_effects='network', deterministic=False,
        description='Read a ServiceNow table through the Table API: an encoded query (for example active=true^priority=1), '
                    'chosen fields, all pages. Values are stored values unless display_values is set; reference fields '
                    'are sys_ids.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'table'],
                       'properties': {
            'provider': {'type': 'string', 'enum': ['servicenow']}, 'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                              'description': 'Saved ServiceNow connection, base URL https://<instance>.service-now.com'},
            'table': {'type': 'string', 'minLength': 1, 'description': 'Table name, for example incident'},
            'query': {'type': ['string', 'null'], 'default': None, 'description': 'Encoded query, e.g. active=true^ORDERBYnumber'},
            'fields': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[a-z0-9_.]{1,80}$'}, 'uniqueItems': True, 'default': []},
            'display_values': {'type': 'boolean', 'default': False},
            'limit': {'type': 'integer', 'minimum': 0, 'default': 0},
            'page_size': {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 1000},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'servicenow')
        if not _SN_TABLE.match(cfg['table']):
            raise ConfigError(f"{cfg['table']!r} is not a ServiceNow table name")
        _permit(ctx)
        sn = ServiceNow(cfg)
        records, offset = [], 0
        while True:
            ctx.check_cancelled()
            size = cfg['page_size'] if not cfg['limit'] else min(cfg['page_size'], cfg['limit'] - len(records))
            q = {'sysparm_limit': str(size), 'sysparm_offset': str(offset), 'sysparm_exclude_reference_link': 'true',
                 'sysparm_display_value': 'true' if cfg['display_values'] else 'false'}
            if cfg['query']:
                q['sysparm_query'] = cfg['query']
            if cfg['fields']:
                q['sysparm_fields'] = ','.join(cfg['fields'])
            page = (sn.request('GET', f"api/now/table/{cfg['table']}?" + urllib.parse.urlencode(q)) or {}).get('result', [])
            records.extend({k: (None if v == '' else v) for k, v in row.items()} for row in page)
            offset += len(page)
            if len(page) < size or (cfg['limit'] and len(records) >= cfg['limit']):
                break
        # The Table API returns every value as text; keep them as text so stored values are not reinterpreted.
        names = list(cfg['fields'])
        for r in records:
            names.extend(k for k in r if k not in names)
        schema = Schema([Field(n, FieldType.V_WSTRING, UNBOUNDED_TEXT) for n in names])
        ctx.info(f"read {len(records):,} rows from {cfg['table']}")
        return {'output': Table.from_rows(schema, [[r.get(n) for n in names] for r in records]) if names else Table([], [])}


TOOLS = [SalesforceInput, SalesforceOutput, ServiceNowInput]
