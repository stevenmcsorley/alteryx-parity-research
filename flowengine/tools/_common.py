"""Shared helpers for built-in tool kernels."""
from __future__ import annotations

from ..errors import ConfigError, DataError, SchemaError
from ..types import CastFailure, Field, FieldType, cast_value

TYPE_ENUM = [t.value for t in FieldType]

FIELD_SPEC = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['name', 'type'],
    'properties': {
        'name': {'type': 'string', 'minLength': 1},
        'type': {'enum': TYPE_ENUM},
        'size': {'type': 'integer', 'minimum': 1},
        'scale': {'type': 'integer', 'minimum': 0},
    },
}

ON_ERROR = {'enum': ['error', 'null'], 'default': 'error',
            'description': "Conversion failure policy: fail the node, or emit null and a warning"}


def field_from_spec(spec: dict) -> Field:
    try:
        return Field(spec['name'], FieldType(spec['type']), spec.get('size'), spec.get('scale'))
    except SchemaError as e:
        raise ConfigError(e.message) from None


def require_fields(schema, names, what='field'):
    missing = [n for n in names if n not in schema]
    if missing:
        raise SchemaError(f'Unknown {what}(s) {missing}; available: {schema.names}')


def cast_column(values, field: Field, ctx, on_error: str = 'error', *, row_offset: int = 0):
    """Cast every value; apply the failure policy; summarise warnings once."""
    out, failures, truncations, first = [], 0, 0, None
    for i, v in enumerate(values):
        try:
            c, w = cast_value(v, field)
            if w:
                truncations += 1
        except CastFailure as e:
            if on_error == 'error':
                raise DataError(f'Field {field.name!r} row {i + 1 + row_offset}: {e}') from None
            failures += 1
            first = first or str(e)
            c = None
        out.append(c)
    if failures:
        ctx.warn(f'{failures} value(s) in {field.name!r} could not be converted to {field.type.value} and were set to null '
                 f'(first: {first})', field=field.name, count=failures)
    if truncations:
        ctx.warn(f'{truncations} value(s) in {field.name!r} truncated to {field.size} characters',
                 field=field.name, count=truncations)
    return out
