#!/usr/bin/env python3
"""Black-box differential conformance harness.

AUTHORISATION: reference execution runs only under the scope recorded in
docs/AUTHORISATION.md. Inputs are original synthetic data authored in
conformance/cases.py; only those inputs, our configurations, observed output
values/digests and comparison results are stored.

For each case the harness
1. builds a flowengine workflow (Text Input -> tool) and runs it in-process;
2. builds a reference workflow document (Text Input -> null-sentinel Formula ->
   typing Select -> tool -> one CSV output per compared anchor), runs it with the
   reference command-line engine, and reads the CSV files back;
3. compares header + rendered values with the case's rule and records the
   outcome, digests and the first differences.

Known comparison limits (recorded with every result): CSV cannot distinguish
null from empty string, and does not carry field types.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flowengine.executor import run_workflow  # noqa: E402
from flowengine.types import format_value  # noqa: E402

RESULTS_DIR = ROOT / 'conformance' / 'results'
NULL_SENTINEL = '__FLOWENGINE_NULL__'
FIXED_CLOCK = datetime(2026, 1, 15, 9, 30, 0)

REF_TYPES = {'bool': 'Bool', 'byte': 'Byte', 'int16': 'Int16', 'int32': 'Int32', 'int64': 'Int64',
             'fixed_decimal': 'FixedDecimal', 'float': 'Float', 'double': 'Double', 'string': 'String',
             'wstring': 'WString', 'v_string': 'V_String', 'v_wstring': 'V_WString', 'date': 'Date',
             'time': 'Time', 'datetime': 'DateTime'}
_DEFAULT_SIZES = {'bool': 1, 'byte': 1, 'int16': 2, 'int32': 4, 'int64': 8, 'float': 4, 'double': 8,
                  'date': 10, 'time': 8, 'datetime': 19}


def default_engine() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', '')) / 'Alteryx' / 'bin' / 'AlteryxEngineCmd.exe'


# =========================================================================== reference document

def _node(tool_id, plugin, dll, entry, config_xml, x):
    return (f'<Node ToolID="{tool_id}"><GuiSettings Plugin={quoteattr(plugin)}><Position x="{x}" y="60" />'
            f'</GuiSettings><Properties>{config_xml}</Properties>'
            f'<EngineSettings EngineDll={quoteattr(dll)} EngineDllEntryPoint={quoteattr(entry)} /></Node>')


def _size_attr(f):
    t = f['type']
    if t == 'fixed_decimal':
        return f"{f.get('size', 19)}.{f.get('scale', 6)}"
    return str(f.get('size', _DEFAULT_SIZES.get(t, 254)))


def _input_chain(start_id, inp, x, workdir: Path, tag: str):
    """Text Input (every value prefixed with '~' so no column is auto-typed; nulls as a
    sentinel) -> Formula (strip prefix / sentinel -> Null()) -> Select (types).

    Observed: the reference's delimited-file reader trims unquoted whitespace and
    narrows text to a code page, and its Text Input auto-types numeric-looking
    columns; the prefix keeps every value as authored. Returns (nodes, conns, last_id)."""
    fields = inp['fields']
    data = ''.join('<r>' + ''.join(
        f'<c>{escape(NULL_SENTINEL if v is None else "~" + str(v))}</c>' for v in r) + '</r>' for r in inp['rows'])
    ti = (f'<Configuration><NumRows value="{len(inp["rows"])}" /><Fields>'
          + ''.join(f'<Field name={quoteattr(f["name"])} />' for f in fields)
          + f'</Fields><Data>{data}</Data></Configuration>')
    nodes = [_node(start_id, 'AlteryxBasePluginsGui.TextInput.TextInput', 'AlteryxBasePluginsEngine.dll',
                   'AlteryxTextInput', ti, x)]
    sentinel = json.dumps(NULL_SENTINEL)
    formulas = ''.join(
        '<FormulaField expression=' + quoteattr('IIF([%s] = %s, Null(), Substring([%s], 1))' % (f['name'], sentinel, f['name']))
        + ' field=' + quoteattr(f['name']) + ' size="4096" type="V_WString" />' for f in fields)
    nodes.append(_node(start_id + 1, 'AlteryxBasePluginsGui.Formula.Formula', 'AlteryxBasePluginsEngine.dll',
                       'AlteryxFormula', f'<Configuration><FormulaFields>{formulas}</FormulaFields></Configuration>',
                       x + 80))
    sel = ''.join(f'<SelectField field={quoteattr(f["name"])} selected="True" type="{REF_TYPES[f["type"]]}" '
                  f'size="{_size_attr(f)}" />' for f in fields)
    nodes.append(_node(start_id + 2, 'AlteryxBasePluginsGui.AlteryxSelect.AlteryxSelect', 'AlteryxBasePluginsEngine.dll',
                       'AlteryxSelect', f'<Configuration><OrderChanged value="False" /><CommaDecimal value="False" />'
                                        f'<SelectFields>{sel}<SelectField field="*Unknown" selected="False" />'
                                        f'</SelectFields></Configuration>', x + 160))
    conns = [(start_id, 'Output', start_id + 1, 'Input'), (start_id + 1, 'Output', start_id + 2, 'Input')]
    return nodes, conns, start_id + 2


def build_reference_document(case: dict, outdir: Path, mode: str = 'e1') -> tuple[str, dict]:
    ref = case['reference']
    nodes, conns = [], []
    next_id = 1
    subject = 900
    for anchor, inp in case['inputs'].items():
        specs = inp if isinstance(inp, list) else [inp]
        for k, spec in enumerate(specs):
            n, c, last = _input_chain(next_id, spec, 40, outdir, f'{case["case_id"]}__in_{anchor}_{k}')
            nodes += n
            conns += c
            # multi-connection anchors carry ordered connection names "#1", "#2", ... (observed format)
            conns.append((last, 'Output', subject, ref['input_anchors'][anchor], f'#{k + 1}' if len(specs) > 1 else None))
            next_id += 10
    nodes.append(_node(subject, ref['plugin'], ref['engine_dll'], ref['entry_point'], ref['configuration'], 400))
    outputs = {}
    for k, (ours, theirs) in enumerate(sorted(case['outputs'].items())):
        path = outdir / f'{case["case_id"]}__{ours}.csv'
        outputs[ours] = path
        cfg = (f'<Configuration><File MaxRecords="" FileFormat="0">{escape(str(path))}</File><Passwords />'
               '<FormatSpecificOptions><LineEndStyle>LF</LineEndStyle><Delimeter>,</Delimeter>'
               '<ForceQuotes>False</ForceQuotes><HeaderRow>True</HeaderRow><CodePage>65001</CodePage>'
               '<WriteBOM>False</WriteBOM></FormatSpecificOptions><MultiFile value="False" /></Configuration>')
        oid = 950 + k
        nodes.append(_node(oid, 'AlteryxBasePluginsGui.DbFileOutput.DbFileOutput', 'AlteryxBasePluginsEngine.dll',
                           'AlteryxDbFileOutput', cfg, 600))
        conns.append((subject, theirs, oid, 'Input'))
    conn_xml = ''.join(f'<Connection{f" name={quoteattr(rest[0])}" if rest and rest[0] else ""}><Origin ToolID="{a}" '
                       f'Connection="{b}" /><Destination ToolID="{c}" Connection="{d}" /></Connection>'
                       for a, b, c, d, *rest in conns)
    doc = ('<?xml version="1.0"?>\n<AlteryxDocument yxmdVer="2026.1"><Nodes>' + ''.join(nodes) + '</Nodes>'
           f'<Connections>{conn_xml}</Connections><Properties><Memory default="True" />'
           '<GlobalRecordLimit value="0" /><TempFiles default="True" />'
           f'<RunWithE2 value="{mode == "e2"}" />'
           '</Properties></AlteryxDocument>\n')
    return doc, outputs


def run_reference(case: dict, engine: Path, workdir: Path, mode: str = 'e1') -> dict:
    doc, outputs = build_reference_document(case, workdir, mode)
    wf = workdir / f'{case["case_id"]}.yxmd'
    wf.write_text(doc, encoding='utf-8')
    for p in outputs.values():
        if p.exists():
            p.unlink()
    proc = subprocess.run([str(engine), str(wf)], capture_output=True, text=True, timeout=300,
                          encoding='utf-8', errors='replace')
    log = [l for l in proc.stdout.splitlines() if l.strip()]
    errors = [l for l in log if re.search(r'\bError\b', l)]
    warnings = [re.sub(re.escape(str(workdir)), '<work>', l) for l in log if re.search(r'\bWarning\b', l)]
    result = {'exit_code': proc.returncode, 'errors': errors[:10], 'warnings': warnings[:10],
              'messages': [re.sub(re.escape(str(workdir)), '<work>', l) for l in log if l.startswith('ToolId 900')][:10],
              'outputs': {}}
    for anchor, path in outputs.items():
        if path.exists():
            with path.open(encoding='utf-8', newline='') as fh:
                result['outputs'][anchor] = list(csv.reader(fh))
        else:
            result['outputs'][anchor] = None
    return result


# =========================================================================== flowengine side

def run_ours(case: dict, profile: str | None = None) -> dict:
    fe = case['flowengine']
    nodes, edges = [], []
    for anchor, inp in case['inputs'].items():
        specs = inp if isinstance(inp, list) else [inp]
        for k, spec in enumerate(specs):
            nid = f'in_{anchor}_{k}'
            nodes.append({'id': nid, 'tool': 'core.text_input',
                          'config': {'fields': spec['fields'], 'rows': spec['rows']}})
            edges.append({'from': {'node': nid, 'anchor': 'output'}, 'to': {'node': 'subject', 'anchor': anchor}})
    nodes.append({'id': 'subject', 'tool': fe['tool'], 'config': fe['config']})
    doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': nodes, 'edges': edges}
    try:
        r = run_workflow(doc, base_dir=ROOT, clock=lambda: FIXED_CLOCK, **({'profile': profile} if profile else {}))
    except Exception as e:  # validation error is a legitimate, comparable outcome
        return {'status': 'invalid', 'error': str(e), 'outputs': {}}
    if not r.ok:
        return {'status': 'failed', 'error': r.error, 'outputs': {}}
    out = {}
    for anchor in case['outputs']:
        t = r.outputs['subject'][anchor]
        dt = r.manifest['profile']['double_text']
        out[anchor] = [t.schema.names] + [['' if v is None else format_value(v, dt) for v in row] for row in t.rows()]
    return {'status': 'succeeded', 'outputs': out}


# =========================================================================== comparison

def _num(s):
    try:
        return float(s)
    except ValueError:
        return None


def compare(ours, theirs, rule: dict) -> list[str]:
    diffs = []
    if theirs is None:
        return ['reference produced no output file']
    if ours[0] != theirs[0]:
        diffs.append(f'header: ours={ours[0]} ref={theirs[0]}')
        return diffs
    a, b = ours[1:], theirs[1:]
    if not rule.get('ordered', True):
        a, b = sorted(a), sorted(b)
    if len(a) != len(b):
        diffs.append(f'row count: ours={len(a)} ref={len(b)}')
    tol = rule.get('tolerance')
    for i, (ra, rb) in enumerate(zip(a, b)):
        for c, (x, y) in enumerate(zip(ra, rb)):
            if x == y:
                continue
            if tol is not None and _num(x) is not None and _num(y) is not None and \
                    math.isclose(_num(x), _num(y), rel_tol=tol, abs_tol=tol):
                continue
            diffs.append(f'row {i + 1} field {ours[0][c]!r}: ours={x!r} ref={y!r}')
            if len(diffs) >= 12:
                return diffs
    return diffs


def digest(rows) -> str | None:
    if rows is None:
        return None
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def case_digest(case) -> str:
    keep = {k: case[k] for k in ('inputs', 'flowengine', 'reference', 'outputs', 'compare', 'expect')
            if k in case}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def evaluate(case, ours, ref, mode='e1') -> tuple[str, list[str]]:
    expect = case.get('expect', 'match')
    if expect == 'both-reject':
        ref_rejected = ref['exit_code'] >= 2 or bool(ref['errors'])
        ok = ours['status'] in ('invalid', 'failed') and ref_rejected
        return ('passed' if ok else 'failed'), ([] if ok else [f"ours={ours['status']} ref_exit={ref['exit_code']} "
                                                                 f"ref_errors={ref['errors'][:2]}"])
    if ours['status'] != 'succeeded':
        return 'failed', [f"flowengine {ours['status']}: {ours.get('error')}"]
    if ref['exit_code'] >= 2 or ref['errors']:  # exit 1 = completed with warnings
        return 'failed', [f"reference error (exit {ref['exit_code']}): {ref['errors'][:3]}"]
    from flowengine.profiles import get_profile
    rule = dict(case.get('compare', {}))
    unspecified = get_profile(f'ayx-2026.2-{mode}').order_unspecified
    grouped = bool(case['flowengine']['config'].get('group_by'))
    if case['catalog_id'] in unspecified or (grouped and f"{case['catalog_id']}[grouped]" in unspecified):
        rule['ordered'] = False  # order not defined by the reference under this profile; values still exact
    diffs = []
    for anchor in case['outputs']:
        for d in compare(ours['outputs'][anchor], ref['outputs'].get(anchor), rule):
            diffs.append(f'[{anchor}] {d}')
    return ('passed' if not diffs else 'failed'), diffs


def reference_build(engine: Path) -> str:
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command',
                              f"(Get-Item -LiteralPath '{engine}').VersionInfo.ProductVersion"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return out or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def main():
    if '--holdout' in sys.argv:
        from holdout_cases import CASES
    else:
        from cases import CASES
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--engine', type=Path, default=default_engine())
    p.add_argument('--only', nargs='*', help='case_id prefixes to run')
    p.add_argument('--mode', choices=['e1', 'e2'], default='e1', help='Reference engine: E1 original or E2 (AMP)')
    p.add_argument('--holdout', action='store_true', help='Run holdout cases (conformance/holdout_cases.py) instead')
    p.add_argument('--keep-workdir', type=Path, help='Keep generated reference documents here (outside Git)')
    args = p.parse_args()
    if not args.engine.is_file():
        sys.exit(f'Reference engine not found: {args.engine} (conformance status stays not-tested)')
    build = reference_build(args.engine)
    cases = [c for c in CASES if not args.only or any(c['case_id'].startswith(o) for o in args.only)]
    ids = [c['case_id'] for c in CASES]
    assert len(ids) == len(set(ids)), 'duplicate case ids'
    workdir_ctx = tempfile.TemporaryDirectory() if not args.keep_workdir else None
    workdir = Path(workdir_ctx.name) if workdir_ctx else args.keep_workdir
    workdir.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        ours = run_ours(case, profile=f'ayx-2026.2-{args.mode}')
        ref = run_reference(case, args.engine, workdir, args.mode)
        status, diffs = evaluate(case, ours, ref, args.mode)
        results.append({
            'case_id': case['case_id'], 'catalog_id': case['catalog_id'], 'description': case['description'],
            'status': status, 'differences': diffs, 'config_digest': case_digest(case),
            'comparison_rule': {'expect': case.get('expect', 'match'), **case.get('compare', {'ordered': True})},
            'ours': {'status': ours['status'], 'digests': {a: digest(v) for a, v in ours['outputs'].items()}},
            'reference': {'exit_code': ref['exit_code'], 'errors': ref['errors'], 'warnings': ref['warnings'],
                          'messages': ref['messages'],
                          'digests': {a: digest(v) for a, v in ref['outputs'].items()},
                          'observed': ref['outputs'] if status == 'failed' else None},
        })
        print(f"{status.upper():7} {case['case_id']}" + (f"  -> {diffs[0]}" if diffs else ''))
    if workdir_ctx:
        workdir_ctx.cleanup()
    out_dir = RESULTS_DIR / build / (f'{args.mode}-holdout' if args.holdout else args.mode)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / 'results.json'
    previous = json.loads(out.read_text(encoding='utf-8'))['results'] if out.exists() and args.only else []
    merged = {r['case_id']: r for r in previous}
    merged.update({r['case_id']: r for r in results})
    ordered = [merged[i] for i in ids if i in merged]
    summary = {}
    for r in ordered:
        s = summary.setdefault(r['catalog_id'], {'passed': 0, 'failed': 0})
        s[r['status']] += 1
    out.write_text(json.dumps({
        'reference_build': build, 'reference_engine': f'AlteryxEngineCmd ({args.mode.upper()}; RunWithE2={args.mode == "e2"})', 'engine_mode': args.mode,
        'flowengine_profile': f'ayx-2026.2-{args.mode}', 'holdout': args.holdout,
        'authorisation': 'docs/AUTHORISATION.md', 'executed_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'flowengine_clock': FIXED_CLOCK.isoformat(),
        'limits': ['CSV output cannot distinguish null from empty string',
                   'CSV output does not carry field types; types compared only through rendered values',
                   'Reference inputs are typed by a Select from text; casts use canonical literals only'],
        'summary': summary, 'results': ordered}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    passed = sum(r['status'] == 'passed' for r in ordered)
    print(f'{passed}/{len(ordered)} cases passed against reference build {build}; wrote {out.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
