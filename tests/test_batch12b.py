"""Batch 12b: OCR, Key-Value Pair Extraction and Image Recognition plumbing (canned worker replies).

The real PaddleOCR and ImageNet models are exercised by tests/integration/test_vision_addons_integration.py.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import flowengine.addons as addons
from flowengine.executor import run_workflow
from flowengine.workflow_effects import workflow_effects

from helpers import ROOT, edge, text_input, workflow


def word(text, x, y, w=None, h=20):
    w = w or 12 * len(text)
    return {'text': text, 'score': 0.99, 'box': [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]}


class VisionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def install(self, *components):
        exe = addons.env_python()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text('')
        for c in addons.with_requirements(components):
            if c in addons.MODEL_FILES:
                for name in addons.MODEL_FILES[c]['files']:
                    (addons.model_dir(c) / name).parent.mkdir(parents=True, exist_ok=True)
                    (addons.model_dir(c) / name).write_text('x')
            addons._marker(c).parent.mkdir(parents=True, exist_ok=True)
            addons._marker(c).write_text('{}')

    def images(self, name, *colors, size=(200, 100)):
        folder = Path(self.tmp.name) / name
        folder.mkdir()
        for i, c in enumerate(colors):
            Image.new('RGB', size, c).save(folder / f'{i:03d}.png')
        return {'id': name, 'tool': 'core.image_input', 'config': {'path': str(folder)}}

    def run_with(self, replies, nodes, edges):
        seen = []

        def fake(ctx, script, request, timeout=1800):
            seen.append((script, request))
            return {'results': replies(script, request)}
        with mock.patch.object(addons, 'run_worker', fake):
            return run_workflow(workflow(nodes, edges), base_dir=ROOT), seen


class OCRTests(VisionCase):
    def test_lines_in_reading_order_and_confidence(self):
        self.install('ocr-models')
        page = [word('world', 120, 12), word('Hello', 10, 10), word('Second', 10, 60), word('line', 100, 62)]
        r, seen = self.run_with(lambda s, req: [page for _ in req['images']], [self.images('i', 'white'),
                                {'id': 'o', 'tool': 'core.image_to_text', 'config': {'image_field': 'Image'}}],
                                [edge('i', 'output', 'o', 'input')])
        self.assertTrue(r.ok, r.error)
        out = r.output('o').to_dicts()[0]
        self.assertEqual(out['text'], 'Hello world\nSecond line')
        self.assertAlmostEqual(out['confidence'], 0.99)
        self.assertEqual(seen[0][0], 'ocr_worker.py')
        self.assertTrue(seen[0][1]['rec'].endswith('rec.onnx'))

    def test_template_regions(self):
        self.install('ocr-models')
        nodes = [self.images('t', 'white', size=(400, 200)), self.images('d', 'white', size=(800, 400)),
                 {'id': 'tpl', 'tool': 'core.image_template', 'config': {'image_field': 'Image', 'annotations': [
                     {'label': 'name', 'x': 0, 'y': 0, 'width': 200, 'height': 50},
                     {'label': 'rows', 'type': 'table', 'x': 0, 'y': 100, 'width': 400, 'height': 100},
                     {'label': 'logo', 'type': 'image', 'x': 300, 'y': 0, 'width': 100, 'height': 100}]}},
                 {'id': 'o', 'tool': 'core.image_to_text', 'config': {'image_field': 'Image'}}]
        edges = [edge('t', 'output', 'tpl', 'input'), edge('d', 'output', 'o', 'input'), edge('tpl', 'output', 'o', 'template')]
        replies = iter([[[word('ACME', 5, 5)], [word('a', 5, 5), word('b', 5, 40)]]])
        r, seen = self.run_with(lambda s, req: next(replies), nodes, edges)
        self.assertTrue(r.ok, r.error)
        rows = [(x['Region'], x['Line'], x['Text'], x['RegionImage'] is not None) for x in r.output('o').to_dicts()]
        self.assertEqual(rows, [('name', None, 'ACME', False), ('rows', 1, 'a', False), ('rows', 2, 'b', False), ('logo', None, None, True)])
        crops = [Image.open(p) for p in seen[0][1]['images']] if False else None
        del crops
        logo = next(x for x in r.output('o').to_dicts() if x['Region'] == 'logo')
        import io
        self.assertEqual(Image.open(io.BytesIO(logo['RegionImage'])).size, (200, 200))  # scaled 2x from the template

    def test_preflight_and_missing_models(self):
        doc = workflow([{'id': 'o', 'tool': 'core.image_to_text', 'config': {'image_field': 'Image'}},
                        {'id': 'r', 'tool': 'core.image_recognition', 'config': {'image_field': 'i', 'label_field': 'l', 'model': 'resnet50_v2'}},
                        {'id': 'p', 'tool': 'core.pdf_to_text', 'config': {'path': 'x.pdf', 'mode': 'text_and_ocr'}}], [])
        found = workflow_effects(doc, ROOT)
        self.assertEqual([d['id'] for d in found['downloads']],
                         ['neural-runtime', 'vision-runtime', 'ocr-models', 'image-model:resnet50_v2'])
        self.assertEqual(found['downloads'][2]['nodes'], ['o', 'p'])
        r = run_workflow(workflow([self.images('i', 'white'), {'id': 'o', 'tool': 'core.image_to_text', 'config': {'image_field': 'Image'}}],
                                  [edge('i', 'output', 'o', 'input')]), base_dir=ROOT)
        self.assertEqual(r.error['code'], 'addon-missing')


class KeyValueTests(VisionCase):
    def test_values_after_keys_below_keys_and_repeats(self):
        from flowengine.tools.vision_ai import find_values, ocr_lines
        lines = ocr_lines([word('Invoice', 10, 10), word('Numbr:', 100, 10), word('INV-42', 200, 10),
                           word('Total', 10, 60), word('Due', 80, 60), word('Company', 10, 110), word('Acme Ltd', 12, 140),
                           word('Total', 10, 190), word('Due', 80, 190), word('£12.50', 150, 190)])
        self.assertEqual(find_values(lines, 'Invoice Number'), ['INV-42'])  # OCR typo still matches
        self.assertEqual(find_values(lines, 'Company'), ['Acme Ltd'])  # value on the line below
        self.assertEqual(find_values(lines, 'Total Due'), ['Company', '£12.50'])
        self.assertEqual(find_values(lines, 'Shipping'), [])

    def test_tool_columns(self):
        self.install('ocr-models')
        page = [word('Company:', 10, 10), word('Acme', 120, 10), word('Ref', 10, 60), word('A1', 60, 60),
                word('Ref', 10, 110), word('B2', 60, 110)]
        nodes = [self.images('i', 'white', 'white'), text_input('k', [('key', 'v_wstring')], [['Company'], ['Ref']]),
                 {'id': 'kv', 'tool': 'core.key_value_pair_extraction', 'config': {'image_field': 'Image', 'key_field': 'key'}}]
        replies = lambda s, req: [page, page[:2]]
        r, _ = self.run_with(replies, nodes, [edge('i', 'output', 'kv', 'input'), edge('k', 'output', 'kv', 'keys')])
        self.assertTrue(r.ok, r.error)
        rows = [{k: x[k] for k in ('Company', 'Ref', 'Ref2')} for x in r.output('kv').to_dicts()]
        self.assertEqual(rows, [{'Company': 'Acme', 'Ref': 'A1', 'Ref2': 'B2'}, {'Company': 'Acme', 'Ref': None, 'Ref2': None}])
        bad = run_workflow(workflow(nodes[:1] + [text_input('k', [('key', 'v_wstring')], [['Company'], [None]])] + nodes[2:],
                                    [edge('i', 'output', 'kv', 'input'), edge('k', 'output', 'kv', 'keys')]), base_dir=ROOT)
        self.assertIn("This key isn't supported", bad.error['message'])


class ImageRecognitionTests(VisionCase):
    def test_trains_on_backbone_scores_and_predicts(self):
        self.install('image-model:mobilenet_v2')
        rng = np.random.default_rng(0)

        def scores(script, req):
            out = []
            for path in req['images']:
                red = Image.open(path).getpixel((0, 0))[0] > 127
                v = rng.normal(size=1000)
                v[7 if red else 400] += 8
                out.append(v.tolist())
            return out
        reds, blues = ['#e01010'] * 8, ['#1010e0'] * 8
        nodes = [self.images('t', *(reds + blues)), self.images('v', '#e02020', '#2020e0'), self.images('n', '#ff0000'),
                 {'id': 'lab', 'tool': 'core.formula', 'config': {'formulas': [
                     {'field': 'label', 'expression': 'IIF(ToNumber(Left(Right([File], 7), 3)) < 8, "red", "blue")', 'type': 'v_wstring'}]}},
                 {'id': 'vlab', 'tool': 'core.formula', 'config': {'formulas': [
                     {'field': 'label', 'expression': 'IIF(ToNumber(Left(Right([File], 7), 3)) = 0, "red", "blue")', 'type': 'v_wstring'}]}},
                 {'id': 'ir', 'tool': 'core.image_recognition', 'config': {'image_field': 'Image', 'label_field': 'label', 'epochs': 20}},
                 {'id': 'p', 'tool': 'core.ml_predict', 'config': {}}]
        edges = [edge('t', 'output', 'lab', 'input'), edge('v', 'output', 'vlab', 'input'), edge('lab', 'output', 'ir', 'train'),
                 edge('vlab', 'output', 'ir', 'validation'), edge('ir', 'model', 'p', 'model'), edge('n', 'output', 'p', 'input')]
        r, seen = self.run_with(scores, nodes, edges)
        self.assertTrue(r.ok, r.error)
        ev = {x['Label']: x for x in r.output('ir', 'evaluation').to_dicts()}
        self.assertEqual(ev['(overall)']['Accuracy'], 1.0)
        self.assertEqual(r.output('p').column('Prediction'), ['red'])
        self.assertEqual(len(r.output('ir', 'report')), 2)
        self.assertTrue(seen[0][1]['model'].endswith('model.onnx'))
        info = json.loads(r.output('ir', 'model').column('Info')[0])
        self.assertEqual((info['backbone'], len(info['history'])), ('mobilenet_v2', 20))


if __name__ == '__main__':
    unittest.main()
