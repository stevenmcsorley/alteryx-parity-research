"""OAuth 2.0 for saved connections: access tokens from a token endpoint, never from the workflow.

A connection with ``auth: oauth2`` names its ``token_url``, ``client_id`` and ``scope``. Its vault
secret is JSON holding what the grant needs: ``client_secret`` (a confidential app), and/or the
``refresh_token`` obtained when the user signed in with the device authorization grant (RFC 8628,
``device_code_url``). With a refresh token the refresh grant is used (RFC 6749 section 6); with only
a client secret, the client credentials grant (section 4.4). A refresh token the server rotates is
saved back to the vault. Access tokens are kept in memory only, until a minute before they expire.

Tokens are sent only to the connection's own token and device URLs; redirects are refused.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import ConfigError, DataError

_CACHE: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post(url: str, form: dict, timeout: float = 30) -> tuple[int, dict]:
    body = urllib.parse.urlencode({k: v for k, v in form.items() if v}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        with e:
            try:
                return e.code, json.loads(e.read() or b'{}')
            except ValueError:
                return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DataError(f'Could not reach the sign-in server: {getattr(e, "reason", None) or e}') from None


def _secrets(raw: str) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise ConfigError('The saved OAuth credential is not valid; edit the connection and save it again')
    return value


class OAuthSession:
    def __init__(self, conn, raw_secret: str, store=None):
        if conn.auth != 'oauth2':
            raise ConfigError(f'Connection {conn.name!r} does not use OAuth')
        self.conn, self.secrets, self.store = conn, _secrets(raw_secret), store

    @classmethod
    def for_connection(cls, conn, raw_secret=None):
        from .service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        return cls(conn, store.secret(conn) if raw_secret is None else raw_secret, store)

    def access_token(self, refresh=False) -> str:
        with _LOCK:
            cached = _CACHE.get(self.conn.id)
            if cached and not refresh and cached[1] > time.time() + 60:
                return cached[0]
        rt, cs = self.secrets.get('refresh_token'), self.secrets.get('client_secret')
        if rt:
            form = {'grant_type': 'refresh_token', 'refresh_token': rt, 'client_id': self.conn.client_id,
                    'client_secret': cs, 'scope': self.conn.scope}
        elif cs:
            form = {'grant_type': 'client_credentials', 'client_id': self.conn.client_id, 'client_secret': cs,
                    'scope': self.conn.scope}
        else:
            raise ConfigError(f'Connection {self.conn.name!r} has no sign-in yet: sign in (device code) or save a client secret')
        status, doc = _post(self.conn.token_url, form)
        if status != 200 or not doc.get('access_token'):
            reason = doc.get('error_description') or doc.get('error') or f'HTTP {status}'
            raise DataError(f'Sign-in for {self.conn.name!r} failed: {str(reason).splitlines()[0][:300]}. '
                            'Sign in again from the connection if the refresh token expired.')
        if doc.get('refresh_token') and doc['refresh_token'] != rt and rt:
            self._save({**self.secrets, 'refresh_token': doc['refresh_token']})
        expires = time.time() + float(doc.get('expires_in') or 3600)
        with _LOCK:
            _CACHE[self.conn.id] = (doc['access_token'], expires)
        return doc['access_token']

    def forget(self):
        with _LOCK:
            _CACHE.pop(self.conn.id, None)

    def _save(self, secrets: dict):
        self.secrets = secrets
        if self.store is not None:
            self.store.save(self.conn.public(), secret=json.dumps(secrets))

    # ---------------------------------------------------------------- device authorization grant (RFC 8628)

    def device_start(self) -> dict:
        if not self.conn.device_code_url:
            raise ConfigError(f'Connection {self.conn.name!r} has no device sign-in URL')
        status, doc = _post(self.conn.device_code_url, {'client_id': self.conn.client_id, 'scope': self.conn.scope})
        if status != 200 or not doc.get('device_code'):
            raise DataError(f"Device sign-in could not start: {doc.get('error_description') or doc.get('error') or status}")
        return {'device_code': doc['device_code'], 'user_code': doc.get('user_code'),
                'verification_uri': doc.get('verification_uri') or doc.get('verification_url'),
                'expires_in': int(doc.get('expires_in') or 900), 'interval': int(doc.get('interval') or 5),
                'message': doc.get('message')}

    def device_poll(self, device_code: str) -> dict:
        """One poll: {'status': 'pending'|'slow_down'|'signed_in'|'declined'|'expired'}. Signed in saves the refresh token."""
        status, doc = _post(self.conn.token_url, {'grant_type': DEVICE_GRANT, 'client_id': self.conn.client_id,
                                                  'device_code': device_code,
                                                  'client_secret': self.secrets.get('client_secret')})
        if status == 200 and doc.get('access_token'):
            if not doc.get('refresh_token'):
                raise DataError('Signed in, but no refresh token was issued; add offline_access to the scope')
            self._save({**self.secrets, 'refresh_token': doc['refresh_token']})
            with _LOCK:
                _CACHE[self.conn.id] = (doc['access_token'], time.time() + float(doc.get('expires_in') or 3600))
            return {'status': 'signed_in'}
        error = doc.get('error', '')
        mapped = {'authorization_pending': 'pending', 'slow_down': 'slow_down', 'expired_token': 'expired',
                  'authorization_declined': 'declined', 'access_denied': 'declined'}.get(error)
        if mapped:
            return {'status': mapped}
        raise DataError(f"Device sign-in failed: {doc.get('error_description') or error or status}")
