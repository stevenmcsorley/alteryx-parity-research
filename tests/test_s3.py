"""Amazon S3 Download/Upload: the signer against the AWS documentation example, and live round trips against an
S3-compatible server that validates signatures (SeaweedFS in the service-integration CI job).

Live tests need FLOWBENCH_S3_ENDPOINT, FLOWBENCH_S3_KEY and FLOWBENCH_S3_SECRET.
"""
import datetime as dt
import hashlib
import os
import tempfile
import unittest
import urllib.parse
import urllib.request
import uuid
from unittest import mock

import flowengine.db.vault as vault_module
from flowengine.aws_sigv4 import EMPTY_SHA256, sign
from flowengine.errors import ConfigError
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

LIVE = all(os.environ.get(k) for k in ('FLOWBENCH_S3_ENDPOINT', 'FLOWBENCH_S3_KEY', 'FLOWBENCH_S3_SECRET'))


class SigningTests(unittest.TestCase):
    def test_signature_matches_the_aws_documentation_example(self):
        # "GET Object" example in the S3 API reference, Signature Calculations for the Authorization Header
        h = sign('GET', 'https://examplebucket.s3.amazonaws.com/test.txt', {'Range': 'bytes=0-9'}, EMPTY_SHA256,
                 'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 'us-east-1',
                 now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc))
        self.assertEqual(h['Authorization'], 'AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, '
                         'SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, '
                         'Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41')

    def test_sigv4_connections_never_hand_out_plain_auth_headers(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'FLOWENGINE_HOME': home}), \
                mock.patch.object(vault_module, '_VAULT', MemoryVault()):
            conn = ServiceConnectionStore().save({'name': 'S3', 'kind': 'web', 'base_url': 'https://s3.eu-west-2.amazonaws.com',
                                                  'auth': 'aws-sigv4', 'username': 'AKIA'}, secret='s')
            with self.assertRaises(ConfigError):
                conn.authorize('https://s3.eu-west-2.amazonaws.com/b/k', 's')
            with self.assertRaises(ConfigError):
                ServiceConnectionStore().save({'name': 'S3', 'kind': 'web', 'base_url': 'https://s3.amazonaws.com',
                                               'auth': 'aws-sigv4'}, secret='s')


@unittest.skipUnless(LIVE, 'no S3-compatible server configured (runs in the service-integration CI job)')
class LiveS3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.endpoint = os.environ['FLOWBENCH_S3_ENDPOINT'].rstrip('/')
        cls.key, cls.secret = os.environ['FLOWBENCH_S3_KEY'], os.environ['FLOWBENCH_S3_SECRET']
        cls.bucket = f'fb-{uuid.uuid4().hex[:12]}'
        url = f'{cls.endpoint}/{cls.bucket}'
        req = urllib.request.Request(url, method='PUT', headers=sign('PUT', url, {}, EMPTY_SHA256, cls.key, cls.secret, 'us-east-1'))
        urllib.request.urlopen(req, timeout=30).close()

    @classmethod
    def tearDownClass(cls):
        # Remove what the tests wrote: every object (ListObjectsV2), then the bucket.
        import re
        def call(method, url):
            req = urllib.request.Request(url, method=method, headers=sign(method, url, {}, EMPTY_SHA256, cls.key, cls.secret, 'us-east-1'))
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        bucket = f'{cls.endpoint}/{cls.bucket}'
        for key in re.findall(rb'<Key>([^<]+)</Key>', call('GET', f'{bucket}?list-type=2')):
            call('DELETE', f"{bucket}/{urllib.parse.quote(key.decode(), safe='/')}")
        call('DELETE', bucket)

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        self.conn = self.connect(self.secret)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def connect(self, secret):
        return ServiceConnectionStore().save({'name': f'S3 {uuid.uuid4().hex[:4]}', 'kind': 'web', 'base_url': self.endpoint,
                                              'auth': 'aws-sigv4', 'username': self.key}, secret=secret).id

    def cfg(self, key, conn=None, **extra):
        return {'provider': 'amazon-s3', 'connection_id': conn or self.conn, 'region': 'us-east-1', 'bucket': self.bucket,
                'key': key, **extra}

    def upload(self, key, fields, rows, limits={'allow_network': True, 'allow_remote_writes': True}, **extra):
        doc = workflow([text_input('t', fields, rows), {'id': 'u', 'tool': 'core.s3_upload', 'config': self.cfg(key, **extra)}],
                       [edge('t', 'output', 'u', 'input')])
        return run_workflow(doc, base_dir=ROOT, limits=limits)

    def download(self, key, conn=None, **extra):
        doc = workflow([{'id': 'd', 'tool': 'core.s3_download', 'config': self.cfg(key, conn, **extra)}], [])
        return run_workflow(doc, base_dir=ROOT, limits={'allow_network': True})

    def test_round_trip_in_several_formats(self):
        fields = [('id', 'int32'), ('city', 'v_wstring'), ('amount', 'double')]
        rows = [['1', 'Zürich', '10.5'], ['2', 'Lagos', '7.25'], ['3', 'São Paulo', None]]
        # CSV carries no types or nulls: declare them, as with a local file in Input Data
        declared = {'fields': [{'name': n, 'type': t} for n, t in fields], 'empty_as_null': True}
        for key, read in (('dir/sales.csv', declared), ('dir/sales.parquet', {}), ('dir/sales.json', {})):
            r = self.upload(key, fields, rows)
            self.assertEqual(r.status, 'succeeded', (key, r.error))
            got = self.download(key, read=read)
            self.assertEqual(got.status, 'succeeded', (key, got.error))
            out = got.outputs['d']['output']
            self.assertEqual([tuple(x) for x in out.rows()], [(1, 'Zürich', 10.5), (2, 'Lagos', 7.25), (3, 'São Paulo', None)], key)

    def test_a_wrong_secret_is_rejected_by_the_server(self):
        r = self.download('dir/sales.csv', conn=self.connect('not-the-secret'))
        self.assertEqual(r.status, 'failed')
        self.assertIn('S3 GET returned 403', r.error['message'])

    def test_upload_needs_remote_writes_and_missing_objects_fail_clearly(self):
        r = self.upload('x.csv', [('a', 'int32')], [['1']], limits={'allow_network': True})
        self.assertEqual(r.error['code'], 'permission-denied')
        self.assertIn('NoSuchKey', self.download('does/not/exist.csv').error['message'])

    def test_multipart_upload_above_the_part_size(self):
        import flowengine.tools.s3 as s3
        rows = [[str(i), 'x' * 40] for i in range(3000)]
        with mock.patch.object(s3, 'PART_BYTES', 5 * 1024 * 1024):  # S3's minimum part size
            r = self.upload('big/data.csv', [('i', 'int32'), ('pad', 'v_wstring')], rows * 50)
        self.assertEqual(r.status, 'succeeded', r.error)
        out = self.download('big/data.csv', read={'fields': [{'name': 'i', 'type': 'int32'}, {'name': 'pad', 'type': 'v_wstring'}]}).outputs['d']['output']
        self.assertEqual(len(out), 150000)
        self.assertEqual(hashlib.sha256(str(list(out.rows())[-1]).encode()).hexdigest(),
                         hashlib.sha256(str((2999, 'x' * 40)).encode()).hexdigest())


if __name__ == '__main__':
    unittest.main()
