#!/usr/bin/env python3
"""Opt-in public docs metadata auditor: writes links, headings and content hashes only.

To avoid unauthorised redistribution, does not store vendor article bodies.
Respects robots.txt and throttles reads. Use after sync_official_catalog.py.
"""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from pathlib import Path
from catalog_lib import CATALOG, ROOT

UA='IndependentWorkflowParityResearch/0.1 (public documentation indexing)'
ALLOW_HOST='help.alteryx.com'
OUT=ROOT/'data'/'public-doc-audit.json'

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--max-pages',type=int,default=15)
    p.add_argument('--delay',type=float,default=1.5)
    p.add_argument('--timeout',type=int,default=15)
    args=p.parse_args()
    if args.max_pages<=0 or args.delay<1: p.error('max-pages must be >0; delay must be >=1s')
    rows=json.loads(CATALOG.read_text(encoding='utf-8'))['tools']
    urls=sorted({r['documentation_url'] for r in rows if r.get('documentation_url')})
    if not urls:
        p.error('No verified per-tool URLs. Run python scripts/sync_official_catalog.py first.')
    rp=RobotFileParser()
    rp.set_url('https://help.alteryx.com/robots.txt')
    try: rp.read()
    except Exception as e: p.error(f'Could not check robots policy: {e}')
    from bs4 import BeautifulSoup
    results=[]
    for url in urls[:args.max_pages]:
        if urlparse(url).hostname!=ALLOW_HOST or not rp.can_fetch(UA,url):
            results.append({'url':url,'status':'not-permitted-by-host-or-robots'})
            continue
        try:
            req=Request(url,headers={'User-Agent':UA,'Accept':'text/html'})
            with urlopen(req,timeout=args.timeout) as r:
                raw=r.read(2_000_000)
                typ=r.headers.get('Content-Type','')
            if 'html' not in typ: raise ValueError('not an HTML document')
            soup=BeautifulSoup(raw,'html.parser')
            heading=lambda tag: [x.get_text(' ',strip=True)[:200] for x in soup.find_all(tag)[:40]]
            results.append({'url':url,'status':'metadata-fetched','retrieved_utc':datetime.now(timezone.utc).isoformat(),'sha256':hashlib.sha256(raw).hexdigest(),'page_title':soup.title.get_text(' ',strip=True)[:250] if soup.title else None,'h1':heading('h1'),'h2':heading('h2'),'h3':heading('h3')})
        except Exception as e:
            results.append({'url':url,'status':'fetch-failed','error_type':type(e).__name__})
        time.sleep(args.delay)
    OUT.write_text(json.dumps({'notes':'Public documentation metadata only; no vendor page bodies. Not a pinned-build parity assertion.','total_unique_urls':len(urls),'processed':len(results),'pages':results},indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(f'Audited {len(results)}/{len(urls)} URLs; output {OUT}')

if __name__=='__main__':main()
