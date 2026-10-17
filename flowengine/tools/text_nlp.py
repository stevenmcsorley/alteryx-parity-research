"""Text Mining tools that need downloaded components: Text Pre-processing, Named Entity Recognition and the
Part-of-Speech Tagger (spaCy), and Zero-shot Text Classification (ONNX Runtime and an NLI model).

The components install on first use (see ``flowengine.addons``) and run in a separate process. Each tool's
``downloads(config)`` lists the components it needs, so the run preflight can offer the download before the
run starts.

* spaCy uses its small official models for English, French, German, Italian, Portuguese and Spanish
  (``*_core_*_sm`` 3.8.0).
* Custom entities are matched by a spaCy entity ruler placed ahead of the statistical recogniser. It finds
  every listed term, with or without case sensitivity. The network is not retrained, as it is by the
  vendor's custom training option.
* Zero-shot classification uses the standard NLI method, scoring "This example is {label}." for entailment.
  The model is cross-encoder/nli-deberta-v3-xsmall (Apache-2.0) as a quantized ONNX export. It is not the
  vendor's unnamed model.
"""
from __future__ import annotations

import json

from .. import addons
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import MODEL_SCHEMA, missing, model_table
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
LANGUAGES = list(addons.SPACY_MODELS)
_LANG = {'enum': LANGUAGES, 'default': 'english'}


def _texts(table, field):
    return ['' if missing(v) else str(v) for v in table.column(field)]


def _spacy(ctx, tool, request, components):
    addons.require(components, tool)
    return addons.run_worker(ctx, 'spacy_worker.py', request)['results']


def _model_name(language):
    return addons.SPACY_MODELS[language][0]


def _clash(schema, names, tool):
    clash = [n for n in names if n in schema]
    if clash:
        raise SchemaError(f'{tool} would overwrite field(s) {clash}')


# =========================================================================== Text Pre-processing

class TextPreprocessing(Tool):
    manifest = ToolManifest(
        tool_id='core.text_preprocessing', version='1.0.0', display_name='Text Pre-processing',
        catalog_ids=('text-mining:text-pre-processing',),
        inputs=(Anchor('input', 'Input', description='Text records'),
                Anchor('stopwords', 'Stop words', required=False, description='Optional extra stop words, one per record')),
        outputs=(Anchor('output', 'Output', description='The records with <field>_processed'),),
        description='Prepare text for analysis with spaCy. It tokenizes the text, optionally converts words to '
                    'their root (lemmatize: "running", "ran" → "run"), and removes digits, punctuation and stop '
                    'words: the language\'s defaults, a custom list and/or the Stop words input. The tokens are '
                    'joined by spaces in <field>_processed. Lemmatizing uses the language\'s spaCy model, which '
                    'downloads on first use; everything else needs only the language pack.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1}, 'language': _LANG,
            'lemmatize': {'type': 'boolean', 'default': False},
            'filter_digits': {'type': 'boolean', 'default': True},
            'filter_punctuation': {'type': 'boolean', 'default': True},
            'filter_stopwords': {'type': 'boolean', 'default': True},
            'default_stopwords': {'type': 'boolean', 'default': True},
            'custom_stopwords': {'type': 'string', 'default': '', 'description': 'Words separated by commas'}}})

    @staticmethod
    def downloads(cfg):
        return ['language-pack'] + ([f'spacy-model:{cfg.get("language", "english")}'] if cfg.get('lemmatize') else [])

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['text_field']])
        out = f'{cfg["text_field"]}_processed'
        _clash(inputs['input'], [out], 'Text Pre-processing')
        return {'output': Schema([*inputs['input'], Field(out, FieldType.V_WSTRING, TEXT)])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        custom = [w.strip() for w in cfg['custom_stopwords'].split(',') if w.strip()]
        if inputs.get('stopwords') is not None:
            sw = inputs['stopwords']
            text_fields = [f.name for f in sw.schema if f.type.is_string]
            if not text_fields:
                raise SchemaError('The stop-word input needs a text field')
            custom += [str(v).strip() for v in sw.column(text_fields[0]) if not missing(v) and str(v).strip()]
        request = {'task': 'preprocess', 'language': cfg['language'],
                   'model': _model_name(cfg['language']) if cfg['lemmatize'] else None,
                   'texts': _texts(table, cfg['text_field']), 'lemmatize': cfg['lemmatize'],
                   'filter_digits': cfg['filter_digits'], 'filter_punctuation': cfg['filter_punctuation'],
                   'filter_stopwords': cfg['filter_stopwords'], 'default_stopwords': cfg['default_stopwords'],
                   'custom_stopwords': custom}
        results = _spacy(ctx, 'Text Pre-processing', request, self.downloads(cfg))
        rows = [(*r, None if missing(v) else out) for r, v, out in zip(table.rows(), table.column(cfg['text_field']), results)]
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== Named Entity Recognition

def ner_apply(ctx, info, table, tool='Named Entity Recognition'):
    if info['text_field'] not in table.schema:
        raise SchemaError(f"Data has no text field {info['text_field']!r}")
    _clash(table.schema, ['entities', 'entity_diagram'], tool)
    request = {'task': 'ner', 'language': info['language'], 'model': _model_name(info['language']),
               'texts': _texts(table, info['text_field']), 'patterns': info['patterns'], 'case_sensitive': info['case_sensitive']}
    results = _spacy(ctx, tool, request, ['language-pack', f'spacy-model:{info["language"]}'])
    schema = Schema([*table.schema, Field('entities', FieldType.V_WSTRING, TEXT), Field('entity_diagram', FieldType.V_WSTRING, TEXT)])
    rows = [(*r, json.dumps(res['entities'], ensure_ascii=False), res['diagram']) if not missing(v) else (*r, None, None)
            for r, v, res in zip(table.rows(), table.column(info['text_field']), results)]
    return Table.from_rows(schema, rows)


class NamedEntityRecognition(Tool):
    manifest = ToolManifest(
        tool_id='core.named_entity_recognition', version='1.0.0', display_name='Named Entity Recognition',
        catalog_ids=('text-mining:named-entity-recognition',),
        inputs=(Anchor('input', 'D', description='Text records'),
                Anchor('entities', 'E', required=False, description='Optional custom entities: an entity column and a label column')),
        outputs=(Anchor('output', 'D', description='The records with entities (JSON) and entity_diagram (HTML)'),
                 Anchor('model', 'M', description='Model for Predict')),
        description='Find named entities with spaCy\'s small model for the language. English has 18 types, among '
                    'them PERSON, ORG, GPE, DATE and MONEY. entities is a JSON list of {entity, label, '
                    'character_index, word_index, entity_length} (indexes from 0), and entity_diagram is an HTML '
                    'view. Custom entities from E (for example Riesling → GRAPE) take priority. They are matched by '
                    'an entity ruler, case-sensitively or not, rather than by retraining the network. Models '
                    'download on first use.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1}, 'language': _LANG,
            'train_new_entities': {'type': 'boolean', 'default': False},
            'entity_field': {'type': 'string', 'default': 'Entity'}, 'label_field': {'type': 'string', 'default': 'Label'},
            'case_sensitive': {'type': 'boolean', 'default': False},
            'model_name': {'type': ['string', 'null'], 'default': None}}})

    @staticmethod
    def downloads(cfg):
        return ['language-pack', f'spacy-model:{cfg.get("language", "english")}']

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['text_field']])
        if cfg['train_new_entities']:
            if inputs.get('entities') is None:
                raise ConfigError('Connect the custom entity list to E to train with new entities')
            require_fields(inputs['entities'], [cfg['entity_field'], cfg['label_field']])
        _clash(inputs['input'], ['entities', 'entity_diagram'], 'Named Entity Recognition')
        return {'output': Schema([*inputs['input'], Field('entities', FieldType.V_WSTRING, TEXT),
                                  Field('entity_diagram', FieldType.V_WSTRING, TEXT)]), 'model': MODEL_SCHEMA}

    def run(self, ctx, cfg, inputs):
        self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items() if v is not None})
        patterns = []
        if cfg['train_new_entities']:
            ent = inputs['entities']
            for e, label in zip(ent.column(cfg['entity_field']), ent.column(cfg['label_field'])):
                if missing(e) or missing(label) or not str(e).strip() or not str(label).strip():
                    raise DataError('Every custom entity needs an entity and a label (empty rows are not allowed)')
                patterns.append({'entity': str(e).strip(), 'label': str(label).strip().upper()})
        info = {'family': 'ner', 'task': 'ner', 'target': None, 'predictors': [cfg['text_field']], 'text_field': cfg['text_field'],
                'language': cfg['language'], 'model': _model_name(cfg['language']), 'model_version': addons.SPACY_MODEL_VERSION,
                'patterns': patterns, 'case_sensitive': cfg['case_sensitive']}
        name = cfg['model_name'] or f'Entities ({cfg["language"]}{", custom" if patterns else ""})'
        return {'output': ner_apply(ctx, info, inputs['input']),
                'model': model_table(name, 'ner', None, info, b'')}


# =========================================================================== Part-of-Speech Tagger

class PartOfSpeechTagger(Tool):
    manifest = ToolManifest(
        tool_id='core.part_of_speech_tagger', version='1.0.0', display_name='Part-of-Speech Tagger',
        catalog_ids=('text-mining:part-of-speech-tagger',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Output', description='The records with part_of_speech_tags (JSON) and dependency_diagram'),),
        description='Tag every word with spaCy\'s small model for the language. part_of_speech_tags is a JSON list '
                    'with, for each token, text, part_of_speech (coarse universal tag), fine_grained_tag and '
                    'dependency (each with an English description), character_index, word_index and text_length. '
                    'dependency_diagram is a displaCy SVG of the parse. Models download on first use.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1}, 'language': _LANG}})

    @staticmethod
    def downloads(cfg):
        return ['language-pack', f'spacy-model:{cfg.get("language", "english")}']

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['text_field']])
        _clash(inputs['input'], ['part_of_speech_tags', 'dependency_diagram'], 'Part-of-Speech Tagger')
        return {'output': Schema([*inputs['input'], Field('part_of_speech_tags', FieldType.V_WSTRING, TEXT),
                                  Field('dependency_diagram', FieldType.V_WSTRING, TEXT)])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        request = {'task': 'pos', 'language': cfg['language'], 'model': _model_name(cfg['language']),
                   'texts': _texts(table, cfg['text_field'])}
        results = _spacy(ctx, 'Part-of-Speech Tagger', request, self.downloads(cfg))
        rows = [(*r, json.dumps(res['tags'], ensure_ascii=False), res['diagram']) if not missing(v) else (*r, None, None)
                for r, v, res in zip(table.rows(), table.column(cfg['text_field']), results)]
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== Zero-shot Text Classification

class ZeroShotClassification(Tool):
    manifest = ToolManifest(
        tool_id='core.zero_shot_classification', version='1.0.0', display_name='Zero-shot Text Classification',
        catalog_ids=('text-mining:zero-shot-text-classification',),
        inputs=(Anchor('input', 'D', description='Text records'), Anchor('labels', 'L', description='Category labels, one per record')),
        outputs=(Anchor('output', 'Output', description='One score field per label, and top_label'),),
        description='Score English text against labels you choose, with no training. Each label becomes the '
                    'hypothesis "This example is {label}." (the template can be changed), scored for entailment by '
                    'an NLI model (DeBERTa-v3 xsmall, quantized ONNX, about 92 MB, downloaded on first use). '
                    'Scores share 1 across the labels, or with multi-label on each label is scored on its own '
                    'from 0 to 1. top_label, added when there is more than one label, is the highest-scoring '
                    'label.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['text_field', 'label_field'], 'properties': {
            'text_field': {'type': 'string', 'minLength': 1}, 'label_field': {'type': 'string', 'minLength': 1},
            'multi_label': {'type': 'boolean', 'default': False},
            'template': {'type': 'string', 'pattern': r'\{\}', 'default': 'This example is {}.'}}})

    @staticmethod
    def downloads(cfg):
        return ['neural-runtime', 'zero-shot-model']

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['text_field']])
        require_fields(inputs['labels'], [cfg['label_field']])
        return None  # one field per label

    def run(self, ctx, cfg, inputs):
        table, lab = inputs['input'], inputs['labels']
        require_fields(table.schema, [cfg['text_field']])
        require_fields(lab.schema, [cfg['label_field']])
        labels = []
        for v in lab.column(cfg['label_field']):
            text = '' if missing(v) else format_value(v).strip()
            if text and text not in labels:
                labels.append(text)
        if not labels:
            raise DataError('The label input has no labels')
        if len(labels) > 100:
            raise DataError(f'{len(labels)} labels; at most 100 are supported')
        names = labels + (['top_label'] if len(labels) > 1 else [])
        _clash(table.schema, names, 'Zero-shot Text Classification')
        addons.require(self.downloads(cfg), 'Zero-shot Text Classification')
        texts = _texts(table, cfg['text_field'])
        present = [i for i, t in enumerate(texts) if t.strip()]
        request = {'model_dir': str(addons.zero_shot_dir()), 'texts': [texts[i] for i in present], 'labels': labels,
                   'template': cfg['template'], 'multi_label': cfg['multi_label']}
        scores = dict(zip(present, addons.run_worker(ctx, 'zeroshot_worker.py', request)['results'])) if present else {}
        schema = Schema([*table.schema, *[Field(l, FieldType.DOUBLE) for l in labels],
                         *([Field('top_label', FieldType.V_WSTRING, TEXT)] if len(labels) > 1 else [])])
        rows = []
        for i, r in enumerate(table.rows()):
            s = scores.get(i)
            if s is None:
                rows.append((*r, *([None] * (len(schema) - len(r)))))
                continue
            top = [labels[max(range(len(labels)), key=lambda k: s[k])]] if len(labels) > 1 else []
            rows.append((*r, *s, *top))
        return {'output': Table.from_rows(schema, rows)}


TOOLS = [TextPreprocessing, NamedEntityRecognition, PartOfSpeechTagger, ZeroShotClassification]
