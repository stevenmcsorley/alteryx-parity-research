"""Error hierarchy. Every failure surfaced to a user carries a stable ``code``."""
from __future__ import annotations


class EngineError(Exception):
    code = 'engine-error'

    def __init__(self, message: str, *, node_id: str | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.node_id = node_id
        if code:
            self.code = code

    def to_dict(self) -> dict:
        return {'code': self.code, 'message': self.message, 'node_id': self.node_id}

    def __str__(self) -> str:
        prefix = f'[{self.node_id}] ' if self.node_id else ''
        return f'{prefix}{self.code}: {self.message}'


class WorkflowValidationError(EngineError):
    """The workflow document or graph is invalid; nothing was executed."""
    code = 'workflow-invalid'

    def __init__(self, message: str, *, problems: list[dict] | None = None, **kw):
        super().__init__(message, **kw)
        self.problems = problems or []

    def to_dict(self) -> dict:
        return {**super().to_dict(), 'problems': self.problems}


class ConfigError(EngineError):
    """A node's configuration violates its tool's schema or semantics."""
    code = 'config-invalid'


class SchemaError(EngineError):
    """Incoming field metadata is incompatible with the configuration."""
    code = 'schema-invalid'


class DataError(EngineError):
    """A value could not be processed (parse/overflow/type error) at run time."""
    code = 'data-error'


class ToolUnavailableError(EngineError):
    """The catalogue entry exists but no working implementation is registered."""
    code = 'tool-unavailable'


class CancelledError(EngineError):
    code = 'cancelled'


class ExpressionError(EngineError):
    """Formula parse or evaluation failure."""
    code = 'expression-error'

    def __init__(self, message: str, *, position: int | None = None, **kw):
        super().__init__(message if position is None else f'{message} (at offset {position})', **kw)
        self.position = position
