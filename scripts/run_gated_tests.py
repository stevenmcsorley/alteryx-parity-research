#!/usr/bin/env python3
"""Run the runtime-gated tests of one CI job; fail if any fails, errors or is skipped.

    python scripts/run_gated_tests.py r-runtime

The list lives in RUNTIME_GATED_EVIDENCE (scripts/build_status.py); the ledger counts these tests as
evidence only because this job refuses to pass when the runtime is missing.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'scripts')]
from build_status import RUNTIME_GATED_EVIDENCE  # noqa: E402


def main(job: str) -> int:
    ids = sorted(t for t, info in RUNTIME_GATED_EVIDENCE.items() if info['job'] == job)
    if not ids:
        print(f'No runtime-gated tests are declared for job {job!r}')
        return 2
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(ids))
    if result.skipped:
        for test, reason in result.skipped:
            print(f'SKIPPED (not allowed in a gated job): {test.id()}: {reason}')
    ok = result.wasSuccessful() and not result.skipped and result.testsRun == len(ids)
    print(f'{job}: {result.testsRun}/{len(ids)} gated tests ran; ' + ('passed' if ok else 'FAILED'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
