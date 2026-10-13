#!/usr/bin/env python3
"""Apply manual public XML tool name transcription; dynamic official HTML sync supersedes this."""
import json
from collections import defaultdict
from pathlib import Path
from catalog_lib import ROOT, SOURCE, seed_rows, save_catalog

source = ROOT/'data/tool-xml-reference.txt'
bycat=defaultdict(list)
cat=None
for line in source.read_text(encoding='utf-8').splitlines():
    line=line.strip()
    if not line or line.startswith('#'):continue
    if line.startswith('[') and line.endswith(']'):
        cat=line[1:-1]
    else:
        if cat is None:raise ValueError('Missing category')
        bycat[cat].extend(s.strip() for s in line.split(';') if s.strip())
seeds=seed_rows()
expected=defaultdict(list)
for row in seeds:expected[row['category']].append(row['name'])
for c,names in expected.items():
    if len(bycat[c])!=len(names):
        raise SystemExit(f'Wrong xml count for {c}: {len(bycat[c])} vs {len(names)}')
if set(bycat)!=set(expected):raise SystemExit('Missing/extra categories')
indices=defaultdict(int)
for r in seeds:
    c=r['category']
    r['xml_tool_name']=bycat[c][indices[c]]
    r['source_state']='public-list-manually-transcribed-xml'
    indices[c]+=1
# Names/roles involving add-on clearly documented on official list; verify other roles with live sync.
add_on_names={'Find Nearest','Non Overlap Drivetime','Trade Area','Reverse Geocoder','US Geocoder'}
add_on_categories={'Behavior Analysis (Deprecated)'}
basic_full_names={'Auto Insights Uploader','Browse','Directory','Input Data','Output Data','Text Input','Auto Field','Data Cleanse Pro','Filter','Formula','Generate Rows','Multi-Field Formula','Multi-Row Formula','Record ID','Sample','Select','Sort','Unique','Append Fields','Find Replace','Join','Join Multiple','Union','DateTime','RegEx','Text To Columns','Cross Tab','Summarize','Transpose','Comment','Tool Container','Block Until Done','Control Container','Dynamic Input','Dynamic Rename','Message'}
for r in seeds:
    if r['name'] in basic_full_names and r['category'] not in ('Connectors',):r['user_role']='Basic, Full'
    elif r['category']=='Connectors':r['user_role']='Basic, Full' if r['name'] not in ('Marketo Append','Marketo Input','Marketo Output') else 'Full'
    else:r['user_role']='Full'
    if r['name'] in add_on_names or r['category'] in add_on_categories:
        r['user_role']+=' (Data Packages Add-on)'
save_catalog(seeds,{'source_url':SOURCE, 'target_version':'Designer 2026.1.4.126',
                    'status':'public-list-manually-transcribed-names-and-xml',
                    'seed_row_count':len(seeds),
                    'source_accessed_date':'2026-10-13',
                    'source_type':'manually-transcribed-public-tool-list',
                    'warning':'Public XML tool identifiers/user role text manually transcribed and unverified against installed 2026.1 build. Official per-tool help URL enrichment requires sync_official_catalog.py. No tool implementations or tests.'})
print(f'Populated {len(seeds)} public XML identifiers and user roles; verify in official online sync and pinned desktop build.')
