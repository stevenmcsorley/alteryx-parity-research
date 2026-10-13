#!/usr/bin/env python3
"""Tool-by-tool implementation and verification ledger for all catalogue entries.

Derives every status from evidence; nothing is hand-set:

* kernels       - tools registered in flowengine (plugin manifests)
* tests         - the unit/integration suite is executed; each test is mapped to the
                  tools it actually instantiated (recorded at run time)
* reference     - data/reference-observations.json (structural scan, authorised install)
* public docs   - data/public-doc-audit.json (metadata of official help pages)
* conformance   - conformance/results/<build>/results.json (differential runs)

Status rules (docs/TOOL_STATUS_RULES.md):
  implementation_status = implemented  if a kernel is registered AND >=1 test exercises it AND all such tests pass
                        = in-progress  if a kernel is registered but tests are missing or failing
                        = not-started  otherwise
  verification_status   = partial      if >=1 conformance case exists and all pass
                        = failed       if any conformance case fails
                        = not-tested   otherwise
  'verified' is never assigned automatically: it requires the full configuration matrix
  for the tool to be covered and reviewed (see docs/TOOL_STATUS_RULES.md).

Outputs: tool-contracts/<category>/<slug>.contract.json (323), data/tool-status.json,
updated status fields in data/catalog.json/.csv and the brief headers.
Use --check to fail if regenerating would change tracked files (CI).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'conformance'))

from catalog_lib import CATALOG, save_catalog  # noqa: E402

TARGET = 'Designer 2026.1.4.126 (pinned public catalogue); reference observations from installed build 2026.2.0.8'

CATEGORY_DEPENDENCIES = {
    'Predictive': ['r-runtime (statistical models; vendor ships R-based macros)'],
    'AB Testing': ['r-runtime'], 'Time Series': ['r-runtime'], 'Predictive Grouping': ['r-runtime'],
    'Prescriptive': ['r-runtime', 'optimisation-solver'],
    'Connectors': ['external-service-account (credentials/API access)'],
    'Address': ['licensed-reference-dataset (address/geocoding data)'],
    'Demographic Analysis': ['licensed-reference-dataset (demographic data)'],
    'Behavior Analysis (Deprecated)': ['licensed-reference-dataset (behaviour clusters)', 'deprecated-in-reference'],
    'Calgary': ['licensed-reference-dataset (indexed Calgary data)'],
    'In-Database': ['database-connection (SQL pushdown target)'],
    'Machine Learning': ['python-ml-runtime'], 'Text Mining': ['python-nlp-runtime'],
    'Computer Vision': ['python-cv-runtime (image/OCR)'], 'AI Tools': ['llm-service (model provider credentials)'],
    'Connect': ['vendor-cloud-service'], 'Spatial': ['spatial-geometry-library'],
    'Reporting': ['report-renderer (layout/PDF/image)'], 'Interface': ['analytic-app-and-macro-runtime'],
    'Laboratory': ['experimental-in-reference'], 'SDK Examples': ['sdk-runtime'],
}
BLOCKING = ('licensed-reference-dataset', 'external-service-account', 'vendor-cloud-service', 'llm-service')


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


# --------------------------------------------------------------------------- tests

def run_tests_with_tool_map():
    """Run the suite; return {test_id: {'ok': bool, 'tools': set(tool_id)}}."""
    import flowengine.plugin as plugin
    current = {'test': None}
    used = defaultdict(set)
    original_get = plugin.Registry.get

    def recording_get(self, tool_id):
        tool = original_get(self, tool_id)
        if current['test']:
            used[current['test']].add(tool.manifest.tool_id)
        return tool
    plugin.Registry.get = recording_get

    class Recorder(unittest.TextTestResult):
        def startTest(self, test):
            current['test'] = test.id()
            super().startTest(test)

    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT / 'tests'))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, resultclass=Recorder, verbosity=0).run(suite)
    plugin.Registry.get = original_get
    bad = {t.id() for t, _ in result.failures + result.errors}
    tests = {}
    for tid in set(used) | bad:
        tests[tid] = {'ok': tid not in bad, 'tools': used.get(tid, set())}
    return tests, result.testsRun, len(bad)


# --------------------------------------------------------------------------- build

def anchor_names_from_cases(cases):
    """flowengine anchor -> reference anchor names, per catalogue id (from conformance cases)."""
    m = defaultdict(dict)
    for c in cases:
        m[c['catalog_id']].update(c['outputs'])
        m[c['catalog_id']].update(c['reference']['input_anchors'])
    return m


def build(check=False):
    from flowengine.plugin import default_registry
    try:
        from cases import CASES
    except ImportError:
        CASES = []
    catalog_doc = json.loads(CATALOG.read_text(encoding='utf-8'))
    rows = catalog_doc['tools']
    registry = default_registry()
    impl = {}
    for t in registry.tools():
        for cid in t.manifest.catalog_ids:
            impl[cid] = t
    tests, ran, failed = run_tests_with_tool_map()
    tests_by_tool = defaultdict(list)
    for tid, info in sorted(tests.items()):
        for tool in info['tools']:
            tests_by_tool[tool].append((tid, info['ok']))
    obs = load_json(ROOT / 'data' / 'reference-observations.json', {'tools': []})
    obs_by_cid = defaultdict(list)
    for r in obs['tools']:
        for cid in r['catalog_ids']:
            obs_by_cid[cid].append(r)
    audit = load_json(ROOT / 'data' / 'public-doc-audit.json', {'pages': []})
    audit_by_url = {p['url']: p for p in audit['pages']}
    results_files = sorted((ROOT / 'conformance' / 'results').glob('*/*/results.json'))
    conf = defaultdict(list)       # primary profile (E1, incl. holdout) -> drives verification_status
    conf_other = defaultdict(list)  # other engine modes, recorded per contract
    conf_builds = []
    for f in results_files:
        doc = json.loads(f.read_text(encoding='utf-8'))
        mode = doc.get('engine_mode', 'e1')
        conf_builds.append(f"{doc['reference_build']}/{f.parent.name}")
        for r in doc['results']:
            rec = {**r, 'build': doc['reference_build'], 'mode': f.parent.name,
                   'file': f.relative_to(ROOT).as_posix()}
            (conf if mode == 'e1' else conf_other)[r['catalog_id']].append(rec)
    ref_anchor_map = anchor_names_from_cases(CASES)

    ledger, changed = [], []
    for row in rows:
        cid = row['id']
        tool = impl.get(cid)
        deps = list(CATEGORY_DEPENDENCIES.get(row['category'], []))
        # blocking is decided by reviewed category rules only, never by unreviewed keyword signals
        blocking = any(d.startswith(BLOCKING) for d in deps)
        page = audit_by_url.get(row.get('documentation_url'))
        if page:
            for sig in page.get('dependency_signals', []):
                if sig in ('r-runtime', 'python-runtime', 'credentials', 'database-driver', 'licensed-dataset',
                           'llm-service', 'alteryx-server', 'vendor-cloud-service') and not any(sig in d for d in deps):
                    deps.append(f'{sig} (public-doc keyword signal; unreviewed)')
        tool_tests = tests_by_tool.get(tool.manifest.tool_id, []) if tool else []
        if tool and tool_tests and all(ok for _, ok in tool_tests):
            impl_status = 'implemented'
        elif tool:
            impl_status = 'in-progress'
        else:
            impl_status = 'not-started'
        cases = conf.get(cid, [])
        if cases and all(c['status'] == 'passed' for c in cases):
            ver_status = 'partial'
        elif cases:
            ver_status = 'failed'
        else:
            ver_status = 'not-tested'
        o = obs_by_cid.get(cid, [])
        ref_obs = None
        if o:
            ref_obs = {'source': 'data/reference-observations.json', 'build': obs.get('reference_build'),
                       'tool_identifiers': sorted({r['tool_identifier'] for r in o}),
                       'input_anchors': sorted({a for r in o for a in r['input_anchors']}),
                       'output_anchors': sorted({a for r in o for a in r['output_anchors']}),
                       'config_elements': sorted({a for r in o for a in r['config_elements']}),
                       'nodes_observed': sum(r['nodes'] for r in o)}
        public = None
        if page:
            public = {'source': 'data/public-doc-audit.json', 'page_title': page.get('page_title'),
                      'sha256': page.get('sha256'), 'retrieved_utc': page.get('retrieved_utc'),
                      'anchors_extracted': page.get('anchors', []), 'anchor_extraction': page.get('anchor_extraction'),
                      'section_headings': [h['text'] for h in page.get('headings', []) if h['level'] in (2, 3)][:25]}
        rmap = ref_anchor_map.get(cid, {})
        if tool:
            m = tool.manifest
            inputs = [{'name': a.name, 'direction': 'input', 'cardinality': 'many' if a.multiple else
                       ('one' if a.required else 'zero-or-one'), 'provenance': 'flowengine-manifest',
                       'reference_name': rmap.get(a.name)} for a in m.inputs]
            outputs = [{'name': a.name, 'direction': 'output', 'cardinality': 'one', 'provenance': 'flowengine-manifest',
                        'reference_name': rmap.get(a.name)} for a in m.outputs]
            config_schema = m.config_schema
            implementation = {'engine': 'flowengine', 'tool_id': m.tool_id, 'version': m.version,
                              'module': type(tool).__module__, 'side_effects': m.side_effects}
            deterministic = m.deterministic
        else:
            src = 'reference-observed' if ref_obs else 'public-doc-extracted'
            ins = ref_obs['input_anchors'] if ref_obs else [a['label'] for a in (public or {}).get('anchors_extracted', [])
                                                            if a['direction_guess'] == 'input']
            outs = ref_obs['output_anchors'] if ref_obs else [a['label'] for a in (public or {}).get('anchors_extracted', [])
                                                              if a['direction_guess'] == 'output']
            inputs = [{'name': a, 'direction': 'input', 'cardinality': 'unknown', 'provenance': src} for a in ins]
            outputs = [{'name': a, 'direction': 'output', 'cardinality': 'unknown', 'provenance': src} for a in outs]
            config_schema = {'status': 'not-yet-specified',
                             'reference_config_elements': ref_obs['config_elements'] if ref_obs else [],
                             'public_doc_sections': (public or {}).get('section_headings', [])}
            implementation = None
            deterministic = None
        unresolved = []
        if not tool:
            unresolved.append('No independent kernel yet; configuration semantics not specified.')
            if not inputs and not outputs:
                unresolved.append('Anchors unknown: not described in public page text and not observed in shipped workflows.')
        if blocking:
            unresolved.append('Exact behaviour needs separately licensed data/services; record authorised sandbox access before testing.')
        if tool and ver_status == 'not-tested':
            unresolved.append('No differential conformance case yet.')
        sources = [{'url': row['source_url'], 'evidence_type': 'public-tool-list', 'accessed_utc': catalog_doc['metadata'].get('source_retrieved_at_utc')}]
        if row.get('documentation_url'):
            sources.append({'url': row['documentation_url'], 'evidence_type': 'public-tool-help-page-metadata',
                            'accessed_utc': (page or {}).get('retrieved_utc')})
        if ref_obs:
            sources.append({'url': 'data/reference-observations.json', 'evidence_type': 'authorised-reference-structural-scan',
                            'accessed_utc': obs.get('scanned_utc')})
        contract = {
            'id': cid, 'target_version': TARGET, 'name': row['name'], 'category': row['category'],
            'xml_tool_name': row.get('xml_tool_name'), 'user_role': row.get('user_role'),
            'documentation_url': row.get('documentation_url'),
            'status': impl_status if impl_status != 'not-started' else ('specified' if False else 'not-started'),
            'implementation_status': impl_status, 'verification_status': ver_status,
            'inputs': inputs, 'outputs': outputs, 'configuration_schema': config_schema,
            'deterministic': deterministic, 'external_dependencies': deps,
            'external_prerequisites_blocking': blocking,
            'license_notes': 'Independent implementation; no vendor code, assets or documentation text copied.',
            'implementation': implementation, 'reference_observation': ref_obs, 'public_doc': public,
            'independent_tests': {'count': len(tool_tests), 'passing': sum(ok for _, ok in tool_tests),
                                  'tests': [t for t, _ in tool_tests]},
            'sources': sources,
            'cases': [{'case_id': c['case_id'], 'status': c['status'],
                       'evidence': [f"{c['file']}#{c['case_id']}", f"reference build {c['build']} ({c['mode']})",
                                    f"config sha256 {c['config_digest']}"]} for c in cases + conf_other.get(cid, [])],
            'unresolved': unresolved,
        }
        path = ROOT / 'tool-contracts' / slug(row['category']) / f"{slug(row['name'])}.contract.json"
        text = json.dumps(contract, indent=2, ensure_ascii=False) + '\n'
        if not path.exists() or path.read_text(encoding='utf-8') != text:
            changed.append(path)
            if not check:
                path.write_text(text, encoding='utf-8')
        brief = path.with_name(f"{slug(row['name'])}.md")
        if brief.exists():
            b = brief.read_text(encoding='utf-8')
            nb = re.sub(r'- Implementation: \*\*[^*]+\*\*', f'- Implementation: **{impl_status}**', b)
            nb = re.sub(r'- Conformance verification: \*\*[^*]+\*\*', f'- Conformance verification: **{ver_status}**', nb)
            old_banner = '> **Research record; NOT a working tool implementation or verified specification.**'
            new_banner = ('> **Research record.** An independent kernel exists and its evidence is in the contract JSON; '
                          'this brief is NOT a verified specification or a parity claim.')
            nb = nb.replace(old_banner, new_banner) if impl_status == 'implemented' else nb.replace(new_banner, old_banner)
            if '- Machine-readable contract:' not in nb:
                nb = nb.replace('- Conformance verification:', f'- Machine-readable contract: `{path.name}`\n- Conformance verification:', 1)
            if nb != b:
                changed.append(brief)
                if not check:
                    brief.write_text(nb, encoding='utf-8')
        if row['implementation_status'] != impl_status or row['verification_status'] != ver_status:
            changed.append(CATALOG)
            row['implementation_status'] = impl_status
            row['verification_status'] = ver_status
        ledger.append({'id': cid, 'name': row['name'], 'category': row['category'],
                       'kernel': implementation['tool_id'] if implementation else None,
                       'implementation_status': impl_status, 'verification_status': ver_status,
                       'independent_tests': contract['independent_tests']['count'],
                       'conformance_cases': len(cases),
                       'conformance_passed': sum(c['status'] == 'passed' for c in cases),
                       'e2_cases': len(conf_other.get(cid, [])),
                       'e2_passed': sum(c['status'] == 'passed' for c in conf_other.get(cid, [])),
                       'reference_observed': bool(ref_obs),
                       'anchors_known': bool(inputs or outputs),
                       'external_prerequisites_blocking': blocking})
    status_doc = {
        'notes': 'Generated by scripts/build_status.py from evidence; see docs/TOOL_STATUS_RULES.md. '
                 '"partial" verification = every recorded differential case passes on the stated reference build; '
                 'it is not a full-parity claim.',
        'test_run': {'tests_run': ran, 'tests_failed': failed},
        'conformance_builds': conf_builds,
        'totals': summarize(ledger), 'tools': ledger}
    status_path = ROOT / 'data' / 'tool-status.json'
    stext = json.dumps(status_doc, indent=2, ensure_ascii=False) + '\n'
    if not status_path.exists() or status_path.read_text(encoding='utf-8') != stext:
        changed.append(status_path)
        if not check:
            status_path.write_text(stext, encoding='utf-8')
    if CATALOG in changed and not check:
        save_catalog(rows, catalog_doc['metadata'])
    return status_doc, sorted({str(p.relative_to(ROOT)) for p in changed})


def summarize(ledger):
    from collections import Counter
    return {
        'catalogue_entries': len(ledger),
        'kernels_registered': sum(1 for l in ledger if l['kernel']),
        'implementation_status': dict(sorted(Counter(l['implementation_status'] for l in ledger).items())),
        'verification_status': dict(sorted(Counter(l['verification_status'] for l in ledger).items())),
        'anchors_known': sum(l['anchors_known'] for l in ledger),
        'reference_observed': sum(l['reference_observed'] for l in ledger),
        'conformance_cases': sum(l['conformance_cases'] for l in ledger),
        'conformance_cases_passed': sum(l['conformance_passed'] for l in ledger),
        'e2_cases': sum(l['e2_cases'] for l in ledger),
        'e2_cases_passed': sum(l['e2_passed'] for l in ledger),
        'external_prerequisites_blocking': sum(l['external_prerequisites_blocking'] for l in ledger),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--check', action='store_true', help='Exit 1 if tracked outputs are stale')
    args = p.parse_args()
    doc, changed = build(check=args.check)
    print(json.dumps(doc['totals'], indent=2))
    print(f"tests: {doc['test_run']['tests_run']} run, {doc['test_run']['tests_failed']} failed")
    if args.check and changed:
        print('STALE:', *changed[:20], sep='\n  ')
        sys.exit(1)
    if doc['test_run']['tests_failed']:
        sys.exit(1)


if __name__ == '__main__':
    main()
