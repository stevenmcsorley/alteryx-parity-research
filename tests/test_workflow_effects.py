"""Permission preflight must find side effects inside every macro form."""
import json
import tempfile
import unittest
from pathlib import Path

from flowengine.service import EngineService
from flowengine.workflow_effects import workflow_effects


def macro(node_id, tool_id, config):
    return {'id': node_id, 'tool': tool_id, 'config': config}


def workflow(nodes):
    return {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': nodes, 'edges': []}


class WorkflowEffectsTests(unittest.TestCase):
    def test_embedded_batch_and_iterative_macros_report_nested_blob_output(self):
        leaf = workflow([macro('writer', 'core.blob_output', {'path': 'out.bin'})])
        middle = workflow([macro('loop', 'flowengine.iterative_macro', {'workflow': leaf})])
        root = workflow([macro('batch', 'flowengine.batch_macro', {'workflow': middle})])

        result = workflow_effects(root, Path.cwd())

        self.assertTrue(result['complete'], result['unresolved'])
        self.assertEqual(len(result['file_writes']), 1)
        self.assertEqual(result['file_writes'][0]['path'], ['batch', 'loop', 'writer'])

    def test_file_backed_macro_effect_is_available_through_permission_rpc(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'child.flow.json').write_text(json.dumps(workflow([
                macro('writer', 'core.blob_output', {'path': 'out.bin'})
            ])), encoding='utf-8')
            root = workflow([macro('nested', 'flowengine.macro', {'definition_path': 'child.flow.json'})])
            response = EngineService().handle('workflow.permissions', {'workflow': root, 'base_dir': str(base)})

            self.assertTrue(response['complete'], response['unresolved'])
            self.assertEqual(response['file_writes'][0]['path'], ['nested', 'writer'])

    def test_macro_cycles_terminate_without_hiding_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cycle = workflow([
                macro('writer', 'core.blob_output', {'path': 'out.bin'}),
                macro('again', 'flowengine.macro', {'definition_path': 'cycle.flow.json'}),
            ])
            (base / 'cycle.flow.json').write_text(json.dumps(cycle), encoding='utf-8')
            root = workflow([macro('entry', 'flowengine.macro', {'definition_path': 'cycle.flow.json'})])

            result = workflow_effects(root, base)

            self.assertTrue(result['complete'], result['unresolved'])
            self.assertEqual(len(result['file_writes']), 1)
            self.assertEqual(result['file_writes'][0]['path'], ['entry', 'writer'])

    def test_unresolvable_child_fails_closed(self):
        root = workflow([macro('missing', 'flowengine.macro', {'definition_path': 'absent.flow.json'})])

        result = workflow_effects(root, Path.cwd())

        self.assertFalse(result['complete'])
        self.assertTrue(result['unresolved'])
        self.assertEqual(result['file_writes'], [])

    def test_unavailable_tool_makes_permission_scan_incomplete(self):
        result = workflow_effects(workflow([macro('unknown', 'catalog:unimplemented', {})]), Path.cwd())
        self.assertFalse(result['complete'])
        self.assertEqual(result['unresolved'][0]['path'], ['unknown'])


if __name__ == '__main__':
    unittest.main()
