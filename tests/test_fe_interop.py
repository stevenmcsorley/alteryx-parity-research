"""Workflow document import/export (original fixtures only)."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from flowengine.errors import WorkflowValidationError
from flowengine.executor import plan_workflow, run_workflow
from flowengine.interop.yxmd import export_document, import_document, import_file
from flowengine.interface_app import bind_application

from helpers import ROOT, rows

FIXTURE = ROOT / 'tests' / 'fixtures' / 'yxmd' / 'regional_sales.yxmd'
ACTION_FIXTURE = ROOT / 'tests' / 'fixtures' / 'action' / 'filter_update_app.xml'


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.doc, self.report = import_file(FIXTURE)

    def test_everything_is_accounted_for(self):
        by_node = {n['node']: n for n in self.report['nodes']}
        self.assertEqual(by_node['t10']['as'], 'container')
        self.assertEqual(by_node['t9']['as'], 'comment')
        self.assertEqual(by_node['t8']['status'], 'unsupported')
        self.assertEqual(by_node['t1']['status'], 'partial')  # Text Input types inferred (warning)
        self.assertTrue(all(by_node[f't{i}']['status'] == 'mapped' for i in (2, 3, 4, 5, 6, 7)))
        ui = self.doc['metadata']['ui']
        self.assertEqual(ui['containers'][0]['children'], ['t2', 't3'])
        self.assertEqual(ui['comments'][0]['text'], 'Sums revenue by region.')
        self.assertIn('t8', self.doc['metadata']['import']['nodes'])  # original XML preserved
        self.assertIn('BufferDistance', self.doc['metadata']['import']['nodes']['t8']['xml'])
        self.assertEqual(next(n for n in self.doc['nodes'] if n['id'] == 't1')['annotation'], 'synthetic sales')

    def test_validation_reports_the_unsupported_catalogue_tool(self):
        problems = plan_workflow(self.doc, base_dir=ROOT)[1]
        self.assertEqual([(p['node_id'], p['code']) for p in problems], [('t8', 'tool-unavailable')])
        self.assertIn('Buffer', problems[0]['message'])
        with self.assertRaises(WorkflowValidationError):
            run_workflow(self.doc, base_dir=ROOT)

    def test_runs_once_the_unsupported_branch_is_removed(self):
        doc = json.loads(json.dumps(self.doc))
        doc['nodes'] = [n for n in doc['nodes'] if n['id'] != 't8']
        doc['edges'] = [e for e in doc['edges'] if e['to']['node'] != 't8']
        r = run_workflow(doc, base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(rows(r.output('t5')), [['East', 21.0], ['North', 11.5], ['South', 10.0]])
        self.assertEqual(len(r.browse['t7']), 6)  # the 3-row stream unioned twice (#1, #2)

    def test_export_round_trip(self):
        doc = json.loads(json.dumps(self.doc))
        doc['nodes'] = [n for n in doc['nodes'] if n['id'] != 't8']
        doc['edges'] = [e for e in doc['edges'] if e['to']['node'] != 't8']
        xml, problems = export_document(doc)
        self.assertEqual(problems, [])
        again, report = import_document(xml)
        self.assertEqual([(n['tool'], n['config']) for n in again['nodes']],
                         [(n['tool'], n['config']) for n in doc['nodes']])
        a = run_workflow(again, base_dir=ROOT)
        self.assertEqual(rows(a.output('t5')), [['East', 21.0], ['North', 11.5], ['South', 10.0]])

    def test_export_lists_what_cannot_be_expressed(self):
        doc = {'format': 'flowengine.workflow', 'formatVersion': 1,
               'nodes': [{'id': 'a', 'tool': 'core.python', 'config': {'code': 'pass'}}], 'edges': []}
        _, problems = export_document(doc)
        self.assertEqual(problems, ['a: core.python has no export mapping'])

    def test_packaged_workflow(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / 'sales.yxzp'
            with zipfile.ZipFile(pkg, 'w') as z:
                z.write(FIXTURE, 'regional_sales.yxmd')
                z.writestr('data/readme.txt', 'asset')
            doc, report = import_file(pkg)
            self.assertEqual(len(doc['nodes']), len(self.doc['nodes']))
            self.assertIn('data/readme.txt', report['package_members'])

    def test_rejects_non_workflow_documents(self):
        with self.assertRaises(ValueError):
            import_document('<NotAWorkflow />')
        with self.assertRaises(ValueError):
            import_document('not xml')

    def test_imported_alteryx_update_value_action_binds_filter_and_executes(self):
        # Project-authored XML follows the structurally observed Designer form:
        # interface wiring is in Connections and the update rule in RuntimeProperties.
        xml = '''<AlteryxDocument yxmdVer="2026.2"><Nodes>
          <Node ToolID="1"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.TextBox.QuestionTextBox"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="2"><GuiSettings Plugin="AlteryxGuiToolkit.Action.Action"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="3"><GuiSettings Plugin="AlteryxBasePluginsGui.TextInput.TextInput"/><Properties><Configuration><NumRows value="2"/><Fields><Field name="region"/></Fields><Data><r><c>East</c></r><r><c>West</c></r></Data></Configuration></Properties></Node>
          <Node ToolID="4"><GuiSettings Plugin="AlteryxBasePluginsGui.Filter.Filter"/><Properties><Configuration><Expression>[region] = &quot;West&quot;</Expression></Configuration></Properties></Node>
        </Nodes><Connections>
          <Connection><Origin ToolID="1" Connection="Question"/><Destination ToolID="2" Connection="Question"/></Connection>
          <Connection><Origin ToolID="2" Connection="Action"/><Destination ToolID="4" Connection="Action"/></Connection>
          <Connection><Origin ToolID="3" Connection="Output"/><Destination ToolID="4" Connection="Input"/></Connection>
        </Connections><Properties><RuntimeProperties><Actions><NoCondition><Type>NoCondition</Type><True><Action><Type>UpdateValue</Type><ToolId value="2"/><Mode>Simple</Mode><Replace value="False"/><Destination>4/Expression</Destination></Action></True></NoCondition></Actions>
          <Questions><Question><Type>TextBox</Type><Description>Region</Description><Name>Region</Name><ToolId value="1"/><Default>\"West\"</Default></Question></Questions>
          <ModuleType>Wizard</ModuleType></RuntimeProperties></Properties></AlteryxDocument>'''
        imported, report = import_document(xml)
        self.assertEqual([n['tool'] for n in imported['nodes']], ['core.text_input', 'core.filter'])
        self.assertEqual(len(imported['edges']), 1)
        self.assertEqual(imported['metadata']['application']['actions'][0]['alteryx']['destination'], '4/Expression')
        bound = bind_application(imported, {'alteryx-question-1': '[region] = "East"'})
        result = run_workflow(bound, base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(rows(result.output('t4', 'true')), [['East']])
        self.assertEqual(rows(result.output('t4', 'false')), [['West']])
        self.assertEqual(imported['nodes'][1]['config']['expression'], '[region] = "West"')
        self.assertEqual(report['edge_warnings'], [])
        exported, problems = export_document(imported)
        self.assertEqual(problems, [])
        roundtrip, second_report = import_document(exported)
        self.assertEqual(len(roundtrip['metadata']['application']['actions']), 1)
        self.assertIn('AlteryxGuiToolkit.Action.Action', exported)
        self.assertIn('<Destination>2/Expression</Destination>', exported)
        qid = roundtrip['metadata']['application']['questions'][0]['id']
        rebound = bind_application(roundtrip, {qid: '[region] = "East"'})
        rerun = run_workflow(rebound, base_dir=ROOT)
        self.assertTrue(rerun.ok, rerun.error)
        self.assertEqual(rows(rerun.output('t2', 'true')), [['East']])

    def test_imported_update_value_action_targets_single_formula_expression(self):
        # Structure and target path are present in installed Designer macro samples.
        # This project-authored fixture verifies Flowbench execution, not Designer output parity.
        xml = '''<AlteryxDocument yxmdVer="2026.2"><Nodes>
          <Node ToolID="1"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.TextBox.QuestionTextBox"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="2"><GuiSettings Plugin="AlteryxGuiToolkit.Action.Action"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="3"><GuiSettings Plugin="AlteryxBasePluginsGui.TextInput.TextInput"/><Properties><Configuration><NumRows value="2"/><Fields><Field name="x" type="Int32" size="4"/></Fields><Data><r><c>2</c></r><r><c>4</c></r></Data></Configuration></Properties></Node>
          <Node ToolID="4"><GuiSettings Plugin="AlteryxBasePluginsGui.Formula.Formula"/><Properties><Configuration><FormulaFields><FormulaField field="y" type="Int32" size="4" expression="[x] * 2"/></FormulaFields></Configuration></Properties></Node>
        </Nodes><Connections>
          <Connection><Origin ToolID="1" Connection="Question"/><Destination ToolID="2" Connection="Question"/></Connection>
          <Connection><Origin ToolID="2" Connection="Action"/><Destination ToolID="4" Connection="Action"/></Connection>
          <Connection><Origin ToolID="3" Connection="Output"/><Destination ToolID="4" Connection="Input"/></Connection>
        </Connections><Properties><RuntimeProperties><Actions><NoCondition><Type>NoCondition</Type><True><Action><Type>UpdateValue</Type><ToolId value="2"/><Mode>Simple</Mode><Replace value="True"/><Destination>4/FormulaFields/FormulaField/@expression</Destination></Action></True></NoCondition></Actions>
          <Questions><Question><Type>TextBox</Type><Description>Formula</Description><Name>Formula</Name><ToolId value="1"/><Default>[x] * 2</Default></Question></Questions>
        </RuntimeProperties></Properties></AlteryxDocument>'''
        imported, report = import_document(xml)
        self.assertEqual(report['edge_warnings'], [])
        action = imported['metadata']['application']['actions'][0]
        self.assertEqual(action['config_path'], ['formulas', '0', 'expression'])
        before = json.dumps(imported, sort_keys=True)
        bound = bind_application(imported, {'alteryx-question-1': '[x] + 5'})
        result = run_workflow(bound, base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(rows(result.output('t4')), [[2, 7], [4, 9]])
        self.assertEqual(json.dumps(imported, sort_keys=True), before)

    def test_formula_action_with_ambiguous_multiple_formula_fields_is_unsupported(self):
        xml = '''<AlteryxDocument><Nodes>
          <Node ToolID="1"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.TextBox.QuestionTextBox"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="2"><GuiSettings Plugin="AlteryxGuiToolkit.Action.Action"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="3"><GuiSettings Plugin="AlteryxBasePluginsGui.Formula.Formula"/><Properties><Configuration><FormulaFields>
            <FormulaField field="a" type="Int32" size="4" expression="1"/><FormulaField field="b" type="Int32" size="4" expression="2"/>
          </FormulaFields></Configuration></Properties></Node>
        </Nodes><Connections><Connection><Origin ToolID="1" Connection="Question"/><Destination ToolID="2" Connection="Question"/></Connection>
          <Connection><Origin ToolID="2" Connection="Action"/><Destination ToolID="3" Connection="Action"/></Connection></Connections>
          <Properties><RuntimeProperties><Actions><NoCondition><Type>NoCondition</Type><True><Action><Type>UpdateValue</Type><ToolId value="2"/><Mode>Simple</Mode><Destination>3/FormulaFields/FormulaField/@expression</Destination></Action></True></NoCondition></Actions>
            <Questions><Question><Type>TextBox</Type><ToolId value="1"/></Question></Questions></RuntimeProperties></Properties></AlteryxDocument>'''
        imported, report = import_document(xml)
        self.assertEqual(imported['metadata']['application']['actions'], [])
        self.assertTrue(any('preserved as unsupported' in w for w in report['edge_warnings']))

    def test_designer_action_acceptance_fixture_executes_on_a_private_copy(self):
        source = ACTION_FIXTURE.read_text(encoding='utf-8')
        imported, report = import_document(source, name='Action acceptance')
        original = json.dumps(imported, sort_keys=True)
        self.assertEqual(report['edge_warnings'], [])
        self.assertEqual(len(imported['metadata']['application']['actions']), 1)
        self.assertEqual(imported['metadata']['application']['actions'][0]['config_path'], ['expression'])

        west = bind_application(imported, {'alteryx-question-1': '[region] = "West"'})
        west_result = run_workflow(west, base_dir=ROOT)
        self.assertTrue(west_result.ok, west_result.error)
        self.assertEqual(rows(west_result.output('t4', 'true')), [['West']])
        self.assertEqual(rows(west_result.output('t4', 'false')), [['East'], ['North']])

        north = bind_application(imported, {'alteryx-question-1': '[region] = "North"'})
        north_result = run_workflow(north, base_dir=ROOT)
        self.assertTrue(north_result.ok, north_result.error)
        self.assertEqual(rows(north_result.output('t4', 'true')), [['North']])
        self.assertEqual(rows(north_result.output('t4', 'false')), [['East'], ['West']])

        self.assertEqual(json.dumps(imported, sort_keys=True), original)
        self.assertEqual(imported['nodes'][1]['config']['expression'], '[region] = "East"')
        exported, problems = export_document(imported)
        self.assertEqual(problems, [])
        round_trip, round_trip_report = import_document(exported)
        self.assertEqual(round_trip_report['edge_warnings'], [])
        action = round_trip['metadata']['application']['actions'][0]
        self.assertEqual(action['config_path'], ['expression'])
        self.assertEqual(action['alteryx']['type'], 'UpdateValue')
        self.assertEqual(action['alteryx']['mode'], 'Simple')
        self.assertEqual(action['alteryx']['destination'].split('/', 1)[1], 'Expression')
        rebound = bind_application(round_trip, {round_trip['metadata']['application']['questions'][0]['id']:
                                                '[region] = "West"'})
        after_round_trip = run_workflow(rebound, base_dir=ROOT)
        self.assertTrue(after_round_trip.ok, after_round_trip.error)
        self.assertEqual(rows(after_round_trip.output(action['node_id'], 'true')), [['West']])

    def test_imported_unsupported_action_is_diagnostic_and_password_default_is_not_retained(self):
        xml = '''<AlteryxDocument><Nodes>
          <Node ToolID="1"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.TextBox.QuestionTextBox"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="2"><GuiSettings Plugin="AlteryxGuiToolkit.Action.Action"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="3"><GuiSettings Plugin="AlteryxBasePluginsGui.Filter.Filter"/><Properties><Configuration><Expression>True</Expression></Configuration></Properties></Node>
        </Nodes><Connections><Connection><Origin ToolID="1" Connection="Question"/><Destination ToolID="2" Connection="Question"/></Connection>
          <Connection><Origin ToolID="2" Connection="Action"/><Destination ToolID="3" Connection="Action"/></Connection></Connections>
        <Properties><RuntimeProperties><Actions><NoCondition><Type>NoCondition</Type><True><Action><Type>UpdateRawXml</Type><ToolId value="2"/><Mode>Expression</Mode><Destination>3/Expression</Destination></Action></True></NoCondition></Actions>
          <Questions><Question><Type>TextBox</Type><Description>Password</Description><Name>Password</Name><ToolId value="1"/><Default>dont-store-this</Default><Password value="True"/></Question></Questions>
        </RuntimeProperties></Properties></AlteryxDocument>'''
        imported, report = import_document(xml)
        self.assertEqual(imported['metadata']['application']['questions'][0]['secret'], True)
        self.assertNotIn('default', imported['metadata']['application']['questions'][0])
        self.assertNotIn('dont-store-this', json.dumps(imported['metadata']))
        self.assertEqual(imported['metadata']['application']['actions'], [])
        self.assertTrue(any('UpdateRawXml' in warning for warning in report['edge_warnings']))
        self.assertTrue(any('Password question defaults were redacted' in warning for warning in report['edge_warnings']))
        exported, problems = export_document(imported)
        self.assertEqual(problems, [])
        self.assertNotIn('dont-store-this', exported)

    def test_imported_application_controls_expose_typed_questions(self):
        xml = '''<AlteryxDocument><Nodes>
          <Node ToolID="1"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.NumericUpDown.NumericUpDown"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="2"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.Date.Date"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="3"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.FileBrowse.FileBrowse"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="4"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.DropDownListBox.DropDown"/><Properties><Configuration/></Properties></Node>
        </Nodes><Properties><RuntimeProperties><Questions>
          <Question><Type>NumericUpDown</Type><ToolId value="1"/><Default>5</Default></Question>
          <Question><Type>Date</Type><ToolId value="2"/><Default>2026-01-02</Default></Question>
          <Question><Type>FileBrowse</Type><ToolId value="3"/></Question>
          <Question><Type>DropDown</Type><ToolId value="4"><![CDATA[<Option>red</Option><Option>blue</Option>]]></ToolId><Option>red</Option><Option>blue</Option></Question>
        </Questions></RuntimeProperties></Properties></AlteryxDocument>'''
        imported, report = import_document(xml)
        questions = imported['metadata']['application']['questions']
        self.assertEqual([q['type'] for q in questions], ['number', 'date', 'file', 'choice'])
        self.assertEqual(questions[0]['default'], 5.0)
        self.assertEqual(questions[3]['options'], ['red', 'blue'])
        self.assertEqual(report['edge_warnings'], [])

    def test_application_answers_enforce_numeric_bounds_and_multiselect_options(self):
        doc = {'nodes': [{'id': 'n', 'config': {'value': 0}}], 'metadata': {'application': {
            'questions': [{'id': 'n', 'type': 'number', 'minimum': 1, 'maximum': 9},
                          {'id': 'colors', 'type': 'multi-choice', 'options': ['red', 'blue']}],
            'actions': [{'question': 'n', 'node_id': 'n', 'config_path': ['value']}]}}}
        with self.assertRaises(Exception):
            bind_application(doc, {'n': 10, 'colors': ['red']})
        bound = bind_application(doc, {'n': 4, 'colors': ['red', 'blue']})
        self.assertEqual(bound['nodes'][0]['config']['value'], 4)

    def test_native_tree_question_binds_a_defined_leaf_path_only(self):
        from flowengine.errors import EngineError
        doc = {'nodes': [{'id': 'filter', 'config': {'expression': 'original'}}], 'metadata': {'application': {
            'questions': [{'id': 'category', 'type': 'tree', 'options': ['Sales > Online', 'Sales > Retail', 'Returns'],
                           'default': 'Sales > Online'}],
            'actions': [{'question': 'category', 'node_id': 'filter', 'config_path': ['expression']}],
        }}}
        bound = bind_application(doc, {'category': 'Sales > Retail'})
        self.assertEqual(bound['nodes'][0]['config']['expression'], 'Sales > Retail')
        with self.assertRaises(EngineError) as caught:
            bind_application(doc, {'category': 'Sales > Wholesale'})
        self.assertEqual(caught.exception.code, 'application-input-invalid')
        doc['metadata']['application']['questions'][0]['options'] = ['Sales', 'Sales > Online']
        with self.assertRaisesRegex(EngineError, 'duplicate paths or a selectable parent'):
            bind_application(doc, {})

    def test_native_error_message_stops_only_on_matching_application_condition(self):
        from flowengine.errors import EngineError
        doc = {'nodes': [], 'metadata': {'application': {
            'questions': [{'id': 'continue', 'type': 'boolean'}], 'actions': [],
            'errors': [{'id': 'error_message_1', 'message': 'Choose a valid region.',
                        'when': {'question': 'continue', 'operator': 'falsy'}}]}}}
        self.assertEqual(bind_application(doc, {'continue': True})['nodes'], [])
        with self.assertRaises(EngineError) as caught:
            bind_application(doc, {'continue': False})
        self.assertEqual(caught.exception.code, 'application-error-message')
        self.assertEqual(caught.exception.message, 'Choose a valid region.')
        self.assertEqual(caught.exception.node_id, 'error_message_1')

    def test_native_error_message_rejects_invalid_predicates_and_empty_messages(self):
        doc = {'nodes': [], 'metadata': {'application': {'questions': [{'id': 'ok', 'type': 'boolean'}],
            'actions': [], 'errors': [{'message': ' ', 'when': {'question': 'missing', 'operator': 'truthy'}}]}}}
        with self.assertRaisesRegex(Exception, 'non-empty message'):
            bind_application(doc, {})
        doc['metadata']['application']['errors'][0] = {'message': 'Stop', 'when': {'question': 'missing', 'operator': 'truthy'}}
        with self.assertRaisesRegex(Exception, 'invalid conditional predicate'):
            bind_application(doc, {})

    def test_native_conditional_action_updates_only_when_activated(self):
        # Native Flowbench predicate, not a claim of Alteryx Condition XML parity.
        doc = {'nodes': [{'id': 'n', 'config': {'expression': 'original'}}],
               'metadata': {'application': {
                   'questions': [{'id': 'enabled', 'type': 'boolean', 'default': False},
                                 {'id': 'value', 'type': 'text', 'default': 'replacement'},
                                 {'id': 'tags', 'type': 'multi-choice', 'options': ['red', 'blue'],
                                  'default': ['red']}],
                   'actions': [{'question': 'value', 'node_id': 'n', 'config_path': ['expression'],
                                'when': {'question': 'enabled', 'operator': 'truthy'}}]}}}
        original = json.dumps(doc, sort_keys=True)
        self.assertEqual(bind_application(doc, {})['nodes'][0]['config']['expression'], 'original')
        self.assertEqual(bind_application(doc, {'enabled': True})['nodes'][0]['config']['expression'], 'replacement')
        self.assertEqual(json.dumps(doc, sort_keys=True), original)
        doc['metadata']['application']['actions'][0]['when'] = {
            'question': 'tags', 'operator': 'contains', 'value': 'blue'}
        self.assertEqual(bind_application(doc, {})['nodes'][0]['config']['expression'], 'original')
        self.assertEqual(bind_application(doc, {'tags': ['blue']})['nodes'][0]['config']['expression'], 'replacement')
        doc['metadata']['application']['actions'][0]['when'] = {
            'question': 'missing', 'operator': 'truthy'}
        with self.assertRaises(Exception):
            bind_application(doc, {})

    def test_condition_branches_apply_only_active_updates_and_allow_exclusive_target_updates(self):
        doc = {'nodes': [{'id': 'filter', 'config': {'expression': '1'}}], 'metadata': {'application': {
            'questions': [{'id': 'enabled', 'type': 'boolean'},
                          {'id': 'yes', 'type': 'text', 'default': '[amount] > 100'},
                          {'id': 'no', 'type': 'text', 'default': '[amount] > 0'}],
            'actions': [
                {'question': 'yes', 'node_id': 'filter', 'config_path': ['expression'],
                 'when': {'question': 'enabled', 'operator': 'truthy'}},
                {'question': 'no', 'node_id': 'filter', 'config_path': ['expression'],
                 'when': {'question': 'enabled', 'operator': 'falsy'}},
            ]}}}
        self.assertEqual(bind_application(doc, {'enabled': True})['nodes'][0]['config']['expression'], '[amount] > 100')
        self.assertEqual(bind_application(doc, {'enabled': False})['nodes'][0]['config']['expression'], '[amount] > 0')
        doc['metadata']['application']['actions'][1]['when'] = {'question': 'enabled', 'operator': 'truthy'}
        with self.assertRaisesRegex(Exception, 'Conflicting active'):
            bind_application(doc, {'enabled': True})

    def test_condition_branches_execute_different_filter_configurations(self):
        doc, report = import_document(ACTION_FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(report['edge_warnings'], [])
        app = doc['metadata']['application']
        app['questions'].extend([
            {'id': 'use-east', 'type': 'boolean'},
            {'id': 'east-expression', 'type': 'text', 'default': '[region] = "East"'},
            {'id': 'west-expression', 'type': 'text', 'default': '[region] = "West"'},
        ])
        node_id, config_path = app['actions'][0]['node_id'], app['actions'][0]['config_path']
        app['actions'] = [
            {'question': 'east-expression', 'node_id': node_id, 'config_path': config_path,
             'when': {'question': 'use-east', 'operator': 'truthy'}},
            {'question': 'west-expression', 'node_id': node_id, 'config_path': config_path,
             'when': {'question': 'use-east', 'operator': 'falsy'}},
        ]
        east = run_workflow(bind_application(doc, {'use-east': True}), base_dir=ROOT)
        west = run_workflow(bind_application(doc, {'use-east': False}), base_dir=ROOT)
        self.assertTrue(east.ok, east.error)
        self.assertTrue(west.ok, west.error)
        self.assertEqual(rows(east.output(node_id, 'true')), [['East']])
        self.assertEqual(rows(west.output(node_id, 'true')), [['West']])
        self.assertEqual(doc['nodes'][1]['config']['expression'], '[region] = "East"')

    def test_nested_application_condition_predicates(self):
        doc = {'nodes': [{'id': 'n', 'config': {'value': 'old'}}], 'metadata': {'application': {
            'questions': [{'id': 'enabled', 'type': 'boolean'}, {'id': 'name', 'type': 'text'}],
            'actions': [{'question': 'name', 'node_id': 'n', 'config_path': ['value'], 'when': {
                'operator': 'all', 'conditions': [
                    {'question': 'enabled', 'operator': 'truthy'},
                    {'operator': 'not', 'condition': {'question': 'name', 'operator': 'equals', 'value': ''}},
                ]}}]}}}
        self.assertEqual(bind_application(doc, {'enabled': True, 'name': 'ok'})['nodes'][0]['config']['value'], 'ok')
        self.assertEqual(bind_application(doc, {'enabled': False, 'name': 'ok'})['nodes'][0]['config']['value'], 'old')

    def test_condition_comparisons_are_typed_for_numeric_and_date_answers(self):
        doc = {'nodes': [{'id': 'n', 'config': {'value': 0}}], 'metadata': {'application': {
            'questions': [{'id': 'amount', 'type': 'number'}, {'id': 'day', 'type': 'date'}],
            'actions': [{'question': 'amount', 'node_id': 'n', 'config_path': ['value'],
                         'when': {'question': 'amount', 'operator': 'greater_than', 'value': 100}}]}}}
        self.assertEqual(bind_application(doc, {'amount': 101, 'day': '2026-10-15'})['nodes'][0]['config']['value'], 101)
        self.assertEqual(bind_application(doc, {'amount': 100, 'day': '2026-10-15'})['nodes'][0]['config']['value'], 0)
        doc['metadata']['application']['actions'][0]['when'] = {
            'question': 'amount', 'operator': 'equals', 'value': 100.0}
        self.assertEqual(bind_application(doc, {'amount': 100, 'day': '2026-10-15'})['nodes'][0]['config']['value'], 100)
        doc['metadata']['application']['actions'][0]['when'] = {
            'question': 'day', 'operator': 'greater_than_or_equal', 'value': '2026-01-01'}
        self.assertEqual(bind_application(doc, {'amount': 1, 'day': '2026-10-15'})['nodes'][0]['config']['value'], 1)
        with self.assertRaisesRegex(Exception, 'ISO date'):
            bind_application(doc, {'amount': 1, 'day': '15/10/2026'})

    def test_simple_action_updates_an_existing_text_input_cell(self):
        xml = '''<AlteryxDocument><Nodes>
          <Node ToolID="1"><GuiSettings Plugin="AlteryxGuiToolkit.Questions.TextBox.QuestionTextBox"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="2"><GuiSettings Plugin="AlteryxGuiToolkit.Action.Action"/><Properties><Configuration/></Properties></Node>
          <Node ToolID="3"><GuiSettings Plugin="AlteryxBasePluginsGui.TextInput.TextInput"/><Properties><Configuration><Fields><Field name="region"/></Fields><Data><r><c>East</c></r></Data></Configuration></Properties></Node>
        </Nodes><Connections><Connection><Origin ToolID="1" Connection="Question"/><Destination ToolID="2" Connection="Question"/></Connection>
          <Connection><Origin ToolID="2" Connection="Action"/><Destination ToolID="3" Connection="Action"/></Connection></Connections>
        <Properties><RuntimeProperties><Actions><NoCondition><Type>NoCondition</Type><True><Action><Type>UpdateValue</Type><ToolId value="2"/><Mode>Simple</Mode><Destination>3/Text Input Data/r[1]/c[1]</Destination></Action></True></NoCondition></Actions>
          <Questions><Question><Type>TextBox</Type><ToolId value="1"/><Default>West</Default></Question></Questions></RuntimeProperties></Properties></AlteryxDocument>'''
        imported, report = import_document(xml)
        self.assertEqual(report['edge_warnings'], [])
        self.assertEqual(imported['metadata']['application']['actions'][0]['config_path'], ['rows', '0', '0'])
        bound = bind_application(imported, {'alteryx-question-1': 'West'})
        result = run_workflow(bound, base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(rows(result.output('t3')), [['West']])

    def test_imported_fuzzy_match_executes_supported_flowbench_translation_and_roundtrips_xml(self):
        # Project-authored fixture uses structurally observed FuzzyMatch XML fields.
        # Scores use Flowbench v1 and are explicitly not asserted as Alteryx parity.
        xml = '''<AlteryxDocument yxmdVer="2026.2"><Nodes>
          <Node ToolID="1"><GuiSettings Plugin="AlteryxBasePluginsGui.TextInput.TextInput"/><Properties><Configuration>
            <Fields><Field name="id" type="Int32" size="4"/><Field name="name" type="V_WString" size="40"/></Fields>
            <Data><r><c>1</c><c>Smith</c></r><r><c>2</c><c>Schmidt</c></r><r><c>3</c><c>Jones</c></r></Data>
          </Configuration></Properties></Node>
          <Node ToolID="2"><GuiSettings Plugin="AlteryxBasePluginsGui.FuzzyMatch.FuzzyMatch"/><Properties><Configuration>
            <RecordIdField>id</RecordIdField><MatchThreshold value="30"/><OutputScore value="True"/>
            <OutputKey value="True"/><OutputUnmatched value="True"/><OutputMakeGroup value="False"/>
            <GenerateKeys value="False"/><MatchFields><MatchField><FieldName>name</FieldName><Style Name="Custom">
              <KeyGeneration><KeyFunction>DblMetaphone</KeyFunction><UseWords value="False"/><IgnoreEmpty value="True"/><MaxKeyLen value="8"/></KeyGeneration>
              <Match><MatchFunction>Levenshtein</MatchFunction><MatchThreshold value="0"/><MatchWeight value="100"/></Match>
            </Style></MatchField></MatchFields><MergeMode value="False"/><SourceIdField/>
          </Configuration></Properties></Node>
        </Nodes><Connections><Connection><Origin ToolID="1" Connection="Output"/><Destination ToolID="2" Connection="Input"/></Connection></Connections>
        <Properties/></AlteryxDocument>'''
        imported, report = import_document(xml)
        fuzzy = next(n for n in imported['nodes'] if n['id'] == 't2')
        self.assertEqual(fuzzy['tool'], 'core.fuzzy_match')
        self.assertEqual(report['nodes'][1]['status'], 'partial')
        result = run_workflow(imported, base_dir=ROOT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual([(r[0], r[1]) for r in rows(result.output('t2'))], [(1, 2), (3, None)])
        key_only_xml = xml.replace('<MatchFunction>Levenshtein</MatchFunction>',
                                   '<MatchFunction>None</MatchFunction>')
        key_only_doc, _ = import_document(key_only_xml)
        key_only_cfg = next(n for n in key_only_doc['nodes'] if n['id'] == 't2')['config']
        self.assertEqual(key_only_cfg['match_fields'][0]['match_method'], 'key_only')
        key_only_result = run_workflow(key_only_doc, base_dir=ROOT)
        self.assertTrue(key_only_result.ok, key_only_result.error)
        self.assertEqual(rows(key_only_result.output('t2'))[0][2], 100)
        exported, problems = export_document(imported)
        self.assertEqual(problems, [])
        self.assertIn('AlteryxBasePluginsGui.FuzzyMatch.FuzzyMatch', exported)
        again, again_report = import_document(exported)
        self.assertEqual(again_report['nodes'][1]['status'], 'partial')
        rerun = run_workflow(again, base_dir=ROOT)
        self.assertTrue(rerun.ok, rerun.error)
        self.assertEqual(rows(rerun.output('t2')), rows(result.output('t2')))

    def test_imported_fuzzy_match_rejects_vendor_word_frequency_without_dropping_xml(self):
        xml = '''<AlteryxDocument><Nodes><Node ToolID="1"><GuiSettings Plugin="AlteryxBasePluginsGui.FuzzyMatch.FuzzyMatch"/>
          <Properties><Configuration><RecordIdField>id</RecordIdField><MatchFields><MatchField><FieldName>name</FieldName>
            <Style><KeyGeneration><KeyFunction>DblMetaphone</KeyFunction></KeyGeneration><Match>
              <MatchFunction>JaroTFIDF</MatchFunction><WordStats>Name.yxdb</WordStats></Match></Style>
          </MatchField></MatchFields></Configuration></Properties></Node></Nodes><Connections/><Properties/></AlteryxDocument>'''
        imported, report = import_document(xml)
        self.assertEqual(imported['nodes'][0]['tool'], 'AlteryxBasePluginsGui.FuzzyMatch.FuzzyMatch')
        self.assertEqual(report['nodes'][0]['status'], 'unsupported')
        self.assertIn('WordStats', report['nodes'][0]['reason'])
        self.assertIn('JaroTFIDF', imported['metadata']['import']['nodes']['t1']['xml'])
        exported, problems = export_document(imported)
        self.assertIn('AlteryxBasePluginsGui.FuzzyMatch.FuzzyMatch', exported)
        self.assertIn('<WordStats>Name.yxdb</WordStats>', exported)
        self.assertTrue(any('anchors are not translated' in problem for problem in problems))


if __name__ == '__main__':
    unittest.main()
