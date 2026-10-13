#!/usr/bin/env python3
import json
from pathlib import Path
from catalog_lib import ROOT
catalog=json.loads((ROOT/'data/catalog.json').read_text(encoding='utf-8'))['tools']
rows=[]
for r in catalog:
 rows.append({'tool_id':r['id'],'category':r['category'],'name':r['name'],'xml_tool_name':r.get('xml_tool_name'),
              'baseline_cases':['null-empty','schema-and-types','boundary-values','order-and-duplicates','error-behaviour','serialization','performance'],
              'configuration_cases':[],'oracle_status':'missing','implementation_status':r['implementation_status'],
              'verification_status':r['verification_status'], 'evidence_refs':[]})
out=ROOT/'data/test-matrix.json'
out.write_text(json.dumps({'notes':'Coverage requirements only. No tests have been executed.','tools':rows},indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
print(f'Created untested matrix for {len(rows)} category/tool occurrences')
