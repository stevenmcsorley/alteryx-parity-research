"""Executable workflow macros with typed table boundaries and nested provenance."""
from __future__ import annotations

import json
import copy
from decimal import Decimal
from pathlib import Path

from ..errors import CancelledError, ConfigError, DataError, EngineError, ExpressionError
from ..executor import run_workflow
from ..plugin import Anchor, RunContext, Tool, ToolManifest
from ..table import Table


class MacroInput(Tool):
    manifest = ToolManifest(
        tool_id='flowengine.macro_input', version='1.0.0', display_name='Macro Input',
        catalog_ids=('interface:macro-input',),
        inputs=(), outputs=(Anchor('output', 'O'),),
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['name'],
                       'properties': {'name': {'type': 'string', 'minLength': 1}}},
        description='Expose a typed parent workflow input table inside a macro definition.')

    def infer(self, ctx, config, inputs):
        schema = ctx.macro_input_schemas.get(config['name'])
        return {'output': schema} if schema is not None else None

    def run(self, ctx: RunContext, config, inputs):
        ctx.check_cancelled()
        table = ctx.macro_inputs.get(config['name'])
        if not isinstance(table, Table):
            raise DataError(f"Macro input {config['name']!r} is not bound to a parent table", node_id=ctx.node_id)
        return {'output': table}


class MacroOutput(Tool):
    manifest = ToolManifest(
        tool_id='flowengine.macro_output', version='1.0.0', display_name='Macro Output',
        catalog_ids=('interface:macro-output',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        config_schema={'type': 'object', 'additionalProperties': False,
                       'properties': {'name': {'type': 'string', 'default': 'Output'}}},
        description='Expose a typed child workflow table as a named macro result.')

    def run(self, ctx, config, inputs):
        ctx.check_cancelled()
        return {'output': inputs['input']}


class WorkflowMacro(Tool):
    manifest = ToolManifest(
        tool_id='flowengine.macro', version='1.0.0', display_name='Workflow Macro', catalog_ids=(),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'workflow': {'type': 'object', 'description': 'Embedded flowengine.workflow macro definition'},
                'definition_path': {'type': 'string', 'minLength': 1},
                'macro_id': {'type': 'string'},
                'input_bindings': {'type': 'object', 'default': {'Input': 'Input'},
                                   'additionalProperties': {'type': 'string'}},
                'output_name': {'type': 'string'},
                'max_depth': {'type': 'integer', 'minimum': 1, 'maximum': 128, 'default': 32},
            },
            'oneOf': [{'required': ['workflow']}, {'required': ['definition_path']}],
        },
        description='Execute a validated child workflow with table-valued macro inputs and named outputs.')

    def infer(self, ctx, config, inputs):
        # Child schema depends on the macro boundary binding and is checked by its own plan at run time.
        return None

    def _definition(self, ctx, config):
        if 'workflow' in config:
            return config['workflow'], Path(ctx.base_dir)
        path = ctx.resolve_path(config['definition_path'])
        if not path.is_file():
            raise ConfigError(f'Macro definition does not exist: {path}', node_id=ctx.node_id)
        if path.suffix.lower() in ('.yxmc', '.yxmd', '.yxwz', '.yxzp'):
            from ..interop.yxmd import import_file
            doc, report = import_file(path)
            incomplete = [n for n in report.get('nodes', [])
                          if n.get('status') in ('unsupported', 'partial')]
            if incomplete:
                detail = ', '.join(
                    f"{n.get('node')}: {n.get('reason') or '; '.join(n.get('warnings', []))}"
                    for n in incomplete[:5])
                raise ConfigError(f'Macro contains imported tools with unmapped settings: {detail}',
                                  node_id=ctx.node_id)
            return doc, path.parent
        try:
            return json.loads(path.read_text(encoding='utf-8')), path.parent
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f'Cannot load macro definition {path}: {type(exc).__name__}: {exc}',
                              node_id=ctx.node_id) from None

    def run(self, ctx: RunContext, config, inputs):
        ctx.check_cancelled()
        doc, child_base = self._definition(ctx, config)
        identity = config.get('macro_id') or _macro_identity(doc)
        stack = (*ctx.macro_stack, identity)
        if identity in ctx.macro_stack:
            chain = ' -> '.join(ctx.macro_stack + (identity,))
            raise ConfigError(f'Recursive macro invocation detected: {chain}', node_id=ctx.node_id)
        if len(stack) > config['max_depth']:
            raise ConfigError(f"Macro nesting exceeds configured limit {config['max_depth']}", node_id=ctx.node_id)

        bindings = {}
        input_names = [(n.get('config') or {}).get('name') for n in doc.get('nodes', [])
                       if n.get('tool') == 'flowengine.macro_input']
        configured = dict(config['input_bindings'])
        if len(inputs) == len(configured) == 1 and next(iter(configured.values())) not in inputs:
            configured[next(iter(configured))] = next(iter(inputs))
        if (len(input_names) == len(configured) == 1 and input_names[0] not in configured
                and 'Input' in configured):
            # Alteryx macro names its lone Macro Input in the child document; map the one parent table by position.
            configured[input_names[0]] = configured.pop('Input')
        for macro_input, parent_anchor in configured.items():
            if parent_anchor not in inputs:
                raise ConfigError(f"Macro input {macro_input!r} refers to unconnected parent anchor {parent_anchor!r}",
                                  node_id=ctx.node_id)
            value = inputs[parent_anchor]
            if not isinstance(value, Table):
                raise ConfigError(f"Parent anchor {parent_anchor!r} must provide one table per macro input",
                                  node_id=ctx.node_id)
            bindings[macro_input] = value

        outputs = [n for n in doc.get('nodes', []) if n.get('tool') == 'flowengine.macro_output']
        desired = config.get('output_name')
        matches = [n for n in outputs if (n.get('config') or {}).get('name', 'Output') == (desired or 'Output')]
        if not desired and len(outputs) == 1:
            matches = outputs
        if len(matches) != 1:
            names = [n.get('config', {}).get('name', 'Output') for n in outputs]
            raise ConfigError(f"Macro output selection is ambiguous or missing (requested {desired!r}; available {names})",
                              node_id=ctx.node_id)
        target = matches[0]
        ctx.emit('macro-started', definition=identity, depth=len(stack), input_names=sorted(bindings))
        try:
            result = run_workflow(doc, base_dir=child_base, registry=None, clock=ctx.clock,
                                  cancel_token=ctx.cancel_token, limits=ctx.limits,
                                  profile=ctx.profile.name if ctx.profile else None,
                                  _macro_inputs=bindings,
                                  _macro_input_schemas={name: table.schema for name, table in bindings.items()},
                                  _macro_stack=stack)
        except EngineError as e:
            raise EngineError(f'Macro child validation failed at {e.node_id or "-"}: {e.message}; '
                              f'call stack: {" -> ".join(ctx.macro_stack + (identity,))}',
                              node_id=ctx.node_id, code='macro-execution-failed') from None
        if not result.ok:
            if result.status == 'cancelled':
                raise CancelledError(f'Macro child cancelled at {result.error.get("node_id") or "-"}',
                                     node_id=ctx.node_id)
            nested = result.error or {'code': result.status}
            raise EngineError(f"Macro child failed at {nested.get('node_id') or '-'}: {nested.get('message') or nested['code']}; "
                              f"call stack: {' -> '.join(ctx.macro_stack + (identity,))}",
                              node_id=ctx.node_id, code='macro-execution-failed')
        for event in result.events:
            if event['type'] in ('macro-started', 'macro-completed', 'macro-nested-event'):
                ctx.emit('macro-nested-event', child_run_id=result.run_id, child_node_id=event.get('node_id'),
                         event_type=event['type'], details={k: v for k, v in event.items()
                                                           if k not in ('seq', 'ts', 'run_id', 'type', 'node_id')})
        child_table = result.outputs.get(target['id'], {}).get('output')
        if child_table is None:
            raise EngineError(f"Macro output node {target['id']!r} did not retain its table",
                              node_id=ctx.node_id, code='macro-output-missing')
        ctx.emit('macro-completed', definition=identity, output=desired or 'Output', rows=len(child_table),
                 digest=child_table.canonical_digest(), child_run_id=result.run_id)
        return {'output': child_table}


class BatchMacro(Tool):
    """Re-run one child workflow for each ordered control record and combine matching schemas."""
    manifest = ToolManifest(
        tool_id='flowengine.batch_macro', version='1.0.0', display_name='Batch Macro', catalog_ids=(),
        inputs=(Anchor('input', 'I'), Anchor('control', 'C')),
        outputs=(Anchor('output', 'O'),),
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'workflow': {'type': 'object'},
                'definition_path': {'type': 'string', 'minLength': 1},
                'macro_id': {'type': 'string'},
                'input_name': {'type': 'string', 'minLength': 1, 'default': 'Input'},
                'output_name': {'type': 'string', 'default': 'Output'},
                'parameters': {'type': 'array', 'default': [], 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['name', 'field'],
                    'properties': {'name': {'type': 'string', 'minLength': 1},
                                   'field': {'type': 'string', 'minLength': 1}}}},
                'actions': {'type': 'array', 'default': [], 'items': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['parameter', 'target_node', 'config_path'],
                    'properties': {'parameter': {'type': 'string', 'minLength': 1},
                                   'target_node': {'type': 'string', 'minLength': 1},
                                   'config_path': {'type': 'string', 'minLength': 1}}}},
                'max_iterations': {'type': 'integer', 'minimum': 1, 'default': 10000},
                'max_depth': {'type': 'integer', 'minimum': 1, 'maximum': 128, 'default': 32},
            },
            'oneOf': [{'required': ['workflow']}, {'required': ['definition_path']}],
        },
        description='Run one child workflow per control-table record. Declared actions update existing child '
                    'tool configuration paths; outputs require an identical schema for every iteration.')

    def infer(self, ctx, config, inputs):
        # Static inference requires the child definition; runtime validates and infers with actual parent schemas.
        return None

    def run(self, ctx: RunContext, config, inputs):
        ctx.check_cancelled()
        doc, child_base = WorkflowMacro()._definition(ctx, config)
        identity = config.get('macro_id') or _macro_identity(doc)
        if identity in ctx.macro_stack:
            raise ConfigError('Recursive batch macro invocation detected: ' +
                              ' -> '.join(ctx.macro_stack + (identity,)), node_id=ctx.node_id)
        if len(ctx.macro_stack) >= config['max_depth']:
            raise ConfigError(f"Macro nesting exceeds configured limit {config['max_depth']}", node_id=ctx.node_id)
        data = inputs['input']
        controls = inputs['control']
        parameters = {p['name']: p['field'] for p in config['parameters']}
        if len(parameters) != len(config['parameters']):
            raise ConfigError('Batch macro parameter names must be unique', node_id=ctx.node_id)
        for field_name in parameters.values():
            if field_name not in controls.schema:
                raise DataError(f'Control input has no parameter field {field_name!r}', node_id=ctx.node_id)
        if not parameters:
            raise ConfigError('At least one named control parameter is required', node_id=ctx.node_id)
        for action in config['actions']:
            if action['parameter'] not in parameters:
                raise ConfigError(f"Action refers to undefined control parameter {action['parameter']!r}",
                                  node_id=ctx.node_id)
        child_inputs = [n for n in doc.get('nodes', []) if n.get('tool') == 'flowengine.macro_input']
        if not any(n.get('config', {}).get('name') == config['input_name'] for n in child_inputs):
            raise ConfigError(f"Child macro has no Macro Input named {config['input_name']!r}", node_id=ctx.node_id)
        output_nodes = [n for n in doc.get('nodes', []) if n.get('tool') == 'flowengine.macro_output'
                        and n.get('config', {}).get('name', 'Output') == config['output_name']]
        if len(output_nodes) != 1:
            raise ConfigError(f"Expected one child Macro Output named {config['output_name']!r}; "
                              f'found {len(output_nodes)}', node_id=ctx.node_id)
        output_node = output_nodes[0]['id']
        run_stack = (*ctx.macro_stack, identity)
        child_schemas = {config['input_name']: data.schema}
        planned, problems = _plan_macro(doc, child_base, ctx, child_schemas)
        if problems:
            issue = problems[0]
            raise ConfigError(f"Child macro does not validate before batch execution: {issue['message']}",
                              node_id=ctx.node_id)
        empty_schema = planned.schemas.get((output_node, 'output')) if planned else None
        if not len(controls):
            if empty_schema is None:
                raise DataError('Cannot infer batch output schema for zero control records', node_id=ctx.node_id)
            ctx.emit('batch-completed', definition=identity, iterations=0, rows=0,
                     digest=Table.empty(empty_schema).canonical_digest())
            return {'output': Table.empty(empty_schema)}

        limit = min(config['max_iterations'], int(ctx.limits.get('macro_max_iterations', config['max_iterations'])))
        if len(controls) > limit:
            raise DataError(f'Batch has {len(controls)} control records, exceeding iteration limit {limit}',
                            node_id=ctx.node_id)
        output_parts = []
        expected_schema = None
        for index, row in enumerate(controls.rows()):
            ctx.check_cancelled()
            iteration_values = {name: _control_json_value(row[controls.schema.index(field_name)], name)
                                for name, field_name in parameters.items()}
            iteration_doc = copy.deepcopy(doc)
            _apply_batch_actions(iteration_doc, config['actions'], iteration_values, ctx.node_id, index)
            ctx.emit('batch-iteration-started', definition=identity, iteration=index + 1,
                     parameter_names=sorted(iteration_values))
            try:
                result = run_workflow(iteration_doc, base_dir=child_base, clock=ctx.clock,
                                      cancel_token=ctx.cancel_token, limits=ctx.limits,
                                      profile=ctx.profile.name if ctx.profile else None,
                                      _macro_inputs={config['input_name']: data},
                                      _macro_input_schemas=child_schemas, _macro_stack=run_stack)
            except EngineError as e:
                raise EngineError(f'Batch iteration {index + 1} failed validation at {e.node_id or "-"}: '
                                  f'{e.message}; call stack: {" -> ".join(run_stack)}', node_id=ctx.node_id,
                                  code='batch-macro-iteration-failed') from None
            if not result.ok:
                if result.status == 'cancelled':
                    raise CancelledError(f'Batch iteration {index + 1} cancelled', node_id=ctx.node_id)
                failure = result.error or {'code': result.status, 'message': 'child execution failed'}
                raise EngineError(f"Batch iteration {index + 1} failed at {failure.get('node_id') or '-'}: "
                                  f"{failure.get('message') or failure['code']}; call stack: "
                                  f"{' -> '.join(run_stack)}", node_id=ctx.node_id,
                                  code='batch-macro-iteration-failed')
            output = result.outputs.get(output_node, {}).get('output')
            if output is None:
                raise EngineError(f'Batch iteration {index + 1} did not produce the selected macro output',
                                  node_id=ctx.node_id, code='macro-output-missing')
            if expected_schema is None:
                expected_schema = output.schema
            elif output.schema != expected_schema:
                raise DataError(f'Batch iteration {index + 1} output schema differs from iteration 1',
                                node_id=ctx.node_id)
            output_parts.append(output)
            ctx.emit('batch-iteration-completed', definition=identity, iteration=index + 1,
                     rows=len(output), digest=output.canonical_digest(), child_run_id=result.run_id)
        merged = Table.from_rows(expected_schema, (row for part in output_parts for row in part.rows()))
        ctx.emit('batch-completed', definition=identity, iterations=len(output_parts), rows=len(merged),
                 digest=merged.canonical_digest())
        return {'output': merged}


class IterativeMacro(Tool):
    """Feed each child output into the next invocation until all rows satisfy a predicate."""
    manifest = ToolManifest(
        tool_id='flowengine.iterative_macro', version='1.0.0', display_name='Iterative Macro', catalog_ids=(),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),),
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'workflow': {'type': 'object'},
                'definition_path': {'type': 'string', 'minLength': 1},
                'macro_id': {'type': 'string'},
                'input_name': {'type': 'string', 'minLength': 1, 'default': 'Input'},
                'output_name': {'type': 'string', 'default': 'Output'},
                'terminate_expression': {'type': 'string', 'minLength': 1},
                'max_iterations': {'type': 'integer', 'minimum': 1, 'default': 100},
                'max_depth': {'type': 'integer', 'minimum': 1, 'maximum': 128, 'default': 32},
            },
            'required': ['terminate_expression'],
            'oneOf': [{'required': ['workflow']}, {'required': ['definition_path']}],
        },
        description='Feed each child output back as its next input until every output row satisfies the '
                    'termination expression. Empty outputs satisfy the all-rows condition.')

    def infer(self, ctx, config, inputs):
        return None

    def run(self, ctx: RunContext, config, inputs):
        ctx.check_cancelled()
        doc, child_base = WorkflowMacro()._definition(ctx, config)
        identity = config.get('macro_id') or _macro_identity(doc)
        if identity in ctx.macro_stack:
            raise ConfigError('Recursive iterative macro invocation detected: ' +
                              ' -> '.join(ctx.macro_stack + (identity,)), node_id=ctx.node_id)
        if len(ctx.macro_stack) >= config['max_depth']:
            raise ConfigError(f"Macro nesting exceeds configured limit {config['max_depth']}", node_id=ctx.node_id)
        input_name = config['input_name']
        if not any(n.get('tool') == 'flowengine.macro_input' and n.get('config', {}).get('name') == input_name
                   for n in doc.get('nodes', [])):
            raise ConfigError(f"Child macro has no Macro Input named {input_name!r}", node_id=ctx.node_id)
        outputs = [n for n in doc.get('nodes', []) if n.get('tool') == 'flowengine.macro_output'
                   and n.get('config', {}).get('name', 'Output') == config['output_name']]
        if len(outputs) != 1:
            raise ConfigError(f"Expected one child Macro Output named {config['output_name']!r}; found {len(outputs)}",
                              node_id=ctx.node_id)
        output_node = outputs[0]['id']
        stack = (*ctx.macro_stack, identity)
        state = inputs['input']
        expected_schema = None
        from ..formula import EvalContext, compile_expression, truthy
        for index in range(min(config['max_iterations'],
                               int(ctx.limits.get('macro_max_iterations', config['max_iterations'])))):
            ctx.check_cancelled()
            try:
                result = run_workflow(doc, base_dir=child_base, clock=ctx.clock,
                                      cancel_token=ctx.cancel_token, limits=ctx.limits,
                                      profile=ctx.profile.name if ctx.profile else None,
                                      _macro_inputs={input_name: state},
                                      _macro_input_schemas={input_name: state.schema}, _macro_stack=stack)
            except EngineError as e:
                raise EngineError(f'Iterative pass {index + 1} failed validation at {e.node_id or "-"}: {e.message}; '
                                  f'call stack: {" -> ".join(stack)}', node_id=ctx.node_id,
                                  code='iterative-macro-pass-failed') from None
            if result.status == 'cancelled':
                raise CancelledError(f'Iterative pass {index + 1} cancelled', node_id=ctx.node_id)
            if not result.ok:
                failure = result.error or {'code': result.status, 'message': 'child execution failed'}
                raise EngineError(f'Iterative pass {index + 1} failed at {failure.get("node_id") or "-"}: '
                                  f'{failure.get("message") or failure["code"]}; call stack: {" -> ".join(stack)}',
                                  node_id=ctx.node_id, code='iterative-macro-pass-failed')
            output = result.outputs.get(output_node, {}).get('output')
            if output is None:
                raise EngineError(f'Iterative pass {index + 1} did not produce the selected macro output',
                                  node_id=ctx.node_id, code='macro-output-missing')
            if expected_schema is None:
                expected_schema = output.schema
            elif output.schema != expected_schema:
                raise DataError(f'Iterative pass {index + 1} output schema differs from pass 1',
                                node_id=ctx.node_id)
            try:
                expression = compile_expression(config['terminate_expression'], output.schema)
            except ExpressionError as e:
                raise ExpressionError(e.message, node_id=ctx.node_id) from None
            if expression.kind not in ('bool', 'number', 'null', 'any'):
                raise ExpressionError(f'Iterative termination expression must be boolean, got {expression.kind}',
                                      node_id=ctx.node_id)
            eval_context = EvalContext(now=ctx.clock, warn=lambda message: ctx.warn(message))
            converged = all(truthy(expression.evaluate(row, eval_context)) for row in output.rows())
            ctx.emit('iterative-pass-completed', definition=identity, iteration=index + 1,
                     rows=len(output), digest=output.canonical_digest(), converged=converged,
                     child_run_id=result.run_id)
            if converged:
                ctx.emit('iterative-completed', definition=identity, iterations=index + 1,
                         rows=len(output), digest=output.canonical_digest())
                return {'output': output}
            state = output
        raise EngineError(f"Iterative macro did not converge within {config['max_iterations']} passes",
                          node_id=ctx.node_id, code='iterative-macro-limit')


def _plan_macro(doc, base_dir, ctx, macro_input_schemas):
    from ..executor import plan_workflow
    return plan_workflow(doc, base_dir=base_dir, profile=ctx.profile,
                         macro_input_schemas=macro_input_schemas)


def _control_json_value(value, name):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        as_float = float(value)
        if Decimal(str(as_float)) != value:
            raise DataError(f'Control parameter {name!r} cannot be represented exactly in child JSON configuration')
        return as_float
    raise DataError(f'Control parameter {name!r} has unsupported configuration value type {type(value).__name__}')


def _apply_batch_actions(doc, actions, values, node_id, iteration):
    nodes = {node['id']: node for node in doc.get('nodes', [])}
    for action in actions:
        target_id = action['target_node']
        if target_id not in nodes:
            raise ConfigError(f"Action target node {target_id!r} does not exist", node_id=node_id)
        segments = action['config_path'].split('/')
        if not action['config_path'].startswith('/config/') or len(segments) < 3:
            raise ConfigError('Batch actions must target an existing child tool config path beginning with /config/',
                              node_id=node_id)
        try:
            segments = [part.replace('~1', '/').replace('~0', '~') for part in segments[1:]]
            cursor = nodes[target_id]
            for segment in segments[:-1]:
                if not isinstance(cursor, dict) or segment not in cursor:
                    raise KeyError(segment)
                cursor = cursor[segment]
            leaf = segments[-1]
            if not isinstance(cursor, dict) or leaf not in cursor:
                raise KeyError(leaf)
        except KeyError as exc:
            raise ConfigError(f"Action config path {action['config_path']!r} does not exist on node {target_id!r}",
                              node_id=node_id) from None
        cursor[leaf] = values[action['parameter']]


def _macro_identity(doc):
    from ..executor import workflow_digest
    return workflow_digest(doc)


TOOLS = [MacroInput, MacroOutput, WorkflowMacro, BatchMacro, IterativeMacro]
