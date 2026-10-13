import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from catalog_lib import seed_rows, parse_official_html, merge_official

class CatalogTests(unittest.TestCase):
    def test_public_names_all_present(self):
        rows=seed_rows()
        self.assertEqual(323,len(rows))
        self.assertEqual(321,len({r['name'] for r in rows}))
        self.assertEqual(29,len({r['category'] for r in rows}))

    def test_no_duplicate_category_ids(self):
        rows=seed_rows()
        self.assertEqual(len(rows),len({r['id'] for r in rows}))

    def test_parser_reads_xml_roles_urls_and_repeated_categories(self):
        html='''<html><body><h2>In/Out</h2><table><tr><th>Tool Name</th><th>XML Tool Name</th><th>User Role</th></tr>
<tr><td><a href="/current/en/designer/tools/in-out-tools/input-data-tool.html">Input Data</a></td>
<td>AlteryxBasePluginsGui.DbFileInput.DbFileInput</td><td>Basic, Full</td></tr></table>
<h2>Connect</h2><table><tr><th>Tool Name</th><th>XML Tool Name</th><th>User Role</th></tr><tr>
<td><a href="/x">Connect Output</a></td><td>ConnectTools.ConnectOutput</td><td>Full</td></tr></table></body></html>'''
        parsed=parse_official_html(html)
        self.assertEqual(2,len(parsed))
        self.assertEqual('AlteryxBasePluginsGui.DbFileInput.DbFileInput',parsed[0]['xml_tool_name'])
        self.assertTrue(parsed[0]['documentation_url'].startswith('https://help.alteryx.com/'))
        merged=merge_official(seed_rows(),parsed)
        self.assertEqual(323,len(merged))
        self.assertEqual(2,sum(r['source_state']=='official-catalog-verified' for r in merged))

    def test_no_fake_tool_implementations(self):
        rows=json.loads((ROOT/'data/catalog.json').read_text())['tools']
        self.assertFalse(any(r['verification_status']=='verified' for r in rows))
        self.assertEqual(323,len(rows))
        self.assertTrue(all(r.get('xml_tool_name') for r in rows))

if __name__=='__main__':unittest.main()
