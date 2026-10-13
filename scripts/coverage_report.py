#!/usr/bin/env python3
"""Produce a factual coverage report. Never imply research documents mean parity."""
from __future__ import annotations
import json
from collections import Counter
from datetime import date
from pathlib import Path
from catalog_lib import ROOT, CATALOG

def report():
    rows=json.loads(CATALOG.read_text(encoding='utf-8'))['tools']
    bycat=Counter(x['category'] for x in rows)
    implementation=Counter(x.get('implementation_status','unknown') for x in rows)
    verification=Counter(x.get('verification_status','unknown') for x in rows)
    research=sum((ROOT/'tool-contracts'/slug(x['category'])/(slug(x['name'])+'.md')).is_file() for x in rows)
    lines=[
        '# Research and parity status',
        '',
        '> **Catalogue entries are NOT implemented or verified tools.**',
        '',
        f'- Catalogued category/tool occurrences: **{len(rows)}**',
        f'- Distinct tool names: **{len(set(x["name"] for x in rows))}**',
        f'- Per-tool research records: **{research} / {len(rows)}**',
        f'- XML names present (manually transcribed unless synced): **{sum(bool(x.get("xml_tool_name")) for x in rows)}**',
        f'- Public per-tool documentation URL resolved: **{sum(bool(x.get("documentation_url")) for x in rows)}**',
        '', '## Implementation states', '',
        *[f'- {s}: {n}' for s,n in sorted(implementation.items())],
        '', '## Verification states', '',
        *[f'- {s}: {n}' for s,n in sorted(verification.items())],
        '', '## Category inventory', '', '| Category | Entries |', '|---|---:|',
        *[f'| {cat} | {num} |' for cat,num in sorted(bycat.items())],
        '', '## Interpretations', '',
        'Research records contain checklists and unverified identities. They are not feature implementations. A synthetic engine prototype, if present, is **not** an Alteryx conformance result.',
        'Vendor-specific add-ons, external services and proprietary datasets require separately licensed/authorised equivalents. See the dependency and rights documents.',
    ]
    return '\n'.join(lines)+'\n'

def slug(s):
    import re
    return re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')

def main():
    dest=ROOT/'docs'/'COVERAGE.md'
    dest.write_text(report(), encoding='utf-8')
    print(f'Wrote {dest}')

if __name__ == '__main__':main()
