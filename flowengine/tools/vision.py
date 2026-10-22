"""Computer Vision tools: image input and output, profiling, processing, barcodes, templates and PDF text.

Images travel as blob fields (PNG, JPEG, BMP or TIFF bytes). Pillow, NumPy and SciPy do the image work,
pypdfium2 (PDFium) reads and renders PDFs, and zxing-cpp reads and writes barcodes. All of these are
bundled. OCR (Image to Text, scanned PDFs, Key-Value Pair Extraction) and Image Recognition use components
that download on first use, and are separate tools.

Where the vendor uses a deep-learning step, a classical method is used and named in the tool description:
text enhancement is 4× Lanczos upscaling with unsharp masking, not a learned super-resolution model.
"""
from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from ..errors import ConfigError, DataError, EngineError, SchemaError
from ..modeling import missing
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
IMAGE_TYPES = {'bmp': ('.bmp',), 'jpeg': ('.jpg', '.jpeg'), 'png': ('.png',), 'tiff': ('.tif', '.tiff'), 'pdf': ('.pdf',)}
_SAVE = {'png': 'PNG', 'jpeg': 'JPEG', 'bmp': 'BMP', 'tiff': 'TIFF'}
MAX_PIXELS = 80_000_000


def blob_field(name):
    return Field(name, FieldType.BLOB)


def open_image(value, where=''):
    from PIL import Image, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    if missing(value):
        return None
    if not isinstance(value, (bytes, bytearray)):
        raise DataError(f'Image{where} is not a blob; read images with Image Input or Blob Input')
    try:
        img = Image.open(io.BytesIO(bytes(value)))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise DataError(f'Image{where} could not be read: {e}') from None
    return img


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def _generate_barcodes(texts, kinds, scale, check_cancelled=None):
    """Run native ZXing bitmap generation in a short-lived child process.

    The Windows zxing-cpp extension can access-violate after unrelated image and
    ML native libraries have run in the long-lived workflow engine process.
    Isolating only its encoder protects the desktop process and returns real PNGs.
    """
    root = Path(__file__).resolve().parents[2]
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(root), 'PYTHONNOUSERSITE': '1'}
    for name in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'):
        if os.environ.get(name):
            env[name] = os.environ[name]
    encoded = []
    for start in range(0, len(texts), 32):
        if check_cancelled:
            check_cancelled()
        payload = {'texts': texts[start:start + 32], 'types': kinds, 'scale': scale}
        try:
            proc = subprocess.run([sys.executable, '-m', 'flowengine.barcode_worker'], cwd=root, env=env,
                                  input=json.dumps(payload, ensure_ascii=False), text=True,
                                  capture_output=True, timeout=90, check=False)
        except subprocess.TimeoutExpired:
            raise DataError('Barcode generation exceeded its 90 second execution limit') from None
        if proc.returncode != 0:
            try:
                detail = json.loads(proc.stdout).get('error', 'native worker failed')
            except (json.JSONDecodeError, AttributeError):
                detail = f'native worker exited with code {proc.returncode}'
            raise DataError(f'Barcode generation failed safely ({detail})')
        try:
            response = json.loads(proc.stdout)
            if response.get('ok') is not True or not isinstance(response.get('codes'), list):
                raise ValueError('invalid worker response')
            encoded.extend(response['codes'])
        except (json.JSONDecodeError, ValueError, TypeError):
            raise DataError('Barcode generation worker returned an invalid result') from None
    import base64
    try:
        return [[None if value is None else base64.b64decode(value, validate=True) for value in row]
                for row in encoded]
    except (ValueError, TypeError):
        raise DataError('Barcode generation worker returned invalid image data') from None


def _path(ctx, value):
    p = Path(str(value))
    return p if p.is_absolute() else Path(ctx.base_dir) / p


def render_pdf(path_or_bytes, dpi, first=None, last=None):
    """[(page_number, PIL image)] for the page range (1-based, inclusive)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path_or_bytes)
    try:
        pages = range((first or 1) - 1, min(last or len(pdf), len(pdf)))
        return [(i + 1, pdf[i].render(scale=dpi / 72).to_pil().convert('RGB')) for i in pages]
    finally:
        pdf.close()


# =========================================================================== Image Input

class ImageInput(Tool):
    SCHEMA = Schema([Field('File', FieldType.V_WSTRING, TEXT), Field('Page', FieldType.INT32), blob_field('Image'),
                     Field('Format', FieldType.V_WSTRING, 16)])
    manifest = ToolManifest(
        tool_id='core.image_input', version='1.0.0', display_name='Image Input', catalog_ids=('computer-vision:image-input',),
        inputs=(Anchor('input', 'Input', required=False, description='Optional records with folder or file paths'),),
        outputs=(Anchor('output', 'Output', description='File, Page, Image (blob) and Format; Image Profile adds sizes'),),
        description='Read BMP, JPEG, PNG and TIFF images and PDF pages from a folder, a file, or the paths in a field. '
                    'You can limit the file types. Each image is one record with its original bytes. Each PDF page '
                    'is one record, rendered to PNG at the chosen resolution (PDFium), with its page number. Files '
                    'are read in name order; subfolders are included when recursive is on.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'path': {'type': ['string', 'null'], 'default': None}, 'path_field': {'type': ['string', 'null'], 'default': None},
            'file_types': {'type': 'array', 'uniqueItems': True, 'items': {'enum': list(IMAGE_TYPES)}, 'default': list(IMAGE_TYPES)},
            'recursive': {'type': 'boolean', 'default': False},
            'pdf_dpi': {'type': 'integer', 'minimum': 36, 'maximum': 600, 'default': 200}}})

    def infer(self, ctx, cfg, inputs):
        if inputs.get('input') is not None:
            if not cfg['path_field']:
                raise ConfigError('Choose the field that holds the folder or file paths')
            require_fields(inputs['input'], [cfg['path_field']])
        elif not cfg['path']:
            raise ConfigError('Enter a folder or file path, or connect records with paths')
        return {'output': self.SCHEMA}

    def _files(self, root: Path, cfg):
        exts = {e for t in cfg['file_types'] for e in IMAGE_TYPES[t]}
        if root.is_file():
            return [root] if root.suffix.lower() in exts else []
        if not root.is_dir():
            raise DataError(f'{str(root)!r} is not a file or folder')
        pattern = '**/*' if cfg['recursive'] else '*'
        return sorted(p for p in root.glob(pattern) if p.is_file() and p.suffix.lower() in exts)

    def run(self, ctx, cfg, inputs):
        self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items() if v is not None})
        roots = ([_path(ctx, v) for v in inputs['input'].column(cfg['path_field']) if not missing(v)]
                 if inputs.get('input') is not None else [_path(ctx, cfg['path'])])
        rows = []
        for root in roots:
            for f in self._files(root, cfg):
                ctx.check_cancelled()
                if f.suffix.lower() == '.pdf':
                    for page, img in render_pdf(str(f), cfg['pdf_dpi']):
                        rows.append((str(f), page, png_bytes(img), 'PNG'))
                    continue
                data = f.read_bytes()
                img = open_image(data, f' {f.name}')
                rows.append((str(f), None, data, img.format))
        if not rows:
            ctx.warn('No images were found')
        return {'output': Table.from_rows(self.SCHEMA, rows)}


# =========================================================================== Image Output

class ImageOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.image_output', version='1.0.0', display_name='Image Output', catalog_ids=('computer-vision:image-output',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output', description='The records with the written Path'),),
        side_effects='writes-files',
        description='Save images to a folder in PNG, JPEG, BMP or TIFF. Each file is named from the name field (its '
                    'extension is replaced) or numbered image_1, image_2 and so on. Names are made safe for the file '
                    'system, and existing files are overwritten or cause an error. Needs permission to write '
                    'files.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['image_field', 'folder'], 'properties': {
            'image_field': {'type': 'string', 'minLength': 1}, 'name_field': {'type': ['string', 'null'], 'default': None},
            'folder': {'type': 'string', 'minLength': 1}, 'format': {'enum': list(_SAVE), 'default': 'png'},
            'jpeg_quality': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 90},
            'if_exists': {'enum': ['overwrite', 'error'], 'default': 'overwrite'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['image_field']] + ([cfg['name_field']] if cfg['name_field'] else []))
        if 'Path' in inputs['input']:
            raise SchemaError('Image Output would overwrite the Path field')
        return {'output': Schema([*inputs['input'], Field('Path', FieldType.V_WSTRING, TEXT)])}

    def run(self, ctx, cfg, inputs):
        import re
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        if not ctx.limits.get('allow_file_writes'):
            raise EngineError('Image Output writes files and is disabled for this run. Allow file writes (CLI '
                              '--allow-file-writes, or confirm in the desktop app).', code='permission-denied', node_id=ctx.node_id)
        folder = _path(ctx, cfg['folder'])
        folder.mkdir(parents=True, exist_ok=True)
        ext = {'jpeg': '.jpg', 'tiff': '.tif'}.get(cfg['format'], f'.{cfg["format"]}')
        rows, used = [], set()
        for i, (row, value) in enumerate(zip(table.rows(), table.column(cfg['image_field'])), 1):
            ctx.check_cancelled()
            img = open_image(value, f' in record {i}')
            if img is None:
                rows.append((*row, None))
                continue
            raw = format_value(table.column(cfg['name_field'])[i - 1]) if cfg['name_field'] and not missing(table.column(cfg['name_field'])[i - 1]) else f'image_{i}'
            stem = re.sub(r'[^\w.-]+', '_', Path(raw).stem).strip('._') or f'image_{i}'
            name = stem
            n = 2
            while name.lower() in used:
                name, n = f'{stem}_{n}', n + 1
            used.add(name.lower())
            target = folder / f'{name}{ext}'
            if target.exists() and cfg['if_exists'] == 'error':
                raise DataError(f'{target} already exists')
            if cfg['format'] == 'jpeg' and img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            img.save(target, _SAVE[cfg['format']], **({'quality': cfg['jpeg_quality']} if cfg['format'] == 'jpeg' else {}))
            rows.append((*row, str(target)))
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== Image Profile

def _gps(exif):
    from PIL.ExifTags import GPSTAGS, IFD
    try:
        gps = exif.get_ifd(IFD.GPSInfo)
    except Exception:
        return {}
    if not gps:
        return {}
    g = {GPSTAGS.get(k, k): v for k, v in gps.items()}

    def deg(values, ref):
        if not values:
            return None
        d, m, s = (float(x) for x in values)
        v = d + m / 60 + s / 3600
        return -v if ref in ('S', 'W') else v
    stamp = g.get('GPSTimeStamp')
    return {'Latitude': deg(g.get('GPSLatitude'), g.get('GPSLatitudeRef')),
            'Longitude': deg(g.get('GPSLongitude'), g.get('GPSLongitudeRef')),
            'Altitude': float(g['GPSAltitude']) * (-1 if g.get('GPSAltitudeRef') in (1, b'\x01') else 1) if 'GPSAltitude' in g else None,
            'Speed': float(g['GPSSpeed']) if 'GPSSpeed' in g else None, 'SpeedRef': g.get('GPSSpeedRef'),
            'Track': float(g['GPSTrack']) if 'GPSTrack' in g else None, 'TrackRef': g.get('GPSTrackRef'),
            'TimeStamp': ':'.join(f'{float(x):02.0f}' for x in stamp) if stamp else None}


class ImageProfile(Tool):
    BASE = [('Mode', FieldType.V_WSTRING), ('Width', FieldType.INT32), ('Height', FieldType.INT32), ('Channels', FieldType.INT32),
            ('Min_Value', FieldType.INT32), ('Max_Value', FieldType.INT32), ('Most_Freq_Color_RGB', FieldType.V_WSTRING),
            ('Most_Freq_Color_Hex', FieldType.V_WSTRING), ('Dark_Pixel_count', FieldType.INT64), ('Bright_Pixel_count', FieldType.INT64)]
    EXIF = [('Exif', FieldType.V_WSTRING), ('Latitude', FieldType.DOUBLE), ('Longitude', FieldType.DOUBLE), ('Altitude', FieldType.DOUBLE),
            ('Speed', FieldType.DOUBLE), ('SpeedRef', FieldType.V_WSTRING), ('Track', FieldType.DOUBLE), ('TrackRef', FieldType.V_WSTRING),
            ('TimeStamp', FieldType.V_WSTRING)]
    SUMMARY = [('Pixel_Count', FieldType.INT64), ('Pixel_Sum', FieldType.V_WSTRING), ('Pixel_Squared_Sum', FieldType.V_WSTRING),
               ('Pixel_Mean', FieldType.V_WSTRING), ('Pixel_Median', FieldType.V_WSTRING), ('Root_Mean_Square', FieldType.V_WSTRING),
               ('Pixel_Variances', FieldType.V_WSTRING), ('Pixel_Std_Deviation', FieldType.V_WSTRING)]
    manifest = ToolManifest(
        tool_id='core.image_profile', version='1.0.0', display_name='Image Profile', catalog_ids=('computer-vision:image-profile',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Add profile fields to each image. **Base:** mode, size, channels, minimum and maximum channel '
                    'values, the most frequent colour (RGB and hex), and dark (0–30) and bright (220–255) pixel '
                    'counts on the luminance. **Exif:** the tags as JSON, plus GPS latitude, longitude, altitude, '
                    'speed, track and time when present. **Summary:** pixel count and per-channel sum, squared '
                    'sum, mean, median, RMS, variance and standard deviation, each a JSON list with one value per '
                    'channel.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['image_field'], 'properties': {
            'image_field': {'type': 'string', 'minLength': 1},
            'profiles': {'type': 'array', 'uniqueItems': True, 'minItems': 1, 'items': {'enum': ['base', 'exif', 'summary']},
                         'default': ['base', 'exif', 'summary']}}})

    def _added(self, cfg):
        out = []
        for name, cols in (('base', self.BASE), ('exif', self.EXIF), ('summary', self.SUMMARY)):
            if name in cfg['profiles']:
                out += [Field(n, t, TEXT if t is FieldType.V_WSTRING else None) for n, t in cols]
        return out

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['image_field']])
        added = self._added(cfg)
        clash = [f.name for f in added if f.name in inputs['input']]
        if clash:
            raise SchemaError(f'Image Profile would overwrite field(s) {clash}')
        return {'output': Schema([*inputs['input'], *added])}

    def _profile(self, img, cfg):
        import numpy as np
        out = []
        arr = np.asarray(img)
        chans = arr.reshape(arr.shape[0], arr.shape[1], -1).astype(np.float64)
        if 'base' in cfg['profiles']:
            rgb = np.asarray(img.convert('RGB')).reshape(-1, 3)
            colours, counts = np.unique(rgb, axis=0, return_counts=True)
            top = colours[counts.argmax()]
            lum = np.asarray(img.convert('L'))
            out += [img.mode, img.width, img.height, chans.shape[2], int(chans.min()), int(chans.max()),
                    json.dumps([int(v) for v in top]), '#%02x%02x%02x' % tuple(int(v) for v in top),
                    int((lum <= 30).sum()), int((lum >= 220).sum())]
        if 'exif' in cfg['profiles']:
            from PIL.ExifTags import TAGS
            exif = img.getexif()
            tags = {}
            for k, v in exif.items():
                if isinstance(v, bytes):
                    v = v.decode('utf-8', 'replace').strip('\x00')
                tags[TAGS.get(k, str(k))] = v if isinstance(v, (int, float, str)) else str(v)
            gps = _gps(exif)
            out += [json.dumps(tags, ensure_ascii=False, default=str) if tags else None,
                    *[gps.get(k) for k in ('Latitude', 'Longitude', 'Altitude', 'Speed', 'SpeedRef', 'Track', 'TrackRef', 'TimeStamp')]]
        if 'summary' in cfg['profiles']:
            flat = chans.reshape(-1, chans.shape[2])
            r = lambda a: json.dumps([round(float(x), 6) for x in a])  # noqa: E731
            out += [int(flat.shape[0]), r(flat.sum(axis=0)), r((flat ** 2).sum(axis=0)), r(flat.mean(axis=0)),
                    r(np.median(flat, axis=0)), r(np.sqrt((flat ** 2).mean(axis=0))), r(flat.var(axis=0)), r(flat.std(axis=0))]
        return out

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        width = len(schema) - len(table.schema)
        rows = []
        for i, (row, value) in enumerate(zip(table.rows(), table.column(cfg['image_field'])), 1):
            ctx.check_cancelled()
            img = open_image(value, f' in record {i}')
            rows.append((*row, *(self._profile(img, cfg) if img is not None else [None] * width)))
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== Image Processing

def deskew_angle(gray, limit=45.0):
    """Rotation (degrees, counter-clockwise) that makes text lines horizontal, by projection-profile variance."""
    import numpy as np
    from PIL import Image
    small = gray.copy()
    small.thumbnail((800, 800))
    arr = np.asarray(small, dtype=np.float64)
    ink = arr < (arr.mean() - 0.5 * arr.std() if arr.std() > 0 else 128)
    if ink.sum() < 50:
        return 0.0
    mask = Image.fromarray((ink * 255).astype(np.uint8))

    def score(a):
        rows = np.asarray(mask.rotate(a, resample=Image.BILINEAR, expand=True), dtype=np.float64).sum(axis=1)
        return float(np.var(np.diff(rows)))
    coarse = max(np.arange(-limit, limit + 0.01, 1.0), key=score)
    fine = max(np.arange(coarse - 1, coarse + 1.001, 0.1), key=score)
    return round(float(fine), 1)


def adaptive_threshold(gray, block=31, c=10):
    """Gaussian adaptive threshold: a pixel is white when it is brighter than its neighbourhood mean − c."""
    import numpy as np
    from PIL import Image
    from scipy.ndimage import gaussian_filter
    arr = np.asarray(gray, dtype=np.float64)
    local = gaussian_filter(arr, sigma=block / 6.0)
    return Image.fromarray(np.where(arr > local - c, 255, 0).astype(np.uint8))


def ocr_optimize(img, mode='auto', non_white=False):
    """Flatten the background, denoise and binarise a document image."""
    import numpy as np
    from PIL import Image, ImageFilter
    from scipy.ndimage import grey_closing, uniform_filter
    gray = np.asarray(img.convert('L'), dtype=np.float64)
    if non_white:  # text on coloured or dark panels: equalise local contrast first
        mean = uniform_filter(gray, 41)
        std = np.sqrt(np.maximum(uniform_filter(gray ** 2, 41) - mean ** 2, 1.0))
        gray = np.clip(128 + 64 * (gray - mean) / std, 0, 255)
    background = grey_closing(gray, size=(25, 25))
    flat = np.clip(gray / np.maximum(background, 1) * 255, 0, 255)
    if mode == 'auto':  # noisy pages (much fine-grained variation) get the heavy treatment
        noise = float(np.abs(flat - uniform_filter(flat, 3)).mean())
        mode = 'heavy' if noise > 6 else 'light'
    out = Image.fromarray(flat.astype(np.uint8))
    if mode == 'heavy':
        out = out.filter(ImageFilter.MedianFilter(3))
        return adaptive_threshold(out, block=41, c=12)
    return adaptive_threshold(out, block=31, c=8)


def _crop_box(w, h, cw, ch, anchor):
    v, hz = anchor.split('_') if '_' in anchor else ('middle', 'center')
    x = {'left': 0, 'center': (w - cw) // 2, 'right': w - cw}[hz]
    y = {'top': 0, 'middle': (h - ch) // 2, 'bottom': h - ch}[v]
    return x, y, x + cw, y + ch


def _size(value, unit, total):
    return max(1, round(total * value / 100)) if unit == 'percent' else int(value)


def locate(img, anchor_img):
    """Top-left (x, y) where anchor_img best matches img, by normalised cross-correlation (FFT)."""
    import numpy as np
    from scipy.signal import fftconvolve
    a = np.asarray(img.convert('L'), dtype=np.float64)
    t = np.asarray(anchor_img.convert('L'), dtype=np.float64)
    if t.shape[0] > a.shape[0] or t.shape[1] > a.shape[1]:
        raise DataError('The Shift anchor is larger than the image')
    t = t - t.mean()
    corr = fftconvolve(a, t[::-1, ::-1], mode='valid')
    ones = np.ones_like(t)
    local_sum = fftconvolve(a, ones, mode='valid')
    local_sq = fftconvolve(a ** 2, ones, mode='valid')
    local_var = np.maximum(local_sq - local_sum ** 2 / t.size, 1e-9)
    score = corr / np.sqrt(local_var * max((t ** 2).sum(), 1e-9))
    y, x = np.unravel_index(int(np.argmax(score)), score.shape)
    return int(x), int(y)


STEP_SCHEMA = {'type': 'object', 'required': ['op'], 'properties': {
    'op': {'enum': ['alignment', 'brightness', 'crop', 'enhance', 'grayscale', 'ocr_optimization', 'scale', 'shift', 'threshold']},
    'mode': {'type': 'string'}, 'degrees': {'type': 'number', 'minimum': -180, 'maximum': 180},
    'value': {'type': 'number'}, 'width': {'type': 'number', 'exclusiveMinimum': 0}, 'height': {'type': 'number', 'exclusiveMinimum': 0},
    'unit': {'enum': ['px', 'percent']}, 'anchor': {'type': 'string'}, 'keep_aspect': {'type': 'boolean'},
    'non_white_background': {'type': 'boolean'}, 'method': {'enum': ['adaptive_gaussian', 'binary']},
    'block_size': {'type': 'integer', 'minimum': 3}, 'c': {'type': 'number'}, 'label': {'type': 'string'}},
    'additionalProperties': False}
_ANCHORS = ['top_left', 'top_center', 'top_right', 'middle_left', 'middle_center', 'middle_right',
            'bottom_left', 'bottom_center', 'bottom_right']


def apply_steps(img, steps, anchors=None):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    import numpy as np
    for n, s in enumerate(steps, 1):
        op = s['op']
        if op == 'alignment':
            angle = deskew_angle(img.convert('L')) if s.get('mode', 'auto') == 'auto' else float(s.get('degrees', 0))
            if angle:
                fill = 255 if img.mode in ('L', '1') else (255,) * len(img.getbands())
                img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=fill)
        elif op == 'brightness':
            if s.get('mode', 'auto') == 'auto':
                img = ImageOps.autocontrast(img.convert('RGB') if img.mode not in ('L', 'RGB') else img, cutoff=1)
            else:
                img = ImageEnhance.Brightness(img).enhance(max(0.0, 1 + float(s.get('value', 0)) / 100))
        elif op == 'crop':
            cw, ch = _size(s['width'], s.get('unit', 'px'), img.width), _size(s['height'], s.get('unit', 'px'), img.height)
            anchor = s.get('anchor', 'middle_center')
            if anchor not in _ANCHORS:
                raise ConfigError(f'Step {n}: crop anchor must be one of {_ANCHORS}')
            img = img.crop(_crop_box(img.width, img.height, min(cw, img.width), min(ch, img.height), anchor))
        elif op == 'enhance':
            if img.width <= 1000 and img.height <= 1000:
                img = img.resize((img.width * 4, img.height * 4), Image.LANCZOS).filter(ImageFilter.UnsharpMask(2, 120, 2))
        elif op == 'grayscale':
            img = img.convert('L')
        elif op == 'ocr_optimization':
            img = ocr_optimize(img, s.get('mode', 'auto'), s.get('non_white_background', False))
        elif op == 'scale':
            unit = s.get('unit', 'px')
            w = _size(s['width'], unit, img.width) if s.get('width') else None
            h = _size(s['height'], unit, img.height) if s.get('height') else None
            if s.get('keep_aspect', True) or not (w and h):
                ratio = min((w / img.width) if w else math.inf, (h / img.height) if h else math.inf)
                w, h = max(1, round(img.width * ratio)), max(1, round(img.height * ratio))
            img = img.resize((w, h), Image.LANCZOS)
        elif op == 'shift':
            anchor = (anchors or {}).get(s.get('label'))
            if anchor is None:
                raise ConfigError(f'Step {n}: Shift needs annotation {s.get("label")!r} from a connected Image Template')
            x, y = locate(img, anchor['image'])
            dx, dy = anchor['x'] - x, anchor['y'] - y
            fill = 255 if img.mode in ('L', '1') else (255,) * len(img.getbands())
            img = img.transform(img.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy), fillcolor=fill)
        elif op == 'threshold':
            if s.get('method', 'adaptive_gaussian') == 'adaptive_gaussian':
                img = adaptive_threshold(img.convert('L'), int(s.get('block_size', 31)), float(s.get('c', 10)))
            else:
                cut = float(s.get('value', 127))
                bands = [Image.fromarray(np.where(np.asarray(b) > cut, 255, 0).astype(np.uint8)) for b in img.split()]
                img = bands[0] if len(bands) == 1 else Image.merge(img.mode if img.mode in ('RGB', 'RGBA') else 'RGB', bands[:3])
    return img


class ImageProcessing(Tool):
    manifest = ToolManifest(
        tool_id='core.image_processing', version='1.0.0', display_name='Image Processing',
        catalog_ids=('computer-vision:image-processing',),
        inputs=(Anchor('input', 'Input'), Anchor('template', 'Template', required=False,
                                                  description='Image Template annotations, used as Shift anchors')),
        outputs=(Anchor('output', 'Output', description='The records with image_processed (PNG)'),),
        description='Apply steps in order. **Alignment** rotates automatically (deskew by projection profile, up to '
                    '±45°) or by custom degrees. **Brightness Balance** auto-stretches the contrast or applies '
                    'custom −100 to 100. **Crop** keeps a width × height (pixels or %) at one of nine anchors. '
                    '**Enhance** upscales 4× with Lanczos and unsharp masking (images up to 1000 × 1000; a classical '
                    'stand-in for the vendor\'s learned super-resolution). **Grayscale.** **OCR Optimization** '
                    'flattens the background and binarises: heavy for noisy pages, light for clean ones, auto '
                    'chooses, with optional local contrast for text on coloured backgrounds. **Scale** resizes '
                    '(pixels or %, aspect lock). **Shift** realigns to an Image Template anchor found by '
                    'normalised cross-correlation. **Thresholding** is adaptive Gaussian (for text) or binary per '
                    'channel.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['image_field', 'steps'], 'properties': {
            'image_field': {'type': 'string', 'minLength': 1}, 'steps': {'type': 'array', 'minItems': 1, 'items': STEP_SCHEMA}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['image_field']])
        if 'image_processed' in inputs['input']:
            raise SchemaError('Image Processing would overwrite image_processed')
        return {'output': Schema([*inputs['input'], blob_field('image_processed')])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        anchors = read_template(inputs['template']) if inputs.get('template') is not None else None
        rows = []
        for i, (row, value) in enumerate(zip(table.rows(), table.column(cfg['image_field'])), 1):
            ctx.check_cancelled()
            img = open_image(value, f' in record {i}')
            rows.append((*row, png_bytes(apply_steps(img, cfg['steps'], anchors)) if img is not None else None))
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== Image Template

TEMPLATE_SCHEMA = Schema([Field('Label', FieldType.V_WSTRING, 255), Field('Type', FieldType.V_WSTRING, 16),
                          Field('Page', FieldType.INT32), Field('X', FieldType.INT32), Field('Y', FieldType.INT32),
                          Field('Width', FieldType.INT32), Field('Height', FieldType.INT32),
                          Field('TemplateWidth', FieldType.INT32), Field('TemplateHeight', FieldType.INT32),
                          blob_field('AnchorImage')])


def read_template(table):
    """{label: {type, page, x, y, w, h, tw, th, image}} from Image Template output."""
    if tuple(table.schema.names) != tuple(TEMPLATE_SCHEMA.names):
        raise SchemaError('The template input must be the output of Image Template')
    out = {}
    for label, kind, page, x, y, w, h, tw, th, blob in table.rows():
        out[label] = {'type': kind, 'page': page, 'x': x, 'y': y, 'w': w, 'h': h, 'tw': tw, 'th': th,
                      'image': open_image(blob) if blob else None}
    return out


class ImageTemplate(Tool):
    manifest = ToolManifest(
        tool_id='core.image_template', version='1.0.0', display_name='Image Template', catalog_ids=('computer-vision:image-template',),
        inputs=(Anchor('input', 'Input', required=False, description='Optional record whose image is the template'),),
        outputs=(Anchor('output', 'Output', description='One record per annotation'),),
        description='Define labelled regions (annotations) on a template image or PDF page. Each has a type (string, '
                    'table or image) and a box in template pixels. They are passed downstream to Image Processing '
                    '(Shift anchors), Image to Text and PDF to Text, which scale them to each page. annotations '
                    'uses the same JSON as the export, so saved templates can be pasted in. Each record carries '
                    'the template crop for Shift. Annotations are entered as coordinates, since there is no '
                    'drawing tool yet, and automatic table detection is not implemented.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['annotations'], 'properties': {
            'template_path': {'type': ['string', 'null'], 'default': None},
            'image_field': {'type': ['string', 'null'], 'default': None},
            'pdf_dpi': {'type': 'integer', 'minimum': 36, 'maximum': 600, 'default': 200},
            'annotations': {'type': 'array', 'minItems': 1, 'items': {
                'type': 'object', 'additionalProperties': False, 'required': ['label', 'x', 'y', 'width', 'height'],
                'properties': {'label': {'type': 'string', 'minLength': 1}, 'type': {'enum': ['string', 'table', 'image'], 'default': 'string'},
                               'page': {'type': 'integer', 'minimum': 1, 'default': 1}, 'x': {'type': 'integer', 'minimum': 0},
                               'y': {'type': 'integer', 'minimum': 0}, 'width': {'type': 'integer', 'minimum': 1},
                               'height': {'type': 'integer', 'minimum': 1}}}}}})

    def infer(self, ctx, cfg, inputs):
        if inputs.get('input') is not None:
            if not cfg['image_field']:
                raise ConfigError('Choose the field holding the template image')
            require_fields(inputs['input'], [cfg['image_field']])
        elif not cfg['template_path']:
            raise ConfigError('Enter the template image or PDF, or connect a record with the image')
        labels = [a['label'] for a in cfg['annotations']]
        if len(set(labels)) != len(labels):
            raise ConfigError('Annotation labels must be unique')
        return {'output': TEMPLATE_SCHEMA}

    def run(self, ctx, cfg, inputs):
        self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items() if v is not None})
        pages = {}
        if inputs.get('input') is not None:
            first = next((v for v in inputs['input'].column(cfg['image_field']) if not missing(v)), None)
            if first is None:
                raise DataError('The template input has no image')
            pages[1] = open_image(first, ' (template)')
        else:
            path = _path(ctx, cfg['template_path'])
            if not path.is_file():
                raise DataError(f'Template {str(path)!r} was not found')
            if path.suffix.lower() == '.pdf':
                pages = dict(render_pdf(str(path), cfg['pdf_dpi']))
            else:
                pages[1] = open_image(path.read_bytes(), f' {path.name}')
        rows = []
        for a in cfg['annotations']:
            page = a.get('page', 1)
            img = pages.get(page) if page in pages else pages.get(1) if len(pages) == 1 else None
            if img is None:
                raise ConfigError(f'Annotation {a["label"]!r} is on page {page}, which the template does not have')
            box = (a['x'], a['y'], min(a['x'] + a['width'], img.width), min(a['y'] + a['height'], img.height))
            if box[0] >= img.width or box[1] >= img.height:
                raise ConfigError(f'Annotation {a["label"]!r} lies outside the {img.width} × {img.height} template')
            rows.append((a['label'], a.get('type', 'string'), page, a['x'], a['y'], a['width'], a['height'],
                         img.width, img.height, png_bytes(img.crop(box))))
        return {'output': Table.from_rows(TEMPLATE_SCHEMA, rows)}


# =========================================================================== Barcode

_READ_FORMATS = {'QR Code': 'QRCode', 'Code 128': 'Code128', 'Code 39': 'Code39', 'UPC-A': 'UPCA', 'EAN-8': 'EAN8',
                 'EAN-13': 'EAN13', 'ITF': 'ITF', 'Data Matrix': 'DataMatrix', 'PDF417': 'PDF417'}


def _isbn_type(fmt, text):
    if fmt == 'EAN-13' and text[:3] in ('978', '979'):
        return 'ISBN-13'
    if fmt == 'EAN-13' and text[:3] == '977':
        return 'ISSN'
    if fmt == 'EAN-13' and text[:2] in ('45', '49'):
        return 'JAN'
    if fmt == 'ITF':
        return 'ITF-14' if len(text) == 14 else 'I25'
    return fmt


class Barcode(Tool):
    manifest = ToolManifest(
        tool_id='core.barcode', version='1.0.0', display_name='Barcode', catalog_ids=('computer-vision:barcode-tool',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        description='Generate or read barcodes (zxing-cpp). **Generate** adds a PNG blob per chosen type: QR (UTF-8 '
                    'text) and Code 128 (ASCII, at most 253 characters). **Read** finds every barcode in an image. '
                    'It handles QR, Code 128, Code 39, UPC-A, EAN-8, EAN-13 (reported as ISBN-13, ISSN or JAN by '
                    'prefix), ITF (I25 or ITF-14), Data Matrix and PDF417. Each barcode is one record with decoded '
                    'and type, plus optionally the cropped barcode and the image with the barcode outlined. An '
                    'image with no barcode keeps one record with nulls.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'mode': {'enum': ['generate', 'read'], 'default': 'read'},
            'text_field': {'type': ['string', 'null'], 'default': None}, 'image_field': {'type': ['string', 'null'], 'default': None},
            'types': {'type': 'array', 'uniqueItems': True, 'minItems': 1, 'items': {'enum': ['qr', 'code128']}, 'default': ['qr']},
            'scale': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 4},
            'cropped': {'type': 'boolean', 'default': False}, 'bounded': {'type': 'boolean', 'default': False}}})

    def _added(self, cfg):
        if cfg['mode'] == 'generate':
            return [blob_field({'qr': 'QR', 'code128': 'Code128'}[t]) for t in cfg['types']]
        return [Field('decoded', FieldType.V_WSTRING, TEXT), Field('type', FieldType.V_WSTRING, 32),
                *([blob_field('cropped_barcode')] if cfg['cropped'] else []), *([blob_field('bounded_image')] if cfg['bounded'] else [])]

    def infer(self, ctx, cfg, inputs):
        field = cfg['text_field'] if cfg['mode'] == 'generate' else cfg['image_field']
        if not field:
            raise ConfigError('Choose the ' + ('text field' if cfg['mode'] == 'generate' else 'image field'))
        require_fields(inputs['input'], [field])
        if cfg['mode'] == 'generate' and not inputs['input'].field(field).type.is_string:
            raise SchemaError('Barcode text must be a string field')
        added = self._added(cfg)
        clash = [f.name for f in added if f.name in inputs['input']]
        if clash:
            raise SchemaError(f'Barcode would overwrite field(s) {clash}')
        return {'output': Schema([*inputs['input'], *added])}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        rows = []
        if cfg['mode'] == 'generate':
            text_values = list(table.column(cfg['text_field']))
            for i, value in enumerate(text_values, 1):
                if missing(value):
                    continue
                text = str(value)
                if 'code128' in cfg['types'] and (len(text) > 253 or not text.isascii()):
                    raise DataError(f'Record {i}: Code 128 takes ASCII text of at most 253 characters')
            generated = _generate_barcodes([None if missing(v) else str(v) for v in text_values],
                                           cfg['types'], cfg['scale'], ctx.check_cancelled)
            for row, blobs in zip(table.rows(), generated):
                rows.append((*row, *blobs))
            return {'output': Table.from_rows(schema, rows)}
        import zxingcpp
        from PIL import Image, ImageDraw
        formats = [getattr(zxingcpp.BarcodeFormat, name) for name in _READ_FORMATS.values()]
        for i, (row, value) in enumerate(zip(table.rows(), table.column(cfg['image_field'])), 1):
            ctx.check_cancelled()
            img = open_image(value, f' in record {i}')
            # Give the native decoder an owned, contiguous buffer. Passing a PIL
            # view can retain a borrowed buffer whose lifetime varies with other
            # imaging extensions loaded by the application.
            pixels = np.array(img.convert('RGB'), dtype=np.uint8, copy=True) if img is not None else None
            found = zxingcpp.read_barcodes(pixels, formats=formats) if pixels is not None else []
            if not found:
                rows.append((*row, *([None] * (len(schema) - len(row)))))
                continue
            for r in found:
                pts = [(p.x, p.y) for p in (r.position.top_left, r.position.top_right, r.position.bottom_right, r.position.bottom_left)]
                extra = []
                if cfg['cropped']:
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    extra.append(png_bytes(img.crop((max(min(xs) - 4, 0), max(min(ys) - 4, 0), max(xs) + 4, max(ys) + 4))))
                if cfg['bounded']:
                    boxed = img.convert('RGB').copy()
                    ImageDraw.Draw(boxed).polygon(pts, outline=(0, 90, 255), width=3)
                    extra.append(png_bytes(boxed))
                rows.append((*row, r.text, _isbn_type(str(r.format), r.text), *extra))
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== PDF to Text

def _page_lines(page):
    """[(top, [(x0, x1, text), ...])] text segments grouped into lines, top to bottom."""
    tp = page.get_textpage()
    try:
        segs = []
        for i in range(tp.count_rects()):
            l, b, r, t = tp.get_rect(i)
            text = tp.get_text_bounded(left=l, bottom=b, right=r, top=t).strip()
            if text:
                segs.append((t, b, l, r, text))
    finally:
        tp.close()
    segs.sort(key=lambda s: (-s[0], s[2]))
    lines = []
    for top, bottom, l, r, text in segs:
        mid = (top + bottom) / 2
        if lines and abs(lines[-1][0] - mid) <= max(2.0, (top - bottom) * 0.5):
            lines[-1][1].append((l, r, text))
        else:
            lines.append([mid, [(l, r, text)]])
    return [(m, sorted(parts)) for m, parts in lines]


def _columns(lines):
    """Column boundaries from horizontal overlap of segments across all lines."""
    spans = sorted((l, r) for _, parts in lines for l, r, _ in parts)
    cols = []
    for l, r in spans:
        if cols and l <= cols[-1][1]:
            cols[-1][1] = max(cols[-1][1], r)
        else:
            cols.append([l, r])
    return cols


def _risk(page, chars):
    import pypdfium2.raw as raw
    w, h = page.get_size()
    area = 0.0
    for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=4):
        l, b, r, t = obj.get_pos()
        area += max(0, r - l) * max(0, t - b)
    share = min(area / max(w * h, 1), 1.0)
    if share > 0.5 and chars < 100:
        return 'high'
    if share > 0.2 or chars < 20:
        return 'medium'
    return 'low'


class PDFToText(Tool):
    SCHEMA = Schema([Field('File', FieldType.V_WSTRING, TEXT), Field('Page', FieldType.INT32), Field('Format', FieldType.V_WSTRING, 16),
                     Field('Line', FieldType.INT32), Field('Region', FieldType.V_WSTRING, 255), Field('Text', FieldType.V_WSTRING, TEXT),
                     Field('RiskScore', FieldType.V_WSTRING, 8), blob_field('PageImage')])
    manifest = ToolManifest(
        tool_id='core.pdf_to_text', version='1.0.0', display_name='PDF to Text', catalog_ids=('computer-vision:pdf-to-text',),
        inputs=(Anchor('input', 'D', required=False, description='Optional PDF file or folder paths'),
                Anchor('template', 'T', required=False, description='Optional Image Template regions')),
        outputs=(Anchor('output', 'Output'),),
        description='Extract the text stored in PDFs with PDFium, for one file, a folder, or paths from a field, with an '
                    'optional page range. Output formats: **String** (one record per page), **Lines** (one per line, '
                    'top to bottom), **Pipe-delimited Table** (one per page, columns separated by |) and **Table** '
                    '(one per line, split into Column_1 … Column_n by horizontal overlap). Several formats give '
                    'several records. **Risk score** (low, medium or high) flags pages whose text may be stored as '
                    'images and need OCR, from image coverage and character count. **Page image** adds a rendered '
                    'PNG. With Image Template regions, string regions give their text, table regions their lines '
                    'and image regions a cropped image, scaled from the template to each page. **Read text and image '
                    'content** (text_and_ocr) OCRs scanned pages entirely, and adds the text of pictures on other '
                    'pages. Its models download on first use.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'path': {'type': ['string', 'null'], 'default': None}, 'path_field': {'type': ['string', 'null'], 'default': None},
            'mode': {'enum': ['text_only', 'text_and_ocr'], 'default': 'text_only'},
            'formats': {'type': 'array', 'uniqueItems': True, 'minItems': 1, 'items': {'enum': ['string', 'lines', 'pipe_table', 'table']},
                        'default': ['string']},
            'start_page': {'type': ['integer', 'null'], 'minimum': 1, 'default': None},
            'end_page': {'type': ['integer', 'null'], 'minimum': 1, 'default': None},
            'risk_score': {'type': 'boolean', 'default': False}, 'page_image': {'type': 'boolean', 'default': False},
            'first_page_template_for_all': {'type': 'boolean', 'default': True},
            'dpi': {'type': 'integer', 'minimum': 36, 'maximum': 600, 'default': 150}}})

    def infer(self, ctx, cfg, inputs):
        if inputs.get('input') is not None:
            if not cfg['path_field']:
                raise ConfigError('Choose the field with PDF paths')
            require_fields(inputs['input'], [cfg['path_field']])
        elif not cfg['path']:
            raise ConfigError('Enter a PDF file or folder, or connect records with paths')
        if cfg['start_page'] and cfg['end_page'] and cfg['start_page'] > cfg['end_page']:
            raise ConfigError('start_page is after end_page')
        if 'table' in cfg['formats']:
            return None  # Column_1..n depend on the documents
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        import pypdfium2 as pdfium
        self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items() if v is not None})
        roots = ([_path(ctx, v) for v in inputs['input'].column(cfg['path_field']) if not missing(v)]
                 if inputs.get('input') is not None else [_path(ctx, cfg['path'])])
        files = []
        for root in roots:
            if root.is_dir():
                files += sorted(p for p in root.iterdir() if p.suffix.lower() == '.pdf')
            elif root.is_file():
                files.append(root)
            else:
                raise DataError(f'{str(root)!r} is not a PDF file or folder')
        regions = read_template(inputs['template']) if inputs.get('template') is not None else None
        records, widest = [], 0
        for f in files:
            try:
                pdf = pdfium.PdfDocument(str(f))
            except pdfium.PdfiumError as e:
                raise DataError(f'{f.name} could not be opened as a PDF: {e}') from None
            try:
                first, last = (cfg['start_page'] or 1), min(cfg['end_page'] or len(pdf), len(pdf))
                for index in range(first - 1, last):
                    ctx.check_cancelled()
                    page = pdf[index]
                    tp = page.get_textpage()
                    full = tp.get_text_range().replace('\r\n', '\n')
                    chars = tp.count_chars()
                    tp.close()
                    risk = _risk(page, chars) if cfg['risk_score'] else None
                    image = png_bytes(page.render(scale=cfg['dpi'] / 72).to_pil()) if cfg['page_image'] else None
                    if regions:
                        records += self._regions(f, index + 1, page, regions, cfg, risk)
                        continue
                    lines = _page_lines(page)
                    if cfg['mode'] == 'text_and_ocr':
                        lines, full = self._with_ocr(ctx, page, lines, full, chars)
                    for fmt in cfg['formats']:
                        if fmt == 'string':
                            records.append((str(f), index + 1, 'String', None, None, full.strip(), risk, image, None))
                        elif fmt == 'lines':
                            for n, (_, parts) in enumerate(lines, 1):
                                records.append((str(f), index + 1, 'Lines', n, None, ' '.join(p[2] for p in parts), risk, image, None))
                        elif fmt == 'pipe_table':
                            cols = _columns(lines)
                            text = '\n'.join('|'.join(self._cells(parts, cols)) for _, parts in lines)
                            records.append((str(f), index + 1, 'Pipe Table', None, None, text, risk, image, None))
                        else:
                            cols = _columns(lines)
                            widest = max(widest, len(cols))
                            for n, (_, parts) in enumerate(lines, 1):
                                records.append((str(f), index + 1, 'Table', n, None, None, risk, image, self._cells(parts, cols)))
            finally:
                pdf.close()
        if 'table' in cfg['formats']:
            schema = Schema([*self.SCHEMA, *[Field(f'Column_{i + 1}', FieldType.V_WSTRING, TEXT) for i in range(widest)]])
            rows = [(*r[:8], *((r[8] or []) + [None] * (widest - len(r[8] or [])))) for r in records]
            return {'output': Table.from_rows(schema, rows)}
        return {'output': Table.from_rows(self.SCHEMA, [r[:8] for r in records])}

    @staticmethod
    def downloads(cfg):
        return ['ocr-models'] if cfg.get('mode') == 'text_and_ocr' else []

    def _with_ocr(self, ctx, page, lines, full, chars):
        """Scanned pages (little or no stored text) are read entirely by OCR; otherwise text in pictures is added."""
        import pypdfium2.raw as raw
        from .vision_ai import ocr, ocr_lines
        scale = 200 / 72
        rendered = page.render(scale=scale).to_pil()
        width, height = page.get_size()
        if chars < 20:
            words = ocr(ctx, [rendered], 'PDF to Text')[0]
            new = [(m, parts) for m, parts in ocr_lines(words)]
            return new, '\n'.join(' '.join(p[2] for p in parts) for _, parts in new)
        crops = []
        for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            l, b, r, t = obj.get_pos()
            if (r - l) * (t - b) > 0.01 * width * height:
                crops.append(rendered.crop((round(l * scale), round((height - t) * scale), round(r * scale), round((height - b) * scale))))
        if not crops:
            return lines, full
        extra = [(0, parts) for words in ocr(ctx, crops, 'PDF to Text') for _, parts in ocr_lines(words)]
        return lines + extra, full + '\n' + '\n'.join(' '.join(p[2] for p in parts) for _, parts in extra)

    @staticmethod
    def _cells(parts, cols):
        cells = [''] * len(cols)
        for l, r, text in parts:
            mid = (l + r) / 2
            k = next((i for i, (a, b) in enumerate(cols) if a <= mid <= b), len(cols) - 1)
            cells[k] = (cells[k] + ' ' + text).strip()
        return cells

    def _regions(self, f, number, page, regions, cfg, risk):
        out = []
        width, height = page.get_size()
        for label, reg in regions.items():
            if reg['page'] != number and not (cfg['first_page_template_for_all'] and reg['page'] == 1):
                continue
            sx, sy = width / reg['tw'], height / reg['th']
            left, right = reg['x'] * sx, (reg['x'] + reg['w']) * sx
            top, bottom = height - reg['y'] * sy, height - (reg['y'] + reg['h']) * sy
            if reg['type'] == 'image':
                scale = cfg['dpi'] / 72
                img = page.render(scale=scale).to_pil()
                crop = img.crop((round(left * scale), round((height - top) * scale), round(right * scale), round((height - bottom) * scale)))
                out.append((str(f), number, 'Image', None, label, None, risk, png_bytes(crop), None))
                continue
            tp = page.get_textpage()
            text = tp.get_text_bounded(left=left, bottom=bottom, right=right, top=top).replace('\r\n', '\n').strip()
            tp.close()
            if reg['type'] == 'table':
                for n, line in enumerate([l for l in text.split('\n') if l.strip()], 1):
                    out.append((str(f), number, 'Table', n, label, line.strip(), risk, None, None))
            else:
                out.append((str(f), number, 'String', None, label, text, risk, None, None))
        return out


TOOLS = [ImageInput, ImageOutput, ImageProfile, ImageProcessing, ImageTemplate, Barcode, PDFToText]
