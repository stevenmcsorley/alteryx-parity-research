"""Agent operations: how an AI orchestrator discovers and operates Flowbench without moving data
into the model. Flowbench does the computing; the model works with tool ids, configurations,
schemas, file and run references and small aggregated results.

Each operation has an MCP-style specification (name, description, inputSchema, annotations) and is
served by the engine as ``agent.tools_list`` / ``agent.call`` and by the stdio MCP server
(``python -m flowengine mcp``). Permissions are enforced here, not left to model instructions: an
operation that reaches the network or writes files fails with ``permission-required`` unless the
caller grants that permission for the call.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from . import __version__, catalog
from .errors import EngineError
from .plugin import default_registry

SPEC_VERSION = '1'
DEFAULT_ROWS = 100
MAX_LIST = 1000
DOWNLOAD_LIMIT = 1024 ** 3

# Words people use for a task, mapped to words that appear in the tools that do it.
SYNONYMS = {
    'average': 'avg summarize', 'mean': 'avg summarize', 'total': 'sum summarize', 'group': 'group_by summarize',
    'aggregate': 'summarize', 'count': 'count summarize', 'download': 'download http url', 'http': 'download',
    'api': 'download json', 'web': 'download', 'url': 'download', 'fetch': 'download', 'merge': 'join union',
    'combine': 'join union', 'lookup': 'join find replace', 'chart': 'chart plot', 'plot': 'chart',
    'graph': 'chart', 'visualise': 'chart', 'visualize': 'chart', 'clean': 'cleansing data auto field',
    'type': 'auto field select', 'convert': 'auto field select', 'rename': 'select', 'calculate': 'formula',
    'column': 'formula select', 'filter': 'filter', 'where': 'filter', 'sort': 'sort', 'order': 'sort',
    'dedupe': 'unique', 'duplicate': 'unique', 'pivot': 'cross tab transpose', 'unpivot': 'transpose',
    'regression': 'linear regression', 'forecast': 'arima ets forecast', 'cluster': 'k-centroids cluster',
    'python': 'python', 'script': 'python run command', 'shell': 'run command', 'command': 'run command',
    'rolling': 'multi-row formula moving', 'moving': 'multi-row formula', 'running': 'running total',
    'percentile': 'summarize percentile', 'sample': 'sample random', 'outlier': 'outlier',
    'correlation': 'correlation', 'map': 'spatial map', 'geocode': 'spatial', 'text': 'text',
    'sentiment': 'sentiment', 'ocr': 'image to text', 'pdf': 'pdf', 'excel': 'excel xlsx', 'csv': 'input data csv',
    'read': 'input data', 'load': 'input data', 'import': 'input data', 'write': 'output data', 'save': 'output data',
    'export': 'output data', 'report': 'report render', 'database': 'database db input',
}

# Facts an AI needs that the configuration schema alone does not say. Keep these true to the kernel.
USAGE_NOTES = {
    'core.input_data': ['Delimited text (CSV/TSV) fields arrive as text (v_wstring). Add Auto Field, or Select with explicit types, before numeric work such as Summarize sums or Formula arithmetic.',
                        'Relative paths resolve against the workflow folder.'],
    'core.auto_field': ['Chooses the narrowest type that fits every value, for example byte, int32, double or date. Use it straight after Input Data for CSV files.'],
    'core.summarize': ['Each action is {field, op, output?}. Use op group_by for grouping fields. Numeric ops (sum, avg, std_dev, ...) need numeric fields.',
                       'Output field names default to <Op>_<Field>, for example Sum_Sales.'],
    'core.download': ['Makes one HTTP request per input record, reading the URL from url_field. Feed it a Text Input with one URL per row.',
                      'Needs network permission at run time.'],
    'core.formula': ['Expressions use the Alteryx formula language, for example [Price] * [Qty] or IIF([Sales] > 100, "High", "Low").'],
    'core.python': ['Runs a Python script over the input table. Use it when no native tool fits; record why in the workflow.',
                    'Needs process permission at run time.'],
    'core.run_command': ['Runs an external program. Needs process permission at run time.'],
}


class Op:
    def __init__(self, name, description, schema, handler, read_only=True, open_world=False,
                 writes_files=False, permissions=()):
        self.name, self.description, self.schema, self.handler = name, description, schema, handler
        self.read_only, self.open_world, self.writes_files = read_only, open_world, writes_files
        self.permissions = tuple(permissions)

    def spec(self) -> dict:
        return {'name': self.name, 'description': self.description, 'inputSchema': self.schema,
                'annotations': {'readOnlyHint': self.read_only, 'destructiveHint': False,
                                'idempotentHint': self.read_only, 'openWorldHint': self.open_world},
                'x-flowbench': {'version': SPEC_VERSION, 'permissions': list(self.permissions)}}


def _obj(properties: dict, required=()) -> dict:
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


class Context:
    """What an operation may touch: the workspace folder, granted permissions and the engine's runs."""

    def __init__(self, base_dir, permissions=None, runs=None):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.permissions = set(permissions or ())
        self.runs = runs if runs is not None else {}

    def path(self, raw: str) -> Path:
        p = Path(raw).expanduser()
        return (p if p.is_absolute() else self.base_dir / p).resolve()


# ---------------------------------------------------------------------------------------- discovery
def _words(text: str) -> list[str]:
    return [w for w in re.split(r'[^a-z0-9]+', text.lower()) if len(w) > 1]


def _tool_text(tool, names: dict) -> list[tuple[str, int]]:
    m = tool.manifest
    props = ' '.join((m.config_schema or {}).get('properties', {}).keys())
    return [(m.display_name, 6), (m.tool_id.replace('core.', '').replace('_', ' '), 5),
            (' '.join(names.get(c, '') for c in m.catalog_ids), 4),
            (m.description or '', 2), (' '.join(USAGE_NOTES.get(m.tool_id, [])), 1), (props.replace('_', ' '), 1)]


def find_tools(ctx: Context, query: str, limit: int = 12) -> dict:
    reg = default_registry()
    names = {e['id']: f"{e['name']} {e['category']}" for e in catalog.entries()}
    terms = []
    for w in _words(query):
        terms.append(w)
        terms.extend(_words(SYNONYMS.get(w, '')))
    scored = []
    for tool in reg.tools():
        score = 0
        for text, weight in _tool_text(tool, names):
            words = set(_words(text))
            score += weight * sum(1 for t in terms if t in words or any(w.startswith(t) for w in words if len(t) > 3))
        if score:
            scored.append((score, tool))
    scored.sort(key=lambda s: (-s[0], s[1].manifest.tool_id))
    out = []
    for _, tool in scored[:max(1, min(int(limit), 30))]:
        m = tool.manifest
        out.append({'tool_id': m.tool_id, 'name': m.display_name,
                    'category': next((names[c].split(' ', 1)[-1] for c in m.catalog_ids if c in names), None),
                    'purpose': (m.description or '').split('. ')[0][:200], 'side_effects': m.side_effects})
    return {'query': query, 'tools': out, 'note': 'Call describe_tool for the inputs, outputs and settings of a tool before using it.'}


def _setting(name: str, schema: dict, required: bool) -> dict:
    out = {'name': name, 'required': required}
    for key in ('type', 'enum', 'default', 'description', 'minimum', 'maximum', 'minLength', 'items', 'oneOf', 'anyOf'):
        if key in schema:
            out[key] = schema[key]
    return out


def describe_tool(ctx: Context, tool_id: str) -> dict:
    reg = default_registry()
    try:
        tool = reg.get(tool_id)
    except Exception:
        raise EngineError(f'No installed tool {tool_id!r}; use find_tools to discover tool ids', code='unknown-tool')
    m = tool.manifest
    schema = m.config_schema or {}
    required = set(schema.get('required', []))
    names = {e['id']: e for e in catalog.entries()}
    return {
        'tool_id': m.tool_id, 'version': m.version, 'name': m.display_name,
        'catalog': [{'id': c, 'name': names[c]['name'], 'category': names[c]['category'],
                     'verification_status': names[c]['verification_status']} for c in m.catalog_ids if c in names],
        'purpose': m.description,
        'inputs': [a.to_dict() if hasattr(a, 'to_dict') else a for a in m.inputs],
        'outputs': [a.to_dict() if hasattr(a, 'to_dict') else a for a in m.outputs],
        'settings': [_setting(k, v, k in required) for k, v in schema.get('properties', {}).items()],
        'config_schema': schema,
        'execution': {'side_effects': m.side_effects, 'deterministic': m.deterministic,
                      'dependencies': list(m.dependencies or [])},
        'usage_notes': USAGE_NOTES.get(m.tool_id, []),
        'node_example': {'tool_id': m.tool_id, 'config': {k: _placeholder(schema['properties'][k]) for k in required
                                                         if k in schema.get('properties', {})}},
    }


def _placeholder(schema: dict):
    if 'default' in schema:
        return schema['default']
    if 'enum' in schema:
        return schema['enum'][0]
    kind = schema.get('type')
    kind = kind[0] if isinstance(kind, list) else kind
    return {'string': '<text>', 'integer': 0, 'number': 0, 'boolean': False, 'array': [], 'object': {}}.get(kind, None)


# ---------------------------------------------------------------------------------------- files and data
def list_files(ctx: Context, path: str = '.', pattern: str = '*') -> dict:
    folder = ctx.path(path)
    if not folder.is_dir():
        raise EngineError(f'{folder} is not a folder', code='not-found')
    entries = []
    for p in sorted(folder.glob(pattern))[:MAX_LIST]:
        st = p.stat()
        entries.append({'name': p.name, 'path': str(p), 'kind': 'folder' if p.is_dir() else 'file',
                        'bytes': None if p.is_dir() else st.st_size,
                        'modified': _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')})
    return {'folder': str(folder), 'entries': entries, 'truncated': len(entries) == MAX_LIST}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def profile_file(ctx: Context, path: str, format: str = 'auto') -> dict:
    """Reads a file with the Input Data and Auto Field kernels and profiles it natively."""
    from .executor import run_workflow
    target = ctx.path(path)
    if not target.is_file():
        raise EngineError(f'{target} does not exist', code='not-found')
    doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
        {'id': 'read', 'tool': 'core.input_data', 'config': {'path': str(target), 'format': format}},
        {'id': 'typed', 'tool': 'core.auto_field', 'config': {}}],
        'edges': [{'from': {'node': 'read', 'anchor': 'output'}, 'to': {'node': 'typed', 'anchor': 'input'}}]}
    result = run_workflow(doc, base_dir=ctx.base_dir)
    if result.status != 'succeeded':
        raise EngineError(f'Could not read {target.name}: {(result.error or {}).get("message", result.status)}', code='read-failed')
    raw, typed = result.outputs['read']['output'], result.outputs['typed']['output']
    rows = list(typed.rows())
    fields = []
    raw_types = {f.name: f.type for f in raw.schema}
    for i, f in enumerate(typed.schema):
        values = [r[i] for r in rows]
        present = [v for v in values if v is not None and v != '']
        info = {'name': f.name, 'type_as_read': raw_types.get(f.name), 'suggested_type': f.type,
                'nulls': len(values) - len(present), 'distinct': len({str(v) for v in present})}
        if present and isinstance(present[0], (int, float)) and not isinstance(present[0], bool):
            nums = [float(v) for v in present]
            info.update(min=min(nums), max=max(nums), mean=sum(nums) / len(nums))
        elif present:
            info['examples'] = [str(v)[:60] for v in present[:3]]
        fields.append(info)
    return {'path': str(target), 'bytes': target.stat().st_size, 'sha256': _sha256(target), 'rows': len(rows),
            'fields': fields, 'sample_rows': [[None if v is None else str(v)[:80] for v in r] for r in rows[:5]],
            'note': 'Statistics were computed by Flowbench. Build workflows on this file rather than reading its rows.'}


# ---------------------------------------------------------------------------------------- results
AGGREGATES = {'sum': 'sum', 'avg': 'mean', 'mean': 'mean', 'min': 'min', 'max': 'max', 'count': 'count',
              'count_distinct': 'count_distinct', 'std_dev': 'stddev', 'median': 'approximate_median'}


def _run(ctx: Context, run_id: str) -> dict:
    run = ctx.runs.get(run_id)
    if not run:
        raise EngineError('Unknown run id; use the run_id returned when the workflow ran', code='unknown-run')
    if run.get('status') == 'running':
        raise EngineError('The run has not finished', code='run-active')
    return run


def query_results(ctx: Context, run_id: str, node_id: str, anchor: str = 'output', columns=None, group_by=None,
                  aggregates=None, sort=None, limit: int | None = DEFAULT_ROWS) -> dict:
    """Aggregates or pages a full run output natively, so only the answer reaches the model."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    run = _run(ctx, run_id)
    source = run.get('artifacts', {}).get((node_id, anchor))
    if source is None:
        raise EngineError(f'No full output for node {node_id!r} anchor {anchor!r} in this run', code='result-unavailable')
    table = pq.read_table(source)
    names = table.column_names
    missing = [c for c in (columns or []) + (group_by or []) + [a['field'] for a in (aggregates or [])] if c not in names]
    if missing:
        raise EngineError(f'Unknown field(s) {missing}; fields are {names[:80]}', code='unknown-field')
    total_rows = table.num_rows
    if aggregates:
        specs = []
        for a in aggregates:
            op = AGGREGATES.get(a['op'])
            if not op:
                raise EngineError(f"Unsupported op {a['op']!r}; use one of {sorted(AGGREGATES)}", code='bad-aggregate')
            specs.append((a['field'], op))
        if group_by:
            table = table.group_by(group_by).aggregate(specs)
        else:
            import pyarrow as pa
            table = pa.table({f'{f}_{op}': [getattr(pc, op)(table[f]).as_py()] for f, op in specs})
    elif columns:
        table = table.select(columns)
    if sort:
        table = table.sort_by([(s['field'], 'descending' if s.get('descending') else 'ascending') for s in sort])
    rows = (table if limit is None else table.slice(0, max(1, int(limit)))).to_pylist()
    return {'run_id': run_id, 'node_id': node_id, 'anchor': anchor, 'source_rows': total_rows,
            'result_rows': table.num_rows, 'returned_rows': len(rows), 'fields': table.column_names,
            'rows': [{k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)) for k, v in r.items()} for r in rows],
            'computed_by': 'flowbench'}


def run_evidence(ctx: Context, run_id: str) -> dict:
    run = _run(ctx, run_id)
    result = run.get('result') or {}
    manifest = result.get('manifest') or {}
    inputs = []
    for node_id, node in (manifest.get('nodes') or {}).items():
        path = (node.get('config') or {}).get('path') if isinstance(node.get('config'), dict) else None
        if path and Path(path).is_file():
            inputs.append({'node_id': node_id, 'path': path, 'sha256': _sha256(Path(path))})
    return {'run_id': run_id, 'status': run.get('status'), 'error': result.get('error'),
            'workflow_digest': manifest.get('workflow_digest'), 'engine_version': manifest.get('engine_version'),
            'nodes': {k: {'tool': v.get('tool'), 'tool_version': v.get('tool_version'),
                          'outputs': {a: {'rows': o.get('rows'), 'digest': o.get('digest')} for a, o in (v.get('outputs') or {}).items()}}
                      for k, v in (manifest.get('nodes') or {}).items()},
            'artifacts': [{'node_id': n, 'anchor': a} for (n, a) in run.get('artifacts', {})],
            'input_files': inputs}


# ---------------------------------------------------------------------------------------- acquisition
def download_file(ctx: Context, url: str, filename: str | None = None, folder: str = 'data') -> dict:
    """Downloads a file into the workspace and records where it came from beside it."""
    import requests
    if not re.match(r'^https?://', url, re.I):
        raise EngineError('Only http:// and https:// URLs can be downloaded', code='bad-url')
    target_dir = ctx.path(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=(15, 300), headers={'User-Agent': f'Flowbench/{__version__}'}) as r:
        if r.status_code >= 400:
            raise EngineError(f'The server returned HTTP {r.status_code} for {url}', code='http-error')
        name = filename or _filename(r, url)
        if not re.fullmatch(r'[\w.\- ()]{1,200}', name) or name.startswith('.'):
            raise EngineError(f'Unsafe file name {name!r}; pass filename', code='bad-filename')
        target = target_dir / name
        tmp = target.with_name(f'.{name}.part')
        size = 0
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(1 << 20):
                size += len(chunk)
                if size > DOWNLOAD_LIMIT:
                    f.close(); tmp.unlink(missing_ok=True)
                    raise EngineError('The download is larger than 1 GB', code='too-large')
                f.write(chunk)
        tmp.replace(target)
        provenance = {'url': url, 'final_url': r.url, 'http_status': r.status_code,
                      'content_type': r.headers.get('Content-Type'), 'last_modified': r.headers.get('Last-Modified'),
                      'bytes': size, 'sha256': _sha256(target),
                      'downloaded_at': _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds'),
                      'tool': f'flowbench-agent/{__version__}'}
    (target.parent / f'{target.name}.source.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
    return {'path': str(target), **provenance}


def _filename(response, url: str) -> str:
    cd = response.headers.get('Content-Disposition', '')
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    name = m.group(1) if m else url.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
    return re.sub(r'[^\w.\- ()]', '_', name)[:200] or 'download'


# ---------------------------------------------------------------------------------------- registry
OPS: dict[str, Op] = {op.name: op for op in [
    Op('find_tools', 'Search the installed Flowbench tools by task, method, data type or name, for example "average by group", "download csv", "join two tables". Returns tool ids with a one-line purpose.',
       _obj({'query': {'type': 'string', 'minLength': 1}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 30}}, ['query']), find_tools),
    Op('describe_tool', 'Full specification of one tool: purpose, input and output anchors, every setting with type, default and allowed values, side effects and usage notes. Call before adding a tool to a workflow.',
       _obj({'tool_id': {'type': 'string', 'minLength': 1}}, ['tool_id']), describe_tool),
    Op('list_files', 'List files in the workspace folder or a sub-folder (relative paths resolve against the workspace).',
       _obj({'path': {'type': 'string'}, 'pattern': {'type': 'string'}}), list_files),
    Op('profile_file', 'Read a data file with Flowbench and return its fields, suggested types, null counts, distinct counts, numeric ranges and five sample rows. Use this instead of reading the file yourself.',
       _obj({'path': {'type': 'string', 'minLength': 1}, 'format': {'enum': ['auto', 'csv', 'tsv', 'delimited', 'json', 'jsonl', 'parquet', 'arrow', 'yxdb']}}, ['path']), profile_file),
    Op('query_results', 'Compute on the full output of a finished run inside Flowbench: group_by with aggregates (sum, avg, min, max, count, count_distinct, std_dev, median), selected columns, sorting, and the rows you ask for (100 by default; set limit, or null for all). Prefer aggregating here over reading raw rows.',
       _obj({'run_id': {'type': 'string'}, 'node_id': {'type': 'string'}, 'anchor': {'type': 'string'},
             'columns': {'type': 'array', 'items': {'type': 'string'}}, 'group_by': {'type': 'array', 'items': {'type': 'string'}},
             'aggregates': {'type': 'array', 'items': _obj({'field': {'type': 'string'}, 'op': {'enum': sorted(AGGREGATES)}}, ['field', 'op'])},
             'sort': {'type': 'array', 'items': _obj({'field': {'type': 'string'}, 'descending': {'type': 'boolean'}}, ['field'])},
             'limit': {'type': ['integer', 'null'], 'minimum': 1}}, ['run_id', 'node_id']), query_results),
    Op('run_evidence', 'Execution evidence for a run: status, workflow digest, tool versions, output row counts and digests, input file hashes and artifact references. Cite this when reporting results.',
       _obj({'run_id': {'type': 'string'}}, ['run_id']), run_evidence),
    Op('download_file', 'Download a file over HTTP(S) into the workspace (data/ by default) and save its provenance (URL, time, size, SHA-256) beside it as <file>.source.json. Use for public datasets; then build the workflow on the saved file.',
       _obj({'url': {'type': 'string', 'minLength': 8}, 'filename': {'type': 'string'}, 'folder': {'type': 'string'}}, ['url']),
       download_file, read_only=False, open_world=True, writes_files=True, permissions=('network', 'file_writes')),
]}


def tools_list() -> list[dict]:
    return [op.spec() for op in OPS.values()]


def call(name: str, arguments: dict, ctx: Context, ops: dict[str, Op] | None = None) -> dict:
    import jsonschema
    op = (OPS if ops is None else ops).get(name)
    if op is None:
        raise EngineError(f'Unknown agent operation {name!r}', code='unknown-operation')
    try:
        jsonschema.validate(arguments or {}, op.schema)
    except jsonschema.ValidationError as e:
        where = '/'.join(str(p) for p in e.absolute_path) or 'arguments'
        raise EngineError(f'Invalid arguments at {where}: {e.message}', code='invalid-arguments')
    missing = [p for p in op.permissions if p not in ctx.permissions]
    if missing:
        raise PermissionRequired(name, missing)
    return op.handler(ctx, **(arguments or {}))


class PermissionRequired(EngineError):
    code = 'permission-required'

    def __init__(self, op: str, permissions: list[str]):
        super().__init__(f'{op} needs permission: {", ".join(permissions)}')
        self.permissions = permissions

    def to_dict(self) -> dict:
        return {**super().to_dict(), 'permissions': self.permissions}


def call_envelope(name: str, arguments: dict, ctx: Context, ops: dict[str, Op] | None = None) -> dict:
    """MCP-style outcome: {ok, result} or {ok: false, error: {code, message, ...}} for the model to act on."""
    try:
        return {'ok': True, 'operation': name, 'result': call(name, arguments, ctx, ops)}
    except EngineError as e:
        return {'ok': False, 'operation': name, 'error': e.to_dict()}
    except Exception as e:  # report, never crash the engine
        return {'ok': False, 'operation': name, 'error': {'code': 'operation-failed', 'message': f'{type(e).__name__}: {e}'}}
