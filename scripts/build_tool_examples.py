"""Harvest executable tool examples from the test suite into data/tool-examples.json.

    python scripts/build_tool_examples.py          # regenerate
    python scripts/build_tool_examples.py --check  # fail if the stored file differs

Runs the unit tests with FLOWENGINE_EXAMPLE_LOG set (see flowengine/examples.py) and keeps, per
tool, up to two of the smallest distinct successful examples. tests/test_tool_examples.py replays
every stored example against its kernel.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'tool-examples.json'
PER_TOOL = 2


def harvest() -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / 'examples.jsonl'
        env = dict(os.environ, FLOWENGINE_EXAMPLE_LOG=str(log))
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'],
                       cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]


def size(e: dict) -> tuple:
    """Prefer examples with real input (two or more rows on every anchor), then the shortest configuration."""
    rows = [len(t['rows']) for t in e['inputs'].values()]
    thin = any(n < 2 for n in rows)
    return (thin, len(json.dumps(e['config'])), sum(rows), json.dumps(e, sort_keys=True))


def select(entries: list[dict]) -> dict:
    from flowengine.examples import replay
    from flowengine.plugin import default_registry
    reg = default_registry()
    by_tool: dict[str, list[dict]] = {}
    for e in entries:
        try:
            tool = reg.get(e['tool_id'])
        except Exception:
            continue
        if e['tool_version'] != tool.manifest.version:
            continue
        if not any(t['row_count'] for t in e['outputs'].values()):
            continue  # an empty result shows little
        by_tool.setdefault(e['tool_id'], []).append(e)
    out = {}
    for tool_id in sorted(by_tool):
        chosen, seen_configs = [], set()
        tool = reg.get(tool_id)
        for e in sorted(by_tool[tool_id], key=size):
            key = json.dumps(e['config'], sort_keys=True)
            if key in seen_configs:
                continue
            seen_configs.add(key)
            example = {'config': e['config'], 'inputs': e['inputs'], 'expected_outputs': e['outputs'],
                       'deterministic': tool.manifest.deterministic}
            with tempfile.TemporaryDirectory() as tmp:
                if replay(tool_id, example, tmp) is not None:
                    continue  # keep only examples that reproduce from their stored inputs
            chosen.append(example)
            if len(chosen) == PER_TOOL:
                break
        if chosen:
            out[tool_id] = chosen
    return out


def build() -> str:
    sys.path.insert(0, str(ROOT))
    tools = select(harvest())
    doc = {'description': 'Executable examples per tool, harvested from passing tests by scripts/build_tool_examples.py '
                          'and replayed by tests/test_tool_examples.py. Inputs are synthetic test data.',
           'tool_count': len(tools), 'tools': tools}
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + '\n'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    text = build()
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding='utf-8') != text:
            print('STALE: data/tool-examples.json (run scripts/build_tool_examples.py)')
            return 1
        print('tool examples up to date')
        return 0
    OUT.write_text(text, encoding='utf-8')
    print(f'wrote {OUT.relative_to(ROOT)}: {json.loads(text)["tool_count"]} tools')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
