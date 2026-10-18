"""Amazon S3 Download and Upload through the S3 REST API, signed with AWS Signature V4; no AWS SDK.

Functional source: the public Amazon S3 Download/Upload tool help and the S3 API reference. A
saved Web/API connection holds the endpoint (``https://s3.<region>.amazonaws.com``, or any
S3-compatible server such as MinIO), the access key id as username and the secret access key in
the OS vault. Requests use path-style addresses (``<endpoint>/<bucket>/<key>``) so they stay
under the connection's base URL.

Download fetches the object to a temporary file and reads it exactly as Input Data would, with
the same format options. Upload writes the records exactly as Output Data would and sends the
file in one signed PUT, or as a multipart upload above 64 MB. Both need the network permission;
Upload also needs remote writes.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from ..aws_sigv4 import EMPTY_SHA256, sign
from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from .download import _HttpOnlyRedirects
from .github import _permit

_BUCKET = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
_REGION = re.compile(r'^[a-z]{2}(-[a-z]+)+-\d$|^[a-z0-9-]{1,40}$')
PART_BYTES = 64 * 1024 * 1024
CHUNK = 1024 * 1024


def _check(cfg):
    if not _BUCKET.match(cfg['bucket']) or '..' in cfg['bucket']:
        raise ConfigError(f"{cfg['bucket']!r} is not a valid S3 bucket name")
    key = cfg['key']
    if not key or key.startswith('/') or len(key.encode()) > 1024 or any(seg in ('.', '..') for seg in key.split('/')):
        raise ConfigError('Object key must be 1-1024 bytes, not start with "/" and not contain . or .. segments')
    if not _REGION.match(cfg['region']):
        raise ConfigError(f"{cfg['region']!r} is not a valid region name")


class _S3:
    def __init__(self, cfg, ctx):
        from ..service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        self.conn = store.get(cfg['connection_id'], kind='web')
        if self.conn.auth != 'aws-sigv4':
            raise ConfigError('An S3 connection uses AWS Signature V4: the access key id as username and the secret '
                              'access key as the secret')
        self.secret = store.secret(self.conn)
        self.region, self.timeout, self.ctx = cfg['region'], cfg['timeout_seconds'], ctx
        self.object_url = (f"{self.conn.base_url.rstrip('/')}/{cfg['bucket']}/"
                           f"{urllib.parse.quote(cfg['key'], safe='/-_.~')}")
        self.opener = urllib.request.build_opener(_HttpOnlyRedirects(self.conn))

    def request(self, method, url, body=b'', headers=None, payload_sha=None):
        if not self.conn.covers(url):
            raise ConfigError(f'{url} is outside the connection base URL')
        sha = payload_sha or (hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256)
        signed = sign(method, url, dict(headers or {}), sha, self.conn.username, self.secret, self.region)
        req = urllib.request.Request(url, data=body if method in ('PUT', 'POST') else None, method=method, headers=signed)
        try:
            return self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read(64 * 1024)
            raise DataError(f'S3 {method} returned {e.code}: {_error_text(raw)}') from None
        except (urllib.error.URLError, OSError) as e:
            raise DataError(f'Could not reach the S3 endpoint: {getattr(e, "reason", None) or e}') from None


def _error_text(raw: bytes) -> str:
    try:
        root = ET.fromstring(raw)
        return f"{root.findtext('Code') or ''}: {root.findtext('Message') or ''}".strip(': ')
    except ET.ParseError:
        return raw[:300].decode('utf-8', 'replace')


def _xml_text(raw: bytes, tag: str) -> str | None:
    root = ET.fromstring(raw)
    for el in root.iter():
        if el.tag.rsplit('}', 1)[-1] == tag:
            return el.text
    return None


_COMMON = {
    'provider': {'type': 'string', 'enum': ['amazon-s3']},
    'provider_semantics': {'type': 'string'},
    'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                      'description': 'Saved Web/API connection to the S3 endpoint (AWS Signature V4)'},
    'region': {'type': 'string', 'minLength': 1, 'default': 'us-east-1'},
    'bucket': {'type': 'string', 'minLength': 3},
    'key': {'type': 'string', 'minLength': 1, 'description': 'Object key, for example exports/2026/sales.csv'},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600, 'default': 300},
}


class S3Download(Tool):
    manifest = ToolManifest(
        tool_id='core.s3_download', version='1.0.0', display_name='Amazon S3 Download',
        catalog_ids=('connectors:amazon-s3-download',),
        inputs=(), outputs=(Anchor('output', 'O', description='Records read from the object'),),
        side_effects='network', deterministic=False,
        description='Read an object from Amazon S3 (or an S3-compatible store) in any format Input Data reads: CSV and '
                    'other delimited text, JSON/JSON Lines, Parquet, Arrow, .yxdb, Excel or OpenDocument. read holds the '
                    'Input Data settings (format, delimiter, sheet, ...).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'bucket', 'key'], 'properties': {
            **_COMMON, 'read': {'type': 'object', 'default': {}, 'description': 'Input Data settings except path'}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def effects(self, cfg):
        return {'network_requests'}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        from .inout import InputData
        providers.resolve(cfg, 'object-storage')
        _check(cfg)
        if 'path' in cfg['read']:
            raise ConfigError('read holds format settings; the object is named by bucket and key')
        _permit(ctx)
        s3 = _S3(cfg, ctx)
        reader = InputData()
        suffix = Path(cfg['key']).suffix
        with tempfile.TemporaryDirectory(prefix='flowbench-s3-') as d:
            local = Path(d) / f'object{suffix}'
            with s3.request('GET', s3.object_url) as r, open(local, 'wb') as fh:
                total = 0
                while chunk := r.read(CHUNK):
                    ctx.check_cancelled()
                    fh.write(chunk)
                    total += len(chunk)
            ctx.info(f"Downloaded s3://{cfg['bucket']}/{cfg['key']} ({total:,} bytes)")
            read_cfg = reader.normalize_config({**cfg['read'], 'path': str(local)})
            out = reader.run(ctx, read_cfg, {})
            # materialise before the temporary file goes away
            return {'output': out['output']}


class S3Upload(Tool):
    manifest = ToolManifest(
        tool_id='core.s3_upload', version='1.0.0', display_name='Amazon S3 Upload',
        catalog_ids=('connectors:amazon-s3-upload',),
        inputs=(Anchor('input', 'I', description='Records to upload'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Write records to an Amazon S3 (or S3-compatible) object in any format Output Data writes: delimited '
                    'text, JSON/JSON Lines, Parquet or Arrow. write holds the Output Data settings. Optional server-side '
                    'encryption (AES256 or aws:kms).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'bucket', 'key'], 'properties': {
            **_COMMON, 'write': {'type': 'object', 'default': {}, 'description': 'Output Data settings except path'},
            'server_side_encryption': {'enum': ['none', 'AES256', 'aws:kms'], 'default': 'none'},
            'kms_key_id': {'type': ['string', 'null'], 'default': None},
            'storage_class': {'enum': ['STANDARD', 'STANDARD_IA', 'ONEZONE_IA', 'INTELLIGENT_TIERING', 'GLACIER_IR'],
                              'default': 'STANDARD'}}})

    def infer(self, ctx, cfg, inputs):
        return {}

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        from .inout import OutputData
        providers.resolve(cfg, 'object-storage')
        _check(cfg)
        if 'path' in cfg['write']:
            raise ConfigError('write holds format settings; the object is named by bucket and key')
        if cfg['kms_key_id'] and cfg['server_side_encryption'] != 'aws:kms':
            raise ConfigError('kms_key_id needs server_side_encryption aws:kms')
        _permit(ctx, writes=True)
        s3 = _S3(cfg, ctx)
        headers = {'x-amz-storage-class': cfg['storage_class']}
        if cfg['server_side_encryption'] != 'none':
            headers['x-amz-server-side-encryption'] = cfg['server_side_encryption']
        if cfg['kms_key_id']:
            headers['x-amz-server-side-encryption-aws-kms-key-id'] = cfg['kms_key_id']
        writer = OutputData()
        with tempfile.TemporaryDirectory(prefix='flowbench-s3-') as d:
            local = Path(d) / f'upload{Path(cfg["key"]).suffix}'
            writer.run(ctx, writer.normalize_config({**cfg['write'], 'path': str(local)}), inputs)
            size = local.stat().st_size
            if size <= PART_BYTES:
                body = local.read_bytes()
                s3.request('PUT', s3.object_url, body, headers).close()
            else:
                self._multipart(ctx, s3, local, headers)
        ctx.info(f"Uploaded {len(inputs['input']):,} records to s3://{cfg['bucket']}/{cfg['key']} ({size:,} bytes)")
        return {}

    def _multipart(self, ctx, s3, local, headers):
        with s3.request('POST', s3.object_url + '?uploads', b'', headers) as r:
            upload_id = _xml_text(r.read(), 'UploadId')
        if not upload_id:
            raise DataError('S3 did not start the multipart upload')
        q = urllib.parse.quote(upload_id, safe='')
        etags = []
        try:
            with open(local, 'rb') as fh:
                number = 1
                while part := fh.read(PART_BYTES):
                    ctx.check_cancelled()
                    with s3.request('PUT', f'{s3.object_url}?partNumber={number}&uploadId={q}', part) as r:
                        etags.append((number, r.headers.get('ETag')))
                    number += 1
            body = ('<CompleteMultipartUpload>' + ''.join(
                f'<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>' for n, e in etags) +
                '</CompleteMultipartUpload>').encode()
            with s3.request('POST', f'{s3.object_url}?uploadId={q}', body) as r:
                raw = r.read()
            if b'<Error>' in raw:  # S3 can report a failed completion inside a 200 response
                raise DataError(f'S3 multipart completion failed: {_error_text(raw)}')
        except BaseException:
            try:
                s3.request('DELETE', f'{s3.object_url}?uploadId={q}').close()
            except DataError:
                pass
            raise


TOOLS = [S3Download, S3Upload]
