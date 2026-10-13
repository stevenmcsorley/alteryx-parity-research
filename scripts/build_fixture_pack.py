#!/usr/bin/env python3
"""Generate original, deterministic fixtures; these are NOT vendor-oracle outputs."""
from __future__ import annotations
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from catalog_lib import ROOT

DEST = ROOT/'fixtures'/'generated'

def csv_write(name, fields, rows):
    p = DEST/name
    with p.open('w', encoding='utf-8', newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields)
        w.writeheader();w.writerows(rows)
    return {'file': name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'rows':len(rows)}

def main():
    DEST.mkdir(parents=True, exist_ok=True)
    customers = [
      {'customer_id':'101','name':'Ada','region':'UK','score':'75'},
      {'customer_id':'102','name':'Ben','region':'EU','score':''},
      {'customer_id':'102','name':'Ben Duplicate','region':'EU','score':'85'},
      {'customer_id':'103','name':'Zoë','region':'UK','score':'92'},
      {'customer_id':'104','name':'李','region':'APAC','score':'0'},
      {'customer_id':'105','name':'','region':'UK','score':'-5'},
    ]
    orders = [
      {'order_id':'o1','customer_id':'101','amount':'12.50'},
      {'order_id':'o2','customer_id':'101','amount':'7.50'},
      {'order_id':'o3','customer_id':'102','amount':'99.95'},
      {'order_id':'o4','customer_id':'999','amount':'3.00'},
    ]
    files=[]
    files.append(csv_write('customers.csv',['customer_id','name','region','score'], customers))
    files.append(csv_write('orders.csv',['order_id','customer_id','amount'],orders))
    files.append(csv_write('filter-region-uk.csv',['customer_id','name','region','score'],[r for r in customers if r['region']=='UK']))
    seen=set();unique=[];dup=[]
    for r in customers:
      (dup if r['customer_id'] in seen else unique).append(r)
      seen.add(r['customer_id'])
    files.append(csv_write('unique-first-by-customer-id.csv',['customer_id','name','region','score'],unique))
    files.append(csv_write('duplicate-by-customer-id.csv',['customer_id','name','region','score'],dup))
    joined=[{'order_id':o['order_id'],'customer_id':o['customer_id'],'name':c['name'],'amount':o['amount']}
            for o in orders for c in customers if o['customer_id']==c['customer_id']]
    files.append(csv_write('inner-join-orders-customers.csv',['order_id','customer_id','name','amount'],joined))
    counts=defaultdict(int)
    for c in customers:counts[c['region']]+=1
    files.append(csv_write('count-by-region.csv',['region','count'],[{'region':k,'count':counts[k]} for k in sorted(counts)]))
    manifest={'kind':'original-standalone-fixture-pack','scope':'independent sample calculations; no claims about vendor output',
              'tested_vendor_version':None,'created_by':'scripts/build_fixture_pack.py',
              'cases':[{'name':'Filter UK','tool':'Filter','output':'filter-region-uk.csv'},
                       {'name':'Unique by customer_id','tool':'Unique','outputs':['unique-first-by-customer-id.csv','duplicate-by-customer-id.csv']},
                       {'name':'Join on customer_id','tool':'Join','output':'inner-join-orders-customers.csv'},
                       {'name':'Count by region','tool':'Summarize','output':'count-by-region.csv'}],
              'files':files}
    (DEST/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(f'Generated {len(files)} deterministic fixture files in {DEST.relative_to(ROOT)}')

if __name__=='__main__':main()
