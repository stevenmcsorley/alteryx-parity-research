"""Repository completeness tests: no implied Alteryx behaviour or vendor oracle."""
import json
import re
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class ResearchCoverageTests(unittest.TestCase):
    def test_every_category_occurrence_has_a_brief(self):
        rows=json.loads((ROOT/'data/catalog.json').read_text(encoding='utf-8'))['tools']
        paths=[]
        for row in rows:
            slug=lambda s:re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')
            path=ROOT/'tool-contracts'/slug(row['category'])/(slug(row['name'])+'.md')
            self.assertTrue(path.is_file(),msg=row['id'])
            self.assertIn('NOT a working tool implementation',path.read_text(encoding='utf-8'))
            paths.append(path)
        self.assertEqual(len(paths),len(set(paths)))
        self.assertEqual(323,len(paths))

    def test_no_research_brief_claims_verified(self):
        rows=json.loads((ROOT/'data/catalog.json').read_text(encoding='utf-8'))['tools']
        self.assertTrue(all(r['verification_status']=='not-tested' for r in rows))
        self.assertTrue(all(r['implementation_status']=='not-started' for r in rows))

if __name__=='__main__':unittest.main()
