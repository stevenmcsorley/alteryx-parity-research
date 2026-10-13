"""Tool plugin contract and registry.

A tool is a class with a :class:`ToolManifest` and a ``run`` method. The
manifest is pure data (serialisable for the editor's palette and property
panel); ``run`` is the kernel. Tools are discovered from the built-in
``flowengine.tools`` package and from the ``flowengine.tools`` entry-point group
so third-party packages can add tools without editing the engine.
"""
from __future__ import annotations

import copy
import datetime as _dt
import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import jsonschema

from .errors import CancelledError, ConfigError, ToolUnavailableError
from .table import Table
from .types import Schema

SIDE_EFFECTS = ('none', 'reads-files', 'writes-files', 'network', 'process')


@dataclass(frozen=True)
class Anchor:
    name: str
    label: str
    required: bool = True
    multiple: bool = False  # accepts several connections (ordered)
    description: str = ''

    def to_dict(self):
        return {'name': self.name, 'label': self.label, 'required': self.required,
                'multiple': self.multiple, 'description': self.description}


@dataclass(frozen=True)
class ToolManifest:
    tool_id: str
    version: str
    display_name: str
    catalog_ids: tuple[str, ...]
    inputs: tuple[Anchor, ...]
    outputs: tuple[Anchor, ...]
    config_schema: Mapping[str, Any]
    side_effects: str = 'none'
    deterministic: bool = True
    dependencies: tuple[str, ...] = ()
    description: str = ''

    def __post_init__(self):
        if self.side_effects not in SIDE_EFFECTS:
            raise ValueError(f'{self.tool_id}: bad side_effects {self.side_effects!r}')
        names = [a.name for a in self.inputs] + [a.name for a in self.outputs]
        if len(set(a.name for a in self.inputs)) != len(self.inputs) or \
                len(set(a.name for a in self.outputs)) != len(self.outputs):
            raise ValueError(f'{self.tool_id}: duplicate anchor names {names}')
        jsonschema.Draft202012Validator.check_schema(self.config_schema)

    def input(self, name: str) -> Anchor | None:
        return next((a for a in self.inputs if a.name == name), None)

    def output(self, name: str) -> Anchor | None:
        return next((a for a in self.outputs if a.name == name), None)

    def to_dict(self) -> dict:
        return {'tool_id': self.tool_id, 'version': self.version, 'display_name': self.display_name,
                'catalog_ids': list(self.catalog_ids), 'inputs': [a.to_dict() for a in self.inputs],
                'outputs': [a.to_dict() for a in self.outputs], 'config_schema': self.config_schema,
                'side_effects': self.side_effects, 'deterministic': self.deterministic,
                'dependencies': list(self.dependencies), 'description': self.description}


class CancelToken:
    def __init__(self):
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@dataclass
class RunContext:
    """Services available to a kernel while it runs one node."""
    node_id: str
    base_dir: Any  # pathlib.Path
    clock: Callable[[], _dt.datetime]
    emit: Callable[..., None]
    cancel_token: CancelToken
    dry_run: bool = False  # schema inference: kernels receive empty tables and must not cause side effects
    limits: dict = field(default_factory=dict)
    browse_sink: Callable[[str, Table], None] | None = None
    constants: dict = field(default_factory=dict)
    profile: Any = None  # flowengine.profiles.Profile

    def warn(self, message: str, **data):
        self.emit('warning', message=message, **data)

    def info(self, message: str, **data):
        self.emit('info', message=message, **data)

    def check_cancelled(self):
        if self.cancel_token.cancelled:
            raise CancelledError('Run cancelled', node_id=self.node_id)

    def resolve_path(self, path: str):
        """Resolve a configured path relative to the workflow directory."""
        from pathlib import Path
        p = Path(path)
        return (p if p.is_absolute() else Path(self.base_dir) / p).resolve()


class Tool:
    """Base class for tool kernels. Subclasses set ``manifest`` and implement ``run``."""
    manifest: ToolManifest

    def normalize_config(self, config: dict) -> dict:
        """Validate against the JSON schema, apply defaults, then semantic checks."""
        cfg = copy.deepcopy(config if config is not None else {})
        _apply_defaults(self.manifest.config_schema, cfg)
        errors = sorted(jsonschema.Draft202012Validator(self.manifest.config_schema).iter_errors(cfg),
                        key=lambda e: list(e.absolute_path))
        if errors:
            msgs = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors[:5]]
            raise ConfigError(f'{self.manifest.display_name} configuration invalid: ' + '; '.join(msgs))
        self.check_config(cfg)
        return cfg

    def check_config(self, config: dict) -> None:
        """Hook for semantic validation that JSON schema cannot express."""

    def infer(self, ctx: RunContext, config: dict, inputs: dict[str, Any]) -> dict[str, Schema] | None:
        """Static schema propagation. ``inputs`` maps anchor -> Schema (or list for
        multi-connection anchors). Default: run the kernel on empty tables, which
        is exact for pure kernels. Tools with side effects must override."""
        if self.manifest.side_effects != 'none':
            return None
        empty = {k: ([Table.empty(s) for s in v] if isinstance(v, list) else Table.empty(v)) for k, v in inputs.items()}
        out = self.run(ctx, config, empty)
        return {k: t.schema for k, t in out.items()}

    def run(self, ctx: RunContext, config: dict, inputs: dict[str, Any]) -> dict[str, Table]:
        raise NotImplementedError


def _apply_defaults(schema: Mapping, value):
    if not isinstance(value, dict) or schema.get('type') != 'object':
        return
    for key, sub in schema.get('properties', {}).items():
        if key not in value and 'default' in sub:
            value[key] = copy.deepcopy(sub['default'])
        if key in value:
            if isinstance(value[key], dict):
                _apply_defaults(sub, value[key])
            elif isinstance(value[key], list) and isinstance(sub.get('items'), dict):
                for item in value[key]:
                    _apply_defaults(sub['items'], item)


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._by_catalog: dict[str, str] = {}

    def register(self, tool_cls: type[Tool]) -> type[Tool]:
        tool = tool_cls()
        m = tool.manifest
        if m.tool_id in self._tools:
            raise ValueError(f'Duplicate tool id {m.tool_id}')
        for cid in m.catalog_ids:
            if cid in self._by_catalog:
                raise ValueError(f'Catalogue id {cid} claimed by both {self._by_catalog[cid]} and {m.tool_id}')
            self._by_catalog[cid] = m.tool_id
        self._tools[m.tool_id] = tool
        return tool_cls

    def get(self, tool_id: str) -> Tool:
        tool = self._tools.get(tool_id)
        if tool is None:
            tool_id2 = self._by_catalog.get(tool_id)
            tool = self._tools.get(tool_id2) if tool_id2 else None
        if tool is None:
            from . import catalog
            entry = catalog.lookup(tool_id)
            if entry:
                raise ToolUnavailableError(
                    f"Catalogue tool '{entry['name']}' ({entry['id']}) has no working implementation yet "
                    f"(status: {entry.get('implementation_status')})")
            raise ToolUnavailableError(f'Unknown tool {tool_id!r}')
        return tool

    def __contains__(self, tool_id) -> bool:
        return tool_id in self._tools or tool_id in self._by_catalog

    def tools(self) -> list[Tool]:
        return [self._tools[k] for k in sorted(self._tools)]

    def catalog_map(self) -> dict[str, str]:
        return dict(self._by_catalog)


_DEFAULT: Registry | None = None
log = logging.getLogger('flowengine')


def default_registry() -> Registry:
    """Built-in tools plus any installed ``flowengine.tools`` entry points."""
    global _DEFAULT
    if _DEFAULT is None:
        reg = Registry()
        from . import tools as builtin
        for mod in sorted(pkgutil.iter_modules(builtin.__path__), key=lambda m: m.name):
            module = importlib.import_module(f'{builtin.__name__}.{mod.name}')
            for cls in getattr(module, 'TOOLS', ()):
                reg.register(cls)
        try:
            from importlib.metadata import entry_points
            for ep in entry_points(group='flowengine.tools'):
                for cls in ep.load():
                    reg.register(cls)
        except Exception as e:  # a broken third-party plugin must not break the engine
            log.warning('Failed loading plugin entry points: %s', e)
        _DEFAULT = reg
    return _DEFAULT
