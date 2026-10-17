"""Saved web-service credentials (Web/API, GitHub and SMTP email), the non-database part of the connection manager.

Non-secret settings are stored as JSON at ``<FLOWENGINE_HOME or ~/.flowbench>/service_connections.json``.
The token or password lives in the same OS credential vault as database passwords, under the
connection id. Workflows reference a connection by ``connection_id`` only.

A credential is scoped to its ``base_url``: :meth:`ServiceConnection.authorize` returns auth headers
only for URLs with the same scheme, host and port whose path is under the base path. Tools must
call it for every request, including each redirect target, so a workflow cannot send a saved
token to another server.

An SMTP connection's ``base_url`` is ``smtps://host:port`` (TLS from the start) or ``smtp://host:port``
(STARTTLS, required except on localhost). It always uses a username and password, which the Email
tool only sends over an encrypted connection (or to localhost).
"""
from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import ConfigError
from .runtimes import home

KINDS = ('web', 'github', 'smtp')
AUTH_MODES = ('bearer', 'basic', 'header')
_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_LOOPBACK = ('localhost', '127.0.0.1', '::1')


def _origin_and_path(url: str):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or {'http': 80, 'https': 443}.get(scheme)
    return (scheme, (parts.hostname or '').lower(), port), parts.path or '/'


@dataclass
class ServiceConnection:
    id: str
    name: str
    kind: str
    base_url: str
    auth: str = 'bearer'
    username: str | None = None      # basic auth
    header_name: str | None = None   # custom header auth, e.g. X-Api-Key

    def validate(self):
        if not re.fullmatch(r'svc_[A-Za-z0-9_.-]{1,60}', self.id):
            raise ConfigError('Invalid service connection id')
        if not self.name.strip():
            raise ConfigError('Service connection name is required')
        if self.kind not in KINDS:
            raise ConfigError(f'Service connection kind must be one of {KINDS}')
        if self.kind == 'smtp':
            return self._validate_smtp()
        if self.auth not in AUTH_MODES or (self.kind == 'github' and self.auth != 'bearer'):
            raise ConfigError('GitHub connections use a token (bearer)' if self.kind == 'github'
                              else f'Authentication must be one of {AUTH_MODES}')
        parts = urlsplit(self.base_url)
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.query or parts.fragment \
                or parts.username or parts.password:
            raise ConfigError('Base URL must be an absolute https:// URL without credentials, query or fragment')
        if parts.scheme == 'http' and parts.hostname not in _LOOPBACK:
            raise ConfigError('Plain http:// is allowed only for localhost; use https:// so the credential is encrypted')
        if self.auth == 'basic' and not (self.username or '').strip():
            raise ConfigError('Basic authentication needs a username')
        if self.auth == 'header' and not _HEADER.match(self.header_name or ''):
            raise ConfigError('Header authentication needs a valid header name')
        return self

    def _validate_smtp(self):
        parts = urlsplit(self.base_url)
        try:
            port = parts.port
        except ValueError:
            port = None
        if parts.scheme not in ('smtps', 'smtp') or not parts.hostname or parts.path not in ('', '/') or parts.query \
                or parts.fragment or parts.username or parts.password or port is None:
            raise ConfigError('SMTP server must be smtps://host:port (TLS) or smtp://host:port (STARTTLS), '
                              'for example smtps://smtp.example.com:465')
        if self.auth != 'basic' or not (self.username or '').strip():
            raise ConfigError('SMTP connections need a username (and the password as the secret)')
        return self

    def smtp_server(self):
        """(host, port, encryption) with encryption 'ssl' or 'starttls'."""
        parts = urlsplit(self.base_url)
        return parts.hostname, parts.port, 'ssl' if parts.scheme == 'smtps' else 'starttls'

    def public(self) -> dict:
        return asdict(self)

    def covers(self, url: str) -> bool:
        base_origin, base_path = _origin_and_path(self.base_url)
        origin, path = _origin_and_path(url)
        if any(segment in ('.', '..') for segment in unquote(path).replace('\\', '/').split('/')):
            return False  # never let dot segments climb out of the base path
        prefix = base_path if base_path.endswith('/') else base_path + '/'
        return origin == base_origin and (path == base_path or path.startswith(prefix) or base_path == '/')

    def authorize(self, url: str, secret: str) -> dict:
        """Auth headers for ``url``; raises ConfigError when the URL is outside the base URL."""
        if not self.covers(url):
            raise ConfigError(f'URL is outside the base URL of connection {self.name!r}')
        if self.auth == 'basic':
            token = base64.b64encode(f'{self.username}:{secret}'.encode('utf-8')).decode('ascii')
            return {'Authorization': f'Basic {token}'}
        if self.auth == 'header':
            return {self.header_name: secret}
        return {'Authorization': f'Bearer {secret}'}


class ServiceConnectionStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else home() / 'service_connections.json'
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        return {c['id']: c for c in data.get('connections', [])}

    def _save(self, items: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'format': 'flowbench.service-connections', 'version': 1,
                                   'connections': sorted(items.values(), key=lambda c: c['name'].lower())},
                                  indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def list(self, kind: str | None = None) -> list[ServiceConnection]:
        with self._lock:
            items = [ServiceConnection(**c) for c in self._load().values()]
        return [c for c in items if kind is None or c.kind == kind]

    def get(self, conn_id: str, kind: str | None = None) -> ServiceConnection:
        with self._lock:
            data = self._load().get(conn_id)
        if not data or (kind and data['kind'] != kind):
            raise ConfigError(f'Unknown {kind or "service"} connection {conn_id!r}; create it under Connections')
        return ServiceConnection(**data)

    def save(self, data: dict, secret: str | None = None) -> ServiceConnection:
        data = {k: v for k, v in dict(data).items() if v is not None or k in ('username', 'header_name')}
        unknown = set(data) - set(ServiceConnection.__dataclass_fields__)
        if unknown:
            raise ConfigError(f'Unknown service connection field(s) {sorted(unknown)}; secrets are passed separately')
        data['id'] = data.get('id') or f'svc_{uuid.uuid4().hex[:12]}'
        if data.get('kind') == 'github':
            data.setdefault('base_url', 'https://api.github.com')
        conn = ServiceConnection(**data).validate()
        from .db.vault import vault
        with self._lock:
            items = self._load()
            if conn.id not in items and not secret:
                raise ConfigError('A new service connection needs its token or password')
            items[conn.id] = conn.public()
            if secret:
                vault().set(conn.id, conn.username or '', secret)
            self._save(items)
        return conn

    def delete(self, conn_id: str) -> None:
        from .db.vault import vault
        with self._lock:
            items = self._load()
            items.pop(conn_id, None)
            self._save(items)
        vault().remove(conn_id)

    def secret(self, conn: ServiceConnection) -> str:
        from .db.vault import vault
        value = vault().get(conn.id)
        if not value:
            raise ConfigError(f'No saved credential for connection {conn.name!r}; edit the connection to add it')
        return value
