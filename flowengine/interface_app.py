"""Typed, per-run question binding for Flowbench analytic applications.

This is the native workflow-format contract. It is deliberately separate from
Alteryx XML Interface-tool import, whose Action serialization still needs
reference-backed mappings.
"""
from __future__ import annotations

import copy
import math

from .errors import EngineError


def _spec(doc):
    spec = (doc.get('metadata') or {}).get('application')
    if spec is None:
        return {'questions': [], 'actions': []}
    if not isinstance(spec, dict) or set(spec) - {'questions', 'actions'}:
        raise EngineError('metadata.application must contain only questions and actions', code='application-invalid')
    questions, actions = spec.get('questions', []), spec.get('actions', [])
    if not isinstance(questions, list) or not isinstance(actions, list):
        raise EngineError('Application questions and actions must be arrays', code='application-invalid')
    ids = set()
    for q in questions:
        if not isinstance(q, dict) or q.get('type') not in ('text', 'boolean', 'number', 'choice', 'date', 'file', 'folder', 'multi-choice'):
            raise EngineError('Each application question needs a supported type', code='application-invalid')
        qid = q.get('id')
        if not isinstance(qid, str) or not qid or qid in ids:
            raise EngineError('Application question ids must be unique non-empty strings', code='application-invalid')
        ids.add(qid)
        if q.get('secret') and 'default' in q:
            raise EngineError(f'Secret application question {qid!r} must not store a default value', code='application-invalid')
        if q.get('secret') and q['type'] != 'text':
            raise EngineError(f'Secret application question {qid!r} must be text', code='application-invalid')
        if q['type'] in ('choice', 'multi-choice') and (not isinstance(q.get('options'), list) or not q['options']):
            raise EngineError(f'Choice question {qid!r} requires options', code='application-invalid')
    for a in actions:
        if not isinstance(a, dict) or a.get('question') not in ids or not isinstance(a.get('node_id'), str):
            raise EngineError('Application action references an unknown question or node', code='application-invalid')
        when = a.get('when')
        if when is not None:
            allowed = ('equals', 'not_equals', 'truthy', 'falsy', 'contains', 'not_contains')
            if (not isinstance(when, dict) or set(when) - {'question', 'operator', 'value'}
                    or when.get('question') not in ids or when.get('operator') not in allowed
                    or (when['operator'] in ('equals', 'not_equals', 'contains', 'not_contains')) != ('value' in when)):
                raise EngineError('Application action has an invalid conditional predicate', code='application-invalid')
        path = a.get('config_path')
        if not isinstance(path, list) or not path or any(not isinstance(k, str) or k in ('__proto__', 'prototype', 'constructor') for k in path):
            raise EngineError('Application action config_path must be a safe list of property names', code='application-invalid')
    occupied = set()
    for a in actions:
        target = (a['node_id'], tuple(a['config_path']))
        if target in occupied:
            raise EngineError('Conflicting application actions target the same configuration value',
                              code='application-invalid', node_id=a['node_id'])
        occupied.add(target)
    return {'questions': questions, 'actions': actions}


def _condition_matches(predicate, answers):
    if not predicate:
        return True
    actual = answers[predicate['question']]
    operation = predicate['operator']
    if operation == 'truthy':
        return bool(actual)
    if operation == 'falsy':
        return not bool(actual)
    expected = predicate['value']
    if operation in ('equals', 'not_equals'):
        equal = actual == expected and type(actual) is type(expected)
        return equal if operation == 'equals' else not equal
    # A multi-select question can control an action by membership; for a
    # scalar question this is deliberately false rather than substring matching.
    has_value = isinstance(actual, list) and expected in actual
    return has_value if operation == 'contains' else not has_value


def describe_application(doc):
    """Return public question definitions without exposing action internals."""
    spec = _spec(doc)
    public = []
    for question in spec['questions']:
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
    unknown = set(supplied) - set(by_id)
    if unknown:
        raise EngineError('Unknown application input(s): ' + ', '.join(sorted(unknown)), code='application-input-invalid')
    values = {}
    for qid, q in by_id.items():
        value = supplied.get(qid, q.get('default'))
        if value is None and q.get('required', False):
            raise EngineError(f'Application input {qid!r} is required', code='application-input-invalid')
        if value is None:
            values[qid] = None
        elif q['type'] == 'text' and isinstance(value, str):
            values[qid] = value
        elif q['type'] == 'boolean' and isinstance(value, bool):
            values[qid] = value
        elif q['type'] == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values[qid] = value
        elif q['type'] in ('text', 'date', 'file', 'folder') and isinstance(value, str):
            values[qid] = value
        elif q['type'] in ('choice', 'multi-choice') and value in q['options']:
            values[qid] = value
        elif q['type'] == 'multi-choice' and isinstance(value, list) and all(v in q['options'] for v in value) and len(set(value)) == len(value):
            values[qid] = value
        else:
            raise EngineError(f'Application input {qid!r} has an invalid value or type', code='application-input-invalid')
        if q['type'] == 'number' and values[qid] is not None:
            if q.get('minimum') is not None and values[qid] < q['minimum']:
                raise EngineError(f'Application input {qid!r} is below its minimum', code='application-input-invalid')
            if q.get('maximum') is not None and values[qid] > q['maximum']:
                raise EngineError(f'Application input {qid!r} is above its maximum', code='application-input-invalid')
    result = copy.deepcopy(doc)
    nodes = {n['id']: n for n in result['nodes']}
    for action in spec['actions']:
        node = nodes.get(action['node_id'])
        if node is None:
            raise EngineError(f"Application action target {action['node_id']!r} does not exist", code='application-invalid')
        if not _condition_matches(action.get('when'), values):
            continue
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
