"""Managed runtimes for user code: isolated Python environments and separately installed R.

Python: named virtual environments under ``<home>/envs/<name>`` created from a pinned
requirements list. Each environment records a manifest (python version, requested
requirements, resolved ``pip freeze`` lock) so runs are reproducible and upgrades are
explicit. The default environment is the engine's own interpreter (Python 3.12 baseline).

R: discovered ``Rscript`` executables (FLOWENGINE_RSCRIPT, PATH, standard Windows install
folders). Package dependencies are declared per tool and verified with ``requireNamespace``
before running; versions are recorded in the run output.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

from .errors import ConfigError, DataError

BASELINE = (3, 12)


def home() -> Path:
    return Path(os.environ.get('FLOWENGINE_HOME', Path.home() / '.flowbench'))


def env_dir(name: str) -> Path:
    if not name.replace('-', '').replace('_', '').isalnum():
        raise ConfigError(f'Invalid environment name {name!r}')
    return home() / 'envs' / name


def interpreter(name: str | None) -> str:
    """Python executable for an environment name (None/'default' = the engine interpreter)."""
    if not name or name == 'default':
        return sys.executable
    d = env_dir(name)
    exe = d / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not exe.is_file():
        raise ConfigError(f'Python environment {name!r} does not exist; create it with '
                          f'"python -m flowengine env create {name} -r requirements.txt"')
    return str(exe)


def create_env(name: str, requirements: list[str], python: str | None = None) -> dict:
    d = env_dir(name)
    if d.exists():
        raise ConfigError(f'Environment {name!r} already exists at {d}')
    d.parent.mkdir(parents=True, exist_ok=True)
    if python:
        subprocess.run([python, '-m', 'venv', str(d)], check=True)
    else:
        venv.EnvBuilder(with_pip=True).create(d)
    exe = interpreter(name)
    if requirements:
        subprocess.run([exe, '-m', 'pip', 'install', '--disable-pip-version-check', *requirements], check=True)
    return write_manifest(name, requirements)


def write_manifest(name: str, requirements: list[str]) -> dict:
    exe = interpreter(name)
    version = subprocess.run([exe, '-c', 'import sys;print(".".join(map(str, sys.version_info[:3])))'],
                             capture_output=True, text=True, check=True).stdout.strip()
    lock = subprocess.run([exe, '-m', 'pip', 'freeze', '--disable-pip-version-check'], capture_output=True, text=True,
                          check=True).stdout.split()
    manifest = {'name': name, 'python': version, 'requirements': requirements, 'lock': lock}
    (env_dir(name) / 'flowbench-env.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def list_envs() -> list[dict]:
    root = home() / 'envs'
    out = [{'name': 'default', 'python': '.'.join(map(str, sys.version_info[:3])), 'path': sys.executable}]
    if root.is_dir():
        for d in sorted(root.iterdir()):
            m = d / 'flowbench-env.json'
            if m.is_file():
                out.append({**json.loads(m.read_text(encoding='utf-8')), 'path': str(d)})
    return out


def find_rscript() -> str | None:
    cand = os.environ.get('FLOWENGINE_RSCRIPT') or shutil.which('Rscript')
    if cand:
        return cand
    for base in (Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'R',
                 Path.home() / 'AppData/Local/Programs/R'):
        if base.is_dir():
            for d in sorted(base.iterdir(), reverse=True):
                exe = d / 'bin' / 'Rscript.exe'
                if exe.is_file():
                    return str(exe)
    return None


def require_rscript() -> str:
    exe = find_rscript()
    if not exe:
        raise DataError('No R installation found. Install R (CRAN) and/or set FLOWENGINE_RSCRIPT to Rscript.exe',
                        code='runtime-missing')
    return exe


def clean_env(extra: dict | None = None) -> dict:
    """Minimal environment for user code: no inherited credentials or tokens."""
    keep = ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP', 'COMSPEC', 'PATHEXT', 'WINDIR', 'LANG', 'HOME', 'USERPROFILE',
            'LOCALAPPDATA', 'APPDATA', 'R_LIBS_USER')
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env['PYTHONIOENCODING'] = 'utf-8'
    env.update(extra or {})
    return env
