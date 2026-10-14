#!/usr/bin/env python3
"""Run the workflow importer over the authorised reference install's shipped workflows and record
aggregate coverage only (counts per tool identifier and status). No workflow content is written.

AUTHORISATION: docs/AUTHORISATION.md (structural use of shipped workflow XML).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from flowengine.executor import plan_workflow  # noqa: E402
from flowengine.interop.yxmd import import_file  # noqa: E402

OUT = ROOT / 'data' / 'import-survey.json'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--install', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '')) / 'Alteryx')
    p.add_argument('--lang', default='en')
    args = p.parse_args()
    files = sorted((args.install / 'Samples' / args.lang).rglob('*.yxmd'))
    if not files:
        sys.exit('No reference samples found')
    status = Counter()
    per_tool = defaultdict(Counter)
    reasons = Counter()
    docs_ok = docs_valid = 0
    for f in files:
        try:
            doc, report = import_file(f)
        except Exception as e:  # malformed or unusual documents are counted, not fatal
            status['document-error'] += 1
            reasons[type(e).__name__] += 1
            continue
        docs_ok += 1
        for n in report['nodes']:
            status[n['status']] += 1
            per_tool[n['source']][n['status']] += 1
            if n['status'] == 'unsupported':
                reasons[n.get('reason', '')[:80]] += 1
        problems = plan_workflow(doc, base_dir=f.parent, infer_schemas=False)[1]
        if not problems:
            docs_valid += 1
    OUT.write_text(json.dumps({
        'notes': 'Aggregate importer coverage over the reference install\'s shipped sample workflows (counts only).',
        'authorisation': 'docs/AUTHORISATION.md', 'surveyed_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'documents': len(files), 'documents_imported': docs_ok,
        'documents_fully_runnable_after_import': docs_valid,
        'node_status': dict(status),
        'top_unsupported_reasons': reasons.most_common(25),
        'per_tool': {k: dict(v) for k, v in sorted(per_tool.items(), key=lambda kv: -sum(kv[1].values()))},
    }, indent=2) + '\n', encoding='utf-8')
    print(f'{docs_ok}/{len(files)} imported; {docs_valid} validate cleanly; node status {dict(status)}')


if __name__ == '__main__':
    main()
