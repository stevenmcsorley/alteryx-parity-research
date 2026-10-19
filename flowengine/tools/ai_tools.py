"""AI Tools: LLM Override, Prompt, Precision Match, Schema Fit, Invoice Extractor and Synthetic Input; no vendor code.

Functional source: the public AI Tools help. The reference routes AI tools through an Alteryx One LLM connection;
Flowbench uses the user's own model provider through a saved connection (flowengine/llm.py): an OpenAI-compatible
server (OpenAI, Ollama, LM Studio, vLLM, ...) or Anthropic. LLM Override passes the provider, connection id and model
(never a key) to the other tools' model input.

Model output is checked, not trusted: structured answers are validated against their JSON schema, Precision Match
can be held to a reference list, and the Python that Schema Fit asks the model for runs only in the isolated user-code
process, with the run's process permission.
"""
from __future__ import annotations

import json
import re

from ..errors import ConfigError, DataError, EngineError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, cast_value, format_value, type_for_values
from ._common import require_fields

TEXT = 2**30 - 1
_PLACEHOLDER = re.compile(r'\[([^\[\]]+)\]')


def _t(name):
    return Field(name, FieldType.V_WSTRING, TEXT)


_MODEL = {
    'provider': {'type': ['string', 'null'], 'enum': ['openai-compatible', 'anthropic', None], 'default': None},
    'provider_semantics': {'type': 'string'},
    'connection_id': {'type': ['string', 'null'], 'default': None, 'x-flowbench-connection': 'web',
                      'description': 'Model service connection (used when no LLM Override is connected)'},
    'model': {'type': ['string', 'null'], 'default': None},
    'temperature': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 2, 'default': None},
    'max_tokens': {'type': ['integer', 'null'], 'minimum': 1, 'default': None},
    'top_p': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 1, 'default': None},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1800, 'default': 180},
}
_M_IN = Anchor('model', 'M', required=False, description='Model settings from LLM Override')


class _AiTool(Tool):
    def effects(self, cfg):
        return {'network_requests'}


def _model(cfg, inputs):
    from ..llm import model_from
    return model_from(cfg, inputs)


def _typed_table(records, names):
    fields, cols = [], []
    for n in names:
        values = [r.get(n) for r in records]
        values = [json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for v in values]
        t = type_for_values(values)
        f = Field(n, t, TEXT if t.is_string else None)
        fields.append(f)
        cols.append([cast_value(v, f)[0] for v in values])
    return Table(Schema(fields), cols) if fields else Table([], [])


# --------------------------------------------------------------------------- LLM Override

MODEL_RECORD = Schema([_t('Provider'), _t('ConnectionId'), _t('Model'), Field('Temperature', FieldType.DOUBLE),
                       Field('MaxTokens', FieldType.INT64), Field('TopP', FieldType.DOUBLE)])


class LlmOverride(Tool):
    manifest = ToolManifest(
        tool_id='core.llm_override', version='1.0.0', display_name='LLM Override', catalog_ids=('ai-tools:llm-override',),
        inputs=(), outputs=(Anchor('model', 'M', description='Model settings for AI tools'),),
        description='Choose the model provider, saved connection and model (and generation settings) for the AI tools '
                    'connected to its M output. Passes identifiers only, never keys.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'model'], 'properties': {
            **_MODEL, 'provider': {'type': 'string', 'enum': ['openai-compatible', 'anthropic']},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web'},
            'model': {'type': 'string', 'minLength': 1}}})

    def infer(self, ctx, cfg, inputs):
        return {'model': MODEL_RECORD}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve({'provider': cfg['provider']}, 'llm')
        return {'model': Table.from_rows(MODEL_RECORD, [(cfg['provider'], cfg['connection_id'], cfg['model'], cfg['temperature'],
                                                         cfg['max_tokens'], cfg['top_p'])])}


# --------------------------------------------------------------------------- Prompt

def fill(template: str, record: dict, where='') -> str:
    def sub(m):
        name = m.group(1)
        if name not in record:
            raise ConfigError(f'{where}The prompt refers to [{name}], which is not a field of the data input')
        v = record[name]
        return '' if v is None else (v.decode('utf-8', 'replace') if isinstance(v, bytes) else format_value(v))
    return _PLACEHOLDER.sub(sub, template)


class Prompt(_AiTool):
    manifest = ToolManifest(
        tool_id='core.prompt', version='1.0.0', display_name='Prompt', catalog_ids=('ai-tools:prompt',),
        inputs=(_M_IN, Anchor('data', 'D', required=False, description='One prompt per record; [Field] inserts a value')),
        outputs=(Anchor('output', 'O', description='The records with the prompt and the response'),),
        side_effects='network', deterministic=False,
        description='Send a prompt to a language model for each record ([Field] inserts the record’s value), optionally '
                    'with image or PDF attachments and a JSON schema the answer must match. Adds the prompt and the response.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['prompt'], 'properties': {**_MODEL,
            'prompt': {'type': 'string', 'minLength': 1},
            'system': {'type': ['string', 'null'], 'default': None},
            'response_field': {'type': 'string', 'minLength': 1, 'default': 'LLM_Response'},
            'prompt_field': {'type': 'string', 'minLength': 1, 'default': 'LLM_Prompt'},
            'attachment_fields': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            'schema_field': {'type': ['string', 'null'], 'default': None, 'description': 'Field holding a JSON schema for the answer'},
            'schema': {'type': ['object', 'null'], 'default': None, 'description': 'Or a JSON schema here'},
            'on_invalid': {'enum': ['null', 'keep', 'error'], 'default': 'null', 'description': 'When an answer does not match the schema'},
            'on_error': {'enum': ['error', 'warning', 'ignore'], 'default': 'error'}}})

    def infer(self, ctx, cfg, inputs):
        data = inputs.get('data')
        base = list(data) if data is not None else []
        if data is not None:
            require_fields(data, [*cfg['attachment_fields'], *([cfg['schema_field']] if cfg['schema_field'] else [])])
        clash = [n for n in (cfg['prompt_field'], cfg['response_field']) if n in [f.name for f in base]]
        if clash:
            raise ConfigError(f'Field(s) {clash} already exist; rename the prompt or response field')
        return {'output': Schema([*base, _t(cfg['prompt_field']), _t(cfg['response_field'])])}

    def run(self, ctx, cfg, inputs):
        from .github import _permit
        data: Table | None = inputs.get('data')
        schema = self.infer(ctx, cfg, {'data': data.schema if data is not None else None})['output']
        if data is None and (cfg['attachment_fields'] or cfg['schema_field']):
            raise ConfigError('Attachment and schema fields need the data input')
        _permit(ctx)
        model = _model(cfg, inputs)
        records = data.to_dicts() if data is not None else [{}]
        rows = []
        for i, rec in enumerate(records, start=1):
            ctx.check_cancelled()
            text = fill(cfg['prompt'], rec, f'Record {i}: ')
            answer = None
            try:
                attachments = [rec[f] for f in cfg['attachment_fields'] if rec.get(f) is not None]
                if any(not isinstance(a, bytes) for a in attachments):
                    raise ConfigError('Attachment fields must be Blob fields (use Blob Input to read files)')
                json_schema = cfg['schema'] or (json.loads(rec[cfg['schema_field']]) if cfg['schema_field'] and rec.get(cfg['schema_field']) else None)
                if json_schema is None:
                    answer = model.complete(text, system=cfg['system'], attachments=attachments)
                else:
                    try:
                        answer = json.dumps(model.complete_json(text, json_schema, system=cfg['system'], attachments=attachments),
                                            ensure_ascii=False)
                    except DataError as e:
                        if cfg['on_invalid'] == 'error':
                            raise
                        ctx.warn(f'Record {i}: {e}')
                        answer = model.complete(text, system=cfg['system'], attachments=attachments) if cfg['on_invalid'] == 'keep' else None
            except (DataError, ValueError) as e:
                if cfg['on_error'] == 'error':
                    raise DataError(f'Record {i}: {e}') from None
                if cfg['on_error'] == 'warning':
                    ctx.warn(f'Record {i}: {e}')
            base = [] if data is None else [rec[f.name] for f in data.schema]
            rows.append([*base, text, answer])
        ctx.info(f'{model.calls} model request(s) to {model.spec["model"]}')
        return {'output': Table.from_rows(schema, rows)}


# --------------------------------------------------------------------------- Precision Match

MAPPING = {'type': 'object', 'required': ['mapping'], 'properties': {'mapping': {'type': 'array', 'items': {
    'type': 'object', 'required': ['original', 'standard'], 'properties': {'original': {'type': 'string'}, 'standard': {'type': 'string'}}}}}}


class PrecisionMatch(_AiTool):
    manifest = ToolManifest(
        tool_id='core.precision_match', version='1.0.0', display_name='Precision Match', catalog_ids=('ai-tools:precision-match',),
        inputs=(_M_IN, Anchor('data', 'D', description='Records with the field to standardise'),
                Anchor('reference', 'R', required=False, description='Preferred standard values')),
        outputs=(Anchor('output', 'D', description='Records with the standardised values'),
                 Anchor('mapping', 'M', description='Original value → standard value')),
        side_effects='network', deterministic=False,
        description='Standardise spelling variants of a categorical field (names, places, …) to one canonical value per '
                    'meaning with a language model, optionally only to the values of a reference list. Each distinct value '
                    'is sent once; the mapping table is returned too.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {**_MODEL,
            'field': {'type': 'string', 'minLength': 1},
            'reference_field': {'type': ['string', 'null'], 'default': None},
            'strict': {'type': 'boolean', 'default': True, 'description': 'Only use reference values (unmatched values stay unchanged)'},
            'output_mode': {'enum': ['replace', 'append'], 'default': 'append'},
            'output_field': {'type': 'string', 'default': 'Standardized'},
            'max_categories': {'type': 'integer', 'minimum': 1, 'default': 2000},
            'batch_size': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 100}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['data']
        require_fields(schema, [cfg['field']])
        if cfg['output_mode'] == 'append' and cfg['output_field'] in schema:
            raise ConfigError(f"Field {cfg['output_field']!r} already exists")
        out = list(schema) if cfg['output_mode'] == 'replace' else [*schema, _t(cfg['output_field'])]
        return {'output': Schema(out), 'mapping': Schema([_t('Original'), _t('Standard')])}

    def run(self, ctx, cfg, inputs):
        from .github import _permit
        table: Table = inputs['data']
        schema = self.infer(ctx, cfg, {'data': table.schema})
        values = table.column(cfg['field'])
        distinct = sorted({format_value(v) for v in values if v is not None and format_value(v).strip()})
        if len(distinct) > cfg['max_categories']:
            raise DataError(f'{len(distinct)} distinct values exceed max_categories ({cfg["max_categories"]}); raise it to send them')
        reference = []
        ref_table = inputs.get('reference')
        if ref_table is not None:
            if not cfg['reference_field']:
                raise ConfigError('Name the reference_field of the reference input')
            require_fields(ref_table.schema, [cfg['reference_field']])
            reference = sorted({format_value(v) for v in ref_table.column(cfg['reference_field']) if v is not None})
        _permit(ctx)
        model = _model(cfg, inputs)
        mapping = {}
        for start in range(0, len(distinct), cfg['batch_size']):
            ctx.check_cancelled()
            batch = distinct[start:start + cfg['batch_size']]
            rule = (f'Map each value to the one of these standard values with the same meaning: {json.dumps(reference, ensure_ascii=False)}. '
                    + ('If none has the same meaning, return the value unchanged.' if cfg['strict'] else 'If none fits, choose the best standard form.')
                    if reference else 'Group values that mean the same thing (spelling variants, abbreviations, case) and map each to the '
                                      'clearest standard form; keep values that are already distinct.')
            answer = model.complete_json(f'Standardise the values of the field "{cfg["field"]}". {rule}\nReturn every input value '
                                         f'once as "original".\nValues: {json.dumps(batch, ensure_ascii=False)}', MAPPING)
            for m in answer['mapping']:
                if m['original'] in batch:
                    std = m['standard']
                    if reference and cfg['strict'] and std not in reference:
                        std = m['original']
                    mapping[m['original']] = std
        missing = [v for v in distinct if v not in mapping]
        if missing:
            ctx.warn(f'The model left {len(missing)} value(s) unmapped; they are kept unchanged')
        rows = []
        idx = table.schema.names.index(cfg['field'])
        for row, v in zip(table.rows(), values):
            std = mapping.get(format_value(v), format_value(v)) if v is not None else None
            if cfg['output_mode'] == 'replace':
                row = list(row)
                row[idx] = std
                rows.append(row)
            else:
                rows.append([*row, std])
        return {'output': Table.from_rows(schema['output'], rows),
                'mapping': Table.from_rows(schema['mapping'], sorted((k, v) for k, v in mapping.items()))}


# --------------------------------------------------------------------------- Synthetic Input

class SyntheticInput(_AiTool):
    manifest = ToolManifest(
        tool_id='core.synthetic_input', version='1.0.0', display_name='Synthetic Input', catalog_ids=('ai-tools:synthetic-input',),
        inputs=(_M_IN,), outputs=(Anchor('output', 'D', description='Generated records'),),
        side_effects='network', deterministic=False,
        description='Generate realistic sample records with a language model from a use-case description and column names '
                    '(in batches), to build workflows before real data is available. The data is invented.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['use_case', 'columns'], 'properties': {**_MODEL,
            'use_case': {'type': 'string', 'minLength': 3},
            'columns': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1}},
            'rows': {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 20},
            'batch_size': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 25}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .github import _permit
        _permit(ctx)
        model = _model(cfg, inputs)
        records = []
        while len(records) < cfg['rows']:
            ctx.check_cancelled()
            n = min(cfg['batch_size'], cfg['rows'] - len(records))
            row_schema = {'type': 'object', 'required': cfg['columns'],
                          'properties': {c: {'type': ['string', 'number', 'integer', 'boolean', 'null']} for c in cfg['columns']}}
            schema = {'type': 'object', 'required': ['rows'], 'properties': {'rows': {'type': 'array', 'items': row_schema}}}
            answer = model.complete_json(f'Generate {n} varied, realistic, fictional records for this use case: {cfg["use_case"]}\n'
                                         f'Columns: {json.dumps(cfg["columns"])}. Do not use real people\'s personal data.'
                                         + (f' Continue after the {len(records)} records already made; do not repeat them.' if records else ''),
                                         schema)
            got = [{c: r.get(c) for c in cfg['columns']} for r in answer['rows']][:n]
            if not got:
                raise DataError('The model returned no records')
            records.extend(got)
        ctx.info(f'generated {len(records)} synthetic records in {model.calls} request(s)')
        return {'output': _typed_table(records, cfg['columns'])}


# --------------------------------------------------------------------------- Invoice Extractor

DEFAULT_METADATA = ['InvoiceNumber', 'InvoiceDate', 'DueDate', 'VendorName', 'CustomerName', 'Currency', 'Subtotal', 'Tax', 'Total']
DEFAULT_LINES = ['Description', 'Quantity', 'UnitPrice', 'Amount']


class InvoiceExtractor(_AiTool):
    manifest = ToolManifest(
        tool_id='core.invoice_extractor', version='1.0.0', display_name='Invoice Extractor', catalog_ids=('ai-tools:invoice-extractor',),
        inputs=(_M_IN, Anchor('data', 'D', description='Records with an invoice image or PDF in a Blob field')),
        outputs=(Anchor('output', 'D', description='One record per invoice line item'),
                 Anchor('metadata', 'M', description='One record per invoice with its header fields')),
        side_effects='network', deterministic=False,
        description='Extract invoice header fields and line items from invoice images or PDFs with a multimodal model. '
                    'Fields can be named; dates are returned in the chosen format or as on the invoice. Check results before use.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {**_MODEL,
            'field': {'type': 'string', 'minLength': 1, 'description': 'Blob field holding the invoice'},
            'metadata_fields': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            'line_item_fields': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            'date_format': {'type': ['string', 'null'], 'default': None, 'description': 'For example YYYY-MM-DD; empty keeps the invoice format'},
            'extract_signature': {'type': 'boolean', 'default': False}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['data'], [cfg['field']])
        return None

    def run(self, ctx, cfg, inputs):
        from .github import _permit
        table: Table = inputs['data']
        self.infer(ctx, cfg, {'data': table.schema})
        meta_fields = cfg['metadata_fields'] or DEFAULT_METADATA
        if cfg['extract_signature']:
            meta_fields = [*meta_fields, 'SignaturePresent', 'SignedBy']
        line_fields = cfg['line_item_fields'] or DEFAULT_LINES
        scalar = {'type': ['string', 'number', 'boolean', 'null']}
        schema = {'type': 'object', 'required': ['metadata', 'line_items'], 'properties': {
            'metadata': {'type': 'object', 'properties': {f: scalar for f in meta_fields}},
            'line_items': {'type': 'array', 'items': {'type': 'object', 'properties': {f: scalar for f in line_fields}}}}}
        _permit(ctx)
        model = _model(cfg, inputs)
        metas, lines = [], []
        for i, blob in enumerate(table.column(cfg['field']), start=1):
            ctx.check_cancelled()
            if blob is None:
                continue
            if not isinstance(blob, bytes):
                raise ConfigError(f"{cfg['field']} must be a Blob field (use Blob Input to read invoice files)")
            dates = f' Write dates as {cfg["date_format"]}.' if cfg['date_format'] else ' Write dates as they appear.'
            answer = model.complete_json(
                f'Extract this invoice. Header fields: {", ".join(meta_fields)}. Line item fields: {", ".join(line_fields)}.'
                f'{dates} Use null for anything not on the invoice; do not guess. Numbers without currency symbols.',
                schema, attachments=[blob])
            metas.append({'Invoice': i, **{f: answer['metadata'].get(f) for f in meta_fields}})
            lines += [{'Invoice': i, 'Line': n, **{f: item.get(f) for f in line_fields}} for n, item in enumerate(answer['line_items'], start=1)]
        ctx.info(f'{len(metas)} invoice(s), {len(lines)} line item(s)')
        return {'output': _typed_table(lines, ['Invoice', 'Line', *line_fields]),
                'metadata': _typed_table(metas, ['Invoice', *meta_fields])}


# --------------------------------------------------------------------------- Schema Fit

STEPS = {'type': 'object', 'required': ['steps'], 'properties': {'steps': {'type': 'array', 'items': {
    'type': 'object', 'required': ['description', 'code'], 'properties': {'description': {'type': 'string'}, 'code': {'type': 'string'}}}}}}


class SchemaFit(_AiTool):
    manifest = ToolManifest(
        tool_id='core.schema_fit', version='1.0.0', display_name='Schema Fit', catalog_ids=('ai-tools:schema-fit',),
        inputs=(_M_IN, Anchor('target', 'T', description='Correctly formatted target data'),
                Anchor('source', 'S', description='Data to transform into the target format')),
        outputs=(Anchor('steps', 'T', description='Each transformation step with its Python code'),
                 Anchor('output', 'D', description='The source data in the target format')),
        side_effects='process', deterministic=False,
        description='Ask a language model for pandas steps that reshape the source data into the target data’s columns and '
                    'formats, run them in the isolated Python process (needs the process permission), and retry with the '
                    'error when a step fails. Returns the steps and code for review, and the transformed data.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {**_MODEL,
            'max_transformations': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 3,
                                    'description': 'Maximum model requests (each failure is sent back once)'},
            'only_target_columns': {'type': 'boolean', 'default': True},
            'environment': {'type': 'string', 'default': 'default'}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return {'steps': Schema([Field('Step', FieldType.INT32), _t('Transformation Step'), _t('Code')])}

    def run(self, ctx, cfg, inputs):
        from .developer import PythonTool, _require_processes
        from .github import _permit
        _require_processes(ctx, 'Schema Fit')
        _permit(ctx)
        target, source = inputs['target'], inputs['source']
        model = _model(cfg, inputs)

        def describe(t):
            sample = [{k: format_value(v) if v is not None else None for k, v in r.items()} for r in t.to_dicts()[:5]]
            return json.dumps({'columns': [{'name': f.name, 'type': f.type.value} for f in t.schema], 'sample_rows': sample},
                              ensure_ascii=False)
        ask = (f'Write pandas code that transforms a DataFrame named df (the source) so it has exactly the target columns, '
               f'names, order and value formats. Use only pandas, numpy, re and datetime. Split the work into short steps; each '
               f'step is code that reads and assigns df.\nTARGET: {describe(target)}\nSOURCE: {describe(source)}')
        error = None
        for attempt in range(cfg['max_transformations']):
            ctx.check_cancelled()
            prompt = ask if error is None else f'{ask}\nYour previous code failed with: {error}\nReturn corrected steps.'
            steps = model.complete_json(prompt, STEPS)['steps']
            code = '\n'.join(['from flowbench_io import read_input, write_output', 'import pandas as pd, numpy as np, re, datetime',
                              'df = read_input(1, as_frame=True)', *[s['code'] for s in steps]]
                             + ([f'df = df[[c for c in {target.schema.names!r} if c in df.columns]]'] if cfg['only_target_columns'] else [])
                             + ['write_output(1, df)'])
            try:
                result = PythonTool().run(ctx, {'code': code, 'environment': cfg['environment'], 'timeout_seconds': 300},
                                          {'inputs': [source]})['output1']
            except EngineError as e:
                error = str(e)[-1500:]
                ctx.warn(f'Schema Fit attempt {attempt + 1} failed: {error[:300]}')
                continue
            missing = [n for n in target.schema.names if n not in result.schema.names]
            if missing and attempt + 1 < cfg['max_transformations']:
                error = f'the result lacks the target columns {missing}'
                continue
            step_table = Table.from_rows(self.infer(ctx, cfg, {})['steps'], [(i, s['description'], s['code']) for i, s in enumerate(steps, 1)])
            if missing:
                ctx.warn(f'The transformed data lacks target columns {missing}')
            return {'steps': step_table, 'output': result}
        raise DataError(f"Schema Fit found no working transformation in {cfg['max_transformations']} attempt(s): {error}")


TOOLS = [LlmOverride, Prompt, PrecisionMatch, SyntheticInput, InvoiceExtractor, SchemaFit]
