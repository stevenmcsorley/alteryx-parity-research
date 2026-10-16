"""Small deterministic chart renderer shared by graphical tools and reports.

Charts are built from primitive drawing operations on a :class:`Canvas` (rect, line, polyline, polygon,
circle, text) in pixel coordinates with the origin at the top left. Two backends draw the same
operations: :meth:`Canvas.to_svg` for the desktop and HTML reports, and :func:`draw_reportlab` for PDF.
All text is escaped. The SVG is self-contained (no scripts, external references or fonts) and is
shown in the desktop through an ``<img>`` data URL.

The builders (bar, line/area, scatter, histogram, heatmap, means, violin) take already-computed data.
They render what the engine calculated and never invent values.
"""
from __future__ import annotations

import datetime as _dt
import math
from xml.sax.saxutils import escape, quoteattr

CATEGORICAL = ('#0072B2', '#E69F00', '#009E73', '#D55E00', '#CC79A7', '#56B4E9', '#F0E442', '#000000')  # Okabe-Ito
_VIRIDIS = ((68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37))
_BLUES = ((247, 251, 255), (198, 219, 239), (107, 174, 214), (33, 113, 181), (8, 48, 107))
_REDS = ((255, 245, 240), (252, 187, 161), (251, 106, 74), (203, 24, 29), (103, 0, 13))
_DIVERGING = ((33, 102, 172), (146, 197, 222), (247, 247, 247), (244, 165, 130), (178, 24, 43))
PALETTES = {'viridis': _VIRIDIS, 'blues': _BLUES, 'reds': _REDS, 'diverging': _DIVERGING}
AXIS, GRID, INK = '#444444', '#e3e3e3', '#222222'


def color_at(palette: str, t: float) -> str:
    stops = PALETTES[palette]
    t = 0.0 if t is None or math.isnan(t) else min(1.0, max(0.0, t))
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    f = pos - i
    rgb = [round(a + (b - a) * f) for a, b in zip(stops[i], stops[i + 1])]
    return '#%02x%02x%02x' % tuple(rgb)


def series_color(i: int) -> str:
    return CATEGORICAL[i % len(CATEGORICAL)]


class Canvas:
    def __init__(self, width: int, height: int, title: str | None = None):
        self.width, self.height, self.title, self.ops = int(width), int(height), title, []

    def rect(self, x, y, w, h, fill='none', stroke=None, opacity=1.0, title=None):
        self.ops.append(('rect', dict(x=x, y=y, w=max(w, 0), h=max(h, 0), fill=fill, stroke=stroke, opacity=opacity, title=title)))

    def line(self, x1, y1, x2, y2, stroke=AXIS, width=1.0, dash=None):
        self.ops.append(('line', dict(x1=x1, y1=y1, x2=x2, y2=y2, stroke=stroke, width=width, dash=dash)))

    def polyline(self, points, stroke, width=1.5):
        if len(points) > 1:
            self.ops.append(('polyline', dict(points=list(points), stroke=stroke, width=width)))

    def polygon(self, points, fill, stroke=None, opacity=1.0):
        if len(points) > 2:
            self.ops.append(('polygon', dict(points=list(points), fill=fill, stroke=stroke, opacity=opacity)))

    def circle(self, cx, cy, r, fill, stroke=None, opacity=1.0, title=None):
        self.ops.append(('circle', dict(cx=cx, cy=cy, r=r, fill=fill, stroke=stroke, opacity=opacity, title=title)))

    def text(self, x, y, s, size=11, anchor='start', weight='normal', fill=INK, rotate=0):
        self.ops.append(('text', dict(x=x, y=y, s=str(s), size=size, anchor=anchor, weight=weight, fill=fill, rotate=rotate)))

    # ------------------------------------------------------------------ SVG backend
    def to_svg(self) -> str:
        n = lambda v: f'{v:.2f}'.rstrip('0').rstrip('.')
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
               f'viewBox="0 0 {self.width} {self.height}" font-family="Helvetica, Arial, sans-serif">']
        if self.title:
            out.append(f'<title>{escape(self.title)}</title>')
        out.append(f'<rect width="{self.width}" height="{self.height}" fill="#ffffff"/>')
        for kind, o in self.ops:
            stroke = f' stroke="{o["stroke"]}"' if o.get('stroke') else ''
            opacity = f' fill-opacity="{n(o["opacity"])}"' if o.get('opacity', 1) != 1 else ''
            tip = f'<title>{escape(o["title"])}</title>' if o.get('title') else ''
            if kind == 'rect':
                attrs = f'x="{n(o["x"])}" y="{n(o["y"])}" width="{n(o["w"])}" height="{n(o["h"])}" fill="{o["fill"]}"{stroke}{opacity}'
                out.append(f'<rect {attrs}>{tip}</rect>' if tip else f'<rect {attrs}/>')
            elif kind == 'line':
                dash = f' stroke-dasharray="{o["dash"]}"' if o['dash'] else ''
                out.append(f'<line x1="{n(o["x1"])}" y1="{n(o["y1"])}" x2="{n(o["x2"])}" y2="{n(o["y2"])}" '
                           f'stroke="{o["stroke"]}" stroke-width="{n(o["width"])}"{dash}/>')
            elif kind in ('polyline', 'polygon'):
                pts = ' '.join(f'{n(x)},{n(y)}' for x, y in o['points'])
                if kind == 'polyline':
                    out.append(f'<polyline points="{pts}" fill="none" stroke="{o["stroke"]}" stroke-width="{n(o["width"])}" stroke-linejoin="round"/>')
                else:
                    out.append(f'<polygon points="{pts}" fill="{o["fill"]}"{stroke}{opacity}/>')
            elif kind == 'circle':
                attrs = f'cx="{n(o["cx"])}" cy="{n(o["cy"])}" r="{n(o["r"])}" fill="{o["fill"]}"{stroke}{opacity}'
                out.append(f'<circle {attrs}>{tip}</circle>' if tip else f'<circle {attrs}/>')
            elif kind == 'text':
                rot = f' transform="rotate({n(o["rotate"])} {n(o["x"])} {n(o["y"])})"' if o['rotate'] else ''
                weight = ' font-weight="bold"' if o['weight'] == 'bold' else ''
                out.append(f'<text x="{n(o["x"])}" y="{n(o["y"])}" font-size="{n(o["size"])}" text-anchor="{o["anchor"]}" '
                           f'fill="{o["fill"]}"{weight}{rot}>{escape(o["s"])}</text>')
        out.append('</svg>')
        return ''.join(out)


def draw_reportlab(canvas: Canvas, pdf, x0: float, y0: float, scale: float = 1.0):
    """Paint ``canvas`` onto a reportlab canvas with its top-left corner at (x0, y0) in PDF points."""
    from reportlab.lib.colors import HexColor

    def X(v):
        return x0 + v * scale

    def Y(v):
        return y0 - v * scale

    def color(value):
        return HexColor(value) if value and value != 'none' else None

    for kind, o in canvas.ops:
        pdf.saveState()
        if kind == 'rect':
            fill, stroke = color(o['fill']), color(o.get('stroke'))
            if fill:
                pdf.setFillColor(fill, alpha=o['opacity'])
            if stroke:
                pdf.setStrokeColor(stroke)
            pdf.rect(X(o['x']), Y(o['y'] + o['h']), o['w'] * scale, o['h'] * scale, fill=bool(fill), stroke=bool(stroke))
        elif kind == 'line':
            pdf.setStrokeColor(color(o['stroke']))
            pdf.setLineWidth(o['width'] * scale)
            if o['dash']:
                pdf.setDash([float(v) * scale for v in str(o['dash']).split()])
            pdf.line(X(o['x1']), Y(o['y1']), X(o['x2']), Y(o['y2']))
        elif kind in ('polyline', 'polygon'):
            path = pdf.beginPath()
            (fx, fy), rest = o['points'][0], o['points'][1:]
            path.moveTo(X(fx), Y(fy))
            for px, py in rest:
                path.lineTo(X(px), Y(py))
            if kind == 'polygon':
                path.close()
                pdf.setFillColor(color(o['fill']), alpha=o['opacity'])
                if o.get('stroke'):
                    pdf.setStrokeColor(color(o['stroke']))
                pdf.drawPath(path, fill=1, stroke=1 if o.get('stroke') else 0)
            else:
                pdf.setStrokeColor(color(o['stroke']))
                pdf.setLineWidth(o['width'] * scale)
                pdf.drawPath(path, fill=0, stroke=1)
        elif kind == 'circle':
            pdf.setFillColor(color(o['fill']), alpha=o['opacity'])
            if o.get('stroke'):
                pdf.setStrokeColor(color(o['stroke']))
            pdf.circle(X(o['cx']), Y(o['cy']), o['r'] * scale, fill=1, stroke=1 if o.get('stroke') else 0)
        elif kind == 'text':
            pdf.setFillColor(color(o['fill']))
            pdf.setFont('Helvetica-Bold' if o['weight'] == 'bold' else 'Helvetica', o['size'] * scale)
            pdf.translate(X(o['x']), Y(o['y']))
            if o['rotate']:
                pdf.rotate(-o['rotate'])
            draw = {'start': pdf.drawString, 'middle': pdf.drawCentredString, 'end': pdf.drawRightString}[o['anchor']]
            draw(0, 0, o['s'])
        pdf.restoreState()


# =========================================================================== scales and axes

def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / count))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= count:
            step *= m
            break
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 1e-9:
        if v >= lo - step * 1e-9:
            ticks.append(round(v, 12))
        v += step
    return ticks


def fmt_number(v: float) -> str:
    if v == 0:
        return '0'
    if abs(v) >= 1e6 or abs(v) < 1e-3:
        return f'{v:.3g}'
    return f'{v:,.6g}' if abs(v) >= 1000 else f'{v:.6g}'


class Linear:
    def __init__(self, lo, hi, a, b, pad=0.0, ticks=5, formatter=None):
        lo, hi = float(lo), float(hi)
        if lo == hi:
            lo, hi = lo - 0.5, hi + 0.5
        span = hi - lo
        self.lo, self.hi = lo - span * pad, hi + span * pad
        self.a, self.b, self.count, self.formatter = a, b, ticks, formatter or fmt_number

    def __call__(self, v):
        return self.a + (float(v) - self.lo) / (self.hi - self.lo) * (self.b - self.a)

    def ticks(self):
        return [t for t in nice_ticks(self.lo, self.hi, self.count) if self.lo <= t <= self.hi]


def time_formatter(lo: float, hi: float):
    span = hi - lo
    fmt = '%Y' if span > 3 * 365 * 86400 else '%Y-%m' if span > 60 * 86400 else '%Y-%m-%d' if span > 2 * 86400 else '%m-%d %H:%M'
    return lambda v: _dt.datetime.fromtimestamp(v, _dt.timezone.utc).strftime(fmt)


def to_timestamp(v) -> float:
    if isinstance(v, _dt.datetime):
        return (v.replace(tzinfo=_dt.timezone.utc) if v.tzinfo is None else v).timestamp()
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day, tzinfo=_dt.timezone.utc).timestamp()
    raise TypeError(f'not a date or datetime: {v!r}')


class Frame:
    """Plot area with margins, title and axis labels."""

    def __init__(self, width=720, height=420, title=None, x_label=None, y_label=None, legend=False):
        self.c = Canvas(width, height, title)
        self.left, self.right = 70, width - (170 if legend else 24)
        self.top, self.bottom = 40 if title else 20, height - 56
        if title:
            self.c.text(width / 2, 24, title, size=15, anchor='middle', weight='bold')
        if x_label:
            self.c.text((self.left + self.right) / 2, height - 14, x_label, size=12, anchor='middle')
        if y_label:
            self.c.text(16, (self.top + self.bottom) / 2, y_label, size=12, anchor='middle', rotate=-90)

    def y_axis(self, scale: Linear):
        for t in scale.ticks():
            y = scale(t)
            self.c.line(self.left, y, self.right, y, stroke=GRID)
            self.c.text(self.left - 6, y + 4, scale.formatter(t), size=10, anchor='end', fill=AXIS)
        self.c.line(self.left, self.top, self.left, self.bottom)

    def x_axis(self, scale: Linear):
        for t in scale.ticks():
            x = scale(t)
            self.c.line(x, self.bottom, x, self.bottom + 4)
            self.c.text(x, self.bottom + 17, scale.formatter(t), size=10, anchor='middle', fill=AXIS)
        self.c.line(self.left, self.bottom, self.right, self.bottom)

    def x_categories(self, labels, centres):
        rotate = len(labels) > 8 or max((len(l) for l in labels), default=0) * 6 > (self.right - self.left) / max(len(labels), 1)
        for label, x in zip(labels, centres):
            text = label if len(label) <= 24 else label[:23] + '…'
            if rotate:
                self.c.text(x, self.bottom + 12, text, size=10, anchor='end', fill=AXIS, rotate=-40)
            else:
                self.c.text(x, self.bottom + 17, text, size=10, anchor='middle', fill=AXIS)
        self.c.line(self.left, self.bottom, self.right, self.bottom)

    def legend(self, names):
        x, y = self.right + 16, self.top + 6
        for i, name in enumerate(names):
            self.c.rect(x, y + i * 18 - 9, 12, 12, fill=series_color(i))
            self.c.text(x + 18, y + i * 18 + 1, name if len(name) <= 20 else name[:19] + '…', size=11)


def _extent(values):
    finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return (min(finite), max(finite)) if finite else (0.0, 1.0)


# =========================================================================== chart builders

def bar_chart(categories, series: dict, title=None, x_label=None, y_label=None, stacked=False, width=720, height=420):
    names = list(series)
    f = Frame(width, height, title, x_label, y_label, legend=len(names) > 1)
    totals = [sum(max(series[s][i] or 0, 0) for s in names) for i in range(len(categories))] if stacked else []
    values = totals if stacked else [v for s in names for v in series[s] if v is not None]
    lo = min(0.0, min(values, default=0.0))
    hi = max(0.0, max(values, default=1.0))
    y = Linear(lo, hi, f.bottom, f.top, pad=0.05)
    f.y_axis(y)
    band = (f.right - f.left) / max(len(categories), 1)
    inner = band * 0.8
    centres = []
    for i, cat in enumerate(categories):
        x0 = f.left + i * band + (band - inner) / 2
        centres.append(x0 + inner / 2)
        base = 0.0
        for k, s in enumerate(names):
            v = series[s][i]
            if v is None:
                continue
            if stacked:
                f.c.rect(x0, y(base + v), inner, y(base) - y(base + v), fill=series_color(k), title=f'{cat} · {s}: {fmt_number(v)}')
                base += v
            else:
                w = inner / len(names)
                top, bot = (y(v), y(0)) if v >= 0 else (y(0), y(v))
                f.c.rect(x0 + k * w, top, w, bot - top, fill=series_color(k), title=f'{cat} · {s}: {fmt_number(v)}')
    f.x_categories([str(c) for c in categories], centres)
    if len(names) > 1:
        f.legend(names)
    return f.c


def line_chart(series: dict, title=None, x_label=None, y_label=None, area=False, time_axis=False, width=720, height=420):
    """series: name -> [(x, y), ...] with numeric x (or timestamps when time_axis)."""
    names = list(series)
    f = Frame(width, height, title, x_label, y_label, legend=len(names) > 1)
    xs = [p[0] for s in names for p in series[s]]
    ys = [p[1] for s in names for p in series[s] if p[1] is not None]
    xlo, xhi = _extent(xs)
    ylo, yhi = _extent(ys)
    if area:
        ylo = min(0.0, ylo)
    x = Linear(xlo, xhi, f.left, f.right, formatter=time_formatter(xlo, xhi) if time_axis else None)
    y = Linear(ylo, yhi, f.bottom, f.top, pad=0.05)
    f.y_axis(y)
    f.x_axis(x)
    for k, s in enumerate(names):
        pts = sorted((px, py) for px, py in series[s] if py is not None)
        mapped = [(x(px), y(py)) for px, py in pts]
        if area and mapped:
            f.c.polygon([(mapped[0][0], y(max(ylo, 0) if ylo >= 0 else ylo))] + mapped + [(mapped[-1][0], y(max(ylo, 0) if ylo >= 0 else ylo))],
                        fill=series_color(k), opacity=0.35)
        f.c.polyline(mapped, stroke=series_color(k), width=2)
    if len(names) > 1:
        f.legend(names)
    return f.c


def scatter_chart(groups: dict, trends: dict | None = None, title=None, x_label=None, y_label=None, width=720, height=420):
    """groups: name -> [(x, y)], trends: name -> (slope, intercept)."""
    names = list(groups)
    f = Frame(width, height, title, x_label, y_label, legend=len(names) > 1)
    xlo, xhi = _extent(p[0] for g in names for p in groups[g])
    ylo, yhi = _extent(p[1] for g in names for p in groups[g])
    x, y = Linear(xlo, xhi, f.left, f.right, pad=0.03), Linear(ylo, yhi, f.bottom, f.top, pad=0.05)
    f.y_axis(y)
    f.x_axis(x)
    for k, g in enumerate(names):
        for px, py in groups[g]:
            f.c.circle(x(px), y(py), 3, fill=series_color(k), opacity=0.7)
    for k, g in enumerate(names):
        if trends and trends.get(g):
            slope, intercept = trends[g]
            gx = [p[0] for p in groups[g]]
            a, b = min(gx), max(gx)
            f.c.line(x(a), y(intercept + slope * a), x(b), y(intercept + slope * b), stroke=series_color(k), width=2, dash='6 3')
    if len(names) > 1:
        f.legend(names)
    return f.c


def histogram_chart(edges, counts, title=None, x_label=None, y_label='Count', width=720, height=420):
    f = Frame(width, height, title, x_label, y_label)
    x = Linear(edges[0], edges[-1], f.left, f.right)
    y = Linear(0, max(counts, default=1) or 1, f.bottom, f.top, pad=0.05)
    f.y_axis(y)
    f.x_axis(x)
    for i, c in enumerate(counts):
        x0, x1 = x(edges[i]), x(edges[i + 1])
        f.c.rect(x0, y(c), max(x1 - x0 - 1, 0.5), y(0) - y(c), fill=series_color(0),
                 title=f'[{fmt_number(edges[i])}, {fmt_number(edges[i + 1])}): {c}')
    return f.c


def heatmap_chart(x_labels, y_labels, matrix, palette='viridis', title=None, x_label=None, y_label=None,
                  value_label='Value', lo=None, hi=None, annotate=None, width=720, height=480):
    f = Frame(width, height, title, x_label, y_label, legend=True)
    f.left = max(f.left, 14 + 6 * min(max((len(str(l)) for l in y_labels), default=4), 24))
    finite = [v for row in matrix for v in row if v is not None and math.isfinite(v)]
    lo = min(finite, default=0.0) if lo is None else lo
    hi = max(finite, default=1.0) if hi is None else hi
    cw = (f.right - f.left) / max(len(x_labels), 1)
    ch = (f.bottom - f.top) / max(len(y_labels), 1)
    annotate = (len(x_labels) * len(y_labels) <= 400) if annotate is None else annotate
    for r, ylab in enumerate(y_labels):
        for k, xlab in enumerate(x_labels):
            v = matrix[r][k]
            t = None if v is None or hi == lo else (v - lo) / (hi - lo)
            fill = '#f2f2f2' if v is None else color_at(palette, t if t is not None else 0.5)
            f.c.rect(f.left + k * cw, f.top + r * ch, cw, ch, fill=fill, stroke='#ffffff',
                     title=f'{ylab} × {xlab}: {"null" if v is None else fmt_number(v)}')
            if annotate and v is not None and cw > 30 and ch > 14:
                light = t is not None and (t < 0.45 if palette == 'viridis' else t > 0.6)
                f.c.text(f.left + (k + 0.5) * cw, f.top + (r + 0.5) * ch + 4, fmt_number(v), size=9, anchor='middle',
                         fill='#ffffff' if light else INK)
        f.c.text(f.left - 6, f.top + (r + 0.5) * ch + 4, str(ylab)[:24], size=10, anchor='end', fill=AXIS)
    f.x_categories([str(l) for l in x_labels], [f.left + (k + 0.5) * cw for k in range(len(x_labels))])
    # colour scale
    lx, top, bar_h = f.right + 20, f.top + 16, f.bottom - f.top - 32
    f.c.text(lx, f.top + 4, value_label[:22], size=11)
    for i in range(50):
        f.c.rect(lx, top + bar_h * (1 - (i + 1) / 50), 14, bar_h / 50 + 0.5, fill=color_at(palette, (i + 0.5) / 50))
    f.c.text(lx + 20, top + 8, fmt_number(hi), size=10, fill=AXIS)
    f.c.text(lx + 20, top + bar_h, fmt_number(lo), size=10, fill=AXIS)
    return f.c


def means_chart(groups, means, lower, upper, title=None, x_label=None, y_label=None, width=720, height=420):
    f = Frame(width, height, title, x_label, y_label)
    ylo, yhi = _extent(list(means) + [v for v in lower if v is not None] + [v for v in upper if v is not None])
    y = Linear(ylo, yhi, f.bottom, f.top, pad=0.1)
    f.y_axis(y)
    band = (f.right - f.left) / max(len(groups), 1)
    centres = [f.left + (i + 0.5) * band for i in range(len(groups))]
    prev = None
    for cx, m, lo, hi, g in zip(centres, means, lower, upper, groups):
        if lo is not None and hi is not None:
            f.c.line(cx, y(lo), cx, y(hi), stroke=AXIS, width=1.5)
            f.c.line(cx - 6, y(lo), cx + 6, y(lo), stroke=AXIS, width=1.5)
            f.c.line(cx - 6, y(hi), cx + 6, y(hi), stroke=AXIS, width=1.5)
        if prev:
            f.c.line(prev[0], prev[1], cx, y(m), stroke=series_color(0), width=1, dash='3 3')
        f.c.circle(cx, y(m), 5, fill=series_color(0), title=f'{g}: mean {fmt_number(m)}')
        prev = (cx, y(m))
    f.x_categories([str(g) for g in groups], centres)
    return f.c


def violin_chart(groups, grids, densities, quartiles, title=None, x_label=None, y_label=None, width=720, height=440):
    """grids[i]/densities[i]: KDE evaluation points and density; quartiles[i]: (min, q1, median, q3, max)."""
    f = Frame(width, height, title, x_label, y_label)
    ylo = min(min(g) for g in grids) if grids else 0
    yhi = max(max(g) for g in grids) if grids else 1
    y = Linear(ylo, yhi, f.bottom, f.top, pad=0.03)
    f.y_axis(y)
    band = (f.right - f.left) / max(len(groups), 1)
    peak = max((max(d) for d in densities if len(d)), default=1) or 1
    centres = []
    for i, (g, grid, dens, q) in enumerate(zip(groups, grids, densities, quartiles)):
        cx = f.left + (i + 0.5) * band
        centres.append(cx)
        half = band * 0.42
        right = [(cx + half * d / peak, y(v)) for v, d in zip(grid, dens)]
        left = [(cx - half * d / peak, y(v)) for v, d in reversed(list(zip(grid, dens)))]
        f.c.polygon(right + left, fill=series_color(i), opacity=0.45, stroke=series_color(i))
        mn, q1, med, q3, mx = q
        f.c.line(cx, y(mn), cx, y(mx), stroke=INK, width=1)
        f.c.rect(cx - 5, y(q3), 10, y(q1) - y(q3), fill='#ffffff', stroke=INK)
        f.c.line(cx - 7, y(med), cx + 7, y(med), stroke=INK, width=2)
    f.x_categories([str(g) for g in groups], centres)
    return f.c


CHART_FIELDS = ('Chart', 'Title')


def chart_table(canvas: Canvas, title: str | None):
    """One-row table holding the SVG; the desktop renders a cell that starts with <svg as an image."""
    from .table import Table
    from .types import Field, FieldType, Schema
    schema = Schema([Field('Chart', FieldType.V_WSTRING, 2**30 - 1), Field('Title', FieldType.V_WSTRING, 2**30 - 1)])
    return Table.from_rows(schema, [(canvas.to_svg(), title)])


def chart_schema():
    from .types import Field, FieldType, Schema
    return Schema([Field('Chart', FieldType.V_WSTRING, 2**30 - 1), Field('Title', FieldType.V_WSTRING, 2**30 - 1)])


__all__ = ['Canvas', 'draw_reportlab', 'bar_chart', 'line_chart', 'scatter_chart', 'histogram_chart', 'heatmap_chart',
           'means_chart', 'violin_chart', 'chart_table', 'chart_schema', 'quoteattr', 'to_timestamp']
