import unittest
from pathlib import Path

from flowengine.types import FieldType
from helpers import text_input, workflow, edge
from flowengine.executor import run_workflow


class BlobToolsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_output(self, cfg, codes_payloads, allow=False):
        nodes = [
            text_input('rows', [('Code', 'v_wstring'), ('PayloadText', 'v_wstring')], codes_payloads),
            {'id': 'convert', 'tool': 'core.blob_convert', 'config': {
                'direction': 'to_blob', 'field': 'PayloadText', 'output_field': 'Payload', 'encoding': 'base64'}},
            {'id': 'sink', 'tool': 'core.blob_output', 'config': cfg},
        ]
        edges = [edge('rows', 'output', 'convert', 'input'), edge('convert', 'output', 'sink', 'input')]
        return run_workflow(workflow(nodes, edges), base_dir=self.root,
                            limits={'allow_file_writes': allow})

    def test_blob_input_reads_binary_and_emits_bounded_chunks(self):
        data = bytes(range(256)) * 10
        source = self.root / 'sample.bin'
        source.write_bytes(data)
        doc = workflow([{'id': 'blob', 'tool': 'core.blob_input', 'config': {
            'mode': 'file', 'path': 'sample.bin', 'split_blobs': True, 'chunk_size_kb': 1,
            'max_blob_size_mb': 1, 'blob_field': 'Payload'}}], [], name='Blob input')
        result = run_workflow(doc, base_dir=self.root)
        self.assertTrue(result.ok, result.error)
        table = result.outputs['blob']['output']
        self.assertEqual(table.schema.field('Payload').type, FieldType.BLOB)
        self.assertEqual(table.schema.field('BlobNumber').type, FieldType.INT64)
        self.assertEqual(table.column('BlobNumber'), [1, 2, 3])
        self.assertEqual(b''.join(table.column('Payload')), data)

    def test_blob_input_pass_through_preserves_rows_and_reads_each_path(self):
        (self.root / 'a.bin').write_bytes(b'\x00a')
        (self.root / 'b.bin').write_bytes(b'\xffb')
        from helpers import workflow as make_workflow, edge as make_edge
        doc = make_workflow([
            text_input('paths', [('File', 'v_wstring')], [['a.bin'], ['b.bin']]),
            {'id': 'blob', 'tool': 'core.blob_input', 'config': {
                'mode': 'pass_through', 'filename_field': 'File', 'blob_field': 'Content'}},
        ], [make_edge('paths', 'output', 'blob', 'input')])
        result = run_workflow(doc, base_dir=self.root)
        self.assertTrue(result.ok, result.error)
        table = result.outputs['blob']['output']
        self.assertEqual(table.column('File'), ['a.bin', 'b.bin'])
        self.assertEqual(table.column('Content'), [b'\x00a', b'\xffb'])

    def test_blob_output_requires_consent_and_writes_per_record_files(self):
        cfg = {'path': 'image.png', 'blob_field': 'Payload', 'modify_method': 'append',
               'filename_field': 'Code', 'if_exists': 'error'}
        denied = self.run_output(cfg, [['A', 'b25l'], ['B', 'dHdv']])
        self.assertEqual(denied.status, 'failed')
        self.assertEqual(list(self.root.glob('image*.png')), [])
        result = self.run_output(cfg, [['A', 'b25l'], ['B', 'dHdv']], allow=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.root / 'imageA.png').read_bytes(), b'one')
        self.assertEqual((self.root / 'imageB.png').read_bytes(), b'two')
        self.assertEqual(result.manifest['nodes']['sink']['tool'], 'core.blob_output')

    def test_blob_output_rejects_traversal_and_preexisting_destination(self):
        cfg = {'path': 'image.png', 'blob_field': 'Payload', 'modify_method': 'replace_name',
               'filename_field': 'Code'}
        traversal = self.run_output(cfg, [['..\\escape', 'eA==']], allow=True)
        self.assertFalse(traversal.ok)
        self.assertFalse((self.root.parent / 'escape.png').exists())

        (self.root / 'image.png').write_bytes(b'original')
        plain = {'path': 'image.png', 'blob_field': 'Payload'}
        existing = self.run_output(plain, [['', 'Y2hhbmdlZA==']], allow=True)
        self.assertFalse(existing.ok)
        self.assertEqual((self.root / 'image.png').read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
