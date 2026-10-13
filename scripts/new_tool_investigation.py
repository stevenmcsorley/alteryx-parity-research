#!/usr/bin/env python3
"""Scaffold original research notes for one catalogued tool (no binary inspection)."""
import argparse
import json
import re
from pathlib import Path
from catalog_lib import ROOT

p=argparse.ArgumentParser()
p.add_argument('tool_id',help='Use an ID from data/catalog.json, e.g. preparation:filter')
args=p.parse_args()
rows=json.loads((ROOT/'data/catalog.json').read_text(encoding='utf-8'))['tools']
match=next((r for r in rows if r['id']==args.tool_id),None)
if match is None:p.error('Tool ID not in official-name seed')
path=ROOT/'research'/re.sub(r'[^a-z0-9]+','-',match['category'].lower()).strip('-')/(re.sub(r'[^a-z0-9]+','-',match['name'].lower()).strip('-')+'.md')
path.parent.mkdir(parents=True,exist_ok=True)
if path.exists():p.error(f'Refusing overwrite: {path}')
path.write_text(f'''# {match['name']} — investigation\n\n- Catalog ID: `{match['id']}`\n- Category: {match['category']}\n- Public source: {match['source_url']}\n- XML tool ID: {match.get('xml_tool_name') or '**Unverified — sync catalog**'}\n- Official help: {match.get('documentation_url') or '**Unverified — sync catalog**'}\n- Pinned app build: **2026.1.4.126**\n- Authorisation status: **not reviewed**\n- Implementation status: **not started**\n- Reference tests: **not executed**\n\n## Public documented configuration and constraints\n\nPending evidence.\n\n## Anchor/input-output contracts and schemas\n\nPending evidence.\n\n## Independent algorithm/data model\n\nPending design.\n\n## Edge cases/errors/determinism\n\nPending tests.\n\n## Sources and retrieval dates\n\nPending.\n\n## Test results and unresolved questions\n\nPending.\n''',encoding='utf-8')
print(path.relative_to(ROOT))
