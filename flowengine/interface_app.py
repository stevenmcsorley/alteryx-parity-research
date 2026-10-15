"""Typed, per-run question binding for Flowbench analytic applications.

This is the native workflow-format contract. It is deliberately separate from
Alteryx XML Interface-tool import, whose Action serialization still needs
reference-backed mappings.
"""
from __future__ import annotations

import copy
import datetime as dt
import math

from .errors import EngineError


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
        if not isinstance(q, dict) or q.get('type') not in ('text', 'text-area', 'boolean', 'number', 'choice', 'date', 'file', 'folder', 'multi-choice', 'control-parameter', 'tree'):
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
    for a in actions:
        if not isinstance(a, dict) or a.get('question') not in ids or not isinstance(a.get('node_id'), str):
            raise EngineError('Application action references an unknown question or node', code='application-invalid')
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
    return {'questions': questions, 'actions': actions, 'errors': errors}


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


def describe_application(doc):
    """Return public question definitions without exposing action internals."""
    spec = _spec(doc)
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


def bind_application(doc, supplied):
    """Return a modified document copy and normalized answers; never mutate input."""
    spec = _spec(doc)
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
        elif q['type'] in ('text', 'file', 'folder') and isinstance(value, str):
            values[qid] = value
        elif q['type'] == 'choice' and value in q['options']:
            values[qid] = value
        elif q['type'] == 'multi-choice' and isinstance(value, list) and all(v in q['options'] for v in value) and len(set(value)) == len(value):
            values[qid] = value
        elif q['type'] == 'tree' and isinstance(value, str) and value in q['options']:
            values[qid] = ' > '.join(part.strip() for part in value.split('>'))
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
        if isinstance(target, dict) and leaf in target:
            target[leaf] = copy.deepcopy(values[action['question']])
        elif isinstance(target, list) and leaf.isdecimal() and int(leaf) < len(target):
            target[int(leaf)] = copy.deepcopy(values[action['question']])
        else:
            raise EngineError('Application actions may only update existing configuration properties', code='application-invalid', node_id=node['id'])
    return result
