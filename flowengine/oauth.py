"""OAuth 2.0 for saved connections: access tokens from a token endpoint, never from the workflow.

A connection with ``auth: oauth2`` names its ``token_url``, ``client_id`` and ``scope``. Its vault
secret is JSON holding what the grant needs:

* ``service_account`` - a service account key (Google's JSON key file): a JWT signed with its private
  key is exchanged for an access token (JWT bearer grant, RFC 7523);
* ``refresh_token`` - from signing in as a user, either with the device authorization grant
  (RFC 8628, ``device_code_url``) or in the browser with the authorization code grant and PKCE on a
  loopback redirect (RFC 6749 section 4.1, RFC 7636, RFC 8252; ``authorize_url``); used with the
  refresh grant (RFC 6749 section 6);
* ``client_secret`` - a confidential app; alone it gives the client credentials grant (section 4.4).

A refresh token the server rotates is saved back to the vault. Access tokens are kept in memory
only, until a minute before they expire.

Tokens are sent only to the connection's own token and device URLs; redirects are refused.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as _secrets_mod
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import ConfigError, DataError

_CACHE: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
JWT_GRANT = 'urn:ietf:params:oauth:grant-type:jwt-bearer'
BROWSER_TIMEOUT = 600
_BROWSER: dict[str, dict] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _service_account_jwt(key, conn) -> str:
    """RS256 JWT for a service account key ({client_email, private_key, ...}), audience the token URL."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    if not isinstance(key, dict) or not key.get('client_email') or not key.get('private_key'):
        raise ConfigError('The service account key must be the JSON key file (client_email and private_key)')
    now = int(time.time())
    header = {'alg': 'RS256', 'typ': 'JWT', **({'kid': key['private_key_id']} if key.get('private_key_id') else {})}
    claims = {'iss': key['client_email'], 'scope': conn.scope or '', 'aud': conn.token_url, 'iat': now, 'exp': now + 3600}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    try:
        private = serialization.load_pem_private_key(key['private_key'].encode(), password=None)
    except (ValueError, TypeError) as e:
        raise ConfigError(f'The service account private key cannot be read: {e}') from None
    signature = private.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f'{signing_input}.{_b64url(signature)}'


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
        rt, cs, sa = self.secrets.get('refresh_token'), self.secrets.get('client_secret'), self.secrets.get('service_account')
        if sa:
            form = {'grant_type': JWT_GRANT, 'assertion': _service_account_jwt(sa, self.conn)}
        elif rt:
            form = {'grant_type': 'refresh_token', 'refresh_token': rt, 'client_id': self.conn.client_id,
                    'client_secret': cs, 'scope': self.conn.scope}
        elif cs:
            form = {'grant_type': 'client_credentials', 'client_id': self.conn.client_id, 'client_secret': cs,
                    'scope': self.conn.scope}
        else:
            raise ConfigError(f'Connection {self.conn.name!r} has no sign-in yet: sign in, or save a client secret or '
                              'service account key')
        if not sa and not (self.conn.client_id or '').strip():
            raise ConfigError(f'Connection {self.conn.name!r} needs its client (application) id')
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

    # ---------------------------------------------------------------- browser sign-in (code + PKCE, loopback)

    def browser_start(self) -> dict:
        """Start a loopback listener and return the URL to open. The code is exchanged when the browser comes back."""
        import http.server
        if not self.conn.authorize_url:
            raise ConfigError(f'Connection {self.conn.name!r} has no browser sign-in (authorization) URL')
        if not (self.conn.client_id or '').strip():
            raise ConfigError(f'Connection {self.conn.name!r} needs its client (application) id')
        verifier = _b64url(_secrets_mod.token_bytes(48))
        state = _b64url(_secrets_mod.token_bytes(24))
        session_id = _b64url(_secrets_mod.token_bytes(12))
        entry = {'status': 'pending', 'error': None, 'started': time.time()}
        owner = self

        class Callback(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != '/callback':
                    self.send_response(404)
                    self.end_headers()
                    return
                q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
                if q.get('state') != state:
                    message, entry['status'], entry['error'] = 'This sign-in link is not the one Flowbench started.', 'failed', 'state mismatch'
                elif 'error' in q:
                    message, entry['status'], entry['error'] = 'Sign-in was cancelled or refused.', 'declined', q['error']
                else:
                    try:
                        owner._exchange_code(q.get('code', ''), verifier, redirect_uri)
                        message, entry['status'] = 'Signed in to Flowbench. You can close this tab.', 'signed_in'
                    except Exception as e:  # report the failure to the waiting app
                        message, entry['status'], entry['error'] = 'Sign-in failed; see Flowbench for details.', 'failed', str(e)
                page = f'<!doctype html><meta charset="utf-8"><title>Flowbench</title><p>{message}</p>'.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                threading.Thread(target=server.shutdown, daemon=True).start()

        server = http.server.HTTPServer(('127.0.0.1', 0), Callback)
        server.timeout = 1
        redirect_uri = f'http://127.0.0.1:{server.server_address[1]}/callback'

        def serve():
            deadline = time.time() + BROWSER_TIMEOUT
            while entry['status'] == 'pending' and time.time() < deadline:
                server.handle_request()
            if entry['status'] == 'pending':
                entry['status'] = 'expired'
            server.server_close()
        threading.Thread(target=serve, daemon=True).start()
        params = {'response_type': 'code', 'client_id': self.conn.client_id, 'redirect_uri': redirect_uri,
                  'scope': self.conn.scope or '', 'state': state, 'code_challenge_method': 'S256',
                  'code_challenge': _b64url(hashlib.sha256(verifier.encode()).digest())}
        host = urllib.parse.urlsplit(self.conn.authorize_url).hostname
        # Some providers issue a refresh token only when asked for offline access.
        if host == 'accounts.google.com':
            params.update(access_type='offline', prompt='consent')
        elif host == 'www.dropbox.com':
            params.update(token_access_type='offline')
        _BROWSER[session_id] = entry
        return {'session_id': session_id, 'authorize_url': f'{self.conn.authorize_url}?{urllib.parse.urlencode(params)}',
                'redirect_uri': redirect_uri, 'expires_in': BROWSER_TIMEOUT}

    def _exchange_code(self, code, verifier, redirect_uri):
        status, doc = _post(self.conn.token_url, {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri,
                                                  'client_id': self.conn.client_id, 'code_verifier': verifier,
                                                  'client_secret': self.secrets.get('client_secret')})
        if status != 200 or not doc.get('access_token'):
            raise DataError(f"Code exchange failed: {doc.get('error_description') or doc.get('error') or status}")
        if not doc.get('refresh_token'):
            raise DataError('Signed in, but no refresh token was issued; the app or scope must allow offline access')
        self._save({**self.secrets, 'refresh_token': doc['refresh_token']})
        with _LOCK:
            _CACHE[self.conn.id] = (doc['access_token'], time.time() + float(doc.get('expires_in') or 3600))

    @staticmethod
    def browser_poll(session_id: str) -> dict:
        entry = _BROWSER.get(session_id)
        if entry is None:
            return {'status': 'unknown'}
        if entry['status'] != 'pending':
            _BROWSER.pop(session_id, None)
        return {'status': entry['status'], **({'error': entry['error']} if entry['error'] else {})}
