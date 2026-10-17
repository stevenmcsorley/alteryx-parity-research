"""OCR, Key-Value Pair Extraction, scanned-PDF OCR and Image Recognition on the real downloaded models.

Runs only where the components are installed (``python -m flowengine addons install ocr-models
image-model:mobilenet_v2``), as in the ``text-addons`` CI job.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from flowengine import addons  # noqa: E402
from flowengine.executor import run_workflow  # noqa: E402

from helpers import ROOT, edge, text_input, workflow  # noqa: E402

READY = all(addons.installed(c) for c in ('ocr-models', 'image-model:mobilenet_v2'))


def font(size):
    for name in ('DejaVuSans.ttf', 'Arial.ttf', 'arial.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf', 'C:/Windows/Fonts/arial.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def document(lines, size=(1000, 420)):
    img = Image.new('RGB', size, 'white')
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((50, 50 + i * 80), line, fill='black', font=font(30))
    return img


@unittest.skipUnless(READY, 'vision add-on components are not installed')
class VisionAddonsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def folder(self, name, *imgs):
        d = Path(self.tmp.name) / name
        d.mkdir()
        for i, img in enumerate(imgs):
            img.save(d / f'{i:03d}.png')
        return {'id': name, 'tool': 'core.image_input', 'config': {'path': str(d)}}

    def test_ocr_and_key_values(self):
        doc = document(['Invoice Number: INV-2026-0042', 'Company: Acme Trading Ltd', 'Total Due: 1,234.50', 'Café crème à Genève'])
        nodes = [self.folder('i', doc), text_input('k', [('key', 'v_wstring')], [['Invoice Number'], ['Total Due']]),
                 {'id': 'o', 'tool': 'core.image_to_text', 'config': {'image_field': 'Image', 'language': 'french'}},
                 {'id': 'kv', 'tool': 'core.key_value_pair_extraction', 'config': {'image_field': 'Image', 'key_field': 'key'}}]
        r = run_workflow(workflow(nodes, [edge('i', 'output', 'o', 'input'), edge('i', 'output', 'kv', 'input'),
                                          edge('k', 'output', 'kv', 'keys')]), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        text = r.output('o').column('text')[0].split('\n')
        self.assertEqual(text[0], 'Invoice Number: INV-2026-0042')
        self.assertEqual(text[3], 'Café crème à Genève')
        self.assertGreater(r.output('o').column('confidence')[0], 0.9)
        kv = r.output('kv').to_dicts()[0]
        self.assertEqual((kv['Invoice Number'], kv['Total Due']), ('INV-2026-0042', '1,234.50'))

    def test_scanned_pdf_pages_are_read_by_ocr(self):
        pdf = Path(self.tmp.name) / 'scan.pdf'
        document(['Scanned page heading', 'Amount 99.95']).save(pdf, 'PDF', resolution=100)
        nodes = [{'id': 'p', 'tool': 'core.pdf_to_text', 'config': {'path': str(pdf), 'mode': 'text_and_ocr', 'formats': ['lines']}}]
        r = run_workflow(workflow(nodes, []), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.output('p').column('Text')[:2], ['Scanned page heading', 'Amount 99.95'])

    def test_image_recognition_separates_two_kinds_of_picture(self):
        def picture(shape, colour, shift):
            img = Image.new('RGB', (256, 256), (235, 235, 235))
            d = ImageDraw.Draw(img)
            box = (40 + shift, 40, 200 + shift, 200)
            (d.ellipse if shape == 'circle' else d.rectangle)(box, fill=colour)
            return img
        train = [picture('circle', (200, 30, 30), s) for s in (0, 5, 10, 15, 20, 25)] + \
                [picture('square', (30, 30, 200), s) for s in (0, 5, 10, 15, 20, 25)]
        val = [picture('circle', (210, 40, 40), 8), picture('square', (40, 40, 210), 12)]
        label = "IIF(ToNumber(Left(Right([File], 7), 3)) < {n}, 'circle', 'square')"
        nodes = [self.folder('t', *train), self.folder('v', *val),
                 {'id': 'lt', 'tool': 'core.formula', 'config': {'formulas': [{'field': 'label', 'expression': label.format(n=6), 'type': 'v_wstring'}]}},
                 {'id': 'lv', 'tool': 'core.formula', 'config': {'formulas': [{'field': 'label', 'expression': label.format(n=1), 'type': 'v_wstring'}]}},
                 {'id': 'ir', 'tool': 'core.image_recognition', 'config': {'image_field': 'Image', 'label_field': 'label', 'epochs': 30}}]
        edges = [edge('t', 'output', 'lt', 'input'), edge('v', 'output', 'lv', 'input'),
                 edge('lt', 'output', 'ir', 'train'), edge('lv', 'output', 'ir', 'validation')]
        r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        overall = next(x for x in r.output('ir', 'evaluation').to_dicts() if x['Label'] == '(overall)')
        self.assertEqual(overall['Accuracy'], 1.0)


if __name__ == '__main__':
    unittest.main()
