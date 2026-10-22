"""Batch 12a: Computer Vision tools without downloads, checked against constructed images and PDFs."""
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from flowengine.executor import run_workflow

from helpers import ROOT, edge, run_tool, text_input, workflow


def png(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def text_image(lines, size=(900, 520)):
    img = Image.new('L', size, 255)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 28)
    except OSError:
        font = ImageFont.load_default(size=28)
    for i, line in enumerate(lines):
        d.text((60, 60 + i * 50), line, fill=0, font=font)
    return img


def make_pdf(path, pages):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=(612, 792))
    c.setFont('Helvetica', 12)
    for page in pages:
        for x, y, text in page:
            c.drawString(x, y, text)
        c.showPage()
    c.save()


TABLE_PAGE = [(72, 750, 'Quarterly report'), (72, 700, 'Item'), (250, 700, 'Qty'), (400, 700, 'Price'),
              (72, 680, 'Apples'), (250, 680, '3'), (400, 680, '1.20'), (72, 660, 'Pears'), (250, 660, '10'), (400, 660, '0.80')]


class ImageCase(unittest.TestCase):
    """Feeds images to tools through Image Input, from a temporary folder per node."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def images(self, nid, *imgs):
        folder = Path(self.tmp.name) / nid
        folder.mkdir()
        for i, img in enumerate(imgs):
            img.save(folder / f'{i:03d}.png', 'PNG')
        return {'id': nid, 'tool': 'core.image_input', 'config': {'path': str(folder)}}

    def run_on(self, tool, config, *imgs, anchor='input'):
        self.runs = getattr(self, 'runs', 0) + 1
        nodes = [self.images(f'src{self.runs}', *imgs), {'id': 't', 'tool': tool, 'config': config}]
        r = run_workflow(workflow(nodes, [edge(f'src{self.runs}', 'output', 't', anchor)]), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        return r.output('t').to_dicts()


class InputOutputTests(ImageCase):
    def test_folder_input_renders_pdf_pages_and_filters_types(self):
        with tempfile.TemporaryDirectory() as d:
            Image.new('RGB', (40, 30), (200, 10, 10)).save(Path(d) / 'b.png')
            Image.new('RGB', (20, 10)).save(Path(d) / 'a.jpg')
            make_pdf(Path(d) / 'c.pdf', [TABLE_PAGE, [(72, 750, 'two')]])
            (Path(d) / 'notes.txt').write_text('x')
            out, _ = run_tool('core.image_input', {'path': d, 'pdf_dpi': 72}, {})
            rows = out['output'].to_dicts()
            self.assertEqual([(Path(r['File']).name, r['Page'], r['Format']) for r in rows],
                             [('a.jpg', None, 'JPEG'), ('b.png', None, 'PNG'), ('c.pdf', 1, 'PNG'), ('c.pdf', 2, 'PNG')])
            self.assertEqual(Image.open(io.BytesIO(rows[2]['Image'])).size, (612, 792))  # 72 dpi = PDF points
            only, _ = run_tool('core.image_input', {'path': d, 'file_types': ['png']}, {})
            self.assertEqual(len(only['output']), 1)

    def test_output_writes_named_files_with_permission(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            for name in ('scan 1.png', 'other.png'):
                Image.new('RGBA', (10, 10), (0, 0, 255, 128)).save(Path(src) / name)
            nodes = [{'id': 'i', 'tool': 'core.image_input', 'config': {'path': src}},
                     {'id': 'f', 'tool': 'core.formula', 'config': {'formulas': [
                         {'field': 'name', 'expression': '"scan 1.png"', 'type': 'v_wstring'}]}},
                     {'id': 'o', 'tool': 'core.image_output', 'config': {'image_field': 'Image', 'name_field': 'name',
                                                                         'folder': dst, 'format': 'jpeg'}}]
            edges = [edge('i', 'output', 'f', 'input'), edge('f', 'output', 'o', 'input')]
            denied = run_workflow(workflow(nodes, edges), base_dir=ROOT)
            self.assertEqual(denied.error['code'], 'permission-denied')
            r = run_workflow(workflow(nodes, edges), base_dir=ROOT, limits={'allow_file_writes': True})
            self.assertTrue(r.ok, r.error)
            paths = [Path(p) for p in r.output('o').column('Path')]
            self.assertEqual([p.name for p in paths], ['scan_1.jpg', 'scan_1_2.jpg'])
            with Image.open(paths[0]) as written:
                self.assertEqual(written.mode, 'RGB')


class ProfileTests(ImageCase):
    def test_base_and_summary_statistics(self):
        img = Image.new('RGB', (2, 2))
        img.putdata([(0, 0, 0), (255, 255, 255), (255, 255, 255), (10, 20, 30)])
        r = self.run_on('core.image_profile', {'image_field': 'Image'}, img)[0]
        self.assertEqual((r['Mode'], r['Width'], r['Height'], r['Channels'], r['Min_Value'], r['Max_Value']), ('RGB', 2, 2, 3, 0, 255))
        self.assertEqual((r['Most_Freq_Color_RGB'], r['Most_Freq_Color_Hex']), ('[255, 255, 255]', '#ffffff'))
        self.assertEqual((r['Dark_Pixel_count'], r['Bright_Pixel_count']), (2, 2))
        self.assertEqual(r['Pixel_Count'], 4)
        np.testing.assert_allclose(json.loads(r['Pixel_Mean']), [130.0, 132.5, 135.0])
        np.testing.assert_allclose(json.loads(r['Pixel_Sum']), [520, 530, 540])

    def test_exif_gps(self):
        from PIL.ExifTags import IFD
        img = Image.new('RGB', (8, 8))
        exif = img.getexif()
        exif[0x010F] = 'TestCam'
        exif.get_ifd(IFD.GPSInfo).update({1: 'N', 2: (51.0, 30.0, 0.0), 3: 'W', 4: (0.0, 7.0, 30.0), 6: 35.0})
        folder = Path(self.tmp.name) / 'exif'
        folder.mkdir()
        img.save(folder / 'gps.jpg', 'JPEG', exif=exif)
        nodes = [{'id': 'src', 'tool': 'core.image_input', 'config': {'path': str(folder)}},
                 {'id': 't', 'tool': 'core.image_profile', 'config': {'image_field': 'Image', 'profiles': ['exif']}}]
        res = run_workflow(workflow(nodes, [edge('src', 'output', 't', 'input')]), base_dir=ROOT)
        self.assertTrue(res.ok, res.error)
        r = res.output('t').to_dicts()[0]
        self.assertAlmostEqual(r['Latitude'], 51.5)
        self.assertAlmostEqual(r['Longitude'], -0.125)
        self.assertAlmostEqual(r['Altitude'], 35.0)
        self.assertEqual(json.loads(r['Exif'])['Make'], 'TestCam')


class ProcessingTests(ImageCase):
    def test_deskew_recovers_the_rotation(self):
        from flowengine.tools.vision import deskew_angle
        base = text_image([f'Invoice line {i}: widgets and gadgets {i * 7}' for i in range(8)])
        for angle in (-12, 5):
            self.assertAlmostEqual(deskew_angle(base.rotate(angle, expand=True, fillcolor=255)), -angle, delta=0.5)

    def test_steps(self):
        img = Image.new('RGB', (100, 50), (255, 255, 255))
        ImageDraw.Draw(img).rectangle((0, 0, 9, 9), fill=(0, 0, 0))
        steps = [{'op': 'crop', 'width': 50, 'height': 50, 'unit': 'percent', 'anchor': 'top_left'},
                 {'op': 'scale', 'width': 100, 'unit': 'px'}, {'op': 'threshold', 'method': 'binary', 'value': 127}]
        res = Image.open(io.BytesIO(self.run_on('core.image_processing', {'image_field': 'Image', 'steps': steps}, img)[0]['image_processed']))
        self.assertEqual(res.size, (100, 50))  # 50x25 crop doubled, aspect kept
        arr = np.asarray(res)
        self.assertTrue(set(np.unique(arr)) <= {0, 255})
        self.assertEqual(arr[5, 5].tolist(), [0, 0, 0])
        ocr = self.run_on('core.image_processing', {'image_field': 'Image', 'steps': [{'op': 'ocr_optimization'}]}, text_image(['Hello world']))
        self.assertEqual(set(np.unique(np.asarray(Image.open(io.BytesIO(ocr[0]['image_processed']))))), {0, 255})

    def test_shift_realigns_to_a_template_anchor(self):
        page = text_image(['ACME LTD   INVOICE', 'Total: 42.00'], size=(600, 300))
        moved = Image.new('L', page.size, 255)
        moved.paste(page, (17, -9))
        nodes = [self.images('t', page), self.images('d', moved),
                 {'id': 'tpl', 'tool': 'core.image_template', 'config': {'image_field': 'Image', 'annotations': [
                     {'label': 'logo', 'x': 55, 'y': 55, 'width': 160, 'height': 40}]}},
                 {'id': 'p', 'tool': 'core.image_processing', 'config': {'image_field': 'Image', 'steps': [{'op': 'shift', 'label': 'logo'}]}}]
        edges = [edge('t', 'output', 'tpl', 'input'), edge('tpl', 'output', 'p', 'template'), edge('d', 'output', 'p', 'input')]
        r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        fixed = np.asarray(Image.open(io.BytesIO(r.output('p').column('image_processed')[0])), dtype=float)
        original = np.asarray(page, dtype=float)
        self.assertLess(np.abs(fixed[40:260, 40:560] - original[40:260, 40:560]).mean(), 1.0)


class BarcodeTests(ImageCase):
    def test_generate_then_read_round_trip(self):
        gen, _ = run_tool('core.barcode', {'mode': 'generate', 'text_field': 'text', 'types': ['qr', 'code128']},
                          {'input': ([('text', 'v_wstring')], [['HELLO-42']])})
        row = gen['output'].to_dicts()[0]
        qr, c128 = (Image.open(io.BytesIO(row[k])) for k in ('QR', 'Code128'))
        # Keep native barcode generation in a clean process; zxing-cpp's Windows
        # writer can access-violate after other imaging extensions are loaded.
        ean = Image.open(Path(__file__).parent / 'fixtures' / 'barcode_ean13.png')
        blank = Image.new('L', (200, 200), 255)
        rows = self.run_on('core.barcode', {'mode': 'read', 'image_field': 'Image', 'cropped': True, 'bounded': True},
                           qr, c128, ean, blank)
        self.assertEqual([(r['decoded'], r['type']) for r in rows],
                         [('HELLO-42', 'QR Code'), ('HELLO-42', 'Code 128'), ('9780306406157', 'ISBN-13'), (None, None)])
        self.assertIsNotNone(rows[0]['cropped_barcode'])
        self.assertEqual(Image.open(io.BytesIO(rows[0]['bounded_image'])).mode, 'RGB')


class PDFAndTemplateTests(unittest.TestCase):
    def test_formats_page_range_and_risk(self):
        with tempfile.TemporaryDirectory() as d:
            make_pdf(Path(d) / 'r.pdf', [TABLE_PAGE, [(72, 750, 'Page two body text that is long enough to count as text content.')]])
            out, _ = run_tool('core.pdf_to_text', {'path': str(Path(d) / 'r.pdf'), 'formats': ['lines', 'table'], 'risk_score': True,
                                                   'end_page': 1}, {})
            rows = out['output'].to_dicts()
            lines = [r['Text'] for r in rows if r['Format'] == 'Lines']
            self.assertEqual(lines, ['Quarterly report', 'Item Qty Price', 'Apples 3 1.20', 'Pears 10 0.80'])
            table = [[r['Column_1'], r['Column_2'], r['Column_3']] for r in rows if r['Format'] == 'Table']
            self.assertEqual(table[1:], [['Item', 'Qty', 'Price'], ['Apples', '3', '1.20'], ['Pears', '10', '0.80']])
            self.assertEqual({r['Page'] for r in rows}, {1})
            self.assertEqual({r['RiskScore'] for r in rows}, {'low'})

    def test_template_regions_scale_to_the_page(self):
        with tempfile.TemporaryDirectory() as d:
            make_pdf(Path(d) / 'r.pdf', [TABLE_PAGE])
            # template rendered at 144 dpi: PDF point (x, y_top) is pixel (2x, 2(792 - y_top))
            annotations = [{'label': 'title', 'x': 140, 'y': 2 * (792 - 762), 'width': 300, 'height': 40},
                           {'label': 'prices', 'type': 'table', 'x': 790, 'y': 2 * (792 - 712), 'width': 120, 'height': 140},
                           {'label': 'snap', 'type': 'image', 'x': 0, 'y': 0, 'width': 100, 'height': 100}]
            nodes = [{'id': 'tpl', 'tool': 'core.image_template', 'config': {'template_path': str(Path(d) / 'r.pdf'), 'pdf_dpi': 144,
                                                                              'annotations': annotations}},
                     {'id': 'p', 'tool': 'core.pdf_to_text', 'config': {'path': str(Path(d) / 'r.pdf')}}]
            r = run_workflow(workflow(nodes, [edge('tpl', 'output', 'p', 'template')]), base_dir=ROOT)
            self.assertTrue(r.ok, r.error)
            rows = r.output('p').to_dicts()
            by = {}
            for row in rows:
                by.setdefault(row['Region'], []).append(row)
            self.assertEqual(by['title'][0]['Text'], 'Quarterly report')
            self.assertEqual([x['Text'] for x in by['prices']], ['Price', '1.20', '0.80'])
            self.assertIsNotNone(by['snap'][0]['PageImage'])
            tpl = r.output('tpl').to_dicts()
            self.assertEqual((tpl[0]['TemplateWidth'], tpl[0]['TemplateHeight']), (1224, 1584))


if __name__ == '__main__':
    unittest.main()
