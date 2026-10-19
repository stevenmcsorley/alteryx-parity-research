"""Address tools with open-source and public alternatives to the reference's licensed address data.

Functional source: the public Address tool help. None of these use the reference's CASS library or address data
(they need a separate licensed install there); each names its own provider, and results differ from the reference:

* Parse Address - usaddress (MIT), a statistical model of US address parts, run locally. Output fields follow the
  reference (CASSParse_*); the Canada-only fields stay empty because the model is US-only.
* Street Geocoder / US Geocoder - provider ``us-census-geocoder`` (the US Census Bureau Geocoder: public, free,
  TIGER/Line address ranges, US only; batches of up to 10,000 addresses) or ``nominatim`` (OpenStreetMap, worldwide;
  the public server allows one request per second and no bulk use, so large jobs need a self-hosted server).
* Reverse Geocoder - ``nominatim`` returns the nearest address; ``us-census-geocoder`` returns census geographies
  (state, county, tract, block), not a street address.
* CASS - provider ``smarty`` (the Smarty US Street API, a USPS CASS-certified vendor service; account required).
* US ZIP9 Coder - a ZIP+4 centroid data pack the user registers (ZIP+4 location data is licensed; Flowbench ships none).
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields
from .download import _HttpOnlyRedirects
from .github import _permit

TEXT = 2**30 - 1
CENSUS_BATCH = 10_000
SMARTY_BATCH = 100
PUBLIC_NOMINATIM = 'nominatim.openstreetmap.org'
USER_AGENT = 'Flowbench/1.0 (https://github.com/stevenmcsorley/alteryx-parity-research)'


def _t(name):
    return Field(name, FieldType.V_WSTRING, TEXT)


def _text(v):
    return None if v is None else format_value(v).strip() or None


# --------------------------------------------------------------------------- Parse Address

PARSE_FIELDS = ['CASSParse_CA_DeliveryInstallation', 'CASSParse_CA_LockBox', 'CASSParse_CA_RouteService', 'CASSParse_Garbage',
                'CASSParse_PostDirection', 'CASSParse_PreDirection', 'CASSParse_PrivateMailboxName',
                'CASSParse_PrivateMailboxNumber', 'CASSParse_Range', 'CASSParse_StreetName', 'CASSParse_Suffix',
                'CASSParse_SuiteName', 'CASSParse_SuiteNumber']
LAST_LINE_FIELDS = ['CASSParse_City', 'CASSParse_State', 'CASSParse_Plus4', 'CASSParse_Zip']
_TAGS = {
    'CASSParse_Range': ('AddressNumberPrefix', 'AddressNumber', 'AddressNumberSuffix'),
    'CASSParse_PreDirection': ('StreetNamePreDirectional',),
    'CASSParse_StreetName': ('StreetNamePreModifier', 'StreetNamePreType', 'StreetName'),
    'CASSParse_Suffix': ('StreetNamePostType',),
    'CASSParse_PostDirection': ('StreetNamePostDirectional',),
    'CASSParse_SuiteName': ('OccupancyType', 'SubaddressType'),
    'CASSParse_SuiteNumber': ('OccupancyIdentifier', 'SubaddressIdentifier'),
    'CASSParse_PrivateMailboxName': ('USPSBoxType',),
    'CASSParse_PrivateMailboxNumber': ('USPSBoxID',),
    'CASSParse_City': ('PlaceName',), 'CASSParse_State': ('StateName',),
}
_KNOWN = {t for tags in _TAGS.values() for t in tags} | {'ZipCode', 'ZipPlus4'}


def parse_us_address(text: str) -> dict:
    import usaddress
    try:
        parts = usaddress.parse(text)
    except Exception:
        return {'CASSParse_Garbage': text}
    out, garbage = {}, []
    for token, label in parts:
        token = token.strip(',;')
        if not token:
            continue
        if label in _KNOWN:
            out.setdefault(label, []).append(token)
        else:
            garbage.append(token)
    result = {name: ' '.join(sum((out.get(t, []) for t in tags), [])) or None for name, tags in _TAGS.items()}
    zips = ' '.join(out.get('ZipCode', []) + out.get('ZipPlus4', []))
    m = re.search(r'(\d{5})(?:[- ]?(\d{4}))?', zips)
    result['CASSParse_Zip'], result['CASSParse_Plus4'] = (m.group(1), m.group(2)) if m else (None, None)
    result['CASSParse_Garbage'] = ' '.join(garbage) or None
    return result


class ParseAddress(Tool):
    manifest = ToolManifest(
        tool_id='core.parse_address', version='1.0.0', display_name='Parse Address', catalog_ids=('address:parse-address',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Split US street addresses into range, directions, street name, suffix, suite and PO box '
                    '(CASSParse_* fields), and city, state, ZIP and ZIP+4 when a last-line field is given. Uses the '
                    'open-source usaddress model locally; it does not validate addresses (that is CASS).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['address_field'], 'properties': {
            'address_field': {'type': 'string', 'minLength': 1},
            'last_line_field': {'type': ['string', 'null'], 'default': None, 'description': 'City, state and ZIP'}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['address_field']] + ([cfg['last_line_field']] if cfg['last_line_field'] else []))
        names = PARSE_FIELDS + (LAST_LINE_FIELDS if cfg['last_line_field'] else [])
        clash = [n for n in names if n in schema]
        if clash:
            raise ConfigError(f'Field(s) {clash} already exist')
        return {'output': Schema([*schema, *[_t(n) for n in names]])}

    def run(self, ctx, cfg, inputs):
        try:
            import usaddress  # noqa: F401
        except ImportError:
            raise ConfigError('Parse Address needs the usaddress package') from None
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        names = PARSE_FIELDS + (LAST_LINE_FIELDS if cfg['last_line_field'] else [])
        addr = table.column(cfg['address_field'])
        last = table.column(cfg['last_line_field']) if cfg['last_line_field'] else [None] * len(table)
        rows = []
        for row, a, ll in zip(table.rows(), addr, last):
            text = ', '.join(x for x in (_text(a), _text(ll)) if x)
            parsed = parse_us_address(text) if text else {}
            if not cfg['last_line_field']:
                for k in LAST_LINE_FIELDS:
                    parsed.pop(k, None)
            rows.append([*row, *[parsed.get(n) for n in names]])
        return {'output': Table.from_rows(schema, rows)}


# --------------------------------------------------------------------------- geocoding providers

class _Http:
    def __init__(self, cfg, kind='web'):
        from ..service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        self.conn = store.get(cfg['connection_id'], kind=kind)
        self.secret = store.secret(self.conn)
        self.base = self.conn.base_url.rstrip('/')
        self.timeout = cfg['timeout_seconds']
        self.opener = urllib.request.build_opener(_HttpOnlyRedirects(self.conn))
        self.public_nominatim = urllib.parse.urlsplit(self.base).hostname == PUBLIC_NOMINATIM
        self._last = 0.0
        self.cache = {}

    def get(self, path, params=None, body=None, content_type=None, raw=False):
        url = f'{self.base}/{path}' + (('?' + urllib.parse.urlencode(params)) if params else '')
        if not self.conn.covers(url):
            raise ConfigError(f'{url} is outside the connection base URL')
        if body is None and url in self.cache:
            return self.cache[url]
        if self.public_nominatim:  # the public server allows at most one request per second
            wait = 1.0 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
        if self.conn.auth not in ('none', 'basic'):
            headers.update(self.conn.authorize(url, self.secret))
        if content_type:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=body, method='POST' if body is not None else 'GET', headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read(2000).decode('utf-8', 'replace')
            raise DataError(f'{self.base} returned {e.code}: {detail[:300]}') from None
        except (urllib.error.URLError, OSError) as e:
            raise DataError(f'Could not reach {self.base}: {getattr(e, "reason", None) or e}') from None
        result = data if raw else json.loads(data or b'null')
        if body is None:
            self.cache[url] = result
        return result


GEO_FIELDS = [Field('Latitude', FieldType.DOUBLE), Field('Longitude', FieldType.DOUBLE), _t('MatchedAddress'), _t('MatchStatus'),
              _t('MatchType'), _t('GeocodeSource')]
_GEO_PROPS = {
    'provider': {'type': 'string', 'enum': ['us-census-geocoder', 'nominatim']}, 'provider_semantics': {'type': 'string'},
    'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                      'description': 'https://geocoding.geo.census.gov/geocoder or a Nominatim server, authentication None'},
    'address_field': {'type': 'string', 'minLength': 1, 'description': 'Street address, or the whole one-line address'},
    'city_field': {'type': ['string', 'null'], 'default': None}, 'state_field': {'type': ['string', 'null'], 'default': None},
    'zip_field': {'type': ['string', 'null'], 'default': None},
    'country_codes': {'type': ['string', 'null'], 'default': None, 'description': 'Nominatim only, e.g. gb,ie'},
    'benchmark': {'type': 'string', 'default': 'Public_AR_Current', 'description': 'Census address range benchmark'},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120},
}


def _geocode_census(http, ctx, cfg, records):
    """Census batch geocoding: CSV of id, street, city, state, zip; up to 10,000 per request."""
    out = {}
    for start in range(0, len(records), CENSUS_BATCH):
        ctx.check_cancelled()
        buf = io.StringIO()
        w = csv.writer(buf)
        for i, rec in records[start:start + CENSUS_BATCH]:
            w.writerow([i, rec['street'] or '', rec['city'] or '', rec['state'] or '', rec['zip'] or ''])
        boundary = 'flowbench' + format(int(time.time() * 1000), 'x')
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="benchmark"\r\n\r\n{cfg["benchmark"]}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="addressFile"; filename="addresses.csv"\r\n'
                'Content-Type: text/csv\r\n\r\n').encode() + buf.getvalue().encode() + f'\r\n--{boundary}--\r\n'.encode()
        text = http.get('locations/addressbatch', body=body, content_type=f'multipart/form-data; boundary={boundary}',
                        raw=True).decode('utf-8', 'replace')
        for line in csv.reader(io.StringIO(text)):
            if not line or not line[0].strip().isdigit():
                continue
            i = int(line[0])
            status = line[2] if len(line) > 2 else 'No_Match'
            if status == 'Match' and len(line) > 5 and ',' in line[5]:
                lon, lat = (float(v) for v in line[5].split(',')[:2])
                out[i] = [lat, lon, line[4], 'Match', line[3], 'US Census Geocoder']
            else:
                out[i] = [None, None, None, status or 'No_Match', None, 'US Census Geocoder']
    return out


def _geocode_nominatim(http, ctx, cfg, records):
    if http.public_nominatim and len(records) > 100:
        ctx.warn(f'{len(records)} addresses at one per second on the public Nominatim server; its policy discourages bulk '
                 'geocoding - use a self-hosted Nominatim for large jobs')
    out = {}
    for i, rec in records:
        ctx.check_cancelled()
        q = ', '.join(x for x in (rec['street'], rec['city'], rec['state'], rec['zip']) if x)
        params = {'q': q, 'format': 'jsonv2', 'limit': '1', 'addressdetails': '0'}
        if cfg['country_codes']:
            params['countrycodes'] = cfg['country_codes']
        found = http.get('search', params) or []
        if found:
            f = found[0]
            out[i] = [float(f['lat']), float(f['lon']), f.get('display_name'), 'Match', f.get('addresstype') or f.get('type'),
                      'Nominatim (OpenStreetMap)']
        else:
            out[i] = [None, None, None, 'No_Match', None, 'Nominatim (OpenStreetMap)']
    return out


class _Geocoder(Tool):
    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg[k] for k in ('address_field', 'city_field', 'state_field', 'zip_field') if cfg.get(k)])
        clash = [f.name for f in GEO_FIELDS if f.name in schema]
        if clash:
            raise ConfigError(f'Field(s) {clash} already exist')
        return {'output': Schema([*schema, *GEO_FIELDS])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'geocoding')
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _permit(ctx)
        http = _Http(cfg)
        col = {k: table.column(cfg[f'{k}_field']) if cfg.get(f'{k}_field') else [None] * len(table)
               for k in ('address', 'city', 'state', 'zip')}
        records = [(i, {'street': _text(col['address'][i]), 'city': _text(col['city'][i]), 'state': _text(col['state'][i]),
                        'zip': _text(col['zip'][i])}) for i in range(len(table))]
        todo = [(i, r) for i, r in records if r['street']]
        if cfg['provider'] == 'us-census-geocoder':
            if not (cfg['city_field'] or cfg['zip_field']):
                raise ConfigError('The US Census Geocoder batch needs city (and state) or ZIP fields as well as the street')
            found = _geocode_census(http, ctx, cfg, todo)
        else:
            found = _geocode_nominatim(http, ctx, cfg, todo)
        empty = [None, None, None, 'No_Address', None, None]
        rows = [[*row, *found.get(i, empty)] for i, row in enumerate(table.rows())]
        ctx.info(f"{sum(1 for v in found.values() if v[3] == 'Match'):,} of {len(table):,} addresses matched")
        return {'output': Table.from_rows(schema, rows)}


def _geocoder(tool_id, name, catalog_id):
    class G(_Geocoder):
        manifest = ToolManifest(
            tool_id=tool_id, version='1.0.0', display_name=name, catalog_ids=(catalog_id,),
            inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),), side_effects='network', deterministic=False,
            description='Geocode addresses to latitude and longitude with the named provider: the US Census Geocoder '
                        '(public, US, batches of 10,000) or Nominatim (OpenStreetMap; the public server allows one request '
                        'per second). Adds Latitude, Longitude, MatchedAddress, MatchStatus, MatchType and GeocodeSource.',
            config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'address_field'],
                           'properties': dict(_GEO_PROPS)})
    G.__name__ = G.__qualname__ = name.replace(' ', '')
    return G


StreetGeocoder = _geocoder('core.street_geocoder', 'Street Geocoder', 'address:street-geocoder')
USGeocoder = _geocoder('core.us_geocoder', 'US Geocoder', 'address:us-geocoder')

REVERSE_FIELDS = [_t('ReverseAddress'), _t('ReverseCity'), _t('ReverseState'), _t('ReversePostcode'), _t('ReverseCountry'),
                  _t('StateFIPS'), _t('CountyFIPS'), _t('Tract'), _t('Block'), _t('GeocodeSource')]


class ReverseGeocoder(Tool):
    manifest = ToolManifest(
        tool_id='core.reverse_geocoder', version='1.0.0', display_name='Reverse Geocoder', catalog_ids=('address:reverse-geocoder',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),), side_effects='network', deterministic=False,
        description='Find what is at each longitude/latitude: the nearest address (Nominatim) or the census state, '
                    'county, tract and block (US Census Geocoder, US only; it returns geographies, not street addresses).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'latitude_field', 'longitude_field'], 'properties': {
            'provider': {'type': 'string', 'enum': ['us-census-geocoder', 'nominatim']}, 'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web'},
            'latitude_field': {'type': 'string', 'minLength': 1}, 'longitude_field': {'type': 'string', 'minLength': 1},
            'benchmark': {'type': 'string', 'default': 'Public_AR_Current'}, 'vintage': {'type': 'string', 'default': 'Current_Current'},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['latitude_field'], cfg['longitude_field']])
        return {'output': Schema([*schema, *[f for f in REVERSE_FIELDS if f.name not in schema]])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'geocoding')
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _permit(ctx)
        http = _Http(cfg)
        rows = []
        for row, lat, lon in zip(table.rows(), table.column(cfg['latitude_field']), table.column(cfg['longitude_field'])):
            ctx.check_cancelled()
            vals = [None] * 9
            if lat is not None and lon is not None:
                if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
                    raise DataError(f'({lat}, {lon}) is not a latitude/longitude')
                if cfg['provider'] == 'nominatim':
                    r = http.get('reverse', {'lat': f'{float(lat):.7f}', 'lon': f'{float(lon):.7f}', 'format': 'jsonv2', 'addressdetails': '1'})
                    a = (r or {}).get('address') or {}
                    vals[:5] = [(r or {}).get('display_name'), a.get('city') or a.get('town') or a.get('village'), a.get('state'),
                                a.get('postcode'), a.get('country_code', '').upper() or None]
                else:
                    r = http.get('geographies/coordinates', {'x': f'{float(lon):.7f}', 'y': f'{float(lat):.7f}', 'benchmark': cfg['benchmark'],
                                                             'vintage': cfg['vintage'], 'format': 'json'})
                    geo = ((r or {}).get('result') or {}).get('geographies') or {}
                    block = (geo.get('2020 Census Blocks') or geo.get('Census Blocks') or [{}])[0]
                    vals[5:9] = [block.get('STATE'), block.get('COUNTY'), block.get('TRACT'), block.get('BLOCK')]
            source = 'Nominatim (OpenStreetMap)' if cfg['provider'] == 'nominatim' else 'US Census Geocoder'
            extra = dict(zip([f.name for f in REVERSE_FIELDS], vals + [source]))
            rows.append([*row, *[extra[f.name] for f in list(schema)[len(table.schema):]]])
        return {'output': Table.from_rows(schema, rows)}


# --------------------------------------------------------------------------- CASS (Smarty)

CASS_FIELDS = [_t('CASS_DeliveryLine1'), _t('CASS_LastLine'), _t('CASS_City'), _t('CASS_State'), _t('CASS_Zip'), _t('CASS_Plus4'),
               _t('CASS_DeliveryPoint'), _t('CASS_County'), _t('CASS_DPVMatchCode'), _t('CASS_DPVFootnotes'),
               Field('CASS_Latitude', FieldType.DOUBLE), Field('CASS_Longitude', FieldType.DOUBLE), _t('CASS_Status')]


class Cass(Tool):
    manifest = ToolManifest(
        tool_id='core.cass', version='1.0.0', display_name='CASS', catalog_ids=('address:cass',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),), side_effects='network', deterministic=False,
        description='Standardise and validate US addresses with a USPS CASS-certified service (provider smarty): '
                    'delivery line, last line, ZIP+4, delivery point, county, DPV match code, coordinates. 100 per request.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'address_field'], 'properties': {
            'provider': {'type': 'string', 'enum': ['smarty']}, 'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                              'description': 'https://us-street.api.smarty.com, basic: auth-id as username, auth-token as secret'},
            'address_field': {'type': 'string', 'minLength': 1}, 'city_field': {'type': ['string', 'null'], 'default': None},
            'state_field': {'type': ['string', 'null'], 'default': None}, 'zip_field': {'type': ['string', 'null'], 'default': None},
            'match': {'enum': ['strict', 'invalid', 'enhanced'], 'default': 'strict'},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg[k] for k in ('address_field', 'city_field', 'state_field', 'zip_field') if cfg.get(k)])
        clash = [f.name for f in CASS_FIELDS if f.name in schema]
        if clash:
            raise ConfigError(f'Field(s) {clash} already exist')
        return {'output': Schema([*schema, *CASS_FIELDS])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'address-validation')
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _permit(ctx)
        http = _Http(cfg)
        if http.conn.auth != 'basic':
            raise ConfigError('A Smarty connection uses basic authentication: the auth-id as username and the auth-token as secret')
        cols = {k: table.column(cfg[f'{k}_field']) if cfg.get(f'{k}_field') else [None] * len(table)
                for k in ('address', 'city', 'state', 'zip')}
        results = {}
        for start in range(0, len(table), SMARTY_BATCH):
            ctx.check_cancelled()
            batch = [{'input_id': str(i), 'street': _text(cols['address'][i]) or '', 'city': _text(cols['city'][i]) or '',
                      'state': _text(cols['state'][i]) or '', 'zipcode': _text(cols['zip'][i]) or '', 'candidates': 1,
                      'match': cfg['match']} for i in range(start, min(start + SMARTY_BATCH, len(table)))]
            # Smarty authenticates with auth-id and auth-token query parameters (its documented secret-key method).
            found = http.get(f"street-address?{urllib.parse.urlencode({'auth-id': http.conn.username, 'auth-token': http.secret})}",
                             body=json.dumps(batch).encode(), content_type='application/json; charset=utf-8')
            for cand in found or []:
                c, m, a = cand.get('components', {}), cand.get('metadata', {}), cand.get('analysis', {})
                plus = '-'.join(x for x in (c.get('zipcode'), c.get('plus4_code')) if x)
                results[int(cand.get('input_id', start + cand.get('input_index', 0)))] = [
                    cand.get('delivery_line_1'), cand.get('last_line'), c.get('city_name'), c.get('state_abbreviation'),
                    c.get('zipcode'), c.get('plus4_code'), c.get('delivery_point'), m.get('county_name'), a.get('dpv_match_code'),
                    a.get('dpv_footnotes'), m.get('latitude'), m.get('longitude'), 'Valid' if a.get('dpv_match_code') == 'Y'
                    else 'Partial' if plus else 'Unverified']
        none = [None] * 12 + ['Not found']
        rows = [[*row, *results.get(i, none)] for i, row in enumerate(table.rows())]
        ctx.info(f'{len(results):,} of {len(table):,} addresses standardised')
        return {'output': Table.from_rows(schema, rows)}


# --------------------------------------------------------------------------- US ZIP9 Coder (data pack)

ZIP9_FIELDS = [Field('ZIP9_Latitude', FieldType.DOUBLE), Field('ZIP9_Longitude', FieldType.DOUBLE), _t('ZIP9_MatchLevel')]


class UsZip9Coder(Tool):
    manifest = ToolManifest(
        tool_id='core.us_zip9_coder', version='1.0.0', display_name='US ZIP9 Coder', catalog_ids=('address:us-zip9-coder',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),), side_effects='reads-files',
        description='Add the latitude and longitude of each ZIP+4 (or ZIP when the +4 is unknown) from a ZIP+4 centroid '
                    'data pack you register (CSV with Zip5, Plus4, Latitude, Longitude; Plus4 empty for ZIP centroids).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'zip_field'], 'properties': {
            'provider': {'type': 'string', 'enum': ['zip4-data-pack']}, 'provider_semantics': {'type': 'string'},
            'zip_field': {'type': 'string', 'minLength': 1, 'description': 'ZIP or ZIP+4 (12345, 123456789 or 12345-6789)'},
            'plus4_field': {'type': ['string', 'null'], 'default': None},
            'data_pack_id': {'type': ['string', 'null'], 'default': None, 'description': 'Registered zip4-centroids pack; the only one if empty'}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['zip_field']] + ([cfg['plus4_field']] if cfg['plus4_field'] else []))
        return {'output': Schema([*schema, *ZIP9_FIELDS])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'zip4-centroids')
        packs = providers.DataPackStore().for_family('zip4-centroids')
        if cfg['data_pack_id']:
            packs = [p for p in packs if p['id'] == cfg['data_pack_id']]
        if len(packs) != 1:
            raise ConfigError('Register exactly one zip4-centroids data pack (Settings > Integrations > Data packs), or name it '
                              'with data_pack_id' if packs else 'No zip4-centroids data pack is registered; ZIP+4 location data '
                              'is licensed and Flowbench ships none (Settings > Integrations > Data packs)')
        index = {}
        with open(Path(packs[0]['path']), newline='', encoding='utf-8-sig') as fh:
            for rec in csv.DictReader(fh):
                try:
                    index[(rec['Zip5'].strip().zfill(5), (rec.get('Plus4') or '').strip())] = (float(rec['Latitude']), float(rec['Longitude']))
                except (KeyError, ValueError):
                    raise DataError('The zip4-centroids pack must have Zip5, Plus4, Latitude and Longitude columns') from None
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        plus_col = table.column(cfg['plus4_field']) if cfg['plus4_field'] else [None] * len(table)
        rows, hits = [], 0
        for row, z, p in zip(table.rows(), table.column(cfg['zip_field']), plus_col):
            digits = re.sub(r'\D', '', _text(z) or '') + re.sub(r'\D', '', _text(p) or '')
            zip5, plus4 = digits[:5], digits[5:9]
            hit = index.get((zip5, plus4)) if len(plus4) == 4 else None
            level = 'ZIP9' if hit else None
            if not hit and len(zip5) == 5:
                hit = index.get((zip5, ''))
                level = 'ZIP5' if hit else 'None'
            hits += bool(hit)
            rows.append([*row, *(hit or (None, None)), level or 'None'])
        ctx.info(f'{hits:,} of {len(table):,} records located from data pack {packs[0]["name"]}')
        return {'output': Table.from_rows(schema, rows)}


TOOLS = [ParseAddress, StreetGeocoder, USGeocoder, ReverseGeocoder, Cass, UsZip9Coder]
