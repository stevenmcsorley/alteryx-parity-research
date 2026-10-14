#!/usr/bin/env python3
"""Opt-in public docs metadata auditor: links, headings, anchor labels, hashes only.

To avoid redistributing vendor content, page bodies are never written to the
repository. An optional ``--cache-dir`` (keep it OUTSIDE Git) lets the extractor
be re-run without refetching. Respects robots.txt and throttles reads.
Use after sync_official_catalog.py.
"""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from catalog_lib import CATALOG, ROOT
from doc_extract import extract_metadata

UA = 'IndependentWorkflowParityResearch/0.1 (public documentation indexing)'
ALLOW_HOST = 'help.alteryx.com'
OUT = ROOT / 'data' / 'public-doc-audit.json'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--max-pages', type=int, default=15)
    p.add_argument('--delay', type=float, default=1.5)
    p.add_argument('--timeout', type=int, default=20)
    p.add_argument('--cache-dir', type=Path, help='Local page cache outside the repository (never commit)')
    args = p.parse_args()
    if args.max_pages <= 0 or args.delay < 1:
        p.error('max-pages must be >0; delay must be >=1s')
    if args.cache_dir and ROOT in args.cache_dir.resolve().parents:
        p.error('cache-dir must be outside the repository so vendor pages are never committed')
    rows = json.loads(CATALOG.read_text(encoding='utf-8'))['tools']
    urls = sorted({r['documentation_url'] for r in rows if r.get('documentation_url')})
    if not urls:
        p.error('No verified per-tool URLs. Run python scripts/sync_official_catalog.py first.')
    rp = RobotFileParser()
    rp.set_url('https://help.alteryx.com/robots.txt')
    try:
        rp.read()
    except Exception as e:
        p.error(f'Could not check robots policy: {e}')
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for url in urls[:args.max_pages]:
        if urlparse(url).hostname != ALLOW_HOST or not rp.can_fetch(UA, url):
            results.append({'url': url, 'status': 'not-permitted-by-host-or-robots'})
            continue
        cached = args.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + '.html') if args.cache_dir else None
        try:
            fetched_now = False
            if cached and cached.exists():
                raw = cached.read_bytes()
            else:
                req = Request(url, headers={'User-Agent': UA, 'Accept': 'text/html'})
                with urlopen(req, timeout=args.timeout) as r:
                    raw = r.read(4_000_000)
                    if 'html' not in r.headers.get('Content-Type', ''):
                        raise ValueError('not an HTML document')
                fetched_now = True
                if cached:
                    cached.write_bytes(raw)
            meta = extract_metadata(raw.decode('utf-8', errors='replace'))
            results.append({'url': url, 'status': 'metadata-extracted',
                            'retrieved_utc': datetime.now(timezone.utc).isoformat(),
                            'sha256': hashlib.sha256(raw).hexdigest(), **meta})
            if fetched_now:
                time.sleep(args.delay)
        except Exception as e:
            results.append({'url': url, 'status': 'fetch-failed', 'error_type': type(e).__name__})
            time.sleep(args.delay)
    OUT.write_text(json.dumps({
        'notes': 'Public documentation metadata only (titles, headings, anchor labels, dependency keyword '
                 'signals, hashes); no vendor page bodies. Auto-extracted values require human review. '
                 'Rolling /current/ docs; not a pinned-build parity assertion.',
        'source_index': 'https://help.alteryx.com/current/en/designer/tools/designer-tools-list.html',
        'total_unique_urls': len(urls), 'processed': len(results),
        'extracted': sum(r['status'] == 'metadata-extracted' for r in results),
        'pages': results}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'Audited {len(results)}/{len(urls)} URLs; output {OUT}')


if __name__ == '__main__':
    main()
