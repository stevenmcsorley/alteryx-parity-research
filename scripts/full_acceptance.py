#!/usr/bin/env python3
"""Run Flowbench's available acceptance suites and emit an evidence report.

The runner never converts missing external tools into a pass. Platform-specific
or entitlement-dependent suites are reported BLOCKED/NOT RUN and make the run
incomplete (exit 2); a runnable failing suite exits 1.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flowengine.runtimes import find_rscript

VALID = {'PASS', 'FAIL', 'SKIP', 'BLOCKED', 'NOT RUN'}


def catalogue_matrix() -> list[dict]:
    data = json.loads((ROOT / 'data' / 'tool-status.json').read_text(encoding='utf-8'))
    rows = data.get('tools', [])
    if len(rows) != 323 or len({x.get('id') for x in rows}) != 323:
        raise ValueError(f"Expected 323 unique catalogue IDs, found {len(rows)}")
    return [{
        'catalogue_id': x['id'], 'name': x['name'], 'category': x['category'],
        'implementation_status': x['implementation_status'],
        'verification_status': x['verification_status'],
        'independent_tests': x.get('independent_tests', 0),
        'reference_cases': x.get('conformance_cases', 0),
        'reference_passed': x.get('conformance_passed', 0),
        'e2_cases': x.get('e2_cases', 0), 'e2_passed': x.get('e2_passed', 0),
        'external_prerequisites_blocking': bool(x.get('external_prerequisites_blocking')),
    } for x in rows]


def _cmd(name: str, command: list[str], cwd: Path = ROOT, timeout: int | None = None) -> dict:
    started = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=cwd, text=True, encoding='utf-8', errors='replace',
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        output = proc.stdout
        m = re.search(r'Ran (\d+) tests?', output)
        if not m:
            m = re.search(r'Tests\s+(\d+)\s+passed', output, re.IGNORECASE)
        if not m:
            m = re.search(r'(\d+) passed \(', output, re.IGNORECASE)
        reference_cases = re.search(r'(\d+)/(\d+) cases passed against reference', output, re.IGNORECASE)
        holdout = re.search(r'(\d+)/(\d+) sample workflows fully match; (\d+)/(\d+) sinks match', output,
                            re.IGNORECASE)
        skipped = re.search(r'skipped[= ]+(\d+)', output, re.IGNORECASE)
        status = 'PASS' if proc.returncode == 0 else 'FAIL'
        if reference_cases and int(reference_cases.group(1)) < int(reference_cases.group(2)):
            status = 'FAIL'
        if holdout and (int(holdout.group(1)) < int(holdout.group(2)) or
                        int(holdout.group(3)) < int(holdout.group(4))):
            status = 'FAIL'
        result = {'name': name, 'status': status,
                'return_code': proc.returncode, 'duration_seconds': round(time.monotonic() - started, 3),
                'test_count': int(m.group(1)) if m else None,
                'skipped_count': int(skipped.group(1)) if skipped else 0,
                'command': command, 'output_tail': output[-12000:]}
        if reference_cases:
            result['reference_cases_passed'], result['reference_cases_run'] = map(int, reference_cases.groups())
        if holdout:
            result['workflows_passed'], result['workflows_run'], result['sinks_passed'], result['sinks_run'] = map(
                int, holdout.groups())
        return result
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ''
        if isinstance(out, bytes):
            out = out.decode(errors='replace')
        return {'name': name, 'status': 'FAIL', 'return_code': None,
                'duration_seconds': round(time.monotonic() - started, 3), 'test_count': None,
                'skipped_count': 0,
                'command': command, 'output_tail': (str(out) + f'\nTimed out after {timeout}s')[-12000:]}
    except OSError as e:
        return {'name': name, 'status': 'BLOCKED', 'return_code': None,
                'duration_seconds': round(time.monotonic() - started, 3), 'test_count': None,
                'skipped_count': 0,
                'command': command, 'output_tail': f'Could not start suite: {e}'}


def _state(name: str, status: str, reason: str) -> dict:
    assert status in VALID
    return {'name': name, 'status': status, 'return_code': None, 'duration_seconds': 0,
            'test_count': None, 'skipped_count': 0, 'command': [], 'output_tail': reason}


def _database_suite() -> list[dict]:
    docker = shutil.which('docker')
    if not docker:
        return [_state('database-integration', 'BLOCKED', 'Docker CLI is unavailable')]
    if not shutil.which('openssl'):
        return [_state('database-integration', 'BLOCKED',
                       'OpenSSL CLI is required by tests/integration/db_env.py to create the PostgreSQL TLS fixture')]
    up = _cmd('database-fixtures-up', [sys.executable, 'tests/integration/db_env.py', 'up'], timeout=900)
    if up['status'] != 'PASS':
        down = _cmd('database-fixtures-down', [sys.executable, 'tests/integration/db_env.py', 'down'], timeout=300)
        return [up, _state('database-integration', 'BLOCKED', 'Database fixtures did not start'), down]
    try:
        result = _cmd('database-integration', [sys.executable, '-m', 'unittest', 'discover', '-s',
                      'tests/integration', '-p', 'test_database_integration.py', '-v'], timeout=1800)
    finally:
        down = _cmd('database-fixtures-down', [sys.executable, 'tests/integration/db_env.py', 'down'], timeout=300)
    return [up, result, down]


def run_suites(include_installer: bool = False, include_performance: bool = False) -> list[dict]:
    runs = [
        _cmd('repository-integrity', [sys.executable, 'scripts/validate_repository.py'], timeout=300),
        _cmd('status-ledger', [sys.executable, 'scripts/build_status.py', '--check'], timeout=900),
        _cmd('parity-roadmap', [sys.executable, 'scripts/build_parity_roadmap.py', '--check'], timeout=300),
        _cmd('python-engine-and-import-macro-security', [sys.executable, '-m', 'unittest', 'discover',
             '-s', 'tests', '-v'], timeout=1800),
    ]
    npm = shutil.which('npm')
    if npm:
        runs.extend([
            _cmd('desktop-typescript-build', [npm, 'run', 'build'], cwd=ROOT / 'desktop', timeout=900),
            _cmd('desktop-unit-tests', [npm, 'test'], cwd=ROOT / 'desktop', timeout=900),
            _cmd('desktop-playwright', [npm, 'run', 'e2e'], cwd=ROOT / 'desktop', timeout=1800),
        ])
    else:
        runs.extend([_state('desktop-typescript-build', 'BLOCKED', 'Node.js/npm is unavailable'),
                     _state('desktop-unit-tests', 'BLOCKED', 'Node.js/npm is unavailable'),
                     _state('desktop-playwright', 'BLOCKED', 'Node.js/npm is unavailable')])
    runs.extend(_database_suite())

    # Use the same discovery logic as the engine. A managed per-user CRAN
    # install is valid even when Rscript.exe is intentionally absent from PATH.
    rscript = find_rscript()
    if rscript:
        runs.append(_cmd('r-tool-runtime', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                        '-p', 'test_fe_tools_wave2.py', '-k', 'test_r_tool', '-v'], timeout=900))
    else:
        runs.append(_state('r-tool-runtime', 'BLOCKED', 'Pinned external R runtime is not installed'))

    runs.append(_cmd('statistical-and-ml-engine-tests', [sys.executable, '-m', 'unittest', 'discover',
                 '-s', 'tests', '-p', 'test_fe_tools_wave2.py', '-v'], timeout=1200))
    for name, pattern, reason in (
        ('spatial-tool-tests', 'test_*spatial*.py', 'No spatial implementation test module exists yet'),
        ('connector-contract-tests', 'test_*connector*.py', 'No connector contract test module exists yet'),
    ):
        matches = list((ROOT / 'tests').rglob(pattern))
        if matches:
            for test_file in matches:
                runs.append(_cmd(f'{name}:{test_file.name}', [sys.executable, '-m', 'unittest', 'discover',
                             '-s', str(test_file.parent.relative_to(ROOT)), '-p', test_file.name, '-v'], timeout=1800))
        else:
            runs.append(_state(name, 'NOT RUN', reason))

    cargo = shutil.which('cargo')
    if cargo:
        runs.append(_cmd('rust-desktop-shell', [cargo, 'test', '--manifest-path',
                     str(ROOT / 'desktop' / 'src-tauri' / 'Cargo.toml')], timeout=1800))
    else:
        runs.append(_state('rust-desktop-shell', 'BLOCKED', 'Rust cargo toolchain is unavailable'))

    ref = Path(os.environ.get('FLOWBENCH_REFERENCE_ENGINE',
                 os.environ.get('LOCALAPPDATA', '') + r'\Alteryx\bin\AlteryxEngineCmd.exe'))
    if ref.is_file():
        install = ref.parent.parent
        for mode in ('e1', 'e2'):
            args = [sys.executable, 'conformance/harness.py', '--engine', str(ref), '--mode', mode]
            runs.append(_cmd(f'conformance-{mode}', args, timeout=3600))
            holdout_args = [sys.executable, 'conformance/reference/holdout_samples.py',
                            '--install', str(install), '--mode', mode]
            runs.append(_cmd(f'conformance-{mode}-holdout', holdout_args, timeout=7200))
    else:
        runs.append(_state('reference-conformance-e1-e2-holdout', 'BLOCKED',
                           f'Reference engine not found; set FLOWBENCH_REFERENCE_ENGINE (looked for {ref})'))

    if include_performance:
        (ROOT / 'artifacts').mkdir(parents=True, exist_ok=True)
        runs.append(_cmd('performance-200k', [sys.executable, 'scripts/benchmark.py', '--rows', '200000',
                     '--out', str(ROOT / 'artifacts' / 'acceptance-benchmarks.md')], timeout=3600))
    else:
        runs.append(_state('performance', 'NOT RUN', 'Pass --include-performance to execute the 200k-row benchmark'))

    if include_installer and os.name == 'nt':
        if not os.environ.get('RUNNER_TEMP'):
            runs.append(_state('windows-installer-build-and-smoke', 'BLOCKED',
                               'Installer verification requires an isolated runner with RUNNER_TEMP set'))
        elif not shutil.which('cargo') or not shutil.which('npm'):
            runs.append(_state('windows-installer-build-and-smoke', 'BLOCKED',
                               'NSIS packaging requires Node.js/npm and the Rust cargo toolchain'))
        else:
            build = _cmd('windows-installer-build', ['npm', 'run', 'tauri', '--', 'build'],
                         cwd=ROOT / 'desktop', timeout=3600)
            runs.append(build)
            if build['status'] == 'PASS':
                smoke = _cmd('windows-installer-clean-runner-smoke', ['pwsh', '-NoProfile', '-ExecutionPolicy',
                             'Bypass', '-File', 'desktop/scripts/windows_installer_smoke.ps1'], timeout=3600)
                runs.append(smoke)
            else:
                runs.append(_state('windows-installer-clean-runner-smoke', 'BLOCKED',
                                   'Installer smoke skipped because packaging failed'))
    elif include_installer:
        runs.append(_state('windows-installer-clean-runner-smoke', 'BLOCKED',
                           'Installer smoke test requires a clean Windows runner; use desktop-shell CI'))
    else:
        runs.append(_state('windows-installer-clean-runner-smoke', 'NOT RUN',
                           'Installer changes the host installation state; run on isolated Windows CI with --include-installer'))
    return runs


def render_markdown(report: dict) -> str:
    counts = report['summary']
    out = [f"# Flowbench full acceptance — {report['run_id']}", '',
           f"Started: {report['started_utc']}",
           f"Catalogue: {report['catalogue_baseline']['entries']} entries / "
           f"{report['catalogue_baseline']['distinct_names']} distinct names; "
           f"reference E1/E2: {report['reference_builds']}", '',
           '## Suite results', '', '| Suite | Status | Tests | Skipped | Cases/workflows passed | Seconds | Evidence |',
           '|---|---|---:|---:|---:|---:|---|']
    for s in report['suites']:
        comparisons = (f"{s['reference_cases_passed']}/{s['reference_cases_run']} cases" if 'reference_cases_run' in s else
                      f"{s['workflows_passed']}/{s['workflows_run']} workflows; {s['sinks_passed']}/{s['sinks_run']} sinks"
                      if 'workflows_run' in s else '—')
        evidence = s.get('output_tail', '').splitlines()
        last_evidence = evidence[-1][:180] if evidence else 'No command output'
        out.append(f"| {s['name']} | **{s['status']}** | {s.get('test_count') if s.get('test_count') is not None else '—'} | "
                   f"{s.get('skipped_count', 0)} | {comparisons} | {s.get('duration_seconds', 0):.3f} | "
                   f"{last_evidence} |")
    out.extend(['', f"Totals: {counts}", '', '## Catalogue matrix', '',
                '| ID | Tool | Implementation | Reference | E1 | E2 | Tests | External blocker |',
                '|---|---|---|---|---:|---:|---:|---|'])
    for x in report['catalogue']:
        out.append(f"| `{x['catalogue_id']}` | {x['name']} | {x['implementation_status']} | {x['verification_status']} | "
                   f"{x['reference_passed']}/{x['reference_cases']} | {x['e2_passed']}/{x['e2_cases']} | "
                   f"{x['independent_tests']} | {'yes' if x['external_prerequisites_blocking'] else 'no'} |")
    return '\n'.join(out) + '\n'


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-dir', type=Path, default=ROOT / 'artifacts' / 'full-acceptance')
    p.add_argument('--include-performance', action='store_true')
    p.add_argument('--include-installer', action='store_true')
    p.add_argument('--list', action='store_true', help='Print suites and prerequisites without executing')
    args = p.parse_args()
    if args.list:
        print('repository-integrity, status-ledger, parity-roadmap, Python/security, desktop build/unit/Playwright, '
              'database-integration, R, statistical/ML, spatial, connector contracts, Rust, E1/E2 conformance/holdouts, '
              'performance, Windows installer')
        return 0
    started = datetime.now(timezone.utc)
    runs = run_suites(args.include_installer, args.include_performance)
    try:
        matrix = catalogue_matrix()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        runs.append(_state('catalogue-matrix', 'FAIL', str(e)))
        matrix = []
    statuses = {s: sum(x['status'] == s for x in runs) for s in sorted(VALID)}
    ledger = json.loads((ROOT / 'data' / 'tool-status.json').read_text(encoding='utf-8'))
    report = {
        'schema_version': 1, 'run_id': started.strftime('%Y%m%dT%H%M%SZ'),
        'started_utc': started.isoformat(), 'finished_utc': datetime.now(timezone.utc).isoformat(),
        'host': {'platform': platform.platform(), 'python': sys.version.split()[0]},
        'catalogue_baseline': {'entries': len(matrix), 'distinct_names': len({x['name'] for x in matrix})},
        'reference_builds': ledger.get('conformance_builds', []),
        'summary': statuses, 'suites': runs, 'catalogue': matrix,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / 'full_acceptance.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    (args.out_dir / 'full_acceptance.md').write_text(render_markdown(report), encoding='utf-8')
    for suite in runs:
        print(f"{suite['status']:8} {suite['name']}: {suite.get('test_count') or '—'} tests; "
              f"{suite.get('duration_seconds', 0):.1f}s")
    print(f"Reports: {args.out_dir / 'full_acceptance.json'} and {args.out_dir / 'full_acceptance.md'}")
    print(f"Catalogue: {len(matrix)} entries / {len({x['name'] for x in matrix})} distinct names")
    return 1 if statuses['FAIL'] else (2 if statuses['BLOCKED'] or statuses['NOT RUN'] else 0)


if __name__ == '__main__':
    raise SystemExit(main())
