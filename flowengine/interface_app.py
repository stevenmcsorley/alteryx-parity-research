"""Typed, per-run question binding for Flowbench analytic applications.

This is the native workflow-format contract. It is deliberately separate from
Alteryx XML Interface-tool import, whose Action serialization still needs
reference-backed mappings.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
import re
from pathlib import Path

from .errors import EngineError

_EXTENSION = re.compile(r'^[a-z0-9][a-z0-9_+-]{0,15}$')


QUESTION_TYPES = ('text', 'text-area', 'boolean', 'number', 'choice', 'date', 'file', 'folder', 'multi-choice',
                  'control-parameter', 'tree', 'map', 'connection')
CONNECTION_KINDS = {'database', 'web', 'github', 'smtp'}
SOURCE_LIMIT = 1000


def _check_source(qid, q):
    """Option sources: a data file's field values (Drop Down, List Box, Tree) or a folder tree (Tree)."""
    src = q['source']
    if q['type'] not in ('choice', 'multi-choice', 'tree') or not isinstance(src, dict):
        raise EngineError(f'Question {qid!r}: only Drop Down, List Box, Radio Button and Tree questions can take a source', code='application-invalid')
    if src.get('kind') == 'file':
        fields = src.get('fields') if q['type'] == 'tree' else [src.get('field')]
        if not isinstance(src.get('path'), str) or not src['path'] or not isinstance(fields, list) or not fields \
                or any(not isinstance(f, str) or not f for f in fields):
            raise EngineError(f'Question {qid!r}: a file source needs path and field (or fields for a Tree, one per level)', code='application-invalid')
    elif src.get('kind') == 'folder':
        if q['type'] != 'tree' or not isinstance(src.get('path'), str) or not src['path']:
            raise EngineError(f'Question {qid!r}: a folder source is for Tree questions and needs a path', code='application-invalid')
    else:
        raise EngineError(f'Question {qid!r}: source kind must be file or folder', code='application-invalid')


def _resolve(path, base_dir):
    p = Path(path).expanduser()
    return p if p.is_absolute() or base_dir is None else Path(base_dir) / p


def source_options(q, base_dir=None):
    """The options a question's source yields now (dynamic: re-read whenever the application is described or run)."""
    src = q['source']
    if src['kind'] == 'folder':
        root = _resolve(src['path'], base_dir)
        if not root.is_dir():
            raise EngineError(f"Tree source folder {root} does not exist", code='application-source-missing')
        depth = int(src.get('depth', 4))
        exts = {e.lower().lstrip('.') for e in src.get('extensions') or []}
        out = []
        for p in sorted(root.rglob('*')):
            rel = p.relative_to(root).parts
            if len(rel) > depth or any(part.startswith('.') for part in rel):
                continue
            if p.is_file() and (not exts or p.suffix.lower().lstrip('.') in exts):
                out.append(' > '.join(rel))
            if len(out) >= SOURCE_LIMIT:
                break
        return out
    from .executor import run_workflow
    path = _resolve(src['path'], base_dir)
    doc = {'format': 'flowengine.workflow', 'formatVersion': 1,
           'nodes': [{'id': 'src', 'tool': 'core.input_data', 'config': {'path': str(path)}}], 'edges': []}
    r = run_workflow(doc, base_dir=base_dir or Path.cwd())
    if r.status != 'succeeded':
        raise EngineError(f'Could not read the option source {path}: {(r.error or {}).get("message")}', code='application-source-missing')
    table = r.outputs['src']['output']
    names = table.schema.names
    fields = src['fields'] if q['type'] == 'tree' else [src['field']]
    missing = [f for f in fields if f not in names]
    if missing:
        raise EngineError(f'Option source {path.name} has no field(s) {missing}', code='application-source-missing')
    idx = [names.index(f) for f in fields]
    seen, out = set(), []
    for row in table.rows():
        parts = [str(row[i]).strip() for i in idx]
        if any(not p or p == 'None' for p in parts):
            continue
        label = ' > '.join(parts)
        if label not in seen:
            seen.add(label)
            out.append(label)
        if len(out) >= SOURCE_LIMIT:
            break
    return sorted(out) if src.get('sort', True) else out


def _with_sources(spec, base_dir):
    questions = []
    for q in spec['questions']:
        if q.get('source') is not None:
            q = {**q, 'options': source_options(q, base_dir)}
            if not q['options']:
                raise EngineError(f"Question {q['id']!r}: its source yielded no options", code='application-source-missing')
        questions.append(q)
    return {**spec, 'questions': questions}


def _map_answer(qid, value):
    """Map questions take a GeoJSON geometry (object or text); the answer is stored as GeoJSON text."""
    import json
    try:
        geometry = json.loads(value) if isinstance(value, str) else value
        if not isinstance(geometry, dict) or 'type' not in geometry:
            raise ValueError('not a GeoJSON geometry')
        from shapely.geometry import shape
        shape(geometry)
    except Exception as exc:
        raise EngineError(f'Application input {qid!r} must be a GeoJSON geometry, such as {{"type":"Point","coordinates":[-0.12,51.5]}}',
                          code='application-input-invalid') from exc
    return json.dumps(geometry, separators=(',', ':'))


def _spec(doc):
    spec = (doc.get('metadata') or {}).get('application')
    if spec is None:
        return {'questions': [], 'actions': [], 'errors': []}
    if not isinstance(spec, dict) or set(spec) - {'questions', 'actions', 'errors'}:
        raise EngineError('metadata.application may contain only questions, actions, and errors', code='application-invalid')
    questions, actions, errors = spec.get('questions', []), spec.get('actions', []), spec.get('errors', [])
    if not isinstance(questions, list) or not isinstance(actions, list) or not isinstance(errors, list):
        raise EngineError('Application questions, actions, and errors must be arrays', code='application-invalid')
    ids = set()
    question_types = {}
    for q in questions:
        if not isinstance(q, dict) or q.get('type') not in QUESTION_TYPES:
            raise EngineError('Each application question needs a supported type', code='application-invalid')
        qid = q.get('id')
        if not isinstance(qid, str) or not qid or qid in ids:
            raise EngineError('Application question ids must be unique non-empty strings', code='application-invalid')
        ids.add(qid)
        question_types[qid] = q['type']
        if q['type'] == 'control-parameter' and (not isinstance(q.get('control_field'), str) or not q['control_field'].strip()):
            raise EngineError(f'Control parameter {qid!r} needs a control_field name', code='application-invalid')
        if q.get('secret') and 'default' in q:
            raise EngineError(f'Secret application question {qid!r} must not store a default value', code='application-invalid')
        if q.get('secret') and q['type'] != 'text':
            raise EngineError(f'Secret application question {qid!r} must be text', code='application-invalid')
        if ('mode' in q or 'extensions' in q) and q['type'] != 'file':
            raise EngineError(f'Only File Browse question {qid!r} may set mode or extensions', code='application-invalid')
        if q['type'] == 'file':
            if q.get('mode', 'open') not in ('open', 'save'):
                raise EngineError(f'File Browse question {qid!r} mode must be open or save', code='application-invalid')
            extensions = q.get('extensions')
            if extensions is not None and (
                    not isinstance(extensions, list) or not extensions or len(extensions) > 32
                    or any(not isinstance(ext, str) or not _EXTENSION.match(ext) for ext in extensions)
                    or len(set(extensions)) != len(extensions)):
                raise EngineError(f'File Browse question {qid!r} extensions must be unique lowercase names without dots',
                                  code='application-invalid')
        if q['type'] in ('file', 'folder') and 'default' in q and not isinstance(q['default'], str):
            raise EngineError(f'Browse question {qid!r} default must be a path string', code='application-invalid')
        if q.get('source') is not None:
            _check_source(qid, q)
            continue  # options come from the source when the application is described or bound
        if q['type'] == 'connection' and q.get('kinds') is not None and (
                not isinstance(q['kinds'], list) or any(k not in CONNECTION_KINDS for k in q['kinds'])):
            raise EngineError(f'Connection question {qid!r} kinds must be from {sorted(CONNECTION_KINDS)}', code='application-invalid')
        if q['type'] in ('choice', 'multi-choice') and (
                not isinstance(q.get('options'), list) or not q['options']
                or any(not isinstance(option, str) for option in q['options'])
                or len(set(q['options'])) != len(q['options'])):
            raise EngineError(f'Choice question {qid!r} requires unique string options', code='application-invalid')
        if q['type'] == 'tree':
            options = q.get('options')
            if not isinstance(options, list) or not options or len(options) > 1000 or any(not isinstance(path, str) for path in options):
                raise EngineError(f'Tree question {qid!r} requires 1 to 1000 path options', code='application-invalid')
            normalized = []
            for path in options:
                parts = [part.strip() for part in path.split('>')]
                if not path or len(parts) > 16 or any(not part for part in parts):
                    raise EngineError(f'Tree question {qid!r} has an invalid path; use non-empty labels separated by >', code='application-invalid')
                canonical = ' > '.join(parts)
                if path != canonical:
                    raise EngineError(f'Tree question {qid!r} paths must use spaces around > separators', code='application-invalid')
                normalized.append(canonical)
            path_set = set(normalized)
            has_selectable_parent = False
            for path in normalized:
                parts = [part.strip() for part in path.split('>')]
                if any(' > '.join(parts[:depth]) in path_set for depth in range(1, len(parts))):
                    has_selectable_parent = True
                    break
            if len(path_set) != len(normalized) or has_selectable_parent:
                raise EngineError(f'Tree question {qid!r} has duplicate paths or a selectable parent path', code='application-invalid')
            if 'default' in q and q['default'] not in normalized:
                raise EngineError(f'Tree question {qid!r} default must be one of its paths', code='application-invalid')
    normalized_actions = []
    for a in actions:
        if not isinstance(a, dict) or not isinstance(a.get('node_id'), str):
            raise EngineError('Application action references an unknown question or node', code='application-invalid')
        kind = a.get('kind', 'value')
        if kind not in ('value', 'formula', 'input'):
            raise EngineError('Application action kind must be value, formula or input', code='application-invalid')
        if kind == 'formula':
            if not isinstance(a.get('expression'), str) or not a['expression'].strip():
                raise EngineError('A formula action needs an expression over the question answers', code='application-invalid')
        elif a.get('question') not in ids:
            raise EngineError('Application action references an unknown question or node', code='application-invalid')
        if kind == 'input' and a.get('config_path', ['path']) != ['path']:
            raise EngineError('An input action updates the Input Data path; omit config_path or use ["path"]', code='application-invalid')
        if kind == 'input' and 'config_path' not in a:
            a = {**a, 'config_path': ['path']}
        normalized_actions.append(a)
        when = a.get('when')
        if when is not None:
            if not _valid_predicate(when, ids, question_types):
                raise EngineError('Application action has an invalid conditional predicate', code='application-invalid')
        path = a.get('config_path')
        if not isinstance(path, list) or not path or any(not isinstance(k, str) or k in ('__proto__', 'prototype', 'constructor') for k in path):
            raise EngineError('Application action config_path must be a safe list of property names', code='application-invalid')
    for error in errors:
        if not isinstance(error, dict) or not isinstance(error.get('message'), str) or not error['message'].strip():
            raise EngineError('Each application error needs a non-empty message', code='application-invalid')
        if error.get('when') is not None and not _valid_predicate(error['when'], ids, question_types):
            raise EngineError('Application error has an invalid conditional predicate', code='application-invalid')
    return {'questions': questions, 'actions': normalized_actions, 'errors': errors}


def _valid_predicate(predicate, question_ids, question_types, depth=0):
    if depth > 16 or not isinstance(predicate, dict):
        return False
    op = predicate.get('operator')
    if op in ('all', 'any'):
        children = predicate.get('conditions')
        return (set(predicate) == {'operator', 'conditions'} and isinstance(children, list)
                and bool(children) and len(children) <= 128
                and all(_valid_predicate(child, question_ids, question_types, depth + 1) for child in children))
    if op == 'not':
        return (set(predicate) == {'operator', 'condition'}
                and _valid_predicate(predicate.get('condition'), question_ids, question_types, depth + 1))
    value_ops = ('equals', 'not_equals', 'contains', 'not_contains', 'greater_than', 'greater_than_or_equal',
                 'less_than', 'less_than_or_equal')
    allowed = (*value_ops, 'truthy', 'falsy')
    qid = predicate.get('question')
    if (set(predicate) - {'question', 'operator', 'value'} or qid not in question_ids
            or question_types.get(qid) == 'control-parameter' or op not in allowed
            or ((op in value_ops) != ('value' in predicate))):
        return False
    if op in ('greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal'):
        if question_types.get(qid) == 'number':
            return isinstance(predicate['value'], (int, float)) and not isinstance(predicate['value'], bool) and math.isfinite(predicate['value'])
        if question_types.get(qid) == 'date' and isinstance(predicate['value'], str):
            try:
                dt.date.fromisoformat(predicate['value'])
                return True
            except ValueError:
                return False
        return False
    return True


def _condition_matches(predicate, answers):
    if not predicate:
        return True
    operation = predicate['operator']
    if operation == 'all':
        return all(_condition_matches(child, answers) for child in predicate['conditions'])
    if operation == 'any':
        return any(_condition_matches(child, answers) for child in predicate['conditions'])
    if operation == 'not':
        return not _condition_matches(predicate['condition'], answers)
    actual = answers[predicate['question']]
    if operation == 'truthy':
        return bool(actual)
    if operation == 'falsy':
        return not bool(actual)
    expected = predicate['value']
    if operation in ('equals', 'not_equals'):
        both_numeric = (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                        and isinstance(expected, (int, float)) and not isinstance(expected, bool))
        equal = actual == expected and (both_numeric or type(actual) is type(expected))
        return equal if operation == 'equals' else not equal
    if operation in ('greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal'):
        if actual is None:
            return False
        if isinstance(actual, str):
            try:
                actual = dt.date.fromisoformat(actual)
                expected = dt.date.fromisoformat(expected)
            except (TypeError, ValueError):
                return False
        if type(actual) is not type(expected) or isinstance(actual, bool):
            return False
        return {'greater_than': actual > expected, 'greater_than_or_equal': actual >= expected,
                'less_than': actual < expected, 'less_than_or_equal': actual <= expected}[operation]
    # A multi-select question can control an action by membership; for a
    # scalar question this is deliberately false rather than substring matching.
    has_value = isinstance(actual, list) and expected in actual
    return has_value if operation == 'contains' else not has_value


def describe_application(doc, base_dir=None):
    """Return public question definitions without exposing action internals. Option sources (a data file's
    values, a folder tree) are read now when the workspace folder is given."""
    spec = _spec(doc)
    if base_dir is not None:
        spec = _with_sources(spec, base_dir)
    public = []
    for question in spec['questions']:
        if question['type'] == 'control-parameter':
            continue
        q = {k: copy.deepcopy(v) for k, v in question.items() if k not in ('secret', 'default')}
        if not question.get('secret') and 'default' in question:
            q['default'] = copy.deepcopy(question['default'])
        q['secret'] = bool(question.get('secret'))
        public.append(q)
    return {'questions': public}


def _check_path_answer(qid, q, value, base_dir):
    """Validate a File/Folder Browse answer; existence is checked only when base_dir is known."""
    if any(ord(ch) < 32 for ch in value):
        raise EngineError(f'Application input {qid!r} contains control characters', code='application-input-invalid')
    extensions = q.get('extensions') if q['type'] == 'file' else None
    if extensions and not any(value.lower().endswith('.' + ext) for ext in extensions):
        raise EngineError(f'Application input {qid!r} must be a ' + ', '.join('.' + e for e in extensions) + ' file',
                          code='application-input-invalid')
    if base_dir is None:
        return
    path = Path(value)
    path = path if path.is_absolute() else Path(base_dir) / path
    if q['type'] == 'folder':
        ok, expected = path.is_dir(), 'an existing folder'
    elif q.get('mode', 'open') == 'save':
        ok, expected = path.parent.is_dir() and not path.is_dir(), 'a file path in an existing folder'
    else:
        ok, expected = path.is_file(), 'an existing file'
    if not ok:
        raise EngineError(f'Application input {qid!r} must be {expected}', code='application-input-invalid')


def _action_value(action, values, spec):
    """value: the answer as given; formula: an expression over all answers ([question_id]); input: a file path."""
    if action.get('kind') != 'formula':
        return copy.deepcopy(values[action['question']])
    from .formula import EvalContext, compile_expression
    from .types import Field, FieldType, Schema
    kinds = {'number': FieldType.DOUBLE, 'boolean': FieldType.BOOL}
    fields = [Field(q['id'], kinds.get(q['type'], FieldType.V_WSTRING), None if q['type'] in kinds else 1073741823)
              for q in spec['questions'] if q['type'] != 'control-parameter']
    row = tuple((values.get(f.name) if f.type is not FieldType.V_WSTRING or values.get(f.name) is None
                 else (', '.join(values[f.name]) if isinstance(values[f.name], list) else str(values[f.name]))) for f in fields)
    try:
        expression = compile_expression(action['expression'], Schema(fields))
        return expression.evaluate(row, EvalContext())
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError(f'Application formula action failed: {exc}', code='application-invalid', node_id=action.get('node_id')) from exc


def bind_application(doc, supplied, base_dir=None):
    """Return a modified document copy and normalized answers; never mutate input.

    When ``base_dir`` is given, File/Folder Browse answers are resolved against it
    and must exist (a save-mode file needs only an existing parent folder).
    """
    spec = _with_sources(_spec(doc), base_dir)
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise EngineError('application_values must be an object', code='application-input-invalid')
    by_id = {q['id']: q for q in spec['questions']}
    if any(q['type'] == 'control-parameter' for q in spec['questions']):
        raise EngineError('This workflow requires a Batch Macro control input; control parameters cannot be supplied as standalone application answers',
                          code='application-control-parameter-context')
    unknown = set(supplied) - set(by_id)
    if unknown:
        raise EngineError('Unknown application input(s): ' + ', '.join(sorted(unknown)), code='application-input-invalid')
    values = {}
    for qid, q in by_id.items():
        value = supplied.get(qid, q.get('default'))
        empty_answer = (value is None or value == '' or (q['type'] == 'multi-choice' and value == []))
        if empty_answer and q.get('required', False):
            raise EngineError(f'Application input {qid!r} is required', code='application-input-invalid')
        if value is None:
            values[qid] = None
        elif q['type'] in ('text', 'text-area') and isinstance(value, str):
            values[qid] = value
        elif q['type'] == 'boolean' and isinstance(value, bool):
            values[qid] = value
        elif q['type'] == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values[qid] = value
        elif q['type'] == 'date' and isinstance(value, str):
            try:
                dt.date.fromisoformat(value)
            except ValueError as exc:
                raise EngineError(f'Application input {qid!r} must be an ISO date (YYYY-MM-DD)', code='application-input-invalid') from exc
            values[qid] = value
        elif q['type'] in ('file', 'folder') and isinstance(value, str):
            if value:
                _check_path_answer(qid, q, value, base_dir)
            values[qid] = value
        elif q['type'] == 'choice' and value in q['options']:
            values[qid] = value
        elif q['type'] == 'multi-choice' and isinstance(value, list) and all(v in q['options'] for v in value) and len(set(value)) == len(value):
            values[qid] = value
        elif q['type'] == 'tree' and isinstance(value, str) and value in q['options']:
            values[qid] = ' > '.join(part.strip() for part in value.split('>'))
        elif q['type'] == 'map':
            values[qid] = _map_answer(qid, value)
        elif q['type'] == 'connection' and isinstance(value, str) and value.strip():
            values[qid] = value.strip()
        else:
            raise EngineError(f'Application input {qid!r} has an invalid value or type', code='application-input-invalid')
        if q['type'] == 'number' and values[qid] is not None:
            if q.get('minimum') is not None and values[qid] < q['minimum']:
                raise EngineError(f'Application input {qid!r} is below its minimum', code='application-input-invalid')
            if q.get('maximum') is not None and values[qid] > q['maximum']:
                raise EngineError(f'Application input {qid!r} is above its maximum', code='application-input-invalid')
    for error in spec['errors']:
        if _condition_matches(error.get('when'), values):
            raise EngineError(error['message'], code='application-error-message', node_id=error.get('id'))
    result = copy.deepcopy(doc)
    nodes = {n['id']: n for n in result['nodes']}
    active_actions = [a for a in spec['actions'] if _condition_matches(a.get('when'), values)]
    occupied = set()
    for action in active_actions:
        node = nodes.get(action['node_id'])
        if node is None:
            raise EngineError(f"Application action target {action['node_id']!r} does not exist", code='application-invalid')
        target_key = (action['node_id'], tuple(action['config_path']))
        if target_key in occupied:
            raise EngineError('Conflicting active application actions target the same configuration value',
                              code='application-invalid', node_id=action['node_id'])
        occupied.add(target_key)
        target = node.get('config')
        for key in action['config_path'][:-1]:
            if isinstance(target, dict) and key in target:
                target = target[key]
            elif isinstance(target, list) and key.isdecimal() and int(key) < len(target):
                target = target[int(key)]
            else:
                target = None
                break
        leaf = action['config_path'][-1]
        new_value = _action_value(action, values, spec)
        if action.get('kind') == 'input' and node.get('tool') != 'core.input_data':
            raise EngineError('An input action must target an Input Data node', code='application-invalid', node_id=node['id'])
        if isinstance(target, dict) and leaf in target:
            target[leaf] = new_value
        elif isinstance(target, list) and leaf.isdecimal() and int(leaf) < len(target):
            target[int(leaf)] = new_value
        else:
            raise EngineError('Application actions may only update existing configuration properties', code='application-invalid', node_id=node['id'])
    return result
