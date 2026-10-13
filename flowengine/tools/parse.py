"""Parse category kernels: Text To Columns, RegEx."""
from __future__ import annotations

import re

from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, format_value
from ._common import require_fields

TEXT = 2**30 - 1


def _text(v):
    return v if v is None or isinstance(v, str) else format_value(v)


class TextToColumns(Tool):
    manifest = ToolManifest(
        tool_id='core.text_to_columns', version='1.0.0', display_name='Text To Columns',
        catalog_ids=('parse:text-to-columns',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Split a text field on any of the delimiter characters into N columns or into rows.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['field', 'delimiters', 'mode'],
            'properties': {
                'field': {'type': 'string'},
                'delimiters': {'type': 'string', 'minLength': 1,
                               'description': r'Each character is a delimiter; \t means tab'},
                'mode': {'enum': ['columns', 'rows']},
                'columns': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 2},
                'extra': {'enum': ['last_column', 'drop', 'error'], 'default': 'last_column',
                          'description': 'Columns mode: keep remainder in last column, drop it, or fail'},
                'output_root': {'type': ['string', 'null'], 'default': None},
                'skip_empty': {'type': 'boolean', 'default': False},
                'quote_aware': {'type': 'boolean', 'default': False,
                                'description': 'Ignore delimiters inside double quotes'},
            },
        })

    def _split(self, s, delims, quote_aware, maxsplit=None):
        if s is None:
            return []
        if not quote_aware:
            parts = re.split('[' + re.escape(delims) + ']', s, maxsplit=maxsplit or 0)
            return parts
        parts, cur, inq = [], [], False
        for ch in s:
            if ch == '"':
                inq = not inq
                cur.append(ch)
            elif ch in delims and not inq and (maxsplit is None or len(parts) < maxsplit):
                parts.append(''.join(cur))
                cur = []
            else:
                cur.append(ch)
        parts.append(''.join(cur))
        return parts

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        delims = cfg['delimiters'].replace('\\t', '\t').replace('\\n', '\n')
        col = t.column(cfg['field'])
        root = cfg['output_root'] or cfg['field']
        if cfg['mode'] == 'columns':
            n = cfg['columns']
            names = [f'{root}{i + 1}' for i in range(n)]
            clash = [x for x in names if x in t.schema]
            if clash:
                raise SchemaError(f'Output fields already exist: {clash}')
            outs = [[] for _ in range(n)]
            for v in col:
                v = _text(v)
                if v is None:
                    parts = [None] * n
                else:
                    parts = self._split(v, delims, cfg['quote_aware'],
                                        maxsplit=(n - 1) if cfg['extra'] == 'last_column' else None)
                    if cfg['skip_empty']:
                        parts = [p for p in parts if p != '']
                    if len(parts) > n:
                        if cfg['extra'] == 'error':
                            raise DataError(f'Value split into {len(parts)} parts; only {n} columns configured')
                        parts = parts[:n]
                    parts += [None] * (n - len(parts))
                for o, p in zip(outs, parts):
                    o.append(p)
            out = t
            for name, values in zip(names, outs):
                out = out.with_column(Field(name, FieldType.V_WSTRING, TEXT), values)
            return {'output': out}
        idx = t.schema.index(cfg['field'])
        fields = list(t.schema)
        fields[idx] = Field(cfg['field'], FieldType.V_WSTRING, TEXT)
        rows = []
        for row in t.rows():
            v = _text(row[idx])
            parts = [None] if v is None else self._split(v, delims, cfg['quote_aware'])
            if cfg['skip_empty']:
                parts = [p for p in parts if p != ''] or []
            for p in parts:
                rows.append(row[:idx] + (p,) + row[idx + 1:])
        return {'output': Table.from_rows(fields, rows)}


class RegEx(Tool):
    manifest = ToolManifest(
        tool_id='core.regex', version='1.0.0', display_name='RegEx',
        catalog_ids=('parse:regex',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Regular expressions (Python "re" dialect) to replace, match, parse groups into fields or '
                    'tokenize into columns/rows. Dialect differences from other engines are documented.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['field', 'pattern', 'mode'],
            'properties': {
                'field': {'type': 'string'}, 'pattern': {'type': 'string', 'minLength': 1},
                'mode': {'enum': ['replace', 'match', 'parse', 'tokenize_columns', 'tokenize_rows']},
                'case_insensitive': {'type': 'boolean', 'default': False},
                'replacement': {'type': 'string', 'default': '',
                                'description': 'Replace mode: $1..$n refer to capture groups'},
                'output_field': {'type': ['string', 'null'], 'default': None},
                'group_fields': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
                'columns': {'type': 'integer', 'minimum': 1, 'default': 3},
                'keep_unmatched': {'type': 'boolean', 'default': True,
                                   'description': 'Match mode: keep records that do not match (with False)'},
            },
        })

    def check_config(self, cfg):
        try:
            rx = re.compile(cfg['pattern'], re.I if cfg['case_insensitive'] else 0)
        except re.error as e:
            raise ConfigError(f'Invalid regular expression: {e}') from None
        if cfg['mode'] == 'parse':
            names = cfg.get('group_fields') or [f'RegExOut{i + 1}' for i in range(rx.groups)]
            if rx.groups == 0:
                raise ConfigError('Parse mode needs at least one capture group')
            if len(names) != rx.groups:
                raise ConfigError(f'{rx.groups} capture group(s) but {len(names)} group_fields')
            cfg['group_fields'] = names

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        rx = re.compile(cfg['pattern'], re.I if cfg['case_insensitive'] else 0)
        col = [_text(v) for v in t.column(cfg['field'])]
        mode = cfg['mode']
        text_field = lambda n: Field(n, FieldType.V_WSTRING, TEXT)
        if mode == 'replace':
            repl = re.sub(r'\$(\d+)', r'\\g<\1>', cfg['replacement'])
            vals = [None if v is None else rx.sub(repl, v) for v in col]
            name = cfg['output_field'] or cfg['field']
            target = text_field(name) if name != cfg['field'] or not t.schema.field(name).type.is_string \
                else t.schema.field(name)
            return {'output': t.with_column(target, vals)}
        if mode == 'match':
            name = cfg['output_field'] or f"{cfg['field']}_Matched"
            if name in t.schema:
                raise SchemaError(f'Field {name!r} already exists')
            vals = [None if v is None else rx.fullmatch(v) is not None for v in col]  # [ref:regex-match]
            out = t.with_column(Field(name, FieldType.BOOL), vals)
            if not cfg['keep_unmatched']:
                out = out.take([i for i, m in enumerate(vals) if m])
            return {'output': out}
        if mode == 'parse':
            names = cfg['group_fields']
            clash = [n for n in names if n in t.schema]
            if clash:
                raise SchemaError(f'Output fields already exist: {clash}')
            outs = [[] for _ in names]
            for v in col:
                m = rx.search(v) if v is not None else None
                for g, o in enumerate(outs):
                    o.append(m.group(g + 1) if m else None)
            out = t
            for n, values in zip(names, outs):
                out = out.with_column(text_field(n), values)
            return {'output': out}

        def tokens(v):
            if v is None:
                return []
            return [m.group(1) if rx.groups else m.group(0) for m in rx.finditer(v)]
        root = cfg['output_field'] or cfg['field']
        if mode == 'tokenize_columns':
            n = cfg['columns']
            names = [f'{root}{i + 1}' for i in range(n)]
            clash = [x for x in names if x in t.schema]
            if clash:
                raise SchemaError(f'Output fields already exist: {clash}')
            outs = [[] for _ in range(n)]
            for v in col:
                toks = tokens(v)[:n]
                toks += [None] * (n - len(toks))
                for o, tk in zip(outs, toks):
                    o.append(tk)
            out = t
            for name, values in zip(names, outs):
                out = out.with_column(text_field(name), values)
            return {'output': out}
        idx = t.schema.index(cfg['field'])
        fields = list(t.schema)
        fields[idx] = text_field(cfg['field'])
        rows = []
        for row, v in zip(t.rows(), col):
            for tk in tokens(v):
                rows.append(row[:idx] + (tk,) + row[idx + 1:])
        return {'output': Table.from_rows(fields, rows)}


TOOLS = [TextToColumns, RegEx]
