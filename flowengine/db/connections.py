"""Saved database connections (non-secret configuration only).

Stored as JSON at ``<FLOWENGINE_HOME or ~/.flowbench>/connections.json``. Passwords live in the credential
vault (vault.py) under the connection id; the store refuses to persist any field named like a secret.
Workflows reference connections by ``connection_id``, so workflow files never contain credentials.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import ConfigError
from ..runtimes import home

DIALECTS = ('postgresql', 'sqlserver')       # SQL databases (Database Input/Output, In-Database tools)
DOCUMENT_STORES = ('mongodb',)               # document databases (MongoDB Input/Output)
GENERIC = ('odbc',)                          # any ODBC driver or DSN (ODBC Loader metadata)
TLS_MODES = ('disable', 'prefer', 'require', 'verify-full')
_SECRET_KEYS = re.compile(r'pass|secret|token|pwd', re.I)


def _secret_key_path(value, path=''):
    if isinstance(value, dict):
        for key, child in value.items():
            here = f'{path}.{key}' if path else str(key)
            if _SECRET_KEYS.search(str(key)) and key != 'allow_writes':
                return here
            nested = _secret_key_path(child, here)
            if nested:
                return nested
    elif isinstance(value, (list, tuple)):
        for i, child in enumerate(value):
            nested = _secret_key_path(child, f'{path}[{i}]')
            if nested:
                return nested
    return None


@dataclass
class Connection:
    id: str
    name: str
    dialect: str
    host: str
    port: int
    database: str
    user: str
    tls: str = 'prefer'
    ca_file: str | None = None
    connect_timeout: int = 10   # seconds
    query_timeout: int = 300    # seconds (0 = none)
    allow_writes: bool = False  # connections are read-only unless explicitly enabled
    options: dict = field(default_factory=dict)

    def validate(self):
        if self.dialect not in DIALECTS + DOCUMENT_STORES + GENERIC:
            raise ConfigError(f'Unsupported database type {self.dialect!r}; supported: {DIALECTS + DOCUMENT_STORES + GENERIC}')
        if self.tls not in TLS_MODES:
            raise ConfigError(f'TLS mode must be one of {TLS_MODES}')
        if self.dialect == 'odbc':
            # An ODBC connection names its driver (or a DSN); host, port, database and user are passed when set.
            if not (self.options.get('driver') or self.options.get('dsn')):
                raise ConfigError('An ODBC connection needs options.driver (for example "PostgreSQL Unicode") or options.dsn')
            if any(c in str(v) for v in (self.options.get('driver'), self.options.get('dsn'), self.host, self.database, self.user)
                   for c in ';{}' if v):
                raise ConfigError('ODBC settings may not contain ; { or }')
        else:
            if not self.host or not self.database or not self.user:
                raise ConfigError('host, database and user are required')
        if not (0 <= int(self.port) < 65536) or (self.dialect != 'odbc' and int(self.port) == 0):
            raise ConfigError('port must be 1-65535')
        secret_path = _secret_key_path(self.options)
        if secret_path:
            raise ConfigError(f'Connection option {secret_path!r} may not contain secrets; use the credential vault')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.id):
            raise ConfigError('Invalid connection id')
        return self

    def public(self) -> dict:
        return asdict(self)


class ConnectionStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else home() / 'connections.json'
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        return {c['id']: c for c in data.get('connections', [])}

    def _save(self, items: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'format': 'flowbench.connections', 'version': 1,
                                   'connections': sorted(items.values(), key=lambda c: c['name'].lower())},
                                  indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def list(self) -> list[Connection]:
        with self._lock:
            return [Connection(**c) for c in self._load().values()]

    def get(self, conn_id: str) -> Connection:
        with self._lock:
            c = self._load().get(conn_id)
        if not c:
            raise ConfigError(f'Unknown database connection {conn_id!r}; create it in the connection manager')
        return Connection(**c)

    def save(self, data: dict, password: str | None = None) -> Connection:
        data = dict(data)
        secret_path = _secret_key_path(data)
        if secret_path:
            raise ConfigError(f'Connection field {secret_path!r} may not contain secrets; pass the password separately')
        data.setdefault('id', uuid.uuid4().hex[:12])
        conn = Connection(**data).validate()
        from .vault import vault
        with self._lock:
            items = self._load()
            items[conn.id] = conn.public()
            if password is not None:
                vault().set(conn.id, conn.user, password)
            self._save(items)
        return conn

    def delete(self, conn_id: str) -> None:
        from .vault import vault
        with self._lock:
            items = self._load()
            items.pop(conn_id, None)
            self._save(items)
        vault().remove(conn_id)

    def secret(self, conn_id: str) -> str | None:
        from .vault import vault
        return vault().get(conn_id)
