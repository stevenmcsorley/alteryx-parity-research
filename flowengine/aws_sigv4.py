"""AWS Signature Version 4 for S3 requests (the published algorithm; no AWS SDK).

Only what the Amazon S3 tools need: header-based signing of a single request whose payload hash is
known (``UNSIGNED-PAYLOAD`` is never used, so the body is covered by the signature).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
from urllib.parse import quote, urlsplit, parse_qsl

EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def _canonical_query(query: str) -> str:
    pairs = sorted((quote(k, safe='-_.~'), quote(v, safe='-_.~')) for k, v in parse_qsl(query, keep_blank_values=True))
    return '&'.join(f'{k}={v}' for k, v in pairs)


def sign(method: str, url: str, headers: dict, payload_sha256: str, access_key: str, secret_key: str, region: str,
         service: str = 's3', now: _dt.datetime | None = None, session_token: str | None = None) -> dict:
    """Headers to send: the given ones plus host, x-amz-date, x-amz-content-sha256 and Authorization."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date, day = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    parts = urlsplit(url)
    host = parts.netloc
    out = {**headers, 'host': host, 'x-amz-date': amz_date, 'x-amz-content-sha256': payload_sha256}
    if session_token:
        out['x-amz-security-token'] = session_token
    canon = {k.lower().strip(): ' '.join(str(v).split()) for k, v in out.items()}
    signed = ';'.join(sorted(canon))
    # S3 paths are signed as sent: each segment URI-encoded once
    path = quote(parts.path or '/', safe='/-_.~%')
    request = '\n'.join([method, path, _canonical_query(parts.query),
                         ''.join(f'{k}:{canon[k]}\n' for k in sorted(canon)), signed, payload_sha256])
    scope = f'{day}/{region}/{service}/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(request.encode('utf-8')).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(('AWS4' + secret_key).encode('utf-8'), day), region), service), 'aws4_request')
    signature = hmac.new(key, to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    out['Authorization'] = f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}'
    del out['host']  # urllib sets Host itself
    return out
