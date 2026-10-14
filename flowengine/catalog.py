"""Read access to the 323-entry public tool catalogue (data/catalog.json)."""
from __future__ import annotations

import json
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
