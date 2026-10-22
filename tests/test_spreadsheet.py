"""Excel and OpenDocument worksheets through Input Data, and inspect_spreadsheet."""
import datetime as dt
import tempfile
import unittest
import zipfile
from pathlib import Path

import openpyxl

from flowengine import agent
from flowengine.executor import run_workflow

from helpers import workflow

ODS_HEAD = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>')
ODS_TAIL = '</office:spreadsheet></office:body></office:document-content>'


def s(text):
    return f'<table:table-cell office:value-type="string"><text:p>{text}</text:p></table:table-cell>'


def n(value, repeat=1):
    attr = f' table:number-columns-repeated="{repeat}"' if repeat > 1 else ''
    return f'<table:table-cell office:value-type="float" office:value="{value}"{attr}/>'


def write_ods(path, tables):
    body = ''.join(f'<table:table table:name="{name}">{rows}</table:table>' for name, rows in tables)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/vnd.oasis.opendocument.spreadsheet')
        z.writestr('content.xml', ODS_HEAD + body + ODS_TAIL)


def row(*cells, repeat=1):
    attr = f' table:number-rows-repeated="{repeat}"' if repeat > 1 else ''
    return f'<table:table-row{attr}>{"".join(cells)}</table:table-row>'


EMPTY = '<table:table-cell/>'


class SpreadsheetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, **config):
        r = run_workflow(workflow([{'id': 'in', 'tool': 'core.input_data', 'config': config}], []), base_dir=self.dir)
        self.assertEqual(r.status, 'succeeded', r.error)
        t = r.outputs['in']['output']
        return [(f.name, f.type.value) for f in t.schema], list(t.rows()), r

    def published_table(self):
        wb = openpyxl.Workbook()
        wb.active.title = 'Contents'
        wb.active['A1'] = 'Table A1: households owed a duty'
        ws = wb.create_sheet('A1')
        ws.append(['Table A1 - Households, England'])
        ws.append([])
        ws.append(['Code', 'Area', 'Owed duty', 'Spend £k', 'Period', None, 'Area'])
        ws.append(['E06000001', 'Hartlepool', 120, 1534.5, dt.datetime(2024, 4, 1)])
        ws.append(['E06000002', 'Middlesbrough', None, 2010, dt.datetime(2024, 4, 1)])
        ws.append([])
        ws.append(['E06000003', 'Redcar and Cleveland', 95, 880.25, dt.datetime(2024, 4, 1)])
        wb.save(self.dir / 'la.xlsx')


class ExcelTests(SpreadsheetCase):
    def test_legacy_xls_preserves_identifier_numeric_and_date_types(self):
        import shutil
        fixture = Path(__file__).parent / 'fixtures' / 'legacy_input.xls'
        shutil.copyfile(fixture, self.dir / 'legacy.xls')
        fields, rows, _ = self.read(path='legacy.xls')
        self.assertEqual(fields, [('Account Code', 'v_wstring'), ('Quantity', 'int64'),
                                  ('Sales', 'double'), ('Sale Date', 'date')])
        self.assertEqual(rows[0], ('00123', 2.0, 5.0, dt.date(2026, 1, 2)))
        self.assertEqual(rows[1], ('00456', 12.0, 100.25, dt.date(2026, 1, 3)))

    def test_sheet_and_skip_rows_select_the_table_with_cell_types(self):
        self.published_table()
        fields, rows, r = self.read(path='la.xlsx', sheet='A1', skip_rows=2)
        self.assertEqual(fields, [('Code', 'v_wstring'), ('Area', 'v_wstring'), ('Owed duty', 'int64'),
                                  ('Spend £k', 'double'), ('Period', 'date'), ('Field_6', 'v_wstring'), ('Area_2', 'v_wstring')])
        self.assertEqual(len(rows), 3, 'blank rows inside the data are skipped')
        self.assertEqual(rows[1][:5], ('E06000002', 'Middlesbrough', None, 2010.0, dt.date(2024, 4, 1)))

    def test_range_without_header_and_missing_sheet(self):
        self.published_table()
        fields, rows, _ = self.read(path='la.xlsx', sheet='A1', range='A4:C5', has_header=False)
        self.assertEqual(fields, [('Field_1', 'v_wstring'), ('Field_2', 'v_wstring'), ('Field_3', 'int64')])
        self.assertEqual(rows, [('E06000001', 'Hartlepool', 120), ('E06000002', 'Middlesbrough', None)])
        r = run_workflow(workflow([{'id': 'in', 'tool': 'core.input_data', 'config': {'path': 'la.xlsx', 'sheet': 'Nope'}}], []), base_dir=self.dir)
        self.assertEqual(r.status, 'failed')
        self.assertIn("no sheet 'Nope'", r.error['message'])
        self.assertIn('Contents', r.error['message'])

    def test_first_sheet_is_the_default(self):
        self.published_table()
        fields, rows, _ = self.read(path='la.xlsx', has_header=False)
        self.assertEqual(rows, [('Table A1: households owed a duty',)])


class OdsTests(SpreadsheetCase):
    def test_repeated_cells_and_rows_types_and_trailing_blank_runs(self):
        write_ods(self.dir / 'la.ods', [
            ('Notes', row(s('About this table'))),
            ('Data', row(s('Title<text:line-break/>line'), ) + row(EMPTY) +
             row(s('Code'), s('Count'), s('Rate'), s('When')) +
             row(s('E1'), n(4), n(0.5), '<table:table-cell office:value-type="date" office:date-value="2024-01-31"/>') +
             row(s('E2'), n(7, repeat=2), '<table:table-cell office:value-type="date" office:date-value="2024-02-29T10:30:00"/>') +
             row(EMPTY, repeat=1048000) +
             row('<table:table-cell table:number-columns-repeated="16384"/>', repeat=1000)),
        ])
        fields, rows, _ = self.read(path='la.ods', sheet='Data', skip_rows=2)
        self.assertEqual(fields, [('Code', 'v_wstring'), ('Count', 'int64'), ('Rate', 'double'), ('When', 'datetime')])
        self.assertEqual(rows, [('E1', 4, 0.5, dt.datetime(2024, 1, 31)), ('E2', 7, 7.0, dt.datetime(2024, 2, 29, 10, 30))])

    def test_inspect_lists_sheets_with_first_rows(self):
        write_ods(self.dir / 'la.ods', [('Notes', row(s('About'))), ('Data', row(s('Code'), s('Count')) + row(s('E1'), n(3)))])
        out = agent.call('inspect_spreadsheet', {'path': 'la.ods'}, agent.Context(self.dir))
        self.assertEqual([(x['sheet'], x['rows'], x['columns']) for x in out['sheets']], [('Notes', 1, 1), ('Data', 2, 2)])
        self.assertEqual(out['sheets'][1]['first_rows'], [['Code', 'Count'], ['E1', '3']])
        prof = agent.call('profile_file', {'path': 'la.ods', 'sheet': 'Data'}, agent.Context(self.dir))
        self.assertEqual((prof['rows'], prof['fields'][1]['suggested_type']), (1, 'int64'))


if __name__ == '__main__':
    unittest.main()
