"""Interface questions and actions added for v1: Map, DCM-style Connection, sourced options (Drop Down,
List Box, Tree from a data file or folder) and formula / Input Data actions."""
import json
import tempfile
import unittest
from pathlib import Path

from flowengine.errors import EngineError
from flowengine.executor import run_workflow
from flowengine.explorer import explore
from flowengine.interface_app import bind_application, describe_application
from flowengine.interop.yxmd import import_file

from helpers import edge, text_input, workflow


def answer(question, value, base_dir=None):
    """The bound answer, as an action writes it into a workflow (a Text Input cell)."""
    doc = app([question], [{'node_id': 't', 'question': question['id'], 'config_path': ['rows', '0', '0']}],
              [text_input('t', [('v', 'v_wstring')], [['placeholder']])])
    return bind_application(doc, {question['id']: value}, base_dir)['nodes'][0]['config']['rows'][0][0]


def app(questions, actions=(), nodes=None, edges=()):
    doc = workflow(nodes or [text_input('t', [('v', 'int32')], [['1']])], list(edges))
    doc['metadata'] = {'application': {'questions': list(questions), 'actions': list(actions), 'errors': []}}
    return doc


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_map_question_takes_a_geojson_geometry(self):
        q = {'id': 'area', 'type': 'map', 'prompt': 'Draw the trade area'}
        point = {'type': 'Point', 'coordinates': [-0.1276, 51.5072]}
        self.assertEqual(json.loads(answer(q, point)), point)
        polygon = '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}'
        self.assertEqual(json.loads(answer(q, polygon))['type'], 'Polygon')
        with self.assertRaises(EngineError):
            answer(q, 'somewhere')

    def test_connection_question_selects_a_saved_connection(self):
        self.assertEqual(answer({'id': 'db', 'type': 'connection', 'kinds': ['database'], 'prompt': 'Warehouse'}, ' conn-123 '), 'conn-123')
        bad = app([{'id': 'db', 'type': 'connection', 'kinds': ['ftp']}])
        with self.assertRaises(EngineError):
            bind_application(bad, {})

    def test_options_come_from_a_data_file_and_follow_its_changes(self):
        (self.dir / 'regions.csv').write_text('region,country\nNorth,UK\nSouth,UK\nNorth,UK\nOuest,FR\n')
        doc = app([{'id': 'r', 'type': 'choice', 'source': {'kind': 'file', 'path': 'regions.csv', 'field': 'region'}},
                   {'id': 't', 'type': 'tree', 'source': {'kind': 'file', 'path': 'regions.csv', 'fields': ['country', 'region']}}])
        q = describe_application(doc, self.dir)['questions']
        self.assertEqual(q[0]['options'], ['North', 'Ouest', 'South'])
        self.assertEqual(q[1]['options'], ['FR > Ouest', 'UK > North', 'UK > South'])
        (self.dir / 'regions.csv').write_text('region,country\nEast,UK\n')
        self.assertEqual(describe_application(doc, self.dir)['questions'][0]['options'], ['East'], 'options are read each time')
        with self.assertRaises(EngineError):
            bind_application(doc, {'r': 'North'}, self.dir)

    def test_tree_can_browse_a_folder(self):
        for rel in ('2025/q1.csv', '2025/q2.csv', '2026/q1.csv', '2026/notes.txt', '.hidden/x.csv'):
            (self.dir / 'reports' / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / 'reports' / rel).write_text('a\n1\n')
        doc = app([{'id': 'f', 'type': 'tree', 'source': {'kind': 'folder', 'path': 'reports', 'extensions': ['csv']}}])
        self.assertEqual(describe_application(doc, self.dir)['questions'][0]['options'],
                         ['2025 > q1.csv', '2025 > q2.csv', '2026 > q1.csv'])
        self.assertEqual(answer(doc['metadata']['application']['questions'][0], '2026 > q1.csv', self.dir), '2026 > q1.csv')


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_formula_action_builds_a_value_from_several_answers(self):
        nodes = [text_input('t', [('sales', 'int32')], [['5'], ['50'], ['500']]),
                 {'id': 'f', 'tool': 'core.filter', 'config': {'expression': '[sales] > 0'}}]
        doc = app([{'id': 'min', 'type': 'number'}, {'id': 'max', 'type': 'number'}],
                  [{'kind': 'formula', 'node_id': 'f', 'config_path': ['expression'],
                    'expression': '"[sales] >= " + ToString([min]) + " AND [sales] <= " + ToString([max])'}],
                  nodes, [edge('t', 'output', 'f', 'input')])
        bound = bind_application(doc, {'min': 10, 'max': 100})
        self.assertEqual(bound['nodes'][1]['config']['expression'], '[sales] >= 10 AND [sales] <= 100')
        r = run_workflow(bound, base_dir=self.dir)
        self.assertEqual(list(r.outputs['f']['true'].rows()), [(50,)])

    def test_input_action_points_input_data_at_the_chosen_file(self):
        (self.dir / 'jan.csv').write_text('k\n1\n2\n')
        (self.dir / 'feb.csv').write_text('k\n1\n2\n3\n')
        nodes = [{'id': 'in', 'tool': 'core.input_data', 'config': {'path': 'jan.csv'}}]
        doc = app([{'id': 'month', 'type': 'file', 'extensions': ['csv']}], [{'kind': 'input', 'node_id': 'in', 'question': 'month'}], nodes)
        bound = bind_application(doc, {'month': 'feb.csv'}, self.dir)
        self.assertEqual(len(run_workflow(bound, base_dir=self.dir).outputs['in']['output']), 3)
        wrong = app([{'id': 'month', 'type': 'file'}], [{'kind': 'input', 'node_id': 't', 'question': 'month'}])
        with self.assertRaises(EngineError):
            bind_application(wrong, {'month': 'feb.csv'}, self.dir)
        self.assertNotIn('config_path', doc['metadata']['application']['actions'][0], 'binding does not change the document')



# Project-authored; HtmlBox's URL / HideAddressBar elements are the ones observed in the reference install.
EXPLORER_YXMD = '''<?xml version="1.0"?>
<AlteryxDocument yxmdVer="2026.1"><Nodes>
  <Node ToolID="1"><GuiSettings Plugin="AlteryxGuiToolkit.HtmlBox.HtmlBox"><Position x="10" y="20" width="400" height="300" /></GuiSettings>
    <Properties><Configuration><URL>docs</URL><HideAddressBar value="True" /></Configuration></Properties></Node>
</Nodes><Connections /><Properties /></AlteryxDocument>'''


class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_explorer_shows_a_folder_a_file_or_a_web_address(self):
        (self.dir / 'docs' / 'img').mkdir(parents=True)
        (self.dir / 'docs' / 'readme.txt').write_text('How this workflow works')
        (self.dir / 'docs' / '.secret').write_text('x')
        folder = explore('docs', self.dir)
        self.assertEqual([(e['name'], e['folder']) for e in folder['entries']], [('img', True), ('readme.txt', False)])
        self.assertEqual(explore('docs/readme.txt', self.dir)['text'], 'How this workflow works')
        self.assertEqual(explore((self.dir / 'docs' / 'readme.txt').as_uri())['kind'], 'file')
        self.assertEqual(explore('https://example.com/help'), {'kind': 'web', 'url': 'https://example.com/help'})
        for bad in ('', 'ftp://host/x', 'docs/missing.txt'):
            with self.assertRaises(EngineError):
                explore(bad, self.dir)

    def test_html_box_imports_as_an_explorer_and_never_runs(self):
        path = self.dir / 'explorer.yxmd'
        path.write_text(EXPLORER_YXMD)
        doc, report = import_file(path)
        self.assertEqual(report['nodes'][0]['as'], 'explorer')
        self.assertEqual(doc['metadata']['ui']['explorers'][0] | {'position': None},
                         {'id': 't1', 'url': 'docs', 'hide_address_bar': True, 'position': None,
                          'size': {'width': 400.0, 'height': 300.0}, 'parent': None})
        self.assertEqual(doc['nodes'], [])


if __name__ == '__main__':
    unittest.main()
