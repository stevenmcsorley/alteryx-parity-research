"""Computer Vision tools that need downloaded models: Image to Text (OCR), Key-Value Pair Extraction and
Image Recognition. The shared OCR helper is also used by PDF to Text for scanned pages.

The models install on first use (see ``flowengine.addons``) and run in a worker process:

* **OCR** uses PaddleOCR models in ONNX Runtime: DB text detection (PP-OCRv3) and PP-OCRv5 Latin-script
  recognition. One model covers English, French, German, Italian, Portuguese and Spanish, including accents.
  The vendor's OCR engine is not named.
* **Image Recognition** uses transfer learning. A pretrained ImageNet backbone (MobileNetV2 or ResNet50-v2
  from the ONNX Model Zoo) scores each image on the 1000 ImageNet classes. Those scores are standardised
  and fed to a small neural network (one hidden layer, Adam), trained for the chosen epochs and batch size.
  The backbone is not fine-tuned.
"""
from __future__ import annotations

import io
import json
import re
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

from .. import addons, charts, modeling
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import MODEL_SCHEMA, missing, model_table
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields
from .vision import blob_field, open_image, png_bytes, read_template

TEXT = 2**30 - 1
OCR_LANGUAGES = ['english', 'french', 'german', 'italian', 'portuguese', 'spanish']
_LANG = {'enum': OCR_LANGUAGES, 'default': 'english'}
OCR = ['ocr-models']


# =========================================================================== OCR helper

def ocr(ctx, images, tool):
    """OCR PIL images; per image a list of {text, score, box} in reading order."""
    addons.require(OCR, tool)
    d = addons.model_dir('ocr-models')
    with tempfile.TemporaryDirectory(prefix='flowbench-ocr-') as tmp:
        paths = []
        for i, img in enumerate(images):
            p = Path(tmp) / f'{i}.png'
            img.convert('RGB').save(p, 'PNG')
            paths.append(str(p))
        request = {'det': str(d / 'det.onnx'), 'rec': str(d / 'rec.onnx'), 'dict': str(d / 'dict.txt'), 'images': paths,
                   'rec_height': 48}
        return addons.run_worker(ctx, 'ocr_worker.py', request)['results']


def ocr_lines(words):
    """Group OCR boxes into lines: [(top, [(x0, x1, text), ...])] top to bottom, left to right."""
    items = []
    for w in words:
        xs, ys = [p[0] for p in w['box']], [p[1] for p in w['box']]
        items.append((min(ys), max(ys), min(xs), max(xs), w['text']))
    items.sort(key=lambda t: ((t[0] + t[1]) / 2, t[2]))
    lines = []
    for top, bottom, x0, x1, text in items:
        mid = (top + bottom) / 2
        if lines and abs(lines[-1][0] - mid) <= max(4.0, (bottom - top) * 0.5):
            lines[-1][1].append((x0, x1, text))
        else:
            lines.append([mid, [(x0, x1, text)]])
    return [(m, sorted(parts)) for m, parts in lines]


def _scaled_box(region, img):
    sx, sy = img.width / region['tw'], img.height / region['th']
    return (round(region['x'] * sx), round(region['y'] * sy), round((region['x'] + region['w']) * sx),
            round((region['y'] + region['h']) * sy))


# =========================================================================== Image to Text

class ImageToText(Tool):
    manifest = ToolManifest(
        tool_id='core.image_to_text', version='1.0.0', display_name='Image to Text', catalog_ids=('computer-vision:image-to-text',),
        inputs=(Anchor('input', 'D', description='Images (for example from Image Input)'),
                Anchor('template', 'T', required=False, description='Optional Image Template regions')),
        outputs=(Anchor('output', 'Output'),),
        description='Read the text in images by OCR: PaddleOCR detection and Latin-script recognition in ONNX '
                    'Runtime, for English, French, German, Italian, Portuguese and Spanish. The models download on '
                    'first use (about 70 MB with the runtimes). Without a template, adds text (lines top to '
                    'bottom) and confidence. With Image Template regions, each region gives one record: string '
                    'regions their text, table regions one record per line, and image regions a cropped '
                    'RegionImage. Regions are scaled from the template to each image, and the first page\'s '
                    'regions can apply to every page. Null or empty images are not supported.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['image_field'], 'properties': {
            'image_field': {'type': 'string', 'minLength': 1}, 'language': _LANG,
            'first_page_template_for_all': {'type': 'boolean', 'default': True}}})

    @staticmethod
    def downloads(cfg):
        return OCR

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['image_field']])
        added = ['Region', 'Line', 'Text', 'RegionImage'] if inputs.get('template') is not None else ['text', 'confidence']
        clash = [n for n in added if n in inputs['input']]
        if clash:
            raise SchemaError(f'Image to Text would overwrite field(s) {clash}')
        if inputs.get('template') is not None:
            return {'output': Schema([*inputs['input'], Field('Region', FieldType.V_WSTRING, 255), Field('Line', FieldType.INT32),
                                      Field('Text', FieldType.V_WSTRING, TEXT), blob_field('RegionImage')])}
        return {'output': Schema([*inputs['input'], Field('text', FieldType.V_WSTRING, TEXT), Field('confidence', FieldType.DOUBLE)])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items() if v is not None})['output']
        images = []
        for i, v in enumerate(table.column(cfg['image_field']), 1):
            if missing(v) or not v:
                raise DataError(f'Record {i} has no image; Image to Text does not support null or empty rows')
            images.append(open_image(v, f' in record {i}'))
        if inputs.get('template') is None:
            rows = []
            for row, words in zip(table.rows(), ocr(ctx, images, 'Image to Text')):
                text = '\n'.join(' '.join(p[2] for p in parts) for _, parts in ocr_lines(words))
                conf = sum(w['score'] for w in words) / len(words) if words else None
                rows.append((*row, text, conf))
            return {'output': Table.from_rows(schema, rows)}
        regions = read_template(inputs['template'])
        pages = table.column('Page') if 'Page' in table.schema else [1] * len(table)
        jobs, crops = [], []
        for r, (row, img, page) in enumerate(zip(table.rows(), images, pages)):
            page = page or 1
            for label, reg in regions.items():
                if reg['page'] != page and not (cfg['first_page_template_for_all'] and reg['page'] == 1):
                    continue
                crop = img.crop(_scaled_box(reg, img))
                jobs.append((row, label, reg['type'], crop))
                if reg['type'] != 'image':
                    crops.append(crop)
        results = iter(ocr(ctx, crops, 'Image to Text') if crops else [])
        rows = []
        for row, label, kind, crop in jobs:
            if kind == 'image':
                rows.append((*row, label, None, None, png_bytes(crop)))
                continue
            lines = [' '.join(p[2] for p in parts) for _, parts in ocr_lines(next(results))]
            if kind == 'table':
                rows += [(*row, label, n, line, None) for n, line in enumerate(lines, 1)]
            else:
                rows.append((*row, label, None, '\n'.join(lines), None))
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== Key-Value Pair Extraction

def _norm(text):
    return re.sub(r'[\W_]+', '', text.lower())


def find_values(lines, key, threshold=0.8):
    """Values for a key in OCR lines: the text after the key on its line, else the closest line below."""
    target = _norm(key)
    n_key = max(1, len(key.split()))
    found = []
    flat = [(m, parts, ' '.join(p[2] for p in parts)) for m, parts in lines]
    for i, (mid, parts, text) in enumerate(flat):
        tokens = text.split()
        best = (0.0, None)
        for start in range(len(tokens)):
            for n in range(1, min(n_key + 1, len(tokens) - start) + 1):
                cand = ' '.join(tokens[start:start + n])
                score = SequenceMatcher(None, target, _norm(cand)).ratio() if _norm(cand) else 0.0
                if score > best[0]:
                    best = (score, (start, n))
        if best[0] < threshold:
            continue
        start, n = best[1]
        value = ' '.join(tokens[start + n:]).lstrip(':-–= ').strip()
        if not value and i + 1 < len(flat):
            value = flat[i + 1][2].strip()  # the key ends its line: the value is on the next line
        found.append(value or None)
    return found


class KeyValuePairExtraction(Tool):
    manifest = ToolManifest(
        tool_id='core.key_value_pair_extraction', version='1.0.0', display_name='Key-Value Pair Extraction',
        catalog_ids=('laboratory:key-value-pair-extraction',),
        inputs=(Anchor('input', 'D', description='Document images'), Anchor('keys', 'K', description='The keys to look for, one per record')),
        outputs=(Anchor('output', 'Output', description='The records with one field per key (key2, key3 … for repeats)'),),
        description='Find values next to keys (for example "Invoice Number: INV-42") in document images. The text '
                    'is read by OCR, and each key is matched to the words of each line by fuzzy matching (a '
                    'normalised similarity of at least 0.8, so small OCR errors still match). The value is the rest '
                    'of the line after the key, or the next line when the key ends its line. A key found '
                    'several times gives key, key2 and so on. Words in a value are space-separated. Not designed '
                    'for tables (use Image Template) or handwriting. Models download on first use.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['image_field', 'key_field'], 'properties': {
            'image_field': {'type': 'string', 'minLength': 1}, 'key_field': {'type': 'string', 'minLength': 1}, 'language': _LANG,
            'threshold': {'type': 'number', 'minimum': 0.5, 'maximum': 1, 'default': 0.8}}})

    @staticmethod
    def downloads(cfg):
        return OCR

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['image_field']])
        require_fields(inputs['keys'], [cfg['key_field']])
        return None  # one field per key

    def run(self, ctx, cfg, inputs):
        table, keys_table = inputs['input'], inputs['keys']
        require_fields(table.schema, [cfg['image_field']])
        require_fields(keys_table.schema, [cfg['key_field']])
        keys = []
        for v in keys_table.column(cfg['key_field']):
            if missing(v) or not str(v).strip():
                raise DataError("This key isn't supported: None (remove empty rows from the key list)")
            if str(v).strip() not in keys:
                keys.append(str(v).strip())
        images = [open_image(v, f' in record {i}') for i, v in enumerate(table.column(cfg['image_field']), 1)]
        present = [i for i, img in enumerate(images) if img is not None]
        words = dict(zip(present, ocr(ctx, [images[i] for i in present], 'Key-Value Pair Extraction'))) if present else {}
        per_record = []
        for i in range(len(table)):
            lines = ocr_lines(words.get(i, []))
            per_record.append({k: find_values(lines, k, cfg['threshold']) for k in keys})
        width = {k: max([1] + [len(r[k]) for r in per_record]) for k in keys}
        names = [k if n == 0 else f'{k}{n + 1}' for k in keys for n in range(width[k])]
        clash = [n for n in names if n in table.schema]
        if clash:
            raise SchemaError(f'Key-Value Pair Extraction would overwrite field(s) {clash}')
        schema = Schema([*table.schema, *[Field(n, FieldType.V_WSTRING, TEXT) for n in names]])
        rows = [(*row, *[(r[k] + [None] * width[k])[n] for k in keys for n in range(width[k])])
                for row, r in zip(table.rows(), per_record)]
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== Image Recognition

BACKBONES = {'mobilenet_v2': 'image-model:mobilenet_v2', 'resnet50_v2': 'image-model:resnet50_v2'}


def image_features(ctx, backbone, images, tool):
    import numpy as np
    component = BACKBONES[backbone]
    addons.require([component], tool)
    with tempfile.TemporaryDirectory(prefix='flowbench-img-') as tmp:
        paths = []
        for i, img in enumerate(images):
            p = Path(tmp) / f'{i}.png'
            img.convert('RGB').save(p, 'PNG')
            paths.append(str(p))
        out = addons.run_worker(ctx, 'classify_worker.py', {'model': str(addons.model_dir(component) / 'model.onnx'), 'images': paths})
    return np.array(out['results'], dtype=np.float64)


def image_predict(ctx, info, payload, table):
    import numpy as np
    if info['image_field'] not in table.schema:
        raise SchemaError(f"Data has no image field {info['image_field']!r}")
    images = [open_image(v, f' in record {i}') for i, v in enumerate(table.column(info['image_field']), 1)]
    mask = np.array([img is not None for img in images])
    proba = np.full((len(images), len(info['classes'])), np.nan)
    if mask.any():
        feats = image_features(ctx, info['backbone'], [img for img in images if img is not None], 'Predict')
        proba[mask] = modeling.sk_loads(payload).predict_proba(feats)
    return {'proba': proba, 'mask': mask, 'unseen': 0}


class ImageRecognition(Tool):
    EVAL = Schema([Field('Label', FieldType.V_WSTRING, TEXT), Field('Precision', FieldType.DOUBLE), Field('Recall', FieldType.DOUBLE),
                   Field('Accuracy', FieldType.DOUBLE), Field('Support', FieldType.INT64)])
    manifest = ToolManifest(
        tool_id='core.image_recognition', version='1.0.0', display_name='Image Recognition',
        catalog_ids=('computer-vision:image-recognition',),
        inputs=(Anchor('train', 'T', description='Training images and labels'), Anchor('validation', 'V', description='Validation images and labels')),
        outputs=(Anchor('model', 'M', description='Model for Predict'),
                 Anchor('evaluation', 'E', description='Precision, recall and accuracy per label on the validation images'),
                 Anchor('report', 'R', description='Accuracy and loss per epoch')),
        description='Classify images with a pretrained backbone (transfer learning). MobileNetV2 is fast and small '
                    '(14 MB). ResNet50-v2 is more accurate (102 MB). Both are ImageNet models that download on '
                    'first use. The backbone\'s 1000 ImageNet class scores, standardised, feed a one-hidden-layer '
                    'network trained with Adam for the chosen epochs, batch size and learning rate (the backbone is '
                    'not fine-tuned). E gives per-label precision, recall and one-vs-rest accuracy on the '
                    'validation images. R charts training and validation accuracy and loss per epoch. Images are '
                    'centre-cropped to 224 × 224. Connect M to Predict.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['image_field', 'label_field'], 'properties': {
            'image_field': {'type': 'string', 'minLength': 1}, 'label_field': {'type': 'string', 'minLength': 1},
            'validation_image_field': {'type': ['string', 'null'], 'default': None},
            'validation_label_field': {'type': ['string', 'null'], 'default': None},
            'model': {'enum': list(BACKBONES), 'default': 'mobilenet_v2'},
            'epochs': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 10},
            'batch_size': {'type': 'integer', 'minimum': 1, 'maximum': 4096, 'default': 32},
            'learning_rate': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1, 'default': 0.001},
            'hidden_units': {'type': 'integer', 'minimum': 1, 'maximum': 4096, 'default': 128},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1}, 'model_name': {'type': ['string', 'null'], 'default': None}}})

    @staticmethod
    def downloads(cfg):
        return [BACKBONES.get(cfg.get('model', 'mobilenet_v2'), 'image-model:mobilenet_v2')]

    def _vfields(self, cfg):
        return cfg['validation_image_field'] or cfg['image_field'], cfg['validation_label_field'] or cfg['label_field']

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['train'], [cfg['image_field'], cfg['label_field']])
        require_fields(inputs['validation'], list(self._vfields(cfg)))
        return {'model': MODEL_SCHEMA, 'evaluation': self.EVAL, 'report': charts.chart_schema()}

    def _pairs(self, table, image_field, label_field):
        imgs, labels = [], []
        for i, (v, label) in enumerate(zip(table.column(image_field), table.column(label_field)), 1):
            if missing(label) or missing(v):
                continue
            img = open_image(v, f' in record {i}')
            if img.width > 512 or img.height > 512:
                img = img.copy()
                img.thumbnail((512, 512))
            imgs.append(img)
            labels.append(format_value(label))
        return imgs, labels

    def run(self, ctx, cfg, inputs):
        import warnings
        import numpy as np
        from sklearn.exceptions import ConvergenceWarning
        from sklearn.metrics import log_loss
        from sklearn.neural_network import MLPClassifier
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        self.infer(ctx, cfg, {'train': inputs['train'].schema, 'validation': inputs['validation'].schema})
        timgs, tlabels = self._pairs(inputs['train'], cfg['image_field'], cfg['label_field'])
        vimgs, vlabels = self._pairs(inputs['validation'], *self._vfields(cfg))
        classes = sorted(set(tlabels))
        if len(classes) < 2:
            raise DataError('Image Recognition needs at least two labels in the training images')
        if not vimgs:
            raise DataError('The validation input has no labelled images')
        feats = image_features(ctx, cfg['model'], timgs + vimgs, 'Image Recognition')
        xt, xv = feats[:len(timgs)], feats[len(timgs):]
        index = {c: i for i, c in enumerate(classes)}
        yt = np.array([index[l] for l in tlabels])
        known = np.array([l in index for l in vlabels])
        yv = np.array([index.get(l, -1) for l in vlabels])
        scaler = StandardScaler().fit(xt)
        clf = MLPClassifier(hidden_layer_sizes=(cfg['hidden_units'],), learning_rate_init=cfg['learning_rate'],
                            batch_size=min(cfg['batch_size'], len(xt)), random_state=cfg['seed'], alpha=1e-3)
        history = []
        st, sv = scaler.transform(xt), scaler.transform(xv)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', ConvergenceWarning)
            for epoch in range(1, cfg['epochs'] + 1):
                ctx.check_cancelled()
                clf.partial_fit(st, yt, classes=np.arange(len(classes)))
                pt, pv = clf.predict_proba(st), clf.predict_proba(sv)
                vk = known.any()
                history.append((epoch, float((pt.argmax(1) == yt).mean()), float(log_loss(yt, pt, labels=range(len(classes)))),
                                float((pv[known].argmax(1) == yv[known]).mean()) if vk else None,
                                float(log_loss(yv[known], pv[known], labels=range(len(classes)))) if vk else None))
        pred = clf.predict(sv)
        rows = []
        for c, name in enumerate(classes):
            tp = int(((pred == c) & (yv == c)).sum())
            fp = int(((pred == c) & (yv != c)).sum())
            fn = int(((pred != c) & (yv == c)).sum())
            tn = len(yv) - tp - fp - fn
            rows.append((name, tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0, (tp + tn) / len(yv), tp + fn))
        rows.append(('(overall)', None, None, float((pred == yv).mean()), len(yv)))
        unseen = sorted({l for l in vlabels if l not in index})
        if unseen:
            ctx.warn(f'Validation labels {unseen} never appear in training and cannot be predicted')
        report_rows = []
        for title, a, b in (('Accuracy per epoch', 1, 3), ('Loss per epoch', 2, 4)):
            canvas = charts.line_chart({'training': [(h[0], h[a]) for h in history],
                                        'validation': [(h[0], h[b]) for h in history]},
                                       title=title, x_label='Epoch', y_label=title.split()[0])
            report_rows.append((canvas.to_svg(), title))
        pipe = Pipeline([('scale', scaler), ('network', clf)])
        info = {'family': 'image_classifier', 'task': 'classification', 'target': cfg['label_field'], 'image_field': cfg['image_field'],
                'predictors': [cfg['image_field']], 'classes': classes, 'backbone': cfg['model'], 'epochs': cfg['epochs'],
                'metrics': {'Accuracy': rows[-1][3]}, 'history': history}
        name = cfg['model_name'] or f'Image classifier ({cfg["model"]})'
        return {'model': model_table(name, 'image_classifier', cfg['label_field'], info, modeling.sk_dumps(pipe)),
                'evaluation': Table.from_rows(self.EVAL, rows), 'report': Table.from_rows(charts.chart_schema(), report_rows)}


TOOLS = [ImageToText, KeyValuePairExtraction, ImageRecognition]
