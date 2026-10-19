#!/usr/bin/env python3
"""Tool-by-tool implementation and verification ledger for all catalogue entries.

Derives every status from evidence; nothing is hand-set:

* kernels       - tools registered in flowengine (plugin manifests)
* tests         - the unit/integration suite is executed; each test is mapped to the
                  tools it actually instantiated (recorded at run time)
* reference     - data/reference-observations.json (structural scan, authorised install)
* public docs   - data/public-doc-audit.json (metadata of official help pages)
* conformance   - conformance/results/<build>/results.json (differential runs)

Status rules (docs/TOOL_STATUS_RULES.md):
  implementation_status = implemented  if a kernel has passing tests, or a registered application adapter has passing tests and no known unsupported
                                       (functional) variants; variants not yet compared with the reference
                                       are listed as unverified and leave verification_status untouched
                        = in-progress  if a kernel or application adapter exists but has missing/failing tests or unsupported variants
                        = not-started  otherwise
  verification_status   = partial      if >=1 conformance case exists and all pass
                        = failed       if any conformance case fails
                        = not-tested   otherwise
  'verified' is never assigned automatically: it requires the full configuration matrix
  for the tool to be covered and reviewed (see docs/TOOL_STATUS_RULES.md).

Outputs: tool-contracts/<category>/<slug>.contract.json (323), data/tool-status.json,
updated status fields in data/catalog.json/.csv and the brief headers.
Use --check to fail if regenerating would change tracked files (CI).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'conformance'))

from catalog_lib import CATALOG, save_catalog  # noqa: E402

TARGET = 'Designer 2026.1.4.126 (pinned public catalogue); reference observations from installed build 2026.2.0.8'
APPLICATION_ADAPTERS = {
    'documentation:comment': {
        'module': 'flowengine.interop.yxmd + desktop CommentNode + desktop/src/model/workflow.ts',
        'engine': 'Flowbench native canvas comment adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_everything_is_accounted_for'],
        'supported': ['Editable canvas text annotation persisted in workflow UI metadata and restored on reopen; Alteryx TextBox text imported as a comment; never executed'],
        'unsupported': ['Alteryx TextBox styling (Shape, Font, FillColor, TextColor, Justification, BG_Image) is not imported; only the text is'],
        'unverified': [],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-ui-serialization-and-import-evidence',
    },
    'documentation:explorer-box': {
        'module': 'flowengine.explorer + flowengine.interop.yxmd + desktop ExplorerNode',
        'engine': 'Flowbench native Explorer Box adapter',
        'test_ids': [
            'test_interface_batch.ExplorerTests.test_explorer_shows_a_folder_a_file_or_a_web_address',
            'test_interface_batch.ExplorerTests.test_html_box_imports_as_an_explorer_and_never_runs',
        ],
        'supported': ['Shows a web page (sandboxed frame), a folder listing or a text file preview; paths resolve against the workflow folder',
                      'Alteryx HtmlBox URL and HideAddressBar imported; the box is documentation and never runs'],
        'unsupported': [],
        'unverified': ['Authentic HtmlBox XML value encoding is not verified against a reference fixture', 'Pages that forbid embedding render blank'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:condition': {
        'module': 'flowengine.interface_app + desktop ApplicationConditionNode',
        'engine': 'Flowbench native analytic-app Condition adapter',
        'test_ids': [
            'test_fe_interop.ImportTests.test_condition_branches_apply_only_active_updates_and_allow_exclusive_target_updates',
            'test_fe_interop.ImportTests.test_condition_branches_execute_different_filter_configurations',
            'test_fe_interop.ImportTests.test_nested_application_condition_predicates',
            'test_fe_interop.ImportTests.test_condition_comparisons_are_typed_for_numeric_and_date_answers',
        ],
        'supported': ['Typed predicates over answers (equals, contains, comparisons, truthy) combined with all/any/not; True and False branches activate only their actions'],
        'unsupported': ['Imported Alteryx Condition XML is preserved but its evaluation is not adapted'],
        'unverified': [],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:map': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native Map question adapter',
        'test_ids': ['test_interface_batch.QuestionTests.test_map_question_takes_a_geojson_geometry'],
        'supported': ['Answer is a valid GeoJSON geometry (or a typed longitude, latitude point), checked with shapely and bound as compact GeoJSON'],
        'unsupported': ['Drawing on an interactive base map with reference layers (no map tile provider is bundled)'],
        'unverified': ['Alteryx Map question default serialisation is not verified, so imported defaults are dropped'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:dcm-connection': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native saved-connection question adapter',
        'test_ids': ['test_interface_batch.QuestionTests.test_connection_question_selects_a_saved_connection'],
        'supported': ['Picks a saved database, web, GitHub or SMTP connection by its stable id (never a secret) and binds the id through actions'],
        'unsupported': ['Alteryx DCM Connection questions are not mapped by the importer (no reference fixture)'],
        'unverified': [],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'documentation:tool-container': {
        'module': 'desktop editor ContainerNode + desktop/src/model/workflow.ts',
        'engine': 'Flowbench native editor container adapter',
        'test_ids': [
            'test_fe_executor.ExecutionTests.test_disabled_flowbench_tool_container_children_skip_execution',
        ],
        'supported': ['Group top-level canvas nodes; edit caption; collapse/expand child nodes; disable/enable contained tools; persist container metadata through workflow save/reopen; propose these edits through atomic AI workflow patches'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx Tool Container XML configuration mapping and imported Style variants are not verified'],
        'evidence': 'docs/TOOL_CONTAINERS.md',
        'evidence_type': 'project-authored-ui-serialization-and-execution-evidence',
    },
    'interface:control-parameter': {
        'module': 'flowengine.tools.macros.BatchMacro + flowengine.interface_app',
        'engine': 'Flowbench batch-macro control parameter adapter',
        'test_ids': [
            'test_macros.BatchMacroTests.test_native_control_parameter_uses_each_parent_row_and_updates_child_configuration',
        ],
        'supported': ['Native control-parameter question maps a parent control-record field to connected child configuration actions for each ordered Batch Macro invocation'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx Control Parameter XML/action connection mapping is not verified'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
    },
    'interface:tree': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native analytic-app Tree question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_tree_question_binds_a_defined_leaf_path_only',
                     'test_interface_batch.QuestionTests.test_options_come_from_a_data_file_and_follow_its_changes',
                     'test_interface_batch.QuestionTests.test_tree_can_browse_a_folder'],
        'supported': ['Single selected configured leaf path with hierarchical " > " labels; answer is bound as the normalized full path string and validated against the declared tree choices',
                      'Options read from a data file (one field per level) or a folder of files, refreshed each run'],
        'unsupported': ['Allocate Geography/Variables data sources (they need the licensed Allocate dataset)', 'Alteryx Tree questions are not mapped by the importer'],
        'unverified': ['Authentic Alteryx Tree XML option settings are not verified'],
        'evidence': 'docs/INTERFACE_TREE.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:date': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native analytic-app ISO date question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_date_question_binds_only_a_real_iso_calendar_date'],
        'supported': ['Native date control accepts valid YYYY-MM-DD calendar dates and binds the selected ISO string through configured actions'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx Date question XML configuration and locale-specific date parsing have not been verified'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:file-browse': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native analytic-app File Browse question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_browse_questions_check_extension_mode_and_existence',
                     'test_fe_service.ServiceTests.test_browse_answers_choose_input_output_and_folder_for_real_execution'],
        'supported': ['Native open-mode answer must be an existing file and save-mode answer a file path in an existing folder, resolved against the workflow directory',
                      'Optional extension filter applied in the native picker and enforced by the engine; selected path bound through configured actions'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx File Browse XML configuration and file-type filter syntax are not verified', 'Gallery/Server upload behaviour is server-only and not part of the desktop engine'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:folder-browse': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native analytic-app Folder Browse question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_browse_questions_check_extension_mode_and_existence',
                     'test_fe_service.ServiceTests.test_browse_answers_choose_input_output_and_folder_for_real_execution'],
        'supported': ['Native folder answer must be an existing directory resolved against the workflow directory; selected path bound through configured actions'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx Folder Browse XML configuration is not verified', 'Gallery/Server behaviour is server-only and not part of the desktop engine'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:text-box': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native multiline Text Box question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_text_area_preserves_multiline_answer_through_action_binding'],
        'supported': ['Native multiline string answer preserved exactly, including newline and Unicode characters, through validated application actions'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx Text Box XML configuration and vendor-specific limits/formatting are not verified'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:check-box': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native Boolean question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_checkbox_binds_boolean_without_string_coercion'],
        'supported': ['Boolean answer, default and Action binding preserve JSON booleans; strings are rejected'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx Check Box XML and Action-connection parity are unverified'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:numeric-up-down': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native bounded numeric question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_application_answers_enforce_numeric_bounds_and_multiselect_options'],
        'supported': ['Finite numeric answers with configured minimum/maximum bounds and typed Action binding'],
        'unsupported': [],
        'unverified': ['Alteryx numeric storage precision, step increment and XML settings are unverified'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:drop-down': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native single-choice question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_choice_controls_enforce_single_and_multi_select_shapes',
                     'test_interface_batch.QuestionTests.test_options_come_from_a_data_file_and_follow_its_changes'],
        'supported': ['Unique string options; exactly one declared option is bound as a string',
                      'Options read from a field of a data file (distinct values), refreshed each time the app runs'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx Drop Down XML option-source settings are not verified'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:radio-button': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native single-choice question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_choice_controls_enforce_single_and_multi_select_shapes',
                     'test_interface_batch.QuestionTests.test_options_come_from_a_data_file_and_follow_its_changes'],
        'supported': ['Unique string options; exactly one declared option is bound as a string',
                      'Options read from a field of a data file (distinct values), refreshed each time the app runs'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx Radio Button XML option-source settings are not verified'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:list-box': {
        'module': 'flowengine.interface_app + desktop/src/ui/dialogs.tsx',
        'engine': 'Flowbench native multi-choice question adapter',
        'test_ids': ['test_fe_interop.ImportTests.test_native_choice_controls_enforce_single_and_multi_select_shapes',
                     'test_interface_batch.QuestionTests.test_options_come_from_a_data_file_and_follow_its_changes'],
        'supported': ['Unique string options; answers must be lists of selected declared values, preserving multi-select shape',
                      'Options read from a field of a data file (distinct values), refreshed each time the app runs'],
        'unsupported': [],
        'unverified': ['Authentic Alteryx List Box XML option-source settings are not verified'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
        'evidence_type': 'project-authored-runtime-and-ui-evidence',
    },
    'interface:error-message': {
        'module': 'flowengine.interface_app',
        'test_ids': [
            'test_fe_interop.ImportTests.test_native_error_message_stops_only_on_matching_application_condition',
            'test_fe_interop.ImportTests.test_native_error_message_rejects_invalid_predicates_and_empty_messages',
        ],
        'supported': ['Native Flowbench application Error Message with optional typed Condition predicate; failure is raised before node execution'],
        'unsupported': [],
        'unverified': ['Alteryx Error Message XML-to-native mapping has not been established against an authorised reference fixture'],
        'evidence': 'docs/INTERFACE_APPLICATIONS.md',
    },
    'interface:action': {
        'module': 'flowengine.interop.yxmd',
        'test_ids': [
            'test_fe_interop.ImportTests.test_imported_alteryx_update_value_action_binds_filter_and_executes',
            'test_fe_interop.ImportTests.test_imported_update_value_action_targets_single_formula_expression',
            'test_fe_interop.ImportTests.test_formula_action_with_ambiguous_multiple_formula_fields_is_unsupported',
            'test_fe_interop.ImportTests.test_designer_action_acceptance_fixture_executes_on_a_private_copy',
            'test_fe_interop.ImportTests.test_imported_unsupported_action_is_diagnostic_and_password_default_is_not_retained',
                     'test_interface_batch.ActionTests.test_formula_action_builds_a_value_from_several_answers',
                     'test_interface_batch.ActionTests.test_input_action_points_input_data_at_the_chosen_file'],
        'supported': ['UpdateValue / Simple / Filter Expression / single connected question / NoCondition True',
                      'UpdateValue / Simple / FormulaFields/FormulaField/@expression / one formula field',
                      'Native formula action: an expression over all answers sets the target value',
                      'Native input action: points an Input Data tool at the chosen file'],
        'unsupported': ['Imported UpdateValueFormula, Dynamic, UpdateInput, UpdateMap, UpdateSelect and UpdateRawXml actions', 'imported condition-driven actions', 'ambiguous Formula targets with multiple FormulaField entries', 'all other unmapped imported target paths'],
        'unverified': [],
        'evidence': 'data/action-reference-evidence.json',
    },
}

CATEGORY_DEPENDENCIES = {
    'Predictive': ['r-runtime (statistical models; vendor ships R-based macros)'],
    'AB Testing': ['r-runtime'], 'Time Series': ['r-runtime'], 'Predictive Grouping': ['r-runtime'],
    'Prescriptive': ['r-runtime', 'optimisation-solver'],
    'Connectors': ['external-service-account (credentials/API access)'],
    'Address': ['licensed-reference-dataset (address/geocoding data)'],
    'Demographic Analysis': ['licensed-reference-dataset (demographic data)'],
    'Behavior Analysis (Deprecated)': ['licensed-reference-dataset (behaviour clusters)', 'deprecated-in-reference'],
    'Calgary': ['licensed-reference-dataset (indexed Calgary data)'],
    'In-Database': ['database-connection (SQL pushdown target)'],
    'Machine Learning': ['python-ml-runtime'], 'Text Mining': ['python-nlp-runtime'],
    'Computer Vision': ['python-cv-runtime (image/OCR)'], 'AI Tools': ['llm-service (model provider credentials)'],
    'Connect': ['vendor-cloud-service'], 'Spatial': ['spatial-geometry-library'],
    'Reporting': ['report-renderer (layout/PDF/image)'], 'Interface': ['analytic-app-and-macro-runtime'],
    'Laboratory': ['experimental-in-reference'], 'SDK Examples': ['sdk-runtime'],
}
BLOCKING = ('licensed-reference-dataset', 'external-service-account', 'vendor-cloud-service', 'llm-service')
# Tests that need a runtime the generic runners do not have (R, Spark with Java). Each runs in its
# own CI job through scripts/run_gated_tests.py, which fails the job when a listed test fails *or is
# skipped*, so every merged commit has passed them. The ledger build does not run them (the result
# would depend on the machine), so the tools they exercise are declared here instead of recorded.
RUNTIME_GATED_EVIDENCE = {
    'test_fe_tools_wave2.UserCodeTools.test_r_tool': {'job': 'r-runtime', 'tools': ['core.r']},
    'test_providers.SparkRuntimeTests.test_spark_code_transforms_typed_tables': {'job': 'spark-runtime',
                                                                                 'tools': ['core.spark_code']},
    **{f'test_s3.LiveS3Tests.{name}': {'job': 'service-integration', 'tools': ['core.s3_download', 'core.s3_upload']}
       for name in ('test_round_trip_in_several_formats', 'test_a_wrong_secret_is_rejected_by_the_server',
                    'test_upload_needs_remote_writes_and_missing_objects_fail_clearly',
                    'test_multipart_upload_above_the_part_size')},
    **{f'test_mongodb.MongoDBTests.{name}': {'job': 'service-integration',
                                             'tools': ['core.mongodb_input', 'core.mongodb_output']}
       for name in ('test_insert_then_find_with_filter_projection_sort_and_types',
                    'test_nested_documents_json_output_and_upsert',
                    'test_writes_need_permission_writable_connection_and_replace_confirmation',
                    'test_bad_password_and_bad_filter_fail_clearly')},
    'test_tableau_anaplan_odbc.TableauTests.test_local_extract_holds_typed_and_spatial_columns':
        {'job': 'service-integration', 'tools': ['core.tableau_output']},
    'test_tableau_anaplan_odbc.TableauTests.test_publish_signs_in_with_a_token_publishes_and_signs_out':
        {'job': 'service-integration', 'tools': ['core.tableau_output', 'core.publish_to_tableau_server']},
    'test_tableau_anaplan_odbc.OdbcTests.test_metadata_of_tables_views_and_columns':
        {'job': 'service-integration', 'tools': ['core.odbc_loader']},
}
ENVIRONMENT_GATED_LEDGER_TESTS = set(RUNTIME_GATED_EVIDENCE)
# Replays outputs Flowbench itself recorded (data/tool-examples.json): a regression guard for the
# AI's examples, not independent evidence that a tool behaves correctly.
SELF_REFERENTIAL_LEDGER_TESTS = {
    'test_tool_examples.ToolExampleTests.test_every_stored_example_replays',
    # Check that every tool has a specification (they look every tool up), not how tools behave.
    'test_tool_specs.ContractTests.test_every_tool_has_a_contract_with_purpose_settings_and_execution',
    'test_tool_specs.ContractTests.test_errors_are_read_from_the_kernel',
    'test_tool_specs.ContractTests.test_formula_reference_lists_real_functions',
}
LEDGER_EXCLUDED_TESTS = ENVIRONMENT_GATED_LEDGER_TESTS | SELF_REFERENTIAL_LEDGER_TESTS


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


# --------------------------------------------------------------------------- tests

def run_tests_with_tool_map():
    """Run the suite; return {test_id: {'ok': bool, 'tools': set(tool_id)}}."""
    import flowengine.plugin as plugin
    current = {'test': None}
    used = defaultdict(set)
    seen = set()
    original_get = plugin.Registry.get

    def recording_get(self, tool_id):
        tool = original_get(self, tool_id)
        if current['test'] and current['test'] not in LEDGER_EXCLUDED_TESTS:
            used[current['test']].add(tool.manifest.tool_id)
        return tool
    plugin.Registry.get = recording_get

    class Recorder(unittest.TextTestResult):
        def startTest(self, test):
            current['test'] = test.id()
            if test.id() not in LEDGER_EXCLUDED_TESTS:
                seen.add(test.id())
            super().startTest(test)

    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT / 'tests'))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, resultclass=Recorder, verbosity=0).run(suite)
    plugin.Registry.get = original_get
    bad = {t.id() for t, _ in result.failures + result.errors}
    tests = {}
    for tid in set(used) | bad | seen:
        tests[tid] = {'ok': tid not in bad, 'tools': used.get(tid, set())}
    return tests, result.testsRun, len(bad)


def gated_evidence():
    """Runtime-gated tests as evidence for their declared tools; a listed test that no longer exists is a failure."""
    out = {}
    for tid, info in RUNTIME_GATED_EVIDENCE.items():
        try:
            exists = unittest.defaultTestLoader.loadTestsFromName(tid).countTestCases() == 1
        except (AttributeError, ImportError):
            exists = False
        out[tid] = {'ok': exists, 'tools': set(info['tools']), 'gated_job': info['job']}
    return out


# --------------------------------------------------------------------------- build

def anchor_names_from_cases(cases):
    """flowengine anchor -> reference anchor names, per catalogue id (from conformance cases)."""
    m = defaultdict(dict)
    for c in cases:
        m[c['catalog_id']].update(c['outputs'])
        m[c['catalog_id']].update(c['reference']['input_anchors'])
    return m


def build(check=False):
    from flowengine.plugin import default_registry
    try:
        from cases import CASES
    except ImportError:
        CASES = []
    catalog_doc = json.loads(CATALOG.read_text(encoding='utf-8'))
    rows = catalog_doc['tools']
    registry = default_registry()
    impl = {}
    for t in registry.tools():
        for cid in t.manifest.catalog_ids:
            impl[cid] = t
    tests, ran, failed = run_tests_with_tool_map()
    tests.update(gated_evidence())
    tests_by_tool = defaultdict(list)
    for tid, info in sorted(tests.items()):
        for tool in info['tools']:
            tests_by_tool[tool].append((tid, info['ok']))
    obs = load_json(ROOT / 'data' / 'reference-observations.json', {'tools': []})
    obs_by_cid = defaultdict(list)
    for r in obs['tools']:
        for cid in r['catalog_ids']:
            obs_by_cid[cid].append(r)
    audit = load_json(ROOT / 'data' / 'public-doc-audit.json', {'pages': []})
    audit_by_url = {p['url']: p for p in audit['pages']}
    results_files = sorted(p for p in (ROOT / 'conformance' / 'results').glob('*/*/results.json')
                           if not p.parent.name.endswith('-samples'))  # holdout runs are workflow-level
    conf = defaultdict(list)       # primary profile (E1, incl. holdout) -> drives verification_status
    conf_other = defaultdict(list)  # other engine modes, recorded per contract
    conf_builds = []
    for f in results_files:
        doc = json.loads(f.read_text(encoding='utf-8'))
        mode = doc.get('engine_mode', 'e1')
        conf_builds.append(f"{doc['reference_build']}/{f.parent.name}")
        for r in doc['results']:
            rec = {**r, 'build': doc['reference_build'], 'mode': f.parent.name,
                   'file': f.relative_to(ROOT).as_posix()}
            (conf if mode == 'e1' else conf_other)[r['catalog_id']].append(rec)
    ref_anchor_map = anchor_names_from_cases(CASES)

    from flowengine import providers as _providers
    provider_notes = defaultdict(list)
    for prov in _providers.providers():
        for served in prov.serves:
            provider_notes[served].append(prov)

    ledger, changed = [], []
    for row in rows:
        cid = row['id']
        tool = impl.get(cid)
        deps = list(CATEGORY_DEPENDENCIES.get(row['category'], []))
        # blocking is decided by reviewed category rules only, never by unreviewed keyword signals
        blocking = any(d.startswith(BLOCKING) for d in deps)
        page = audit_by_url.get(row.get('documentation_url'))
        if page:
            for sig in page.get('dependency_signals', []):
                if sig in ('r-runtime', 'python-runtime', 'credentials', 'database-driver', 'licensed-dataset',
                           'llm-service', 'alteryx-server', 'vendor-cloud-service') and not any(sig in d for d in deps):
                    deps.append(f'{sig} (public-doc keyword signal; unreviewed)')
        adapter = APPLICATION_ADAPTERS.get(cid)
        tool_tests = tests_by_tool.get(tool.manifest.tool_id, []) if tool else []
        if adapter:
            tool_tests = [(tid, tests.get(tid, {}).get('ok', False)) for tid in adapter['test_ids']]
        if tool and tool_tests and all(ok for _, ok in tool_tests):
            impl_status = 'implemented'
        elif tool:
            impl_status = 'in-progress'
        else:
            impl_status = 'not-started'
        cases = conf.get(cid, [])
        if cases and all(c['status'] == 'passed' for c in cases):
            ver_status = 'partial'
        elif cases:
            ver_status = 'failed'
        else:
            ver_status = 'not-tested'
        o = obs_by_cid.get(cid, [])
        ref_obs = None
        if o:
            ref_obs = {'source': 'data/reference-observations.json', 'build': obs.get('reference_build'),
                       'tool_identifiers': sorted({r['tool_identifier'] for r in o}),
                       'input_anchors': sorted({a for r in o for a in r['input_anchors']}),
                       'output_anchors': sorted({a for r in o for a in r['output_anchors']}),
                       'config_elements': sorted({a for r in o for a in r['config_elements']}),
                       'nodes_observed': sum(r['nodes'] for r in o)}
        public = None
        if page:
            public = {'source': 'data/public-doc-audit.json', 'page_title': page.get('page_title'),
                      'sha256': page.get('sha256'), 'retrieved_utc': page.get('retrieved_utc'),
                      'anchors_extracted': page.get('anchors', []), 'anchor_extraction': page.get('anchor_extraction'),
                      'section_headings': [h['text'] for h in page.get('headings', []) if h['level'] in (2, 3)][:25]}
        rmap = ref_anchor_map.get(cid, {})
        if tool:
            m = tool.manifest
            inputs = [{'name': a.name, 'direction': 'input', 'cardinality': 'many' if a.multiple else
                       ('one' if a.required else 'zero-or-one'), 'provenance': 'flowengine-manifest',
                       'reference_name': rmap.get(a.name)} for a in m.inputs]
            outputs = [{'name': a.name, 'direction': 'output', 'cardinality': 'one', 'provenance': 'flowengine-manifest',
                        'reference_name': rmap.get(a.name)} for a in m.outputs]
            config_schema = m.config_schema
            implementation = {'engine': 'flowengine', 'tool_id': m.tool_id, 'version': m.version,
                              'module': type(tool).__module__, 'side_effects': m.side_effects}
            deterministic = m.deterministic
        else:
            src = 'reference-observed' if ref_obs else 'public-doc-extracted'
            ins = ref_obs['input_anchors'] if ref_obs else [a['label'] for a in (public or {}).get('anchors_extracted', [])
                                                            if a['direction_guess'] == 'input']
            outs = ref_obs['output_anchors'] if ref_obs else [a['label'] for a in (public or {}).get('anchors_extracted', [])
                                                              if a['direction_guess'] == 'output']
            inputs = [{'name': a, 'direction': 'input', 'cardinality': 'unknown', 'provenance': src} for a in ins]
            outputs = [{'name': a, 'direction': 'output', 'cardinality': 'unknown', 'provenance': src} for a in outs]
            config_schema = {'status': 'not-yet-specified',
                             'reference_config_elements': ref_obs['config_elements'] if ref_obs else [],
                             'public_doc_sections': (public or {}).get('section_headings', [])}
            implementation = None
            deterministic = None
        if adapter:
            # Interface execution is an importer/application adapter, not a row
            # kernel registered in the data-processing plugin registry.
            impl_status = ('in-progress' if not tool_tests or not all(ok for _, ok in tool_tests) or adapter['unsupported']
                           else 'implemented')
            implementation = {'engine': adapter.get('engine', 'Alteryx workflow import/application adapter'),
                              'tool_id': cid, 'module': adapter['module'],
                              'supported_variants': adapter['supported'],
                              'unimplemented_variants': adapter['unsupported'],
                              'unverified_variants': adapter['unverified']}
            config_schema = {'status': 'partially-specified',
                             'source': adapter['evidence'],
                             'supported_variants': adapter['supported'],
                             'unsupported_variants': adapter['unsupported'],
                             'unverified_variants': adapter['unverified'],
                             'preserved_runtime_xml': True}
        unresolved = []
        if not tool:
            unresolved.append('No independent row-processing kernel; this entry is implemented as a workflow application adapter.') if adapter else unresolved.append('No independent kernel yet; configuration semantics not specified.')
            if not inputs and not outputs:
                unresolved.append('Anchors unknown: not described in public page text and not observed in shipped workflows.')
        if blocking:
            unresolved.append('Exact behaviour needs separately licensed data/services; record authorised sandbox access before testing.')
        if tool and ver_status == 'not-tested':
            unresolved.append('No differential conformance case yet.')
        for prov in provider_notes.get(cid, []):
            if 'pending' in prov.verification.lower():
                unresolved.append(f'Provider {prov.id}: {prov.verification}')
        sources = [{'url': row['source_url'], 'evidence_type': 'public-tool-list', 'accessed_utc': catalog_doc['metadata'].get('source_retrieved_at_utc')}]
        if row.get('documentation_url'):
            sources.append({'url': row['documentation_url'], 'evidence_type': 'public-tool-help-page-metadata',
                            'accessed_utc': (page or {}).get('retrieved_utc')})
        if ref_obs:
            sources.append({'url': 'data/reference-observations.json', 'evidence_type': 'authorised-reference-structural-scan',
                            'accessed_utc': obs.get('scanned_utc')})
        for job in sorted({RUNTIME_GATED_EVIDENCE[t]['job'] for t, _ in tool_tests if t in RUNTIME_GATED_EVIDENCE}):
            sources.append({'url': f'.github/workflows/research-validation.yml#{job}', 'evidence_type': 'runtime-gated-ci-job',
                            'accessed_utc': None})
        if adapter:
            sources.append({'url': adapter['evidence'], 'evidence_type': adapter.get('evidence_type', 'authorised-reference-observation-and-project-authored-execution'),
                            'accessed_utc': catalog_doc['metadata'].get('source_retrieved_at_utc')})
        contract = {
            'id': cid, 'target_version': TARGET, 'name': row['name'], 'category': row['category'],
            'xml_tool_name': row.get('xml_tool_name'), 'user_role': row.get('user_role'),
            'documentation_url': row.get('documentation_url'),
            'status': impl_status if impl_status != 'not-started' else ('specified' if False else 'not-started'),
            'implementation_status': impl_status, 'verification_status': ver_status,
            'inputs': inputs, 'outputs': outputs, 'configuration_schema': config_schema,
            'deterministic': deterministic, 'external_dependencies': deps,
            'external_prerequisites_blocking': blocking,
            'license_notes': 'Independent implementation; no vendor code, assets or documentation text copied.',
            'implementation': implementation, 'reference_observation': ref_obs, 'public_doc': public,
            'independent_tests': {'count': len(tool_tests), 'passing': sum(ok for _, ok in tool_tests),
                                  'tests': [t for t, _ in tool_tests]},
            'sources': sources,
            'cases': [{'case_id': c['case_id'], 'status': c['status'],
                       'evidence': [f"{c['file']}#{c['case_id']}", f"reference build {c['build']} ({c['mode']})",
                                    f"config sha256 {c['config_digest']}"]} for c in cases + conf_other.get(cid, [])],
            'unresolved': unresolved,
        }
        path = ROOT / 'tool-contracts' / slug(row['category']) / f"{slug(row['name'])}.contract.json"
        text = json.dumps(contract, indent=2, ensure_ascii=False) + '\n'
        if not path.exists() or path.read_text(encoding='utf-8') != text:
            changed.append(path)
            if not check:
                path.write_text(text, encoding='utf-8')
        brief = path.with_name(f"{slug(row['name'])}.md")
        if brief.exists():
            b = brief.read_text(encoding='utf-8')
            nb = re.sub(r'- Implementation: \*\*[^*]+\*\*', f'- Implementation: **{impl_status}**', b)
            nb = re.sub(r'- Conformance verification: \*\*[^*]+\*\*', f'- Conformance verification: **{ver_status}**', nb)
            old_banner = '> **Research record; NOT a working tool implementation or verified specification.**'
            new_banner = ('> **Research record.** An independent kernel exists and its evidence is in the contract JSON; '
                          'this brief is NOT a verified specification or a parity claim.')
            nb = nb.replace(old_banner, new_banner) if impl_status == 'implemented' else nb.replace(new_banner, old_banner)
            if '- Machine-readable contract:' not in nb:
                nb = nb.replace('- Conformance verification:', f'- Machine-readable contract: `{path.name}`\n- Conformance verification:', 1)
            if nb != b:
                changed.append(brief)
                if not check:
                    brief.write_text(nb, encoding='utf-8')
        if row['implementation_status'] != impl_status or row['verification_status'] != ver_status:
            changed.append(CATALOG)
            row['implementation_status'] = impl_status
            row['verification_status'] = ver_status
        ledger_row = {'id': cid, 'name': row['name'], 'category': row['category'],
                       'kernel': tool.manifest.tool_id if tool else None,
                       'implementation_status': impl_status, 'verification_status': ver_status,
                       'independent_tests': contract['independent_tests']['count'],
                       'conformance_cases': len(cases),
                       'conformance_passed': sum(c['status'] == 'passed' for c in cases),
                       'e2_cases': len(conf_other.get(cid, [])),
                       'e2_passed': sum(c['status'] == 'passed' for c in conf_other.get(cid, [])),
                       'reference_observed': bool(ref_obs),
                       'anchors_known': bool(inputs or outputs),
                       'external_prerequisites_blocking': blocking}
        if adapter:
            ledger_row['adapter'] = adapter['module']
        ledger.append(ledger_row)
    status_doc = {
        'notes': 'Generated by scripts/build_status.py from evidence; see docs/TOOL_STATUS_RULES.md. '
                 '"partial" verification = every recorded differential case passes on the stated reference build; '
                 'it is not a full-parity claim.',
        'test_run': {'tests_run': ran, 'tests_failed': failed},
        'conformance_builds': conf_builds,
        'totals': summarize(ledger), 'tools': ledger}
    status_path = ROOT / 'data' / 'tool-status.json'
    stext = json.dumps(status_doc, indent=2, ensure_ascii=False) + '\n'
    if not status_path.exists() or status_path.read_text(encoding='utf-8') != stext:
        changed.append(status_path)
        if not check:
            status_path.write_text(stext, encoding='utf-8')
    if CATALOG in changed and not check:
        save_catalog(rows, catalog_doc['metadata'])
    return status_doc, sorted({str(p.relative_to(ROOT)) for p in changed})


def summarize(ledger):
    from collections import Counter
    return {
        'catalogue_entries': len(ledger),
        'kernels_registered': sum(1 for l in ledger if l['kernel']),
        'application_adapters': sum(1 for l in ledger if l.get('adapter')),
        'implementation_status': dict(sorted(Counter(l['implementation_status'] for l in ledger).items())),
        'verification_status': dict(sorted(Counter(l['verification_status'] for l in ledger).items())),
        'anchors_known': sum(l['anchors_known'] for l in ledger),
        'reference_observed': sum(l['reference_observed'] for l in ledger),
        'conformance_cases': sum(l['conformance_cases'] for l in ledger),
        'conformance_cases_passed': sum(l['conformance_passed'] for l in ledger),
        'e2_cases': sum(l['e2_cases'] for l in ledger),
        'e2_cases_passed': sum(l['e2_passed'] for l in ledger),
        'external_prerequisites_blocking': sum(l['external_prerequisites_blocking'] for l in ledger),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--check', action='store_true', help='Exit 1 if tracked outputs are stale')
    args = p.parse_args()
    doc, changed = build(check=args.check)
    print(json.dumps(doc['totals'], indent=2))
    print(f"tests: {doc['test_run']['tests_run']} run, {doc['test_run']['tests_failed']} failed")
    if args.check and changed:
        print('STALE:', *changed[:20], sep='\n  ')
        sys.exit(1)
    if doc['test_run']['tests_failed']:
        sys.exit(1)


if __name__ == '__main__':
    main()
