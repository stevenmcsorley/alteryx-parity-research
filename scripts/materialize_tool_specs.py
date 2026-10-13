#!/usr/bin/env python3
"""Create tracked, evidence-labelled per-tool research briefs from the catalog.

Existing hand-authored notes are left untouched unless --refresh is requested.
Never changes verification statuses and never invents tool configuration/anchors.
"""
import argparse
import json
import re
from pathlib import Path
from catalog_lib import CATALOG, ROOT

OUT = ROOT / 'tool-contracts'

def safe_slug(value):
    return re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')

def content(row):
    name = row['name']
    doc = row.get('documentation_url')
    doc_line = f"- Specific reference: {doc}" if doc else '- Specific reference: **NOT YET VERIFIED** — run public catalogue sync'
    return f'''# {name}

> **Research record; NOT a working tool implementation or verified specification.**

## Identity

- Catalogue ID: `{row['id']}`
- Category: {row['category']}
- Published XML identifier: `{row.get('xml_tool_name') or 'UNKNOWN'}` (manual transcription; verify against installed reference)
- Published user role: {row.get('user_role') or 'UNKNOWN'}
- Reference build: Designer 2026.1.4.126 (installed-build comparison outstanding)
- Official catalogue: {row['source_url']}
{doc_line}
- Implementation: **not-started**
- Conformance verification: **not-tested**

## Research to complete (do not infer)

- [ ] Pin per-tool public documentation and availability/version history
- [ ] Define inputs, output anchors, schema and metadata rules
- [ ] Define configuration fields, defaults, UI validation and errors
- [ ] Define row order, null semantics, locale/timezone and deterministic behaviour
- [ ] Identify add-ons, licences, external APIs, services and data-package prerequisites
- [ ] Define concrete independent fixtures, edge cases and expected outputs
- [ ] Implement real independently written kernel or adapter
- [ ] Demonstrate end-to-end workflow execution and unit/integration tests
- [ ] Where authorised, capture reference evidence and run differential tests
- [ ] Record limitations, performance, security and version compatibility

## Sources and observations

No application behaviour or compatibility has been verified. Add specific links, observed facts, dates, permission scope and evidence references here. Keep assumptions explicitly labelled.

## Conformance evidence

Not tested. Do not mark verified without a report, input/config hashes and a reproducible comparison rule.
'''

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--refresh', action='store_true', help='Replace generated briefs; backs up hand-authored notes first instead of overwriting silently')
    args = p.parse_args()
    if args.refresh:
        p.error('Destructive refresh deliberately unsupported. Edit per-tool records manually; new tools can still be added.')
    tools = json.loads(CATALOG.read_text(encoding='utf-8'))['tools']
    created = 0
    for t in tools:
        path = OUT / safe_slug(t['category']) / f"{safe_slug(t['name'])}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            continue
        path.write_text(content(t), encoding='utf-8')
        created += 1
    print(f'Created {created} per-tool research briefs; {len(tools)} tools accounted for.')

if __name__ == '__main__':
    main()
