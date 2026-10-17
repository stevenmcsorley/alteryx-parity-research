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
from .examples import examples_for
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
    'sentiment': 'sentiment', 'ocr': 'image to text', 'pdf': 'pdf', 'excel': 'excel xlsx worksheet input data', 'xlsx': 'excel worksheet input data', 'ods': 'opendocument worksheet input data',
    'spreadsheet': 'excel worksheet input data', 'sheet': 'worksheet input data', 'csv': 'input data csv',
    'read': 'input data', 'load': 'input data', 'import': 'input data', 'write': 'output data', 'save': 'output data',
    'export': 'output data', 'report': 'report render', 'database': 'database db input',
}

# Facts an AI needs that the configuration schema alone does not say. Keep these true to the kernel.
USAGE_NOTES = {
    'core.input_data': ['Delimited text (CSV/TSV) fields arrive as text (v_wstring). Add Auto Field, or Select with explicit types, before numeric work such as Summarize sums or Formula arithmetic.',
                        'Excel and ODS cells keep their types. Use inspect_spreadsheet first, then set sheet and skip_rows (rows above the header) or range such as A6:P400; with multi-row headers, read with has_header false and rename with Select.',
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

    def __init__(self, base_dir, permissions=None, runs=None, service=None):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.permissions = set(permissions or ())
        self.runs = runs if runs is not None else {}
        self.service = service

    def workspace_path(self, raw: str) -> Path:
        """A path that must stay inside the workspace folder (for anything that writes)."""
        p = self.path(raw)
        if p != self.base_dir and self.base_dir not in p.parents:
            raise EngineError(f'{p} is outside the workspace {self.base_dir}; writes stay inside the workspace', code='outside-workspace')
        return p

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


def _example_view(example: dict, full: bool) -> dict:
    inputs = {}
    for anchor, table in example['inputs'].items():
        rows = table['rows'] if full else table['rows'][:5]
        inputs[anchor] = {'fields': table['fields'], 'rows': rows, 'row_count': len(table['rows'])}
    return {'config': example['config'], 'inputs': inputs, 'expected_outputs': example['expected_outputs'],
            'checked': 'replayed against this kernel by tests/test_tool_examples.py'}


def describe_tool(ctx: Context, tool_id: str, full: bool = False) -> dict:
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
        'examples': [_example_view(e, full) for e in examples_for(m.tool_id)],
        'related_tools': _related(reg, m, names),
        'node_example': {'tool_id': m.tool_id, 'config': {k: _placeholder(schema['properties'][k]) for k in required
                                                         if k in schema.get('properties', {})}},
    }


def _related(reg, manifest, names) -> list[str]:
    categories = {names[c]['category'] for c in manifest.catalog_ids if c in names}
    cmap = reg.catalog_map()
    return sorted({cmap[e['id']] for e in names.values() if e['category'] in categories and e['id'] in cmap
                   and cmap[e['id']] != manifest.tool_id})


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


def inspect_spreadsheet(ctx: Context, path: str, preview_rows: int = 15) -> dict:
    """Every worksheet with its size and first rows, to choose sheet, skip_rows and range for Input Data."""
    from .spreadsheet import describe
    target = ctx.path(path)
    if not target.is_file():
        raise EngineError(f'{target} does not exist', code='not-found')
    if target.suffix.lower() not in ('.xlsx', '.xlsm', '.ods'):
        raise EngineError('inspect_spreadsheet reads .xlsx, .xlsm and .ods files', code='bad-format')
    return {'path': str(target), 'sheets': describe(target, preview_rows),
            'note': 'Rows are 1-based. Configure Input Data with sheet, plus skip_rows (rows above the header) or range (e.g. A6:P400 with has_header false).'}


def profile_file(ctx: Context, path: str, format: str = 'auto', sheet: str | None = None, skip_rows: int = 0,
                 range: str | None = None, has_header: bool = True) -> dict:
    """Reads a file with the Input Data and Auto Field kernels and profiles it natively."""
    from .executor import run_workflow
    target = ctx.path(path)
    if not target.is_file():
        raise EngineError(f'{target} does not exist', code='not-found')
    read_cfg = {'path': str(target), 'format': format, 'skip_rows': skip_rows, 'has_header': has_header}
    if sheet is not None:
        read_cfg['sheet'] = sheet
    if range is not None:
        read_cfg['range'] = range
    doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': [
        {'id': 'read', 'tool': 'core.input_data', 'config': read_cfg},
        {'id': 'typed', 'tool': 'core.auto_field', 'config': {}}],
        'edges': [{'from': {'node': 'read', 'anchor': 'output'}, 'to': {'node': 'typed', 'anchor': 'input'}}]}
    result = run_workflow(doc, base_dir=ctx.base_dir)
    if result.status != 'succeeded':
        raise EngineError(f'Could not read {target.name}: {(result.error or {}).get("message", result.status)}', code='read-failed')
    raw, typed = result.outputs['read']['output'], result.outputs['typed']['output']
    rows = list(typed.rows())
    fields = []
    raw_types = {f.name: f.type.value for f in raw.schema}
    for i, f in enumerate(typed.schema):
        values = [r[i] for r in rows]
        present = [v for v in values if v is not None and v != '']
        info = {'name': f.name, 'type_as_read': raw_types.get(f.name), 'suggested_type': f.type.value,
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
    reg = default_registry()
    base = Path(run.get('base_dir') or ctx.base_dir)
    for node in (run.get('workflow') or {}).get('nodes', []):
        try:
            reads = reg.get(node.get('tool', '')).manifest.side_effects == 'reads-files'
        except Exception:
            reads = False
        raw = (node.get('config') or {}).get('path')
        if reads and isinstance(raw, str):
            p = Path(raw).expanduser()
            p = (p if p.is_absolute() else base / p).resolve()
            if p.is_file():
                inputs.append({'node_id': node.get('id'), 'tool': node.get('tool'), 'path': str(p), 'sha256': _sha256(p),
                               'bytes': p.stat().st_size})
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


# ---------------------------------------------------------------------------------------- local environment
def read_text_file(ctx: Context, path: str, encoding: str = 'utf-8', start_line: int = 1, line_count: int | None = None) -> dict:
    target = ctx.path(path)
    if not target.is_file():
        raise EngineError(f'{target} does not exist', code='not-found')
    try:
        lines = target.read_text(encoding=encoding).splitlines()
    except UnicodeDecodeError:
        raise EngineError(f'{target.name} is not {encoding} text; use profile_file for data files', code='not-text') from None
    chosen = lines[start_line - 1:] if line_count is None else lines[start_line - 1:start_line - 1 + line_count]
    return {'path': str(target), 'total_lines': len(lines), 'start_line': start_line, 'text': '\n'.join(chosen)}


def write_text_file(ctx: Context, path: str, text: str, overwrite: bool = False) -> dict:
    target = ctx.workspace_path(path)
    if target.exists() and not overwrite:
        raise EngineError(f'{target} exists; pass overwrite true to replace it', code='exists')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')
    return {'path': str(target), 'bytes': target.stat().st_size, 'sha256': _sha256(target)}


def make_folder(ctx: Context, path: str) -> dict:
    target = ctx.workspace_path(path)
    target.mkdir(parents=True, exist_ok=True)
    return {'path': str(target)}


def extract_archive(ctx: Context, path: str, folder: str | None = None) -> dict:
    import tarfile
    import zipfile
    source = ctx.path(path)
    if not source.is_file():
        raise EngineError(f'{source} does not exist', code='not-found')
    dest = ctx.workspace_path(folder or str(source.with_suffix('')).removesuffix('.tar'))
    dest.mkdir(parents=True, exist_ok=True)

    def safe(name: str) -> Path:
        out = (dest / name).resolve()
        if out != dest and dest not in out.parents:
            raise EngineError(f'Archive entry {name!r} would be written outside {dest}', code='unsafe-archive')
        return out
    files = []
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                target = safe(info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, 'wb') as out:
                    out.write(src.read())
                files.append(str(target))
    elif tarfile.is_tarfile(source):
        with tarfile.open(source) as t:
            for member in t.getmembers():
                if not (member.isfile() or member.isdir()):
                    continue  # links and devices are not extracted
                target = safe(member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(member) as src, open(target, 'wb') as out:
                    out.write(src.read())
                files.append(str(target))
    else:
        raise EngineError(f'{source.name} is not a zip or tar archive', code='bad-archive')
    return {'folder': str(dest), 'files': files}


def _snapshot(folder: Path) -> dict:
    out = {}
    for i, p in enumerate(folder.rglob('*')):
        if i > 20000:
            break
        if p.is_file() and '.flowbench' not in p.parts:
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def _execute(ctx: Context, argv: list[str], cwd: Path, timeout_seconds: int, stdin: str | None = None) -> dict:
    import subprocess
    import time
    before = _snapshot(ctx.base_dir)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout_seconds)
    except FileNotFoundError:
        raise EngineError(f'Program not found: {argv[0]}', code='not-found') from None
    except subprocess.TimeoutExpired:
        raise EngineError(f'The command did not finish within {timeout_seconds} s and was stopped', code='timeout') from None
    after = _snapshot(ctx.base_dir)
    changed = sorted(p for p, sig in after.items() if before.get(p) != sig)
    return {'exit_code': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr,
            'duration_seconds': round(time.monotonic() - started, 3), 'files_written': changed}


def run_command(ctx: Context, argv: list[str], cwd: str = '.', timeout_seconds: int = 600, stdin: str | None = None) -> dict:
    folder = ctx.path(cwd)
    if not folder.is_dir():
        raise EngineError(f'{folder} is not a folder', code='not-found')
    return {'argv': argv, 'cwd': str(folder), **_execute(ctx, argv, folder, timeout_seconds, stdin)}


def run_python(ctx: Context, code: str, timeout_seconds: int = 600) -> dict:
    """Runs a script with Flowbench's own Python (pandas, numpy, pyarrow, scipy, ...) in the workspace.
    The script is kept under .flowbench/scripts/ so the step is recorded and can be rerun."""
    import sys
    folder = ctx.base_dir / '.flowbench' / 'scripts'
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / f'{_dt.datetime.now().strftime("%Y%m%d-%H%M%S")}-{hashlib.sha256(code.encode()).hexdigest()[:10]}.py'
    script.write_text(code, encoding='utf-8')
    return {'script': str(script), 'python': sys.executable, **_execute(ctx, [sys.executable, str(script)], ctx.base_dir, timeout_seconds)}


# ---------------------------------------------------------------------------------------- databases
def _db(ctx: Context, method: str, **params):
    if ctx.service is None:
        raise EngineError('Database access is unavailable here', code='unavailable')
    return ctx.service.handle(method, params)


def list_connections(ctx: Context) -> dict:
    return {'connections': _db(ctx, 'database.connections.list'),
            'note': 'Saved connections only; credentials stay in the system vault.'}


def list_tables(ctx: Context, connection_id: str, schema: str | None = None) -> dict:
    if schema is None:
        return {'schemas': _db(ctx, 'database.schemas', connection_id=connection_id)}
    return {'schema': schema, 'tables': _db(ctx, 'database.tables', connection_id=connection_id, schema=schema)}


def describe_table(ctx: Context, connection_id: str, table: str, schema: str | None = None) -> dict:
    return {'schema': schema, 'table': table,
            'columns': _db(ctx, 'database.columns', connection_id=connection_id, schema=schema, table=table)}


def query_database(ctx: Context, connection_id: str, sql: str, limit: int = 100) -> dict:
    """A read-only query for inspection; build a workflow with Database Input to process whole tables."""
    return _db(ctx, 'database.preview', connection_id=connection_id, mode='query', query=sql, limit=limit)


# ---------------------------------------------------------------------------------------- registry
OPS: dict[str, Op] = {op.name: op for op in [
    Op('find_tools', 'Search the installed Flowbench tools by task, method, data type or name, for example "average by group", "download csv", "join two tables". Returns tool ids with a one-line purpose.',
       _obj({'query': {'type': 'string', 'minLength': 1}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 30}}, ['query']), find_tools),
    Op('describe_tool', 'Full specification of one tool: purpose, input and output anchors, every setting with type, default and allowed values, side effects, usage notes, related tools and tested examples (configuration, input and the output it produces). Call before adding a tool to a workflow. Examples show the first five input rows; pass full true for all of them.',
       _obj({'tool_id': {'type': 'string', 'minLength': 1}, 'full': {'type': 'boolean'}}, ['tool_id']), describe_tool),
    Op('list_files', 'List files in the workspace folder or a sub-folder (relative paths resolve against the workspace).',
       _obj({'path': {'type': 'string'}, 'pattern': {'type': 'string'}}), list_files),
    Op('inspect_spreadsheet', 'Show every worksheet of an Excel (.xlsx/.xlsm) or OpenDocument (.ods) file with its size and first rows, so you can choose the sheet and where the table starts. Published statistics usually have notes and multi-row headers above the data.',
       _obj({'path': {'type': 'string', 'minLength': 1}, 'preview_rows': {'type': 'integer', 'minimum': 1}}, ['path']), inspect_spreadsheet),
    Op('profile_file', 'Read a data file with Flowbench and return its fields, suggested types, null counts, distinct counts, numeric ranges and five sample rows. Use this instead of reading the file yourself. For worksheets pass sheet and skip_rows or range, as for Input Data.',
       _obj({'path': {'type': 'string', 'minLength': 1}, 'format': {'enum': ['auto', 'csv', 'tsv', 'delimited', 'json', 'jsonl', 'parquet', 'arrow', 'yxdb', 'xlsx', 'ods']},
             'sheet': {'type': ['string', 'null']}, 'skip_rows': {'type': 'integer', 'minimum': 0},
             'range': {'type': ['string', 'null']}, 'has_header': {'type': 'boolean'}}, ['path']), profile_file),
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
    Op('read_text_file', 'Read a text file (notes, metadata, provenance .source.json, README, SQL, code). Use profile_file for data files.',
       _obj({'path': {'type': 'string', 'minLength': 1}, 'encoding': {'type': 'string'},
             'start_line': {'type': 'integer', 'minimum': 1}, 'line_count': {'type': ['integer', 'null'], 'minimum': 1}}, ['path']), read_text_file),
    Op('write_text_file', 'Write a text file inside the workspace (a report, notes, a CSV you composed, SQL, configuration). Refuses to replace an existing file unless overwrite is true.',
       _obj({'path': {'type': 'string', 'minLength': 1}, 'text': {'type': 'string'}, 'overwrite': {'type': 'boolean'}}, ['path', 'text']),
       write_text_file, read_only=False, writes_files=True, permissions=('file_writes',)),
    Op('make_folder', 'Create a folder inside the workspace, for example for a project\'s outputs.',
       _obj({'path': {'type': 'string', 'minLength': 1}}, ['path']), make_folder, read_only=False, writes_files=True, permissions=('file_writes',)),
    Op('extract_archive', 'Extract a .zip or .tar(.gz) archive (for example a downloaded dataset) into a folder inside the workspace.',
       _obj({'path': {'type': 'string', 'minLength': 1}, 'folder': {'type': 'string'}}, ['path']),
       extract_archive, read_only=False, writes_files=True, permissions=('file_writes',)),
    Op('run_command', 'Run an installed program (argv list, no shell) in the workspace, for example git, docker, curl, ogr2ogr or a converter, and return its exit code, output and the files it wrote. Prefer native Flowbench tools for data processing.',
       _obj({'argv': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}}, 'cwd': {'type': 'string'},
             'timeout_seconds': {'type': 'integer', 'minimum': 1}, 'stdin': {'type': ['string', 'null']}}, ['argv']),
       run_command, read_only=False, open_world=True, writes_files=True, permissions=('processes',)),
    Op('run_python', 'Run a Python script with Flowbench\'s Python (pandas, numpy, pyarrow, scipy, scikit-learn, statsmodels, openpyxl, requests) in the workspace, and return its output and the files it wrote. Use it to prepare data that no native tool can handle, writing results to files that the workflow then reads; the script is kept under .flowbench/scripts/.',
       _obj({'code': {'type': 'string', 'minLength': 1}, 'timeout_seconds': {'type': 'integer', 'minimum': 1}}, ['code']),
       run_python, read_only=False, open_world=True, writes_files=True, permissions=('processes',)),
    Op('list_connections', 'List the saved database connections (name, kind, host, database; never credentials).', _obj({}), list_connections),
    Op('list_tables', 'List the schemas of a saved database connection, or the tables in one schema.',
       _obj({'connection_id': {'type': 'string', 'minLength': 1}, 'schema': {'type': ['string', 'null']}}, ['connection_id']), list_tables),
    Op('describe_table', 'Columns and types of a database table.',
       _obj({'connection_id': {'type': 'string', 'minLength': 1}, 'schema': {'type': ['string', 'null']}, 'table': {'type': 'string', 'minLength': 1}},
            ['connection_id', 'table']), describe_table),
    Op('query_database', 'Run a read-only SQL query on a saved connection and return its rows (100 by default). For analysis, build a workflow with the Database Input tool so Flowbench processes the data.',
       _obj({'connection_id': {'type': 'string', 'minLength': 1}, 'sql': {'type': 'string', 'minLength': 1}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}},
            ['connection_id', 'sql']), query_database),
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
