"""Tiny original DAG/CSV execution foundation for a fully scoped future product.

IMPORTANT: Generic engineering testbed, NOT a compatible Alteryx runtime. This
module implements only five independently designed primitives with limited
string and Decimal semantics; all 323 vendor tool entries remain unimplemented.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[dict[str, str], ...]

    def __post_init__(self):
        if len(set(self.columns)) != len(self.columns):
            raise WorkflowError('Duplicate column names')
        if any(set(row) != set(self.columns) for row in self.rows):
            raise WorkflowError('Row shape differs from declared columns')


def _csv_input(config, inputs, base):
    if inputs: raise WorkflowError('csv_input accepts no input anchors')
    path = (base / config['path']).resolve()
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames: raise WorkflowError('CSV requires header fields')
        fields = tuple(reader.fieldnames)
        if len(set(fields)) != len(fields): raise WorkflowError('Duplicate CSV header fields')
        rows = []
        for row in reader:
            if None in row: raise WorkflowError('CSV row has extra columns')
            if any(v is None for v in row.values()): raise WorkflowError('CSV row has missing columns')
            rows.append(row)
    return Table(fields, tuple(rows))


def _filter(config, inputs, base):
    _require(inputs, ('main',))
    t=inputs['main']; field=config['field']
    if field not in t.columns: raise WorkflowError(f'Unknown filter field {field!r}')
    value=str(config['value']); op=config.get('op','eq')
    operations={'eq':lambda x:x==value, 'ne':lambda x:x!=value,
                'contains':lambda x:value in x,
                'not_empty':lambda x:x!=''}
    if op not in operations: raise WorkflowError(f'Unsupported filter operator {op!r}')
    return Table(t.columns,tuple(row for row in t.rows if operations[op](row[field])))


def _join(config, inputs, base):
    _require(inputs, ('left', 'right'))
    lt,rt = inputs['left'],inputs['right']; lk=config['left_on']; rk=config['right_on']
    if lk not in lt.columns or rk not in rt.columns: raise WorkflowError('Unknown join key')
    if config.get('how','inner') != 'inner': raise WorkflowError('Only inner join implemented')
    right_cols=[c for c in rt.columns if c!=rk]
    if any(c in lt.columns for c in right_cols): raise WorkflowError('Overlapping column names: explicit renaming required')
    index=defaultdict(list)
    for r in rt.rows:index[r[rk]].append(r)
    result=[]
    for left in lt.rows:
        for right in index.get(left[lk],[]):
            result.append({**left, **{c:right[c] for c in right_cols}})
    return Table(lt.columns+tuple(right_cols),tuple(result))


def _summarize(config,inputs,base):
    _require(inputs, ('main',))
    t=inputs['main']; group_by=config.get('group_by',[])
    if len(group_by)!=len(set(group_by)) or any(c not in t.columns for c in group_by):
        raise WorkflowError('Invalid group_by columns')
    field=config['field']; output=config.get('as',f'{config["op"]}_{field}'); op=config['op']
    if op not in ('sum','count'):raise WorkflowError(f'Unsupported aggregation {op!r}')
    if op=='sum' and field not in t.columns: raise WorkflowError(f'Unknown sum field {field!r}')
    if output in group_by: raise WorkflowError('Summary output shadows grouping column')
    values={}
    for row in t.rows:
        key=tuple(row[c] for c in group_by)
        if key not in values:values[key]=Decimal(0) if op=='sum' else 0
        if op=='count': values[key]+=1
        else:
            try: values[key]+=Decimal(row[field])
            except InvalidOperation as e:raise WorkflowError(f'Non-numeric sum value in {field!r}') from e
    result=[]
    for key,total in values.items():
        result.append({**dict(zip(group_by,key)),output:format(total,'f') if op=='sum' else str(total)})
    return Table(tuple(group_by)+(output,),tuple(result))


def _csv_output(config,inputs,base):
    _require(inputs,('main',))
    t=inputs['main']; path=(base/config['path']).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    import tempfile, os
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.workflow-',suffix='.csv')
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=t.columns)
            writer.writeheader();writer.writerows(t.rows)
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)
    return t


def _require(actual,expected):
    if set(actual)!=set(expected):
        raise WorkflowError(f'Expected anchors {sorted(expected)}, received {sorted(actual)}')

OPERATORS={'csv_input':_csv_input,'filter':_filter,'join':_join,'summarize':_summarize,'csv_output':_csv_output}


def run_workflow(document:dict, *, base:Path) -> dict[str,Table]:
    """Run one small workflow, validating missing nodes, cycles and anchor collisions."""
    nodes=document.get('nodes');edges=document.get('edges')
    if not isinstance(nodes,list) or not isinstance(edges,list):raise WorkflowError('nodes and edges arrays required')
    ids=[n.get('id') for n in nodes]
    if any(not isinstance(x,str) or not x for x in ids) or len(set(ids))!=len(ids):
        raise WorkflowError('Node IDs must be unique nonempty strings')
    byid={n['id']:n for n in nodes}
    for node in nodes:
        if node.get('type') not in OPERATORS:raise WorkflowError(f'Unimplemented operator {node.get("type")!r}')
        if not isinstance(node.get('config'),dict):raise WorkflowError('Every node needs a config object')
    inputs=defaultdict(dict);succ=defaultdict(list);degree={x:0 for x in ids}
    for e in edges:
        a,b,port=e['from'],e['to'],e.get('input','main')
        if a not in byid or b not in byid:raise WorkflowError('Edge refers to unknown node')
        if port in inputs[b]:raise WorkflowError('Two producers connected to same input anchor')
        inputs[b][port]=a;succ[a].append(b);degree[b]+=1
    queue=[x for x in ids if degree[x]==0]
    result={}
    while queue:
        node_id=queue.pop(0);n=byid[node_id]
        args={anchor:result[upstream] for anchor,upstream in inputs[node_id].items()}
        try: result[node_id]=OPERATORS[n['type']](n['config'],args,base)
        except (OSError,KeyError,InvalidOperation) as e:raise WorkflowError(f'Failed node {node_id!r}: {e}') from e
        for next_node in succ[node_id]:
            degree[next_node]-=1
            if degree[next_node]==0:queue.append(next_node)
    if len(result)!=len(nodes):raise WorkflowError('Cycle detected in workflow')
    return result


def run_from_file(file:Path):
    return run_workflow(json.loads(file.read_text(encoding='utf-8')),base=file.parent)
