"""Reporting: elements carry computed content and Render writes real HTML/PDF documents."""
import base64
import io
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_file, run_workflow

from helpers import ROOT, edge, run_tool, text_input, workflow

SALES = ([('region', 'v_wstring'), ('month', 'v_wstring'), ('day', 'date'),
          {'name': 'amount', 'type': 'fixed_decimal', 'size': 12, 'scale': 2}, ('units', 'int32')],
         [['North', 'Jan', '2026-01-05', '1200.50', 10], ['North', 'Feb', '2026-02-02', '980.25', 8],
          ['South', 'Jan', '2026-01-12', '2500.00', 20], ['South', 'Feb', '2026-02-09', '3100.75', 25]])


def element(table, row=0):
    return json.loads(table.column('Report')[row])


def pdf_text(data: bytes) -> str:
    """Decompressed content-stream text of a PDF (enough to find Helvetica text operators)."""
    out = []
    for header, raw in re.findall(rb'<<((?:(?!<<).)*?)>>\s*stream\r?\n(.*?)\s*endstream', data, re.S):
        if b'ASCII85Decode' in header:  # reportlab wraps compressed streams in ASCII85
            raw = base64.a85decode(raw.strip().removesuffix(b'~>'))
        if b'FlateDecode' in header:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                continue
        out.append(raw.decode('latin-1', 'replace'))
    return '\n'.join(out)


def png_bytes(fmt='PNG', size=(40, 20)):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', size, (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


class ElementTests(unittest.TestCase):
    def test_table_formats_groups_and_totals(self):
        cfg = {'group_field': 'region', 'title': 'Sales', 'columns': [
            {'field': 'month', 'label': 'Month'}, {'field': 'amount', 'label': 'Amount', 'total': True},
            {'field': 'units', 'format': 'integer', 'total': True}]}
        out, _ = run_tool('core.report_table', cfg, {'input': SALES})
        self.assertEqual(out['output'].column('region'), ['North', 'South'])
        south = element(out['output'], 1)
        self.assertEqual(south['rows'], [['Jan', '2,500.00', '20'], ['Feb', '3,100.75', '25']])
        self.assertEqual(south['total_row'], ['Total', '5,600.75', '45'])
        self.assertEqual([c['align'] for c in south['columns']], ['left', 'right', 'right'])
        pct, _ = run_tool('core.report_table', {'columns': [{'field': 'x', 'format': 'percent', 'decimals': 1}]},
                          {'input': ([('x', 'double')], [[0.256], [None]])})
        self.assertEqual(element(pct['output'])['rows'], [['25.6%'], ['']])
        with self.assertRaisesRegex(WorkflowValidationError, 'totals need numeric'):
            run_tool('core.report_table', {'columns': [{'field': 'region', 'total': True}]}, {'input': SALES})

    def test_interactive_chart_aggregates_engine_data(self):
        out, _ = run_tool('core.interactive_chart', {'chart_type': 'bar', 'x_field': 'region', 'y_fields': ['amount'],
                                                     'aggregation': 'sum'}, {'input': SALES})
        spec = element(out['output'])['chart']
        self.assertEqual(spec['categories'], ['North', 'South'])
        self.assertAlmostEqual(spec['series']['amount'][1], 5600.75)
        root = ET.fromstring(out['output'].column('Chart')[0])
        self.assertTrue(root.tag.endswith('svg'))
        split, _ = run_tool('core.interactive_chart', {'chart_type': 'line', 'x_field': 'month', 'y_fields': ['units'],
                                                       'series_field': 'region', 'sort': 'none'}, {'input': SALES})
        spec = element(split['output'])['chart']
        self.assertEqual((spec['categories'], sorted(spec['series'])), (['Jan', 'Feb'], ['North', 'South']))
        self.assertEqual(spec['series']['South'], [[0, 20.0], [1, 25.0]])
        ts, _ = run_tool('core.interactive_chart', {'chart_type': 'timeseries', 'x_field': 'day', 'y_fields': ['units'],
                                                    'aggregation': 'none'}, {'input': SALES})
        spec = element(ts['output'])['chart']
        self.assertTrue(spec['time_axis'])
        self.assertEqual(len(spec['series']['units']), 4)
        hist, _ = run_tool('core.interactive_chart', {'chart_type': 'histogram', 'x_field': 'units', 'bins': 3}, {'input': SALES})
        self.assertEqual(sum(element(hist['output'])['chart']['counts']), 4)
        dup = run_workflow(workflow([text_input('t', SALES[0], SALES[1]),
                                     {'id': 'c', 'tool': 'core.interactive_chart',
                                      'config': {'chart_type': 'line', 'x_field': 'region', 'y_fields': ['units'], 'aggregation': 'none'}}],
                                    [edge('t', 'output', 'c', 'input')]), base_dir=ROOT)
        self.assertIn('repeat', dup.error['message'])

    def test_report_text_placeholders(self):
        out, _ = run_tool('core.report_text', {'text': 'Total for {{region}}: {{ units }} units', 'mode': 'per_record',
                                               'style': 'heading'}, {'input': SALES})
        self.assertEqual(element(out['output'], 2)['text'], 'Total for South: 20 units')
        self.assertEqual(out['output'].column('region')[0], 'North')  # records pass through in per_record mode
        with self.assertRaisesRegex(WorkflowValidationError, 'placeholder'):
            run_tool('core.report_text', {'text': '{{missing}}', 'mode': 'first_record'}, {'input': SALES})
        static, _ = run_tool('core.report_text', {'text': 'Hello', 'style': 'title', 'align': 'center'}, {})
        self.assertEqual(element(static['output'])['align'], 'center')

    def test_images_are_validated_and_normalised(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'logo.bmp').write_bytes(png_bytes('BMP'))
            Path(d, 'bad.png').write_bytes(b'not an image')
            out, _ = run_tool('core.report_image', {'path': 'logo.bmp', 'display_width': 72}, {}, base_dir=Path(d))
            img = element(out['output'])
            self.assertEqual((img['image_format'], img['width'], img['height'], img['display_width']), ('png', 40, 20, 72))
            doc = workflow([{'id': 'i', 'tool': 'core.report_image', 'config': {'path': 'bad.png'}}], [])
            self.assertIn('Not a supported image', run_workflow(doc, base_dir=Path(d)).error['message'])


class RenderTests(unittest.TestCase):
    def build(self, d, path, **render):
        nodes = [text_input('t', SALES[0], SALES[1]),
                 {'id': 'title', 'tool': 'core.report_text', 'config': {'text': 'Quarterly <sales> & margins', 'style': 'title'}},
                 {'id': 'table', 'tool': 'core.report_table', 'config': {'columns': [{'field': 'region'}, {'field': 'amount', 'total': True}]}},
                 {'id': 'chart', 'tool': 'core.interactive_chart', 'config': {'chart_type': 'bar', 'x_field': 'region', 'y_fields': ['amount']}},
                 {'id': 'img', 'tool': 'core.report_image', 'config': {'path': 'logo.png'}},
                 {'id': 'side', 'tool': 'core.report_layout', 'config': {'direction': 'horizontal'}},
                 {'id': 'page', 'tool': 'core.report_layout', 'config': {'title': 'Overview'}},
                 {'id': 'out', 'tool': 'core.render', 'config': {'path': path, **render}}]
        edges = [edge('t', 'output', 'table', 'input'), edge('t', 'output', 'chart', 'input'),
                 edge('table', 'output', 'side', 'inputs'), edge('img', 'output', 'side', 'inputs'),
                 edge('title', 'output', 'page', 'inputs'), edge('side', 'output', 'page', 'inputs'),
                 edge('chart', 'output', 'page', 'inputs'), edge('page', 'output', 'out', 'input')]
        Path(d, 'logo.png').write_bytes(png_bytes())
        return run_workflow(workflow(nodes, edges), base_dir=Path(d))

    def test_html_document_contains_escaped_text_table_chart_and_image(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.build(d, 'report.html', orientation='landscape', page_size='Letter')
            self.assertTrue(result.ok, result.error)
            html = Path(d, 'report.html').read_text(encoding='utf-8')
            self.assertIn('Quarterly &lt;sales&gt; &amp; margins', html)
            self.assertIn('7,781.50', html)  # computed total of the fixed-decimal amounts
            self.assertEqual(html.count('<svg'), 1)
            self.assertIn('data:image/png;base64,', html)
            self.assertIn('size:279.4mm 215.9mm', html)
            self.assertEqual(result.output('out').to_dicts()[0]['Format'], 'html')

    def test_pdf_document_has_pages_text_vector_chart_and_image(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.build(d, 'report.pdf', font='helvetica')
            self.assertTrue(result.ok, result.error)
            data = Path(d, 'report.pdf').read_bytes()
            self.assertTrue(data.startswith(b'%PDF-'))
            info = result.output('out').to_dicts()[0]
            self.assertEqual((info['Format'], info['Pages'], info['Bytes']), ('pdf', 1, len(data)))
            text = pdf_text(data)
            shown = [''.join(re.findall(r'\((.*?)\) Tj', block)) for block in re.findall(r'BT(.*?)ET', text, re.S)]
            for expected in ('Quarterly <sales> & margins', '7,781.50', 'Overview', 'North'):
                self.assertIn(expected, shown)
            self.assertIn(b'/Subtype /Image', data)
            bars = re.findall(r'0 \.447059 \.698039 rg\s+n [\d. ]+ re f', text)  # first series colour #0072B2
            self.assertEqual(len(bars), 2)  # one vector bar per region

    def test_long_tables_paginate_and_unicode_needs_a_font(self):
        rows = [[f'Item {i}', float(i)] for i in range(300)]
        with tempfile.TemporaryDirectory() as d:
            doc = workflow([text_input('t', [('name', 'v_wstring'), ('v', 'double')], rows),
                            {'id': 'table', 'tool': 'core.report_table', 'config': {}},
                            {'id': 'out', 'tool': 'core.render', 'config': {'path': 'long.pdf', 'font': 'helvetica'}}],
                           [edge('t', 'output', 'table', 'input'), edge('table', 'output', 'out', 'input')])
            result = run_workflow(doc, base_dir=Path(d))
            self.assertTrue(result.ok, result.error)
            pages = result.output('out').column('Pages')[0]
            self.assertGreater(pages, 3)
            self.assertEqual(pdf_text(Path(d, 'long.pdf').read_bytes()).count('(name) Tj'), pages)  # header repeated
            text = workflow([{'id': 'x', 'tool': 'core.report_text', 'config': {'text': 'Zürich – 東京'}},
                             {'id': 'out', 'tool': 'core.render', 'config': {'path': 'u.pdf', 'font': 'helvetica'}}],
                            [edge('x', 'output', 'out', 'input')])
            warned = run_workflow(text, base_dir=Path(d))
            self.assertTrue(any('replaced' in w['message'] for w in warned.warnings()))
            bad = workflow([{'id': 'x', 'tool': 'core.report_text', 'config': {'text': 'x'}},
                            {'id': 'out', 'tool': 'core.render', 'config': {'path': 'report.docx'}}],
                           [edge('x', 'output', 'out', 'input')])
            with self.assertRaisesRegex(WorkflowValidationError, '.html or .pdf'):
                run_workflow(bad, base_dir=Path(d))

    def test_fuel_report_example(self):
        result = run_file(ROOT / 'examples/reporting/fuel_report.flow.json')
        self.assertTrue(result.ok, result.error)
        written = {r['Format']: r for r in [*result.output('pdf').to_dicts(), *result.output('html').to_dicts()]}
        self.assertGreaterEqual(written['pdf']['Pages'], 1)
        html = Path(written['html']['Path']).read_text(encoding='utf-8')
        self.assertIn('All years', html)
        self.assertEqual(html.count('<svg'), 2)


if __name__ == '__main__':
    unittest.main()
