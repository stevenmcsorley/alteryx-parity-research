#!/usr/bin/env python3
"""Holdout conformance on realistic multi-tool workflows: the reference install's shipped sample
workflows (never used to develop the engine) are run by both engines and every sink is compared.

AUTHORISATION: docs/AUTHORISATION.md. Samples and their data are copied to a temporary mirror and
never committed. Only workflow file names, pass/fail, digests and difference *locations*
(sink, row number, field name) are recorded - no data values from the vendor's samples.

Procedure per sample:
1. import it (flowengine.interop.yxmd); keep only samples that validate cleanly after import;
2. rewrite the document so every Browse and Output Data tool writes a UTF-8 CSV to a run-specific
   folder (reference run and flowengine run use different folders);
3. run the reference engine (E1 or E2) and flowengine (matching profile) and compare the CSVs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from flowengine.executor import plan_workflow, run_workflow  # noqa: E402
from flowengine.interop.yxmd import import_document  # noqa: E402

CSV_OPTS = ('<FormatSpecificOptions><LineEndStyle>LF</LineEndStyle><Delimeter>,</Delimeter><ForceQuotes>False'
            '</ForceQuotes><HeaderRow>True</HeaderRow><CodePage>65001</CodePage><WriteBOM>False</WriteBOM>'
            '</FormatSpecificOptions>')


def rewrite_sinks(xml_text: str, out_dir: Path, mode: str) -> tuple[str, list[str]]:
    root = ET.fromstring(xml_text)
    sinks = []
    for node in root.iter('Node'):
        gs = node.find('GuiSettings')
        plugin = (gs.get('Plugin') if gs is not None else '') or ''
        short = plugin.split('.')[-1]
        if short not in ('BrowseV2', 'DbFileOutput'):
            continue
        sid = f"sink_{node.get('ToolID')}"
        path = out_dir / f'{sid}.csv'
        sinks.append(sid)
        gs.set('Plugin', 'AlteryxBasePluginsGui.DbFileOutput.DbFileOutput')
        props = node.find('Properties')
        cfg = props.find('Configuration')
        if cfg is not None:
            props.remove(cfg)
        cfg = ET.fromstring(f'<Configuration><File MaxRecords="" FileFormat="0">{path}</File><Passwords />{CSV_OPTS}'
                            f'<MultiFile value="False" /></Configuration>')
        props.insert(0, cfg)
        es = node.find('EngineSettings')
        es.attrib.clear()
        es.set('EngineDll', 'AlteryxBasePluginsEngine.dll')
        es.set('EngineDllEntryPoint', 'AlteryxDbFileOutput')
    # attach a CSV sink to every unconnected output anchor of tools the importer understands
    from flowengine.interop.yxmd import CONVERTERS, MACROS
    import re as _re
    connected = {(c.find('Origin').get('ToolID'), c.find('Origin').get('Connection'))
                 for c in root.iter('Connection') if c.find('Origin') is not None}
    nodes_el = root.find('Nodes')
    conns_el = root.find('Connections')
    if conns_el is None:
        conns_el = ET.SubElement(root, 'Connections')
    next_id = max(int(n.get('ToolID')) for n in root.iter('Node')) + 1000
    for node in list(root.iter('Node')):
        gs = node.find('GuiSettings')
        plugin = (gs.get('Plugin') if gs is not None else '') or ''
        es = node.find('EngineSettings')
        macro = (es.get('Macro') if es is not None else '') or ''
        fn = CONVERTERS.get(plugin.split('.')[-1]) if plugin else MACROS.get(_re.split(r'[\\/]', macro)[-1].lower())
        cfg = node.find('Properties/Configuration')
        if not fn or cfg is None or plugin.endswith(('DbFileOutput', 'BrowseV2')):
            continue
        try:
            conv = fn(cfg)
        except Exception:
            continue
        for anchor in conv.outputs:
            if anchor == 'Duplicate' or (node.get('ToolID'), anchor) in connected:
                continue
            sid = f"sink_{node.get('ToolID')}_{anchor}"
            path = out_dir / f'{sid}.csv'
            sinks.append(sid)
            nodes_el.append(ET.fromstring(
                f'<Node ToolID="{next_id}"><GuiSettings Plugin="AlteryxBasePluginsGui.DbFileOutput.DbFileOutput">'
                f'<Position x="0" y="0" /></GuiSettings><Properties><Configuration><File MaxRecords="" FileFormat="0">'
                f'{path}</File><Passwords />{CSV_OPTS}<MultiFile value="False" /></Configuration></Properties>'
                f'<EngineSettings EngineDll="AlteryxBasePluginsEngine.dll" EngineDllEntryPoint="AlteryxDbFileOutput" /></Node>'))
            conns_el.append(ET.fromstring(f'<Connection><Origin ToolID="{node.get("ToolID")}" Connection="{anchor}" />'
                                          f'<Destination ToolID="{next_id}" Connection="Input" /></Connection>'))
            next_id += 1
    props = root.find('Properties')
    if props is not None:
        e2 = props.find('RunWithE2')
        if e2 is None:
            e2 = ET.SubElement(props, 'RunWithE2')
        e2.set('value', 'True' if mode == 'e2' else 'False')
    return ET.tostring(root, encoding='unicode'), sinks


def read_csv(p: Path):
    if not p.is_file():
        return None
    with p.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.reader(f))


def num(s):
    try:
        return float(s)
    except ValueError:
        return None


def compare(a, b, ordered=True):
    """Difference locations only (no values)."""
    if a is None or b is None:
        return ['missing output: ' + ('flowengine' if a is None else 'reference')]
    if a[0] != b[0]:
        return [f'header differs (ours {len(a[0])} fields, reference {len(b[0])} fields; '
                f'names equal={sorted(a[0]) == sorted(b[0])})']
    ra, rb = a[1:], b[1:]
    if not ordered:
        ra, rb = sorted(ra), sorted(rb)
    diffs = []
    if len(ra) != len(rb):
        diffs.append(f'row count ours={len(ra)} reference={len(rb)}')
    for i, (x, y) in enumerate(zip(ra, rb)):
        for j, (u, v) in enumerate(zip(x, y)):
            if u == v:
                continue
            nu, nv = num(u), num(v)
            if nu is not None and nv is not None and math.isclose(nu, nv, rel_tol=1e-12, abs_tol=1e-12):
                continue
            diffs.append(f'row {i + 1} field {a[0][j]!r}')
            if len(diffs) >= 8:
                return diffs
    return diffs


def digest(rows):
    return None if rows is None else hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--install', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '')) / 'Alteryx')
    p.add_argument('--mode', choices=['e1', 'e2'], default='e1')
    p.add_argument('--limit', type=int, default=1000)
    args = p.parse_args()
    engine = args.install / 'bin' / 'AlteryxEngineCmd.exe'
    build = subprocess.run(['powershell', '-NoProfile', '-Command', f"(Get-Item '{engine}').VersionInfo.ProductVersion"],
                           capture_output=True, text=True).stdout.strip()
    results = []
    with tempfile.TemporaryDirectory(prefix='flowbench-holdout-') as tmp:
        mirror = Path(tmp) / 'Samples'
        shutil.copytree(args.install / 'Samples' / 'data', mirror / 'data')
        shutil.copytree(args.install / 'Samples' / 'en', mirror / 'en')
        for k, f in enumerate(sorted((mirror / 'en').rglob('*.yxmd'))[:args.limit]):
            rel = f.relative_to(mirror / 'en').as_posix()
            text = f.read_text(encoding='utf-8-sig')
            try:
                doc, _ = import_document(text)
            except Exception:
                continue
            if plan_workflow(doc, base_dir=f.parent, infer_schemas=False)[1]:
                continue
            ref_dir, our_dir = Path(tmp) / 'ref' / str(k), Path(tmp) / 'ours' / str(k)
            ref_dir.mkdir(parents=True)
            our_dir.mkdir(parents=True)
            ref_xml, sinks = rewrite_sinks(text, ref_dir, args.mode)
            our_xml, _ = rewrite_sinks(text, our_dir, args.mode)
            if not sinks:
                continue
            ref_file = f.with_name(f'{f.stem}.__ref.yxmd')
            ref_file.write_text(ref_xml, encoding='utf-8')
            try:
                proc = subprocess.run([str(engine), str(ref_file)], capture_output=True, text=True, timeout=600,
                                      encoding='utf-8', errors='replace')
                ref_ok = proc.returncode < 2
            except subprocess.TimeoutExpired:
                ref_ok = False
            entry = {'sample': rel, 'sinks': len(sinks), 'reference_exit_ok': ref_ok}
            try:
                our_doc, report = import_document(our_xml)
                r = run_workflow(our_doc, base_dir=f.parent, profile=f'ayx-2026.2-{args.mode}', retain='none')
                entry['flowengine_status'] = r.status
                entry['flowengine_error'] = r.error and {'code': r.error['code'], 'node': r.error['node_id']}
            except Exception as e:
                entry['flowengine_status'] = 'invalid'
                entry['flowengine_error'] = {'code': type(e).__name__, 'node': None}
            per_sink = {}
            for s in sinks:
                a, b = read_csv(our_dir / f'{s}.csv'), read_csv(ref_dir / f'{s}.csv')
                d = compare(a, b, ordered=args.mode == 'e1')
                per_sink[s] = {'status': 'passed' if not d else 'failed', 'differences': d,
                               'rows_reference': None if b is None else len(b) - 1,
                               'digest_ours': digest(a), 'digest_reference': digest(b)}
            entry['sink_results'] = per_sink
            entry['status'] = 'passed' if ref_ok and entry['flowengine_status'] == 'succeeded' and all(
                v['status'] == 'passed' for v in per_sink.values()) else 'failed'
            results.append(entry)
            first = next((f'{s}: {v["differences"][0]}' for s, v in per_sink.items() if v['differences']), '')
            print(f"{entry['status'].upper():7} {rel}  ({sum(v['status'] == 'passed' for v in per_sink.values())}/{len(sinks)} sinks)"
                  f"  {entry['flowengine_status']} {entry['flowengine_error'] or ''} {first}")
    out = ROOT / 'conformance' / 'results' / build / f'{args.mode}-samples' / 'results.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    passed = sum(r['status'] == 'passed' for r in results)
    sinks_total = sum(r['sinks'] for r in results)
    sinks_passed = sum(sum(v['status'] == 'passed' for v in r['sink_results'].values()) for r in results)
    out.write_text(json.dumps({
        'kind': 'holdout: reference sample workflows, imported and run by both engines',
        'reference_build': build, 'engine_mode': args.mode, 'authorisation': 'docs/AUTHORISATION.md',
        'executed_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'limits': ['Only samples that validate cleanly after import are run',
                   'Differences are recorded by location only; vendor data values are never stored',
                   'E2 comparisons are order-insensitive (AMP does not define output order)'],
        'workflows_run': len(results), 'workflows_passed': passed, 'sinks': sinks_total, 'sinks_passed': sinks_passed,
        'results': results}, indent=2) + '\n', encoding='utf-8')
    print(f'{passed}/{len(results)} sample workflows fully match; {sinks_passed}/{sinks_total} sinks match ({args.mode})')


if __name__ == '__main__':
    main()
