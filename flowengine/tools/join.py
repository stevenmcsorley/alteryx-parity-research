"""Join category kernels: Join, Union, Append Fields."""
from __future__ import annotations

from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, Schema, cast_value, CastFailure, unify_fields
from ._common import require_fields


def _join_key(row, idx, ci):
    return tuple((row[i].casefold() if ci and isinstance(row[i], str) else row[i]) for i in idx)


class Join(Tool):
    manifest = ToolManifest(
        tool_id='core.join', version='1.0.0', display_name='Join',
        catalog_ids=('join:join',),
        inputs=(Anchor('left', 'L'), Anchor('right', 'R')),
        outputs=(Anchor('left_unjoined', 'L', description='Left records without a match'),
                 Anchor('joined', 'J', description='Matched records (many-to-many multiplies)'),
                 Anchor('right_unjoined', 'R', description='Right records without a match')),
        description='Join on key fields or record position. Null keys match each other. Output is ordered by '
                    'key (right-record-major within a key); unjoined outputs are ordered by key.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['by'],
            'properties': {
                'by': {'enum': ['fields', 'position']},
                'keys': {'type': 'array', 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['left', 'right'],
                    'properties': {'left': {'type': 'string'}, 'right': {'type': 'string'}}}},
                'case_insensitive': {'type': 'boolean', 'default': False},
                'right_prefix': {'type': 'string', 'default': 'Right_',
                                 'description': 'Prefix applied to right field names that collide with left names'},
                'drop_right_keys': {'type': 'boolean', 'default': False},
                'right_fields': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None,
                                 'description': 'Right fields to keep in the joined output (null = all)'},
                'left_fields': {'type': ['array', 'null'], 'items': {'type': 'string'}, 'default': None},
            },
        })

    def check_config(self, cfg):
        if cfg['by'] == 'fields' and not cfg.get('keys'):
            raise ConfigError('Join by fields requires at least one key pair')

    def run(self, ctx, cfg, inputs):
        lt: Table = inputs['left']
        rt: Table = inputs['right']
        lrows, rrows = list(lt.rows()), list(rt.rows())
        pairs = []
        if cfg['by'] == 'position':
            n = min(len(lrows), len(rrows))
            pairs = [(i, i) for i in range(n)]
            l_un = list(range(n, len(lrows)))
            r_un = list(range(n, len(rrows)))
        else:
            lk = [k['left'] for k in cfg['keys']]
            rk = [k['right'] for k in cfg['keys']]
            require_fields(lt.schema, lk, 'left key field')
            require_fields(rt.schema, rk, 'right key field')
            for a, b in zip(lk, rk):
                fa, fb = lt.schema.field(a), rt.schema.field(b)
                if (fa.type.is_string != fb.type.is_string) or (fa.type.is_numeric != fb.type.is_numeric):
                    raise SchemaError(f'Key types are incompatible: {a} ({fa.type.value}) vs {b} ({fb.type.value})')
            li = [lt.schema.index(k) for k in lk]
            ri = [rt.schema.index(k) for k in rk]
            ci = cfg['case_insensitive']
            # Null keys match each other; output is ordered by key, right-record-major within a key;
            # unjoined outputs are ordered by key [ref:join-many-to-many-nulls, probe-join-unjoined-order]
            index: dict = {}
            for j, r in enumerate(rrows):
                index.setdefault(_join_key(r, ri, ci), []).append(j)
            matched_r = set()
            l_un = []
            for i, r in enumerate(lrows):
                hits = index.get(_join_key(r, li, ci), [])
                if not hits:
                    l_un.append(i)
                for j in hits:
                    pairs.append((i, j))
                    matched_r.add(j)
                if len(pairs) % 100000 == 0:
                    ctx.check_cancelled()
            r_un = [j for j in range(len(rrows)) if j not in matched_r]
            from .preparation import sort_key_fn
            lkey = sort_key_fn(lt.schema, [{'field': k} for k in lk], ci)
            rkey = sort_key_fn(rt.schema, [{'field': k} for k in rk], ci)
            pairs.sort(key=lambda p: (lkey(lrows[p[0]]), p[1], p[0]))
            l_un.sort(key=lambda i: lkey(lrows[i]))
            r_un.sort(key=lambda j: rkey(rrows[j]))
        # joined schema
        l_keep = cfg['left_fields'] if cfg['left_fields'] is not None else lt.schema.names
        r_keep = cfg['right_fields'] if cfg['right_fields'] is not None else rt.schema.names
        require_fields(lt.schema, l_keep, 'left output field')
        require_fields(rt.schema, r_keep, 'right output field')
        if cfg['drop_right_keys'] and cfg['by'] == 'fields':
            r_keep = [n for n in r_keep if n not in {k['right'] for k in cfg['keys']}]
        out_fields = [lt.schema.field(n) for n in l_keep]
        used = set(l_keep)
        for n in r_keep:
            name = n
            if name in used:
                name = cfg['right_prefix'] + n
                if name in used or name in r_keep:
                    raise SchemaError(f'Renamed right field {name!r} still collides; choose another right_prefix')
            used.add(name)
            out_fields.append(rt.schema.field(n).renamed(name))
        lidx = [lt.schema.index(n) for n in l_keep]
        ridx = [rt.schema.index(n) for n in r_keep]
        joined = [tuple(lrows[i][a] for a in lidx) + tuple(rrows[j][b] for b in ridx) for i, j in pairs]
        return {'left_unjoined': lt.take(l_un), 'joined': Table.from_rows(out_fields, joined),
                'right_unjoined': rt.take(r_un)}


class Union(Tool):
    manifest = ToolManifest(
        tool_id='core.union', version='1.0.0', display_name='Union',
        catalog_ids=('join:union',),
        inputs=(Anchor('inputs', 'I', multiple=True, description='Two or more streams, stacked in connection order'),),
        outputs=(Anchor('output', 'O'),),
        description='Stack records by field name or by position. Field types are widened to a common type.',
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'mode': {'enum': ['name', 'position'], 'default': 'name'},
                'missing_fields': {'enum': ['include', 'intersection', 'error'], 'default': 'include',
                                   'description': 'Fields absent from some inputs: fill with null, drop, or fail'},
                'case_insensitive_names': {'type': 'boolean', 'default': False},
            },
        })

    def run(self, ctx, cfg, inputs):
        tables: list[Table] = inputs['inputs']
        if cfg['mode'] == 'position':
            width = max((len(t.schema) for t in tables), default=0)
            if cfg['missing_fields'] == 'error' and len({len(t.schema) for t in tables}) > 1:
                raise SchemaError('Inputs have different field counts')
            if cfg['missing_fields'] == 'intersection':
                width = min(len(t.schema) for t in tables)
            names = []
            fields = []
            for p in range(width):
                fs = [t.schema[p] for t in tables if p < len(t.schema)]
                f = fs[0]
                for g in fs[1:]:
                    f = unify_fields(f, g.renamed(f.name))
                fields.append(f)
                names.append(f.name)
            locate = [[(p if p < len(t.schema) else None) for p in range(width)] for t in tables]
        else:
            norm = (lambda s: s.casefold()) if cfg['case_insensitive_names'] else (lambda s: s)
            order, protos = [], {}
            for t in tables:
                for f in t.schema:
                    k = norm(f.name)
                    if k not in protos:
                        order.append(k)
                        protos[k] = f
                    else:
                        protos[k] = unify_fields(protos[k], f.renamed(protos[k].name))
            present = [{norm(n) for n in t.schema.names} for t in tables]
            missing = [k for k in order if not all(k in p for p in present)]
            if missing and cfg['missing_fields'] == 'error':
                raise SchemaError(f'Fields missing from some inputs: {[protos[k].name for k in missing]}')
            if cfg['missing_fields'] == 'intersection':
                order = [k for k in order if k not in missing]
            fields = [protos[k] for k in order]
            locate = []
            for t in tables:
                pos = {norm(n): i for i, n in enumerate(t.schema.names)}
                locate.append([pos.get(k) for k in order])
        cols = [[] for _ in fields]
        for t, loc in zip(tables, locate):
            for c, (f, p) in enumerate(zip(fields, loc)):
                if p is None:
                    cols[c].extend([None] * len(t))
                else:
                    src = t.columns[p]
                    if t.schema[p].type == f.type and t.schema[p].size == f.size:
                        cols[c].extend(src)
                    else:
                        try:
                            cols[c].extend(cast_value(v, f)[0] for v in src)
                        except CastFailure as e:
                            raise DataError(f'Union cannot widen {f.name!r}: {e}') from None
        return {'output': Table(Schema(fields), cols)}


class AppendFields(Tool):
    manifest = ToolManifest(
        tool_id='core.append_fields', version='1.0.0', display_name='Append Fields',
        catalog_ids=('join:append-fields',),
        inputs=(Anchor('target', 'T'), Anchor('source', 'S')),
        outputs=(Anchor('output', 'O'),),
        description='Cartesian product: every source record appended to every target record.',
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'max_source_records': {'type': 'integer', 'minimum': 1, 'default': 16,
                                       'description': 'Fail when the source exceeds this (guards accidental explosions)'},
                'source_prefix': {'type': 'string', 'default': 'Source_'},
            },
        })

    def run(self, ctx, cfg, inputs):
        tt: Table = inputs['target']
        st: Table = inputs['source']
        if len(st) > cfg['max_source_records']:
            raise DataError(f"Source has {len(st)} records; limit is {cfg['max_source_records']} "
                            f'(raise max_source_records to allow a larger cross product)')
        fields = list(tt.schema)
        names = set(tt.schema.names)
        for f in st.schema:
            name = f.name if f.name not in names else cfg['source_prefix'] + f.name
            if name in names:
                raise SchemaError(f'Field {name!r} collides even after prefixing')
            names.add(name)
            fields.append(f.renamed(name))
        srows = list(st.rows())
        rows = [tr + sr for tr in tt.rows() for sr in srows]
        return {'output': Table.from_rows(fields, rows)}


TOOLS = [Join, Union, AppendFields]
