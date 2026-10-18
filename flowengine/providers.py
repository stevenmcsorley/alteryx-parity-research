"""Providers: the runtimes, open-source services and vendor services that some tools delegate to.

A provider is described once here (what it serves, what it needs, its licence and the semantics of
its results) and is never chosen implicitly: a workflow node names its ``provider`` and, when the
provider needs one, the ``connection_id`` of a saved connection. Secrets stay in the existing
connection stores and OS credential vault; nothing here stores a secret.

Two small stores sit beside the connection stores, under the same Flowbench home folder:

* ``entitlements.json`` - vendor licences the user *declares* they hold (for example Alteryx
  Server). Flowbench records the declaration and shows it; it never validates an entitlement
  against a vendor, because no public validation API is used.
* ``data_packs.json`` - local datasets registered for a provider family (for example an
  OpenStreetMap extract), with their licence, size and checksum.

Switching provider must never silently change what a result means, so a node may record the
``provider_semantics`` fingerprint it was built with; :func:`check_semantics` reports a mismatch.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import ConfigError
from .runtimes import home

KINDS = ('local-runtime', 'open-source', 'vendor')


@dataclass(frozen=True)
class Provider:
    id: str
    family: str
    name: str
    kind: str
    serves: tuple[str, ...]
    licence: str | None = None          # SPDX id of the software (open-source and runtimes)
    needs: tuple[str, ...] = ()         # python:<module>, exe:<name>, rscript, connection:<kind>, datapack:<family>, entitlement:<product>
    semantics: dict = field(default_factory=dict)
    homepage: str | None = None
    setup: str = ''
    # What has been checked and what still needs a real account, licence or dataset.
    verification: str = ''

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps({'id': self.id, 'semantics': self.semantics}, sort_keys=True).encode()).hexdigest()[:16]

    def public(self) -> dict:
        d = asdict(self)
        d['serves'], d['needs'] = list(self.serves), list(self.needs)
        d['semantics_fingerprint'] = self.fingerprint()
        return d


_REGISTRY: dict[str, Provider] = {}


def register(provider: Provider) -> Provider:
    if provider.kind not in KINDS:
        raise ValueError(f'provider kind must be one of {KINDS}')
    if provider.id in _REGISTRY and _REGISTRY[provider.id] != provider:
        raise ValueError(f'provider {provider.id!r} is already registered')
    _REGISTRY[provider.id] = provider
    return provider


def providers(family: str | None = None) -> list[Provider]:
    _load_builtin()
    return [p for p in _REGISTRY.values() if family is None or p.family == family]


def get(provider_id: str) -> Provider:
    _load_builtin()
    p = _REGISTRY.get(provider_id)
    if not p:
        raise ConfigError(f'Unknown provider {provider_id!r}; see Settings > Integrations & Licences')
    return p


def _load_builtin():
    from . import provider_catalog  # noqa: F401  (registers the built-in providers)


# --------------------------------------------------------------------------- small JSON stores

class _Store:
    format = ''

    def __init__(self, name: str, path: Path | None = None):
        self.path = Path(path) if path else home() / name
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        return {x['id']: x for x in json.loads(self.path.read_text(encoding='utf-8')).get('items', [])}

    def _save(self, items: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'format': self.format, 'version': 1, 'items': sorted(items.values(), key=lambda x: x['id'])},
                                  indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        with self._lock:
            return list(self._load().values())

    def remove(self, item_id: str) -> None:
        with self._lock:
            items = self._load()
            items.pop(item_id, None)
            self._save(items)


class EntitlementStore(_Store):
    """Vendor licences the user declares. Declarations are shown as declared, never as validated."""
    format = 'flowbench.entitlements'

    def __init__(self, path: Path | None = None):
        super().__init__('entitlements.json', path)

    def declare(self, product: str, note: str = '') -> dict:
        product = (product or '').strip()
        if not product:
            raise ConfigError('Name the licensed product (for example "Alteryx Server")')
        with self._lock:
            items = self._load()
            existing = next((x for x in items.values() if x['product'].lower() == product.lower()), None)
            item = {'id': existing['id'] if existing else f'ent_{uuid.uuid4().hex[:10]}', 'product': product,
                    'note': (note or '').strip()[:500], 'declared_at': int(time.time()), 'validated': False}
            items[item['id']] = item
            self._save(items)
        return item

    def has(self, product: str) -> bool:
        return any(x['product'].lower() == product.lower() for x in self.list())


def _checksum(path: Path) -> tuple[int, str]:
    h, total = hashlib.sha256(), 0
    files = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
    for f in files:
        if path.is_dir():
            h.update(f.relative_to(path).as_posix().encode() + b'\0')
        with f.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                h.update(chunk)
                total += len(chunk)
    return total, h.hexdigest()


class DataPackStore(_Store):
    """Local datasets a provider family reads, with their licence and checksum."""
    format = 'flowbench.data-packs'

    def __init__(self, path: Path | None = None):
        super().__init__('data_packs.json', path)

    def register(self, family: str, path: str, licence: str, name: str = '', source: str = '') -> dict:
        if not re.fullmatch(r'[a-z][a-z0-9-]{1,40}', family or ''):
            raise ConfigError('Data pack family must be a short lowercase name such as "osm-extract"')
        if not (licence or '').strip():
            raise ConfigError("Record the data's licence (for example ODbL-1.0); Flowbench does not redistribute data")
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise ConfigError(f'Data pack path not found: {p}')
        size, digest = _checksum(p)
        item = {'id': f'pack_{uuid.uuid4().hex[:10]}', 'family': family, 'name': (name or p.name).strip(), 'path': str(p),
                'licence': licence.strip(), 'source': (source or '').strip(), 'bytes': size, 'sha256': digest,
                'registered_at': int(time.time())}
        with self._lock:
            items = self._load()
            items[item['id']] = item
            self._save(items)
        return item

    def for_family(self, family: str) -> list[dict]:
        return [x for x in self.list() if x['family'] == family and Path(x['path']).exists()]


# --------------------------------------------------------------------------- status

def _check(need: str, entitlements: EntitlementStore, packs: DataPackStore) -> dict:
    kind, _, arg = need.partition(':')
    if kind == 'python':
        ok = importlib.util.find_spec(arg) is not None
        return {'need': need, 'ok': ok, 'detail': f'Python package {arg}' + ('' if ok else ' is not installed')}
    if kind == 'exe':
        found = shutil.which(arg)
        return {'need': need, 'ok': bool(found), 'detail': found or f'{arg} not found on PATH'}
    if kind == 'rscript':
        from .runtimes import find_rscript
        found = find_rscript()
        return {'need': need, 'ok': bool(found), 'detail': found or 'Rscript not found (install R or set FLOWENGINE_RSCRIPT)'}
    if kind == 'connection':
        from .service_connections import ServiceConnectionStore
        n = len(ServiceConnectionStore().list(arg))
        return {'need': need, 'ok': n > 0, 'detail': f'{n} saved {arg} connection(s)' if n else f'No saved {arg} connection'}
    if kind == 'datapack':
        n = len(packs.for_family(arg))
        return {'need': need, 'ok': n > 0, 'detail': f'{n} registered {arg} data pack(s)' if n else f'No {arg} data pack registered'}
    if kind == 'entitlement':
        ok = entitlements.has(arg)
        return {'need': need, 'ok': ok, 'detail': f'{arg} licence declared (not validated)' if ok else f'No {arg} licence declared'}
    return {'need': need, 'ok': False, 'detail': f'Unknown requirement {need!r}'}


def status(provider: Provider, entitlements: EntitlementStore | None = None, packs: DataPackStore | None = None) -> dict:
    entitlements, packs = entitlements or EntitlementStore(), packs or DataPackStore()
    checks = [_check(n, entitlements, packs) for n in provider.needs]
    return {**provider.public(), 'ready': all(c['ok'] for c in checks), 'checks': checks}


def availability(entitlements: EntitlementStore | None = None, packs: DataPackStore | None = None) -> list[dict]:
    """For every catalogue entry served by a provider: which providers can serve it and whether one is ready."""
    entitlements, packs = entitlements or EntitlementStore(), packs or DataPackStore()
    by_entry: dict[str, list[dict]] = {}
    for p in providers():
        s = status(p, entitlements, packs)
        for cid in p.serves:
            by_entry.setdefault(cid, []).append({'provider': p.id, 'name': p.name, 'kind': p.kind, 'ready': s['ready'],
                                                 'missing': [c['detail'] for c in s['checks'] if not c['ok']]})
    return [{'catalog_id': cid, 'available': any(x['ready'] for x in ps), 'providers': ps} for cid, ps in sorted(by_entry.items())]


def resolve(config: dict, family: str) -> Provider:
    """The provider a node names, checked against the family the tool needs. Never falls back to another."""
    pid = config.get('provider')
    if not pid:
        names = ', '.join(p.id for p in providers(family)) or 'none registered'
        raise ConfigError(f'Choose a provider ({names}); Flowbench does not pick one for you')
    p = get(pid)
    if p.family != family:
        raise ConfigError(f'Provider {pid!r} provides {p.family}, not {family}')
    problem = check_semantics(config, p)
    if problem:
        raise ConfigError(problem)
    return p


def check_semantics(config: dict, provider: Provider) -> str | None:
    recorded = config.get('provider_semantics')
    if recorded and recorded != provider.fingerprint():
        return (f'Provider {provider.id!r} now describes its results differently from when this node was set up '
                f'({recorded} != {provider.fingerprint()}); review the provider notes and confirm the node again')
    return None
