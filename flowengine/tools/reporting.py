"""Reporting tools: build report elements from data and render them to HTML or PDF files.

Elements travel in a ``Report`` text field (see ``flowengine.reports``). Table, Interactive Chart,
Report Text and Image create elements. Layout arranges them, and Render writes the document. These are
Flowbench semantics derived from the public tool descriptions. The vendor's report snippet format,
styling options and XML configuration are not reproduced.
"""
from __future__ import annotations

import base64
import datetime as _dt
import io
import math
import re
from collections import OrderedDict, defaultdict
from decimal import Decimal

from .. import charts, reports
from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
REPORT = Field('Report', FieldType.V_WSTRING, TEXT)
_PLACEHOLDER = re.compile(r'\{\{\s*([^{}]+?)\s*\}\}')


def _missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _with_report(schema, keep):
    if 'Report' in schema.names:
        raise SchemaError('Input already has a Report field; rename it before adding another report element')
    return Schema([*[schema.field(n) for n in keep], REPORT])


def format_cell(value, field, fmt, decimals):
    """Format one value for display. Numbers get thousands separators; text is unchanged."""
    if _missing(value):
        return ''
    if fmt == 'text' or not field.type.is_numeric or isinstance(value, bool):
        return format_value(value)
    number = float(value)
    if fmt == 'auto':
        if field.type.is_integer:
            return f'{int(value):,}'
        d = decimals if decimals is not None else (field.scale if field.type.value == 'fixed_decimal' and field.scale is not None else 2)
        return f'{number:,.{d}f}'
    d = 2 if decimals is None else decimals
    if fmt == 'integer':
        return f'{round(number):,}'
    if fmt == 'percent':
        return f'{number * 100:,.{d}f}%'
    if fmt == 'currency':
        return f'{"-" if number < 0 else ""}{abs(number):,.{d}f}'
    return f'{number:,.{d}f}'


_COLUMN = {'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {
    'field': {'type': 'string', 'minLength': 1}, 'label': {'type': ['string', 'null'], 'default': None},
    'format': {'enum': ['auto', 'number', 'integer', 'percent', 'currency', 'text'], 'default': 'auto'},
    'decimals': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 10, 'default': None},
    'total': {'type': 'boolean', 'default': False}}}


class ReportTable(Tool):
    manifest = ToolManifest(
        tool_id='core.report_table', version='1.0.0', display_name='Table', catalog_ids=('reporting:table',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Report', description='One table element per group (or one for all records)'),),
        description='Build a report table from selected columns with labels and number formats (thousands '
                    'separators, fixed decimals, integer, percent, currency). Optional group_field gives one table '
                    'per group, and columns marked total add a summed total row. Without columns, every field is '
                    'shown. max_rows limits each table and logs a warning when rows are left out.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'columns': {'type': 'array', 'items': _COLUMN, 'default': []},
            'group_field': {'type': ['string', 'null'], 'default': None},
            'title': {'type': ['string', 'null'], 'default': None},
            'total_label': {'type': 'string', 'default': 'Total'},
            'max_rows': {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 5000}}})

    def _columns(self, schema, cfg):
        cols = cfg['columns'] or [{'field': f.name, 'label': None, 'format': 'auto', 'decimals': None, 'total': False}
                                  for f in schema if f.name != cfg['group_field'] and f.type.value != 'blob']
        require_fields(schema, [c['field'] for c in cols] + ([cfg['group_field']] if cfg['group_field'] else []))
        bad = [c['field'] for c in cols if c['total'] and not schema.field(c['field']).type.is_numeric]
        if bad:
            raise SchemaError(f'Table totals need numeric fields: {bad}')
        return cols

    def infer(self, ctx, cfg, inputs):
        self._columns(inputs['input'], cfg)
        return {'output': _with_report(inputs['input'], [cfg['group_field']] if cfg['group_field'] else [])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        cols = self._columns(table.schema, cfg)
        schema = self.infer(ctx, cfg, inputs={'input': table.schema})['output']
        fields = [table.schema.field(c['field']) for c in cols]
        groups = OrderedDict()
        keys = table.column(cfg['group_field']) if cfg['group_field'] else [None] * len(table)
        for i, key in enumerate(keys):
            groups.setdefault(key, []).append(i)
        if not cfg['group_field']:
            groups = OrderedDict([(None, list(range(len(table))))])
        out = []
        for key, idx in groups.items():
            shown = idx[:cfg['max_rows']]
            if len(idx) > len(shown):
                ctx.warn(f'Table shows {len(shown)} of {len(idx)} rows' + (f' for {format_value(key)}' if key is not None else ''))
            rows = [[format_cell(table.columns[table.schema.index(c['field'])][i], f, c['format'], c['decimals'])
                     for c, f in zip(cols, fields)] for i in shown]
            total_row = None
            if any(c['total'] for c in cols):
                total_row = []
                for n, (c, f) in enumerate(zip(cols, fields)):
                    if c['total']:
                        vals = [table.columns[table.schema.index(c['field'])][i] for i in idx]
                        s = sum((v for v in vals if not _missing(v)), Decimal(0) if f.type.value == 'fixed_decimal' else 0)
                        total_row.append(format_cell(s, f, c['format'], c['decimals']))
                    else:
                        total_row.append(cfg['total_label'] if n == 0 else '')
            columns = [{'label': c['label'] or c['field'], 'align': 'right' if f.type.is_numeric and c['format'] != 'text' else 'left'}
                       for c, f in zip(cols, fields)]
            doc = reports.element('table', title=cfg['title'], group=None if key is None else format_value(key),
                                  columns=columns, rows=rows, total_row=total_row)
            out.append(((key,) if cfg['group_field'] else ()) + (doc,))
        return {'output': Table.from_rows(schema, out)}


_AGGREGATES = ('none', 'sum', 'mean', 'count', 'min', 'max')


class InteractiveChart(Tool):
    manifest = ToolManifest(
        tool_id='core.interactive_chart', version='1.0.0', display_name='Interactive Chart',
        catalog_ids=('reporting:interactive-chart',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Report', description='Chart element plus a rendered preview'),),
        description='Bar, line, area, scatter, histogram or time-series chart from the input. Bar, line, area and '
                    'time-series charts aggregate y_fields by x_field (sum, mean, count, min, max, or none to plot '
                    'values as given). series_field splits one y field into a series per value. Rendered charts '
                    'show values on hover in HTML reports and the results panel.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['chart_type', 'x_field'], 'properties': {
            'chart_type': {'enum': ['bar', 'line', 'area', 'scatter', 'histogram', 'timeseries']},
            'x_field': {'type': 'string', 'minLength': 1},
            'y_fields': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1}, 'default': []},
            'series_field': {'type': ['string', 'null'], 'default': None},
            'aggregation': {'enum': list(_AGGREGATES), 'default': 'sum'},
            'stacked': {'type': 'boolean', 'default': False},
            'sort': {'enum': ['x', 'value_desc', 'none'], 'default': 'x'},
            'bins': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 20},
            'max_categories': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 100},
            'title': {'type': ['string', 'null'], 'default': None},
            'x_label': {'type': ['string', 'null'], 'default': None},
            'y_label': {'type': ['string', 'null'], 'default': None},
            'width': {'type': 'integer', 'minimum': 240, 'maximum': 4000, 'default': 720},
            'height': {'type': 'integer', 'minimum': 180, 'maximum': 4000, 'default': 420}}})

    SCHEMA = Schema([REPORT, Field('Chart', FieldType.V_WSTRING, TEXT)])

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        kind = cfg['chart_type']
        require_fields(schema, [cfg['x_field'], *cfg['y_fields']] + ([cfg['series_field']] if cfg['series_field'] else []))
        if kind != 'histogram' and not cfg['y_fields'] and cfg['aggregation'] != 'count':
            raise ConfigError(f'{kind} chart needs y_fields (or aggregation count)')
        if cfg['series_field'] and len(cfg['y_fields']) > 1:
            raise ConfigError('series_field splits a single y field; choose one y field')
        numeric_y = cfg['aggregation'] != 'count' or kind == 'scatter'
        for y in cfg['y_fields']:
            if numeric_y and not schema.field(y).type.is_numeric:
                raise SchemaError(f'Chart y field {y!r} must be numeric')
        xt = schema.field(cfg['x_field']).type
        if kind in ('scatter', 'histogram') and not xt.is_numeric:
            raise SchemaError(f'{kind} chart x field must be numeric')
        if kind == 'timeseries' and not (xt.value in ('date', 'datetime') or xt.is_numeric):
            raise SchemaError('Time-series chart x field must be a date or date-time')
        if kind == 'scatter' and cfg['aggregation'] not in ('none', 'sum'):
            raise ConfigError('Scatter charts plot individual records; aggregation must be none')
        return {'output': self.SCHEMA}

    @staticmethod
    def _reduce(values, how):
        vals = [float(v) for v in values if not _missing(v)]
        if how == 'count':
            return float(len(values))
        if not vals:
            return None
        return {'sum': sum(vals), 'mean': sum(vals) / len(vals), 'min': min(vals), 'max': max(vals)}[how]

    def _spec(self, ctx, cfg, table):
        kind, xf = cfg['chart_type'], cfg['x_field']
        spec = {'type': 'line' if kind == 'timeseries' else kind, 'title': cfg['title'], 'width': cfg['width'], 'height': cfg['height'],
                'x_label': cfg['x_label'] or xf,
                'y_label': cfg['y_label'] or (', '.join(cfg['y_fields']) if cfg['y_fields'] else 'Count')}
        xs = table.column(xf)
        if kind == 'histogram':
            import numpy as np
            arr = np.array([float(v) for v in xs if not _missing(v)])
            arr = arr[np.isfinite(arr)]
            if not arr.size:
                raise DataError('Histogram chart has no numeric values')
            counts, edges = np.histogram(arr, bins=cfg['bins'])
            spec.update(edges=[float(e) for e in edges], counts=[int(c) for c in counts], y_label=cfg['y_label'] or 'Count')
            return spec
        ys = cfg['y_fields']
        if cfg['series_field']:
            labels = [format_value(v) if not _missing(v) else '[Null]' for v in table.column(cfg['series_field'])]
            series_values = {}
            for label, x, y in zip(labels, xs, table.column(ys[0]) if ys else [1] * len(table)):
                series_values.setdefault(label, []).append((x, y))
        else:
            series_values = {y: list(zip(xs, table.column(y))) for y in ys} if ys else {'Count': [(x, 1) for x in xs]}
        if kind == 'scatter':
            spec['series'] = {name: [(float(x), float(y)) for x, y in pts if not (_missing(x) or _missing(y))]
                              for name, pts in series_values.items()}
            return spec
        how = cfg['aggregation']
        time_axis = kind == 'timeseries' or table.schema.field(xf).type.value in ('date', 'datetime')
        numeric_x = table.schema.field(xf).type.is_numeric or time_axis
        grouped = {}
        for name, pts in series_values.items():
            buckets = defaultdict(list)
            for x, y in pts:
                if _missing(x):
                    continue
                buckets[x].append(y)
            if how == 'none':
                dup = sum(1 for v in buckets.values() if len(v) > 1)
                if dup:
                    raise DataError(f'{dup} x value(s) repeat; choose an aggregation for a {kind} chart')
            grouped[name] = {x: self._reduce(v, 'sum' if how == 'none' else how) for x, v in buckets.items()}
        all_x = sorted({x for g in grouped.values() for x in g}, key=lambda v: (str(type(v)), v))
        if kind == 'bar' or not numeric_x:
            if cfg['sort'] == 'value_desc':
                first = next(iter(grouped.values()), {})
                all_x.sort(key=lambda x: -(first.get(x) or 0))
            elif cfg['sort'] == 'none':
                seen = []
                for x in xs:
                    if not _missing(x) and x not in seen:
                        seen.append(x)
                all_x = seen
            if len(all_x) > cfg['max_categories']:
                ctx.warn(f'Chart shows the first {cfg["max_categories"]} of {len(all_x)} categories')
                all_x = all_x[:cfg['max_categories']]
            labels = [format_value(x) for x in all_x]
            if kind == 'bar':
                spec.update(categories=labels, series={n: [g.get(x) for x in all_x] for n, g in grouped.items()},
                            stacked=cfg['stacked'])
            else:
                spec.update(categories=labels, series={n: [(i, g.get(x)) for i, x in enumerate(all_x)] for n, g in grouped.items()})
            return spec
        to_num = charts.to_timestamp if time_axis and table.schema.field(xf).type.value in ('date', 'datetime') else float
        spec.update(type='area' if kind == 'area' else 'line', time_axis=time_axis,
                    series={n: [(to_num(x), g[x]) for x in all_x if x in g] for n, g in grouped.items()})
        return spec

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        spec = self._spec(ctx, cfg, table)
        svg = reports.build_chart(spec).to_svg()
        return {'output': Table.from_rows(self.SCHEMA, [(reports.element('chart', chart=spec), svg)])}


class ReportText(Tool):
    manifest = ToolManifest(
        tool_id='core.report_text', version='1.0.0', display_name='Report Text', catalog_ids=('reporting:report-text',),
        inputs=(Anchor('input', 'Input', required=False),),
        outputs=(Anchor('output', 'Report'),),
        description='A formatted text block (title, heading, body or caption; left, centre or right). {{Field}} '
                    'placeholders are replaced with field values as plain text, which are never evaluated and are '
                    'escaped when rendered. per_record makes one element per input record, first_record uses '
                    'the first record, and static ignores the input.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text'], 'properties': {
            'text': {'type': 'string', 'maxLength': 100000},
            'mode': {'enum': ['static', 'first_record', 'per_record'], 'default': 'static'},
            'style': {'enum': list(reports.TEXT_STYLES), 'default': 'body'},
            'align': {'enum': ['left', 'center', 'right'], 'default': 'left'},
            'null_text': {'type': 'string', 'default': ''}}})

    def infer(self, ctx, cfg, inputs):
        names = _PLACEHOLDER.findall(cfg['text'])
        schema = inputs.get('input')
        if cfg['mode'] != 'static' and schema is None:
            raise ConfigError(f"Report Text mode {cfg['mode']!r} needs a connected input")
        if names:
            if schema is None or cfg['mode'] == 'static':
                raise ConfigError('Report Text placeholders need an input and mode first_record or per_record')
            require_fields(schema, names, 'placeholder field')
        if cfg['mode'] == 'per_record':
            return {'output': _with_report(schema, list(schema.names))}
        return {'output': Schema([REPORT])}

    def run(self, ctx, cfg, inputs):
        table = inputs.get('input')
        schema = self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items()})['output']

        def fill(record):
            return _PLACEHOLDER.sub(lambda m: cfg['null_text'] if _missing(record[m.group(1).strip()])
                                    else format_value(record[m.group(1).strip()]), cfg['text'])

        def doc(text):
            return reports.element('text', text=text, style=cfg['style'], align=cfg['align'])
        if cfg['mode'] == 'static':
            return {'output': Table.from_rows(schema, [(doc(cfg['text']),)])}
        records = table.to_dicts()
        if cfg['mode'] == 'first_record':
            if not records:
                raise DataError('Report Text first_record mode received no records')
            return {'output': Table.from_rows(schema, [(doc(fill(records[0])),)])}
        return {'output': Table.from_rows(schema, [(*row, doc(fill(r))) for row, r in zip(table.rows(), records)])}


_IMAGE_FORMATS = {'PNG': 'png', 'JPEG': 'jpeg', 'GIF': 'png', 'BMP': 'png', 'WEBP': 'png', 'TIFF': 'png'}


def _image_element(raw: bytes, alt, display_width, max_bytes):
    from PIL import Image, UnidentifiedImageError
    if len(raw) > max_bytes:
        raise DataError(f'Image is larger than max_bytes ({max_bytes})')
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise DataError(f'Not a supported image: {type(e).__name__}') from None
    target = _IMAGE_FORMATS.get(img.format)
    if target is None:
        raise DataError(f'Image format {img.format!r} is not supported; use PNG, JPEG, GIF, BMP, WebP or TIFF')
    if target == 'jpeg' and img.format == 'JPEG' and img.mode in ('RGB', 'L', 'CMYK'):
        data = raw
    else:  # normalise to PNG so HTML and PDF both embed it
        buffer = io.BytesIO()
        (img.convert('RGBA') if img.mode not in ('RGB', 'RGBA', 'L', 'LA') else img).save(buffer, format='PNG')
        data, target = buffer.getvalue(), 'png'
    return reports.element('image', image_format=target, data=base64.b64encode(data).decode('ascii'), width=img.width,
                           height=img.height, display_width=display_width, alt=alt)


class ReportImage(Tool):
    manifest = ToolManifest(
        tool_id='core.report_image', version='1.0.0', display_name='Image', catalog_ids=('reporting:image',),
        inputs=(Anchor('input', 'Input', required=False, description='Records with a blob or path field (field mode)'),),
        outputs=(Anchor('output', 'Report'),),
        description='Add an image to a report from a file (path, relative to the workflow) or per record from a '
                    'blob or path field. Images are decoded and validated (PNG, JPEG, GIF, BMP, WebP, TIFF). Non-JPEG '
                    'formats are converted to PNG for embedding. display_width sets the width in points. Oversized '
                    'and malformed images are rejected.',
        side_effects='reads-files',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'path': {'type': ['string', 'null'], 'default': None},
            'image_field': {'type': ['string', 'null'], 'default': None,
                            'description': 'Blob field with image bytes, or text field with an image path'},
            'alt': {'type': ['string', 'null'], 'default': None},
            'display_width': {'type': ['number', 'null'], 'exclusiveMinimum': 0, 'default': None},
            'max_bytes': {'type': 'integer', 'minimum': 1, 'maximum': 200 * 2**20, 'default': 25 * 2**20}}})

    def infer(self, ctx, cfg, inputs):
        if bool(cfg['path']) == bool(cfg['image_field']):
            raise ConfigError('Image needs exactly one of path or image_field')
        if cfg['image_field']:
            schema = inputs.get('input')
            if schema is None:
                raise ConfigError('Image image_field needs a connected input')
            require_fields(schema, [cfg['image_field']])
            t = schema.field(cfg['image_field']).type
            if not (t.value == 'blob' or t.is_string):
                raise SchemaError('Image image_field must be a blob or text (path) field')
            return {'output': _with_report(schema, list(schema.names))}
        return {'output': Schema([REPORT])}

    def _read(self, ctx, path, cfg):
        p = ctx.resolve_path(path)
        if not p.is_file():
            raise DataError(f'Image file not found: {p}')
        if p.stat().st_size > cfg['max_bytes']:
            raise DataError(f'Image {p.name} is larger than max_bytes ({cfg["max_bytes"]})')
        return p.read_bytes()

    def run(self, ctx, cfg, inputs):
        schema = self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items()})['output']
        if cfg['path']:
            raw = self._read(ctx, cfg['path'], cfg)
            return {'output': Table.from_rows(schema, [(_image_element(raw, cfg['alt'], cfg['display_width'], cfg['max_bytes']),)])}
        table = inputs['input']
        rows = []
        for number, (row, value) in enumerate(zip(table.rows(), table.column(cfg['image_field'])), start=1):
            ctx.check_cancelled()
            if _missing(value):
                raise DataError(f'Image record {number} has no image')
            raw = value if isinstance(value, bytes) else self._read(ctx, str(value), cfg)
            try:
                rows.append((*row, _image_element(raw, cfg['alt'], cfg['display_width'], cfg['max_bytes'])))
            except DataError as e:
                raise DataError(f'Image record {number}: {e.message}') from None
        return {'output': Table.from_rows(schema, rows)}


class Layout(Tool):
    manifest = ToolManifest(
        tool_id='core.report_layout', version='1.0.0', display_name='Layout', catalog_ids=('reporting:layout',),
        inputs=(Anchor('inputs', 'Input', multiple=True, description='Report elements, arranged in connection order'),),
        outputs=(Anchor('output', 'Report'),),
        description='Arrange report elements vertically (stacked) or horizontally (side by side), in connection '
                    'order and then record order, into one layout element with an optional heading. Layouts nest. '
                    'Page size, orientation and margins are set on Render.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'direction': {'enum': ['vertical', 'horizontal'], 'default': 'vertical'},
            'title': {'type': ['string', 'null'], 'default': None},
            'gap': {'type': 'integer', 'minimum': 0, 'maximum': 200, 'default': 16},
            'report_field': {'type': 'string', 'default': 'Report'}}})

    def infer(self, ctx, cfg, inputs):
        for schema in inputs['inputs']:
            require_fields(schema, [cfg['report_field']], 'report field')
        return {'output': Schema([REPORT])}

    def run(self, ctx, cfg, inputs):
        items = []
        for table in inputs['inputs']:
            require_fields(table.schema, [cfg['report_field']], 'report field')
            items += [reports.parse(v) for v in table.column(cfg['report_field'])]
        if not items:
            raise DataError('Layout received no report elements')
        for item in items:
            item.pop('format', None)
        doc = reports.element('layout', direction=cfg['direction'], title=cfg['title'], gap=cfg['gap'], items=items)
        return {'output': Table.from_rows(Schema([REPORT]), [(doc,)])}


class Render(Tool):
    SCHEMA = Schema([Field('Path', FieldType.V_WSTRING, TEXT), Field('Format', FieldType.V_WSTRING, 8),
                     Field('Pages', FieldType.INT32), Field('Bytes', FieldType.INT64), Field('Elements', FieldType.INT32)])
    manifest = ToolManifest(
        tool_id='core.render', version='1.0.0', display_name='Render', catalog_ids=('reporting:render',),
        inputs=(Anchor('input', 'Input', description='Records with report elements'),),
        outputs=(Anchor('output', 'Output', description='Written file, format, page count and size'),),
        side_effects='writes-files',
        description='Write the report elements of every input record, in order, to an HTML or PDF document '
                    '(format auto chooses from the file extension). Sets page size, orientation and margins; '
                    'page_breaks starts each record on a new page. PDF tables split across pages with repeated '
                    'headers. PDF text uses a Unicode TrueType system font when one is available (or font_path), '
                    'otherwise Helvetica with a warning for characters it cannot draw.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path'], 'properties': {
            'path': {'type': 'string', 'minLength': 1},
            'format': {'enum': ['auto', 'html', 'pdf'], 'default': 'auto'},
            'report_field': {'type': 'string', 'default': 'Report'},
            'title': {'type': 'string', 'default': 'Flowbench report'},
            'page_size': {'enum': list(reports.PAGE_SIZES_MM), 'default': 'A4'},
            'orientation': {'enum': ['portrait', 'landscape'], 'default': 'portrait'},
            'margin_mm': {'type': 'number', 'minimum': 0, 'maximum': 100, 'default': 15},
            'page_breaks': {'type': 'boolean', 'default': False},
            'font': {'enum': ['auto', 'helvetica'], 'default': 'auto'},
            'font_path': {'type': ['string', 'null'], 'default': None},
            'if_exists': {'enum': ['overwrite', 'error'], 'default': 'overwrite'}}})

    def _format(self, cfg):
        if cfg['format'] != 'auto':
            return cfg['format']
        suffix = cfg['path'].lower().rsplit('.', 1)[-1] if '.' in cfg['path'] else ''
        if suffix in ('htm', 'html'):
            return 'html'
        if suffix == 'pdf':
            return 'pdf'
        raise ConfigError('Render path must end in .html or .pdf, or set format')

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['report_field']], 'report field')
        self._format(cfg)
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        fmt = self._format(cfg)
        elements = [reports.parse(v) for v in table.column(cfg['report_field'])]
        if not elements:
            raise DataError('Render received no report elements')
        path = ctx.resolve_path(cfg['path'])
        if path.exists() and cfg['if_exists'] == 'error':
            raise DataError(f'Report file already exists: {path}')
        options = dict(title=cfg['title'], page_size=cfg['page_size'], orientation=cfg['orientation'],
                       margin_mm=cfg['margin_mm'], page_breaks=cfg['page_breaks'])
        if fmt == 'html':
            info = reports.render_html(elements, path, **options)
        else:
            font_path = str(ctx.resolve_path(cfg['font_path'])) if cfg['font_path'] else None
            info = reports.render_pdf(elements, path, font=cfg['font'], font_path=font_path, warn=ctx.warn, **options)
        ctx.info(f'wrote {fmt.upper()} report ({info["bytes"]} bytes)', path=str(path))
        return {'output': Table.from_rows(self.SCHEMA, [(str(path), fmt, info['pages'], info['bytes'], len(elements))])}



class ReportHeader(Tool):
    manifest = ToolManifest(
        tool_id='core.report_header', version='1.0.0', display_name='Report Header', catalog_ids=('reporting:report-header',),
        inputs=(), outputs=(Anchor('output', 'Report'),),
        description='A page header (title, optional subtitle, optional run date, optional logo image) that Render '
                    'draws at the top of every PDF page and at the top of the HTML document. Connect it to Layout '
                    'or directly to Render together with the body elements.',
        side_effects='reads-files',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['title'], 'properties': {
            'title': {'type': 'string', 'maxLength': 500}, 'subtitle': {'type': ['string', 'null'], 'default': None},
            'show_date': {'type': 'boolean', 'default': True},
            'date_format': {'type': 'string', 'default': '%d %B %Y', 'description': 'strftime pattern for the run date'},
            'image_path': {'type': ['string', 'null'], 'default': None},
            'image_height': {'type': 'integer', 'minimum': 8, 'maximum': 200, 'default': 40}}})

    def infer(self, ctx, cfg, inputs):
        return {'output': Schema([REPORT])}

    def run(self, ctx, cfg, inputs):
        image = None
        if cfg['image_path']:
            p = ctx.resolve_path(cfg['image_path'])
            if not p.is_file():
                raise DataError(f'Header image not found: {p}')
            image = reports.parse(_image_element(p.read_bytes(), None, None, 25 * 2**20))
            image.pop('format', None)
        try:
            date_text = ctx.clock().strftime(cfg['date_format']) if cfg['show_date'] else None
        except ValueError as e:
            raise ConfigError(f'Invalid date_format: {e}') from None
        doc = reports.element('header', title=cfg['title'], subtitle=cfg['subtitle'], date_text=date_text,
                              image=image, image_height=cfg['image_height'])
        return {'output': Table.from_rows(Schema([REPORT]), [(doc,)])}


class ReportFooter(Tool):
    manifest = ToolManifest(
        tool_id='core.report_footer', version='1.0.0', display_name='Report Footer', catalog_ids=('reporting:report-footer',),
        inputs=(), outputs=(Anchor('output', 'Report'),),
        description='A page footer with left, centre and right text that Render draws at the bottom of every PDF '
                    'page. {page}, {pages} and {date} are replaced with the page number, total pages and run date. '
                    'In HTML, cells that use page numbers are omitted, because a screen page has none.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'left': {'type': ['string', 'null'], 'default': None},
            'center': {'type': ['string', 'null'], 'default': None},
            'right': {'type': ['string', 'null'], 'default': 'Page {page} of {pages}'},
            'date_format': {'type': 'string', 'default': '%d %B %Y'}}})

    def infer(self, ctx, cfg, inputs):
        return {'output': Schema([REPORT])}

    def run(self, ctx, cfg, inputs):
        try:
            date_text = ctx.clock().strftime(cfg['date_format'])
        except ValueError as e:
            raise ConfigError(f'Invalid date_format: {e}') from None
        doc = reports.element('footer', left=cfg['left'], center=cfg['center'], right=cfg['right'], date_text=date_text)
        return {'output': Table.from_rows(Schema([REPORT]), [(doc,)])}


class Overlay(Tool):
    manifest = ToolManifest(
        tool_id='core.report_overlay', version='1.0.0', display_name='Overlay', catalog_ids=('reporting:overlay',),
        inputs=(Anchor('input', 'Input', description='Records with a base element and elements to place over it'),),
        outputs=(Anchor('output', 'Report'),),
        description='Place report elements over a base element (for example a caption or logo over a chart or map) '
                    'at nine positions: top, middle or bottom combined with left, centre or right. Each record '
                    'produces one overlay, which replaces the base field.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['base_field'], 'properties': {
            'base_field': {'type': 'string', 'minLength': 1},
            'positions': {'type': 'object', 'additionalProperties': False, 'default': {},
                          'properties': {p: {'type': 'string', 'minLength': 1} for p in reports.POSITIONS},
                          'description': 'Field holding the element for each position'},
            'padding': {'type': 'integer', 'minimum': 0, 'maximum': 100, 'default': 8}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['base_field'], *cfg['positions'].values()])
        if not cfg['positions']:
            raise ConfigError('Overlay needs at least one positioned element')
        return {'output': inputs['input']}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        bi = table.schema.index(cfg['base_field'])
        out = []
        for rec, row in zip(table.to_dicts(), table.rows()):
            base = reports.parse(rec[cfg['base_field']])
            items = {}
            for position, field in cfg['positions'].items():
                if rec[field] is not None:
                    child = reports.parse(rec[field])
                    child.pop('format', None)
                    items[position] = child
            base.pop('format', None)
            doc = reports.element('overlay', base=base, items=items, padding=cfg['padding'])
            out.append(tuple(doc if i == bi else v for i, v in enumerate(row)))
        return {'output': Table.from_rows(table.schema, out)}


_LAYER = {'type': 'object', 'additionalProperties': False, 'required': ['geometry_field'], 'properties': {
    'geometry_field': {'type': 'string', 'minLength': 1}, 'name': {'type': ['string', 'null'], 'default': None},
    'color': {'type': 'string', 'pattern': '^#[0-9A-Fa-f]{6}$', 'default': '#0072B2'},
    'label_field': {'type': ['string', 'null'], 'default': None},
    'theme_field': {'type': ['string', 'null'], 'default': None, 'description': 'Numeric field coloured with palette'},
    'palette': {'enum': sorted(charts.PALETTES), 'default': 'viridis'},
    'point_size': {'type': 'number', 'minimum': 1, 'maximum': 40, 'default': 5},
    'opacity': {'type': 'number', 'minimum': 0.05, 'maximum': 1, 'default': 0.8}}}


class ReportMap(Tool):
    manifest = ToolManifest(
        tool_id='core.report_map', version='1.0.0', display_name='Report Map', catalog_ids=('reporting:report-map',),
        inputs=(Anchor('layers', 'Layers', multiple=True, description='One connection per layer, bottom layer first'),),
        outputs=(Anchor('output', 'Report', description='Map element plus a rendered preview'),),
        description='Vector map of geometry layers (WKT or GeoJSON fields from the spatial tools), one layer per '
                    'connection in order. Each layer has a colour, optional labels and optional thematic colouring '
                    'of a numeric field, with an automatic legend. Geographic data uses an equirectangular '
                    'projection corrected for latitude. No background tiles are drawn, so it works offline.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['layers'], 'properties': {
            'layers': {'type': 'array', 'minItems': 1, 'items': _LAYER},
            'crs': {'type': 'string', 'default': 'EPSG:4326'},
            'title': {'type': ['string', 'null'], 'default': None}, 'legend': {'type': 'boolean', 'default': True},
            'max_features': {'type': 'integer', 'minimum': 1, 'maximum': 200000, 'default': 20000},
            'width': {'type': 'integer', 'minimum': 240, 'maximum': 4000, 'default': 720},
            'height': {'type': 'integer', 'minimum': 180, 'maximum': 4000, 'default': 520}}})
    SCHEMA = Schema([REPORT, Field('Chart', FieldType.V_WSTRING, TEXT)])

    def infer(self, ctx, cfg, inputs):
        schemas = inputs['layers']
        if len(schemas) != len(cfg['layers']):
            raise ConfigError(f"Report Map has {len(schemas)} connection(s) but {len(cfg['layers'])} layer setting(s)")
        for schema, layer in zip(schemas, cfg['layers']):
            require_fields(schema, [layer['geometry_field']] + [layer[k] for k in ('label_field', 'theme_field') if layer[k]])
            if layer['theme_field'] and not schema.field(layer['theme_field']).type.is_numeric:
                raise SchemaError(f"Report Map theme_field {layer['theme_field']!r} must be numeric")
        from .spatial import crs_info
        crs_info(cfg['crs'])
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        from .spatial import crs_info, dump, parse
        tables = inputs['layers']
        self.infer(ctx, cfg, {'layers': [t.schema for t in tables]})
        _, geographic, _ = crs_info(cfg['crs'])
        layers, legend, total = [], [], 0
        for index, (table, layer) in enumerate(zip(tables, cfg['layers'])):
            name = layer['name'] or layer['geometry_field']
            themes = table.column(layer['theme_field']) if layer['theme_field'] else None
            finite = [float(v) for v in themes or [] if not _missing(v)]
            lo, hi = (min(finite), max(finite)) if finite else (0.0, 1.0)
            features, kinds = [], set()
            for i, rec in enumerate(table.to_dicts()):
                geom = parse(rec[layer['geometry_field']], 'error', f' in layer {name!r} record {i + 1}')
                if geom is None or geom.is_empty:
                    continue
                total += 1
                if total > cfg['max_features']:
                    raise DataError(f"Report Map has more than max_features ({cfg['max_features']}) features")
                kinds.add(geom.geom_type.replace('Multi', ''))
                value = themes[i] if themes else None
                fill = (charts.color_at(layer['palette'], (float(value) - lo) / (hi - lo) if hi > lo else 0.5)
                        if themes and not _missing(value) else None)
                label = format_value(rec[layer['label_field']]) if layer['label_field'] and rec[layer['label_field']] is not None else None
                tip = ' · '.join(filter(None, [label, f"{layer['theme_field']}: {format_value(value)}" if themes and not _missing(value) else None]))
                features.append((dump(geom, 'wkt'), fill, label, tip or None))
            layers.append({'name': name, 'color': layer['color'], 'opacity': layer['opacity'],
                           'point_size': layer['point_size'], 'features': features})
            shape = 'point' if kinds == {'Point'} else 'line' if kinds == {'LineString'} else 'fill'
            if themes and finite:
                for k in range(5):
                    t = k / 4
                    legend.append((f"{layer['theme_field']} {charts.fmt_number(lo + (hi - lo) * t)}",
                                   charts.color_at(layer['palette'], t), shape))
            else:
                legend.append((name, layer['color'], shape))
        spec = {'type': 'map', 'title': cfg['title'], 'width': cfg['width'], 'height': cfg['height'],
                'geographic': geographic, 'layers': layers, 'legend': legend if cfg['legend'] else []}
        svg = reports.build_chart(spec).to_svg()
        return {'output': Table.from_rows(self.SCHEMA, [(reports.element('chart', chart=spec), svg)])}


class MapLegendBuilder(Tool):
    manifest = ToolManifest(
        tool_id='core.map_legend_builder', version='1.0.0', display_name='Map Legend Builder',
        catalog_ids=('reporting:map-legend-builder',),
        inputs=(Anchor('input', 'Input', description='One record per legend entry'),), outputs=(Anchor('output', 'Report'),),
        description='Build a legend element from records with a label, a #RRGGBB colour and an optional shape '
                    '(fill, line or point). The legend can be placed in a layout or overlaid on a map.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['label_field', 'color_field'], 'properties': {
            'label_field': {'type': 'string', 'minLength': 1}, 'color_field': {'type': 'string', 'minLength': 1},
            'shape_field': {'type': ['string', 'null'], 'default': None},
            'title': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['label_field'], cfg['color_field']] + ([cfg['shape_field']] if cfg['shape_field'] else []))
        return {'output': Schema([REPORT])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        entries = []
        for number, rec in enumerate(table.to_dicts(), start=1):
            colour, shape = rec[cfg['color_field']], (rec[cfg['shape_field']] if cfg['shape_field'] else None) or 'fill'
            if not isinstance(colour, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', colour):
                raise DataError(f'Legend record {number} colour must be #RRGGBB')
            if shape not in ('fill', 'line', 'point'):
                raise DataError(f'Legend record {number} shape must be fill, line or point')
            entries.append({'label': format_value(rec[cfg['label_field']]) or '', 'color': colour, 'shape': shape})
        return {'output': Table.from_rows(Schema([REPORT]), [(reports.element('legend', title=cfg['title'], entries=entries),)])}


class MapLegendSplitter(Tool):
    SCHEMA = Schema([Field('Label', FieldType.V_WSTRING, TEXT), Field('Color', FieldType.V_WSTRING, 7),
                     Field('Shape', FieldType.V_WSTRING, 8), Field('Position', FieldType.INT32)])
    manifest = ToolManifest(
        tool_id='core.map_legend_splitter', version='1.0.0', display_name='Map Legend Splitter',
        catalog_ids=('reporting:map-legend-splitter',),
        inputs=(Anchor('input', 'Input', description='Report Map or legend elements'),), outputs=(Anchor('output', 'Output'),),
        description='Split the legend of a Report Map (or a legend element) into one record per entry, with Label, '
                    'Color, Shape and Position, so it can be edited and rebuilt with Map Legend Builder.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'report_field': {'type': 'string', 'default': 'Report'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['report_field']], 'report field')
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        rows = []
        for value in inputs['input'].column(cfg['report_field']):
            doc = reports.parse(value)
            if doc['kind'] == 'legend':
                entries = [(e['label'], e['color'], e['shape']) for e in doc['entries']]
            elif doc['kind'] == 'chart' and doc['chart'].get('type') == 'map':
                entries = [tuple(e) for e in doc['chart'].get('legend') or []]
            else:
                raise DataError(f"Map Legend Splitter needs a map or legend element, not {doc['kind']}")
            rows += [(label, colour, shape, i) for i, (label, colour, shape) in enumerate(entries, start=1)]
        return {'output': Table.from_rows(self.SCHEMA, rows)}


TOOLS = [ReportTable, InteractiveChart, ReportText, ReportImage, Layout, Render, ReportHeader, ReportFooter, Overlay,
         ReportMap, MapLegendBuilder, MapLegendSplitter]
