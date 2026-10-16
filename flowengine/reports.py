"""Report elements and their HTML/PDF renderers.

Reporting tools exchange *report elements*: JSON objects carried in a ``Report`` text field
(``{"format": "flowbench.report.v1", ...}``). An element holds computed content, not rendered output:

* ``table``: columns, already-formatted rows, an optional total row and group label
* ``chart``: a chart specification with the engine-computed data (see :func:`build_chart`)
* ``text``: a text block with style and alignment
* ``image``: a validated PNG or JPEG image, base64-encoded
* ``layout``: child elements arranged vertically or horizontally

:func:`render_html` writes a standalone HTML document (inline SVG charts, embedded images, print CSS).
:func:`render_pdf` lays out the same elements with reportlab: tables split across pages with
repeated headers, and charts are drawn as vector graphics from the same chart operations as the SVG.
All user-supplied text is escaped for the target format.
"""
from __future__ import annotations

import base64
import html
import io
import json
import os
import sys
from pathlib import Path

from . import charts
from .errors import ConfigError, DataError

FORMAT = 'flowbench.report.v1'
PAGE_SIZES_MM = {'A4': (210, 297), 'A3': (297, 420), 'Letter': (215.9, 279.4), 'Legal': (215.9, 355.6)}
TEXT_STYLES = ('title', 'heading', 'body', 'caption')
KINDS = ('table', 'chart', 'text', 'image', 'layout', 'header', 'footer', 'overlay', 'legend')
POSITIONS = ('top_left', 'top_center', 'top_right', 'middle_left', 'middle_center', 'middle_right',
             'bottom_left', 'bottom_center', 'bottom_right')


def element(kind: str, **content) -> str:
    return json.dumps({'format': FORMAT, 'kind': kind, **content}, ensure_ascii=False, separators=(',', ':'))


def parse(value) -> dict:
    if value is None:
        raise DataError('Report field is null; connect the output of a reporting tool')
    try:
        doc = json.loads(value)
    except (TypeError, ValueError):
        raise DataError('Report field does not contain a Flowbench report element') from None
    if not isinstance(doc, dict) or doc.get('format') != FORMAT or doc.get('kind') not in KINDS:
        raise DataError('Report field does not contain a Flowbench report element')
    return doc


# =========================================================================== charts

def build_chart(spec: dict) -> charts.Canvas:
    """Draw a chart specification produced by Interactive Chart."""
    kind = spec['type']
    common = dict(title=spec.get('title'), x_label=spec.get('x_label'), y_label=spec.get('y_label'),
                  width=spec.get('width', 720), height=spec.get('height', 420))
    if kind == 'bar':
        return charts.bar_chart(spec['categories'], spec['series'], stacked=spec.get('stacked', False), **common)
    if kind in ('line', 'area', 'timeseries'):
        series = {k: [tuple(p) for p in v] for k, v in spec['series'].items()}
        if spec.get('categories'):  # categorical x: plot by position and label the axis with the categories
            return _categorical_line(spec, series, area=kind == 'area', **common)
        return charts.line_chart(series, area=kind == 'area', time_axis=kind == 'timeseries' or spec.get('time_axis', False), **common)
    if kind == 'scatter':
        return charts.scatter_chart({k: [tuple(p) for p in v] for k, v in spec['series'].items()}, spec.get('trends'), **common)
    if kind == 'histogram':
        return charts.histogram_chart(spec['edges'], spec['counts'], **common)
    if kind == 'map':
        import shapely
        layers = [{**layer, 'features': [(shapely.from_wkt(w) if w else None, fill, label, tip)
                                         for w, fill, label, tip in layer['features']]} for layer in spec['layers']]
        return charts.map_chart(layers, [tuple(e) for e in spec.get('legend') or []] or None,
                                geographic=spec.get('geographic', True), title=spec.get('title'),
                                width=spec.get('width', 720), height=spec.get('height', 520))
    raise ConfigError(f'Unknown chart type {kind!r}')


def _categorical_line(spec, series, area, title, x_label, y_label, width, height):
    cats = spec['categories']
    names = list(series)
    f = charts.Frame(width, height, title, x_label, y_label, legend=len(names) > 1)
    ys = [p[1] for s in names for p in series[s] if p[1] is not None]
    lo, hi = (min(ys), max(ys)) if ys else (0.0, 1.0)
    y = charts.Linear(min(0.0, lo) if area else lo, hi, f.bottom, f.top, pad=0.05)
    f.y_axis(y)
    band = (f.right - f.left) / max(len(cats), 1)
    centres = [f.left + (i + 0.5) * band for i in range(len(cats))]
    for k, s in enumerate(names):
        pts = [(centres[int(i)], y(v)) for i, v in series[s] if v is not None]
        if area and pts:
            base = y(max(y.lo, 0.0))
            f.c.polygon([(pts[0][0], base)] + pts + [(pts[-1][0], base)], fill=charts.series_color(k), opacity=0.35)
        f.c.polyline(pts, stroke=charts.series_color(k), width=2)
        for px, py in pts:
            f.c.circle(px, py, 2.5, fill=charts.series_color(k))
    f.x_categories([str(c) for c in cats], centres)
    if len(names) > 1:
        f.legend(names)
    return f.c


# =========================================================================== HTML

_CSS = """
body{font-family:Helvetica,Arial,'DejaVu Sans',sans-serif;color:#222;margin:0}
main{padding:24px} .fb-title{font-size:24px;font-weight:bold;margin:0 0 12px}
.fb-heading{font-size:17px;font-weight:bold;margin:14px 0 8px}.fb-body{font-size:12px;margin:6px 0;white-space:pre-wrap}
.fb-caption{font-size:10px;color:#555;margin:4px 0;white-space:pre-wrap}
table.fb{border-collapse:collapse;font-size:11px;margin:8px 0}table.fb th{background:#e9eef4;text-align:left}
table.fb th,table.fb td{border:1px solid #c9d1db;padding:4px 8px}table.fb td.num{text-align:right}
table.fb tr.total td{font-weight:bold;background:#f5f7fa}.fb-group{font-weight:bold;font-size:12px;margin-top:10px}
.fb-row{display:flex;gap:16px;align-items:flex-start}.fb-col{display:flex;flex-direction:column;gap:8px}
.fb-break{break-before:page}svg{max-width:100%;height:auto}img.fb{max-width:100%}
.fb-legend{list-style:none;padding:0;margin:6px 0;font-size:11px}.fb-legend li{margin:3px 0}
.fb-swatch{display:inline-block;width:12px;height:12px;margin-right:6px;vertical-align:middle}
.fb-swatch.fb-point{border-radius:50%}.fb-swatch.fb-line{height:3px}
.fb-overlay{position:relative;display:inline-block}.fb-ov{position:absolute}
.fb-ov-top_left{top:0;left:0}.fb-ov-top_center{top:0;left:50%;transform:translateX(-50%)}.fb-ov-top_right{top:0;right:0}
.fb-ov-middle_left{top:50%;left:0;transform:translateY(-50%)}.fb-ov-middle_center{top:50%;left:50%;transform:translate(-50%,-50%)}
.fb-ov-middle_right{top:50%;right:0;transform:translateY(-50%)}.fb-ov-bottom_left{bottom:0;left:0}
.fb-ov-bottom_center{bottom:0;left:50%;transform:translateX(-50%)}.fb-ov-bottom_right{bottom:0;right:0}
header.fb-page,footer.fb-page{display:flex;align-items:center;gap:12px;color:#444;font-size:11px;padding:8px 24px}
header.fb-page{border-bottom:1px solid #d9dee5}footer.fb-page{border-top:1px solid #d9dee5;justify-content:space-between}
header.fb-page .fb-htitle{font-size:18px;font-weight:bold;color:#222}
"""


def _html_element(doc, depth=0):
    kind = doc['kind']
    if kind == 'text':
        return (f'<div class="fb-{doc.get("style", "body")}" style="text-align:{doc.get("align", "left")}">'
                f'{html.escape(doc["text"])}</div>')
    if kind == 'table':
        parts = []
        if doc.get('title'):
            parts.append(f'<div class="fb-heading">{html.escape(doc["title"])}</div>')
        if doc.get('group') is not None:
            parts.append(f'<div class="fb-group">{html.escape(str(doc["group"]))}</div>')
        cols = doc['columns']
        head = ''.join(f'<th>{html.escape(c["label"])}</th>' for c in cols)
        body = []
        for row in doc['rows']:
            body.append('<tr>' + ''.join(f'<td class="{"num" if c["align"] == "right" else ""}">{html.escape(v or "")}</td>'
                                         for c, v in zip(cols, row)) + '</tr>')
        if doc.get('total_row'):
            body.append('<tr class="total">' + ''.join(f'<td class="{"num" if c["align"] == "right" else ""}">{html.escape(v or "")}</td>'
                                                       for c, v in zip(cols, doc['total_row'])) + '</tr>')
        parts.append(f'<table class="fb"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>')
        return ''.join(parts)
    if kind == 'chart':
        return f'<figure style="margin:8px 0">{build_chart(doc["chart"]).to_svg()}</figure>'
    if kind == 'image':
        width = f' style="width:{doc["display_width"]}pt"' if doc.get('display_width') else ''
        return (f'<img class="fb" alt="{html.escape(doc.get("alt") or "")}"{width} '
                f'src="data:image/{doc["image_format"]};base64,{doc["data"]}">')
    if kind == 'legend':
        items = ''.join(f'<li><span class="fb-swatch fb-{html.escape(e["shape"])}" style="background:{_safe_colour(e["color"])}"></span>'
                        f'{html.escape(e["label"])}</li>' for e in doc['entries'])
        title = f'<div class="fb-heading">{html.escape(doc["title"])}</div>' if doc.get('title') else ''
        return f'{title}<ul class="fb-legend">{items}</ul>'
    if kind == 'overlay':
        slots = ''.join(f'<div class="fb-ov fb-ov-{p}" style="padding:{int(doc.get("padding", 8))}px">{_html_element(child, depth + 1)}</div>'
                        for p, child in doc['items'].items())
        return f'<div class="fb-overlay">{_html_element(doc["base"], depth + 1)}{slots}</div>'
    if kind in ('header', 'footer'):
        return ''  # placed by render_html
    if kind == 'layout':
        cls = 'fb-row' if doc['direction'] == 'horizontal' else 'fb-col'
        title = f'<div class="fb-heading">{html.escape(doc["title"])}</div>' if doc.get('title') else ''
        children = ''.join(f'<div>{_html_element(child, depth + 1)}</div>' for child in doc['items'])
        return f'{title}<div class="{cls}" style="gap:{int(doc.get("gap", 16))}px">{children}</div>'
    raise DataError(f'Unknown report element {kind!r}')


def _safe_colour(value):
    import re
    return value if isinstance(value, str) and re.fullmatch(r'#[0-9A-Fa-f]{6}', value) else '#999999'


def split_page_furniture(elements):
    """(header, footer, body): the first header and footer, at any layout depth, decorate every page."""
    found = {}

    def strip(items):
        kept = []
        for e in items:
            if e['kind'] in ('header', 'footer'):
                found.setdefault(e['kind'], e)
            elif e['kind'] == 'layout':
                kept.append({**e, 'items': strip(e['items'])})
            else:
                kept.append(e)
        return kept
    body = strip(elements)
    return found.get('header'), found.get('footer'), body


def footer_text(template, page, pages, date):
    return (template or '').replace('{page}', str(page)).replace('{pages}', str(pages)).replace('{date}', date)


def render_html(elements, path: Path, *, title, page_size, orientation, margin_mm, page_breaks) -> dict:
    w, h = PAGE_SIZES_MM[page_size]
    if orientation == 'landscape':
        w, h = h, w
    header, footer, elements = split_page_furniture(elements)
    body = []
    if header:
        image = (f'<img alt="" style="height:{int(header.get("image_height") or 40)}px" '
                 f'src="data:image/{header["image"]["image_format"]};base64,{header["image"]["data"]}">') if header.get('image') else ''
        lines = ''.join(f'<div>{html.escape(t)}</div>' for t in (header.get('subtitle'), header.get('date_text')) if t)
        body.append(f'<header class="fb-page">{image}<div><div class="fb-htitle">{html.escape(header.get("title") or "")}</div>{lines}</div></header>')
    for i, doc in enumerate(elements):
        cls = ' class="fb-break"' if page_breaks and i else ''
        body.append(f'<section{cls}>{_html_element(doc)}</section>')
    if footer:
        date = footer.get('date_text') or ''
        # A screen page has no page numbers: cells that use {page}/{pages} appear only in PDF output.
        cells = ''.join(f'<span>{html.escape(footer_text(footer.get(k), 1, 1, date))}</span>'
                        if footer.get(k) and '{page' not in footer[k] else '<span></span>' for k in ('left', 'center', 'right'))
        foot = f'<footer class="fb-page">{cells}</footer>'
    else:
        foot = ''
    head = body.pop(0) if header else ''
    text = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<style>@page{{size:{w}mm {h}mm;margin:{margin_mm}mm}}{_CSS}</style></head>'
            f'<body>{head}<main>{"".join(body)}</main>{foot}</body></html>')
    data = text.encode('utf-8')
    _write(path, data)
    return {'pages': None, 'bytes': len(data)}


# =========================================================================== PDF

_FONT_CANDIDATES = {
    'win32': [('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
              ('C:/Windows/Fonts/segoeui.ttf', 'C:/Windows/Fonts/segoeuib.ttf')],
    'darwin': [('/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf'),
               ('/Library/Fonts/Arial.ttf', '/Library/Fonts/Arial Bold.ttf')],
    'linux': [('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
              ('/usr/share/fonts/TTF/DejaVuSans.ttf', '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf'),
              ('/usr/share/fonts/dejavu/DejaVuSans.ttf', '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf'),
              ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
               '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf')],
}


def resolve_fonts(choice: str, font_path: str | None, warn):
    """Register a Unicode TrueType font if one is available; otherwise use built-in Helvetica (Latin-1 only)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if choice == 'helvetica':
        return 'Helvetica', 'Helvetica-Bold', False
    candidates = []
    if font_path:
        candidates.append((font_path, font_path))
    platform = 'win32' if sys.platform.startswith('win') else 'darwin' if sys.platform == 'darwin' else 'linux'
    candidates += _FONT_CANDIDATES[platform]
    for regular, bold in candidates:
        if os.path.isfile(regular):
            try:
                pdfmetrics.registerFont(TTFont('FlowbenchSans', regular))
                pdfmetrics.registerFont(TTFont('FlowbenchSans-Bold', bold if os.path.isfile(bold) else regular))
                return 'FlowbenchSans', 'FlowbenchSans-Bold', True
            except Exception:
                continue
    if font_path:
        raise ConfigError(f'Font file {font_path!r} could not be loaded as a TrueType font')
    warn('No Unicode TrueType font was found; using Helvetica. Characters outside Latin-1 are replaced with "?".')
    return 'Helvetica', 'Helvetica-Bold', False


def render_pdf(elements, path: Path, *, title, page_size, orientation, margin_mm, page_breaks, font, font_path, warn) -> dict:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (Flowable, Image, LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)
    regular, bold, unicode_font = resolve_fonts(font, font_path, warn)
    replaced = [0]

    def safe(text):
        text = '' if text is None else str(text)
        if not unicode_font:
            encoded = text.encode('latin-1', 'replace').decode('latin-1')
            replaced[0] += sum(1 for a, b in zip(text, encoded) if a != b)
            text = encoded
        return text

    def para_text(text):
        return html.escape(safe(text)).replace('\n', '<br/>')

    size = PAGE_SIZES_MM[page_size]
    pw, ph = (size[1], size[0]) if orientation == 'landscape' else size
    page = (pw * mm, ph * mm)
    margin = margin_mm * mm
    frame_w, frame_h = page[0] - 2 * margin, page[1] - 2 * margin
    align = {'left': TA_LEFT, 'center': TA_CENTER, 'right': TA_RIGHT}
    base = {'title': (20, bold, 10), 'heading': (14, bold, 6), 'body': (10, regular, 4), 'caption': (8, regular, 3)}

    def style(name, alignment='left'):
        fs, fn, after = base[name]
        return ParagraphStyle(name, fontName=fn, fontSize=fs, leading=fs * 1.25, spaceAfter=after,
                              alignment=align.get(alignment, TA_LEFT), textColor=colors.HexColor('#222222'))

    class ChartFlowable(Flowable):
        def __init__(self, canvas, max_w):
            super().__init__()
            self.canvas_ = canvas
            # Chart pixels map to points, scaled to fit the available width and at most 90% of the page height.
            self.scale = min(max_w / canvas.width, (frame_h * 0.9) / canvas.height)
            self.width, self.height = canvas.width * self.scale, canvas.height * self.scale

        def draw(self):
            for kind, op in self.canvas_.ops:
                if kind == 'text':
                    op['s'] = safe(op['s'])
            charts.draw_reportlab(self.canvas_, self.canv, 0, self.height, self.scale, font=regular, bold_font=bold)

    def flow(doc, max_w):
        kind = doc['kind']
        if kind == 'text':
            return [Paragraph(para_text(doc['text']), style(doc.get('style', 'body'), doc.get('align', 'left')))]
        if kind == 'table':
            out = []
            if doc.get('title'):
                out.append(Paragraph(para_text(doc['title']), style('heading')))
            if doc.get('group') is not None:
                out.append(Paragraph(para_text(doc['group']), style('body')))
            cols = doc['columns']
            cell = ParagraphStyle('cell', fontName=regular, fontSize=8, leading=10)
            head = ParagraphStyle('head', fontName=bold, fontSize=8, leading=10)
            right = ParagraphStyle('num', parent=cell, alignment=TA_RIGHT)
            data = [[Paragraph(para_text(c['label']), head) for c in cols]]
            for row in doc['rows'] + ([doc['total_row']] if doc.get('total_row') else []):
                data.append([Paragraph(para_text(v), right if c['align'] == 'right' else cell) for c, v in zip(cols, row)])
            widths = _column_widths(doc, max_w)
            table = LongTable(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
            commands = [('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#c9d1db')),
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e9eef4')),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TOPPADDING', (0, 0), (-1, -1), 2),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 2)]
            if doc.get('total_row'):
                commands.append(('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f5f7fa')))
            table.setStyle(TableStyle(commands))
            out += [table, Spacer(1, 8)]
            return out
        if kind == 'chart':
            return [ChartFlowable(build_chart(doc['chart']), max_w), Spacer(1, 8)]
        if kind == 'image':
            raw = base64.b64decode(doc['data'])
            w_pt = doc.get('display_width') or doc['width'] * 0.75
            w_pt = min(w_pt, max_w)
            h_pt = w_pt * doc['height'] / doc['width']
            if h_pt > frame_h * 0.95:
                h_pt = frame_h * 0.95
                w_pt = h_pt * doc['width'] / doc['height']
            return [Image(io.BytesIO(raw), width=w_pt, height=h_pt), Spacer(1, 6)]
        if kind == 'legend':
            out = [Paragraph(para_text(doc['title']), style('heading'))] if doc.get('title') else []
            rows = [[_Swatch(e['color'], e['shape']), Paragraph(para_text(e['label']), style('body'))] for e in doc['entries']]
            if rows:
                grid = Table(rows, colWidths=[16, max_w - 16], hAlign='LEFT')
                grid.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                          ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1)]))
                out.append(grid)
            return out
        if kind == 'overlay':
            base = flow(doc['base'], max_w)
            base_flow = next((f for f in base if not isinstance(f, Spacer)), None)
            if base_flow is None:
                return base
            items = {p: flow(child, max_w / 2) for p, child in doc['items'].items()}
            return [_Overlay(base_flow, items, float(doc.get('padding', 8)) * 0.75), Spacer(1, 8)]
        if kind in ('header', 'footer'):
            return []
        if kind == 'layout':
            out = [Paragraph(para_text(doc['title']), style('heading'))] if doc.get('title') else []
            if doc['direction'] == 'horizontal' and doc['items']:
                gap = float(doc.get('gap', 16)) * 0.75
                items = doc['items']
                # Images with a display width keep it; the other elements share the remaining width.
                fixed = [min(float(c['display_width']), max_w / 2) if c['kind'] == 'image' and c.get('display_width') else None
                         for c in items]
                free = max_w - gap * (len(items) - 1) - sum(w for w in fixed if w)
                share = free / max(sum(1 for w in fixed if w is None), 1)
                widths = [w if w else share for w in fixed]
                cells = [flow(child, w) for child, w in zip(items, widths)]
                grid = Table([cells], colWidths=[w + (gap if i < len(cells) - 1 else 0) for i, w in enumerate(widths)])
                grid.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                          ('RIGHTPADDING', (0, 0), (-1, -1), gap)]))
                out.append(grid)
            else:
                for child in doc['items']:
                    out += flow(child, max_w)
            return out
        raise DataError(f'Unknown report element {kind!r}')

    header, footer, elements = split_page_furniture(elements)
    head_h = 0.0
    if header:
        head_h = max(float(header.get('image_height') or 0) * 0.75 if header.get('image') else 0, 34.0) + 10
    foot_h = 22.0 if footer else 0.0

    def decorate(canvas_, doc_, total):
        canvas_.saveState()
        top = page[1] - margin
        if header:
            x = margin
            if header.get('image'):
                ih = float(header.get('image_height') or 40) * 0.75
                iw = ih * header['image']['width'] / header['image']['height']
                from reportlab.lib.utils import ImageReader
                canvas_.drawImage(ImageReader(io.BytesIO(base64.b64decode(header['image']['data']))), x, top - ih, iw, ih, mask='auto')
                x += iw + 10
            canvas_.setFont(bold, 15)
            canvas_.setFillColor(colors.HexColor('#222222'))
            canvas_.drawString(x, top - 15, safe(header.get('title') or ''))
            canvas_.setFont(regular, 9)
            line_y = top - 28
            for t in (header.get('subtitle'), header.get('date_text')):
                if t:
                    canvas_.drawString(x, line_y, safe(t))
                    line_y -= 11
            canvas_.setStrokeColor(colors.HexColor('#d9dee5'))
            canvas_.line(margin, top - head_h + 4, page[0] - margin, top - head_h + 4)
        if footer:
            date = footer.get('date_text') or ''
            y = margin - 4
            canvas_.setStrokeColor(colors.HexColor('#d9dee5'))
            canvas_.line(margin, margin + foot_h - 8, page[0] - margin, margin + foot_h - 8)
            canvas_.setFont(regular, 8)
            canvas_.setFillColor(colors.HexColor('#444444'))
            page_no = canvas_.getPageNumber()
            for key, draw, x in (('left', canvas_.drawString, margin), ('center', canvas_.drawCentredString, page[0] / 2),
                                 ('right', canvas_.drawRightString, page[0] - margin)):
                if footer.get(key):
                    draw(x, y + 6, safe(footer_text(footer[key], page_no, total, date)))
        canvas_.restoreState()

    story = []
    for i, doc in enumerate(elements):
        if page_breaks and i:
            story.append(PageBreak())
        story += flow(doc, frame_w)
    while story and isinstance(story[-1], Spacer):
        story.pop()  # a trailing spacer must not spill onto an empty page
    import copy as _copy
    pages = [0]

    def build(total):
        buffer = io.BytesIO()
        pdf = SimpleDocTemplate(buffer, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=margin + head_h,
                                bottomMargin=margin + foot_h, title=safe(title), author='Flowbench', creator='Flowbench')

        def on_page(canvas_, doc_):
            pages[0] = canvas_.getPageNumber()
            decorate(canvas_, doc_, total)
        pdf.build(_copy.deepcopy(story) if total is None else story, onFirstPage=on_page, onLaterPages=on_page)
        return buffer
    try:
        buffer = build(None) if not (footer and any('{pages}' in (footer.get(k) or '') for k in ('left', 'center', 'right'))) \
            else (build(None), build(pages[0]))[1]  # two passes so "of {pages}" is known
    except Exception as e:  # e.g. a cell taller than a page
        raise DataError(f'PDF layout failed: {type(e).__name__}: {e}') from None
    if replaced[0]:
        warn(f'{replaced[0]} character(s) could not be drawn with Helvetica and were replaced with "?"')
    data = buffer.getvalue()
    _write(path, data)
    return {'pages': pages[0], 'bytes': len(data)}


def _Swatch(colour, shape):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Flowable

    class Swatch(Flowable):
        def wrap(self, *_):
            return 12, 12

        def draw(self):
            self.canv.setFillColor(HexColor(_safe_colour(colour)))
            if shape == 'point':
                self.canv.circle(6, 6, 5, fill=1, stroke=0)
            elif shape == 'line':
                self.canv.rect(0, 5, 12, 3, fill=1, stroke=0)
            else:
                self.canv.rect(0, 0, 12, 12, fill=1, stroke=0)
    return Swatch()


def _Overlay(base, items, padding):
    """Draw a base flowable with other flowables placed at nine anchor positions over it."""
    from reportlab.platypus import Flowable

    class Overlay(Flowable):
        def wrap(self, aw, ah):
            self.w, self.h = base.wrap(aw, ah)
            return self.w, self.h

        def draw(self):
            base.drawOn(self.canv, 0, 0)
            for position, flowables in items.items():
                for f in flowables[:1]:
                    fw, fh = f.wrap(self.w / 2, self.h / 2)
                    vertical, horizontal = position.split('_')
                    x = {'left': padding, 'center': (self.w - fw) / 2, 'right': self.w - fw - padding}[horizontal]
                    y = {'top': self.h - fh - padding, 'middle': (self.h - fh) / 2, 'bottom': padding}[vertical]
                    f.drawOn(self.canv, x, y)
    return Overlay()


def _column_widths(doc, max_w):
    cols = doc['columns']
    rows = doc['rows'][:200] + ([doc['total_row']] if doc.get('total_row') else [])
    # Width in points: longest cell, but never narrower than the longest word of the header.
    want = [max([len(c['label'])] + [len(r[i] or '') for r in rows]) + 2 for i, c in enumerate(cols)]
    floor = [max(len(word) for word in (c['label'] or ' ').split()) * 6.2 + 14 for c in cols]  # bold 8pt header
    natural = [max(w * 5.2, f) for w, f in zip(want, floor)]
    if sum(natural) <= max_w:
        return natural
    spare = max_w - sum(floor)
    if spare <= 0:
        return [max_w * f / sum(floor) for f in floor]
    extra = [n - f for n, f in zip(natural, floor)]
    return [f + spare * e / (sum(extra) or 1) for f, e in zip(floor, extra)]


def _write(path: Path, data: bytes):
    if not path.parent.is_dir():
        raise DataError(f'Output folder does not exist: {path.parent}')
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(data)
    tmp.replace(path)
