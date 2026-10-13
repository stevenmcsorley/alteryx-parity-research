#!/usr/bin/env python3
"""Fetch public Alteryx tool directory and extract names, XML identifiers, role and doc links.
Does not fetch application binaries, protected docs, gated content or customer data.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from catalog_lib import ROOT, SOURCE, CATALOG, seed_rows, save_catalog, parse_official_html, merge_official


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input-html', type=Path, help='Use a saved public HTML page instead of network access')
    p.add_argument('--minimum-rows', type=int, default=300)
    args = p.parse_args()
    if args.input_html:
        raw = args.input_html.read_bytes()
        mode = 'saved-official-html'
    else:
        req = Request(SOURCE, headers={'User-Agent':'AlteryxParityResearch/0.1 (+documentation inventory; contact project owner)'})
        with urlopen(req, timeout=40) as response:
            raw = response.read(8_000_000)
        mode = 'public-official-page'
    html = raw.decode('utf-8', errors='replace')
    official = parse_official_html(html)
    if len(official) < args.minimum_rows:
        sys.exit(f'Refusing to replace seed catalog: parsed only {len(official)} entries; expected at least {args.minimum_rows}. Check site structure/version or use --input-html.')
    existing = json.loads(CATALOG.read_text(encoding="utf-8"))["tools"] if CATALOG.exists() else seed_rows()
    rows = merge_official(existing, official)
    save_catalog(rows, {
        'source_url':SOURCE, 'target_version':'Designer 2026.1.4.126 (pinned project reference; current online catalog may differ)',
        'source_retrieved_at_utc':datetime.now(timezone.utc).isoformat(),
        'source_sha256':hashlib.sha256(raw).hexdigest(), 'retrieval_mode':mode,
        'seed_row_count':len(seed_rows()), 'official_row_count':len(official),
        'merged_row_count':len(rows), 'status':'official-page-parsed',
        'notes':'Official current page is rolling. Inspect any differences before treating as pinned version.'
    })
    print(f'Loaded {len(official)} official entries; merged {len(rows)} rows; {sum(bool(r["xml_tool_name"]) for r in rows)} with XML names')

if __name__ == '__main__':
    main()
