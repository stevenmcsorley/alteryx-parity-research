#!/usr/bin/env python3
"""Per-entry test requirement matrix, annotated with the evidence recorded so far."""
import json
from catalog_lib import ROOT

catalog = json.loads((ROOT / 'data/catalog.json').read_text(encoding='utf-8'))['tools']
status_path = ROOT / 'data/tool-status.json'
ledger = {t['id']: t for t in json.loads(status_path.read_text(encoding='utf-8'))['tools']} if status_path.exists() else {}
rows = []
for r in catalog:
    l = ledger.get(r['id'], {})
    rows.append({'tool_id': r['id'], 'category': r['category'], 'name': r['name'], 'xml_tool_name': r.get('xml_tool_name'),
                 'baseline_cases': ['null-empty', 'schema-and-types', 'boundary-values', 'order-and-duplicates',
                                    'error-behaviour', 'serialization', 'performance'],
                 'configuration_cases': [],
                 'oracle_status': 'reference-differential' if l.get('conformance_cases') else 'missing',
                 'implementation_status': r['implementation_status'], 'verification_status': r['verification_status'],
                 'independent_tests': l.get('independent_tests', 0), 'reference_cases': l.get('conformance_cases', 0),
                 'evidence_refs': [f"tool-contracts (contract JSON for {r['id']})"] if l else []})
out = ROOT / 'data/test-matrix.json'
out.write_text(json.dumps({'notes': 'Coverage requirements per entry. Executed evidence counts come from data/tool-status.json; '
                                    'the per-test and per-case detail is in each contract JSON.', 'tools': rows},
                          indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
print(f'Wrote test matrix for {len(rows)} category/tool occurrences')
