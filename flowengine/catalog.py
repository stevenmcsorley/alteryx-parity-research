"""Read access to the 323-entry public tool catalogue (data/catalog.json)."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[1] / 'data' / 'catalog.json'


@lru_cache(maxsize=1)
def entries() -> tuple[dict, ...]:
    if not CATALOG_PATH.is_file():
        return ()
    return tuple(json.loads(CATALOG_PATH.read_text(encoding='utf-8'))['tools'])


def lookup(catalog_id: str) -> dict | None:
    return next((e for e in entries() if e['id'] == catalog_id), None)


def _norm(name: str) -> tuple[str, str]:
    full = re.sub(r'[\\/]+', '/', name).lower()
    return full, full.split('/')[-1]


def lookup_xml(xml_tool_name: str) -> dict | None:
    """Catalogue entry for an original tool identifier (plugin name or macro file name)."""
    key, short = _norm(xml_tool_name)
    for e in entries():
        x, xs = _norm(e.get('xml_tool_name') or '')
        if x == key or xs == short:
            return e
    return None
