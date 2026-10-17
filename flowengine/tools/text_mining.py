"""Text Mining tools: sentiment, summaries, topics, word clouds and text classification.

Independent implementations of the methods the vendor documents:

* VADER sentiment (the ``vaderSentiment`` package)
* TextRank extractive summaries (``summa``, vendored in ``flowengine._vendor``)
* latent Dirichlet allocation (scikit-learn), with pyLDAvis-style relevance and saliency
* TF-IDF text classifiers (multinomial naive Bayes or a linear SVM, chosen by cross-validated grid search)
* a word-cloud layout drawn as an SVG chart

Stop words come from summa's lists (English, German, Spanish, Portuguese, Italian); French uses none. The
spaCy-based tools are separate.
"""
from __future__ import annotations

import io
import json
import math
import re
from collections import Counter

from .. import charts, modeling
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import MODEL_SCHEMA, missing, model_table, read_models
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
LANGUAGES = {'english': 'English', 'french': 'French', 'german': 'German', 'italian': 'Italian',
             'portuguese': 'Portuguese', 'spanish': 'Spanish'}
_LANG = {'enum': list(LANGUAGES), 'default': 'english'}
_SENTENCE = re.compile(r'(?<=[.!?])\s+')
_WORD = re.compile(r"[^\W\d_](?:[\w'’-]*[^\W_])?", re.UNICODE)


def stop_words(language):
    from .._vendor.summa.preprocessing.stopwords import get_stopwords_by_language
    return {w.strip() for w in get_stopwords_by_language(language).split() if w.strip()}


def _text(v):
    return '' if missing(v) else str(v)


# =========================================================================== Sentiment Analysis

class SentimentAnalysis(Tool):
    COLUMNS = ('negative_sentiment', 'neutral_sentiment', 'positive_sentiment', 'compound_sentiment_score')
    manifest = ToolManifest(
        tool_id='core.sentiment_analysis', version='1.0.0', display_name='Sentiment Analysis',
        catalog_ids=('text-mining:sentiment-analysis',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        description='Score English text with VADER (Valence Aware Dictionary for sEntiment Reasoning). Adds '
                    'negative_sentiment, neutral_sentiment and positive_sentiment (shares of the text, summing to '
                    'about 1) and compound_sentiment_score (−1 to 1). At sentence level, the text is split at '
                    'end punctuation and each score is averaged over the sentences. With categories on, '
                    'sentiment_category is negative up to max_negative, positive from min_positive, and neutral '
                    'between (VADER\'s usual cut-offs of −0.05 and 0.05 by default). Use it on raw text: '
                    'pre-processing removes cues VADER relies on, such as capitals, punctuation and negations.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1},
            'sentence_level': {'type': 'boolean', 'default': False},
            'categorical': {'type': 'boolean', 'default': False},
            'max_negative': {'type': 'number', 'minimum': -1, 'maximum': 1, 'default': -0.05},
            'min_positive': {'type': 'number', 'minimum': -1, 'maximum': 1, 'default': 0.05}}})

    def _schema(self, schema, cfg):
        added = [Field(c, FieldType.DOUBLE) for c in self.COLUMNS]
        if cfg['categorical']:
            added.append(Field('sentiment_category', FieldType.V_WSTRING, 16))
        clash = [f.name for f in added if f.name in schema]
        if clash:
            raise SchemaError(f'Sentiment Analysis would overwrite field(s) {clash}')
        return Schema([*schema, *added])

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['text_field']])
        if cfg['max_negative'] > cfg['min_positive']:
            raise ConfigError('max_negative must not be above min_positive')
        return {'output': self._schema(inputs['input'], cfg)}

    def run(self, ctx, cfg, inputs):
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        vader = SentimentIntensityAnalyzer()
        out = []
        for row, value in zip(table.rows(), table.column(cfg['text_field'])):
            text = _text(value).strip()
            if not text:
                out.append((*row, *([None] * (len(schema) - len(row)))))
                continue
            parts = [s for s in _SENTENCE.split(text) if s.strip()] if cfg['sentence_level'] else [text]
            scores = [vader.polarity_scores(p) for p in parts]
            neg, neu, pos, comp = (sum(s[k] for s in scores) / len(scores) for k in ('neg', 'neu', 'pos', 'compound'))
            extra = []
            if cfg['categorical']:
                extra = ['negative' if comp <= cfg['max_negative'] else 'positive' if comp >= cfg['min_positive'] else 'neutral']
            out.append((*row, neg, neu, pos, comp, *extra))
        return {'output': Table.from_rows(schema, out)}


# =========================================================================== Text Summary

class TextSummary(Tool):
    manifest = ToolManifest(
        tool_id='core.text_summary', version='1.0.0', display_name='Text Summary', catalog_ids=('text-mining:text-summary',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        description='Extractive summary by TextRank (the summa implementation). Sentences are ranked by PageRank '
                    'on their word-overlap similarity graph, and the top sentences are returned unchanged, most '
                    'important first, in a new summary field. Texts with no more sentences than the summary length '
                    'are returned whole, with a warning.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1}, 'language': _LANG,
            'sentences': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 3},
            'output_field': {'type': 'string', 'minLength': 1, 'default': 'summary'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['text_field']])
        if cfg['output_field'] in inputs['input']:
            raise SchemaError(f'Field {cfg["output_field"]!r} already exists')
        return {'output': Schema([*inputs['input'], Field(cfg['output_field'], FieldType.V_WSTRING, TEXT)])}

    def run(self, ctx, cfg, inputs):
        from .._vendor.summa import summarizer
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        short, out = 0, []
        for row, value in zip(table.rows(), table.column(cfg['text_field'])):
            text = _text(value).strip()
            ranked = summarizer.summarize(text, ratio=1.0, language=cfg['language'], split=True, scores=True) if text else []
            if len(ranked) <= cfg['sentences']:
                short += bool(text)
                out.append((*row, text or None))
                continue
            order = sorted(range(len(ranked)), key=lambda i: (-ranked[i][1], i))[:cfg['sentences']]
            out.append((*row, ' '.join(ranked[i][0] for i in order)))
        if short:
            ctx.warn(f'{short} text(s) had no more than {cfg["sentences"]} sentence(s) and were returned whole')
        return {'output': Table.from_rows(schema, out)}


# =========================================================================== Topic Modeling

def _vectorizer(cfg, language):
    from sklearn.feature_extraction.text import CountVectorizer
    stops = sorted(stop_words(language)) or None
    return CountVectorizer(lowercase=True, stop_words=stops, min_df=cfg['min_frequency'], max_df=cfg['max_frequency'],
                           max_features=cfg['max_words'] or None, token_pattern=r"(?u)\b[^\W\d_][\w'-]*[^\W_]\b")


def topic_matrix(info, estimator, table):
    import numpy as np
    texts = [_text(v) for v in table.column(info['text_field'])]
    mask = np.array([bool(t.strip()) for t in texts])
    out = np.full((len(texts), info['topics']), np.nan)
    if mask.any():
        out[mask] = estimator.transform([t for t, m in zip(texts, mask) if m])
    return out, mask


class TopicModeling(Tool):
    WORDS = Schema([Field('Topic', FieldType.INT32), Field('Word', FieldType.V_WSTRING, 255), Field('Rank', FieldType.INT32),
                    Field('Relevance', FieldType.DOUBLE), Field('Probability', FieldType.DOUBLE),
                    Field('Lift', FieldType.DOUBLE), Field('Saliency', FieldType.DOUBLE), Field('Frequency', FieldType.INT64)])
    manifest = ToolManifest(
        tool_id='core.topic_modeling', version='1.0.0', display_name='Topic Modeling', catalog_ids=('text-mining:topic-modeling',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'D', description='Records with topic_1 … topic_k shares'),
                 Anchor('report', 'R', description='Interactive chart or word relevance summary'),
                 Anchor('model', 'M', description='Model for Predict')),
        description='Latent Dirichlet allocation (scikit-learn, batch variational Bayes, deterministic for a seed). '
                    'Words are counted after removing the language\'s stop words, keeping words that appear in at '
                    'least min_frequency and at most max_frequency of documents (shares, or whole counts above 1), '
                    'and at most max_words words when set. Adds one topic share per topic to each record. The '
                    'report is either a chart (top terms per topic, plus an intertopic distance map from '
                    'Jensen–Shannon distances scaled to two dimensions) or the word relevance summary. Relevance '
                    'uses λ = 0.6 and saliency is defined as in Chuang et al. (2012).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1}, 'language': _LANG,
            'topics': {'type': 'integer', 'minimum': 2, 'maximum': 100, 'default': 3},
            'report': {'enum': ['chart', 'word_relevance'], 'default': 'chart'},
            'min_frequency': {'type': 'number', 'minimum': 0, 'default': 0.01},
            'max_frequency': {'type': 'number', 'minimum': 0, 'default': 0.8},
            'max_words': {'type': 'integer', 'minimum': 0, 'default': 0},
            'alpha': {'type': ['number', 'null'], 'exclusiveMinimum': 0, 'default': None},
            'eta': {'type': ['number', 'null'], 'minimum': 0, 'default': None},
            'top_words': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 10},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1}, 'model_name': {'type': ['string', 'null'], 'default': None}}})

    def _names(self, cfg):
        return [f'topic_{i + 1}' for i in range(cfg['topics'])]

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['text_field']])
        clash = [n for n in self._names(cfg) if n in inputs['input']]
        if clash:
            raise SchemaError(f'Topic Modeling would overwrite field(s) {clash}')
        report = charts.chart_schema() if cfg['report'] == 'chart' else self.WORDS
        return {'output': Schema([*inputs['input'], *[Field(n, FieldType.DOUBLE) for n in self._names(cfg)]]),
                'report': report, 'model': MODEL_SCHEMA}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        from sklearn.decomposition import LatentDirichletAllocation
        from sklearn.pipeline import Pipeline
        table = inputs['input']
        schemas = self.infer(ctx, cfg, {'input': table.schema})
        texts = [_text(v) for v in table.column(cfg['text_field'])]
        docs = [t for t in texts if t.strip()]
        if len(docs) < cfg['topics']:
            raise DataError(f'Topic Modeling needs at least {cfg["topics"]} non-empty texts; there are {len(docs)}')
        mn, mx = cfg['min_frequency'], cfg['max_frequency']
        cfg = {**cfg, 'min_frequency': int(mn) if mn > 1 else mn, 'max_frequency': int(mx) if mx > 1 else mx}
        lda = LatentDirichletAllocation(n_components=cfg['topics'], doc_topic_prior=cfg['alpha'], topic_word_prior=cfg['eta'],
                                        learning_method='batch', max_iter=50, random_state=cfg['seed'])
        pipe = Pipeline([('counts', _vectorizer(cfg, cfg['language'])), ('lda', lda)])
        try:
            counts = pipe[0].fit_transform(docs)
        except ValueError as e:
            raise DataError(f'No words remain after the dictionary options: {e}') from None
        theta = pipe[1].fit_transform(counts)
        info = {'family': 'topic_model', 'task': 'topics', 'text_field': cfg['text_field'], 'topics': cfg['topics'],
                'language': cfg['language'], 'target': None, 'predictors': [cfg['text_field']],
                'perplexity': float(pipe[1].perplexity(counts))}
        doc_topics = np.full((len(texts), cfg['topics']), np.nan)
        doc_topics[[i for i, t in enumerate(texts) if t.strip()]] = theta
        rows = [(*r, *[None if math.isnan(x) else float(x) for x in d]) for r, d in zip(table.rows(), doc_topics)]
        words = self._words(pipe, counts, theta, cfg)
        report = self._chart(words, theta, pipe, cfg) if cfg['report'] == 'chart' else Table.from_rows(self.WORDS, words)
        name = cfg['model_name'] or f'Topic model ({cfg["topics"]} topics)'
        return {'output': Table.from_rows(schemas['output'], rows), 'report': report,
                'model': model_table(name, 'topic_model', None, info, modeling.sk_dumps(pipe))}

    def _words(self, pipe, counts, theta, cfg, lam=0.6):
        """Per topic: the top words by relevance, with probability, lift, saliency and corpus frequency."""
        import numpy as np
        vocab = pipe[0].get_feature_names_out()
        phi = pipe[1].components_ / pipe[1].components_.sum(axis=1, keepdims=True)
        freq = np.asarray(counts.sum(axis=0)).ravel()
        pw = freq / freq.sum()
        topic_share = (theta * np.asarray(counts.sum(axis=1)).ravel()[:, None]).sum(axis=0)
        topic_share = topic_share / topic_share.sum()
        p_t_given_w = phi * topic_share[:, None] / np.maximum((phi * topic_share[:, None]).sum(axis=0), 1e-300)
        distinct = (p_t_given_w * np.log(np.maximum(p_t_given_w, 1e-300) / topic_share[:, None])).sum(axis=0)
        saliency = pw * distinct
        lift = phi / np.maximum(pw, 1e-300)
        relevance = lam * np.log(np.maximum(phi, 1e-300)) + (1 - lam) * np.log(np.maximum(lift, 1e-300))
        rows = []
        for t in range(phi.shape[0]):
            order = np.argsort(-relevance[t], kind='stable')[:cfg['top_words']]
            for rank, w in enumerate(order, 1):
                rows.append((t + 1, str(vocab[w]), rank, float(relevance[t, w]), float(phi[t, w]), float(lift[t, w]),
                             float(saliency[w]), int(freq[w])))
        return rows

    def _chart(self, words, theta, pipe, cfg):
        import numpy as np
        from scipy.spatial.distance import jensenshannon
        k = cfg['topics']
        phi = pipe[1].components_ / pipe[1].components_.sum(axis=1, keepdims=True)
        width, panel = 900, 26 * cfg['top_words'] + 60
        cols = min(k, 3)
        grid_rows = math.ceil(k / cols)
        map_h = 360
        canvas = charts.Canvas(width, 70 + grid_rows * panel + map_h, f'Topics ({k})')
        canvas.text(width / 2, 30, f'Top terms by relevance (λ = 0.6), {k} topics', size=16, anchor='middle', weight='bold')
        pw = (width - 40) / cols
        for t in range(k):
            x0, y0 = 20 + (t % cols) * pw, 60 + (t // cols) * panel
            canvas.text(x0, y0 + 14, f'Topic {t + 1}', size=13, weight='bold', fill=charts.series_color(t))
            mine = [w for w in words if w[0] == t + 1]
            top = max((w[4] for w in mine), default=1) or 1
            for j, w in enumerate(mine):
                y = y0 + 26 + j * 26
                canvas.text(x0, y + 14, w[1], size=11)
                canvas.rect(x0 + 110, y + 3, (pw - 130) * w[4] / top, 14, fill=charts.series_color(t), opacity=0.8,
                            title=f'{w[1]}: p = {w[4]:.4f}, relevance {w[3]:.3f}')
        # intertopic distance map: classical MDS on Jensen-Shannon distances
        d = np.array([[jensenshannon(phi[i], phi[j]) for j in range(k)] for i in range(k)])
        j = np.eye(k) - 1.0 / k
        b = -0.5 * j @ (d ** 2) @ j
        vals, vecs = np.linalg.eigh(b)
        order = np.argsort(vals)[::-1][:2]
        xy = vecs[:, order] * np.sqrt(np.maximum(vals[order], 0))
        top = 70 + grid_rows * panel
        canvas.text(width / 2, top + 10, 'Intertopic distance map', size=14, anchor='middle', weight='bold')
        share = theta.sum(axis=0) / theta.sum()
        span = max(float(np.abs(xy).max()), 1e-9)
        cx, cy, r = width / 2, top + map_h / 2 + 10, map_h / 2 - 40
        canvas.line(cx - r, cy, cx + r, cy, dash='3,3')
        canvas.line(cx, cy - r, cx, cy + r, dash='3,3')
        for t in range(k):
            px, py = cx + xy[t, 0] / span * r * 0.85, cy - xy[t, 1] / span * r * 0.85
            canvas.circle(px, py, 10 + 40 * math.sqrt(share[t]), fill=charts.series_color(t), opacity=0.45,
                          title=f'Topic {t + 1}: {share[t]:.1%} of tokens')
            canvas.text(px, py + 4, str(t + 1), size=12, anchor='middle', weight='bold')
        return charts.chart_table(canvas, f'Topics ({k})')


# =========================================================================== Word Cloud

THEMES = ('viridis', 'blues', 'reds', 'categorical', 'mono')


def _theme_color(theme, i, n, rank_share):
    """Colour for the i-th largest word; rank_share is 1 for the largest and 0 for the smallest."""
    if theme == 'categorical':
        return charts.series_color(i)
    if theme == 'mono':
        return '#1f2937'
    if theme == 'viridis':  # dark purple to green; the light yellow end is too faint on white
        return charts.color_at('viridis', 0.05 + 0.65 * (1 - rank_share))
    return charts.color_at(theme, 0.35 + 0.65 * rank_share)  # light to dark sequential palettes


def _mask_grid(blob, width, height):
    """Boolean grid (height x width): True where words may go (dark pixels of the mask image)."""
    import numpy as np
    from PIL import Image, UnidentifiedImageError
    try:
        img = Image.open(io.BytesIO(blob))
    except (UnidentifiedImageError, OSError):
        raise DataError('The mask is not an image (use PNG or JPEG)') from None
    img = img.convert('L').resize((width, height))
    return np.asarray(img) < 250


def word_counts(texts, language, phrases, use_stops, extra_stops):
    stops = (stop_words(language) if use_stops else set()) | {w.lower() for w in extra_stops}
    counts = Counter()
    for text in texts:
        tokens = [t.lower() for t in _WORD.findall(text)]
        kept = [t for t in tokens if t not in stops and len(t) > 1]
        counts.update(kept)
        if phrases:
            counts.update(f'{a} {b}' for a, b in zip(tokens, tokens[1:])
                          if a not in stops and b not in stops and len(a) > 1 and len(b) > 1)
    if phrases:  # a phrase that is as common as its words replaces them
        for phrase, n in list(counts.items()):
            if ' ' in phrase and n >= 2:
                for w in phrase.split():
                    if counts.get(w, 0) <= n:
                        counts.pop(w, None)
    return counts


def layout_words(counts, width, height, max_words, mask=None, seed=1, prefer_horizontal=0.9, max_size=None, min_size=8):
    """Greedy spiral placement (largest first). Returns [(word, size, x, y, w, h, rotated)] in pixels."""
    import numpy as np
    from reportlab.pdfbase.pdfmetrics import stringWidth
    rng = np.random.default_rng(seed)
    items = counts.most_common(max_words)
    if not items:
        return []
    top = items[0][1]
    occupied = np.zeros((height, width), dtype=bool) if mask is None else ~mask
    free_integral = np.zeros((height + 1, width + 1))
    max_size = max_size or max(min_size + 2, int(height * 0.22))
    placed, size = [], max_size
    for word, n in items:
        start = size = min(size, max(min_size, round(max_size * (0.35 + 0.65 * n / top))))
        rotated = rng.random() > prefer_horizontal
        spot = None
        while spot is None and size >= min_size:
            tw, th = stringWidth(word, 'Helvetica', size) + 2, size * 1.15
            w, h = (th, tw) if rotated else (tw, th)
            w, h = int(math.ceil(w)), int(math.ceil(h))
            if w < width and h < height:
                np.cumsum(np.cumsum(occupied, axis=0), axis=1, out=free_integral[1:, 1:])
                spot = _spiral(free_integral, w, h, width, height, rng)
            if spot is None:
                size = int(size * 0.85)
        if spot is None:
            size = start  # this word does not fit; the next (shorter) words may
            continue
        x, y = spot
        occupied[y:y + h, x:x + w] = True
        placed.append((word, size, x, y, w, h, rotated))
    return placed


def _spiral(integral, w, h, width, height, rng):
    """First free w×h box along an Archimedean spiral from a jittered centre."""
    cx = width / 2 + rng.uniform(-0.05, 0.05) * width - w / 2
    cy = height / 2 + rng.uniform(-0.05, 0.05) * height - h / 2
    step = 0.0
    while step < 2.5 * max(width, height):
        angle = step / 4.0
        x, y = int(cx + step * math.cos(angle) * width / height), int(cy + step * math.sin(angle))
        if 0 <= x and 0 <= y and x + w <= width and y + h <= height:
            used = integral[y + h, x + w] - integral[y, x + w] - integral[y + h, x] + integral[y, x]
            if used == 0:
                return x, y
        step += 1.0
    return None


class WordCloud(Tool):
    manifest = ToolManifest(
        tool_id='core.word_cloud', version='1.0.0', display_name='Word Cloud', catalog_ids=('text-mining:word-cloud',),
        inputs=(Anchor('input', 'D', description='Text records'),
                Anchor('mask', 'M', required=False, description='Optional mask image (blob); words fill its dark area')),
        outputs=(Anchor('output', 'Output', description='Word cloud chart (SVG) and the word counts used'),),
        description='Draw the most frequent words, sized by frequency and placed largest first along a spiral '
                    'without overlaps. About 10% are set vertically. The layout is deterministic for a seed. Stop '
                    'words for the language are removed (with optional extra words). Generate phrases counts '
                    'two-word phrases, and a phrase as frequent as its words replaces them. A mask image limits '
                    'words to its dark area. Theme sets the word colours; the background can be any hex colour. '
                    'The chart works with Render and other reporting tools.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1}, 'language': _LANG,
            'stop_words': {'type': 'boolean', 'default': True},
            'extra_stop_words': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            'generate_phrases': {'type': 'boolean', 'default': False},
            'max_words': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 150},
            'width': {'type': 'integer', 'minimum': 100, 'maximum': 4000, 'default': 800},
            'height': {'type': 'integer', 'minimum': 100, 'maximum': 4000, 'default': 450},
            'background': {'type': 'string', 'pattern': '^#[0-9A-Fa-f]{6}$', 'default': '#ffffff'},
            'theme': {'enum': list(THEMES), 'default': 'viridis'},
            'title': {'type': ['string', 'null'], 'default': None},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1}}})

    SCHEMA = Schema([Field('Chart', FieldType.V_WSTRING, TEXT), Field('Title', FieldType.V_WSTRING, TEXT),
                     Field('Words', FieldType.V_WSTRING, TEXT)])

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['text_field']])
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        mask = None
        if inputs.get('mask') is not None:
            blobs = [v for f in inputs['mask'].schema if f.type is FieldType.BLOB for v in inputs['mask'].column(f.name) if v]
            if not blobs:
                raise DataError('The mask input has no image blob')
            mask = _mask_grid(blobs[0], cfg['width'], cfg['height'])
        counts = word_counts([_text(v) for v in table.column(cfg['text_field'])], cfg['language'], cfg['generate_phrases'],
                             cfg['stop_words'], cfg['extra_stop_words'])
        if not counts:
            raise DataError('No words to draw after removing stop words')
        placed = layout_words(counts, cfg['width'], cfg['height'], cfg['max_words'], mask, cfg['seed'])
        if len(placed) < min(len(counts), cfg['max_words']):
            ctx.warn(f'{len(placed)} of {min(len(counts), cfg["max_words"])} words fit; enlarge the image or reduce max_words')
        title = cfg['title'] or 'Word cloud'
        canvas = charts.Canvas(cfg['width'], cfg['height'], title)
        canvas.rect(0, 0, cfg['width'], cfg['height'], fill=cfg['background'])
        for i, (word, size, x, y, w, h, rotated) in enumerate(placed):
            color = _theme_color(cfg['theme'], i, len(placed), 1 - i / max(len(placed) - 1, 1))
            if rotated:
                canvas.text(x + w * 0.78, y + h - 1, word, size=size, fill=color, rotate=-90)
            else:
                canvas.text(x + 1, y + h * 0.80, word, size=size, fill=color)
        words = json.dumps([{'word': p[0], 'count': counts[p[0]], 'size': p[1]} for p in placed], ensure_ascii=False)
        return {'output': Table.from_rows(self.SCHEMA, [(canvas.to_svg(), title, words)])}


# =========================================================================== Text Classification

def _text_estimator(cfg, algorithm, param):
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.naive_bayes import MultinomialNB
    from sklearn.pipeline import Pipeline
    from sklearn.svm import LinearSVC
    tfidf = TfidfVectorizer(analyzer=cfg['analyzer'], min_df=cfg['min_df'], sublinear_tf=True,
                            ngram_range=(1, 2) if cfg['analyzer'] == 'word' else (2, 5))
    if algorithm == 'naive_bayes':
        clf = MultinomialNB(alpha=param)
    else:
        l1 = cfg['penalty'] == 'l1'
        clf = CalibratedClassifierCV(LinearSVC(C=param, penalty=cfg['penalty'], loss='squared_hinge' if l1 else 'hinge',
                                               dual=not l1, max_iter=5000, random_state=cfg['seed']), cv=3)
    return Pipeline([('tfidf', tfidf), ('model', clf)])


def _grid(lo, hi, steps, log):
    import numpy as np
    if steps == 1:
        return [10 ** lo if log else lo]
    return [float(v) for v in (np.logspace(lo, hi, steps) if log else np.linspace(lo, hi, steps))]


def text_predict(info, estimator, table):
    import numpy as np
    texts = [_text(v) for v in table.column(info['text_field'])] if info['text_field'] in table.schema else None
    if texts is None:
        raise SchemaError(f"Data has no text field {info['text_field']!r}")
    mask = np.array([bool(t.strip()) for t in texts])
    proba = np.full((len(texts), len(info['classes'])), np.nan)
    if mask.any():
        proba[mask] = estimator.predict_proba([t for t, m in zip(texts, mask) if m])
    return {'proba': proba, 'mask': mask, 'unseen': 0}


class TextClassification(Tool):
    EVAL = Schema([Field('Class', FieldType.V_WSTRING, TEXT), Field('Precision', FieldType.DOUBLE), Field('Recall', FieldType.DOUBLE),
                   Field('F1', FieldType.DOUBLE), Field('Support', FieldType.INT64)])
    manifest = ToolManifest(
        tool_id='core.text_classification', version='1.0.0', display_name='Text Classification',
        catalog_ids=('text-mining:text-classification',),
        inputs=(Anchor('train', 'T', description='Training text and labels'), Anchor('validation', 'V', description='Validation text and labels')),
        outputs=(Anchor('model', 'M', description='Trained model for Predict'),
                 Anchor('evaluation', 'E', description='Precision, recall and F1 per class on the validation data, '
                                                       'then accuracy and macro and weighted averages')),
        description='Train a text classifier on TF-IDF features (sublinear term frequency; words with bigrams, or '
                    'character 2–5-grams). Multinomial naive Bayes searches alpha over a linear range. Linear SVC '
                    'searches C over a log range, with probabilities calibrated by 3-fold sigmoid; the L2 penalty '
                    'uses hinge loss and L1 uses squared hinge. Auto searches both. The search is a stratified '
                    'k-fold grid search on macro F1, and the best model is refitted on all training data and '
                    'evaluated on the validation data. Connect M to Predict.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field', 'label_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1}, 'label_field': {'type': 'string', 'minLength': 1},
            'validation_text_field': {'type': ['string', 'null'], 'default': None},
            'validation_label_field': {'type': ['string', 'null'], 'default': None},
            'algorithm': {'enum': ['auto', 'naive_bayes', 'linear_svc'], 'default': 'auto'},
            'alpha_from': {'type': 'number', 'exclusiveMinimum': 0, 'default': 0.01},
            'alpha_to': {'type': 'number', 'exclusiveMinimum': 0, 'default': 1.0},
            'alpha_steps': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 5},
            'c_log_from': {'type': 'number', 'minimum': -6, 'maximum': 6, 'default': -2},
            'c_log_to': {'type': 'number', 'minimum': -6, 'maximum': 6, 'default': 2},
            'c_steps': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 5},
            'penalty': {'enum': ['l1', 'l2'], 'default': 'l2'},
            'folds': {'type': 'integer', 'minimum': 2, 'maximum': 20, 'default': 3},
            'analyzer': {'enum': ['word', 'char'], 'default': 'word'},
            'min_df': {'type': 'integer', 'minimum': 1, 'default': 1},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1}, 'model_name': {'type': ['string', 'null'], 'default': None}}})

    def _fields(self, cfg):
        return (cfg['validation_text_field'] or cfg['text_field'], cfg['validation_label_field'] or cfg['label_field'])

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['train'], [cfg['text_field'], cfg['label_field']])
        require_fields(inputs['validation'], list(self._fields(cfg)))
        return {'model': MODEL_SCHEMA, 'evaluation': self.EVAL}

    def _pairs(self, table, text_field, label_field):
        out = [(_text(t), format_value(l)) for t, l in zip(table.column(text_field), table.column(label_field))
               if not missing(l) and _text(t).strip()]
        return [t for t, _ in out], [l for _, l in out]

    def run(self, ctx, cfg, inputs):
        import warnings
        from sklearn.metrics import precision_recall_fscore_support
        from sklearn.model_selection import StratifiedKFold, cross_val_score
        self.infer(ctx, cfg, {'train': inputs['train'].schema, 'validation': inputs['validation'].schema})
        x, y = self._pairs(inputs['train'], cfg['text_field'], cfg['label_field'])
        vx, vy = self._pairs(inputs['validation'], *self._fields(cfg))
        classes = sorted(set(y))
        if len(classes) < 2:
            raise DataError('Text Classification needs at least two labels in the training data')
        smallest = min(Counter(y).values())
        if smallest < cfg['folds']:
            raise DataError(f'Every label needs at least {cfg["folds"]} training texts for {cfg["folds"]}-fold search; '
                            f'the rarest has {smallest}')
        if not vx:
            raise DataError('The validation data has no labelled texts')
        candidates = []
        if cfg['algorithm'] in ('auto', 'naive_bayes'):
            candidates += [('naive_bayes', a) for a in _grid(cfg['alpha_from'], cfg['alpha_to'], cfg['alpha_steps'], False)]
        if cfg['algorithm'] in ('auto', 'linear_svc'):
            if smallest < 3:
                raise DataError('Linear SVC calibration needs at least 3 training texts per label')
            candidates += [('linear_svc', c) for c in _grid(cfg['c_log_from'], cfg['c_log_to'], cfg['c_steps'], True)]
        folds = StratifiedKFold(n_splits=cfg['folds'], shuffle=True, random_state=cfg['seed'])
        scored = []
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            for k, (algorithm, param) in enumerate(candidates):
                ctx.check_cancelled()
                score = cross_val_score(_text_estimator(cfg, algorithm, param), x, y, cv=folds, scoring='f1_macro').mean()
                scored.append((-score, k, algorithm, param))
            _, _, algorithm, param = min(scored)
            best = _text_estimator(cfg, algorithm, param).fit(x, y)
        pred = best.predict(vx)
        p, r, f, s = precision_recall_fscore_support(vy, pred, labels=classes, zero_division=0)
        rows = [(c, float(pi), float(ri), float(fi), int(si)) for c, pi, ri, fi, si in zip(classes, p, r, f, s)]
        accuracy = sum(a == b for a, b in zip(vy, pred)) / len(vy)
        for avg in ('macro', 'weighted'):
            ap, ar, af, _ = precision_recall_fscore_support(vy, pred, labels=classes, average=avg, zero_division=0)
            rows.append((f'({avg} avg)', float(ap), float(ar), float(af), len(vy)))
        rows.insert(len(classes), ('(accuracy)', None, None, float(accuracy), len(vy)))
        info = {'family': 'text_classifier', 'task': 'classification', 'target': cfg['label_field'], 'text_field': cfg['text_field'],
                'predictors': [cfg['text_field']], 'classes': [str(c) for c in best.classes_], 'algorithm': algorithm,
                'param': param, 'cv_macro_f1': -min(scored)[0], 'metrics': {'Accuracy': accuracy, 'MacroF1': rows[-2][3]}}
        unseen = sorted(set(vy) - set(classes))
        if unseen:
            ctx.warn(f'Validation labels {unseen} never appear in training and cannot be predicted')
        name = cfg['model_name'] or f'Text classifier ({"naive Bayes" if algorithm == "naive_bayes" else "linear SVC"})'
        return {'model': model_table(name, 'text_classifier', cfg['label_field'], info, modeling.sk_dumps(best)),
                'evaluation': Table.from_rows(self.EVAL, rows)}


TOOLS = [SentimentAnalysis, TextSummary, TopicModeling, WordCloud, TextClassification]
