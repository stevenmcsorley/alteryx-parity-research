"""Explorer Box: what a documentation box on the canvas shows - a web page, a file or a folder.

The box never runs as part of the workflow. The editor asks for its contents with ``workflow.explore``;
paths resolve against the workflow folder, folders list one level, files return a short text preview,
and web pages are handed back as a URL for the editor to open (Flowbench does not fetch them).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from .errors import EngineError

LIST_LIMIT = 500
PREVIEW_BYTES = 16 * 1024


def explore(url: str, base_dir=None) -> dict:
    target = (url or '').strip()
    if not target:
        raise EngineError('Explorer Box has no URL, file or folder', code='explorer-invalid')
    scheme = urlparse(target).scheme.lower()
    if scheme in ('http', 'https'):
        return {'kind': 'web', 'url': target}
    if scheme == 'file':
        target = unquote(urlparse(target).path)
    elif scheme and len(scheme) > 1:  # one letter is a Windows drive
        raise EngineError(f'Explorer Box cannot show {scheme}: addresses', code='explorer-invalid')
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = Path(base_dir or '.') / path
    path = path.resolve()
    if path.is_dir():
        entries = sorted((p for p in path.iterdir() if not p.name.startswith('.')),
                         key=lambda p: (not p.is_dir(), p.name.lower()))
        return {'kind': 'folder', 'path': str(path), 'total': len(entries),
                'entries': [{'name': p.name, 'folder': p.is_dir(), 'bytes': None if p.is_dir() else p.stat().st_size}
                            for p in entries[:LIST_LIMIT]]}
    if path.is_file():
        size = path.stat().st_size
        with path.open('rb') as f:
            head = f.read(PREVIEW_BYTES)
        text = None if b'\0' in head else head.decode('utf-8', errors='replace')
        return {'kind': 'file', 'path': str(path), 'bytes': size, 'text': text, 'truncated': size > PREVIEW_BYTES}
    raise EngineError(f'Explorer Box target not found: {path}', code='explorer-not-found')
