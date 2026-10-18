"""Azure AI Language (Cognitive Services Text Analytics) and Power Automate; no vendor code.

Functional source: the public tool help and the Azure AI Language REST reference (analyze-text, api-version
2023-04-01) / the Power Automate HTTP request trigger documentation.

* Text analytics: a Web/API connection to ``https://<resource>.cognitiveservices.azure.com`` with the key in the
  ``Ocp-Apim-Subscription-Key`` header (API key header authentication) or OAuth (scope
  ``https://cognitiveservices.azure.com/.default``). Documents are sent in the documented batch sizes (sentiment and
  key phrases 10, entities 5, language detection 1000); texts over 5,120 characters are reported, not sent.
* Power Automate: a flow's "When an HTTP request is received" trigger. The trigger URL's signed query string
  (``...&sig=...``) is the secret: save a Web/API connection with authentication "Signed URL", base URL the trigger URL
  up to the ``?``, and the part after the ``?`` as the secret. Flows that require Microsoft Entra sign-in use OAuth.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from .download import UNBOUNDED_TEXT, _HttpOnlyRedirects
from .github import _permit
from .msgraph import Graph, _graph_error

MAX_CHARS = 5120
BATCH = {'SentimentAnalysis': 10, 'KeyPhraseExtraction': 10, 'EntityRecognition': 5, 'LanguageDetection': 1000}
ANALYSES = {'sentiment': 'SentimentAnalysis', 'key_phrases': 'KeyPhraseExtraction', 'entities': 'EntityRecognition',
            'language': 'LanguageDetection'}


class Language(Graph):
    service = 'Azure AI Language'
    other_auth = True


def _text(name):
    return Field(name, FieldType.V_WSTRING, UNBOUNDED_TEXT)


OUT_FIELDS = {
    'sentiment': [_text('Sentiment'), Field('PositiveScore', FieldType.DOUBLE), Field('NeutralScore', FieldType.DOUBLE),
                  Field('NegativeScore', FieldType.DOUBLE)],
    'key_phrases': [_text('KeyPhrases')],
    'entities': [_text('Entities')],
    'language': [_text('DetectedLanguage'), _text('DetectedLanguageCode'), Field('LanguageScore', FieldType.DOUBLE)],
}


class TextAnalytics(Tool):
    manifest = ToolManifest(
        tool_id='core.azure_text_analytics', version='1.0.0', display_name='Cognitive Services Text Analytics',
        catalog_ids=('connectors:cognitive-services-text-analytics',),
        inputs=(Anchor('input', 'I', description='Records with a text field'),),
        outputs=(Anchor('output', 'O', description='Each record with the analysis fields'),),
        side_effects='network', deterministic=False,
        description='Analyse a text field with Azure AI Language: sentiment (label and scores), key phrases, named '
                    'entities (JSON) and language detection. Records whose text cannot be analysed get an AnalysisError.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'text_field', 'analyses'], 'properties': {
            'provider': {'type': 'string', 'enum': ['azure-ai-language']}, 'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web'},
            'text_field': {'type': 'string', 'minLength': 1},
            'analyses': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'enum': list(ANALYSES)}},
            'language': {'type': 'string', 'default': 'en', 'description': 'Language of the texts (ISO 639-1) for sentiment, key phrases and entities'},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        if cfg['text_field'] not in schema:
            raise ConfigError(f"The input has no field {cfg['text_field']!r}")
        added = [f for a in cfg['analyses'] for f in OUT_FIELDS[a]] + [_text('AnalysisError')]
        clash = [f.name for f in added if f.name in schema]
        if clash:
            raise ConfigError(f'Field(s) {clash} already exist')
        return {'output': Schema([*schema, *added])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'azure-ai-language')
        table: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _permit(ctx)
        api = Language(cfg)
        texts = [None if v is None else format_value(v) for v in table.column(cfg['text_field'])]
        results = [dict() for _ in texts]
        errors = [None] * len(texts)
        for i, t in enumerate(texts):
            if not t or not t.strip():
                errors[i] = 'No text to analyse'
            elif len(t) > MAX_CHARS:
                errors[i] = f'Text has {len(t)} characters; the synchronous limit is {MAX_CHARS}'
        todo = [i for i, e in enumerate(errors) if e is None]
        for analysis in cfg['analyses']:
            kind = ANALYSES[analysis]
            for start in range(0, len(todo), BATCH[kind]):
                ctx.check_cancelled()
                ids = todo[start:start + BATCH[kind]]
                docs = [{'id': str(i), 'text': texts[i], **({} if kind == 'LanguageDetection' else {'language': cfg['language']})}
                        for i in ids]
                doc = api.request('POST', 'language/:analyze-text?api-version=2023-04-01',
                                  {'kind': kind, 'analysisInput': {'documents': docs}, 'parameters': {}})
                res = (doc or {}).get('results', {})
                for e in res.get('errors', []):
                    i = int(e['id'])
                    errors[i] = errors[i] or f"{e.get('error', {}).get('code', '')}: {e.get('error', {}).get('message', '')}"
                for d in res.get('documents', []):
                    i = int(d['id'])
                    if kind == 'SentimentAnalysis':
                        s = d.get('confidenceScores', {})
                        results[i]['sentiment'] = [d.get('sentiment'), s.get('positive'), s.get('neutral'), s.get('negative')]
                    elif kind == 'KeyPhraseExtraction':
                        results[i]['key_phrases'] = ['; '.join(d.get('keyPhrases', []))]
                    elif kind == 'EntityRecognition':
                        results[i]['entities'] = [json.dumps([{k: e.get(k) for k in ('text', 'category', 'subcategory', 'offset',
                                                                                     'length', 'confidenceScore') if k in e}
                                                              for e in d.get('entities', [])], ensure_ascii=False)]
                    else:
                        lang = d.get('detectedLanguage', {})
                        results[i]['language'] = [lang.get('name'), lang.get('iso6391Name'), lang.get('confidenceScore')]
        rows = []
        for row, res, err in zip(table.rows(), results, errors):
            extra = []
            for a in cfg['analyses']:
                extra.extend(res.get(a, [None] * len(OUT_FIELDS[a])))
            rows.append([*row, *extra, err])
        ctx.info(f'analysed {len(todo):,} texts; {sum(e is not None for e in errors):,} with errors')
        return {'output': Table.from_rows(schema, rows)}


# --------------------------------------------------------------------------- Power Automate

RESULT_FIELDS = [Field('StatusCode', FieldType.INT32), _text('Response')]


class PowerAutomate(Tool):
    manifest = ToolManifest(
        tool_id='core.power_automate', version='1.0.0', display_name='Power Automate', catalog_ids=('connectors:power-automate',),
        inputs=(Anchor('input', 'I', description='Records to send to the flow'),),
        outputs=(Anchor('output', 'O', description='One record per call with the status and response'),),
        side_effects='network', deterministic=False,
        description='Trigger a Power Automate flow through its HTTP request trigger: each record as its own JSON request '
                    '(per_record) or all records as one JSON array (batch). Returns the HTTP status and response of each call.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id'], 'properties': {
            'provider': {'type': 'string', 'enum': ['power-automate']}, 'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                              'description': 'Saved trigger connection (Signed URL, or OAuth for flows that require sign-in)'},
            'mode': {'enum': ['per_record', 'batch'], 'default': 'per_record'},
            'fail_on_error': {'type': 'boolean', 'default': True},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        if cfg['mode'] == 'batch':
            return {'output': Schema(list(RESULT_FIELDS))}
        names = {f.name for f in RESULT_FIELDS}
        return {'output': Schema([*[f for f in inputs['input'] if f.name not in names], *RESULT_FIELDS])}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        from ..service_connections import ServiceConnectionStore
        providers.resolve(cfg, 'power-automate')
        table: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _permit(ctx, writes=True)
        store = ServiceConnectionStore()
        conn = store.get(cfg['connection_id'], kind='web')
        if conn.auth == 'signed-url':
            url = f"{conn.base_url}?{store.secret(conn).lstrip('?')}"
            headers = {}
        elif conn.auth == 'oauth2':
            url, headers = conn.base_url, conn.authorize(conn.base_url, store.secret(conn))
        else:
            raise ConfigError('A Power Automate trigger connection uses a signed URL or OAuth')
        opener = urllib.request.build_opener(_HttpOnlyRedirects(conn))
        records = [{k: (v if v is None or isinstance(v, (bool, int, float, str)) else format_value(v)) for k, v in r.items()}
                   for r in table.to_dicts()]
        bodies = [records] if cfg['mode'] == 'batch' else records
        out, failures = [], 0
        for rec, body in zip([None] if cfg['mode'] == 'batch' else table.rows(), bodies):
            ctx.check_cancelled()
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                         headers={'Content-Type': 'application/json', **headers})
            try:
                with opener.open(req, timeout=cfg['timeout_seconds']) as r:
                    status, text = r.status, r.read(1 << 20).decode('utf-8', 'replace')
            except urllib.error.HTTPError as e:
                with e:
                    status, text = e.code, _graph_error(e.read(1 << 20))
            except (urllib.error.URLError, OSError) as e:
                raise DataError(f'Could not reach the flow trigger: {getattr(e, "reason", None) or e}') from None
            failures += status >= 300
            names = {f.name for f in RESULT_FIELDS}
            base = [] if rec is None else [v for f, v in zip(table.schema, rec) if f.name not in names]
            out.append([*base, status, text or None])
        if failures and cfg['fail_on_error']:
            raise DataError(f'{failures} of {len(out)} flow call(s) failed; the first returned {next(r[-2] for r in out if r[-2] >= 300)}')
        ctx.info(f'{len(out):,} flow call(s); {failures} failed')
        return {'output': Table.from_rows(schema, out)}


TOOLS = [TextAnalytics, PowerAutomate]
