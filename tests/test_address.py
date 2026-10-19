"""Address tools: Parse Address (usaddress, local), geocoders and reverse geocoder against fixtures of the US Census
Geocoder and Nominatim APIs, CASS against a fixture of the Smarty US Street API, US ZIP9 Coder with a synthetic pack.

FLOWBENCH_LIVE_CENSUS=1 also runs one geocode against the real public Census Geocoder."""
import csv
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import flowengine.db.vault as vault_module
import flowengine.tools.address as address
from flowengine import providers
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}


class H(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        H.calls.append(('GET', unquote(parts.path), q, dict(self.headers), time.monotonic()))
        if parts.path == '/geocoder/geographies/coordinates':
            assert q['benchmark'] == 'Public_AR_Current' and q['format'] == 'json'
            return self.send(200, {'result': {'geographies': {'2020 Census Blocks': [
                {'STATE': '11', 'COUNTY': '001', 'TRACT': '006202', 'BLOCK': '1031'}]}}})
        if parts.path == '/nominatim/search':
            assert self.headers['User-Agent'].startswith('Flowbench/')
            if 'Nowhere' in q['q']:
                return self.send(200, [])
            return self.send(200, [{'lat': '51.5007', 'lon': '-0.1246', 'display_name': 'Westminster, London', 'addresstype': 'building'}])
        if parts.path == '/nominatim/reverse':
            return self.send(200, {'display_name': '10 Downing St, London', 'address': {'city': 'London', 'state': 'England',
                                                                                      'postcode': 'SW1A 2AA', 'country_code': 'gb'}})
        return self.send(404, {'error': self.path})

    def do_POST(self):
        parts = urlsplit(self.path)
        body = self.rfile.read(int(self.headers['Content-Length']))
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        H.calls.append(('POST', unquote(parts.path), q, dict(self.headers), body))
        if parts.path == '/geocoder/locations/addressbatch':
            assert b'name="benchmark"\r\n\r\nPublic_AR_Current' in body
            data = body.split(b'filename="addresses.csv"')[1].split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0]
            out = io.StringIO()
            w = csv.writer(out)
            for rid, street, city, state, z in csv.reader(io.StringIO(data.decode())):
                if 'Pennsylvania' in street:
                    w.writerow([rid, f'{street}, {city}, {state}, {z}', 'Match', 'Exact', '1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500',
                                '-77.03535,38.898754', '76225813', 'L'])
                else:
                    w.writerow([rid, f'{street}, {city}, {state}, {z}', 'No_Match'])
            return self.send(200, out.getvalue().encode(), 'text/csv')
        if parts.path == '/street-address':
            assert (q['auth-id'], q['auth-token']) == ('id-1', 'tok-1')
            lookups = json.loads(body)
            assert len(lookups) <= 100
            out = []
            for i, lk in enumerate(lookups):
                if 'Bogus' in lk['street']:
                    continue
                out.append({'input_id': lk['input_id'], 'input_index': i, 'delivery_line_1': lk['street'].upper(),
                            'last_line': 'SPRINGFIELD IL 62701-1234',
                            'components': {'city_name': 'Springfield', 'state_abbreviation': 'IL', 'zipcode': '62701',
                                           'plus4_code': '1234', 'delivery_point': '05'},
                            'metadata': {'county_name': 'Sangamon', 'latitude': 39.8, 'longitude': -89.6},
                            'analysis': {'dpv_match_code': 'Y', 'dpv_footnotes': 'AABB'}})
            return self.send(200, out)
        return self.send(404, {'error': self.path})


class AddressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.root = f'http://127.0.0.1:{cls.http.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        H.calls = []
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        store = ServiceConnectionStore()
        self.census = store.save({'name': 'Census', 'kind': 'web', 'base_url': f'{self.root}/geocoder', 'auth': 'none'}).id
        self.osm = store.save({'name': 'OSM', 'kind': 'web', 'base_url': f'{self.root}/nominatim', 'auth': 'none'}).id
        self.smarty = store.save({'name': 'Smarty', 'kind': 'web', 'base_url': self.root, 'auth': 'basic', 'username': 'id-1'},
                                 secret='tok-1').id

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def run_tool(self, tool, config, fields, rows, limits=NET):
        doc = workflow([text_input('t', fields, rows), {'id': 'x', 'tool': tool, 'config': config}], [edge('t', 'output', 'x', 'input')])
        r = run_workflow(doc, base_dir=ROOT, limits=limits)
        return r, ([dict(zip(r.outputs['x']['output'].schema.names, row)) for row in r.outputs['x']['output'].rows()]
                   if r.status == 'succeeded' else None)

    def test_parse_address_fills_the_cassparse_fields(self):
        r, rows = self.run_tool('core.parse_address', {'address_field': 'a', 'last_line_field': 'b'}, [('a', 'v_wstring'), ('b', 'v_wstring')],
                                [['123 N Main St Apt 4B', 'Springfield, IL 62701-1234'], ['PO Box 77', 'Austin TX 78701'], [None, None]])
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual({k: rows[0][k] for k in ('CASSParse_Range', 'CASSParse_PreDirection', 'CASSParse_StreetName', 'CASSParse_Suffix',
                                                  'CASSParse_SuiteName', 'CASSParse_SuiteNumber', 'CASSParse_City', 'CASSParse_State',
                                                  'CASSParse_Zip', 'CASSParse_Plus4')},
                         {'CASSParse_Range': '123', 'CASSParse_PreDirection': 'N', 'CASSParse_StreetName': 'Main', 'CASSParse_Suffix': 'St',
                          'CASSParse_SuiteName': 'Apt', 'CASSParse_SuiteNumber': '4B', 'CASSParse_City': 'Springfield',
                          'CASSParse_State': 'IL', 'CASSParse_Zip': '62701', 'CASSParse_Plus4': '1234'})
        self.assertEqual((rows[1]['CASSParse_PrivateMailboxNumber'], rows[2]['CASSParse_Range']), ('77', None))
        self.assertIsNone(rows[0]['CASSParse_CA_LockBox'], 'the US model leaves the Canada fields empty')
        _, no_last = self.run_tool('core.parse_address', {'address_field': 'a'}, [('a', 'v_wstring')], [['1 Elm St']])
        self.assertNotIn('CASSParse_City', no_last[0])

    def test_census_batch_geocoder_and_reverse_geographies(self):
        fields = [('street', 'v_wstring'), ('city', 'v_wstring'), ('state', 'v_wstring'), ('zip', 'v_wstring')]
        rows = [['1600 Pennsylvania Ave NW', 'Washington', 'DC', '20500'], ['1 Nowhere Rd', 'X', 'ZZ', '00000'], [None, None, None, None]]
        cfg = {'provider': 'us-census-geocoder', 'connection_id': self.census, 'address_field': 'street', 'city_field': 'city',
               'state_field': 'state', 'zip_field': 'zip'}
        r, out = self.run_tool('core.street_geocoder', cfg, fields, rows)
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual((out[0]['Latitude'], out[0]['Longitude'], out[0]['MatchStatus'], out[0]['MatchType']), (38.898754, -77.03535, 'Match', 'Exact'))
        self.assertEqual([o['MatchStatus'] for o in out[1:]], ['No_Match', 'No_Address'])
        self.assertEqual(len([c for c in H.calls if c[1] == '/geocoder/locations/addressbatch']), 1, 'one batch request')
        r, rev = self.run_tool('core.reverse_geocoder', {'provider': 'us-census-geocoder', 'connection_id': self.census,
                                                          'latitude_field': 'lat', 'longitude_field': 'lon'},
                               [('lat', 'double'), ('lon', 'double')], [['38.8987', '-77.0353']])
        self.assertEqual((rev[0]['StateFIPS'], rev[0]['Tract'], rev[0]['Block'], rev[0]['ReverseAddress']), ('11', '006202', '1031', None))

    def test_nominatim_identifies_itself_caches_and_paces_the_public_server(self):
        cfg = {'provider': 'nominatim', 'connection_id': self.osm, 'address_field': 'q', 'country_codes': 'gb'}
        rows = [['Parliament Square, London'], ['Parliament Square, London'], ['Nowhere Lane']]
        with mock.patch.object(address, 'PUBLIC_NOMINATIM', '127.0.0.1'):
            r, out = self.run_tool('core.us_geocoder', cfg, [('q', 'v_wstring')], rows)
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual([o['MatchStatus'] for o in out], ['Match', 'Match', 'No_Match'])
        searches = [c for c in H.calls if c[1] == '/nominatim/search']
        self.assertEqual(len(searches), 2, 'the repeated address was answered from the cache')
        self.assertGreaterEqual(searches[1][4] - searches[0][4], 0.95, 'at most one request per second on the public server')
        self.assertEqual(searches[0][2]['countrycodes'], 'gb')
        r, rev = self.run_tool('core.reverse_geocoder', {'provider': 'nominatim', 'connection_id': self.osm, 'latitude_field': 'lat',
                                                          'longitude_field': 'lon'}, [('lat', 'double'), ('lon', 'double')], [['51.5034', '-0.1276']])
        self.assertEqual((rev[0]['ReverseAddress'], rev[0]['ReversePostcode'], rev[0]['ReverseCountry']), ('10 Downing St, London', 'SW1A 2AA', 'GB'))

    def test_cass_standardises_in_batches_and_flags_unfound(self):
        rows = [[f'{i} Main St', 'Springfield', 'IL'] for i in range(150)] + [['1 Bogus Rd', 'X', 'IL']]
        r, out = self.run_tool('core.cass', {'provider': 'smarty', 'connection_id': self.smarty, 'address_field': 'a', 'city_field': 'c',
                                             'state_field': 's'}, [('a', 'v_wstring'), ('c', 'v_wstring'), ('s', 'v_wstring')], rows)
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual((out[0]['CASS_DeliveryLine1'], out[0]['CASS_Plus4'], out[0]['CASS_DPVMatchCode'], out[0]['CASS_Status']),
                         ('0 MAIN ST', '1234', 'Y', 'Valid'))
        self.assertEqual(out[-1]['CASS_Status'], 'Not found')
        self.assertEqual(len([c for c in H.calls if c[1] == '/street-address']), 2, '100 + 51')

    def test_zip9_coder_uses_the_registered_pack_and_falls_back_to_zip5(self):
        r, _ = self.run_tool('core.us_zip9_coder', {'provider': 'zip4-data-pack', 'zip_field': 'z'}, [('z', 'v_wstring')], [['62701']])
        self.assertIn('data pack', r.error['message'])
        pack = os.path.join(self._home.name, 'zip4.csv')
        with open(pack, 'w', newline='') as fh:
            fh.write('Zip5,Plus4,Latitude,Longitude\n62701,1234,39.80,-89.64\n62701,,39.79,-89.65\n')
        providers.DataPackStore().register('zip4-centroids', pack, 'Licensed (test fixture)')
        r, out = self.run_tool('core.us_zip9_coder', {'provider': 'zip4-data-pack', 'zip_field': 'z'}, [('z', 'v_wstring')],
                               [['62701-1234'], ['627019999'], ['99999'], [None]])
        self.assertEqual(r.status, 'succeeded', r.error)
        self.assertEqual([(o['ZIP9_Latitude'], o['ZIP9_MatchLevel']) for o in out], [(39.8, 'ZIP9'), (39.79, 'ZIP5'), (None, 'None'), (None, 'None')])


@unittest.skipUnless(os.environ.get('FLOWBENCH_LIVE_CENSUS'), 'set FLOWBENCH_LIVE_CENSUS=1 to call the real Census Geocoder')
class LiveCensusTests(unittest.TestCase):
    def test_the_public_census_geocoder_finds_the_white_house(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'FLOWENGINE_HOME': home}):
            conn = ServiceConnectionStore().save({'name': 'Census', 'kind': 'web', 'base_url': 'https://geocoding.geo.census.gov/geocoder',
                                                  'auth': 'none'}).id
            doc = workflow([text_input('t', [('s', 'v_wstring'), ('c', 'v_wstring'), ('st', 'v_wstring'), ('z', 'v_wstring')],
                                       [['1600 Pennsylvania Ave NW', 'Washington', 'DC', '20500']]),
                            {'id': 'x', 'tool': 'core.street_geocoder', 'config': {'provider': 'us-census-geocoder', 'connection_id': conn,
                                                                                   'address_field': 's', 'city_field': 'c',
                                                                                   'state_field': 'st', 'zip_field': 'z'}}],
                           [edge('t', 'output', 'x', 'input')])
            r = run_workflow(doc, base_dir=ROOT, limits=NET)
            self.assertEqual(r.status, 'succeeded', r.error)
            row = dict(zip(r.outputs['x']['output'].schema.names, next(r.outputs['x']['output'].rows())))
            self.assertEqual(row['MatchStatus'], 'Match')
            self.assertAlmostEqual(row['Latitude'], 38.8987, delta=0.01)
            self.assertAlmostEqual(row['Longitude'], -77.0353, delta=0.01)


if __name__ == '__main__':
    unittest.main()
