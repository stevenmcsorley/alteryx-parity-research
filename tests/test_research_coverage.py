"""Repository completeness and status-integrity tests (no implied vendor parity)."""
import json
import re
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


class ResearchCoverageTests(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads((ROOT / 'data/catalog.json').read_text(encoding='utf-8'))['tools']

    def test_every_category_occurrence_has_a_brief_and_valid_contract(self):
        schema = json.loads((ROOT / 'schemas/tool-contract.schema.json').read_text(encoding='utf-8'))
        validator = jsonschema.Draft202012Validator(schema)
        paths = []
        for row in self.rows:
            base = ROOT / 'tool-contracts' / slug(row['category'])
            brief = base / (slug(row['name']) + '.md')
            contract = base / (slug(row['name']) + '.contract.json')
            self.assertTrue(brief.is_file(), row['id'])
            self.assertTrue(contract.is_file(), row['id'])
            text = brief.read_text(encoding='utf-8')
            self.assertTrue('NOT a working tool implementation' in text or 'NOT a verified specification' in text, row['id'])
            doc = json.loads(contract.read_text(encoding='utf-8'))
            validator.validate(doc)
            self.assertEqual(doc['id'], row['id'])
            paths.append(contract)
        self.assertEqual(323, len(set(paths)))

    def test_statuses_match_evidence_ledger(self):
        ledger = {t['id']: t for t in json.loads((ROOT / 'data/tool-status.json').read_text(encoding='utf-8'))['tools']}
        self.assertEqual(set(ledger), {r['id'] for r in self.rows})
        for r in self.rows:
            l = ledger[r['id']]
            self.assertEqual(r['implementation_status'], l['implementation_status'], r['id'])
            self.assertEqual(r['verification_status'], l['verification_status'], r['id'])
            if r['implementation_status'] == 'implemented':
                self.assertTrue(l['kernel'] and l['independent_tests'] > 0, r['id'])
            else:
                self.assertIsNone(l['kernel'], r['id'])
            if r['verification_status'] == 'partial':
                self.assertTrue(l['conformance_cases'] and l['conformance_cases'] == l['conformance_passed'], r['id'])

    def test_verified_is_never_claimed_without_review(self):
        self.assertFalse(any(r['verification_status'] == 'verified' for r in self.rows))

    def test_unimplemented_catalogue_tools_fail_clearly(self):
        from flowengine.executor import plan_workflow
        unimplemented = [r for r in self.rows if r['implementation_status'] == 'not-started']
        self.assertTrue(unimplemented)
        for r in unimplemented[:25]:
            doc = {'format': 'flowengine.workflow', 'formatVersion': 1,
                   'nodes': [{'id': 'x', 'tool': r['id'], 'config': {}}], 'edges': []}
            problems = plan_workflow(doc)[1]
            self.assertEqual(problems[0]['code'], 'tool-unavailable', r['id'])


if __name__ == '__main__':
    unittest.main()
