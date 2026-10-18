"""Execution contracts for every installed tool, generated from the implementation and curated where code
cannot say it. Served by describe_tool (one tool at a time) and formula_reference.

Generated: settings, defaults and constraints (config schema), anchors, side effects, streaming support,
dependencies and first-use downloads (manifest and kernel), the error conditions a kernel raises (read
from its source), limitations and verification status (contract ledger), and tested examples. Curated:
the semantics in SEMANTICS below, each rule checked by tests/test_tool_specs.py against the engine.
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_ERRORS = {'ConfigError': 'configuration', 'DataError': 'data', 'SchemaError': 'schema', 'EngineError': 'execution'}

SEMANTICS = {
    'core.summarize': {
        'operations': {
            'group_by': 'Groups records by the field; groups are output in ascending key order (sort_groups true) or first-occurrence order. Null is a group key of its own.',
            'count': 'Number of records in the group, nulls included (int64).',
            'count_non_null / count_null': 'Records whose value is / is not null (int64).',
            'count_blank / count_non_blank': 'Records whose value is null or empty text / neither (int64).',
            'count_distinct / count_distinct_non_null': 'Distinct values, counting null as a value / ignoring nulls (int64).',
            'sum': 'Sum of non-null values. Integer and bool fields give int64, fixed decimal keeps its scale, other numbers give double; text fields are a schema error.',
            'avg / median': 'Mean / median of non-null values (double); null when the group has none.',
            'std_dev / variance': 'Sample (n-1) estimates of non-null values (double); 0 for a single value.',
            'min / max': 'Smallest / largest non-null value, keeping the field type; values must be comparable.',
            'first / last': 'The first / last value in input order, null included.',
            'mode': 'Most frequent non-null value; the earliest among ties.',
            'concat': 'Non-null values joined with separator (default ","), as text.',
            'longest / shortest': 'The longest / shortest non-null value as text; the first among equal lengths.',
        },
        'output_names': 'Each action\'s output, defaulting to <Op>_<Field> (for example Sum_Sales); group_by keeps the field name.',
        'nulls': 'Every operation except count, count_null, count_distinct, count_blank, first and last ignores nulls.',
    },
    'core.formula': {
        'language': 'Call formula_reference for the expression language: operators, keywords, functions and their arguments.',
        'order': 'Formulas run in order; a later formula sees the fields earlier ones created or changed.',
        'nulls': 'Arithmetic with null gives null; text + null treats null as empty text; comparing null with a value is false; IsNull([Field]) tests for null.',
        'division': 'Division by zero gives null rather than an error.',
        'types': 'Each formula names its output type; the result is converted to it, and a value that cannot convert follows the conversion error policy.',
    },
    'core.filter': {
        'routing': 'Records where the condition is true go to the true output; false and null go to the false output.',
        'language': 'Custom conditions use the formula language (formula_reference).',
    },
    'core.join': {
        'outputs': 'joined: matching left and right records; left_unjoined / right_unjoined: records with no match on the other side.',
        'nulls': 'Null keys match each other.',
        'order': 'Output is ordered by key, right-record-major within a key.',
    },
    'core.union': {'fields': 'Stacks inputs by field name (or position); field types are widened to a common type; missing fields are null.'},
    'core.unique': {'outputs': 'unique: the first record for each key combination, in input order; duplicates: every later one.'},
    'core.sort': {'order': 'Stable multi-key sort; nulls sort first ascending and last descending.'},
    'core.multi_row_formula': {'references': 'Use [Row-1:Field] and [Row+1:Field] to read neighbouring records; outside the data they are null (or zero/empty per setting).'},
    'core.input_data': {'types': 'Delimited text arrives as text unless type inference or declared fields are set; Excel/ODS cells keep their types.'},
}


@lru_cache(maxsize=1)
def _contracts() -> dict:
    out = {}
    for path in (ROOT / 'tool-contracts').glob('*/*.contract.json'):
        try:
            c = json.loads(path.read_text(encoding='utf-8'))
            out[c['id']] = c
        except (OSError, ValueError, KeyError):
            continue
    return out


def _message(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                parts.append('{' + ast.unparse(v.value) + '}')
        return ''.join(parts)
    return None


def _sources(tool_cls) -> list[ast.AST]:
    """The class body plus module-level helpers it calls, two levels deep."""
    module = inspect.getmodule(tool_cls)
    try:
        trees = [ast.parse(textwrap.dedent(inspect.getsource(tool_cls)))]
        module_tree = ast.parse(inspect.getsource(module))
    except (OSError, TypeError):
        return []
    helpers = {n.name: n for n in module_tree.body if isinstance(n, ast.FunctionDef)}
    frontier, seen = [trees[0]], set()
    for _ in range(2):
        names = {n.id for tree in frontier for n in ast.walk(tree) if isinstance(n, ast.Name)}
        frontier = [helpers[n] for n in names if n in helpers and n not in seen]
        seen.update(n.name for n in frontier)
        trees += frontier
    return trees


@lru_cache(maxsize=None)
def error_conditions(tool_cls) -> list[dict]:
    """Errors the kernel raises, read from its class source and the helpers it calls: kind and message template."""
    seen, out = set(), []
    for node in (n for tree in _sources(tool_cls) for n in ast.walk(tree)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        name = getattr(node.exc.func, 'id', None) or getattr(node.exc.func, 'attr', None)
        if name not in _ERRORS or not node.exc.args:
            continue
        message = _message(node.exc.args[0])
        if message and (name, message) not in seen:
            seen.add((name, message))
            out.append({'kind': _ERRORS[name], 'message': message})
    return out


def _expression_settings(schema: dict, path='') -> list[str]:
    found = []
    for key, sub in (schema or {}).get('properties', {}).items():
        here = f'{path}.{key}' if path else key
        if key in ('expression', 'expressions', 'condition', 'when') or 'expression' in str(sub.get('description', '')).lower():
            found.append(here)
        if sub.get('type') == 'object':
            found += _expression_settings(sub, here)
        if isinstance(sub.get('items'), dict):
            found += _expression_settings(sub['items'], here + '[]')
    return found


def contract(tool) -> dict:
    m = tool.manifest
    schema = m.config_schema or {}
    contracts = [_contracts().get(c) for c in m.catalog_ids]
    limitations = []
    for c in filter(None, contracts):
        limitations += [u if isinstance(u, str) else json.dumps(u) for u in c.get('unresolved', [])]
        if c.get('verification_status') not in ('verified',):
            limitations.append(f'{c["name"]}: verification {c.get("verification_status")} against the reference product')
    try:
        streams = bool(tool.can_stream(tool.normalize_config({})))
    except Exception:
        streams = None
    downloads = []
    if hasattr(tool, 'downloads'):
        try:
            downloads = [d if isinstance(d, str) else d.get('id') for d in tool.downloads({}) or []]
        except Exception:
            downloads = []
    out = {
        'errors': error_conditions(type(tool)),
        'execution': {'side_effects': m.side_effects, 'deterministic': m.deterministic, 'streaming': streams,
                      'dependencies': list(m.dependencies or []), 'first_use_downloads': downloads,
                      'recovery': 'Side-effect-free: rerunning gives the same result.' if m.side_effects == 'none' and m.deterministic
                      else 'Has side effects or is non-deterministic: check what it already did before running it again.'},
        'limitations': limitations,
    }
    expressions = _expression_settings(schema)
    if expressions:
        out['expression_settings'] = expressions
        out['expression_language'] = 'Call formula_reference for operators, keywords and the function list.'
    if m.tool_id in SEMANTICS:
        out['semantics'] = SEMANTICS[m.tool_id]
    provider = provider_requirements(schema)
    if provider:
        out['provider'] = provider
    return out


def provider_requirements(schema: dict) -> dict | None:
    """For provider-backed tools: which providers can serve the node, whether each is ready here, and how to set it up."""
    props = (schema or {}).get('properties', {})
    allowed = (props.get('provider') or {}).get('enum')
    if not allowed:
        return None
    from . import providers
    connection_kind = (props.get('connection_id') or {}).get('x-flowbench-connection')
    options = []
    for pid in allowed:
        try:
            st = providers.status(providers.get(pid))
        except Exception:  # an unknown provider id is a manifest bug; report it rather than hide the tool
            options.append({'provider': pid, 'ready': False, 'missing': ['provider is not registered']})
            continue
        options.append({'provider': pid, 'name': st['name'], 'kind': st['kind'], 'ready': st['ready'],
                        'missing': [c['detail'] for c in st['checks'] if not c['ok']], 'setup': st['setup'],
                        'results': st['semantics'], 'verification': st['verification']})
    note = ('Set provider to one of these ids; Flowbench never picks one. ')
    if connection_kind:
        note += (f'connection_id must name a saved {"database" if connection_kind.startswith("database") else "Web/API"} '
                 'connection: call list_connections. If none fits, tell the user what to add (see setup); never ask for '
                 'or handle secrets yourself.')
    return {'options': options, 'connection': connection_kind, 'note': note}


def formula_reference() -> dict:
    from .formula import _KEYWORDS, FUNCTIONS
    functions = []
    for name in sorted(FUNCTIONS):
        spec = FUNCTIONS[name]
        functions.append({'name': name, 'min_args': spec.min_args, 'max_args': spec.max_args,
                          'returns': spec.returns if isinstance(spec.returns, str) else 'depends on the arguments'})
    return {
        'fields': '[Field Name] reads a field; [Row-1:Field] and [Row+1:Field] read neighbouring records in Multi-Row Formula.',
        'literals': 'Numbers (1, 2.5, 1e3); text in "double" or \'single\' quotes (double the quote to escape it); TRUE, FALSE, NULL().',
        'operators': {'arithmetic': ['+', '-', '*', '/', '%'], 'comparison': ['=', '==', '!=', '<>', '<', '<=', '>', '>='],
                      'logical': ['AND', '&&', 'OR', '||', 'NOT', '!'], 'membership': '[Field] IN ("a", "b")', 'text': '+ joins text'},
        'conditionals': ['IF c THEN a ELSEIF c2 THEN b ELSE d ENDIF', 'IIF(c, a, b)', 'SWITCH(value, default, case1, result1, ...)'],
        'comments': ['// to end of line', '/* block */'],
        'keywords': sorted(_KEYWORDS),
        'null_rules': SEMANTICS['core.formula']['nulls'] + ' ' + SEMANTICS['core.formula']['division'],
        'functions': functions,
        'note': 'Function names are case-insensitive. Only the functions listed here exist.',
    }
