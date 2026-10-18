"""Marketo Input, Output and Append through the Marketo REST API; no vendor code.

Functional source: the public Marketo tool help and the Marketo REST API reference. A Web/API connection with
OAuth: base URL ``https://<munchkin-id>.mktorest.com/rest``, token URL
``https://<munchkin-id>.mktorest.com/identity/oauth/token``, the client id and the client secret (client
credentials; Marketo's documented token request is a GET with query parameters). Calls send the token in the
``Authorization`` header (the ``access_token`` query parameter is no longer supported).

Marketo reports most errors inside a 200 response (``success: false``): 601/602 (invalid or expired token) renew
the token once; 606 (rate limit) waits and retries; anything else stops the run with Marketo's code and message.
"""
from __future__ import annotations

import datetime as _dt
import time
import urllib.parse

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, cast_value, format_value, type_for_values
from .download import UNBOUNDED_TEXT
from .github import _permit
from .msgraph import Graph

BATCH = 300               # leads per lookup or sync request
RATE_LIMIT_WAIT = 20      # seconds after error 606


class Marketo(Graph):
    service = 'Marketo'

    def call(self, method, path, body=None, ctx=None):
        """A Marketo response's body after checking success; renews the token or waits as Marketo asks."""
        renewed = False
        for attempt in range(4):
            doc = self.request(method, path, body) or {}
            if doc.get('success', True):
                return doc
            errors = doc.get('errors') or [{}]
            code = str(errors[0].get('code', ''))
            if code in ('601', '602') and not renewed and self.oauth:
                self.oauth.access_token(refresh=True)
                renewed = True
                continue
            if code in ('606', '615') and attempt < 3:
                if ctx:
                    ctx.check_cancelled()
                time.sleep(RATE_LIMIT_WAIT)
                continue
            raise DataError(f"Marketo error {code}: {errors[0].get('message', 'request failed')}")
        raise DataError('Marketo kept refusing the request (rate limit)')


def _cell(v):
    if isinstance(v, str) and len(v) == 20 and v.endswith('Z') and v[10] == 'T':
        try:
            return _dt.datetime.fromisoformat(v[:-1])
        except ValueError:
            return v
    return v


def _typed(records, first=()):
    names = list(first)
    for r in records:
        names.extend(k for k in r if k not in names)
    fields = []
    for n in names:
        t = type_for_values([_cell(r.get(n)) for r in records])
        fields.append(Field(n, t, UNBOUNDED_TEXT if t.is_string else None))
    return fields, [[cast_value(_cell(r.get(f.name)), f)[0] for r in records] for f in fields]


def _value(v):
    if isinstance(v, _dt.datetime):
        return v.strftime('%Y-%m-%dT%H:%M:%SZ')
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    return v if v is None or isinstance(v, (bool, int, float, str)) else format_value(v)


_COMMON = {'provider': {'type': 'string', 'enum': ['marketo']}, 'provider_semantics': {'type': 'string'},
           'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                             'description': 'Saved Marketo connection, base URL https://<munchkin-id>.mktorest.com/rest'},
           'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}
_FIELDS = {'fields': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[A-Za-z0-9_]{1,80}$'}, 'uniqueItems': True,
                      'default': [], 'description': 'Lead fields (REST API names); empty for the default set'}}


def _provider(cfg):
    from .. import providers
    providers.resolve(cfg, 'marketo')


def _lookup(mk, ctx, filter_type, values, fields):
    """Leads whose filter_type field is one of values (batches of 300, every page)."""
    leads = []
    for start in range(0, len(values), BATCH):
        token = None
        while True:
            ctx.check_cancelled()
            q = {'filterType': filter_type, 'filterValues': ','.join(values[start:start + BATCH]), 'batchSize': str(BATCH)}
            if fields:
                q['fields'] = ','.join(fields)
            if token:
                q['nextPageToken'] = token
            doc = mk.call('GET', 'v1/leads.json?' + urllib.parse.urlencode(q), ctx=ctx)
            leads.extend(doc.get('result', []))
            token = doc.get('nextPageToken')
            if not doc.get('moreResult') or not token:
                break
    return leads


class MarketoInput(Tool):
    manifest = ToolManifest(
        tool_id='core.marketo_input', version='1.0.0', display_name='Marketo Input', catalog_ids=('connectors:marketo-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Leads or activities'),),
        side_effects='network', deterministic=False,
        description='Read Marketo leads (by a filter field and values) or lead activities since a date (optionally of '
                    'chosen activity types), every page.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id'],
                       'properties': {**_COMMON, **_FIELDS,
            'read': {'enum': ['leads', 'activities'], 'default': 'leads'},
            'filter_type': {'type': 'string', 'default': 'email', 'description': 'Lead field to filter on, e.g. email or id'},
            'filter_values': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            'since': {'type': ['string', 'null'], 'default': None, 'description': 'Activities since, e.g. 2026-10-01T00:00:00Z'},
            'activity_type_ids': {'type': 'array', 'items': {'type': 'integer'}, 'maxItems': 10, 'default': []}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx)
        mk = Marketo(cfg)
        if cfg['read'] == 'leads':
            if not cfg['filter_values']:
                raise ConfigError('Reading leads needs filter_values (for example the email addresses to look up)')
            records = _lookup(mk, ctx, cfg['filter_type'], [str(v) for v in cfg['filter_values']], cfg['fields'])
        else:
            if not cfg['since']:
                raise ConfigError('Reading activities needs since, for example 2026-10-01T00:00:00Z')
            if not cfg['activity_type_ids']:
                raise ConfigError('Reading activities needs activity_type_ids (Marketo requires them; up to 10)')
            token = mk.call('GET', 'v1/activities/pagingtoken.json?' + urllib.parse.urlencode({'sinceDatetime': cfg['since']}),
                            ctx=ctx)['nextPageToken']
            records = []
            while True:
                ctx.check_cancelled()
                doc = mk.call('GET', 'v1/activities.json?' + urllib.parse.urlencode(
                    {'nextPageToken': token, 'activityTypeIds': ','.join(map(str, cfg['activity_type_ids']))}), ctx=ctx)
                for a in doc.get('result', []):
                    attrs = {x['name']: x.get('value') for x in a.pop('attributes', [])}
                    records.append({**a, **{f'attribute.{k}': v for k, v in attrs.items()}})
                token = doc.get('nextPageToken')
                if not doc.get('moreResult'):
                    break
        fields, cols = _typed(records, ['id'])
        ctx.info(f"read {len(records):,} {cfg['read']}")
        return {'output': Table(Schema(fields), cols) if records else Table([], [])}


SYNC_FIELDS = [Field('MarketoId', FieldType.INT64), Field('Status', FieldType.V_WSTRING, UNBOUNDED_TEXT),
               Field('Reasons', FieldType.V_WSTRING, UNBOUNDED_TEXT)]


class MarketoOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.marketo_output', version='1.0.0', display_name='Marketo Output', catalog_ids=('connectors:marketo-output',),
        inputs=(Anchor('input', 'I', description='Lead records; field names are REST API names'),),
        outputs=(Anchor('output', 'O', description='Each record with MarketoId, Status and Reasons'),),
        side_effects='network', deterministic=False,
        description='Create or update Marketo leads, 300 per request: createOrUpdate, createOnly, updateOnly or '
                    'createDuplicate, matched on lookup_field. Each record reports created, updated or skipped (with reasons).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id'],
                       'properties': {**_COMMON,
            'action': {'enum': ['createOrUpdate', 'createOnly', 'updateOnly', 'createDuplicate'], 'default': 'createOrUpdate'},
            'lookup_field': {'type': 'string', 'default': 'email'}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        if cfg['action'] != 'createDuplicate' and cfg['lookup_field'] not in schema:
            raise ConfigError(f"The input needs the lookup field {cfg['lookup_field']!r}")
        names = {f.name for f in SYNC_FIELDS}
        return {'output': Schema([*[f for f in schema if f.name not in names], *SYNC_FIELDS])}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        table: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _permit(ctx, writes=True)
        mk = Marketo(cfg)
        records = table.to_dicts()
        sync = {f.name for f in SYNC_FIELDS}
        out = []
        for start in range(0, len(records), BATCH):
            batch = records[start:start + BATCH]
            body = {'action': cfg['action'], 'input': [{k: _value(v) for k, v in r.items() if k not in sync and v is not None}
                                                       for r in batch]}
            if cfg['action'] != 'createDuplicate':
                body['lookupField'] = cfg['lookup_field']
            result = mk.call('POST', 'v1/leads.json', body, ctx=ctx).get('result', [])
            if len(result) != len(batch):
                raise DataError('Marketo returned a result list that does not match the leads sent')
            for rec, res in zip(batch, result):
                reasons = '; '.join(f"{x.get('code')}: {x.get('message')}" for x in res.get('reasons', []))
                out.append([rec.get(f.name) for f in schema if f.name not in sync] + [res.get('id'), res.get('status'), reasons or None])
        ctx.info(f"{len(out):,} leads sent ({cfg['action']})")
        return {'output': Table.from_rows(schema, out)}


class MarketoAppend(Tool):
    manifest = ToolManifest(
        tool_id='core.marketo_append', version='1.0.0', display_name='Marketo Append', catalog_ids=('connectors:marketo-append',),
        inputs=(Anchor('input', 'I', description='Records with a lookup value'),),
        outputs=(Anchor('output', 'O', description='Each record with the matching lead fields appended'),),
        side_effects='network', deterministic=False,
        description='Append Marketo lead fields to each incoming record by looking the lead up on a field (for example '
                    'email): 300 distinct values per request. Records without a match get nulls.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'key_field'],
                       'properties': {**_COMMON, **_FIELDS,
            'key_field': {'type': 'string', 'minLength': 1, 'description': 'Input field holding the lookup value'},
            'filter_type': {'type': 'string', 'default': 'email', 'description': 'Marketo lead field it matches'},
            'prefix': {'type': 'string', 'default': 'Marketo_'}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        if cfg['key_field'] not in inputs['input']:
            raise ConfigError(f"The input has no field {cfg['key_field']!r}")
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        table: Table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        _permit(ctx)
        mk = Marketo(cfg)
        keys = [format_value(v) for v in table.column(cfg['key_field'])]
        distinct = sorted({k for k in keys if k})
        fields = list(cfg['fields']) or []
        if fields and cfg['filter_type'] not in fields:
            fields.append(cfg['filter_type'])
        leads = _lookup(mk, ctx, cfg['filter_type'], distinct, fields)
        by_key = {}
        for lead in leads:
            k = format_value(lead.get(cfg['filter_type'])) if lead.get(cfg['filter_type']) is not None else None
            if k is not None and k not in by_key:
                by_key[k] = lead
        lead_fields, _ = _typed(leads, ['id'])
        appended = [Field(cfg['prefix'] + f.name, f.type, f.size, f.scale) for f in lead_fields]
        clash = [f.name for f in appended if f.name in table.schema]
        if clash:
            raise ConfigError(f'Appended field(s) {clash} already exist; choose another prefix')
        schema = Schema([*table.schema, *appended])
        rows = []
        for row, key in zip(table.rows(), keys):
            lead = by_key.get(key, {})
            rows.append([*row, *[cast_value(_cell(lead.get(f.name)), a)[0] for f, a in zip(lead_fields, appended)]])
        ctx.info(f'{sum(k in by_key for k in keys):,} of {len(keys):,} records matched a lead')
        return {'output': Table.from_rows(schema, rows)}


TOOLS = [MarketoInput, MarketoOutput, MarketoAppend]
