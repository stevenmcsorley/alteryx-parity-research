import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('full_acceptance', ROOT / 'scripts' / 'full_acceptance.py')
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class FullAcceptanceTests(unittest.TestCase):
    def test_catalogue_matrix_is_complete_without_inflating_status(self):
        rows = acceptance.catalogue_matrix()
        self.assertEqual(len(rows), 323)
        self.assertEqual(len({x['catalogue_id'] for x in rows}), 323)
        self.assertEqual(len({x['name'] for x in rows}), 321)
        action = next(x for x in rows if x['catalogue_id'] == 'interface:action')
        self.assertEqual(action['implementation_status'], 'in-progress')
        self.assertEqual(action['verification_status'], 'not-tested')

    def test_unavailable_suite_is_not_represented_as_success(self):
        blocked = acceptance._state('R', 'BLOCKED', 'not installed')
        not_run = acceptance._state('installer', 'NOT RUN', 'isolated runner required')
        self.assertNotEqual(blocked['status'], 'PASS')
        self.assertNotEqual(not_run['status'], 'PASS')

    def test_suite_runner_records_test_and_skip_counts(self):
        run = acceptance._cmd('count-parser', [__import__('sys').executable, '-c',
                                "print('Ran 4 tests'); print('OK (skipped=1)')"])
        self.assertEqual(run['status'], 'PASS')
        self.assertEqual(run['test_count'], 4)
        self.assertEqual(run['skipped_count'], 1)

    def test_suite_runner_records_differential_and_holdout_counts(self):
        text = '74/75 cases passed against reference build\n3/4 sample workflows fully match; 7/8 sinks match'
        run = acceptance._cmd('evidence-parser', [__import__('sys').executable, '-c',
                                f"print({text!r})"])
        self.assertEqual(run['status'], 'FAIL')
        self.assertEqual((run['reference_cases_passed'], run['reference_cases_run']), (74, 75))
        self.assertEqual((run['workflows_passed'], run['workflows_run'], run['sinks_passed'], run['sinks_run']),
                         (3, 4, 7, 8))

    def test_report_contains_every_catalogue_entry_and_status(self):
        matrix = acceptance.catalogue_matrix()
        report = {'run_id': 'test', 'started_utc': 'now', 'catalogue_baseline': {'entries': 323, 'distinct_names': 321},
                  'reference_builds': ['2026.2.0.8/e1', '2026.2.0.8/e2'],
                  'summary': {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'BLOCKED': 1, 'NOT RUN': 0},
                  'suites': [acceptance._state('R', 'BLOCKED', 'not installed')], 'catalogue': matrix}
        markdown = acceptance.render_markdown(report)
        self.assertIn('323 entries / 321 distinct names', markdown)
        self.assertIn('interface:action', markdown)
        self.assertIn('BLOCKED', markdown)
        self.assertEqual(markdown.count('\n| `'), 323)

    def test_report_handles_empty_command_output(self):
        report = {'run_id': 'test', 'started_utc': 'now', 'catalogue_baseline': {'entries': 323,
                  'distinct_names': 321}, 'reference_builds': [],
                  'summary': {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'BLOCKED': 0, 'NOT RUN': 0},
                  'suites': [{'name': 'empty', 'status': 'PASS', 'test_count': None, 'skipped_count': 0,
                              'duration_seconds': 0, 'output_tail': ''}], 'catalogue': []}
        self.assertIn('No command output', acceptance.render_markdown(report))


if __name__ == '__main__':
    unittest.main()
