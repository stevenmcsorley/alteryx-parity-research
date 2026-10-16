"""Launch an extracted DEB and verify a real workflow using its bundled runtime.

Requires a desktop display (or xvfb-run). No package installation or user profile changes.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


def smoke(package: Path, evidence: Path):
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='flowbench-deb-smoke-') as temporary:
        root = Path(temporary)
        extracted = root / 'package'
        subprocess.run(['dpkg-deb', '--extract', str(package), str(extracted)], check=True)
        output = root / 'correlation.csv'
        workflow = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
            {'id': 'data', 'tool': 'core.text_input', 'config': {'fields': [
                {'name': 'x', 'type': 'double'}, {'name': 'y', 'type': 'double'}],
                'rows': [[1, 2], [2, 4], [3, 6]]}},
            {'id': 'correlation', 'tool': 'core.pearson_correlation', 'config': {'fields': ['x', 'y']}},
            {'id': 'export', 'tool': 'core.output_data', 'config': {'path': str(output), 'format': 'csv'}}],
            'edges': [
                {'from': {'node': 'data', 'anchor': 'output'}, 'to': {'node': 'correlation', 'anchor': 'input'}},
                {'from': {'node': 'correlation', 'anchor': 'output'}, 'to': {'node': 'export', 'anchor': 'input'}}]}
        workflow_path = root / 'workflow.flow.json'
        workflow_path.write_text(json.dumps(workflow))
        env = dict(os.environ, XDG_DATA_HOME=str(root / 'data'),
                   FLOWBENCH_STARTUP_SMOKE_WORKFLOW=str(workflow_path))
        # Prevent developer overrides from hiding missing bundled resources.
        env.pop('FLOWENGINE_ROOT', None)
        env.pop('FLOWENGINE_PYTHON', None)
        log = root / 'data/org.parityresearch.flowbench/engine-startup.log'
        with (evidence / 'application.log').open('w') as stdout:
            process = subprocess.Popen([str(extracted / 'usr/bin/flowbench')], env=env,
                cwd=root, stdout=stdout, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    startup = log.read_text() if log.exists() else ''
                    if 'engine.info succeeded; catalog.list entries=323' in startup:
                        break
                    if process.poll() is not None or 'startup health check failed' in startup:
                        raise RuntimeError(f'Packaged application failed startup: {startup}')
                    time.sleep(.25)
                else:
                    raise RuntimeError('Packaged application did not become healthy within 60 seconds')
                assert f'python={extracted}/usr/lib/Flowbench/runtime/bin/python3' in startup, startup
                assert 'workflow.run succeeded' in startup, startup
                with output.open(newline='') as stream:
                    actual = list(csv.reader(stream))
                assert actual == [['Field1', 'Field2', 'Correlation'], ['x', 'x', '1'],
                                  ['x', 'y', '1'], ['y', 'x', '1'], ['y', 'y', '1']], actual
                (evidence / 'correlation.csv').write_text(output.read_text())
                print('PASS: packaged Linux app, bundled runtime, 323-entry catalogue, correlation workflow')
            finally:
                if log.exists():
                    (evidence / 'engine-startup.log').write_text(log.read_text())
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('package', type=Path)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    smoke(args.package.resolve(), args.evidence.resolve())
