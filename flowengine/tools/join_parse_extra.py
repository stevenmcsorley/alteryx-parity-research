"""Join / Parse / Transform kernels, wave 2: Find Replace, Join Multiple, Make Group,
DateTime, XML Parse, Make Columns, Weighted Average."""
from __future__ import annotations

import datetime as _dt
import math
import re
import xml.etree.ElementTree as ET

from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value, unify_fields, cast_value, CastFailure
from ._common import require_fields

TEXT = 2**30 - 1


def _text(v):
    return v if v is None or isinstance(v, str) else format_value(v)


# =========================================================================== Find Replace

class FindReplace(Tool):
    manifest = ToolManifest(
        tool_id='core.find_replace', version='1.0.0', display_name='Find Replace', catalog_ids=('join:find-replace',),
        inputs=(Anchor('find', 'F', description='Records to search'), Anchor('replace', 'R', description='Lookup table')),
        outputs=(Anchor('output', 'O'),),
        description='Search a field of the F input for values from a lookup field of the R input (whole value, '
                    'beginning, or any part) and either replace the found text with a value from R or append R fields.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['find_field', 'search_field', 'mode'], 'properties': {
            'find_field': {'type': 'string', 'description': 'Field of the F input to search in'},
            'search_field': {'type': 'string', 'description': 'Field of the R input containing the values to find'},
            'match': {'enum': ['entire', 'beginning', 'any'], 'default': 'any'},
            'case_insensitive': {'type': 'boolean', 'default': False},
            'whole_word': {'type': 'boolean', 'default': False},
            'mode': {'enum': ['replace', 'append']},
            'replace_field': {'type': 'string', 'description': 'Replace mode: field of R holding the replacement text'},
            'replace_all': {'type': 'boolean', 'default': True, 'description': 'Replace every match, not only the first'},
            'append_fields': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
        }})

    def check_config(self, cfg):
        if cfg['mode'] == 'replace' and not cfg.get('replace_field'):
            raise ConfigError('Replace mode needs replace_field')

    def run(self, ctx, cfg, inputs):
        ft: Table = inputs['find']
        rt: Table = inputs['replace']
        require_fields(ft.schema, [cfg['find_field']], 'find field')
        need = [cfg['search_field']] + ([cfg['replace_field']] if cfg['mode'] == 'replace' else cfg['append_fields'])
        require_fields(rt.schema, need, 'replace-table field')
        flags = re.I if cfg['case_insensitive'] else 0
        lookups = []
        for i, v in enumerate(rt.column(cfg['search_field'])):
            v = _text(v)
            if not v:
                continue
            body = re.escape(v)
            if cfg['whole_word']:
                body = rf'\b{body}\b'
            pat = {'entire': f'^{body}$', 'beginning': f'^{body}', 'any': body}[cfg['match']]
            lookups.append((re.compile(pat, flags), i))
        col = ft.column(cfg['find_field'])
        if cfg['mode'] == 'replace':
            rep = rt.column(cfg['replace_field'])
            out = []
            for v in col:
                s = _text(v)
                if s is None:
                    out.append(None)
                    continue
                for rx, i in lookups:
                    if rx.search(s):
                        s = rx.sub(lambda m, r=_text(rep[i]) or '': r, s, count=0 if cfg['replace_all'] else 1)
                        if not cfg['replace_all']:
                            break
                out.append(s)
            f = ft.schema.field(cfg['find_field'])
            target = f if f.type.is_string else Field(f.name, FieldType.V_WSTRING, TEXT)
            if target.type.is_string and target.size is not None:
                longest = max((len(x) for x in out if x is not None), default=0)
                if longest > target.size:
                    target = Field(f.name, target.type, longest)
            return {'output': ft.with_column(target, out)}
        names = cfg['append_fields']
        clash = [n for n in names if n in ft.schema]
        if clash:
            raise SchemaError(f'Appended fields already exist in the F input: {clash}')
        cols = [[] for _ in names]
        rcols = [rt.column(n) for n in names]
        for v in col:
            s = _text(v)
            hit = next((i for rx, i in lookups if s is not None and rx.search(s)), None)
            for c, rc in zip(cols, rcols):
                c.append(None if hit is None else rc[hit])
        out = ft
        for n, c in zip(names, cols):
            out = out.with_column(rt.schema.field(n), c)
        return {'output': out}


# =========================================================================== Join Multiple

class JoinMultiple(Tool):
    manifest = ToolManifest(
        tool_id='core.join_multiple', version='1.0.0', display_name='Join Multiple', catalog_ids=('join:join-multiple',),
        inputs=(Anchor('inputs', 'I', multiple=True, description='Two or more streams (#1, #2, ...) in connection order'),),
        outputs=(Anchor('output', 'O'),),
        description='Full outer join of several inputs on one key field each (or by record position). Field names '
                    'appearing in more than one input are prefixed "Input_#n_". Optionally keep only records '
                    'present in every input.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['by'], 'properties': {
            'by': {'enum': ['fields', 'position']},
            'keys': {'type': 'array', 'items': {'type': 'string'},
                     'description': 'Key field for each input, in connection order'},
            'join_only': {'type': 'boolean', 'default': False},
        }})

    def run(self, ctx, cfg, inputs):
        tables: list[Table] = inputs['inputs']
        if len(tables) < 2:
            raise ConfigError('Join Multiple needs at least two inputs')
        counts: dict = {}
        for t in tables:
            for n in t.schema.names:
                counts[n] = counts.get(n, 0) + 1
        fields = []
        for k, t in enumerate(tables):
            for f in t.schema:
                fields.append(f.renamed(f'Input_#{k + 1}_{f.name}') if counts[f.name] > 1 else f)
        Schema(fields)
        if cfg['by'] == 'position':
            n = max(len(t) for t in tables)
            if cfg['join_only']:
                n = min(len(t) for t in tables)
            rows = []
            for i in range(n):
                row = ()
                for t in tables:
                    row += t.row(i) if i < len(t) else (None,) * len(t.schema)
                rows.append(row)
            return {'output': Table.from_rows(fields, rows)}
        keys = cfg.get('keys') or []
        if len(keys) != len(tables):
            raise ConfigError(f'{len(tables)} inputs connected but {len(keys)} key field(s) configured')
        indexes = []
        order: list = []
        seen = set()
        for t, key in zip(tables, keys):
            require_fields(t.schema, [key], 'key field')
            idx: dict = {}
            for i, v in enumerate(t.column(key)):
                idx.setdefault(v, []).append(i)
                if v not in seen:
                    seen.add(v)
                    order.append(v)
            indexes.append(idx)
        rows = []
        for v in order:
            groups = [idx.get(v, []) for idx in indexes]
            if cfg['join_only'] and not all(groups):
                continue
            combos = [[]]
            for t, g in zip(tables, groups):
                choices = [t.row(i) for i in g] or [(None,) * len(t.schema)]
                combos = [c + [r] for c in combos for r in choices]
            for c in combos:
                rows.append(sum((tuple(r) for r in c), ()))
        return {'output': Table.from_rows(fields, rows)}


# =========================================================================== Make Group

class MakeGroup(Tool):
    manifest = ToolManifest(
        tool_id='core.make_group', version='1.0.0', display_name='Make Group', catalog_ids=('join:make-group',),
        inputs=(Anchor('input', 'I', description='Pairs of related keys'),), outputs=(Anchor('output', 'O'),),
        description='Connected components of key pairs: one record per distinct key with its group, where the group '
                    'is the smallest key in the component. Output ordered by group then key.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['key1', 'key2'], 'properties': {
            'key1': {'type': 'string'}, 'key2': {'type': 'string'}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['key1'], cfg['key2']])
        parent: dict = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for a, b in zip(t.column(cfg['key1']), t.column(cfg['key2'])):
            if a is None and b is None:
                continue
            if a is None or b is None:
                find(a if b is None else b)
                continue
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb, key=format_value)] = min(ra, rb, key=format_value)
        members: dict = {}
        for k in parent:
            members.setdefault(find(k), []).append(k)
        rows = []
        for root in sorted(members, key=format_value):
            group = min(members[root], key=format_value)
            for k in sorted(members[root], key=format_value):
                rows.append((group, k))
        f1 = t.schema.field(cfg['key1'])
        f = unify_fields(f1, t.schema.field(cfg['key2']).renamed(f1.name))
        return {'output': Table.from_rows([f.renamed('Group'), f.renamed('Key')],
                                          [tuple(cast_value(v, f)[0] for v in r) for r in rows])}


# =========================================================================== DateTime

_TOKENS = [  # longest first
    ('yyyy', '%Y'), ('yy', '%y'), ('Month', '%B'), ('MMMM', '%B'), ('MMM', '%b'), ('MM', '%m'), ('M', '%m'),
    ('dddd', '%A'), ('day', '%A'), ('ddd', '%a'), ('dd', '%d'), ('d', '%d'),
    ('HH', '%H'), ('hh', '%I'), ('H', '%H'), ('mm', '%M'), ('ss', '%S'), ('ampm', '%p'), ('tt', '%p'),
]
_TIME_CODES = ('%H', '%I', '%M', '%S', '%p')


def translate_format(fmt: str) -> str:
    """Translate a day/month/year pattern (e.g. 'Month dd, yyyy', 'dd/MM/yyyy HH:mm') to strftime codes."""
    out, i = [], 0
    while i < len(fmt):
        for tok, code in _TOKENS:
            if fmt.startswith(tok, i):
                out.append(code)
                i += len(tok)
                break
        else:
            out.append('%%' if fmt[i] == '%' else fmt[i])
            i += 1
    return ''.join(out)


class DateTimeTool(Tool):
    streaming = 'row-local'
    manifest = ToolManifest(
        tool_id='core.datetime', version='1.0.0', display_name='DateTime', catalog_ids=('parse:datetime',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Convert text to date/time with a pattern (yyyy, yy, MM, MMM, Month, dd, d, day, HH, hh, mm, '
                    'ss, ampm), or date/time to text. Unparseable text becomes null with a warning. English names only.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['direction', 'field', 'format', 'output_field'], 'properties': {
            'direction': {'enum': ['to_date', 'to_text']},
            'field': {'type': 'string'}, 'format': {'type': 'string', 'minLength': 1},
            'output_field': {'type': 'string', 'minLength': 1},
            'output_type': {'enum': ['auto', 'date', 'datetime', 'time'], 'default': 'auto'},
        }})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        fmt = translate_format(cfg['format'])
        col = t.column(cfg['field'])
        if cfg['direction'] == 'to_text':
            out = []
            for v in col:
                if isinstance(v, str):  # text input is read as an ISO date/time first [ref:holdout DateTime sample]
                    try:
                        v = cast_value(v.strip(), Field('x', FieldType.DATETIME))[0] if v.strip() else None
                    except CastFailure:
                        v = None
                if v is None:
                    out.append(None)
                elif isinstance(v, (_dt.date, _dt.time)):
                    out.append(v.strftime(fmt))
                else:
                    raise DataError(f"{cfg['field']!r} is not a date/time field")
            return {'output': t.with_column(Field(cfg['output_field'], FieldType.V_WSTRING, TEXT), out)}
        has_time = any(c in fmt for c in _TIME_CODES)
        has_date = any(c in fmt for c in ('%Y', '%y', '%m', '%b', '%B', '%d'))
        kind = cfg['output_type'] if cfg['output_type'] != 'auto' else \
            ('datetime' if has_time and has_date else 'time' if has_time else 'date')
        ftype = {'date': FieldType.DATE, 'datetime': FieldType.DATETIME, 'time': FieldType.TIME}[kind]
        out, bad = [], 0
        for v in col:
            s = _text(v)
            if s is None or s.strip() == '':
                out.append(None)
                continue
            try:
                d = _dt.datetime.strptime(s.strip(), fmt)
                out.append(d.date() if kind == 'date' else d.time() if kind == 'time' else d)
            except ValueError:
                bad += 1
                out.append(None)
        if bad:
            ctx.warn(f'{bad} value(s) in {cfg["field"]!r} did not match "{cfg["format"]}" and became null', count=bad)
        return {'output': t.with_column(Field(cfg['output_field'], ftype), out)}


# =========================================================================== XML Parse

class XMLParse(Tool):
    manifest = ToolManifest(
        tool_id='core.xml_parse', version='1.0.0', display_name='XML Parse', catalog_ids=('parse:xml-parse',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Parse XML text. Each matching element (or the root) becomes a record; its child-element text and '
                    'attributes become fields named after them. Output schema is data-dependent.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['field'], 'properties': {
            'field': {'type': 'string'},
            'element': {'type': ['string', 'null'], 'default': None,
                        'description': 'Element name to return (null = the root element)'},
            'child_values': {'type': 'boolean', 'default': True},
            'attributes': {'type': 'boolean', 'default': True},
            'outer_xml': {'type': 'boolean', 'default': False},
            'ignore_errors': {'type': 'boolean', 'default': False},
        }})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['field']])
        fi = t.schema.index(cfg['field'])
        rows, new_names = [], []
        for n_row, row in enumerate(t.rows()):
            text = _text(row[fi])
            if text is None:
                continue
            try:
                root = ET.fromstring(text)
            except ET.ParseError as e:
                if cfg['ignore_errors']:
                    ctx.warn(f'record {n_row + 1}: XML parse error ignored ({e})')
                    continue
                raise DataError(f'record {n_row + 1}: invalid XML: {e}') from None
            elems = [root] if not cfg['element'] else root.iter(cfg['element'])
            for el in elems:
                vals = {}
                if cfg['attributes']:
                    for k, v in el.attrib.items():
                        vals[k] = v
                if cfg['child_values']:
                    # the element's own (direct) text is a field named after the element [ref:xml-parse-children]
                    vals[el.tag] = (el.text or '').strip()
                    for ch in el:
                        vals.setdefault(ch.tag, (ch.text or '').strip())
                if cfg['outer_xml']:
                    vals[f'{el.tag}_OuterXML'] = ET.tostring(el, encoding='unicode').strip()
                for k in vals:
                    if k not in new_names:
                        new_names.append(k)
                rows.append((row, vals))
        clash = [n for n in new_names if n in t.schema]
        if clash:
            raise SchemaError(f'Parsed names collide with existing fields: {clash}')
        fields = list(t.schema) + [Field(n, FieldType.V_WSTRING, TEXT) for n in new_names]
        return {'output': Table.from_rows(fields, [row + tuple(v.get(n) for n in new_names) for row, v in rows])}


# =========================================================================== Make Columns / Weighted Average

class MakeColumns(Tool):
    manifest = ToolManifest(
        tool_id='core.make_columns', version='1.0.0', display_name='Make Columns', catalog_ids=('transform:make-columns',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='Arrange records side by side into N column groups (fields named Column_<k>_<field>), filling '
                    'across rows (horizontal) or down columns (vertical), optionally per group.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['columns'], 'properties': {
            'columns': {'type': 'integer', 'minimum': 1}, 'horizontal': {'type': 'boolean', 'default': True},
            'group_by': {'type': 'array', 'items': {'type': 'string'}, 'default': []}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        k = cfg['columns']
        require_fields(t.schema, cfg['group_by'])
        gi = [t.schema.index(g) for g in cfg['group_by']]
        data_idx = [i for i in range(len(t.schema)) if i not in gi]
        # [ref:make-columns-horizontal] every column group is named Column_<k>_<field>
        fields = [t.schema[i] for i in gi] + [t.schema[i].renamed(f'Column_{c + 1}_{t.schema[i].name}')
                                              for c in range(k) for i in data_idx]
        groups: dict = {}
        for r in t.rows():
            groups.setdefault(tuple(r[i] for i in gi), []).append(r)
        rows = []
        for key, members in groups.items():
            n_rows = math.ceil(len(members) / k)
            grid = [[None] * k for _ in range(n_rows)]
            for p, r in enumerate(members):
                rr, cc = (p // k, p % k) if cfg['horizontal'] else (p % n_rows, p // n_rows)
                grid[rr][cc] = r
            for line in grid:
                out = tuple(key)
                for cell in line:
                    out += tuple(cell[i] for i in data_idx) if cell is not None else (None,) * len(data_idx)
                rows.append(out)
        return {'output': Table.from_rows(fields, rows)}


class WeightedAverage(Tool):
    manifest = ToolManifest(
        tool_id='core.weighted_average', version='1.0.0', display_name='Weighted Average',
        catalog_ids=('transform:weighted-average',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        description='sum(value * weight) / sum(weight), optionally per group (groups in ascending key order). '
                    'A null value adds nothing to the numerator but its weight still counts in the denominator; '
                    'a null weight is ignored; a zero weight total gives null.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['value', 'weight'], 'properties': {
            'value': {'type': 'string'}, 'weight': {'type': 'string'},
            'output_field': {'type': 'string', 'minLength': 1, 'default': 'WeightedAverage'},
            'group_by': {'type': 'array', 'items': {'type': 'string'}, 'default': []}}})

    def run(self, ctx, cfg, inputs):
        t: Table = inputs['input']
        require_fields(t.schema, [cfg['value'], cfg['weight']] + cfg['group_by'])
        for n in (cfg['value'], cfg['weight']):
            if not t.schema.field(n).type.is_numeric:
                raise SchemaError(f'{n!r} must be numeric')
        gi = [t.schema.index(g) for g in cfg['group_by']]
        vi, wi = t.schema.index(cfg['value']), t.schema.index(cfg['weight'])
        acc: dict = {}
        for r in t.rows():
            k = tuple(r[i] for i in gi)
            s = acc.setdefault(k, [0.0, 0.0])
            if r[wi] is not None:  # [ref:weighted-average-macro]
                if r[vi] is not None:
                    s[0] += float(r[vi]) * float(r[wi])
                s[1] += float(r[wi])
        from .preparation import sort_key_fn
        keyf = sort_key_fn(Schema([t.schema[i] for i in gi]), [{'field': t.schema[i].name} for i in gi])
        rows = [k + ((num / den) if den else None,) for k, (num, den) in sorted(acc.items(), key=lambda kv: keyf(kv[0]))]
        if not gi and not rows:
            rows = [(None,)]
        fields = [t.schema[i] for i in gi] + [Field(cfg['output_field'], FieldType.DOUBLE)]
        return {'output': Table.from_rows(fields, rows)}


TOOLS = [FindReplace, JoinMultiple, MakeGroup, DateTimeTool, XMLParse, MakeColumns, WeightedAverage]
