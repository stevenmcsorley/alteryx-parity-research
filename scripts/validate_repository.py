#!/usr/bin/env python3
import json
import sys
from collections import Counter
from pathlib import Path
from catalog_lib import ROOT, seed_rows

rows = json.loads((ROOT/'data/catalog.json').read_text(encoding='utf-8'))['tools']
seeds = seed_rows()
errors = []
if len(seeds)!=323:errors.append(f'Seed count changed: {len(seeds)} != 323')
if len(rows)<323:errors.append(f'Catalog unexpectedly shrank: {len(rows)}')
keyset = {(r['category'],r['name']) for r in rows}
if any((r['category'],r['name']) not in keyset for r in seeds):errors.append('Seed tool missing from catalog')
ids=[r['id'] for r in rows]
if len(set(ids)) != len(ids):errors.append('Duplicate catalog IDs')
for i,r in enumerate(rows):
 if r['implementation_status'] not in ('not-started','in-progress','implemented','blocked'):
  errors.append(f'Bad implementation status at {i}')
 if r['verification_status'] not in ('not-tested','partial','verified','failed','blocked'):
  errors.append(f'Bad verification status at {i}')
 if r['verification_status']=='verified' and r['implementation_status']!='implemented':
  errors.append(f'Claimed verified but not implemented: {r["id"]}')
print(f'Catalog: {len(rows)} category-tool rows, {len(keyset)} unique category/name pairs, {len(set(r["name"] for r in rows))} distinct names.')
print(f'With XML identifiers: {sum(bool(r.get("xml_tool_name")) for r in rows)}; verified: {sum(r["verification_status"]=="verified" for r in rows)}')
if errors:
 for e in errors:print('ERROR',e,file=sys.stderr)
 sys.exit(1)
print('PASS')
