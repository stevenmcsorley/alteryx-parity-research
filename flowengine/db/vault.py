"""Credential vault for database passwords.

* Windows: Windows Credential Manager (advapi32 CredWriteW/CredReadW/CredDeleteW), generic credentials
  with target ``Flowbench:connection:<id>``, persisted per user (CRED_PERSIST_LOCAL_MACHINE).
* Other platforms: the ``keyring`` package (macOS Keychain, Secret Service).
* ``FLOWBENCH_VAULT=env``: read-only vault from environment variables ``FLOWBENCH_SECRET_<ID>`` for headless
  CI; it can never be selected implicitly.

Secrets never enter connection files, workflow documents, run manifests, event logs or history.
"""
from __future__ import annotations

import os
import re

from ..errors import EngineError

SERVICE = 'Flowbench'


class VaultError(EngineError):
    code = 'vault-error'


def _target(conn_id: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', conn_id):
        raise VaultError(f'Invalid connection id {conn_id!r}')
    return f'{SERVICE}:connection:{conn_id}'


class WindowsVault:
    name = 'windows-credential-manager'

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [('dwLowDateTime', wintypes.DWORD), ('dwHighDateTime', wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                        ('Comment', wintypes.LPWSTR), ('LastWritten', FILETIME),
                        ('CredentialBlobSize', wintypes.DWORD), ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                        ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        self.ct, self.wt, self.CRED = ctypes, wintypes, CREDENTIALW
        adv = ctypes.WinDLL('advapi32', use_last_error=True)
        self.write = adv.CredWriteW
        self.write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self.read = adv.CredReadW
        self.read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        self.delete = adv.CredDeleteW
        self.delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.free = adv.CredFree

    def set(self, conn_id: str, user: str, secret: str) -> None:
        ct = self.ct
        blob = secret.encode('utf-16-le')
        buf = (ct.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = self.CRED()
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = _target(conn_id)
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_ubyte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE (this user, survives logoff)
        cred.UserName = user or ''
        if not self.write(ct.byref(cred), 0):
            raise VaultError(f'Could not store credential (Windows error {ct.get_last_error()})')

    def get(self, conn_id: str) -> str | None:
        ct = self.ct
        p = ct.POINTER(self.CRED)()
        if not self.read(_target(conn_id), 1, 0, ct.byref(p)):
            if ct.get_last_error() == 1168:  # ERROR_NOT_FOUND
                return None
            raise VaultError(f'Could not read credential (Windows error {ct.get_last_error()})')
        try:
            c = p.contents
            raw = ct.string_at(c.CredentialBlob, c.CredentialBlobSize)
            return raw.decode('utf-16-le')
        finally:
            self.free(p)

    def remove(self, conn_id: str) -> None:
        if not self.delete(_target(conn_id), 1, 0) and self.ct.get_last_error() != 1168:
            raise VaultError(f'Could not delete credential (Windows error {self.ct.get_last_error()})')


class KeyringVault:
    name = 'keyring'

    def __init__(self):
        import keyring  # noqa: F401
        self.k = keyring

    def set(self, conn_id, user, secret):
        self.k.set_password(SERVICE, _target(conn_id), secret)

    def get(self, conn_id):
        return self.k.get_password(SERVICE, _target(conn_id))

    def remove(self, conn_id):
        try:
            self.k.delete_password(SERVICE, _target(conn_id))
        except Exception:
            pass


class EnvVault:
    """Headless CI only: FLOWBENCH_SECRET_<ID> (id upper-cased, non-alphanumerics -> _). Read-only."""
    name = 'environment (CI)'

    @staticmethod
    def _var(conn_id):
        return 'FLOWBENCH_SECRET_' + re.sub(r'[^A-Za-z0-9]', '_', _target(conn_id).split(':')[-1]).upper()

    def set(self, conn_id, user, secret):
        raise VaultError(f'The environment vault is read-only; set {self._var(conn_id)} instead')

    def get(self, conn_id):
        return os.environ.get(self._var(conn_id))

    def remove(self, conn_id):
        pass


_VAULT = None


def vault():
    global _VAULT
    if _VAULT is None:
        choice = os.environ.get('FLOWBENCH_VAULT', '').lower()
        if choice == 'env':
            _VAULT = EnvVault()
        elif choice in ('', 'windows') and os.name == 'nt':
            _VAULT = WindowsVault()
        else:
            try:
                _VAULT = KeyringVault()
            except ImportError:
                raise VaultError('No credential vault available: install "keyring" or set FLOWBENCH_VAULT=env (CI)') from None
    return _VAULT
