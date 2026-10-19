"""Allocate Input, Append, MetaInfo and Report on the US Census Bureau American Community Survey (ACS) API.

Functional source: the public Allocate tool help. The reference reads a licensed demographic dataset; Flowbench
reads public-domain ACS estimates instead (provider ``us-census-acs``), so the variables, vintages and values are
the Census Bureau's, not the reference dataset's. Standard geographies only: state, county, census tract, block
group and ZIP code tabulation area, keyed by their GEOID. Allocation to custom polygons (the reference's block
centroid retrieval) is not available: ACS does not publish block-level estimates.

The API needs a free key: a Web/API connection to ``https://api.census.gov/data`` with authentication "Signed URL"
and the secret ``key=<your key>``. Values the Bureau marks as not available (its negative sentinel codes) are null.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields
from .github import _permit
from .msgraph import _NoRedirect

TEXT = 2**30 - 1
MAX_VARIABLES = 49            # the API allows 50 names per request, NAME included
LEVELS = {'state': 2, 'county': 5, 'tract': 11, 'block group': 12, 'zip code tabulation area': 5}
_VAR = re.compile(r'^[A-Z][A-Z0-9_]{2,30}$')
SENTINELS = {-999999999, -888888888, -666666666, -555555555, -333333333, -222222222}


class Census:
    def __init__(self, cfg):
        from ..service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        self.conn = store.get(cfg['connection_id'], kind='web')
        if self.conn.auth not in ('signed-url', 'none'):
            raise ConfigError('A Census API connection uses authentication "Signed URL" with the secret key=<your key>')
        self.key = store.secret(self.conn).lstrip('?') if self.conn.auth == 'signed-url' else ''
        self.base = f"{self.conn.base_url.rstrip('/')}/{cfg['year']}/{cfg['dataset']}"
        self.timeout = cfg['timeout_seconds']
        self.meta = {}

    def get(self, path, params=None):
        query = urllib.parse.urlencode(params or {}, safe=':*,')
        url = f'{self.base}{path}' + (f'?{query}' if query else '')
        if not self.conn.covers(url):
            raise ConfigError(f'{url} is outside the connection base URL')
        if self.key:
            url += ('&' if '?' in url else '?') + self.key
        req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'Flowbench'})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read(2000).decode('utf-8', 'replace')
            if e.code in (301, 302) and 'key' in (e.headers.get('Location') or ''):
                raise DataError('The Census API needs a valid key: request one free at https://api.census.gov/data/key_signup.html '
                                'and save it in the connection as key=<your key>') from None
            if e.code == 204:
                return []
            raise DataError(f'The Census API returned {e.code}: {detail.strip()[:300]}') from None
        except (urllib.error.URLError, OSError) as e:
            raise DataError(f'Could not reach the Census API: {getattr(e, "reason", None) or e}') from None
        if not body.strip():
            return []
        try:
            return json.loads(body)
        except ValueError:
            raise DataError(f'The Census API did not return JSON: {body[:200].decode("utf-8", "replace")}') from None

    def variable(self, code):
        if code not in self.meta:
            self.meta[code] = self.get(f'/variables/{code}.json')
        return self.meta[code]

    def table(self, variables, geo_for, geo_in=None):
        """Rows as dicts for the variables (chunked) at one geography query."""
        rows = None
        geo_keys = []
        for start in range(0, len(variables), MAX_VARIABLES):
            chunk = variables[start:start + MAX_VARIABLES]
            params = {'get': ','.join(['NAME', *chunk]), 'for': geo_for}
            if geo_in:
                params['in'] = geo_in
            data = self.get('', params)
            if not data:
                return []
            header, body = data[0], data[1:]
            geo_keys = [h for h in header if h not in ('NAME', *chunk)]
            part = {tuple(r[header.index(k)] for k in geo_keys): dict(zip(header, r)) for r in body}
            if rows is None:
                rows = part
            else:
                for k, v in part.items():
                    rows.setdefault(k, {}).update(v)
        return [{**v, 'GEOID': ''.join(v[k] for k in geo_keys)} for v in (rows or {}).values()]


def _typed_value(raw, predicate):
    if raw in (None, ''):
        return None
    if predicate in ('int', 'float'):
        try:
            v = int(raw) if predicate == 'int' else float(raw)
        except ValueError:
            return None
        return None if v in SENTINELS else v
    return raw


def _field(code, predicate):
    return Field(code, FieldType.INT64 if predicate == 'int' else FieldType.DOUBLE if predicate == 'float' else FieldType.V_WSTRING,
                 TEXT if predicate not in ('int', 'float') else None)


_COMMON = {
    'provider': {'type': 'string', 'enum': ['us-census-acs']}, 'provider_semantics': {'type': 'string'},
    'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                      'description': 'https://api.census.gov/data, Signed URL with key=<your key>'},
    'year': {'type': 'integer', 'minimum': 2009, 'maximum': 2100, 'default': 2023},
    'dataset': {'enum': ['acs/acs5', 'acs/acs1', 'acs/acs5/profile', 'acs/acs5/subject'], 'default': 'acs/acs5'},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120},
}
_VARS = {'variables': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'type': 'string', 'pattern': _VAR.pattern},
                       'description': 'ACS variable codes, e.g. B01003_001E (total population); see Allocate MetaInfo'}}


def _provider(cfg):
    from .. import providers
    providers.resolve(cfg, 'demographics')


def _check_vars(cfg):
    bad = [v for v in cfg['variables'] if not _VAR.match(v)]
    if bad:
        raise ConfigError(f'{bad} are not Census variable codes')


def _geo_query(level, state=None, county=None, codes=None):
    """(for, in) for a level, within an optional state and county."""
    if level == 'state':
        return f"state:{','.join(codes) if codes else (state or '*')}", None
    if level == 'zip code tabulation area':
        return f"zip code tabulation area:{','.join(codes) if codes else '*'}", None
    if not state:
        raise ConfigError(f'Reading {level} needs a state (FIPS code, for example 06 for California)')
    if level == 'county':
        return f"county:{','.join(codes) if codes else (county or '*')}", f'state:{state}'
    if not county:
        raise ConfigError(f'Reading {level} needs a county as well as a state (Census API rule)')
    return f"{level}:{','.join(codes) if codes else '*'}", f'state:{state} county:{county}'


class AllocateInput(Tool):
    manifest = ToolManifest(
        tool_id='core.allocate_input', version='1.0.0', display_name='Allocate Input', catalog_ids=('demographic-analysis:allocate-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per geography with the chosen variables'),),
        side_effects='network', deterministic=False,
        description='Read US Census ACS estimates for every geography of a level (state, county, tract, block group or '
                    'ZIP code tabulation area) within a state and county: GEOID, NAME and the chosen variables.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'level', 'variables'],
                       'properties': {**_COMMON, **_VARS,
            'level': {'enum': list(LEVELS)},
            'state': {'type': ['string', 'null'], 'default': None, 'pattern': '^[0-9]{2}$'},
            'county': {'type': ['string', 'null'], 'default': None, 'pattern': '^[0-9]{3}$'}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _check_vars(cfg)
        _permit(ctx)
        api = Census(cfg)
        geo_for, geo_in = _geo_query(cfg['level'], cfg['state'], cfg['county'])
        rows = api.table(cfg['variables'], geo_for, geo_in)
        preds = {v: api.variable(v).get('predicateType') for v in cfg['variables']}
        schema = Schema([Field('GEOID', FieldType.V_WSTRING, TEXT), Field('NAME', FieldType.V_WSTRING, TEXT),
                         *[_field(v, preds[v]) for v in cfg['variables']]])
        out = sorted(([r['GEOID'], r.get('NAME'), *[_typed_value(r.get(v), preds[v]) for v in cfg['variables']]] for r in rows),
                     key=lambda r: r[0])
        ctx.info(f"{len(out):,} {cfg['level']} geographies ({cfg['year']} {cfg['dataset']})")
        return {'output': Table.from_rows(schema, out)}


class AllocateAppend(Tool):
    manifest = ToolManifest(
        tool_id='core.allocate_append', version='1.0.0', display_name='Allocate Append', catalog_ids=('demographic-analysis:allocate-append',),
        inputs=(Anchor('input', 'I', description='Records with a geography GEOID'),), outputs=(Anchor('output', 'O'),),
        side_effects='network', deterministic=False,
        description='Append US Census ACS estimates to records keyed by a standard geography GEOID (state 2 digits, county 5, '
                    'tract 11, block group 12, or a 5-digit ZCTA). Unknown keys get nulls; custom polygons are not supported.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'key_field', 'level', 'variables'], 'properties': {**_COMMON, **_VARS,
            'key_field': {'type': 'string', 'minLength': 1}, 'level': {'enum': list(LEVELS)},
            'prefix': {'type': 'string', 'default': ''}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['key_field']])
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _check_vars(cfg)
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        width = LEVELS[cfg['level']]
        keys = [re.sub(r'\D', '', format_value(k)) if k is not None else '' for k in table.column(cfg['key_field'])]
        bad = sorted({k for k in keys if k and len(k) != width})[:5]
        if bad:
            raise DataError(f"{cfg['level']} GEOIDs have {width} digits; found {bad}")
        _permit(ctx)
        api = Census(cfg)
        wanted = sorted({k for k in keys if k})
        found = {}
        if cfg['level'] in ('state', 'zip code tabulation area'):
            for start in range(0, len(wanted), 50):
                geo_for, _ = _geo_query(cfg['level'], codes=wanted[start:start + 50])
                found.update({r['GEOID']: r for r in api.table(cfg['variables'], geo_for)})
        else:
            parents = sorted({(k[:2], k[2:5]) if cfg['level'] != 'county' else (k[:2], None) for k in wanted})
            for state, county in parents:  # one request per state (counties) or per county (tracts, block groups)
                ctx.check_cancelled()
                geo_for, geo_in = _geo_query(cfg['level'], state, county)
                found.update({r['GEOID']: r for r in api.table(cfg['variables'], geo_for, geo_in)})
        preds = {v: api.variable(v).get('predicateType') for v in cfg['variables']}
        added = [_field(cfg['prefix'] + v, preds[v]) for v in cfg['variables']] + [Field(cfg['prefix'] + 'GeographyName', FieldType.V_WSTRING, TEXT)]
        clash = [f.name for f in added if f.name in table.schema]
        if clash:
            raise ConfigError(f'Field(s) {clash} already exist; set a prefix')
        schema = Schema([*table.schema, *added])
        rows = []
        for row, k in zip(table.rows(), keys):
            r = found.get(k, {})
            rows.append([*row, *[_typed_value(r.get(v), preds[v]) for v in cfg['variables']], r.get('NAME')])
        ctx.info(f'{sum(1 for k in keys if k in found):,} of {len(keys):,} records matched a {cfg["level"]}')
        return {'output': Table.from_rows(schema, rows)}


class AllocateMetaInfo(Tool):
    manifest = ToolManifest(
        tool_id='core.allocate_metainfo', version='1.0.0', display_name='Allocate MetaInfo', catalog_ids=('demographic-analysis:allocate-metainfo',),
        inputs=(), outputs=(Anchor('output', 'O', description='One record per variable'),), side_effects='network', deterministic=False,
        description='List the variables of a US Census ACS dataset: code, label, concept, table group and type. Narrow it to '
                    'table groups (e.g. B01003) or a search text.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id'], 'properties': {**_COMMON,
            'groups': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[A-Z][A-Z0-9_]{2,20}$'}, 'default': []},
            'search': {'type': ['string', 'null'], 'default': None}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return {'output': Schema([Field(n, FieldType.V_WSTRING, TEXT) for n in ('Variable', 'Label', 'Concept', 'Group', 'Type')])}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        _permit(ctx)
        api = Census(cfg)
        variables = {}
        if cfg['groups']:
            for g in cfg['groups']:
                variables.update((api.get(f'/groups/{g}.json') or {}).get('variables', {}))
        else:
            variables = (api.get('/variables.json') or {}).get('variables', {})
        needle = (cfg['search'] or '').lower()
        rows = sorted((code, v.get('label'), v.get('concept'), v.get('group'), v.get('predicateType'))
                      for code, v in variables.items()
                      if _VAR.match(code) and code not in ('NAME',) and (not needle or needle in f"{code} {v.get('label')} {v.get('concept')}".lower()))
        return {'output': Table.from_rows(self.infer(ctx, cfg, {})['output'], rows)}


class AllocateReport(Tool):
    manifest = ToolManifest(
        tool_id='core.allocate_report', version='1.0.0', display_name='Allocate Report', catalog_ids=('demographic-analysis:allocate-report',),
        inputs=(Anchor('input', 'I', description='Records from Allocate Input or Append'),),
        outputs=(Anchor('output', 'O', description='One record with the report table'),), side_effects='network', deterministic=False,
        description='A report table of Census variables (rows, labelled from the dataset) for each geography (columns), '
                    'ready for the Render and Layout tools.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'geography_field', 'variables'], 'properties': {**_COMMON, **_VARS,
            'geography_field': {'type': 'string', 'minLength': 1, 'description': 'Field naming each geography, e.g. NAME'},
            'title': {'type': 'string', 'default': 'Demographic profile'}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['geography_field'], *cfg['variables']])
        return {'output': Schema([Field('Report', FieldType.V_WSTRING, TEXT)])}

    def run(self, ctx, cfg, inputs):
        from .. import reports
        _provider(cfg)
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _permit(ctx)
        api = Census(cfg)
        recs = table.to_dicts()
        columns = [{'label': 'Variable', 'align': 'left'}] + [{'label': format_value(r[cfg['geography_field']]), 'align': 'right'} for r in recs]
        rows = []
        for v in cfg['variables']:
            meta = api.variable(v)
            label = (meta.get('label') or v).replace('Estimate!!', '').replace('!!', ' – ')
            rows.append([f"{label} ({v})"] + ['' if r[v] is None else f"{r[v]:,}" if isinstance(r[v], (int, float)) else format_value(r[v])
                                              for r in recs])
        doc = reports.element('table', title=cfg['title'], group=None, columns=columns, rows=rows, total_row=None)
        return {'output': Table.from_rows(schema, [(doc,)])}


TOOLS = [AllocateInput, AllocateAppend, AllocateMetaInfo, AllocateReport]
