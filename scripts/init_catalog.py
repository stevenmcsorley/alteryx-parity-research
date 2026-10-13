#!/usr/bin/env python3
"""Generate honest offline inventory from 323 publicly listed category/name rows."""
from catalog_lib import seed_rows, save_catalog, SOURCE
rows = seed_rows()
save_catalog(rows, {'source_url':SOURCE, 'target_version':'Designer 2026.1.4.126',
                     'status':'seeded-offline', 'seed_row_count':len(rows),
                     'warning':'Names/categories manually transcribed from the official public catalog on 2026-10-13. XML names, roles and per-tool docs NOT imported yet; run sync_official_catalog.py on a connected machine.',
                     'retrieval_mode':'manual-public-catalog-transcription'})
print(f'Wrote {len(rows)} inventory rows; {len(set(r["name"] for r in rows))} distinct names')
