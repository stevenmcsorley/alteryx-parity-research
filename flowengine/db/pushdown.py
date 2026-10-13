"""In-database execution planning (query pushdown).

Starting from Database Input nodes, eligible downstream nodes are folded into one SQL query per database
"head". Every absorbed node is translated with SQL that reproduces flowengine semantics exactly; when that is
not possible the node runs locally and the reason is recorded. Nothing is reported as executed in the
database unless it was.

Supported (initial set): Select (projection, rename, reorder, drop; no type changes), Filter (True output only;
expressions with an exact SQL equivalent), Summarize (group by, sum, count, count_non_null, min, max, avg) and
Join (equi-join on the same connection; joined output only).

Semantics preserved in SQL:
* two-valued filter logic: every predicate is wrapped as CASE WHEN p THEN 1 ELSE 0 END = 1, so null
  comparisons are false (and NOT of them true) exactly as in the engine;
* ``=`` treats null = null as true; string comparisons are case-insensitive (LOWER on both sides) using a
  binary collation; grouping, joins and ordering of text use a binary (code-point) collation;
* division by zero yields NULL; integer division is floating point;
* output schemas are computed by the local kernels themselves and results are cast to them.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from ..formula import Binary, Call, Conditional, FieldRef, InList, Literal, Unary, parse
from ..table import Table
from ..types import CastFailure, Field, FieldType, Schema, cast_value


class NotPushable(Exception):
    pass


@dataclass
class Relation:
    conn_id: str
    dialect: object
    sql: str
    params: list
    fields: list            # list[Field] of this relation's output
    nodes: list             # node ids folded into this relation (source first)
    source: str             # Database Input node id
    steps: list = field(default_factory=list)  # human-readable description of each folded step


@dataclass
class Pushdown:
    heads: dict             # head node id -> Relation (executed as one query)
    absorbed: dict          # absorbed (non-head) node id -> head node id
    fallbacks: list         # [{node, reason}]


# =========================================================================== expression translation

def _kind(f: Field) -> str:
    t = f.type
    if t is FieldType.BOOL:
        return 'bool'
    if t.is_numeric:
        return 'number'
    if t.is_string:
        return 'string'
    if t in (FieldType.DATE, FieldType.DATETIME):
        return 'date'
    raise NotPushable(f'{t.value} fields are not translated')


class _Expr:
    def __init__(self, d, fields, alias):
        self.d = d
        self.by_name = {f.name: f for f in fields}
        self.alias = alias
        self.params: list = []

    def col(self, name):
        if name not in self.by_name:
            raise NotPushable(f'unknown field [{name}]')
        return f'{self.alias}.{self.d.quote(name)}'

    def param(self, v):
        self.params.append(v)
        return '%s'

    def P(self, raw):  # two-valued predicate
        return f'(CASE WHEN {raw} THEN 1 ELSE 0 END = 1)'

    def pred(self, node) -> str:
        if isinstance(node, Binary) and node.op in ('AND', 'OR'):
            return f'({self.pred(node.left)} {node.op} {self.pred(node.right)})'
        if isinstance(node, Unary) and node.op == 'NOT':
            return f'(NOT {self.pred(node.operand)})'
        if isinstance(node, Binary) and node.op in ('=', '!=', '<', '<=', '>', '>='):
            return self.compare(node.op, node.left, node.right)
        if isinstance(node, InList):
            ors = ' OR '.join(self.compare('=', node.operand, it) for it in node.items)
            return f'(NOT ({ors}))' if node.negated else f'({ors})'
        if isinstance(node, Literal) and isinstance(node.value, bool):
            return '(1 = 1)' if node.value else '(1 = 0)'
        if isinstance(node, FieldRef) and not node.row_offset:
            f = self.by_name.get(node.name)
            if f is None or f.type is not FieldType.BOOL:
                raise NotPushable('only boolean fields can be used as conditions')
            c = self.col(node.name)
            return self.P(f'{c} = TRUE' if self.d.name == 'postgresql' else f'{c} = 1')
        if isinstance(node, Call):
            return self.call_pred(node)
        raise NotPushable(f'{type(node).__name__} is not translated to SQL')

    def call_pred(self, node: Call) -> str:
        name, args = node.name, node.args
        if name == 'ISNULL' and len(args) == 1:
            v, _ = self.value(args[0])
            return f'({v} IS NULL)'
        if name == 'ISEMPTY' and len(args) == 1:
            v, k = self.value(args[0])
            return f"({v} IS NULL OR {v} = '')" if k == 'string' else f'({v} IS NULL)'
        if name in ('CONTAINS', 'STARTSWITH', 'ENDSWITH') and len(args) in (2, 3):
            s, ks = self.value(args[0])
            if ks != 'string' or not isinstance(args[1], Literal) or not isinstance(args[1].value, str):
                raise NotPushable(f'{name} is translated only for a text field and a text literal')
            ci = True
            if len(args) == 3:
                if not isinstance(args[2], Literal) or not isinstance(args[2].value, (int, bool)):
                    raise NotPushable(f'{name} flag must be a literal')
                ci = bool(args[2].value)
            t = args[1].value.lower() if ci else args[1].value
            esc = t.replace('!', '!!').replace('%', '!%').replace('_', '!_')
            if self.d.name == 'sqlserver':
                esc = esc.replace('[', '![')
            pattern = {'CONTAINS': f'%{esc}%', 'STARTSWITH': f'{esc}%', 'ENDSWITH': f'%{esc}'}[name]
            target = f"LOWER(COALESCE({s}, ''))" if ci else f"COALESCE({s}, '')"
            return self.P(f"{target} {self.d.binary_collation} LIKE {self.param(pattern)} ESCAPE '!'")
        raise NotPushable(f'function {name} is evaluated by the engine (no exact SQL equivalent)')

    def value(self, node) -> tuple[str, str]:
        """Translate a value expression; returns (sql, kind)."""
        if isinstance(node, FieldRef) and not node.row_offset:
            if node.name not in self.by_name:
                raise NotPushable(f'unknown field [{node.name}]')
            return self.col(node.name), _kind(self.by_name[node.name])
        if isinstance(node, Literal):
            v = node.value
            if v is None:
                return 'NULL', 'null'
            if isinstance(v, bool):
                return self.param(v), 'bool'
            if isinstance(v, (int, float)):
                return self.param(v), 'number'
            return self.param(v), 'string'
        if isinstance(node, Unary) and node.op == 'NEG':
            v, k = self.value(node.operand)
            if k != 'number':
                raise NotPushable('unary minus on a non-number')
            return f'(-{v})', 'number'
        if isinstance(node, Binary) and node.op in ('+', '-', '*', '/'):
            a, ka = self.value(node.left)
            b, kb = self.value(node.right)
            if ka != 'number' or kb != 'number':
                raise NotPushable(f"'{node.op}' is translated for numbers only")
            if node.op == '/':
                fl = 'double precision' if self.d.name == 'postgresql' else 'float'
                return f'(CAST({a} AS {fl}) / NULLIF(CAST({b} AS {fl}), 0))', 'number'
            return f'({a} {node.op} {b})', 'number'
        raise NotPushable(f'{type(node).__name__} values are not translated to SQL')

    def compare(self, op, left, right) -> str:
        a, ka = self.value(left)
        b, kb = self.value(right)
        kinds = {ka, kb} - {'null'}
        if 'date' in kinds and 'string' in kinds:  # ISO text literal compared with a date field
            lit = right if kb == 'string' else left
            if not isinstance(lit, Literal):
                raise NotPushable('date compared with a text expression')
            iso = lit.value.strip()
            try:
                parsed = cast_value(iso, Field('x', FieldType.DATETIME if len(iso) > 10 else FieldType.DATE))[0]
            except CastFailure:
                raise NotPushable('text literal is not an ISO date') from None
            self.params[self.params.index(lit.value)] = parsed
            kinds = {'date'}
        if len(kinds) > 1:
            raise NotPushable(f'comparison between {sorted(kinds)}')
        kind = kinds.pop() if kinds else 'null'
        if kind == 'string':
            a = f'LOWER({a}) {self.d.binary_collation}'
            b = f'LOWER({b})'
        if op == '=':
            return self.P(f'({a} = {b} OR ({a} IS NULL AND {b} IS NULL))')
        if op == '!=':
            return f'(NOT {self.P(f"({a} = {b} OR ({a} IS NULL AND {b} IS NULL))")})'
        return self.P(f'{a} {op} {b}')


# =========================================================================== planning

def _local_schema(tool, cfg, input_schemas: dict) -> dict:
    empty = {k: Table.empty(v) for k, v in input_schemas.items()}
    from ..plugin import CancelToken, RunContext
    ctx = RunContext('pushdown-plan', '.', _dt.datetime.now, lambda *a, **k: None, CancelToken(), dry_run=True)
    return {k: t.schema for k, t in tool.run(ctx, cfg, empty).items()}


def plan_pushdown(plan, profile=None) -> Pushdown:
    from ..tools.database import open_dialect, source_sql
    consumers: dict = {}
    for nid in plan.order:
        for anchor, ups in plan.nodes[nid].inputs.items():
            for u, ua in ups:
                consumers.setdefault((u, ua), []).append((nid, anchor))
    rel: dict[str, Relation] = {}
    out_anchor: dict[str, str] = {}
    fallbacks = []
    absorbed: dict[str, str] = {}
    counter = [0]

    def alias():
        counter[0] += 1
        return f'fb{counter[0]}'

    for nid in plan.order:
        pn = plan.nodes[nid]
        if nid in plan.skipped:
            continue
        tid = pn.tool.manifest.tool_id
        if tid == 'core.db_input':
            try:
                conn_cfg, d = open_dialect(pn.config['connection_id'])
            except Exception:
                continue  # reported when the node runs
            schema = plan.schemas.get((nid, 'output'))
            if schema is None:
                continue
            sql, params = source_sql(d, pn.config)
            if pn.config.get('max_rows') is not None:
                sql = d.limit(sql, pn.config['max_rows'])
            rel[nid] = Relation(pn.config['connection_id'], d, sql, list(params), list(schema), [nid], nid,
                                [f'read {pn.config.get("table") or "query"}'])
            out_anchor[nid] = 'output'
            continue
        ups = [(a, u, ua) for a, lst in pn.inputs.items() for u, ua in lst]
        if not ups or not all(u in rel and ua == out_anchor[u] for _, u, ua in ups):
            continue
        if any(len(consumers.get((u, ua), [])) != 1 for _, u, ua in ups):
            for _, u, ua in ups:
                if u in rel:
                    fallbacks.append({'node': nid, 'reason': f'{u} output is also used elsewhere; folding stops there'})
            continue
        try:
            new, anchor = _fold(nid, pn, ups, rel, alias, profile)
        except NotPushable as e:
            fallbacks.append({'node': nid, 'reason': str(e)})
            continue
        for _, u, _ in ups:
            absorbed[u] = nid
            del rel[u]
        rel[nid] = new
        out_anchor[nid] = anchor
    # every remaining relation is a head; its absorbed members point to it
    heads = {}
    for head, r in rel.items():
        heads[head] = r
        for n in r.nodes:
            if n != head:
                absorbed[n] = head
    absorbed = {n: h for n, h in absorbed.items() if h in heads and n not in heads}
    return Pushdown(heads, absorbed, fallbacks)


def _fold(nid, pn, ups, rel, alias, profile):
    tid = pn.tool.manifest.tool_id
    cfg = pn.config
    if tid == 'core.select':
        (_, u, _), = ups
        r = rel[u]
        if any('type' in f or 'size' in f or 'scale' in f for f in cfg['fields']):
            raise NotPushable('Select changes types; conversions are evaluated by the engine')
        out = _local_schema(pn.tool, cfg, {'input': Schema(r.fields)})['output']
        names = [f.name for f in r.fields]
        renamed = {f['field']: f['rename'] for f in cfg['fields'] if f.get('rename')}
        back = {renamed.get(n, n): n for n in names}
        a = alias()
        cols = ', '.join(f'{a}.{r.dialect.quote(back[f.name])} AS {r.dialect.quote(f.name)}' for f in out)
        return _wrap(r, nid, f'SELECT {cols} FROM ({r.sql}) {a}', [], list(out), 'project ' + ', '.join(out.names)), 'output'
    if tid == 'core.filter':
        (_, u, _), = ups
        r = rel[u]
        a = alias()
        ex = _Expr(r.dialect, r.fields, a)
        where = ex.pred(parse(cfg['expression']))
        return _wrap(r, nid, f'SELECT * FROM ({r.sql}) {a} WHERE {where}', ex.params, r.fields,
                     f'filter {cfg["expression"]}'), 'true'
    if tid == 'core.summarize':
        (_, u, _), = ups
        r = rel[u]
        d = r.dialect
        out = _local_schema(pn.tool, cfg, {'input': Schema(r.fields)})['output']
        a = alias()
        fields = {f.name: f for f in r.fields}
        sel, groups = [], []
        for act, of in zip(cfg['actions'], out):
            col = f'{a}.{d.quote(act["field"])}'
            src = fields[act['field']]
            op = act['op']
            if op == 'group_by':
                g = f'{col} {d.binary_collation}' if src.type.is_string else col
                groups.append(g)
                sel.append(f'{g} AS {d.quote(of.name)}')
                continue
            if op == 'sum':
                if src.type.is_integer or src.type is FieldType.BOOL:
                    inner = f'CAST({col} AS {"integer" if d.name == "postgresql" else "int"})' if src.type is FieldType.BOOL else col
                    expr = f'CAST(SUM(CAST({inner} AS bigint)) AS bigint)'
                elif src.type in (FieldType.FLOAT, FieldType.DOUBLE):
                    expr = f'SUM(CAST({col} AS {"double precision" if d.name == "postgresql" else "float"}))'
                elif src.type is FieldType.FIXED_DECIMAL:
                    expr = f'SUM({col})'
                else:
                    raise NotPushable('sum of a non-numeric field')
            elif op == 'count':
                expr = 'COUNT(*)'
            elif op == 'count_non_null':
                expr = f'COUNT({col})'
            elif op in ('min', 'max'):
                if src.type is FieldType.BOOL:
                    raise NotPushable('min/max of booleans')
                inner = f'{col} {d.binary_collation}' if src.type.is_string else col
                expr = f'{op.upper()}({inner})'
            elif op == 'avg':
                if not src.type.is_numeric:
                    raise NotPushable('avg of a non-numeric field')
                expr = f'AVG(CAST({col} AS {"double precision" if d.name == "postgresql" else "float"}))'
            else:
                raise NotPushable(f'aggregation {op} is evaluated by the engine')
            sel.append(f'{expr} AS {d.quote(of.name)}')
        sql = f'SELECT {", ".join(sel)} FROM ({r.sql}) {a}'
        if groups:
            sql += ' GROUP BY ' + ', '.join(groups)
            if cfg.get('sort_groups', True):
                nulls = ' NULLS FIRST' if d.name == 'postgresql' else ''
                sql += ' ORDER BY ' + ', '.join(f'{g} ASC{nulls}' for g in groups)
        return _wrap(r, nid, sql, [], list(out), 'aggregate ' + ', '.join(f"{x['op']}({x['field']})" for x in cfg['actions'])), 'output'
    if tid == 'core.join':
        lr = next(rel[u] for a_, u, _ in ups if a_ == 'left')
        rr = next(rel[u] for a_, u, _ in ups if a_ == 'right')
        if lr.conn_id != rr.conn_id:
            raise NotPushable('inputs come from different connections (joined locally)')
        if cfg['by'] != 'fields' or cfg.get('case_insensitive'):
            raise NotPushable('only case-sensitive joins on key fields are translated')
        d = lr.dialect
        out = _local_schema(pn.tool, cfg, {'left': Schema(lr.fields), 'right': Schema(rr.fields)})['joined']
        la, ra = alias(), alias()
        l_names = [f.name for f in lr.fields]
        l_keep = cfg['left_fields'] if cfg['left_fields'] is not None else l_names
        r_keep = cfg['right_fields'] if cfg['right_fields'] is not None else [f.name for f in rr.fields]
        if cfg['drop_right_keys']:
            r_keep = [n for n in r_keep if n not in {k['right'] for k in cfg['keys']}]
        sources = [(la, n) for n in l_keep] + [(ra, n) for n in r_keep]
        cols = ', '.join(f'{s}.{d.quote(n)} AS {d.quote(f.name)}' for (s, n), f in zip(sources, out))
        lfields = {f.name: f for f in lr.fields}
        conds, order = [], []
        for k in cfg['keys']:
            lc, rc = f'{la}.{d.quote(k["left"])}', f'{ra}.{d.quote(k["right"])}'
            if lfields[k['left']].type.is_string:
                conds.append(f'({lc} {d.binary_collation} = {rc} {d.binary_collation} OR ({lc} IS NULL AND {rc} IS NULL))')
                order.append(f'{lc} {d.binary_collation}')
            else:
                conds.append(f'({lc} = {rc} OR ({lc} IS NULL AND {rc} IS NULL))')
                order.append(lc)
        nulls = ' NULLS FIRST' if d.name == 'postgresql' else ''
        sql = (f'SELECT {cols} FROM ({lr.sql}) {la} INNER JOIN ({rr.sql}) {ra} ON {" AND ".join(conds)} '
               f'ORDER BY {", ".join(o + " ASC" + nulls for o in order)}')
        new = Relation(lr.conn_id, d, sql, lr.params + rr.params, list(out), lr.nodes + rr.nodes + [nid], lr.source,
                       lr.steps + rr.steps + [f'join on {", ".join(k["left"] + "=" + k["right"] for k in cfg["keys"])}'])
        return new, 'joined'
    raise NotPushable(f'{pn.tool.manifest.display_name} runs in the engine (no SQL translation)')


def _wrap(r: Relation, nid, sql, extra_params, fields, step) -> Relation:
    return Relation(r.conn_id, r.dialect, sql, r.params + list(extra_params), fields, r.nodes + [nid], r.source,
                    r.steps + [step])
