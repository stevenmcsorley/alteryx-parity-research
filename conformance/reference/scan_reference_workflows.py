#!/usr/bin/env python3
"""Structural scan of workflow XML shipped with an authorised reference install.

AUTHORISATION: run only under the scope recorded in docs/AUTHORISATION.md.

Reads ``.yxmd``/``.yxmc``/``.yxwz`` XML documents (sample workflows and bundled
macros) and records, per tool identifier, ONLY structural facts:

* how many nodes use it;
* names of input/output connection anchors observed on connections;
* configuration element tag names (two levels deep) -- never their values;
* document format versions seen.

No sample data, text, expressions, credentials or file contents are written.
Binaries are never opened. The output is a committed, reviewable JSON file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data' / 'reference-observations.json'
EXCLUDE_DIRS = {'Tokens', 'EBWebView', 'AlteryxOne', 'Settings'}  # never scan credential/profile areas


def file_version(exe: Path) -> str | None:
    if os.name != 'nt' or not exe.is_file():
        return None
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command',
                              f"(Get-Item -LiteralPath '{exe}').VersionInfo.ProductVersion"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def tool_identity(node) -> tuple[str | None, str]:
    gs = node.find('GuiSettings')
    plugin = gs.get('Plugin') if gs is not None else None
    es = node.find('EngineSettings')
    macro = es.get('Macro') if es is not None else None
    if macro:
        return macro.replace('/', '\\'), 'macro'
    return plugin, 'plugin'


def config_shape(node) -> list[str]:
    cfg = node.find('Properties/Configuration')
    if cfg is None:
        return []
    tags = set()
    for child in cfg:
        tags.add(child.tag)
        for sub in child:
            tags.add(f'{child.tag}/{sub.tag}')
    return sorted(tags)


def iter_documents(install: Path, langs: list[str]):
    samples = install / 'Samples'
    for lang in langs:
        base = samples / lang
        if base.is_dir():
            yield from (('sample', p) for p in sorted(base.rglob('*')) if p.suffix.lower() in ('.yxmd', '.yxmc', '.yxwz'))
    for sub in ('bin/RuntimeData/Macros', 'bin/Macros'):
        base = install / sub
        if base.is_dir():
            yield from (('bundled-macro', p) for p in sorted(base.rglob('*.yxmc')))


def scan(install: Path, langs: list[str]):
    tools = defaultdict(lambda: {'nodes': 0, 'documents': set(), 'kind': None, 'input_anchors': set(),
                                 'output_anchors': set(), 'config_elements': set(), 'doc_versions': set()})
    stats = {'documents': 0, 'parse_errors': 0, 'by_source': defaultdict(int)}
    for source, path in iter_documents(install, langs):
        if EXCLUDE_DIRS & set(path.relative_to(install).parts):
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            stats['parse_errors'] += 1
            continue
        stats['documents'] += 1
        stats['by_source'][source] += 1
        doc_id = hashlib.sha256(str(path.relative_to(install)).encode()).hexdigest()[:12]
        ver = root.get('yxmdVer')
        ids = {}
        for node in root.iter('Node'):
            tid, kind = tool_identity(node)
            if not tid:
                continue
            ids[node.get('ToolID')] = tid
            t = tools[tid]
            t['nodes'] += 1
            t['kind'] = kind
            t['documents'].add(doc_id)
            t['config_elements'].update(config_shape(node))
            if ver:
                t['doc_versions'].add(ver)
        for conn in root.iter('Connection'):
            o, d = conn.find('Origin'), conn.find('Destination')
            if o is not None and o.get('ToolID') in ids and o.get('Connection'):
                tools[ids[o.get('ToolID')]]['output_anchors'].add(o.get('Connection'))
            if d is not None and d.get('ToolID') in ids and d.get('Connection'):
                tools[ids[d.get('ToolID')]]['input_anchors'].add(d.get('Connection'))
    return tools, stats


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--install', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '')) / 'Alteryx')
    p.add_argument('--langs', nargs='+', default=['en'])
    args = p.parse_args()
    if not (args.install / 'bin').is_dir():
        sys.exit(f'No reference install at {args.install}')
    tools, stats = scan(args.install, args.langs)
    catalog = json.loads((ROOT / 'data' / 'catalog.json').read_text(encoding='utf-8'))['tools']
    by_xml = defaultdict(list)
    for e in catalog:
        by_xml[e['xml_tool_name'].replace('/', '\\').lower()].append(e['id'])
        by_xml[e['xml_tool_name'].replace('/', '\\').split('\\')[-1].lower()].append(e['id'])
    records = []
    for tid in sorted(tools):
        t = tools[tid]
        key = tid.lower()
        matches = sorted(set(by_xml.get(key, []) or by_xml.get(key.split('\\')[-1], [])))
        records.append({'tool_identifier': tid, 'kind': t['kind'], 'catalog_ids': matches, 'nodes': t['nodes'],
                        'documents': len(t['documents']),
                        'input_anchors': sorted(t['input_anchors']), 'output_anchors': sorted(t['output_anchors']),
                        'config_elements': sorted(t['config_elements']), 'doc_versions': sorted(t['doc_versions'])})
    covered = sorted({cid for r in records for cid in r['catalog_ids']})
    doc = {
        'notes': 'Structural facts observed in workflow XML of an authorised reference installation: tool identifiers, '
                 'connection anchor names, configuration element NAMES only. No values, data, text or binaries. '
                 'Observed anchors are those used in shipped workflows and may be incomplete.',
        'authorisation': 'docs/AUTHORISATION.md',
        'reference_build': file_version(args.install / 'bin' / 'AlteryxGui.exe'),
        'scanned_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'sample_languages': args.langs,
        'documents_scanned': stats['documents'], 'documents_by_source': dict(stats['by_source']),
        'parse_errors': stats['parse_errors'],
        'tool_identifiers_observed': len(records),
        'catalog_entries_observed': len(covered),
        'catalog_entries_not_observed': sorted({e['id'] for e in catalog} - set(covered)),
        'tools': records,
    }
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f"Scanned {stats['documents']} documents; {len(records)} tool identifiers; "
          f"{len(covered)}/{len(catalog)} catalogue entries observed; build {doc['reference_build']}")


if __name__ == '__main__':
    main()
