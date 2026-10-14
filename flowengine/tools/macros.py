"""Executable workflow macros with typed table boundaries and nested provenance."""
from __future__ import annotations

import json
from pathlib import Path

from ..errors import ConfigError, DataError, EngineError
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
        return None

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
            unsupported = [n for n in report.get('nodes', []) if n.get('status') == 'unsupported']
            if unsupported:
                detail = ', '.join(f"{n.get('node')}: {n.get('reason')}" for n in unsupported[:5])
                raise ConfigError(f'Macro contains unsupported imported tools: {detail}', node_id=ctx.node_id)
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
        result = run_workflow(doc, base_dir=child_base, registry=None, clock=ctx.clock,
                              cancel_token=ctx.cancel_token, limits=ctx.limits,
                              profile=ctx.profile.name if ctx.profile else None,
                              _macro_inputs=bindings, _macro_stack=stack)
        if not result.ok:
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


def _macro_identity(doc):
    from ..executor import workflow_digest
    return workflow_digest(doc)


TOOLS = [MacroInput, MacroOutput, WorkflowMacro]
